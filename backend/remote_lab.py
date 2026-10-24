"""Controlled LAN TCP/UDP listeners for PacketLab's Remote Device Lab."""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from .tcp_server import encode_frame, recv_frame

MAX_MESSAGE_BYTES = 4096
EventCallback = Callable[[dict[str, Any]], None]


def _event(protocol: str, address: tuple[str, int], message: str, response: str) -> dict[str, Any]:
    return {
        "id": f"{datetime.now(timezone.utc).timestamp()}-{protocol}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "protocol": protocol,
        "address": f"{address[0]}:{address[1]}",
        "message": message,
        "response": response,
    }


class RemoteTCPServer:
    def __init__(self, host: str, port: int, callback: EventCallback, welcome: str, timeout: float = 0.5):
        self.host, self.port, self.callback, self.welcome, self.timeout = host, port, callback, welcome, timeout
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._socket and self._thread and self._thread.is_alive())

    def start(self) -> "RemoteTCPServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(16)
        sock.settimeout(self.timeout)
        self._socket = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                conn, address = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, address), daemon=True).start()

    def _handle(self, conn: socket.socket, address: tuple[str, int]) -> None:
        with conn:
            conn.settimeout(None)
            try:
                conn.sendall(encode_frame({"ok": True, "protocol": "tcp", "type": "welcome", "echo": self.welcome}))
                while True:
                    request = recv_frame(conn)
                    if not isinstance(request, str) or not request.strip():
                        raise ValueError("message must be a non-empty string")
                    if len(request.encode("utf-8")) > MAX_MESSAGE_BYTES:
                        raise ValueError("message is too long")
                    response = f"Echo from PacketLab server: {request}"
                    conn.sendall(encode_frame({"ok": True, "protocol": "tcp", "type": "message", "echo": response}))
                    self.callback(_event("tcp", address, request, response))
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
        self._socket = None


class RemoteUDPServer:
    def __init__(self, host: str, port: int, callback: EventCallback, welcome: str, timeout: float = 0.5):
        self.host, self.port, self.callback, self.welcome, self.timeout = host, port, callback, welcome, timeout
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._socket and self._thread and self._thread.is_alive())

    def start(self) -> "RemoteUDPServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.host, self.port))
        sock.settimeout(self.timeout)
        self._socket = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(65507)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                request = json.loads(data.decode("utf-8"))
                if isinstance(request, dict) and request.get("type") == "register":
                    payload = {"ok": True, "protocol": "udp", "type": "welcome", "echo": self.welcome}
                    self._socket.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), address)
                    self.callback(_event("udp", address, "REGISTER", self.welcome))
                    continue
                if not isinstance(request, str) or not request.strip():
                    raise ValueError("message must be a non-empty string")
                if len(request.encode("utf-8")) > MAX_MESSAGE_BYTES:
                    raise ValueError("message is too long")
                response = f"Echo from PacketLab server: {request}"
                payload = {"ok": True, "protocol": "udp", "echo": response}
                self._socket.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), address)
                self.callback(_event("udp", address, request, response))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                error = {"ok": False, "error": str(exc)}
                try:
                    self._socket.sendto(json.dumps(error).encode("utf-8"), address)
                except OSError:
                    break

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
        self._socket = None


class RemoteLab:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: dict[str, RemoteTCPServer | RemoteUDPServer] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=50)
        self._sessions: dict[str, socket.socket] = {}
        self._udp_sessions: dict[str, socket.socket] = {}

    def _record(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._events.appendleft(item)

    def start(self, protocols: list[str], tcp_port: int, udp_port: int, welcome: str) -> dict[str, Any]:
        self.stop()
        started: dict[str, RemoteTCPServer | RemoteUDPServer] = {}
        try:
            if "tcp" in protocols:
                started["tcp"] = RemoteTCPServer("0.0.0.0", tcp_port, self._record, welcome).start()
            if "udp" in protocols:
                started["udp"] = RemoteUDPServer("0.0.0.0", udp_port, self._record, welcome).start()
        except Exception:
            for server in started.values():
                server.stop()
            raise
        self._servers = started
        return self.status()

    def stop(self) -> None:
        for sock in [*self._sessions.values(), *self._udp_sessions.values()]:
            sock.close()
        self._sessions.clear()
        self._udp_sessions.clear()
        for server in self._servers.values():
            server.stop()
        self._servers = {}

    def connect(self, protocol: str, host: str, port: int, timeout: float) -> dict[str, Any]:
        started = time.perf_counter()
        session_id = uuid.uuid4().hex
        if protocol == "tcp":
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)
            welcome = recv_frame(sock)
            self._sessions[session_id] = sock
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(json.dumps({"type": "register"}).encode("utf-8"))
            welcome = json.loads(sock.recv(65507).decode("utf-8"))
            self._udp_sessions[session_id] = sock
        return {"session_id": session_id, "protocol": protocol, "welcome": welcome.get("echo"), "latency_ms": round((time.perf_counter() - started) * 1000, 3)}

    def send_session(self, session_id: str, protocol: str, message: str, timeout: float) -> dict[str, Any]:
        started = time.perf_counter()
        if protocol == "tcp":
            sock = self._sessions.get(session_id)
            if not sock:
                raise ValueError("TCP session not found; connect first")
            sock.settimeout(timeout)
            sock.sendall(encode_frame(message))
            response = recv_frame(sock)
        else:
            sock = self._udp_sessions.get(session_id)
            if not sock:
                raise ValueError("UDP session not found; register first")
            sock.settimeout(timeout)
            sock.send(json.dumps(message).encode("utf-8"))
            response = json.loads(sock.recv(65507).decode("utf-8"))
        return {"protocol": protocol, "response": response.get("echo"), "success": bool(response.get("ok")), "timestamp": datetime.now(timezone.utc).isoformat(), "latency_ms": round((time.perf_counter() - started) * 1000, 3), "bytes_sent": len(encode_frame(message)) - 4 if protocol == "tcp" else len(json.dumps(message).encode("utf-8")), "bytes_received": len(encode_frame(response)) - 4 if protocol == "tcp" else len(json.dumps(response).encode("utf-8"))}

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._servers),
            "tcp": {"running": self._servers.get("tcp").is_running if self._servers.get("tcp") else False, "port": self._servers.get("tcp").port if self._servers.get("tcp") else None},
            "udp": {"running": self._servers.get("udp").is_running if self._servers.get("udp") else False, "port": self._servers.get("udp").port if self._servers.get("udp") else None},
            "events": list(self._events),
        }
