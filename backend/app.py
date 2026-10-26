"""Flask HTTP API for exercising PacketLab TCP and UDP transports."""
from __future__ import annotations

import os
import socket
import ipaddress
import base64
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

from .socket_client import send_tcp, send_udp
from .tcp_server import TCPServer
from .udp_server import UDPServer
from .remote_lab import MAX_UDP_TRANSFER_BYTES, RemoteLab

MAX_MESSAGE_LENGTH = 4096
MAX_TIMEOUT = 5.0


def _config() -> dict[str, Any]:
    return {
        "tcp_host": os.getenv("PACKETLAB_TCP_HOST", "127.0.0.1"),
        "tcp_port": int(os.getenv("PACKETLAB_TCP_PORT", "9001")),
        "udp_host": os.getenv("PACKETLAB_UDP_HOST", "127.0.0.1"),
        "udp_port": int(os.getenv("PACKETLAB_UDP_PORT", "9002")),
    }


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(_config(), **(config or {}))
    origins = os.getenv("PACKETLAB_FRONTEND_ORIGIN", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000").split(",")
    CORS(app, resources={r"/api/*": {"origins": [o.strip() for o in origins]}})
    app.extensions["packetlab_servers"] = ()
    app.extensions["remote_lab"] = RemoteLab()

    def _remote_host(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("server host must be a private LAN IPv4 address")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("server host must be a private LAN IPv4 address") from exc
        if address.version != 4 or not (address.is_private or address.is_loopback):
            raise ValueError("server host must be a private LAN IPv4 address")
        return value

    def _remote_port(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
            raise ValueError(f"{field} must be between 1024 and 65535")
        return value

    def _local_ip() -> str:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
            for address in addresses:
                parsed = ipaddress.ip_address(address)
                if parsed.version == 4 and parsed.is_private and not parsed.is_loopback:
                    return address
        except OSError:
            pass
        return "127.0.0.1"

    @app.get("/api/health")
    @app.get("/health")
    def health():
        servers = app.extensions["packetlab_servers"]
        tcp_running = bool(servers and servers[0].is_running)
        udp_running = bool(servers and servers[1].is_running)
        return jsonify({
            "ok": tcp_running and udp_running,
            "service": "packetlab-backend",
            "tcp": {"status": "online" if tcp_running else "offline", "host": app.config["tcp_host"], "port": app.config["tcp_port"]},
            "udp": {"status": "online" if udp_running else "offline", "host": app.config["udp_host"], "port": app.config["udp_port"]},
        }), (200 if tcp_running and udp_running else 503)

    @app.post("/api/send")
    @app.post("/send")
    def send():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        protocol = str(body.get("protocol", "")).lower()
        message = body.get("message")
        if protocol not in ("tcp", "udp"):
            return jsonify({"ok": False, "error": "protocol must be 'tcp' or 'udp'"}), 400
        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400
        if len(message.encode("utf-8")) > MAX_MESSAGE_LENGTH:
            return jsonify({"ok": False, "error": "message is too long"}), 400
        timeout = body.get("timeout", 2.0)
        if not isinstance(timeout, (int, float)) or not 0.05 <= timeout <= MAX_TIMEOUT:
            return jsonify({"ok": False, "error": f"timeout must be between 0.05 and {MAX_TIMEOUT} seconds"}), 400
        host = app.config[f"{protocol}_host"]
        port = app.config[f"{protocol}_port"]
        try:
            result = send_tcp(host, port, message, float(timeout)) if protocol == "tcp" else send_udp(host, port, message, float(timeout))
            return jsonify({"ok": True, "protocol": protocol, "result": result})
        except (OSError, TimeoutError, ValueError, socket.timeout) as exc:
            return jsonify({"ok": False, "protocol": protocol, "error": str(exc) or "transport failure"}), 504

    @app.post("/api/compare")
    @app.post("/compare")
    def compare():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or "message" not in body:
            return jsonify({"ok": False, "error": "message is required"}), 400
        message = body["message"]
        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400
        if len(message.encode("utf-8")) > MAX_MESSAGE_LENGTH:
            return jsonify({"ok": False, "error": "message is too long"}), 400
        timeout = body.get("timeout", 2.0)
        if not isinstance(timeout, (int, float)) or not 0.05 <= timeout <= MAX_TIMEOUT:
            return jsonify({"ok": False, "error": "invalid timeout"}), 400
        results: dict[str, Any] = {}
        for protocol, fn in (("tcp", send_tcp), ("udp", send_udp)):
            try:
                results[protocol] = {"ok": True, "result": fn(app.config[f"{protocol}_host"], app.config[f"{protocol}_port"], message, float(timeout))}
            except (OSError, TimeoutError, ValueError, socket.timeout) as exc:
                results[protocol] = {"ok": False, "error": str(exc) or "transport failure"}
        return jsonify({"ok": all(item["ok"] for item in results.values()), "request": message, "results": results})

    @app.get("/api/remote/status")
    def remote_status():
        return jsonify({"ok": True, "local_ip": _local_ip(), **app.extensions["remote_lab"].status()})

    @app.post("/api/remote/server/start")
    def remote_server_start():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        protocols = body.get("protocols", ["tcp", "udp"])
        if not isinstance(protocols, list) or not protocols or any(item not in ("tcp", "udp") for item in protocols):
            return jsonify({"ok": False, "error": "protocols must contain tcp, udp, or both"}), 400
        try:
            tcp_port = _remote_port(body.get("tcp_port", 9101), "tcp_port")
            udp_port = _remote_port(body.get("udp_port", 9102), "udp_port")
            welcome = body.get("welcome", "Welcome to PacketLab")
            if not isinstance(welcome, str) or not welcome.strip() or len(welcome.encode("utf-8")) > MAX_MESSAGE_LENGTH:
                raise ValueError("welcome must be a non-empty message up to 4096 bytes")
            hosted_message = body.get("hosted_message", "")
            if not isinstance(hosted_message, str) or len(hosted_message.encode("utf-8")) > MAX_MESSAGE_LENGTH:
                raise ValueError("hosted_message must be text up to 4096 bytes")
            hosted_file = body.get("hosted_file")
            if hosted_file is not None:
                if not isinstance(hosted_file, dict) or not isinstance(hosted_file.get("name"), str) or not isinstance(hosted_file.get("content"), str):
                    raise ValueError("hosted_file must contain a file name and base64 content")
                try:
                    decoded_file = base64.b64decode(hosted_file["content"], validate=True)
                except (ValueError, TypeError):
                    raise ValueError("hosted_file content must be valid base64")
                if len(decoded_file) > MAX_UDP_TRANSFER_BYTES:
                    raise ValueError(f"hosted_file must be at most {MAX_UDP_TRANSFER_BYTES} bytes")
            if "udp" in protocols and not hosted_message.strip() and hosted_file is None:
                raise ValueError("UDP server needs a hosted message or file")
            result = app.extensions["remote_lab"].start(protocols, tcp_port, udp_port, welcome, hosted_message, hosted_file)
            return jsonify({"ok": True, **result})
        except (OSError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc) or "unable to start remote server"}), 400

    @app.post("/api/remote/server/stop")
    def remote_server_stop():
        app.extensions["remote_lab"].stop()
        return jsonify({"ok": True, **app.extensions["remote_lab"].status()})

    @app.post("/api/remote/server/send")
    def remote_server_send():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        protocol = str(body.get("protocol", "")).lower()
        message = body.get("message")
        if protocol != "tcp":
            return jsonify({"ok": False, "error": "server chat is available for TCP only"}), 400
        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400
        if len(message.encode("utf-8")) > MAX_MESSAGE_LENGTH:
            return jsonify({"ok": False, "error": "message is too long"}), 400
        try:
            result = app.extensions["remote_lab"].send_from_server(protocol, message)
            return jsonify({"ok": True, "result": result})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc) or "unable to send server message"}), 504

    @app.get("/api/remote/client/receive")
    def remote_client_receive():
        session_id = request.args.get("session_id", "")
        if not session_id:
            return jsonify({"ok": False, "error": "session_id is required"}), 400
        try:
            messages = app.extensions["remote_lab"].receive_session(session_id)
            return jsonify({"ok": True, "messages": messages})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/remote/client/send")
    def remote_client_send():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        protocol = str(body.get("protocol", "")).lower()
        message = body.get("message")
        try:
            host = _remote_host(body.get("host"))
            port = _remote_port(body.get("port"), "port")
            timeout = body.get("timeout", 3.0)
            if protocol not in ("tcp", "udp"):
                raise ValueError("protocol must be tcp or udp")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("message must be a non-empty string")
            if len(message.encode("utf-8")) > MAX_MESSAGE_LENGTH:
                raise ValueError("message is too long")
            if not isinstance(timeout, (int, float)) or not 0.05 <= timeout <= MAX_TIMEOUT:
                raise ValueError(f"timeout must be between 0.05 and {MAX_TIMEOUT} seconds")
            fn = send_tcp if protocol == "tcp" else send_udp
            session_id = body.get("session_id")
            if session_id:
                result = app.extensions["remote_lab"].send_session(session_id, protocol, message, float(timeout))
            else:
                result = fn(host, port, message, float(timeout))
            return jsonify({"ok": True, "result": result})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (OSError, TimeoutError, socket.timeout) as exc:
            return jsonify({"ok": False, "error": str(exc) or "remote transport failure"}), 504

    @app.post("/api/remote/client/connect")
    def remote_client_connect():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        try:
            protocol = str(body.get("protocol", "")).lower()
            if protocol not in ("tcp", "udp"):
                raise ValueError("protocol must be tcp or udp")
            host = _remote_host(body.get("host"))
            port = _remote_port(body.get("port"), "port")
            timeout = body.get("timeout", 3.0)
            if not isinstance(timeout, (int, float)) or not 0.05 <= timeout <= MAX_TIMEOUT:
                raise ValueError(f"timeout must be between 0.05 and {MAX_TIMEOUT} seconds")
            return jsonify({"ok": True, "result": app.extensions["remote_lab"].connect(protocol, host, port, float(timeout))})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (OSError, TimeoutError, socket.timeout) as exc:
            return jsonify({"ok": False, "error": str(exc) or "remote connection failed"}), 504

    @app.post("/api/remote/client/contact")
    def remote_client_contact():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        try:
            host = _remote_host(body.get("host"))
            port = _remote_port(body.get("port"), "port")
            timeout = body.get("timeout", 3.0)
            if not isinstance(timeout, (int, float)) or not 0.05 <= timeout <= MAX_TIMEOUT:
                raise ValueError(f"timeout must be between 0.05 and {MAX_TIMEOUT} seconds")
            return jsonify({"ok": True, "result": app.extensions["remote_lab"].contact_udp(host, port, float(timeout))})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (OSError, TimeoutError, socket.timeout) as exc:
            return jsonify({"ok": False, "error": str(exc) or "UDP contact failed"}), 504

    return app


def start_socket_servers(app: Flask) -> None:
    if app.extensions.get("packetlab_servers"):
        return
    tcp_server = TCPServer(app.config["tcp_host"], app.config["tcp_port"]).start()
    try:
        udp_server = UDPServer(app.config["udp_host"], app.config["udp_port"]).start()
    except Exception:
        tcp_server.stop()
        raise
    app.extensions["packetlab_servers"] = (tcp_server, udp_server)


app = create_app()

if __name__ == "__main__":
    start_socket_servers(app)
    app.run(host="127.0.0.1", port=int(os.getenv("PACKETLAB_API_PORT", "5000")))
