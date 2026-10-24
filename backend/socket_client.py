"""Bounded-timeout clients for PacketLab's TCP and UDP transports."""
from __future__ import annotations

import socket
import time
import json
from datetime import datetime, timezone
from typing import Any

from .tcp_server import encode_frame, recv_frame

DEFAULT_TIMEOUT = 2.0


def send_tcp(host: str, port: int, payload: Any, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    started = time.perf_counter()
    request_bytes = encode_frame(payload)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request_bytes)
        result = recv_frame(sock)
    response_bytes = encode_frame(result)
    return {
        "protocol": "tcp",
        "echo": result.get("echo"),
        "request": payload,
        "response": result.get("echo"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "bytes_sent": len(request_bytes) - 4,
        "bytes_received": len(response_bytes) - 4,
        "success": bool(result.get("ok")),
    }


def send_udp(host: str, port: int, payload: Any, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    started = time.perf_counter()
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(data, (host, port))
        response, _ = sock.recvfrom(65507)
    result = json.loads(response.decode("utf-8"))
    return {
        "protocol": "udp",
        "echo": result.get("echo"),
        "request": payload,
        "response": result.get("echo"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "bytes_sent": len(data),
        "bytes_received": len(response),
        "success": bool(result.get("ok")),
    }
