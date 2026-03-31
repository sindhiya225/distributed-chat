"""
Distributed Chat Server
=======================
Implements:
  - Multi-server message replication
  - Bully Algorithm for leader election
  - Lamport logical clocks for message ordering
  - Heartbeat-based fault detection
  - Automatic failover on leader crash
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Dict, Optional, Set

from protocol import Message, MessageType, ChatEntry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Server-%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ─────────────────────────────────────────────
# Peer connection helper
# ─────────────────────────────────────────────

class PeerConnection:
    def __init__(self, server_id: str, host: str, port: int):
        self.server_id = server_id
        self.host = host
        self.port = port
        self.writer: Optional[asyncio.StreamWriter] = None
        self.alive = False
        self.last_heartbeat = 0.0

    async def connect(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=2.0
            )
            self.writer = writer
            self.alive = True
            self.last_heartbeat = time.time()
            return True
        except Exception:
            self.alive = False
            return False

    async def send(self, msg: Message) -> bool:
        if not self.alive or self.writer is None:
            return False
        try:
            self.writer.write(msg.serialize())
            await self.writer.drain()
            return True
        except Exception:
            self.alive = False
            return False

    def close(self):
        if self.writer:
            self.writer.close()
        self.alive = False


# ─────────────────────────────────────────────
# Main Server
# ─────────────────────────────────────────────

class ChatServer:
    def __init__(self, server_id: str, host: str, port: int, peers: list):
        self.server_id = server_id
        self.host = host
        self.port = port
        self.logger = logging.getLogger(server_id)

        # Peer registry  {server_id: PeerConnection}
        self.peers: Dict[str, PeerConnection] = {}
        for p in peers:
            self.peers[p["id"]] = PeerConnection(p["id"], p["host"], p["port"])

        # Leader election (Bully Algorithm)
        self.leader_id: Optional[str] = None
        self.election_in_progress = False
        self.election_ok_received = False

        # Lamport logical clock
        self.logical_clock = 0
        self._clock_lock = asyncio.Lock()

        # Replicated message log  {msg_id: ChatEntry}
        self.message_log: Dict[str, ChatEntry] = {}
        self._log_lock = asyncio.Lock()

        # Connected clients  {client_id: (reader, writer, username)}
        self.clients: Dict[str, tuple] = {}

        # Pending election OK timeout task
        self._election_timeout_task: Optional[asyncio.Task] = None

    # ── Lamport Clock ──────────────────────────────────────────────────────────

    async def tick(self) -> int:
        async with self._clock_lock:
            self.logical_clock += 1
            return self.logical_clock

    async def update_clock(self, received: int) -> int:
        async with self._clock_lock:
            self.logical_clock = max(self.logical_clock, received) + 1
            return self.logical_clock

    # ── Server Lifecycle ───────────────────────────────────────────────────────

    async def start(self):
        server = await asyncio.start_server(
            self.handle_connection, self.host, self.port
        )
        self.logger.info(f"Listening on {self.host}:{self.port}")

        # Give other servers a moment to start before connecting
        await asyncio.sleep(1)
        await self.connect_to_peers()

        # Start background tasks
        asyncio.create_task(self.heartbeat_loop())
        asyncio.create_task(self.fault_detector_loop())

        # Trigger initial election
        await asyncio.sleep(0.5)
        await self.start_election()

        async with server:
            await server.serve_forever()

    async def connect_to_peers(self):
        for peer in self.peers.values():
            connected = await peer.connect()
            if connected:
                self.logger.info(f"Connected to peer {peer.server_id}")
            else:
                self.logger.warning(f"Could not reach peer {peer.server_id} (may not be up yet)")

    # ── Connection Handler ─────────────────────────────────────────────────────

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles both peer-server and client connections."""
        client_id = None
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                msg = Message.deserialize(raw)
                await self.update_clock(msg.logical_clock)
                await self.dispatch(msg, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, json.JSONDecodeError):
            pass
        finally:
            if client_id and client_id in self.clients:
                del self.clients[client_id]
                await self.broadcast_system_message(f"[{client_id}] has left the chat.")
            writer.close()

    async def dispatch(self, msg: Message, reader, writer):
        """Route incoming messages to the right handler."""
        t = msg.type
        if t == MessageType.JOIN:
            await self.on_client_join(msg, reader, writer)
        elif t == MessageType.CHAT:
            await self.on_chat(msg, writer)
        elif t == MessageType.REPLICATE:
            await self.on_replicate(msg)
        elif t == MessageType.SYNC_REQUEST:
            await self.on_sync_request(msg, writer)
        elif t == MessageType.SYNC_RESPONSE:
            await self.on_sync_response(msg)
        elif t == MessageType.ELECTION:
            await self.on_election(msg, writer)
        elif t == MessageType.ELECTION_OK:
            await self.on_election_ok(msg)
        elif t == MessageType.COORDINATOR:
            await self.on_coordinator(msg)
        elif t == MessageType.HEARTBEAT:
            await self.on_heartbeat(msg, writer)
        elif t == MessageType.HEARTBEAT_ACK:
            await self.on_heartbeat_ack(msg)
        elif t == MessageType.LEAVE:
            username = msg.payload.get("username", msg.sender_id)
            del self.clients[msg.sender_id]
            await self.broadcast_system_message(f"👋 {username} left the chat.")

    # ── Client Events ──────────────────────────────────────────────────────────

    async def on_client_join(self, msg: Message, reader, writer):
        client_id = msg.sender_id
        username = msg.payload.get("username", client_id)
        self.clients[client_id] = (reader, writer, username)

        # Send current message history
        async with self._log_lock:
            history = sorted(self.message_log.values(), key=lambda e: e.logical_clock)

        ack = Message(
            type=MessageType.ACK,
            sender_id=self.server_id,
            payload={
                "history": [e.to_dict() for e in history],
                "leader": self.leader_id,
                "server_id": self.server_id,
            },
            logical_clock=await self.tick(),
        )
        writer.write(ack.serialize())
        await writer.drain()
        await self.broadcast_system_message(f"🟢 {username} joined the chat.")
        self.logger.info(f"Client joined: {username} ({client_id})")

    async def on_chat(self, msg: Message, writer):
        clock = await self.tick()
        entry = ChatEntry(
            msg_id=msg.msg_id,
            sender_id=msg.sender_id,
            username=msg.payload.get("username", msg.sender_id),
            content=msg.payload["content"],
            timestamp=msg.timestamp,
            logical_clock=clock,
        )

        async with self._log_lock:
            if entry.msg_id in self.message_log:
                return  # already processed (duplicate)
            self.message_log[entry.msg_id] = entry

        self.logger.info(f"MSG [{clock}] {entry.username}: {entry.content}")

        # Deliver to all local clients
        await self.deliver_to_clients(entry)

        # Replicate to peers
        await self.replicate_to_peers(entry)

        # ACK to original sender
        ack = Message(
            type=MessageType.ACK,
            sender_id=self.server_id,
            payload={"msg_id": entry.msg_id, "logical_clock": clock},
            logical_clock=clock,
        )
        writer.write(ack.serialize())
        await writer.drain()

    async def on_replicate(self, msg: Message):
        entry = ChatEntry.from_dict(msg.payload["entry"])
        async with self._log_lock:
            if entry.msg_id in self.message_log:
                return
            self.message_log[entry.msg_id] = entry
        self.logger.info(f"REPLICATED [{entry.logical_clock}] {entry.username}: {entry.content}")
        await self.deliver_to_clients(entry)

    # ── Message Delivery ───────────────────────────────────────────────────────

    async def deliver_to_clients(self, entry: ChatEntry):
        msg = Message(
            type=MessageType.CHAT,
            sender_id=self.server_id,
            payload=entry.to_dict(),
            logical_clock=entry.logical_clock,
            msg_id=entry.msg_id,
        )
        dead = []
        for cid, (_, writer, _) in self.clients.items():
            try:
                writer.write(msg.serialize())
                await writer.drain()
            except Exception:
                dead.append(cid)
        for cid in dead:
            del self.clients[cid]

    async def broadcast_system_message(self, text: str):
        clock = await self.tick()
        entry = ChatEntry(
            msg_id=f"sys-{time.time()}",
            sender_id="SYSTEM",
            username="SYSTEM",
            content=text,
            timestamp=time.time(),
            logical_clock=clock,
        )
        async with self._log_lock:
            self.message_log[entry.msg_id] = entry
        await self.deliver_to_clients(entry)
        await self.replicate_to_peers(entry)

    # ── Replication ────────────────────────────────────────────────────────────

    async def replicate_to_peers(self, entry: ChatEntry):
        msg = Message(
            type=MessageType.REPLICATE,
            sender_id=self.server_id,
            payload={"entry": entry.to_dict()},
            logical_clock=entry.logical_clock,
        )
        for peer in self.peers.values():
            if not peer.alive:
                await peer.connect()
            await peer.send(msg)

    # ── Sync ───────────────────────────────────────────────────────────────────

    async def on_sync_request(self, msg: Message, writer):
        async with self._log_lock:
            entries = [e.to_dict() for e in self.message_log.values()]
        resp = Message(
            type=MessageType.SYNC_RESPONSE,
            sender_id=self.server_id,
            payload={"entries": entries},
            logical_clock=await self.tick(),
        )
        writer.write(resp.serialize())
        await writer.drain()

    async def on_sync_response(self, msg: Message):
        entries = [ChatEntry.from_dict(e) for e in msg.payload["entries"]]
        async with self._log_lock:
            for e in entries:
                if e.msg_id not in self.message_log:
                    self.message_log[e.msg_id] = e
        self.logger.info(f"Synced {len(entries)} messages from {msg.sender_id}")

    # ── Bully Leader Election ──────────────────────────────────────────────────
    #
    # Rules:
    #   1. Any server can start an election.
    #   2. Send ELECTION to all servers with HIGHER id.
    #   3. If no higher server replies with OK → become coordinator.
    #   4. If a higher server receives ELECTION → send OK, start own election.
    #   5. Coordinator broadcasts COORDINATOR message.

    async def start_election(self):
        if self.election_in_progress:
            return
        self.election_in_progress = True
        self.election_ok_received = False
        self.logger.info(f"⚡ Starting election (my id={self.server_id})")

        higher_peers = [
            p for pid, p in self.peers.items() if pid > self.server_id
        ]

        election_msg = Message(
            type=MessageType.ELECTION,
            sender_id=self.server_id,
            payload={},
            logical_clock=await self.tick(),
        )

        sent = False
        for peer in higher_peers:
            if not peer.alive:
                await peer.connect()
            ok = await peer.send(election_msg)
            if ok:
                sent = True

        if not sent:
            # No higher server → become leader
            await self.become_leader()
        else:
            # Wait for OK (3s timeout)
            if self._election_timeout_task:
                self._election_timeout_task.cancel()
            self._election_timeout_task = asyncio.create_task(
                self._election_timeout()
            )

    async def _election_timeout(self):
        await asyncio.sleep(3)
        if not self.election_ok_received:
            self.logger.info("No OK received → becoming leader")
            await self.become_leader()

    async def on_election(self, msg: Message, writer):
        self.logger.info(f"Received ELECTION from {msg.sender_id}")
        ok = Message(
            type=MessageType.ELECTION_OK,
            sender_id=self.server_id,
            payload={},
            logical_clock=await self.tick(),
        )
        writer.write(ok.serialize())
        await writer.drain()
        # Start own election if not already
        asyncio.create_task(self.start_election())

    async def on_election_ok(self, msg: Message):
        self.logger.info(f"Received OK from {msg.sender_id} — stepping down")
        self.election_ok_received = True
        self.election_in_progress = False

    async def become_leader(self):
        self.leader_id = self.server_id
        self.election_in_progress = False
        self.logger.info(f"👑 I am the new LEADER: {self.server_id}")

        coord = Message(
            type=MessageType.COORDINATOR,
            sender_id=self.server_id,
            payload={"leader_id": self.server_id},
            logical_clock=await self.tick(),
        )
        for peer in self.peers.values():
            if not peer.alive:
                await peer.connect()
            await peer.send(coord)

        await self.broadcast_system_message(f"👑 Server {self.server_id} is the new leader.")

    async def on_coordinator(self, msg: Message):
        new_leader = msg.payload["leader_id"]
        self.leader_id = new_leader
        self.election_in_progress = False
        self.logger.info(f"👑 New leader announced: {new_leader}")

        # Request sync from new leader
        if new_leader != self.server_id and new_leader in self.peers:
            peer = self.peers[new_leader]
            sync_req = Message(
                type=MessageType.SYNC_REQUEST,
                sender_id=self.server_id,
                payload={},
                logical_clock=await self.tick(),
            )
            if not peer.alive:
                await peer.connect()
            await peer.send(sync_req)

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    async def heartbeat_loop(self):
        """Send heartbeats to all peers every 2 seconds."""
        while True:
            await asyncio.sleep(2)
            hb = Message(
                type=MessageType.HEARTBEAT,
                sender_id=self.server_id,
                payload={},
                logical_clock=await self.tick(),
            )
            for peer in self.peers.values():
                if not peer.alive:
                    await peer.connect()
                await peer.send(hb)

    async def on_heartbeat(self, msg: Message, writer):
        ack = Message(
            type=MessageType.HEARTBEAT_ACK,
            sender_id=self.server_id,
            payload={},
            logical_clock=await self.tick(),
        )
        writer.write(ack.serialize())
        await writer.drain()

    async def on_heartbeat_ack(self, msg: Message):
        if msg.sender_id in self.peers:
            self.peers[msg.sender_id].last_heartbeat = time.time()
            self.peers[msg.sender_id].alive = True

    async def fault_detector_loop(self):
        """Detect crashed peers and trigger election if leader died."""
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for pid, peer in self.peers.items():
                if peer.alive and peer.last_heartbeat > 0:
                    if now - peer.last_heartbeat > 6:
                        self.logger.warning(f"💀 Peer {pid} appears to have crashed!")
                        peer.alive = False
                        # If crashed peer was the leader → start new election
                        if pid == self.leader_id:
                            self.logger.warning("Leader has crashed! Starting election...")
                            asyncio.create_task(self.start_election())


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Distributed Chat Server")
    parser.add_argument("--id",   required=True, help="Unique server ID (e.g. S1, S2)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--peers", default="", help="Comma-separated peer specs: id:host:port")
    args = parser.parse_args()

    peers = []
    if args.peers:
        for spec in args.peers.split(","):
            pid, phost, pport = spec.strip().split(":")
            peers.append({"id": pid, "host": phost, "port": int(pport)})

    server = ChatServer(args.id, args.host, args.port, peers)
    asyncio.run(server.start())
