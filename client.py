"""
Distributed Chat Client
=======================
Features:
  - Connects to any server in the cluster
  - Receives full message history on join
  - Auto-reconnects to another server if current one crashes
  - Displays Lamport logical clock for each message
  - Color-coded terminal UI
"""

import asyncio
import os
import sys
import time
import uuid
import argparse
from typing import List, Optional

from protocol import Message, MessageType, ChatEntry
# ANSI colors
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
MAGENTA= "\033[95m"


def color(text, c): return f"{c}{text}{RESET}"


SERVER_LIST = []   # populated from CLI  [(host, port), ...]


class ChatClient:
    def __init__(self, username: str, servers: list):
        self.username = username
        self.client_id = f"{username}-{uuid.uuid4().hex[:6]}"
        self.servers = servers          # [(host, port)]
        self.server_index = 0
        self.writer: Optional[asyncio.StreamWriter] = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.logical_clock = 0
        self.connected = False
        self.running = True

    def tick(self) -> int:
        self.logical_clock += 1
        return self.logical_clock

    def update_clock(self, received: int) -> int:
        self.logical_clock = max(self.logical_clock, received) + 1
        return self.logical_clock

    # ── Connection ─────────────────────────────────────────────────────────────

    async def connect(self):
        attempts = 0
        while self.running:
            host, port = self.servers[self.server_index % len(self.servers)]
            try:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0
                )
                self.connected = True
                print(color(f"\n✅ Connected to server at {host}:{port}", GREEN))

                # Send JOIN
                join = Message(
                    type=MessageType.JOIN,
                    sender_id=self.client_id,
                    payload={"username": self.username},
                    logical_clock=self.tick(),
                )
                self.writer.write(join.serialize())
                await self.writer.drain()
                return True

            except Exception as e:
                attempts += 1
                self.server_index += 1
                print(color(f"⚠️  Cannot connect to {host}:{port}. Trying next server... ({e})", YELLOW))
                await asyncio.sleep(1)
                if attempts >= len(self.servers) * 3:
                    print(color("❌ All servers unreachable.", RED))
                    return False

    async def disconnect(self):
        if self.writer:
            try:
                leave = Message(
                    type=MessageType.LEAVE,
                    sender_id=self.client_id,
                    payload={"username": self.username},
                    logical_clock=self.tick(),
                )
                self.writer.write(leave.serialize())
                await self.writer.drain()
            except Exception:
                pass
            self.writer.close()
        self.connected = False

    # ── Send / Receive ─────────────────────────────────────────────────────────

    async def send_message(self, content: str):
        msg = Message(
            type=MessageType.CHAT,
            sender_id=self.client_id,
            payload={"content": content, "username": self.username},
            logical_clock=self.tick(),
        )
        self.writer.write(msg.serialize())
        await self.writer.drain()

    async def receive_loop(self):
        while self.running:
            try:
                raw = await self.reader.readline()
                if not raw:
                    raise ConnectionResetError("Server closed connection")
                msg = Message.deserialize(raw)
                self.update_clock(msg.logical_clock)
                await self.handle_message(msg)
            except (ConnectionResetError, asyncio.IncompleteReadError, OSError):
                if self.running:
                    print(color("\n💥 Server disconnected! Attempting failover...", RED))
                    self.connected = False
                    self.server_index += 1
                    await asyncio.sleep(1)
                    ok = await self.connect()
                    if not ok:
                        self.running = False
                break
            except Exception as e:
                print(color(f"\nReceive error: {e}", RED))
                break

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.ACK and "history" in msg.payload:
            # Initial history on join
            entries = [ChatEntry.from_dict(e) for e in msg.payload["history"]]
            entries.sort(key=lambda e: e.logical_clock)
            if entries:
                print(color(f"\n{'─'*50}", GRAY))
                print(color(f"  📜 Message History ({len(entries)} messages)", GRAY))
                print(color(f"{'─'*50}", GRAY))
                for e in entries[-20:]:     # show last 20
                    self.print_entry(e)
                print(color(f"{'─'*50}\n", GRAY))
            leader = msg.payload.get("leader")
            print(color(f"  Leader: {leader}  |  Connected to: {msg.payload.get('server_id')}", CYAN))
            print(color(f"  Type your message and press Enter. Ctrl+C to quit.\n", GRAY))

        elif msg.type == MessageType.CHAT:
            entry = ChatEntry.from_dict(msg.payload)
            self.print_entry(entry)

        elif msg.type == MessageType.ACK:
            # Message delivery ack
            pass

    def print_entry(self, entry: ChatEntry):
        ts = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        clock_tag = color(f"[lc={entry.logical_clock}]", GRAY)
        if entry.sender_id == "SYSTEM":
            print(f"  {clock_tag} {color(entry.content, CYAN)}")
        elif entry.sender_id.startswith(self.username):
            print(f"  {clock_tag} {color(ts, GRAY)} {color('You', MAGENTA)}: {entry.content}")
        else:
            print(f"  {clock_tag} {color(ts, GRAY)} {color(entry.username, YELLOW)}: {entry.content}")

    # ── Main Loop ──────────────────────────────────────────────────────────────

    async def run(self):
        print(color(f"\n{'═'*50}", BOLD))
        print(color(f"  🌐 Distributed Chat — {self.username}", BOLD))
        print(color(f"{'═'*50}", BOLD))

        ok = await self.connect()
        if not ok:
            return

        # Concurrent receive loop + input loop
        receive_task = asyncio.create_task(self.receive_loop())

        loop = asyncio.get_event_loop()
        try:
            while self.running:
                # Non-blocking stdin read
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                content = line.strip()
                if not content:
                    continue
                if content.lower() in ("/quit", "/exit"):
                    break
                if self.connected:
                    await self.send_message(content)
                else:
                    print(color("⚠️  Not connected. Reconnecting...", YELLOW))
                    await self.connect()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.running = False
            await self.disconnect()
            receive_task.cancel()
            print(color("\n👋 Goodbye!\n", CYAN))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Distributed Chat Client")
    parser.add_argument("--username", "-u", required=True, help="Your display name")
    parser.add_argument(
        "--servers", "-s",
        default="127.0.0.1:9001,127.0.0.1:9002,127.0.0.1:9003",
        help="Comma-separated server addresses: host:port",
    )
    args = parser.parse_args()

    servers = []
    for spec in args.servers.split(","):
        h, p = spec.strip().rsplit(":", 1)
        servers.append((h, int(p)))

    client = ChatClient(args.username, servers)
    asyncio.run(client.run())
