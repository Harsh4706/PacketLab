from backend.socket_client import send_tcp, send_udp
from backend.tcp_server import TCPServer
from backend.udp_server import UDPServer


def test_tcp_and_udp_round_trip():
    tcp, udp = TCPServer().start(), UDPServer().start()
    try:
        assert send_tcp(tcp.host, tcp.port, {"hello": "tcp"})["echo"] == {"hello": "tcp"}
        assert send_udp(udp.host, udp.port, ["hello", "udp"])["echo"] == ["hello", "udp"]
    finally:
        tcp.stop()
        udp.stop()
