import socket
import select
import secrets
import string
import time
import sys
from typing import Dict, Set, Tuple, Optional

# ---- Command tokens (CASE-SENSITIVE, ALL CAPS) ----
CREATE_CMD = b"CREATE"
JOIN_CMD = b"JOIN"
LEAVE_CMD = b"LEAVE"
PING_CMD = b"PING"
WHO_CMD = b"WHO"

# Max command length for guards
MAX_CMD_LEN = max(len(CREATE_CMD), len(JOIN_CMD), len(LEAVE_CMD), len(PING_CMD), len(WHO_CMD))
MAX_CMD_WITH_PREFIX = 1 + MAX_CMD_LEN
MAX_PAYLOAD = 4096  # hard cap for processed messages

# ---- Reply helpers / codes ----
def OK(detail: str = "OK") -> bytes:
    return f"OK {detail}".encode()

def ERR(code: str, msg: str) -> bytes:
    return f"ERR {code} {msg}".encode()

ERR_BAD_CMD = "BAD_CMD"
ERR_BAD_ARG = "BAD_ARG"
ERR_NOT_IN_GROUP = "NOT_IN_GROUP"
ERR_ALREADY_IN_GROUP = "ALREADY_IN_GROUP"
ERR_GROUP_FULL = "GROUP_FULL"
ERR_CLIENT_LIMIT = "CLIENT_LIMIT"
ERR_TOO_LARGE = "TOO_LARGE"
ERR_INTERNAL = "INTERNAL"


def split_cmd_arg(packet: bytes):
    """
    Extract command token (CASE-SENSITIVE, ALL CAPS) and remainder argument.
    Returns (cmd_token_bytes, arg_bytes). If not a command, returns (b"", b"").
    """
    if not packet or packet[:1] != b'!':
        return b"", b""

    head = packet[:MAX_CMD_WITH_PREFIX + 1]
    sp = head.find(b" ")
    if sp == -1:
        tok = packet[1:]
        return tok, b""

    tok = packet[1:sp]
    return tok, packet[sp + 1:]


