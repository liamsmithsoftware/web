from __future__ import annotations

import json
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import launcher
import run as run_launcher
from app.utils.port_utils import find_free_port, is_our_app_running, is_port_free


class _ZeroByteDropHandler(socketserver.BaseRequestHandler):
    """Simulates a service that accepts TCP and closes immediately (triggers BadStatusLine)."""

    def handle(self) -> None:
        try:
            self.request.recv(64)
        except OSError:
            pass
        # Close without sending any HTTP status line!


class _BinaryGarbageHandler(socketserver.BaseRequestHandler):
    """Simulates a non-HTTP TLS/SOCKS/custom binary service on the port."""

    def handle(self) -> None:
        try:
            self.request.sendall(b"\x16\x03\x01\x00\x2a\xff\x00\xfe\r\n\r\n")
        except OSError:
            pass


class _OurAppHealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        payload = json.dumps({"status": "ok", "app": "website-content-collector"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_tcp_server(handler_cls, host: str = "127.0.0.1"):
    class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = _Server((host, 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def test_zero_byte_non_http_service_does_not_crash_with_bad_status_line():
    """Regression test for http.client.BadStatusLine when port 8000 has a non-HTTP listener."""
    srv, busy_port = _start_tcp_server(_ZeroByteDropHandler)
    try:
        assert is_port_free(busy_port, "0.0.0.0") is False
        assert is_our_app_running(busy_port, "127.0.0.1") is False

        picked_by_launcher = launcher.pick_port(busy_port, "0.0.0.0")
        assert picked_by_launcher != busy_port
        assert is_port_free(picked_by_launcher, "0.0.0.0") is True

        picked_by_run = run_launcher.handle_busy_port("0.0.0.0", busy_port)
        assert picked_by_run != busy_port
        assert is_port_free(picked_by_run, "0.0.0.0") is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_binary_garbage_service_switches_cleanly_to_another_port():
    srv, busy_port = _start_tcp_server(_BinaryGarbageHandler)
    try:
        assert launcher.pick_port(busy_port, "0.0.0.0") != busy_port
        assert run_launcher.handle_busy_port("0.0.0.0", busy_port) != busy_port
    finally:
        srv.shutdown()
        srv.server_close()


def test_skips_multiple_consecutive_occupied_ports():
    base_port = find_free_port(19500, host="127.0.0.1")
    sockets: list[socket.socket] = []
    try:
        occupied = []
        for offset in range(3):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", base_port + offset))
                s.listen(1)
                sockets.append(s)
                occupied.append(base_port + offset)
            except OSError:
                s.close()

        assert len(occupied) >= 1
        chosen = launcher.pick_port(occupied[0], "0.0.0.0")
        assert chosen not in occupied
        assert is_port_free(chosen, "0.0.0.0") is True
    finally:
        for s in sockets:
            s.close()


def test_bypasses_system_proxy_env_vars_and_detects_our_own_running_instance(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")

    server = HTTPServer(("127.0.0.1", 0), _OurAppHealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert is_our_app_running(port, "127.0.0.1") is True
        # launcher keeps same port so open_browser opens existing instance
        assert launcher.pick_port(port, "0.0.0.0") == port
        # run.py exits cleanly with code 0 instead of throwing WinError 10048
        with pytest.raises(SystemExit) as exc_info:
            run_launcher.handle_busy_port("0.0.0.0", port)
        assert exc_info.value.code == 0
    finally:
        server.shutdown()
        server.server_close()


def test_launcher_cli_option_parser_handles_explicit_port_and_host():
    host, port, strict, rest = launcher._extract_cli_options(["--port", "8000", "--reload"])
    assert host == launcher.DEFAULT_HOST
    assert port == 8000
    assert strict is False
    assert rest == ["--reload"]

    host2, port2, strict2, rest2 = launcher._extract_cli_options(
        ["--host=127.0.0.1", "--port=9000", "--strict-port"]
    )
    assert host2 == "127.0.0.1"
    assert port2 == 9000
    assert strict2 is True
    assert "--host=127.0.0.1" in rest2
    assert "--strict-port" in rest2
