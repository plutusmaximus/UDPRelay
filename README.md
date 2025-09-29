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

---

## Protocol

The chat system uses **UDP datagrams**.  
Packets fall into two categories:

1. **Commands**  
   - Always start with `!` (exclamation mark).  
   - **Commands are case-sensitive and must be written in ALL CAPS.**  
   - Arguments (like group IDs) follow the command, separated by spaces.  
   - Unknown or malformed commands result in an error response.  
   - Commands and responses are text-based (UTF-8).

2. **Payload messages**  
   - Any packet not starting with `!` is treated as a chat payload.  
   - Payloads are broadcast to all peers in the same group as the sender.  
   - Maximum payload size: **4096 bytes** (packets above this size are rejected).  
   - Payloads are binary-safe; the server relays them verbatim without decoding.

### Commands

| Command            | Reply format          | Notes                                 |
|--------------------|-----------------------|---------------------------------------|
| `!CREATE`          | `OK CREATED <id>`     | Create a new group (random 8-char ID) |
| `!JOIN <group_id>` | `OK JOINED <id>`      | Join an existing group                 |
| `!LEAVE <group_id>`| `OK LEFT <id>`        | Leave the given group                  |
| `!PING`            | `PONG <seconds>`      | Heartbeat; server suggests interval    |
| `!WHO`             | `OK WHO <id> <count>` | Show member count in your current group |

### Errors

Errors use a structured envelope: `ERR <CODE> <Message>`, where `<CODE>` is machine-friendly and `<Message>` is a short human-readable string.

Examples:  
- `ERR BAD_CMD UnknownCommand`  
- `ERR BAD_ARG Usage:!JOIN <GROUPID>`  
- `ERR NOT_IN_GROUP JoinFirstUseJOIN`  
- `ERR GROUP_FULL ABCD1234`  
- `ERR TOO_LARGE PayloadTooLarge`  

### Inactivity and Cleanup

- Clients are removed after **3 × heartbeat interval** with no **activity** (either payloads or pings).  
- Empty groups are deleted after the configured TTL.  
- Group IDs expire when their group is deleted; IDs may be reused by future groups.  

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
git clone https://github.com/plutusmaximus/UDPRelay.git
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
- Commands are **ALL CAPS and case-sensitive**.  
- Over-long or unknown commands return `ERR BAD_CMD UnknownCommand`.  
- CLI forbids raw `!` input — always use helper commands.  
- Code is formatted for readability, with concise comments and docstrings.
