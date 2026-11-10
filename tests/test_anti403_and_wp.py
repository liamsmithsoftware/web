from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.collectors.html_scraper import extract_listing_posts
from app.collectors.http_fetcher import SmartHttpFetcher, resolve_proxy_candidates
from app.collectors.web_collector import WebSiteCollector
from app.collectors.wp_api_parser import parse_wp_posts_json
from app.database import DEFAULT_SETTINGS
from app.utils.jalali import parse_any_datetime

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE_WP_POSTS_JSON = json.dumps(
    [
        {
            "id": 312516,
            "date": "2026-11-09T16:22:13",
            "date_gmt": "2026-11-09T21:22:13",
            "guid": {"rendered": "https://www.fdd.org/?p=312516"},
            "link": "https://www.fdd.org/analysis/idf-finds-weapons-stockpiles/",
            "title": {
                "rendered": "Nvidia&#8217;s Jensen Huang isn&#8217;t the first alleged &#8216;sexpionage&#8217; target"
            },
            "content": {
                "rendered": "<p>Detailed foreign policy analysis paragraph one.</p><p>Paragraph two with full context.</p>"
            },
            "excerpt": {"rendered": "<p>Short summary</p>"},
            "yoast_head": (
                '<meta property="og:site_name" content="FDD" />'
                '<meta property="og:image" content="https://media.fdd.org/wp-content/uploads/fdd.png" />'
            ),
        }
    ]
)

FDD_LIKE_HTML_NO_FEED_TAG = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>FDD | The Foundation for Defense of Democracies</title>
    <link rel="https://api.w.org/" href="/wp-json/" />
</head>
<body>
    <img src="/wp-content/themes/fdd/assets/images/logo.svg" />
    <a href="/issue/iran/">Iran</a>
    <div class="card">
        <a href="/analysis/lebanon-risks-its-future-by-allowing-hezbollah/">
            <span>November 9th | Hussain Abdul-Hussain</span>
            <h3>Lebanon Risks Its Future by Allowing Hezbollah to Weaponize Public Utilities</h3>
        </a>
    </div>
    <div class="card">
        <a href="/podcasts/leadership-lessons-from-napoleon/">
            <span>November 6, 2026 | Clifford D. May</span>
            <h3>Leadership Lessons from Napoleon</h3>
        </a>
    </div>
