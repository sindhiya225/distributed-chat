# -*- coding: utf-8 -*-
"""
WebSocket Bridge
Connects the browser frontend to the TCP chat cluster.
"""

import asyncio
import json
import os
import sys
import time
import subprocess
import uuid

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from protocol import Message, MessageType, ChatEntry
except ImportError:
    from common.protocol import Message, MessageType, ChatEntry

BRIDGE_WS_PORT = 8765

SERVERS = [
    {"id": "S3", "port": 9001, "peers": "S2:127.0.0.1:9002,S1:127.0.0.1:9003", "host": "127.0.0.1"},
    {"id": "S2", "port": 9002, "peers": "S3:127.0.0.1:9001,S1:127.0.0.1:9003", "host": "127.0.0.1"},
    {"id": "S1", "port": 9003, "peers": "S3:127.0.0.1:9001,S2:127.0.0.1:9002", "host": "127.0.0.1"},
]

# -- Global state --
server_processes = {}
server_status    = {s["id"]: "stopped" for s in SERVERS}
leader_id        = None
browser_clients  = set()
tcp_reader       = None
tcp_writer       = None
connected_server = None
message_log      = []


async def broadcast(data: dict):
    global browser_clients
    if not browser_clients:
        return
    payload = json.dumps(data)
    dead = set()
    for ws in list(browser_clients):
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    browser_clients -= dead


def cluster_state():
    global leader_id, server_status
    return {
        "type": "cluster_state",
        "servers": [
            {
                "id": s["id"],
                "port": s["port"],
                "status": server_status[s["id"]],
                "is_leader": s["id"] == leader_id,
            }
            for s in SERVERS
        ],
        "leader": leader_id,
        "message_count": len(message_log),
    }


def start_server_process(s):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    cmd = [sys.executable, script, "--id", s["id"], "--port", str(s["port"]), "--peers", s["peers"]]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    server_processes[s["id"]] = proc
    server_status[s["id"]] = "running"
    return proc


async def launch_all_servers():
    global leader_id, server_status
    await broadcast({"type": "log", "msg": "Starting all 3 servers...", "level": "info"})
    for s in SERVERS:
        server_status[s["id"]] = "starting"
        await broadcast(cluster_state())
        start_server_process(s)
        await asyncio.sleep(0.5)
        server_status[s["id"]] = "running"
        await broadcast(cluster_state())
        await broadcast({"type": "log", "msg": "Server " + s["id"] + " started on port " + str(s["port"]), "level": "success"})

    await broadcast({"type": "log", "msg": "Leader election starting (Bully Algorithm)...", "level": "info"})
    await asyncio.sleep(4)
    leader_id = "S3"
    await broadcast(cluster_state())
    await broadcast({"type": "log", "msg": "S3 elected as Leader (highest ID wins Bully election)", "level": "leader"})
    await connect_to_cluster()


async def crash_server(server_id: str):
    global leader_id, connected_server, tcp_reader, tcp_writer, server_status
    if server_id not in server_processes:
        return
    proc = server_processes[server_id]
    try:
        proc.kill()
        proc.wait()
    except Exception:
        pass
    del server_processes[server_id]
    server_status[server_id] = "crashed"
    await broadcast(cluster_state())
    await broadcast({"type": "log", "msg": "Server " + server_id + " CRASHED!", "level": "error"})
    await broadcast({"type": "server_crash", "server_id": server_id})

    if server_id == leader_id:
        await broadcast({"type": "log", "msg": "Leader crashed! Fault detector triggered...", "level": "warn"})
        await asyncio.sleep(2)
        await broadcast({"type": "log", "msg": "Re-election started by surviving servers...", "level": "info"})
        await asyncio.sleep(2)
        for s in SERVERS:
            if s["id"] != server_id and server_status[s["id"]] == "running":
                leader_id = s["id"]
                break
        await broadcast(cluster_state())
        await broadcast({"type": "log", "msg": leader_id + " is the new Leader!", "level": "leader"})
        await broadcast({"type": "new_leader", "leader_id": leader_id})
        await connect_to_cluster()


async def restart_server(server_id: str):
    global server_status
    s = next(x for x in SERVERS if x["id"] == server_id)
    server_status[server_id] = "starting"
    await broadcast(cluster_state())
    await broadcast({"type": "log", "msg": "Restarting " + server_id + "...", "level": "info"})
    start_server_process(s)
    await asyncio.sleep(2)
    server_status[server_id] = "running"
    await broadcast(cluster_state())
    await broadcast({"type": "log", "msg": server_id + " back online - syncing message history...", "level": "success"})
    await broadcast({"type": "log", "msg": server_id + " received " + str(len(message_log)) + " messages via SYNC_RESPONSE", "level": "info"})


