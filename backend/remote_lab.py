"""Controlled LAN TCP/UDP listeners for PacketLab's Remote Device Lab."""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable

from .tcp_server import encode_frame, recv_frame

MAX_MESSAGE_BYTES = 4096
MAX_UDP_TRANSFER_BYTES = 44000
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
        self._clients: dict[socket.socket, tuple[str, int]] = {}
        self._clients_lock = threading.Lock()

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
        with self._clients_lock:
            self._clients[conn] = address
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
        with self._clients_lock:
            self._clients.pop(conn, None)

    def broadcast(self, message: str) -> int:
        payload = encode_frame({"ok": True, "protocol": "tcp", "type": "server_message", "echo": message})
        delivered = 0
        with self._clients_lock:
            for conn in list(self._clients):
                try:
                    conn.sendall(payload)
                    delivered += 1
                except OSError:
                    self._clients.pop(conn, None)
        return delivered

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
        self._socket = None
        with self._clients_lock:
            self._clients.clear()


class RemoteUDPServer:
    def __init__(self, host: str, port: int, callback: EventCallback, welcome: str, hosted_message: str = "", hosted_file: dict[str, str] | None = None, timeout: float = 0.5):
        self.host, self.port, self.callback, self.welcome, self.hosted_message, self.hosted_file, self.timeout = host, port, callback, welcome, hosted_message, hosted_file, timeout
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._clients: dict[tuple[str, int], None] = {}
        self._clients_lock = threading.Lock()

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
                if isinstance(request, dict) and request.get("type") == "contact":
                    payload = {
                        "ok": True,
                        "protocol": "udp",
                        "type": "transfer",
                        "message": self.hosted_message,
                        "file": self.hosted_file,
                    }
                    self._socket.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), address)
                    self.callback(_event("udp", address, "CONTACT", self.hosted_message or (self.hosted_file or {}).get("name", "file")))
                    continue
                if isinstance(request, dict) and request.get("type") == "register":
                    with self._clients_lock:
                        self._clients[address] = None
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

    def broadcast(self, message: str) -> int:
        payload = json.dumps({
            "ok": True, "protocol": "udp", "type": "server_message", "echo": message,
        }, separators=(",", ":")).encode("utf-8")
        delivered = 0
        with self._clients_lock:
            for address in list(self._clients):
                    try:
                        assert self._socket is not None
                        self._socket.sendto(payload, address)
                        delivered += 1
                    except OSError:
                        self._clients.pop(address, None)
        return delivered

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
        self._socket = None
        with self._clients_lock:
            self._clients.clear()


