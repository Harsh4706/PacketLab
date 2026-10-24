"""Small framed TCP echo server used by PacketLab."""
from __future__ import annotations

import json
import socket
import struct
import threading
from typing import Any

MAX_FRAME_SIZE = 64 * 1024


def encode_frame(value: Any) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError("frame exceeds maximum size")
    return struct.pack("!I", len(payload)) + payload


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("connection closed before complete frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> Any:
    header = recv_exact(sock, 4)
    size = struct.unpack("!I", header)[0]
    if size > MAX_FRAME_SIZE:
        raise ValueError("frame exceeds maximum size")
    try:
        return json.loads(recv_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON frame") from exc


class TCPServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._socket is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> "TCPServer":
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self.port))
        self._socket.listen(16)
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(self.timeout)
            try:
                request = recv_frame(conn)
                response = {"ok": True, "protocol": "tcp", "echo": request}
                conn.sendall(encode_frame(response))
            except (ConnectionError, OSError, ValueError) as exc:
                try:
                    conn.sendall(encode_frame({"ok": False, "error": str(exc)}))
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
