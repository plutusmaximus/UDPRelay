# UDPRelay
A lightweight **UDP-based group chat system** written in Python, with a command-line interface (CLI) client and a simple broadcast server.  
Supports dynamic group creation, joining, leaving, and automatic cleanup of inactive clients and empty groups.

---

## Features

- **Server**
  - Groups are created with 8-character IDs (`A–Z`, `1–9`, excluding `O` and `0`).
  - Each client can own a limited number of groups (`--max-groups-per-client`).
  - Group size caps supported globally or per group.
  - Inactivity cleanup:
    - Clients removed after `3 × heartbeat interval` with no activity.
    - Empty groups removed after a configurable TTL.
  - Heartbeat support with `!PING → PONG <seconds>`.

- **Client**
  - Interactive CLI for sending messages and running commands.
  - Helpers for `create`, `join`, `leave`, `ping`, `who`, `status`, `quit`.
  - Payload messages broadcast to all peers in the same group.
  - Automatic heartbeat pings at the server’s suggested interval.
  - Guard against sending raw `!` commands — encourages use of helpers.

- **Commands (case-insensitive, start with `!`)**
  - `!CREATE` → create a new group (`OK CREATED <id>`).
  - `!JOIN <id>` → join an existing group (`OK JOINED <id>`).
  - `!LEAVE <id>` → leave a specific group (`OK LEFT <id>`).
  - `!PING` → refresh activity, server replies with `PONG <seconds>`.
  - `!WHO` → query your current group’s member count (`OK WHO <id> <count>`).

---

## Repository Structure

```
.
├── server.py   # UDPGroupServer class and broadcast loop
├── client.py   # UDPCLIClient with interactive CLI
├── chat.py     # Entry point wrapper for running server or client
└── README.md   # You are here
```

---

## Installation

Requires **Python 3.8+**.

Clone the repository:

```bash
git clone [https://github.com/your-username/udp-group-chat.git](https://github.com/plutusmaximus/UDPRelay.git)
cd UDPRelay
```

No external dependencies — only Python standard library modules are used.

---

## Usage

### Start the Server

```bash
python chat.py --server --host 0.0.0.0 --port 5000
```

**Server options:**

- `--empty-ttl` : seconds until empty groups expire (default: 300)
- `--sweep` : cleanup sweep interval (default: 30)
- `--heartbeat` : suggested heartbeat interval in seconds (default: 60)
- `--cap` : maximum group size (default: 128, `None` to disable)
- `--max-groups-per-client` : limit active groups per creator (default: 3)

---

### Run the Client

```bash
python chat.py --host 127.0.0.1 --port 5000
```

You’ll enter an interactive prompt:

```
Connecting to server at 127.0.0.1:5000 ...
Type 'help' for commands. Messages that don't start with '!' are sent as payloads.
> 
```

---

### Client Commands

Local CLI helpers:

```
create [--join]    # Create a new group, optionally auto-join
join <id>          # Join an existing group
leave <id>         # Leave a group
who                # Show your group’s member count
ping               # Send heartbeat ping
status             # Show current status
quit / exit        # Disconnect and quit
```

Messages that don’t start with `!` are sent as chat payloads.

---

## Example Session

1. **Start server**:
   ```bash
   python chat.py --server
   ```

2. **Open two clients**:
   ```bash
   python chat.py
   ```

3. **In client A**:
   ```
   > create --join
   [cli] created group id: ABCD1234
   [server] OK CREATED ABCD1234
   [server] OK JOINED ABCD1234
   ```

4. **In client B**:
   ```
   > join ABCD1234
   [server] OK JOINED ABCD1234
   ```

5. **Chat**:
   ```
   > hello from A
   [payload] hello from A
   ```

---

## Development Notes

- Payload size is clamped to **4 KiB** on both server and client.
- Over-long or unknown commands return `ERR Unknown command`.
- CLI forbids raw `!` input — always use helper commands.
- Code is formatted for readability, with concise comments and docstrings.

---