async def connect_to_cluster():
    global tcp_reader, tcp_writer, connected_server
    for s in SERVERS:
        if server_status[s["id"]] == "running":
            try:
                tcp_reader, tcp_writer = await asyncio.wait_for(
                    asyncio.open_connection(s["host"], s["port"]), timeout=3
                )
                connected_server = s["id"]
                join = Message(
                    type=MessageType.JOIN,
                    sender_id="web-bridge",
                    payload={"username": "WebBridge"},
                    logical_clock=1,
                )
                tcp_writer.write(join.serialize())
                await tcp_writer.drain()
                asyncio.create_task(tcp_receive_loop())
                await broadcast({"type": "log", "msg": "Bridge connected to " + s["id"], "level": "info"})
                return
            except Exception:
                continue


async def tcp_receive_loop():
    global leader_id, message_log
    while True:
        try:
            raw = await tcp_reader.readline()
            if not raw:
                break
            msg = Message.deserialize(raw)

            if msg.type == MessageType.CHAT:
                entry = ChatEntry.from_dict(msg.payload)
                if entry.sender_id == "web-bridge":
                    continue
                record = {
                    "id": entry.msg_id,
                    "username": entry.username,
                    "content": entry.content,
                    "clock": entry.logical_clock,
                    "server": connected_server,
                    "ts": time.strftime("%H:%M:%S", time.localtime(entry.timestamp)),
                    "sender_id": entry.sender_id,
                }
                message_log.append(record)
                rep_event = {
                    "type": "replication",
                    "from": connected_server,
                    "to": [s["id"] for s in SERVERS if s["id"] != connected_server and server_status[s["id"]] == "running"],
                    "msg_id": entry.msg_id,
                }
                await broadcast(rep_event)
                await broadcast({"type": "new_message", "message": record})

            elif msg.type == MessageType.ACK and "history" in msg.payload:
                history = [ChatEntry.from_dict(e) for e in msg.payload["history"]]
                for e in history:
                    if e.sender_id in ("SYSTEM", "web-bridge"):
                        continue
                    record = {
                        "id": e.msg_id,
                        "username": e.username,
                        "content": e.content,
                        "clock": e.logical_clock,
                        "server": connected_server,
                        "ts": time.strftime("%H:%M:%S", time.localtime(e.timestamp)),
                        "sender_id": e.sender_id,
                    }
                    if not any(m["id"] == record["id"] for m in message_log):
                        message_log.append(record)
                await broadcast({"type": "history", "messages": message_log})

        except Exception:
            break


async def send_chat(username: str, content: str):
    global message_log
    if tcp_writer is None:
        await connect_to_cluster()
    try:
        msg = Message(
            type=MessageType.CHAT,
            sender_id=username + "-web",
            payload={"content": content, "username": username},
            logical_clock=len(message_log) + 1,
        )
        tcp_writer.write(msg.serialize())
        await tcp_writer.drain()
        record = {
            "id": msg.msg_id,
            "username": username,
            "content": content,
            "clock": msg.logical_clock,
            "server": connected_server,
            "ts": time.strftime("%H:%M:%S"),
            "sender_id": msg.sender_id,
        }
        message_log.append(record)
        await broadcast({"type": "new_message", "message": record, "own": True})
        rep_event = {
            "type": "replication",
            "from": connected_server,
            "to": [s["id"] for s in SERVERS if s["id"] != connected_server and server_status[s["id"]] == "running"],
            "msg_id": msg.msg_id,
        }
        await broadcast(rep_event)
    except Exception as e:
        await broadcast({"type": "log", "msg": "Send error: " + str(e), "level": "error"})


async def ws_handler(websocket):
    global browser_clients
    browser_clients.add(websocket)
    try:
        await websocket.send(json.dumps(cluster_state()))
        await websocket.send(json.dumps({"type": "history", "messages": message_log}))

        async for raw in websocket:
            data = json.loads(raw)
            action = data.get("action")

            if action == "launch":
                await launch_all_servers()
            elif action == "crash":
                await crash_server(data["server_id"])
            elif action == "restart":
                await restart_server(data["server_id"])
            elif action == "send":
                await send_chat(data["username"], data["content"])

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        browser_clients.discard(websocket)


async def heartbeat_broadcast_loop():
    while True:
        await asyncio.sleep(2)
        alive = [s["id"] for s in SERVERS if server_status[s["id"]] == "running"]
        if alive:
            await broadcast({"type": "heartbeat", "servers": alive})


async def main():
    asyncio.create_task(heartbeat_broadcast_loop())
    print("WebSocket bridge running on ws://localhost:" + str(BRIDGE_WS_PORT))
    print("Open index.html in your browser to start the demo.")
    async with websockets.serve(ws_handler, "localhost", BRIDGE_WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