class RemoteLab:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: dict[str, RemoteTCPServer | RemoteUDPServer] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=50)
        self._sessions: dict[str, socket.socket] = {}
        self._udp_sessions: dict[str, socket.socket] = {}
        self._receive_queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._receive_conditions: dict[str, threading.Condition] = {}

    def _record(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._events.appendleft(item)

    def start(self, protocols: list[str], tcp_port: int, udp_port: int, welcome: str, hosted_message: str = "", hosted_file: dict[str, str] | None = None) -> dict[str, Any]:
        self.stop()
        started: dict[str, RemoteTCPServer | RemoteUDPServer] = {}
        try:
            if "tcp" in protocols:
                started["tcp"] = RemoteTCPServer("0.0.0.0", tcp_port, self._record, welcome).start()
            if "udp" in protocols:
                started["udp"] = RemoteUDPServer("0.0.0.0", udp_port, self._record, welcome, hosted_message, hosted_file).start()
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
        self._receive_queues.clear()
        self._receive_conditions.clear()
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
            sock.settimeout(None)
            self._sessions[session_id] = sock
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(json.dumps({"type": "register"}).encode("utf-8"))
            welcome = json.loads(sock.recv(65507).decode("utf-8"))
            sock.settimeout(None)
            self._udp_sessions[session_id] = sock
        self._receive_conditions[session_id] = threading.Condition()
        threading.Thread(target=self._receive_loop, args=(session_id, protocol), daemon=True).start()
        return {"session_id": session_id, "protocol": protocol, "welcome": welcome.get("echo"), "latency_ms": round((time.perf_counter() - started) * 1000, 3)}

    def contact_udp(self, host: str, port: int, timeout: float) -> dict[str, Any]:
        started = time.perf_counter()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(json.dumps({"type": "contact"}).encode("utf-8"), (host, port))
            response, _ = sock.recvfrom(65507)
        result = json.loads(response.decode("utf-8"))
        if not isinstance(result, dict) or result.get("type") != "transfer":
            raise ValueError("server returned an invalid UDP transfer")
        file_data = result.get("file")
        return {"protocol": "udp", "message": result.get("message", ""), "file": file_data, "success": bool(result.get("ok")), "latency_ms": round((time.perf_counter() - started) * 1000, 3)}

    def send_session(self, session_id: str, protocol: str, message: str, timeout: float) -> dict[str, Any]:
        started = time.perf_counter()
        if protocol == "tcp":
            sock = self._sessions.get(session_id)
            if not sock:
                raise ValueError("TCP session not found; connect first")
            sock.sendall(encode_frame(message))
        else:
            sock = self._udp_sessions.get(session_id)
            if not sock:
                raise ValueError("UDP session not found; register first")
            sock.send(json.dumps(message).encode("utf-8"))
        response = self._wait_for_message(session_id, timeout)
        return {"protocol": protocol, "response": response.get("echo"), "success": bool(response.get("ok")), "timestamp": datetime.now(timezone.utc).isoformat(), "latency_ms": round((time.perf_counter() - started) * 1000, 3), "bytes_sent": len(encode_frame(message)) - 4 if protocol == "tcp" else len(json.dumps(message).encode("utf-8")), "bytes_received": len(encode_frame(response)) - 4 if protocol == "tcp" else len(json.dumps(response).encode("utf-8"))}

    def _receive_loop(self, session_id: str, protocol: str) -> None:
        sock = self._sessions.get(session_id) if protocol == "tcp" else self._udp_sessions.get(session_id)
        condition = self._receive_conditions[session_id]
        try:
            while True:
                message = recv_frame(sock) if protocol == "tcp" else json.loads(sock.recv(65507).decode("utf-8"))
                with condition:
                    self._receive_queues[session_id].append(message)
                    condition.notify_all()
        except (ConnectionError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            with condition:
                condition.notify_all()

    def _wait_for_message(self, session_id: str, timeout: float) -> dict[str, Any]:
        condition = self._receive_conditions.get(session_id)
        if not condition:
            raise ValueError("session not found; connect first")
        deadline = time.monotonic() + timeout
        with condition:
            while not self._receive_queues[session_id]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for server response")
                condition.wait(remaining)
            return self._receive_queues[session_id].popleft()

    def receive_session(self, session_id: str) -> list[dict[str, Any]]:
        condition = self._receive_conditions.get(session_id)
        if not condition:
            raise ValueError("session not found; connect first")
        with condition:
            deadline = time.monotonic() + 0.5
            while not any(item.get("type") == "server_message" for item in self._receive_queues[session_id]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                condition.wait(remaining)
            messages = [item for item in self._receive_queues[session_id] if item.get("type") == "server_message"]
            self._receive_queues[session_id] = deque(
                item for item in self._receive_queues[session_id] if item.get("type") != "server_message"
            )
            return messages

    def send_from_server(self, protocol: str, message: str) -> dict[str, Any]:
        if protocol != "tcp":
            raise ValueError("server chat is available for TCP only")
        server = self._servers.get(protocol)
        if not server:
            raise ValueError(f"{protocol.upper()} remote server is not running")
        delivered = server.broadcast(message)  # type: ignore[attr-defined]
        self._record(_event("tcp", ("connected clients", 0), message, "Sent to connected client"))
        return {"protocol": protocol, "message": message, "delivered": delivered, "timestamp": datetime.now(timezone.utc).isoformat()}

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._servers),
            "tcp": {"running": self._servers.get("tcp").is_running if self._servers.get("tcp") else False, "port": self._servers.get("tcp").port if self._servers.get("tcp") else None},
            "udp": {"running": self._servers.get("udp").is_running if self._servers.get("udp") else False, "port": self._servers.get("udp").port if self._servers.get("udp") else None},
            "events": list(self._events),
        }
