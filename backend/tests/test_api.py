from backend.app import create_app
import socket


def test_health_and_validation():
    client = create_app({"TESTING": True}).test_client()
    assert client.get("/api/health").status_code == 503
    response = client.post("/api/send", json={"protocol": "smtp", "message": "x"})
    assert response.status_code == 400


def test_send_unavailable_is_bounded():
    client = create_app({"TESTING": True, "tcp_port": 1}).test_client()
    response = client.post("/api/send", json={"protocol": "tcp", "message": "hello", "timeout": 0.05})
    assert response.status_code == 504


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_remote_lab_server_and_client_round_trip():
    tcp_port, udp_port = _free_port(), _free_port()
    app = create_app({"TESTING": True})
    client = app.test_client()
    started = client.post(
        "/api/remote/server/start",
        json={"protocols": ["tcp", "udp"], "tcp_port": tcp_port, "udp_port": udp_port},
    )
    assert started.status_code == 200
    try:
        for protocol, port in (("tcp", tcp_port), ("udp", udp_port)):
            connected = client.post(
                "/api/remote/client/connect",
                json={"host": "127.0.0.1", "port": port, "protocol": protocol},
            )
            assert connected.status_code == 200
            assert connected.get_json()["result"]["welcome"] == "Welcome to PacketLab"
            response = client.post(
                "/api/remote/client/send",
                json={"host": "127.0.0.1", "port": port, "protocol": protocol, "session_id": connected.get_json()["result"]["session_id"], "message": f"remote {protocol}"},
            )
            assert response.status_code == 200
            assert response.get_json()["result"]["success"] is True
    finally:
        assert client.post("/api/remote/server/stop").status_code == 200
