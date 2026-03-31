#!/usr/bin/env python3
"""
Live Demo Script — Distributed Chat Fault Tolerance
====================================================
This script demonstrates:
  1. All 3 servers start up
  2. Leader election completes → Leader is announced
  3. Simulate the leader CRASHING
  4. Remaining servers detect failure and re-elect a leader
  5. A client reconnects automatically

Run this in one terminal, then run clients in other terminals.
"""

import subprocess
import time
import sys
import os
import signal

SERVERS = [
    {"id": "S3", "port": 9001, "peers": "S2:127.0.0.1:9002,S1:127.0.0.1:9003"},
    {"id": "S2", "port": 9002, "peers": "S3:127.0.0.1:9001,S1:127.0.0.1:9003"},
    {"id": "S1", "port": 9003, "peers": "S3:127.0.0.1:9001,S2:127.0.0.1:9002"},
]

# Bully algorithm: S3 > S2 > S1 alphabetically → S3 will win initial election

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(BASE, "server", "server.py")
processes = {}

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"


def log(msg, color=CYAN):
    print(f"{color}{BOLD}[DEMO]{RESET} {msg}")


def start_server(s):
    cmd = [
        sys.executable, SERVER_SCRIPT,
        "--id", s["id"],
        "--port", str(s["port"]),
        "--peers", s["peers"],
    ]
    log(f"Starting server {s['id']} on port {s['port']}...", GREEN)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    processes[s["id"]] = proc
    return proc


def crash_server(server_id):
    if server_id in processes:
        proc = processes[server_id]
        log(f"💥 CRASHING server {server_id} (pid={proc.pid})!", RED)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        del processes[server_id]
        log(f"Server {server_id} is DOWN.", RED)


def print_server_output(server_id, proc, lines=8):
    """Read and print last N lines from server output."""
    output = []
    try:
        for _ in range(lines):
            line = proc.stdout.readline()
            if line:
                output.append(line.rstrip())
    except Exception:
        pass
    for line in output:
        print(f"  [{server_id}] {line}")


def cleanup():
    log("Shutting down all servers...", YELLOW)
    for sid, proc in list(processes.items()):
        proc.terminate()
        proc.wait()
    log("Done.")


if __name__ == "__main__":
    print(f"\n{BOLD}{CYAN}{'═'*60}{RESET}")
    print(f"{BOLD}{CYAN}  Distributed Chat — Fault Tolerance Demo{RESET}")
    print(f"{BOLD}{CYAN}{'═'*60}{RESET}\n")

    try:
        # Step 1: Start all servers
        log("STEP 1: Starting 3 chat servers...", GREEN)
        for s in SERVERS:
            start_server(s)
            time.sleep(0.3)

        log("Waiting for servers to initialize and elect a leader (5s)...", CYAN)
        time.sleep(5)

        print(f"\n{BOLD}{'─'*60}{RESET}")
        log("All servers running. Leader election should be complete.", GREEN)
        log("S3 has the highest ID → should be elected as leader.", CYAN)
        print(f"\n👉  Open other terminals and run clients:")
        print(f"    python3 client/client.py --username Alice")
        print(f"    python3 client/client.py --username Bob")
        print(f"\n    Clients will auto-connect to any available server.")
        print(f"{'─'*60}\n")

        input("Press ENTER when ready to CRASH the leader (S3)...")

        # Step 2: Crash the leader
        print()
        log("STEP 2: Simulating leader crash...", RED)
        crash_server("S3")

        log("Waiting 8s for fault detection and re-election...", YELLOW)
        time.sleep(8)

        log("S2 should now be the new leader (next highest ID).", GREEN)
        log("Clients should have auto-reconnected.", GREEN)
        print(f"\n👉  Try sending more messages from your client terminals.")
        print(f"    The chat should continue seamlessly!\n")

        input("Press ENTER to restart S3 (it will rejoin and sync)...")

        # Step 3: Restart crashed server
        log("STEP 3: Restarting S3...", CYAN)
        s3 = next(s for s in SERVERS if s["id"] == "S3")
        start_server(s3)
        time.sleep(3)
        log("S3 is back online and syncing message history.", GREEN)
        log("Note: S2 remains leader (Bully only triggers on crash).", CYAN)

        input("\nPress ENTER to shut down all servers and exit...")

    except KeyboardInterrupt:
        print()
    finally:
        cleanup()
