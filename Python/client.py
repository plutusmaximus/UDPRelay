import socket
import select
import threading
import time
import shlex
import re
import random
from typing import Optional

MAX_PAYLOAD = 4096
DEFAULT_HEARTBEAT_SEC = 60.0


class UDPCLIClient:
    """Interactive CLI client for UDPRelay."""

    def __init__(self, server_host="127.0.0.1", server_port=5000, recv_buf=MAX_PAYLOAD):
        # configure UDP socket
        self.server = (server_host, server_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)
        self.recv_buf = recv_buf

        # state
        self.joined_group: Optional[str] = None
        self.last_created_group_id: Optional[str] = None
        self.heartbeat_interval = DEFAULT_HEARTBEAT_SEC
        self._stop = threading.Event()

        # heartbeat smoothing timestamp
        self._last_ping_ts = 0.0

        # background threads
        self._rx_t = threading.Thread(target=self._recv_loop, daemon=True)
        self._hb_t = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._rx_t.start()
        self._hb_t.start()

    # ---- protocol send helpers (always ALL CAPS commands) ----

    def send_text(self, text: str):
        """Send raw text to server."""
        # Reset heartbeat timer on any user send (smoothing)
        self._last_ping_ts = time.monotonic()
        self.sock.sendto(text.encode(), self.server)

    def create_group(self):
        self.send_text("!CREATE")

    def join(self, gid: str):
        self.send_text(f"!JOIN {gid}")

    def leave(self, gid: str):
        self.send_text(f"!LEAVE {gid}")

    def ping(self):
        self.send_text("!PING")

    def who(self):
        self.send_text("!WHO")

    # ---- background loops ----

    def _recv_loop(self):
        """Listen for server responses and payloads."""
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.sock], [], [], 0.1)
                if not r:
                    continue
                data, _ = self.sock.recvfrom(self.recv_buf)
            except OSError:
                break

            msg = data.decode(errors="ignore")

            # handle server messages
            if msg.startswith("OK CREATED"):
                parts = msg.split(maxsplit=2)
                if len(parts) >= 3:
                    self.last_created_group_id = parts[2]
                print(f"[server] {msg}")
                continue

            if msg.startswith("OK JOINED"):
                parts = msg.split(maxsplit=2)
                if len(parts) >= 3:
                    self.joined_group = parts[2]
                print(f"[server] {msg}")
                continue

            if msg.startswith("OK LEFT"):
                self.joined_group = None
                print(f"[server] {msg}")
                continue

            if msg.startswith("OK WHO"):
                parts = msg.split()
                if len(parts) >= 4:
                    gid, count = parts[2], parts[3]
                    print(f"[server] group={gid} peers={count}")
                else:
                    print(f"[server] {msg}")
                continue

            if msg.startswith("PONG"):
                parts = msg.split()
                if len(parts) >= 2:
                    try:
                        secs = float(parts[1])
                        if secs > 0:
                            self.heartbeat_interval = secs
                            # reset timer with small jitter to avoid herding
                            self._last_ping_ts = time.monotonic() - random.uniform(0.0, 0.1 * secs)
                    except ValueError:
                        pass
                print(f"[server] {msg}")
                continue

            if msg.startswith("ERR"):
                m = re.match(r"^ERR\s+([A-Z_]+)\s+(.*)$", msg.strip())
                if m:
                    code, detail = m.group(1), m.group(2)
                    print(f"[server] error: {code} - {detail}")
                else:
                    print(f"[server] {msg}")
                continue

            # otherwise, treat as payload
            print(f"[payload] {msg}")

    def _heartbeat_loop(self):
        """Send periodic pings while joined to a group."""
        while not self._stop.is_set():
            now = time.monotonic()
            interval = max(1.0, self.heartbeat_interval)
            if self.joined_group and (now - self._last_ping_ts) >= interval:
                try:
                    self.ping()
                except OSError:
                    pass
                self._last_ping_ts = now
            time.sleep(0.25)

    def close(self):
        """Stop threads and close socket."""
        self._stop.set()
        try:
            self.sock.close()  # unblock select
        except OSError:
            pass
        if self._rx_t.is_alive():
            self._rx_t.join(timeout=0.5)
        if self._hb_t.is_alive():
            self._hb_t.join(timeout=0.5)


def run_udp_client_cli(server_host="127.0.0.1", server_port=5000):
    """Run interactive CLI for UDPRelay."""
    print(f"Connecting to server at {server_host}:{server_port} ...")
    client = UDPCLIClient(server_host, server_port)
    try:
        print("Type 'help' for commands. Messages that don't start with '!' are sent as payloads.")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue

            if line.lower() in ("quit", "exit"):
                break

            if line.lower() == "help":
                print(
                    "Local helpers:\n"
                    "  create [--join]\n"
                    "  join <id>\n"
                    "  leave <id>\n"
                    "  who\n"
                    "  ping\n"
                    "  status\n"
                    "  quit/exit\n"
                    "  (messages that don't start with '!' are broadcast to your current group)\n"
                )
                continue

            if line.lower() == "status":
                def _eta():
                    interval = max(1.0, client.heartbeat_interval)
                    return max(0.0, interval - (time.monotonic() - client._last_ping_ts))
                print(
                    f"[cli] server={client.server[0]}:{client.server[1]} "
                    f"group={client.joined_group or '<none>'} "
                    f"last_created={client.last_created_group_id or '<none>'} "
                    f"heartbeat_interval={client.heartbeat_interval:.3f}s "
                    f"next_ping_eta={_eta():.2f}s"
                )
                continue

            # guard against raw '!' commands
            if line.startswith("!"):
                print("[cli] error: direct '!' commands are not allowed. Use helpers like 'create', 'join', 'leave', 'ping', or 'who'.")
                continue

            # parse local helper command
            try:
                parts = shlex.split(line)
            except ValueError as e:
                print(f"[cli] parse error: {e}")
                continue

            if not parts:
                continue

            cmd, *args = parts

            if cmd == "create":
                auto_join = "--join" in args
                client.create_group()
                deadline = time.time() + 2.0
                while client.last_created_group_id is None and time.time() < deadline:
                    time.sleep(0.05)
                if client.last_created_group_id:
                    print(f"[cli] created group id: {client.last_created_group_id}")
                    if auto_join:
                        client.join(client.last_created_group_id)
                else:
                    print("[cli] create timed out")
                continue

            if cmd == "join":
                if args:
                    client.join(args[0].upper())
                else:
                    print("[cli] usage: join <group_id>")
                continue

            if cmd == "leave":
                if args:
                    client.leave(args[0].upper())
                else:
                    print("[cli] usage: leave <group_id>")
                continue

            if cmd == "who":
                client.who()
                continue

            if cmd == "ping":
                client.ping()
                continue

            if not client.joined_group:
                print("[cli] not joined; use 'join <group_id>' or 'create --join'")
                continue

            # default: treat input as payload
            client.send_text(line)
    finally:
        client.close()
        print("Goodbye.")
