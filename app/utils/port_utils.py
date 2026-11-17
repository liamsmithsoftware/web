"""
ابزارهای استاندارد (فقط کتابخانه استاندارد پایتون) برای بررسی اشغال بودن پورت،
تشخیص اجرای قبلی همین برنامه بدون تداخل با پروکسی سیستم یا سرویس‌های غیر HTTP،
و انتخاب خودکار پورت آزاد جایگزین در ویندوز، لینوکس و macOS.
"""
from __future__ import annotations

import http.client
import json
import os
import socket

APP_ID = "website-content-collector"


def _can_bind_ipv4(host: str, port: int) -> bool:
    """Test whether the OS allows binding this IPv4 (host, port) right now."""
    if not (1 <= int(port) <= 65535):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            # در ویندوز هرگز نباید از SO_REUSEADDR برای تست آزاد بودن پورت استفاده کرد
            # چون ویندوز اجازه bind روی پورت در حال شنود برنامه دیگر را می‌دهد!
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, excl, 1)
                except OSError:
                    pass
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def _has_active_listener(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    """Return True if any TCP service (IPv4 or dual-stack) accepts connections on (host, port)."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::", "localhost") else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((probe_host, int(port))) == 0
    except OSError:
        return False


def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """
    True only when nothing is listening on `port` AND the OS allows binding it.
    Checks both 0.0.0.0 and 127.0.0.1 when binding wildcard addresses so Uvicorn
    never hits WinError 10048 or WinError 10013 (Hyper-V reserved port ranges).
    """
    if not (1 <= int(port) <= 65535):
        return False

    normalized = "0.0.0.0" if host in ("", "::") else host
    hosts_to_check = ["0.0.0.0", "127.0.0.1"] if normalized == "0.0.0.0" else [normalized]

    for candidate_host in hosts_to_check:
        if not _can_bind_ipv4(candidate_host, port):
            return False

    if _has_active_listener(port, normalized):
        return False

    return True


def is_our_app_running(
    port: int,
    host: str = "127.0.0.1",
    *,
    app_id: str = APP_ID,
    timeout: float = 1.5,
) -> bool:
    """
    Safely check whether the process occupying `port` is this exact application.

    Uses raw `http.client.HTTPConnection` instead of `urllib.request.urlopen` so:
      1) Windows system proxy / VPN environment variables NEVER intercept localhost;
      2) Non-HTTP services, empty responses (`BadStatusLine`), TLS alerts, or binary
         protocols NEVER crash the launcher and simply return False.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::", "localhost") else host
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(probe_host, int(port), timeout=timeout)
        conn.request(
            "GET",
            "/healthz",
            headers={
                "Host": f"{probe_host}:{int(port)}",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            return False
        raw = response.read(1024).decode("utf-8", "replace")
        payload = json.loads(raw)
        return (
            isinstance(payload, dict)
            and payload.get("app") == app_id
            and payload.get("status") == "ok"
        )
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def find_free_port(start_port: int, host: str = "0.0.0.0", span: int = 200) -> int:
    """
    Find an available TCP port starting at `start_port`.
    Scans `span` sequential ports first (wide enough to step past Windows Hyper-V
    100-port excluded blocks), then fallback ranges, and finally asks the OS kernel.
    """
    base = max(1024, int(start_port))
    upper = min(65535, base + max(1, int(span)))

    for candidate in range(base, upper + 1):
        if is_port_free(candidate, host):
            return candidate

    for fallback_base in (8080, 8500, 9000, 18000, 28000):
        for candidate in range(fallback_base, fallback_base + 50):
            if is_port_free(candidate, host):
                return candidate

    bind_host = "0.0.0.0" if host in ("", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1])
