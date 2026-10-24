"""UDP echo server for PacketLab."""
from __future__ import annotations

import json
import socket
import threading
from typing import Any

MAX_DATAGRAM_SIZE = 65507


class UDPServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._socket is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> "UDPServer":
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((self.host, self.port))
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(MAX_DATAGRAM_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                request: Any = json.loads(data.decode("utf-8"))
                response = {"ok": True, "protocol": "udp", "echo": request}
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = {"ok": False, "error": "invalid JSON datagram"}
            try:
                self._socket.sendto(json.dumps(response, separators=(",", ":")).encode(), address)
            except OSError:
                break

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
