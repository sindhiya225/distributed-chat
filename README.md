#  Distributed Chat Application with Fault Tolerance

A production-grade distributed messaging system built from scratch in Python, implementing core distributed computing concepts: leader election, message replication, Lamport logical clocks, and fault-tolerant failover.

---

##  Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Chat Cluster                           │
│                                                             │
│   ┌──────────┐     ┌──────────┐     ┌──────────┐           │
│   │ Server S3│◄───►│ Server S2│◄───►│ Server S1│           │
│   │  :9001   │     │  :9002   │     │  :9003   │           │
│   │ (LEADER) │     │          │     │          │           │
│   └────┬─────┘     └────┬─────┘     └────┬─────┘           │
│        │               │               │                   │
└────────┼───────────────┼───────────────┼───────────────────┘
         │               │               │
      Client A        Client B        Client C
```

### What happens on a crash:

```
Normal:    S3(leader) ─── S2 ─── S1
Crash S3:  [S3 DEAD]  ─── S2 ─── S1
                           │
                    S2 detects S3 gone (heartbeat timeout)
                    S2 starts Bully Election
                    S2 becomes new leader
                           │
           Clients reconnect → S2(leader) ─── S1
```

---

##  Distributed Computing Concepts Applied

| Concept | Implementation |
|---------|---------------|
| **Leader Election** | Bully Algorithm — highest server ID wins |
| **Fault Tolerance** | Heartbeat monitoring + automatic failover |
| **Message Ordering** | Lamport Logical Clocks on every message |
| **Replication** | Every message is replicated to all peer servers |
| **State Sync** | New/rejoining servers request full message history |
| **Client Failover** | Client auto-reconnects to another server on crash |
| **Concurrent Messaging** | Full `asyncio` event loop — no blocking threads |

---

##  Algorithm Details

### Bully Algorithm (Leader Election)
1. Any server can start an election by sending `ELECTION` to all higher-ID servers
2. If a higher server is alive, it replies with `OK` and starts its own election
3. If no `OK` arrives within 3 seconds → that server declares itself `COORDINATOR`
4. `COORDINATOR` is broadcast to all peers; all peers update their `leader_id`
5. On startup AND on detecting a crashed leader → election is triggered

### Lamport Logical Clocks (Message Ordering)
- Every message carries a `logical_clock` value
- On send: `clock = clock + 1`
- On receive: `clock = max(local, received) + 1`
- Messages displayed with `[lc=N]` so ordering is visible to users

### Heartbeat-Based Fault Detection
- Every server sends a `HEARTBEAT` to all peers every 2 seconds
- Peers reply with `HEARTBEAT_ACK` and update `last_heartbeat` timestamp
- Fault detector runs every 5 seconds; if `now - last_heartbeat > 6s` → peer marked dead
- If the dead peer was the leader → new election is triggered immediately

### Message Replication
- When a server receives a `CHAT` message from a client it:
  1. Assigns a Lamport clock value
  2. Stores it in its local `message_log` (keyed by `msg_id`)
  3. Delivers it to all locally-connected clients
  4. Replicates via `REPLICATE` message to all peer servers
- Duplicate detection: if `msg_id` already in log → ignored (idempotent)

---

##  Quick Start

### Requirements
- Python 3.9+
- No external dependencies (pure stdlib: `asyncio`, `json`, `uuid`)

### 1. Start the servers (3 separate terminals)

```bash
# Terminal 1 — Server S3 (will be initial leader — highest ID)
python3 server/server.py --id S3 --port 9001 --peers "S2:127.0.0.1:9002,S1:127.0.0.1:9003"

# Terminal 2 — Server S2
python3 server/server.py --id S2 --port 9002 --peers "S3:127.0.0.1:9001,S1:127.0.0.1:9003"

# Terminal 3 — Server S1
python3 server/server.py --id S1 --port 9003 --peers "S3:127.0.0.1:9001,S2:127.0.0.1:9002"
```

### 2. Connect clients (more terminals)

```bash
# Terminal 4
python3 client/client.py --username Alice

# Terminal 5
python3 client/client.py --username Bob

# Connect to a specific server (optional)
python3 client/client.py --username Charlie --servers "127.0.0.1:9002"
```

### 3. Live Crash Demo (automated)

```bash
python3 demo.py
```

This script will:
- Start all 3 servers
- Wait for leader election
- Crash the leader on your command
- Show re-election happening in real time
- Restart the crashed server to demonstrate sync

### 4. Web Application

```bash
python3 bridge.py
-- open index.html from directory
```

---

##  Manual Fault Tolerance Demo

1. Start all 3 servers and 2+ clients
2. Send some messages — note the `[lc=N]` Lamport clock values
3. Find and kill the leader server process: `kill -9 <PID>`
4. Watch the remaining servers detect failure and elect a new leader
5. Your clients will **automatically reconnect** to a live server
6. Chat continues — no messages lost!
7. Restart the killed server — it will sync all missed messages

---

##  Project Structure

```
distributed-chat/
├── common/
│   └── protocol.py        # Message types, serialization, ChatEntry
├── server/
│   └── server.py          # Full server: election, replication, heartbeat
├── client/
│   └── client.py          # Terminal client with auto-reconnect
├── demo.py                # Automated crash demo script
├── index.html
├── bridge.py
└── README.md
```

---

### Screenshots

<img width="1919" height="864" alt="image" src="https://github.com/user-attachments/assets/aff6c6d4-46a7-4e0c-bbc0-ecb53ed18366" />

<img width="1919" height="860" alt="image" src="https://github.com/user-attachments/assets/93716135-a7a0-44ae-8d29-617af1802e9e" />

<img width="1287" height="792" alt="image" src="https://github.com/user-attachments/assets/e5911789-340b-4146-9442-c6dc0347795e" />

---
 

##  Academic Concepts Covered

- **CAP Theorem**: This system prioritizes **Availability + Partition Tolerance** (AP). During an election, the system stays available; strict consistency is eventual.
- **Replication**: Active (eager) replication — all servers receive every message immediately.
- **Fault Models**: Crash-stop failure model assumed (servers either work or stop).
- **Clock Synchronization**: Lamport timestamps provide partial ordering without physical clock sync.

---

##  Extending the Project

- Add **Raft or Paxos** for strong consistency (replace replication logic)
- Add **vector clocks** for causal ordering across clients
- Add **persistent storage** (SQLite) so servers survive restarts with full history
- Add **load balancing** so the leader distributes client connections
- Build a **web UI** using WebSockets instead of raw TCP

---

*Built for Distributed Computing coursework — demonstrating Bully Algorithm, Lamport Clocks, heartbeat fault detection, and active replication.*
