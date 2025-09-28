import socket
import select
import secrets
import string
import time
from typing import Dict, Set, Tuple, Optional

CREATE_CMD = b"CREATE"
JOIN_CMD = b"JOIN"
LEAVE_CMD = b"LEAVE"
PING_CMD = b"PING"
WHO_CMD = b"WHO"

ASCII_UP = bytes.maketrans(b"abcdefghijklmnopqrstuvwxyz", b"ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def token_upper(b: bytes) -> bytes:
    return b.translate(ASCII_UP)


MAX_CMD_LEN = max(len(CREATE_CMD), len(JOIN_CMD), len(LEAVE_CMD), len(PING_CMD), len(WHO_CMD))
MAX_CMD_WITH_PREFIX = 1 + MAX_CMD_LEN
MAX_PAYLOAD = 4096  # hard cap for processed messages


def split_cmd_arg(packet: bytes):
    """Extract command (uppercased) and remainder argument."""
    if not packet or packet[:1] != b'!':
        return b"", b""

    head = packet[:MAX_CMD_WITH_PREFIX + 1]
    sp = head.find(b" ")
    if sp == -1:
        tok = packet[1:]
        return token_upper(tok), b""

    tok = packet[1:sp]
    return token_upper(tok), packet[sp + 1:]


def random_group_id(length: int = 8) -> str:
    """Generate unique group IDs (letters/digits, excluding O/0)."""
    alphabet = string.ascii_uppercase.replace("O", "") + string.digits.replace("0", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


class UDPGroupServer:
    """
    UDP-based group chat server.

    Manages creation and cleanup of chat groups, tracks client membership
    and activity, forwards payloads between peers, and enforces group/client
    limits. Uses a heartbeat mechanism with sweeps to expire inactive clients
    and long-empty groups.

    Protocol:
      - Commands start with '!' and are case-insensitive:
          !CREATE             → create a new group, returns "OK CREATED <id>"
          !JOIN <id>          → join an existing group, returns "OK JOINED <id>" or error
          !LEAVE <id>         → leave a specific group, returns "OK LEFT <id>" or error
          !PING               → heartbeat, returns "PONG <seconds>"
          !WHO                → returns "OK WHO <group_id> <count>"

      - Group IDs:
          • 8 random chars (A–Z, 1–9), excluding 'O' and '0'
          • Case-insensitive when joining
          • Expire if empty for too long
          • Each client may only own a limited number of active groups

      - Payloads:
          • Any packet not starting with '!' is treated as a message
          • Broadcast to all peers in the same group
          • Payloads also refresh the sender's activity (count as heartbeat)

      - Inactivity:
          • Clients are removed after 3 × heartbeat interval with no activity
          • Empty groups removed after configured TTL
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
        # config
        self.host = host                                    # bind address
        self.port = port                                    # UDP port
        self.bufsize = bufsize                              # max recv size
        self.empty_group_ttl_sec = empty_group_ttl_sec      # how long empty groups survive
        self.sweep_interval_sec = sweep_interval_sec        # interval between cleanup sweeps
        self.suggested_heartbeat_sec = suggested_heartbeat_sec  # PONG heartbeat hint
        self.max_group_size = max_group_size                # global max peers per group
        self.per_group_caps = per_group_caps or {}          # optional per-group caps
        self.max_groups_per_client = max_groups_per_client  # cap on groups per creator

        # socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.sock.setblocking(False)

        # state tracking
        self.groups: Dict[str, Set[Tuple[str, int]]] = {}               # gid -> set of peers
        self.group_creator: Dict[str, Tuple[str, int]] = {}             # gid -> creator addr
        self.empty_since: Dict[str, float] = {}                         # gid -> empty-since timestamp
        self.creator_active_groups: Dict[Tuple[str, int], Set[str]] = {}  # creator addr -> active gids
        self.client_group: Dict[Tuple[str, int], str] = {}              # client addr -> joined gid
        self.last_seen: Dict[Tuple[str, int], float] = {}               # client addr -> last activity time

        # time tracking
        self.now = time.monotonic()                       # current monotonic timestamp
        self.next_sweep = self.now + self.sweep_interval_sec  # next cleanup time

        print(
            f"UDP group server listening on {self.host}:{self.port} "
            f"(max payload {self.bufsize} bytes)..."
        )

    # ---------- Helpers ----------

    def cap_for(self, gid: str) -> Optional[int]:
        """Look up per-group cap, fall back to global cap."""
        if gid in self.per_group_caps:
            return self.per_group_caps[gid]
        return self.max_group_size

    def mark_group_empty_if_needed(self, gid: str, now: float):
        """Track when groups become empty (for TTL cleanup)."""
        peers = self.groups.get(gid)
        if peers is not None and len(peers) == 0:
            self.empty_since[gid] = now
        else:
            self.empty_since.pop(gid, None)

    def remove_group(self, gid: str):
        """Delete group and update creator tracking."""
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
        """Forward message payloads to all peers in the group."""
        if len(packet) > MAX_PAYLOAD:
            self.sock.sendto(b"ERR Payload too large", addr)
            return

        gid = self.client_group.get(addr)
        if not gid or gid not in self.groups:
            self.client_group.pop(addr, None)
            self.sock.sendto(b"ERR Join an existing group first. Use: !JOIN <group_id>", addr)
            return

        self.last_seen[addr] = self.now
        peers = self.groups[gid]
        to_prune = []

        for peer in peers:
            if peer == addr:
                continue
            try:
                self.sock.sendto(packet, peer)
            except OSError:
                to_prune.append(peer)

        for peer in to_prune:
            peers.discard(peer)
            self.client_group.pop(peer, None)
            self.last_seen.pop(peer, None)

        self.mark_group_empty_if_needed(gid, self.now)

    def handle_create(self, addr: Tuple[str, int]):
        """Create a new group, enforcing per-client group cap."""
        active_set = self.creator_active_groups.get(addr)
        used = len(active_set) if active_set else 0
        if used >= self.max_groups_per_client:
            self.sock.sendto(
                f"ERR Group limit {used}/{self.max_groups_per_client}".encode(), addr
            )
            return

        gid = random_group_id()
        while gid in self.groups:
            gid = random_group_id()

        self.groups[gid] = set()
        self.group_creator[gid] = addr
        self.empty_since[gid] = self.now
        self.creator_active_groups.setdefault(addr, set()).add(gid)
        self.sock.sendto(("OK CREATED " + gid).encode(), addr)

    def handle_join(self, arg_bytes: bytes, addr: Tuple[str, int]):
        """Join an existing group, case-insensitive."""
        gid = arg_bytes.decode('utf-8', 'ignore').strip().upper()
        if not gid:
            self.sock.sendto(b"ERR Usage: !JOIN <group_id>", addr)
            return
        if gid not in self.groups:
            self.sock.sendto(b"ERR No such group", addr)
            return

        current = self.client_group.get(addr)
        if current == gid:
            self.last_seen[addr] = self.now
            self.sock.sendto(("OK JOINED " + gid).encode(), addr)
            return

        cap = self.cap_for(gid)
        if cap is not None and len(self.groups[gid]) >= cap:
            self.sock.sendto(("ERR Group full " + gid).encode(), addr)
            return

        if current and current in self.groups:
            self.groups[current].discard(addr)
            self.mark_group_empty_if_needed(current, self.now)

        self.client_group[addr] = gid
        self.groups[gid].add(addr)
        self.last_seen[addr] = self.now
        self.empty_since.pop(gid, None)
        self.sock.sendto(("OK JOINED " + gid).encode(), addr)

    def handle_leave(self, arg_bytes: bytes, addr: Tuple[str, int]):
        """Leave a specific group by ID (requires !LEAVE <id>)."""
        gid = arg_bytes.decode('utf-8', 'ignore').strip().upper()
        if not gid:
            self.sock.sendto(b"ERR Usage: !LEAVE <group_id>", addr)
            return

        current = self.client_group.get(addr)
        if not current or current.upper() != gid:
            self.sock.sendto(b"ERR Not in that group", addr)
            return

        self.client_group.pop(addr, None)

        if gid in self.groups:
            self.groups[gid].discard(addr)
            self.mark_group_empty_if_needed(gid, self.now)

        self.last_seen.pop(addr, None)
        self.sock.sendto(("OK LEFT " + gid).encode(), addr)

    def handle_ping(self, addr: Tuple[str, int]):
        """Heartbeat ping: refresh last_seen and reply with PONG."""
        gid = self.client_group.get(addr)
        if not gid or gid not in self.groups:
            self.client_group.pop(addr, None)
            self.sock.sendto(b"ERR Join an existing group first. Use: !JOIN <group_id>", addr)
            return

        self.last_seen[addr] = self.now
        self.sock.sendto(
            f"PONG {round(float(self.suggested_heartbeat_sec), 3)}".encode(), addr
        )

    def handle_who(self, addr: Tuple[str, int]):
        """Return group id and peer count for the caller's current group."""
        gid = self.client_group.get(addr)
        if not gid or gid not in self.groups:
            self.sock.sendto(b"ERR Join an existing group first. Use: !JOIN <group_id>", addr)
            return
        count = len(self.groups.get(gid, ()))
        self.sock.sendto(f"OK WHO {gid} {count}".encode(), addr)

    def handle_command(self, packet: bytes, addr: Tuple[str, int]):
        """Dispatch commands by token (with over-long token guard)."""
        cmd_up, arg_bytes = split_cmd_arg(packet)
        if not cmd_up:
            self.sock.sendto(b"ERR Unknown command", addr)
            return

        # Guard over-long or malformed command tokens (e.g., !THISISWAYTOOLONG)
        if len(cmd_up) > MAX_CMD_LEN:
            self.sock.sendto(b"ERR Unknown command", addr)
            return

        if cmd_up == token_upper(CREATE_CMD):
            self.handle_create(addr)
            return

        if cmd_up == token_upper(JOIN_CMD):
            self.handle_join(arg_bytes, addr)
            return

        if cmd_up == token_upper(LEAVE_CMD):
            self.handle_leave(arg_bytes, addr)
            return

        if cmd_up == token_upper(PING_CMD):
            self.handle_ping(addr)
            return

        if cmd_up == token_upper(WHO_CMD):
            self.handle_who(addr)
            return

        self.sock.sendto(b"ERR Unknown command", addr)

    def sweep(self):
        """Periodically clean up inactive clients and long-empty groups."""
        inactivity_limit_sec = 3 * self.suggested_heartbeat_sec
        cutoff_clients = self.now - inactivity_limit_sec
        cutoff_groups = self.now - self.empty_group_ttl_sec

        # Inactive clients
        for a, seen_at in list(self.last_seen.items()):
            if seen_at < cutoff_clients:
                gid = self.client_group.pop(a, None)
                self.last_seen.pop(a, None)
                if gid and gid in self.groups:
                    self.groups[gid].discard(a)
                    self.mark_group_empty_if_needed(gid, self.now)

        # Long-empty groups
        for gid, since in list(self.empty_since.items()):
            if since < cutoff_groups and gid in self.groups and len(self.groups[gid]) == 0:
                self.remove_group(gid)

    # ---------- Main loop ----------

    def run(self):
        while True:
            self.now = time.monotonic()
            timeout = max(0.0, self.next_sweep - self.now)

            readable, _, _ = select.select([self.sock], [], [], timeout)

            if readable:
                try:
                    packet, addr = self.sock.recvfrom(self.bufsize)
                except BlockingIOError:
                    packet, addr = None, None

                if not packet or addr is None:
                    continue

                if len(packet) == 0:
                    continue

                if packet[0:1] != b'!':
                    self.handle_payload(packet, addr)
                    continue

                self.handle_command(packet, addr)
                continue

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
    """Convenience wrapper to match old API."""
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