</body>
</html>
"""


class _WafAndWordPressMockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        # Case 1: /blocked-home/ returns 403 Cloudflare challenge on HTML & /feed/, but /wp-json/ works!
        if self.path in ("/blocked-home", "/blocked-home/", "/blocked-home/feed/", "/blocked-home/feed"):
            self._send(
                403,
                "text/html; charset=utf-8",
                b"<html><head><title>Just a moment...</title></head><body>Cloudflare</body></html>",
            )
            return

        if self.path.startswith("/wp-json/wp/v2/posts"):
            self._send(200, "application/json; charset=utf-8", SAMPLE_WP_POSTS_JSON.encode("utf-8"))
            return

        # Case 2: /fdd-like/ hides <link type="rss+xml"> in <head>, but exposes /fdd-like/feed/
        if self.path in ("/fdd-like", "/fdd-like/"):
            self._send(200, "text/html; charset=utf-8", FDD_LIKE_HTML_NO_FEED_TAG.encode("utf-8"))
            return

        if self.path in ("/fdd-like/feed/", "/fdd-like/feed"):
            self._send(200, "application/rss+xml; charset=utf-8", (FIXTURES / "sample_rss.xml").read_bytes())
            return

        # Case 3: Feed endpoint that rejects generic navigation Accept with 403 unless Referer + RSS Accept sent
        if self.path == "/strict-feed.xml":
            accept = self.headers.get("Accept", "")
            referer = self.headers.get("Referer", "")
            if "application/rss+xml" in accept and referer:
                self._send(
                    200,
                    "application/rss+xml; charset=utf-8",
                    (FIXTURES / "sample_rss.xml").read_bytes(),
                )
            else:
                self._send(403, "text/html", b"403 Forbidden by WAF")
            return

        self._send(404, "text/plain", b"not found")

    def _send(self, code: int, content_type: str, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def waf_server():
    server = HTTPServer(("127.0.0.1", 0), _WafAndWordPressMockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_parse_english_named_and_ordinal_dates():
    dt1 = parse_any_datetime("November 6, 2026", tz_name="UTC")
    assert dt1 == datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)

    dt2 = parse_any_datetime("9th Nov 2026, 2:30 PM", tz_name="UTC")
    assert dt2 == datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc)

    dt3 = parse_any_datetime("November 9th | David Daoud", tz_name="UTC")
    assert dt3 is not None
    assert dt3.month == 11 and dt3.day == 9


def test_parse_wordpress_rest_api_json():
    site_title, posts = parse_wp_posts_json(
        SAMPLE_WP_POSTS_JSON, base_url="https://www.fdd.org/", tz_name="Asia/Tehran"
    )
    assert site_title == "FDD"
    assert len(posts) == 1
    p = posts[0]
    assert "Nvidia’s Jensen Huang" in p.title
    assert "Detailed foreign policy analysis" in p.content
    assert p.media_url == "https://media.fdd.org/wp-content/uploads/fdd.png"
    assert p.published_at == datetime(2026, 11, 9, 21, 22, 13, tzinfo=timezone.utc)


def test_fdd_like_html_scraping_skips_issue_links_and_extracts_clean_titles():
    site_title, posts = extract_listing_posts(
        FDD_LIKE_HTML_NO_FEED_TAG, page_url="https://www.fdd.org/", tz_name="UTC"
    )
    assert site_title == "FDD"
    urls = [p.post_url for p in posts]
    assert "https://www.fdd.org/issue/iran" not in urls
    assert len(posts) == 2
    assert posts[0].title == "Lebanon Risks Its Future by Allowing Hezbollah to Weaponize Public Utilities"
    assert posts[0].published_at is not None


async def test_wordpress_hidden_feed_and_403_rescue_via_wp_json(waf_server: str):
    collector = WebSiteCollector()
    settings = dict(DEFAULT_SETTINGS)

    # 1. WordPress page without <link type="rss+xml"> automatically discovers /feed/
    batch_wp_feed = await collector.fetch_site_posts(
        site={"canonical_url": f"{waf_server}/fdd-like/"},
        settings=settings,
    )
    assert batch_wp_feed.source_mode == "rss"
    assert batch_wp_feed.discovered_feed_url == f"{waf_server}/fdd-like/feed/"
    assert len(batch_wp_feed.posts) == 3

    # 2. Root site returning 403 on / and /feed/ automatically rescues via /wp-json/wp/v2/posts!
    batch_rescued = await collector.fetch_site_posts(
        site={"canonical_url": f"{waf_server}/"},
        settings=settings,
    )
    assert batch_rescued.source_mode == "wp_api"
    assert len(batch_rescued.posts) == 1

    # 3. Strict 403 feed endpoint succeeds via Pass 3 feed-header + origin-referer retry
    batch_strict_feed = await collector.fetch_site_posts(
        site={"canonical_url": f"{waf_server}/strict-feed.xml"},
        settings=settings,
    )
    assert batch_strict_feed.source_mode == "rss"
    assert len(batch_strict_feed.posts) == 3


async def test_dead_system_proxy_falls_back_gracefully_to_direct(monkeypatch, waf_server: str):
    monkeypatch.setattr(
        "urllib.request.getproxies",
        lambda: {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"},
    )
    cands = resolve_proxy_candidates(dict(DEFAULT_SETTINGS), "https://www.fdd.org/")
    assert cands == [("http://127.0.0.1:1", False), (None, False)]

    fetcher = SmartHttpFetcher(dict(DEFAULT_SETTINGS))
    resp = await fetcher.get(f"{waf_server}/fdd-like/")
    assert resp.status_code == 200
