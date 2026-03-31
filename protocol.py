"""
Distributed Chat Protocol
Defines all message types and utilities shared between server and client.
"""

import json
import time
import uuid
from enum import Enum
from dataclasses import dataclass, asdict, field
from typing import Optional


class MessageType(str, Enum):
    # Chat messages
    CHAT = "CHAT"
    
    # Server-to-server (replication)
    REPLICATE = "REPLICATE"
    SYNC_REQUEST = "SYNC_REQUEST"
    SYNC_RESPONSE = "SYNC_RESPONSE"
    
    # Leader Election (Bully Algorithm)
    ELECTION = "ELECTION"
    ELECTION_OK = "ELECTION_OK"
    COORDINATOR = "COORDINATOR"
    
    # Heartbeat / Fault detection
    HEARTBEAT = "HEARTBEAT"
    HEARTBEAT_ACK = "HEARTBEAT_ACK"
    
    # Client-server
    JOIN = "JOIN"
    LEAVE = "LEAVE"
    ACK = "ACK"
    ERROR = "ERROR"
    SERVER_LIST = "SERVER_LIST"
    REDIRECT = "REDIRECT"


@dataclass
class Message:
    type: MessageType
    payload: dict
    sender_id: str
    timestamp: float = field(default_factory=time.time)
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    logical_clock: int = 0          # Lamport clock for ordering

    def serialize(self) -> bytes:
        d = asdict(self)
        d["type"] = self.type.value
        return (json.dumps(d) + "\n").encode()

    @staticmethod
    def deserialize(data: bytes) -> "Message":
        d = json.loads(data.decode().strip())
        d["type"] = MessageType(d["type"])
        return Message(**d)


@dataclass
class ChatEntry:
    """A single chat message stored in the replicated log."""
    msg_id: str
    sender_id: str
    username: str
    content: str
    timestamp: float
    logical_clock: int

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ChatEntry":
        return ChatEntry(**d)
