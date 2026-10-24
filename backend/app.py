"""Flask HTTP API for exercising PacketLab TCP and UDP transports."""
from __future__ import annotations

import os
import socket
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

from .socket_client import send_tcp, send_udp
from .tcp_server import TCPServer
from .udp_server import UDPServer

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