def random_group_id(length: int = 8) -> str:
    """Generate group IDs (A–Z, 1–9), excluding 'O' and '0'."""
    alphabet = string.ascii_uppercase.replace("O", "") + string.digits.replace("0", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


class UDPGroupServer:
    """
    UDPRelay server.

    Protocol:
      - Commands: CASE-SENSITIVE ALL CAPS, start with '!' (e.g., !JOIN ABCD1234)
          !CREATE             → "OK CREATED <id>"
          !JOIN <id>          → "OK JOINED <id>" or error
          !LEAVE <id>         → "OK LEFT <id>" or error
          !PING               → "PONG <seconds>"
          !WHO                → "OK WHO <id> <count>"
    """

    def __init__(
        self,
        host: str = '0.0.0.0',
        port: int = 5000,
        bufsize: int = MAX_PAYLOAD,
        empty_group_ttl_sec: float = 300.0,
        sweep_interval_sec: float = 30.0,
        suggested_heartbeat_sec: float = 60.0,
        max_group_size: Optional[int] = 128,
        per_group_caps: Optional[Dict[str, int]] = None,
        max_groups_per_client: int = 3,
    ):
        # --- configuration ---
        self.host = host                         # bind address
        self.port = port                         # UDP port to listen on
        self.bufsize = bufsize                   # max receive buffer size
        self.empty_group_ttl_sec = empty_group_ttl_sec  # how long empty groups survive
        self.sweep_interval_sec = sweep_interval_sec    # interval between cleanup sweeps
        self.suggested_heartbeat_sec = suggested_heartbeat_sec  # PONG heartbeat hint
        self.max_group_size = max_group_size     # global max peers per group
        self.per_group_caps = per_group_caps or {}  # optional per-group caps
        self.max_groups_per_client = max_groups_per_client  # cap on groups per creator

        # --- UDP socket setup ---
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # UDP socket
        self.sock.bind((self.host, self.port))                        # bind to address/port
        self.sock.setblocking(False)                                  # non-blocking mode

        # --- state tracking ---
        self.groups: Dict[str, Set[Tuple[str, int]]] = {}  # gid → set of peer addresses
        self.group_creator: Dict[str, Tuple[str, int]] = {}  # gid → creator’s address
        self.empty_since: Dict[str, float] = {}  # gid → timestamp when last emptied
        self.creator_active_groups: Dict[Tuple[str, int], Set[str]] = {}  # creator → set of active gids
        self.client_group: Dict[Tuple[str, int], str] = {}  # client address → joined gid
        self.last_seen: Dict[Tuple[str, int], float] = {}  # client address → last activity timestamp

        # --- timers ---
        self.now = time.monotonic()             # current monotonic time
        self.next_sweep = self.now + self.sweep_interval_sec  # when next cleanup sweep should run

        print(
            f"UDPRelay server listening on {self.host}:{self.port} "
            f"(max payload {self.bufsize} bytes)..."
        )
        # simple observability counters
        self.stats = {
            "packets_rx": 0,
            "packets_tx": 0,
            "drops_oversize": 0,
            "joins": 0,
            "leaves": 0,
            "creates": 0,
        }

    # ---------- Helpers ----------

    def cap_for(self, gid: str) -> Optional[int]:
        """Return per-group cap if defined, else global cap."""
        return self.per_group_caps.get(gid, self.max_group_size)

    def mark_group_empty_if_needed(self, gid: str, now: float):
        """Mark group as empty if no peers left (for TTL cleanup)."""
        peers = self.groups.get(gid)
        if peers is not None and len(peers) == 0:
            self.empty_since[gid] = now
        else:
            self.empty_since.pop(gid, None)

    def remove_group(self, gid: str):
        """Remove group and update creator tracking."""
        self.groups.pop(gid, None)
        self.empty_since.pop(gid, None)
        creator = self.group_creator.pop(gid, None)
        if creator is not None:
            s = self.creator_active_groups.get(creator)
            if s is not None:
                s.discard(gid)
                if not s:
                    self.creator_active_groups.pop(creator, None)

    # ---------- Packet handling ----------

    def handle_payload(self, packet: bytes, addr: Tuple[str, int]):
        """Forward payload to all peers in the sender's group."""
        if len(packet) > MAX_PAYLOAD:
            self.sock.sendto(ERR(ERR_TOO_LARGE, "PayloadTooLarge"), addr)
            self.stats["drops_oversize"] += 1
            return

        gid = self.client_group.get(addr)
        if not gid or gid not in self.groups:
            self.client_group.pop(addr, None)
            self.sock.sendto(ERR(ERR_NOT_IN_GROUP, "JoinFirstUseJOIN"), addr)
            return

        # update activity
        self.last_seen[addr] = self.now
        peers = self.groups[gid]
        to_prune = []

        for peer in peers:
            if peer == addr:
                continue
            try:
                self.sock.sendto(packet, peer)
                self.stats["packets_tx"] += 1
            except OSError:
                to_prune.append(peer)

        for peer in to_prune:
            peers.discard(peer)
            self.client_group.pop(peer, None)
            self.last_seen.pop(peer, None)

        self.mark_group_empty_if_needed(gid, self.now)

    def handle_create(self, addr: Tuple[str, int]):
        """Create a new group, respecting per-creator group caps."""
        active_set = self.creator_active_groups.get(addr)
        used = len(active_set) if active_set else 0
        if used >= self.max_groups_per_client:
            self.sock.sendto(ERR(ERR_CLIENT_LIMIT, f"{used}/{self.max_groups_per_client}"), addr)
            return

        # ensure unique group ID
        gid = random_group_id()
        while gid in self.groups:
            gid = random_group_id()

        self.groups[gid] = set()
        self.group_creator[gid] = addr
        self.empty_since[gid] = self.now
        self.creator_active_groups.setdefault(addr, set()).add(gid)
        self.sock.sendto(OK(f"CREATED {gid}"), addr)
        self.stats["creates"] += 1
        print(f"[INFO] create gid={gid} by={addr}", file=sys.stderr)

    def handle_join(self, arg_bytes: bytes, addr: Tuple[str, int]):
        """Join an existing group (group IDs compared case-insensitively)."""
        gid = arg_bytes.decode('utf-8', 'ignore').strip().upper()
        if not gid:
            self.sock.sendto(ERR(ERR_BAD_ARG, "Usage:!JOIN <GROUPID>"), addr)
            return
        if gid not in self.groups:
            self.sock.sendto(ERR(ERR_BAD_ARG, "NoSuchGroup"), addr)
            return

        current = self.client_group.get(addr)
        if current == gid:
            # already in group
            self.last_seen[addr] = self.now
            self.sock.sendto(OK(f"JOINED {gid}"), addr)
            return

        # enforce caps
        cap = self.cap_for(gid)
        if cap is not None and len(self.groups[gid]) >= cap:
            self.sock.sendto(ERR(ERR_GROUP_FULL, gid), addr)
            return

        # leave old group if needed
        if current and current in self.groups:
            self.groups[current].discard(addr)
            self.mark_group_empty_if_needed(current, self.now)

        # join new group
        self.client_group[addr] = gid
        self.groups[gid].add(addr)
        self.last_seen[addr] = self.now
        self.empty_since.pop(gid, None)
        self.sock.sendto(OK(f"JOINED {gid}"), addr)
        self.stats["joins"] += 1
        print(f"[INFO] join gid={gid} addr={addr} size={len(self.groups[gid])}", file=sys.stderr)

    def handle_leave(self, arg_bytes: bytes, addr: Tuple[str, int]):
        """Leave a specific group (requires !LEAVE <id>)."""
        gid = arg_bytes.decode('utf-8', 'ignore').strip().upper()
        if not gid:
            self.sock.sendto(ERR(ERR_BAD_ARG, "Usage:!LEAVE <GROUPID>"), addr)
            return

        current = self.client_group.get(addr)
        if not current or current.upper() != gid:
            self.sock.sendto(ERR(ERR_NOT_IN_GROUP, "NotInThatGroup"), addr)
            return

        # drop client
        self.client_group.pop(addr, None)

        if gid in self.groups:
            self.groups[gid].discard(addr)
            self.mark_group_empty_if_needed(gid, self.now)

        self.last_seen.pop(addr, None)
        self.sock.sendto(OK(f"LEFT {gid}"), addr)
        self.stats["leaves"] += 1
        print(f"[INFO] leave gid={gid} addr={addr} size={len(self.groups.get(gid,set()))}", file=sys.stderr)

    def handle_ping(self, addr: Tuple[str, int]):
        """Refresh last_seen and reply with heartbeat hint."""
        gid = self.client_group.get(addr)
        if not gid or gid not in self.groups:
            self.client_group.pop(addr, None)
            self.sock.sendto(ERR(ERR_NOT_IN_GROUP, "JoinFirstUseJOIN"), addr)
            return

        self.last_seen[addr] = self.now
        self.sock.sendto(
            f"PONG {round(float(self.suggested_heartbeat_sec), 3)}".encode(), addr
        )

    def handle_who(self, addr: Tuple[str, int]):
        """Return group ID and peer count for the caller's group."""
        gid = self.client_group.get(addr)
        if not gid or gid not in self.groups:
            self.sock.sendto(ERR(ERR_NOT_IN_GROUP, "JoinFirstUseJOIN"), addr)
            return
        count = len(self.groups.get(gid, ()))
        self.sock.sendto(OK(f"WHO {gid} {count}"), addr)

    def handle_command(self, packet: bytes, addr: Tuple[str, int]):
        """Dispatch commands by token, with over-long guard."""
        cmd, arg_bytes = split_cmd_arg(packet)
        if not cmd:
            self.sock.sendto(ERR(ERR_BAD_CMD, "UnknownCommand"), addr)
            return

        if len(cmd) > MAX_CMD_LEN:
            self.sock.sendto(ERR(ERR_BAD_CMD, "UnknownCommand"), addr)
            return

        if cmd == CREATE_CMD:
            self.handle_create(addr)
            return
        if cmd == JOIN_CMD:
            self.handle_join(arg_bytes, addr)
            return
        if cmd == LEAVE_CMD:
            self.handle_leave(arg_bytes, addr)
            return
        if cmd == PING_CMD:
            self.handle_ping(addr)
            return
        if cmd == WHO_CMD:
            self.handle_who(addr)
            return

        self.sock.sendto(ERR(ERR_BAD_CMD, "UnknownCommand"), addr)

    def sweep(self):
        """Cleanup inactive clients and long-empty groups."""
        inactivity_limit_sec = 3 * self.suggested_heartbeat_sec
        cutoff_clients = self.now - inactivity_limit_sec
        cutoff_groups = self.now - self.empty_group_ttl_sec

        # prune inactive clients
        for a, seen_at in list(self.last_seen.items()):
            if seen_at < cutoff_clients:
                gid = self.client_group.pop(a, None)
                self.last_seen.pop(a, None)
                if gid and gid in self.groups:
                    self.groups[gid].discard(a)
                    self.mark_group_empty_if_needed(gid, self.now)

        # prune long-empty groups
        for gid, since in list(self.empty_since.items()):
            if since < cutoff_groups and gid in self.groups and len(self.groups[gid]) == 0:
                self.remove_group(gid)

    # ---------- Main loop ----------

    def run(self):
        """Main server loop: wait for packets, process, sweep."""
        while True:
            self.now = time.monotonic()
            timeout = max(0.0, self.next_sweep - self.now)

            readable, _, _ = select.select([self.sock], [], [], timeout)

            if readable:
                try:
                    # read one byte past cap to detect oversize
                    packet, addr = self.sock.recvfrom(self.bufsize + 1)
                except BlockingIOError:
                    packet, addr = None, None

                if not packet or addr is None:
                    continue

                if len(packet) == 0:
                    continue

                # global oversize guard (binary-safe)
                if len(packet) > MAX_PAYLOAD:
                    self.sock.sendto(ERR(ERR_TOO_LARGE, "PayloadTooLarge"), addr)
                    self.stats["drops_oversize"] += 1
                    continue

                self.stats["packets_rx"] += 1

                if packet[0:1] != b'!':
                    self.handle_payload(packet, addr)
                    continue

                self.handle_command(packet, addr)
                continue

            # time for sweep
            if self.now >= self.next_sweep:
                self.sweep()
                self.next_sweep = self.now + self.sweep_interval_sec


def udp_group_broadcast_server(
    host: str = '0.0.0.0',
    port: int = 5000,
    bufsize: int = MAX_PAYLOAD,
    empty_group_ttl_sec: float = 300.0,
    sweep_interval_sec: float = 30.0,
    suggested_heartbeat_sec: float = 60.0,
    max_group_size: Optional[int] = 128,
    per_group_caps: Optional[Dict[str, int]] = None,
    max_groups_per_client: int = 3,
):
    """Convenience wrapper for running a UDPRelay server."""
    server = UDPGroupServer(
        host=host,
        port=port,
        bufsize=bufsize,
        empty_group_ttl_sec=empty_group_ttl_sec,
        sweep_interval_sec=sweep_interval_sec,
        suggested_heartbeat_sec=suggested_heartbeat_sec,
        max_group_size=max_group_size,
        per_group_caps=per_group_caps,
        max_groups_per_client=max_groups_per_client,
    )
    server.run()
