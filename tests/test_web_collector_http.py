from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import run as run_launcher
from app.collectors.web_collector import WebSiteCollector
from app.database import DEFAULT_SETTINGS

FIXTURES = Path(__file__).parent / "fixtures"


class _MockNewsServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return  # silence test output

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = (FIXTURES / "sample_listing.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/rss.xml":
            body = (FIXTURES / "sample_rss.xml").read_bytes()
            self._send(200, "application/rss+xml; charset=utf-8", body)
        elif self.path == "/section/no-feed":
            html = (
                (FIXTURES / "sample_listing.html")
                .read_text(encoding="utf-8")
                .replace('<link rel="alternate" type="application/rss+xml" title="RSS" href="/rss.xml" />', "")
                .encode("utf-8")
            )
            self._send(200, "text/html; charset=utf-8", html)
        elif self.path.startswith("/news/5003"):
            body = (FIXTURES / "sample_article.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/healthz":
            # Simulate the sibling telegram app occupying a port!
            self._send(
                200,
                "application/json",
                b'{"status":"ok","app":"telegram-channel-collector"}',
            )
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, content_type: str, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def local_news_server():
    server = HTTPServer(("127.0.0.1", 0), _MockNewsServerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


async def test_web_collector_auto_discovers_rss_and_falls_back_to_html(local_news_server: str):
    collector = WebSiteCollector()
    settings = dict(DEFAULT_SETTINGS)

    # 1. Root page in 'auto' mode discovers /rss.xml automatically
    batch_rss = await collector.fetch_site_posts(
        site={"canonical_url": f"{local_news_server}/", "domain": "127.0.0.1"},
        settings={**settings, "collector_mode": "auto"},
    )
    assert batch_rss.source_mode == "rss"
    assert batch_rss.discovered_feed_url == f"{local_news_server}/rss.xml"
    assert len(batch_rss.posts) == 3

    # 2. Subpage without feed falls back cleanly to HTML scraping + enriches full content!
    batch_html = await collector.fetch_site_posts(
        site={"canonical_url": f"{local_news_server}/section/no-feed", "domain": "127.0.0.1"},
        settings={**settings, "collector_mode": "auto"},
    )
    assert batch_html.source_mode == "html"
    assert len(batch_html.posts) == 3

    enriched = await collector.enrich_posts_with_full_content(batch_html.posts, settings)
    first_post = enriched[0]
    assert "شاخص‌های رشد بازار فناوری اطلاعات" in first_post.content

    # 3. Probe site over HTTP
    probe = await collector.probe_site(local_news_server, settings)
    assert probe["ok"] is True
    assert probe["source_mode"] == "rss"
    assert probe["found_posts_count"] == 3


def test_launcher_coexists_cleanly_when_telegram_app_uses_same_port(local_news_server: str):
    port = int(local_news_server.rsplit(":", 1)[1])
    # Port is busy, AND is_our_app returns False because /healthz belongs to telegram-channel-collector
    assert run_launcher.port_available("127.0.0.1", port) is False
    assert run_launcher.is_our_app("127.0.0.1", port) is False

    # handle_busy_port automatically picks a free fallback port instead of exiting!
    next_free = run_launcher.handle_busy_port("127.0.0.1", port)
    assert next_free != port
