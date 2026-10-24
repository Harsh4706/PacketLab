from backend.app import create_app


def test_health_and_validation():
    client = create_app({"TESTING": True}).test_client()
    assert client.get("/api/health").status_code == 503
    response = client.post("/api/send", json={"protocol": "smtp", "message": "x"})
    assert response.status_code == 400


def test_send_unavailable_is_bounded():
    client = create_app({"TESTING": True, "tcp_port": 1}).test_client()
    response = client.post("/api/send", json={"protocol": "tcp", "message": "hello", "timeout": 0.05})
    assert response.status_code == 504
