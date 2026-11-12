from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.collectors.base import CollectorError
from app.collectors.browser_fetcher import (
    CLEARANCE_CACHE,
    _GUI_REQUIRED_DOMAINS,
    _build_ua_override_params,
    _looks_like_js_challenge,
    fetch_via_local_browser,
)
from app.collectors.html_scraper import extract_listing_posts
from app.collectors.http_fetcher import HttpFetchResponse, SmartHttpFetcher, resolve_proxy_candidates
from app.collectors.sitemap_parser import extract_child_sitemaps, parse_sitemap_posts
from app.collectors.web_collector import WebSiteCollector, _https_upgrade_candidate, _is_sso_login_wall
from app.collectors.wp_api_parser import parse_wp_posts_json
from app.database import DEFAULT_SETTINGS
from app.utils.jalali import parse_any_datetime, utc_now
from app.utils.site_parser import extract_registrable_domain

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

IISS_AND_RASMUSSEN_LIKE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>The International Institute for Strategic Studies</title>
</head>
<body>
    <a href="/capabilities/political-analysis-and-advisory/"><strong>Political Analysis and Advisory</strong></a>
    <a href="/sectors/">Sectors</a>

    <div class="hero-card">
        <a href="/online-analysis/online-analysis/2026/11/the-consequences-of-the-us-midterm-elections/">
            <span>Online Analysis</span>
            <strong>The consequences of the US midterm elections</strong>
            <p>On 3 November 2026, American voters delivered a resounding defeat in midterm congressional elections across key states and districts, reshaping foreign policy priorities for the coming two years.</p>
            <span>By Paul Fraioli, Benjamin Rhode6th November 2026</span>
        </a>
    </div>

    <div class="item-card">
        <a href="/publications/strategic-comments/2026/11/lebanon-a-ceasefire-without-peace/">
            <span>Strategic Comments</span>
            <strong>Lebanon: a ceasefire without peace</strong>
            <span>Available5th November 2026</span>
        </a>
    </div>
</body>
</html>
"""

SAMPLE_SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap><loc>https://example.org/page-sitemap.xml</loc></sitemap>
    <sitemap><loc>https://example.org/post-sitemap.xml</loc></sitemap>
    <sitemap><loc>https://example.org/news-sitemap.xml</loc></sitemap>
</sitemapindex>
"""

SAMPLE_URLSET_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
    <url>
        <loc>https://www.iiss.org/careers/jobs/2026/11/office-manager/</loc>
        <lastmod>2026-11-10T10:00:00+00:00</lastmod>
    </url>
    <url>
        <loc>https://www.iiss.org/podcasts/arms-control/2026/10/episode-1/transcript/</loc>
        <lastmod>2026-11-10T09:00:00+00:00</lastmod>
    </url>
    <url>
        <loc>https://www.iiss.org/online-analysis/2026/11/older-october-report/</loc>
        <lastmod>2026-10-20T08:00:00+00:00</lastmod>
    </url>
    <url>
        <loc>https://www.iiss.org/online-analysis/2026/11/newest-strategic-report/</loc>
        <news:news>
            <news:publication><news:name>IISS</news:name></news:publication>
            <news:publication_date>2026-11-10T11:30:00+00:00</news:publication_date>
            <news:title>Newest Strategic Report on European Defence</news:title>
        </news:news>
    </url>
</urlset>
"""


class _WafAndWordPressMockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
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

        if self.path in ("/fdd-like", "/fdd-like/"):
            self._send(200, "text/html; charset=utf-8", FDD_LIKE_HTML_NO_FEED_TAG.encode("utf-8"))
            return

        if self.path in ("/fdd-like/feed/", "/fdd-like/feed"):
            self._send(200, "application/rss+xml; charset=utf-8", (FIXTURES / "sample_rss.xml").read_bytes())
            return

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

    # Glued date without space on iiss.org ("Available6th November 2026")
    dt4 = parse_any_datetime("By Wakana Asano6th November 2026", tz_name="UTC")
    assert dt4 == datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)


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


def test_fdd_and_iiss_html_scraping_extracts_clean_titles_and_tail_dates():
    site_title, posts = extract_listing_posts(
        FDD_LIKE_HTML_NO_FEED_TAG, page_url="https://www.fdd.org/", tz_name="UTC"
    )
    assert site_title == "FDD"
    urls = [p.post_url for p in posts]
    assert "https://www.fdd.org/issue/iran" not in urls
    assert len(posts) == 2
    assert posts[0].title == "Lebanon Risks Its Future by Allowing Hezbollah to Weaponize Public Utilities"

    iiss_title, iiss_posts = extract_listing_posts(
        IISS_AND_RASMUSSEN_LIKE_HTML, page_url="https://www.iiss.org/", tz_name="UTC"
    )
    assert "International Institute for Strategic Studies" in (iiss_title or "")
    # /capabilities/... and /sectors/ must be skipped
    assert len(iiss_posts) == 2
    assert iiss_posts[0].title == "The consequences of the US midterm elections"
    assert "American voters delivered" in iiss_posts[0].content
    # Picks tail publication date (6 Nov 2026), NOT body text date (3 Nov 2026)!
    assert iiss_posts[0].published_at == datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    assert iiss_posts[1].title == "Lebanon: a ceasefire without peace"
    assert iiss_posts[1].published_at == datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)


def test_sitemap_priority_and_news_sitemap_extraction():
    children = extract_child_sitemaps(SAMPLE_SITEMAP_INDEX)
    assert children[0] == "https://example.org/news-sitemap.xml"
    assert children[1] == "https://example.org/post-sitemap.xml"

    s_title, s_posts = parse_sitemap_posts(
        SAMPLE_URLSET_SITEMAP, base_url="https://www.iiss.org/", tz_name="UTC"
    )
    assert s_title == "IISS"
    # Careers/jobs and /transcript/ are skipped; remaining 2 sorted newest-first!
    assert len(s_posts) == 2
    assert s_posts[0].title == "Newest Strategic Report on European Defence"
    assert s_posts[0].published_at == datetime(2026, 11, 10, 11, 30, tzinfo=timezone.utc)


def test_http_to_https_upgrade_and_cloudflare_522_error_message():
    assert _https_upgrade_candidate("http://www.bicom.org.uk/") == "https://www.bicom.org.uk/"
    assert _https_upgrade_candidate("http://ravandinstitute.com") == "https://ravandinstitute.com/"
    assert _https_upgrade_candidate("http://127.0.0.1:8000/") is None

    cf_522 = HttpFetchResponse(url="https://ravandinstitute.com/", status_code=522, text="", headers={})
    with pytest.raises(CollectorError) as exc_info:
        cf_522.raise_for_status()
    assert "522" in str(exc_info.value)
    assert "سرور میزبانی اصلی" in str(exc_info.value)


async def test_browser_cdp_escalation_and_clearance_cache_on_js_challenge(monkeypatch):
    CLEARANCE_CACHE.clear()
    CLEARANCE_CACHE.put(
        "https://www.iiss.org/online-analysis/",
        None,
        cookie_header="cf_clearance=solved_token_123",
        user_agent="Mozilla/5.0 Chrome/131.0.0.0",
    )
    cached = CLEARANCE_CACHE.get("https://www.iiss.org/publications/", None)
    assert cached is not None
    assert "cf_clearance=solved_token_123" in cached.cookie_header

    # Simulate a domain where httpx & curl_cffi get 403 JS challenge, then browser_cdp solves it!
    async def _fake_httpx(self, url, **kwargs):
        return HttpFetchResponse(
            url=url,
            status_code=403,
            text="<html><title>Just a moment...</title></html>",
            headers={"cf-mitigated": "challenge"},
            via="httpx",
        )

    async def _fake_curl(self, url, **kwargs):
        return None

    async def _fake_browser(url, **kwargs):
        return {
            "url": url,
            "status_code": 200,
            "text": IISS_AND_RASMUSSEN_LIKE_HTML,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "via": "browser_cdp",
        }

    monkeypatch.setattr(SmartHttpFetcher, "_fetch_httpx", _fake_httpx)
    monkeypatch.setattr(SmartHttpFetcher, "_fetch_curl_impersonate", _fake_curl)
    monkeypatch.setattr("app.collectors.http_fetcher.fetch_via_local_browser", _fake_browser)
    monkeypatch.setattr("app.collectors.http_fetcher._is_loopback_target", lambda u: False)

    fetcher = SmartHttpFetcher(dict(DEFAULT_SETTINGS))
    solved = await fetcher.get("https://www.iiss.org/")
    assert solved.status_code == 200
    assert solved.via == "browser_cdp"
    CLEARANCE_CACHE.clear()


async def test_wordpress_hidden_feed_and_403_rescue_via_wp_json(waf_server: str):
    collector = WebSiteCollector()
    settings = dict(DEFAULT_SETTINGS)

    batch_wp_feed = await collector.fetch_site_posts(
        site={"canonical_url": f"{waf_server}/fdd-like/"},
        settings=settings,
    )
    assert batch_wp_feed.source_mode == "rss"
    assert batch_wp_feed.discovered_feed_url == f"{waf_server}/fdd-like/feed/"
    assert len(batch_wp_feed.posts) == 3

    batch_rescued = await collector.fetch_site_posts(
        site={"canonical_url": f"{waf_server}/"},
        settings=settings,
    )
    assert batch_rescued.source_mode == "wp_api"
    assert len(batch_rescued.posts) == 1

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


LAWFARE_AND_CRITICAL_THREATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Lawfare | Lawfare</title></head>
<body>
    <a href="https://www.lawfaremedia.org/article/highlights">View All Highlights</a>
    <a href="https://www.lawfaremedia.org/contributors/jhealey">Jason Healey</a>
    <ul>
        <li>
            <img src="https://lawfare-assets-new.azureedge.net/featured/7457.jpeg" />
            <h3>
                <a href="/article/ai-can-(mostly)-solve-cyber-regulatory-harmonization-and-efficiency">
                    AI Can (Mostly) Solve Cyber Regulatory Harmonization and Efficiency
                </a>
            </h3>
            <a href="/contributors/jhealey">Jason Healey</a>
            <span>Nov 10, 2026</span>
            <p>With AI-driven compliance, it has never been less important to fix cyber-regulatory inefficiencies.</p>
        </li>
    </ul>
</body>
</html>
"""

CRITICAL_THREATS_LIKE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Critical Threats</title></head>
<body>
    <a href="/webusers/password/new">Forgot Password?</a>
    <article>
        <a href="/analysis/iran-update-november-8-2026">
            article
            <strong>Iran Update, November 8, 2026</strong>
            <span>2 days ago</span>
        </a>
    </article>
    <article>
        <a href="/analysis/al-shabaab-somalia-africa-file">
            article
            <strong>Al Shabaab Ambush in South West State: Africa File</strong>
            <span>3 days ago</span>
        </a>
    </article>
</body>
</html>
"""

MORNING_CONSULT_MC_CONTENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Morning Consult Intelligence</title></head>
<body>
    <a href="https://intel.morningconsult.com/mc-content/verticals/economics">Economics</a>
    <a href="https://morningconsult.com/reports">Explore</a>
    <a href="https://accounts.morningconsult.com/oauth2/authorize">Sign in</a>
    <div class="card">
        <a href="https://pro.morningconsult.com/trackers/consumer-spending-tracker">
            <strong>U.S. Consumer Spending Tracker</strong>
        </a>
        <span>November 10, 2026</span>
    </div>
    <div class="card">
        <a href="https://pro.morningconsult.com/analyst-reports/political-scrutiny-after-2026-midterms">
            <strong>Big business primed to face the heat after 2026 midterms</strong>
        </a>
        <span>November 8, 2026</span>
    </div>
</body>
</html>
"""


def test_relative_english_and_persian_dates():
    now = utc_now()

    dt_18h = parse_any_datetime("18 hours ago", tz_name="UTC")
    assert dt_18h is not None
    assert 17.9 * 3600 <= (now - dt_18h).total_seconds() <= 18.1 * 3600

    dt_2d = parse_any_datetime("2 days ago", tz_name="UTC")
    assert dt_2d is not None
    assert 47.9 * 3600 <= (now - dt_2d).total_seconds() <= 48.1 * 3600

    dt_fa = parse_any_datetime("۳ ساعت پیش", tz_name="Asia/Tehran")
    assert dt_fa is not None
    assert 2.9 * 3600 <= (now - dt_fa).total_seconds() <= 3.1 * 3600

    dt_yest = parse_any_datetime("Yesterday at 2:30 PM", tz_name="UTC")
    assert dt_yest is not None
    assert dt_yest.hour == 14 and dt_yest.minute == 30


def test_lawfare_critical_threats_and_morning_consult_html_extraction():
    # 1. Lawfare: parentheses in slug, skips /contributors/ and 'View All Highlights', extracts summary & date
    lf_title, lf_posts = extract_listing_posts(
        LAWFARE_AND_CRITICAL_THREATS_HTML, page_url="https://www.lawfaremedia.org/", tz_name="UTC"
    )
    assert lf_title == "Lawfare"
    assert len(lf_posts) == 1
    assert lf_posts[0].post_url == (
        "https://www.lawfaremedia.org/article/ai-can-(mostly)-solve-cyber-regulatory-harmonization-and-efficiency"
    )
    assert "With AI-driven compliance" in lf_posts[0].content
    assert lf_posts[0].published_at == datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)

    # 2. Critical Threats: strips 'article' badge inside <article><a><strong>, parses explicit & relative dates
    ct_title, ct_posts = extract_listing_posts(
        CRITICAL_THREATS_LIKE_HTML, page_url="https://www.criticalthreats.org/", tz_name="UTC"
    )
    assert ct_title == "Critical Threats"
    assert len(ct_posts) == 2
    assert ct_posts[0].title == "Iran Update, November 8, 2026"
    assert ct_posts[0].published_at == datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
    assert ct_posts[1].title == "Al Shabaab Ambush in South West State: Africa File"
    assert ct_posts[1].published_at is not None

    # 3. Morning Consult: matches sibling subdomains pro.morningconsult.com <-> intel.morningconsult.com,
    #    skips /verticals/ and accounts.morningconsult.com
    mc_title, mc_posts = extract_listing_posts(
        MORNING_CONSULT_MC_CONTENT_HTML,
        page_url="https://intel.morningconsult.com/mc-content",
        tz_name="UTC",
    )
    assert len(mc_posts) == 2
    assert mc_posts[0].post_url == "https://pro.morningconsult.com/trackers/consumer-spending-tracker"
    assert mc_posts[0].published_at == datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    assert mc_posts[1].post_url == (
        "https://pro.morningconsult.com/analyst-reports/political-scrutiny-after-2026-midterms"
    )


def test_client_hints_metadata_turnstile_detection_and_registrable_domain():
    # Registrable domain handles .org.uk and domains starting with 'w'
    assert extract_registrable_domain("http://www.bicom.org.uk/news") == "bicom.org.uk"
    assert extract_registrable_domain("https://worldnews.com/feed") == "worldnews.com"
    assert extract_registrable_domain("https://intel.morningconsult.com/") == "morningconsult.com"

    # Client Hints userAgentMetadata removes HeadlessChrome and sets Edge/Chrome brands cleanly
    ua_edge = _build_ua_override_params(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HeadlessChrome/131.0.6778.86 Safari/537.36 Edg/131.0.2903.70",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    assert "HeadlessChrome" not in ua_edge["userAgent"]
    brand_names = [b["brand"] for b in ua_edge["userAgentMetadata"]["brands"]]
    assert "Microsoft Edge" in brand_names
    assert "HeadlessChrome" not in brand_names

    # Modern Turnstile challenge where <title> is the domain name
    turnstile_dom = (
        "<html><head><title>rasmussenglobal.com</title></head>"
        "<body><div id=\"turnstile-wrapper\"></div><script>window._cf_chl_opt={};</script></body></html>"
    )
    assert _looks_like_js_challenge(turnstile_dom) is True
    assert _looks_like_js_challenge(LAWFARE_AND_CRITICAL_THREATS_HTML) is False


async def test_two_pass_gui_browser_fallback_when_headless_loops_on_turnstile(monkeypatch):
    _GUI_REQUIRED_DOMAINS.clear()
    calls = []

    async def _fake_attempt(browser_bin, url, *, headless, **kwargs):
        calls.append(headless)
        if headless:
            return {
                "url": url,
                "status_code": 403,
                "text": "<html><title>Just a moment...</title><script>window._cf_chl_opt={};</script></html>",
                "headers": {"content-type": "text/html"},
                "via": "browser_cdp",
                "solved": False,
            }
        return {
            "url": url,
            "status_code": 200,
            "text": LAWFARE_AND_CRITICAL_THREATS_HTML,
            "headers": {"content-type": "text/html"},
            "via": "browser_cdp_gui",
            "solved": True,
        }

    monkeypatch.setattr("app.collectors.browser_fetcher.find_local_browser_executable", lambda: "/usr/bin/chromium")
    monkeypatch.setattr("app.collectors.browser_fetcher._can_use_gui_fallback", lambda: True)
    monkeypatch.setattr("app.collectors.browser_fetcher._run_single_browser_attempt", _fake_attempt)

    res = await fetch_via_local_browser("https://www.bicom.org.uk/")
    assert res is not None
    assert res["status_code"] == 200
    assert res["via"] == "browser_cdp_gui"
    assert calls == [True, False]
    assert "bicom.org.uk" in _GUI_REQUIRED_DOMAINS
    _GUI_REQUIRED_DOMAINS.clear()


async def test_dead_404_subpath_rescues_from_root_and_sso_redirect_rescues_public_portal(
    monkeypatch, waf_server: str
):
    collector = WebSiteCollector()
    settings = dict(DEFAULT_SETTINGS)

    # 1. Dead subpath /nonexistent-news-path (404) rescues from root (wp_api on waf_server)
    batch_404 = await collector.fetch_site_posts(
        site={"canonical_url": f"{waf_server}/news"},
        settings=settings,
    )
    assert batch_404.source_mode == "wp_api"
    assert len(batch_404.posts) == 1

    # 2. SSO redirect detection & automatic rescue from pro.<domain> (intel.morningconsult.com -> pro.morningconsult.com)
    sso_resp = HttpFetchResponse(
        url="https://accounts.morningconsult.com/oauth2/aus267/v1/authorize?client_id=abc&redirect_uri=https://intel.morningconsult.com",
        status_code=200,
        text="<html><title>Sign in to Morning Consult</title></html>",
        headers={"content-type": "text/html"},
    )
    assert _is_sso_login_wall(sso_resp, "https://intel.morningconsult.com/") is True

    async def _mock_get(self, url, **kwargs):
        if "intel.morningconsult.com" in url:
            return sso_resp
        if "pro.morningconsult.com" in url:
            return HttpFetchResponse(
                url="https://intel.morningconsult.com/mc-content",
                status_code=200,
                text=MORNING_CONSULT_MC_CONTENT_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return HttpFetchResponse(url=url, status_code=404, text="not found", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    mc_batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://intel.morningconsult.com/"},
        settings=settings,
    )
    assert mc_batch.source_mode == "html"
    assert len(mc_batch.posts) == 2
    assert mc_batch.posts[0].title == "U.S. Consumer Spending Tracker"


IDI_SPARSE_HOMEPAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>The Israel Democracy Institute</title></head>
<body>
    <nav class="main-menu">
        <a href="/articles">Articles</a>
        <a href="/books">Books</a>
    </nav>
    <main>
        <a href="/tags-en/4512">Judicial Reform Tag</a>
        <a href="/media/25981/annual-democracy-index.pdf">Free Download</a>
        <article class="card">
            <a href="https://en.idi.org.il/articles/67075">
                <img src="https://en.idi.org.il/media/67075.jpg" alt="A Snapshot of Israel's New Knesset" />
            </a>
            <span>November 10, 2026</span>
            <p>An updated breakdown of legislative activity and committee leadership in the winter session.</p>
        </article>
    </main>
</body>
</html>
"""

IDI_ARTICLES_HUB_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Articles - The Israel Democracy Institute</title></head>
<body>
    <main>
        <div class="card">
            <a href="https://en.idi.org.il/articles/67075">
                <img src="/media/67075.jpg" alt="A Snapshot of Israel's New Knesset" />
            </a>
            <span>November 10, 2026</span>
            <p>An updated breakdown of legislative activity and committee leadership in the winter session.</p>
        </div>
        <div class="card">
            <a href="https://en.idi.org.il/articles/67071">
                <img src="/media/67071.jpg" alt="Local Authorities Under Wartime Budget Strain" />
            </a>
            <span>November 8, 2026</span>
            <p>Municipal budgets face acute deficits across northern and southern districts.</p>
        </div>
        <div class="card">
            <a href="https://en.idi.org.il/articles/67068" title="Constitutional Oversight Standards in Comparative Perspective">
                <img src="/media/default.png" alt="image" />
            </a>
            <span>November 6, 2026</span>
            <p>Comparative review of judicial review thresholds in parliamentary democracies.</p>
        </div>
    </main>
</body>
</html>
"""

MEMRI_CAROUSEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>MEMRI | Middle East Media Research Institute</title></head>
<body>
    <div class="breaking-ticker">
        <div class="card">
            <a href="/reports/irans-missile-program-enters-new-phase">
                Iran's Missile Program Enters A New...
            </a>
        </div>
    </div>
    <div class="slick-slider">
        <div class="slick-cloned card">
            <h3><a href="/reports/cloned-old-slide-should-be-ignored">Cloned Slide Should Be Ignored</a></h3>
            <span>September 01, 2026</span>
        </div>
        <div class="card">
            <h3><a href="/reports/older-featured-carousel-report-nov2">Featured Analysis From Early November</a></h3>
            <span>November 02, 2026</span>
        </div>
        <div class="card">
            <h3><a href="/reports/older-featured-carousel-report-oct30">Featured Regional Briefing Late October</a></h3>
            <span>October 30, 2026</span>
        </div>
    </div>
    <section class="latest-grid">
        <article class="card">
            <img src="https://www.memri.org/sites/default/files/missile.jpg" />
            <h3>
                <a href="/reports/irans-missile-program-enters-new-phase">
                    Iran's Missile Program Enters A New Phase After Recent Exercises
                </a>
            </h3>
            <span>November 10, 2026</span>
            <p>Detailed assessment of newly unveiled solid-fuel launchers and command telemetry.</p>
        </article>
        <article class="card">
            <h3>
                <a href="/reports/red-sea-maritime-security-nov9">
                    Red Sea Maritime Security Escort Operations Expand
                </a>
            </h3>
            <span>November 09, 2026</span>
            <p>Coalition naval task forces report increased commercial transit escorts.</p>
        </article>
    </section>
</body>
</html>
"""

AFCENT_MIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Air Forces Central &gt; News</title></head>
<body>
    <main>
        <div class="alist-item card">
            <h1>
                <a href="/News/Article-Display/Article/4021234/us-air-forces-central-conducts-readiness-exercise/">
                    U.S. Air Forces Central conducts coalition readiness exercise
                </a>
            </h1>
            <span>Nov. 4, 2026</span>
            <p>Coalition aircrews integrated across multiple command centers during the multi-day exercise.</p>
        </div>
        <div class="alist-item card">
            <h1>
                <a href="/News/Article-Display/Article/4019800/expeditionary-wing-completes-airlift-surge/">
                    Expeditionary wing completes theater airlift surge
                </a>
            </h1>
            <span>Sept. 10, 2026</span>
            <p>Logistics squadrons moved over 400 pallets of humanitarian and operational cargo.</p>
        </div>
    </main>
</body>
</html>
"""


async def test_idi_image_only_cards_and_sparse_homepage_hub_upgrade(monkeypatch):
    # 1. Direct extraction on /articles/ handles image-alt & a[title] cards with empty anchor text
    title, posts = extract_listing_posts(
        IDI_ARTICLES_HUB_HTML, page_url="https://en.idi.org.il/articles/", tz_name="UTC"
    )
    assert len(posts) == 3
    assert posts[0].title == "A Snapshot of Israel's New Knesset"
    assert posts[0].media_url == "https://en.idi.org.il/media/67075.jpg"
    assert posts[1].title == "Local Authorities Under Wartime Budget Strain"
    assert posts[2].title == "Constitutional Oversight Standards in Comparative Perspective"

    # 2. Root homepage `en.idi.org.il/` skips /tags-en/ and .pdf, and upgrades from 1 sparse card -> 3 cards on `/articles`
    async def _mock_get(self, url, **kwargs):
        if url.rstrip("/") == "https://en.idi.org.il":
            return HttpFetchResponse(
                url="https://en.idi.org.il/",
                status_code=200,
                text=IDI_SPARSE_HOMEPAGE_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if url.rstrip("/") == "https://en.idi.org.il/articles":
            return HttpFetchResponse(
                url="https://en.idi.org.il/articles/",
                status_code=200,
                text=IDI_ARTICLES_HUB_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return HttpFetchResponse(url=url, status_code=404, text="not found", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://en.idi.org.il/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert batch.source_mode == "html"
    assert len(batch.posts) == 3
    assert batch.posts[0].title == "A Snapshot of Israel's New Knesset"


def test_memri_carousel_clones_truncated_ticker_enrichment_and_date_sorting():
    site_title, posts = extract_listing_posts(
        MEMRI_CAROUSEL_HTML, page_url="https://www.memri.org/", tz_name="UTC"
    )
    assert site_title == "MEMRI"
    urls = [p.post_url for p in posts]
    # .slick-cloned slide is completely excluded
    assert all("cloned-old-slide" not in u for u in urls)
    assert len(posts) == 4

    # Out-of-order carousel dates (Nov 02 / Oct 30 before Nov 10) are sorted newest-first!
    assert [p.published_at for p in posts] == [
        datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc),
    ]
    # Truncated top-ticker title ("Iran's Missile Program Enters A New...") was upgraded from full card!
    assert posts[0].title == "Iran's Missile Program Enters A New Phase After Recent Exercises"
    assert posts[0].media_url == "https://www.memri.org/sites/default/files/missile.jpg"


async def test_wsj_public_dowjones_feeds_and_ipforum_cross_domain_redirect(monkeypatch):
    from app.collectors.web_collector import _resolve_known_public_feeds

    assert "https://feeds.content.dowjones.io/public/rss/RSSWorldNews" in _resolve_known_public_feeds(
        "https://www.wsj.com/"
    )
    assert _resolve_known_public_feeds("https://www.wsj.com/markets") == [
        "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"
    ]
    assert _resolve_known_public_feeds("https://www.ipforum.org/") == [
        "https://israelpolicyforum.org/feed/"
    ]

    wsj_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>WSJ.com: World News</title>
        <item>
            <title>Global Central Banks Weigh Late-Year Rate Moves</title>
            <link>https://www.wsj.com/articles/global-central-banks-weigh-rate-moves-12345</link>
            <pubDate>Tue, 10 Nov 2026 11:00:00 GMT</pubDate>
            <description>Policymakers signal caution ahead of December meetings.</description>
        </item>
    </channel></rss>"""

    ipf_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Israel Policy Forum</title>
        <item>
            <title>Regional Diplomacy Assessment</title>
            <link>/2026/11/regional-diplomacy-assessment/</link>
            <pubDate>Tue, 10 Nov 2026 09:00:00 +0000</pubDate>
            <description>Policy analysis of diplomatic frameworks.</description>
        </item>
    </channel></rss>"""

    async def _mock_get(self, url, **kwargs):
        if "feeds.content.dowjones.io" in url:
            return HttpFetchResponse(
                url=url,
                status_code=200,
                text=wsj_rss,
                headers={"content-type": "application/rss+xml"},
            )
        if "wsj.com" in url:
            return HttpFetchResponse(
                url=url,
                status_code=403,
                text="<html><script src='https://geo.captcha-delivery.com/captcha.js'></script></html>",
                headers={"content-type": "text/html"},
            )
        if "israelpolicyforum.org/feed" in url:
            return HttpFetchResponse(
                url="https://israelpolicyforum.org/feed/",
                status_code=200,
                text=ipf_rss,
                headers={"content-type": "application/rss+xml"},
            )
        return HttpFetchResponse(url=url, status_code=404, text="not found", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()

    wsj_batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://www.wsj.com/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert wsj_batch.source_mode == "rss"
    assert len(wsj_batch.posts) == 1
    assert wsj_batch.posts[0].title == "Global Central Banks Weigh Late-Year Rate Moves"

    ipf_batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://www.ipforum.org/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert ipf_batch.source_mode == "rss"
    assert ipf_batch.posts[0].post_url == "https://israelpolicyforum.org/2026/11/regional-diplomacy-assessment"


async def test_dod_mil_ssl_cert_auto_recovery_and_afcent_scraping(monkeypatch):
    import httpx
    from app.collectors.http_fetcher import _is_ssl_cert_error

    ssl_exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")
    assert _is_ssl_cert_error(ssl_exc) is True

    verify_calls = []

    async def _fake_httpx_fetch(
        self, url, *, proxy_url, accept_kind, referer, timeout_sec, verify_override=None
    ):
        effective_verify = self.verify_ssl if verify_override is None else verify_override
        verify_calls.append(effective_verify)
        if effective_verify:
            raise ssl_exc
        return HttpFetchResponse(
            url="https://www.afcent.af.mil/News",
            status_code=200,
            text=AFCENT_MIL_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            via="httpx",
        )

    monkeypatch.setattr(SmartHttpFetcher, "_fetch_httpx", _fake_httpx_fetch)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://www.afcent.af.mil/News"},
        settings={**DEFAULT_SETTINGS, "verify_ssl": "1", "timezone": "UTC"},
    )
    assert verify_calls[0] is True
    assert False in verify_calls
    assert batch.source_mode == "html"
    assert len(batch.posts) == 2
    assert batch.posts[0].title == "U.S. Air Forces Central conducts coalition readiness exercise"
    assert batch.posts[0].published_at == datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
    assert batch.posts[1].published_at == datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)


async def test_turnstile_oopif_auto_attach_and_persistent_profile():
    import asyncio
    import json
    from app.collectors.browser_fetcher import _RawCdpClient, _get_persistent_profile_dir

    sent_frames = []

    class _DummyWs:
        def __init__(self):
            self._events = [
                json.dumps(
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "oopif-turnstile-1",
                            "targetInfo": {
                                "type": "iframe",
                                "url": "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile",
                            },
                            "waitingForDebugger": True,
                        },
                    }
                )
            ]

        async def send(self, payload: str):
            msg = json.loads(payload)
            sent_frames.append(msg)
            # Auto-reply to any CDP command so _configure_child_target completes immediately
            self._events.append(
                json.dumps(
                    {
                        "id": msg["id"],
                        "sessionId": msg.get("sessionId"),
                        "result": {},
                    }
                )
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            for _ in range(20):
                if self._events:
                    return self._events.pop(0)
                await asyncio.sleep(0.01)
            raise StopAsyncIteration

        async def close(self):
            return None

    ua_override = _build_ua_override_params("Mozilla/5.0 HeadlessChrome/131.0.6778.86 Safari/537.36")
    client = _RawCdpClient(_DummyWs(), ua_override=ua_override)
    await asyncio.sleep(0.08)
    await client.close()

    oopif_methods = [f["method"] for f in sent_frames if f.get("sessionId") == "oopif-turnstile-1"]
    assert "Emulation.setUserAgentOverride" in oopif_methods
    assert "Page.addScriptToEvaluateOnNewDocument" in oopif_methods
    assert "Runtime.runIfWaitingForDebugger" in oopif_methods
    # Never leak Runtime.enable into cross-origin Turnstile iframe!
    assert "Runtime.enable" not in oopif_methods

    # Persistent profile directory stays stable across `/` and `/feed/`
    p1 = _get_persistent_profile_dir(headless=False, proxy_url=None)
    p2 = _get_persistent_profile_dir(headless=False, proxy_url=None)
    assert p1 == p2

    # DataDome & Akamai challenge pages are recognized
    assert _looks_like_js_challenge(
        "<html><head><title>wsj.com</title></head><body><script src='https://ct.captcha-delivery.com/c.js'></script></body></html>"
    ) is True
    assert _looks_like_js_challenge(
        "<html><head><title>Access Denied</title></head><body>Reference #18.2d34.edgesuite.net</body></html>"
    ) is True


MEI_CUSTOM_POST_TYPES_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <title>Home - Middle East Institute</title>
    <link rel="alternate" type="application/rss+xml" title="MEI Feed" href="https://mei.edu/feed/" />
    <link rel="https://api.w.org/" href="https://mei.edu/wp-json/" />
</head>
<body>
    <main>
        <div class="card">
            <h3><a href="https://mei.edu/report/a-contested-mediterranean-what-role-for-the-us/">A Contested Mediterranean: What Role for the US?</a></h3>
            <p>This report explores maritime security conditions in the Mediterranean and US engagement.</p>
        </div>
        <div class="card">
            <h3><a href="https://mei.edu/publication/mohsen-rezaei-the-unlikely-peacemaker/">Mohsen Rezaei: The Unlikely Peacemaker</a></h3>
        </div>
        <div class="card">
            <h3><a href="https://mei.edu/podcast/ambassador-talal-al-rahbi-on-omans-diplomacy/">Ambassador Talal Al Rahbi on Oman's Diplomacy</a></h3>
        </div>
        <a href="https://mei.edu/research-commentary/">See More Research &amp; Commentary</a>
        <a href="https://mei.edu/person/karen-e-young/">Karen E. Young</a>
        <a href="https://mei.edu/regions/gulf/united-arab-emirates/">United Arab Emirates (UAE)</a>
    </main>
</body>
</html>
"""

IPIS_PORTAL_HTML = """<!DOCTYPE html>
<html lang="fa">
<head><title>مرکز مطالعات سیاسی و بین المللی</title></head>
<body>
    <div class="slider-item">
        <a href="https://ipis.ir/portal/subjectview/796883">
            <img src="https://ipis.ir/files/mfaipis/newspics/2026103115010867278451362.jpg" alt="" />
        </a>
        <a href="https://ipis.ir/portal/subjectview/796883">
            دیدار هیات کمیته بین‌المللی صلیب سرخ با معاون پژوهشی مرکز مطالعات سیاسی و بین‌المللی
        </a>
    </div>
    <div class="slider-item">
        <a href="https://ipis.ir/portal/subjectview/797397">
            <img src="https://ipis.ir/files/mfaipis/newspics/2026110916111798475416238.jpg" alt="" />
        </a>
        <a href="https://ipis.ir/portal/subjectview/797397">
            غرب آسیای جدید در برابر خاورمیانه جدید؛ توافق مکه و آزمون گذار به معماری امنیتی فراگیر
        </a>
    </div>
    <a href="https://ipis.ir/portal/NewsAgencyShow/3001">آرشیو</a>
    <div class="eurasia-item">
        <a href="https://ipis.ir/portal/subjectview/774932">
            <img src="https://ipis.ir/files/mfaipis/newspics/cr_2025091412255674646271385.jpg" alt="" />
        </a>
        <a href="https://ipis.ir/portal/subjectview/774932">
            گذار نظم کهن به نظم جدید از گذرگاه جنگ اوکراین
        </a>
    </div>
</body>
</html>
"""


async def test_mei_wordpress_hello_world_placeholder_skipped_for_custom_post_types(monkeypatch):
    hello_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Middle East Institute</title>
        <item>
            <title>Hello world!</title>
            <link>https://mei.edu/hello-world/</link>
            <pubDate>Mon, 05 May 2025 18:41:22 +0000</pubDate>
            <description>Welcome to WordPress. This is your first post. Edit or delete it, then start writing!</description>
        </item>
    </channel></rss>"""

    hello_wp_json = (
        '[{"id":1,"date_gmt":"2025-05-05T18:41:22","slug":"hello-world",'
        '"link":"https://mei.edu/hello-world/","title":{"rendered":"Hello world!"},'
        '"content":{"rendered":"<p>Welcome to WordPress. This is your first post.</p>"},'
        '"excerpt":{"rendered":"<p>Welcome to WordPress.</p>"}}]'
    )

    async def _mock_get(self, url, **kwargs):
        if "mei.edu/feed" in url:
            return HttpFetchResponse(
                url="https://mei.edu/feed/",
                status_code=200,
                text=hello_rss,
                headers={"content-type": "application/rss+xml"},
            )
        if "mei.edu/wp-json/wp/v2/posts" in url:
            return HttpFetchResponse(
                url=url,
                status_code=200,
                text=hello_wp_json,
                headers={"content-type": "application/json"},
            )
        if "mei.edu" in url:
            return HttpFetchResponse(
                url="https://mei.edu/",
                status_code=200,
                text=MEI_CUSTOM_POST_TYPES_HTML,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return HttpFetchResponse(url=url, status_code=404, text="not found", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "http://www.mei.edu/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert batch.source_mode == "html"
    urls = [p.post_url for p in batch.posts]
    assert len(batch.posts) == 3
    assert all("hello-world" not in u for u in urls)
    assert all("/person/" not in u and "/regions/" not in u for u in urls)
    assert batch.posts[0].title == "A Contested Mediterranean: What Role for the US?"


async def test_war_gov_afpims_rss_ashx_discovery_and_known_feeds(monkeypatch):
    from app.collectors.feed_parser import discover_feed_urls_in_html
    from app.collectors.web_collector import _resolve_known_public_feeds

    war_feeds = _resolve_known_public_feeds("https://www.war.gov")
    assert "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=1&Site=945" in war_feeds
    assert _resolve_known_public_feeds("https://www.war.gov/News/Speeches") == [
        "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=11&Site=945"
    ]

    afpims_rss_page = (
        '<html><body><a href="/DesktopModules/ArticleCS/RSS.ashx?max=10&amp;ContentType=1&amp;Site=945">'
        "News RSS</a></body></html>"
    )
    discovered = discover_feed_urls_in_html(afpims_rss_page, "https://www.war.gov/News/RSS/")
    assert discovered == ["https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=10&ContentType=1&Site=945"]

    war_rss_xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>Department of War News Feed</title>
        <item>
            <title>USS Nimitz Trains New, Returning Naval Aviators</title>
            <link>https://www.war.gov/News/News-Stories/Article/Article/4646092/uss-nimitz-trains-new-returning-naval-aviators/</link>
            <pubDate>Tue, 10 Nov 2026 19:40:06 GMT</pubDate>
            <description>Carrier qualifications completed in the Atlantic.</description>
        </item>
    </channel></rss>"""

    async def _mock_get(self, url, **kwargs):
        if "RSS.ashx" in url:
            return HttpFetchResponse(
                url=url,
                status_code=200,
                text=war_rss_xml,
                headers={"content-type": "application/rss+xml"},
            )
        return HttpFetchResponse(
            url=url,
            status_code=403,
            text="<html><title>Access Denied</title><body>Reference #18.1a.edgesuite.net</body></html>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://www.war.gov"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert batch.source_mode == "rss"
    assert len(batch.posts) == 1
    assert batch.posts[0].title == "USS Nimitz Trains New, Returning Naval Aviators"


def test_ipis_ir_direct_first_routing_and_newspics_timestamp_sorting(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.getproxies",
        lambda: {"http": "socks5://127.0.0.1:10808", "https": "socks5://127.0.0.1:10808"},
    )
    # Domestic .ir sites try Direct `(None, False)` first so foreign VPN IPs don't hang/block on MFA firewalls
    ir_cands = resolve_proxy_candidates(dict(DEFAULT_SETTINGS), "https://ipis.ir/")
    assert ir_cands == [(None, False), ("socks5://127.0.0.1:10808", False)]

    # International sites still try the VPN proxy first
    intl_cands = resolve_proxy_candidates(dict(DEFAULT_SETTINGS), "https://www.criticalthreats.org/")
    assert intl_cands == [("socks5://127.0.0.1:10808", False), (None, False)]

    title, posts = extract_listing_posts(IPIS_PORTAL_HTML, page_url="https://ipis.ir/", tz_name="UTC")
    assert title == "مرکز مطالعات سیاسی و بین المللی"
    assert len(posts) == 3
    assert all("NewsAgencyShow" not in p.post_url for p in posts)
    # Sorted newest-first using embedded `newspics/YYYYMMDDHHMMSS...jpg` timestamps!
    assert [p.post_url for p in posts] == [
        "https://ipis.ir/portal/subjectview/797397",
        "https://ipis.ir/portal/subjectview/796883",
        "https://ipis.ir/portal/subjectview/774932",
    ]
    assert posts[0].published_at == datetime(2026, 11, 9, 16, 11, 17, tzinfo=timezone.utc)


async def test_bicom_wp_json_fallback_and_turnstile_backend_node_clicker(monkeypatch):
    from app.collectors.browser_fetcher import DomainClearanceCache, _nudge_turnstile_if_present

    # 1. WordPress site (`bicom.org.uk`) where `<head>` advertises `/feed/` (blocked 403),
    #    automatically discovers and rescues via `/wp-json/wp/v2/posts`!
    bicom_html = """<!DOCTYPE html><html><head>
        <title>BICOM</title>
        <link rel="alternate" type="application/rss+xml" href="https://www.bicom.org.uk/feed/" />
        <link rel="https://api.w.org/" href="https://www.bicom.org.uk/wp-json/" />
    </head><body class="home wp-singular"><img src="/wp-content/uploads/hero.jpg"/></body></html>"""

    bicom_wp_json = (
        '[{"id":8020,"date_gmt":"2026-11-11T12:02:19","slug":"eisenkots-bloc-moves-to-take-control",'
        '"link":"https://www.bicom.org.uk/eisenkots-bloc-moves-to-take-control/",'
        '"title":{"rendered":"Eisenkot bloc moves to take control of the Knesset"},'
        '"content":{"rendered":"<p>The 26th Knesset convened in Jerusalem for its opening session.</p>"},'
        '"excerpt":{"rendered":"<p>Opening session briefing.</p>"}}]'
    )

    async def _mock_get(self, url, **kwargs):
        if url.rstrip("/") == "https://www.bicom.org.uk":
            return HttpFetchResponse(
                url="https://www.bicom.org.uk/",
                status_code=200,
                text=bicom_html,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if "/feed" in url:
            return HttpFetchResponse(url=url, status_code=403, text="Forbidden", headers={})
        if "/wp-json/wp/v2/posts" in url:
            return HttpFetchResponse(
                url=url,
                status_code=200,
                text=bicom_wp_json,
                headers={"content-type": "application/json"},
            )
        return HttpFetchResponse(url=url, status_code=404, text="not found", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "http://www.bicom.org.uk/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert batch.source_mode == "wp_api"
    assert len(batch.posts) == 1
    assert "Eisenkot" in batch.posts[0].title

    # 2. Turnstile checkbox clicker uses `backendNodeId` (even when `nodeId == 0` inside closed Shadow DOM!)
    cdp_calls = []

    class _FakeTurnstileCdp:
        async def call(self, method: str, params=None, timeout: float = 2.0):
            cdp_calls.append((method, params or {}))
            if method == "DOM.getFlattenedDocument":
                return {
                    "nodes": [
                        {
                            "nodeId": 0,
                            "backendNodeId": 77,
                            "nodeName": "IFRAME",
                            "attributes": [
                                "src",
                                "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/f",
                                "title",
                                "Widget containing a Cloudflare security challenge",
                            ],
                        }
                    ]
                }
            if method == "DOM.getBoxModel":
                assert params == {"backendNodeId": 77}
                return {"model": {"content": [200, 300, 500, 300, 500, 365, 200, 365]}}
            return {}

    await _nudge_turnstile_if_present(_FakeTurnstileCdp())  # type: ignore[arg-type]
    pressed = [p for m, p in cdp_calls if m == "Input.dispatchMouseEvent" and p.get("type") == "mousePressed"]
    assert len(pressed) == 1
    assert pressed[0]["x"] == 228 and pressed[0]["y"] == 332

    # 3. DomainClearanceCache preserves full HttpOnly / Secure CDP cookies
    cache = DomainClearanceCache()
    cache.put(
        "https://www.criticalthreats.org/",
        None,
        cookie_header="cf_clearance=abc123",
        user_agent="Mozilla/5.0 Chrome/131.0.0.0",
        cdp_cookies=[
            {
                "name": "cf_clearance",
                "value": "abc123",
                "domain": ".criticalthreats.org",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
            }
        ],
    )
    entry = cache.get("https://www.criticalthreats.org/analysis", None)
    assert entry is not None
    assert entry.cdp_cookies[0]["httpOnly"] is True
    assert entry.cdp_cookies[0]["domain"] == ".criticalthreats.org"


async def test_mil_remote_protocol_error_recovery_registrable_domains_and_centcom(monkeypatch):
    import httpx

    assert extract_registrable_domain("https://www.afcent.af.mil/News") == "afcent.af.mil"
    assert extract_registrable_domain("https://www.cusnc.navy.mil/") == "cusnc.navy.mil"
    assert extract_registrable_domain("https://www.centcom.mil/") == "centcom.mil"

    centcom_html = """<!DOCTYPE html>
    <html><head><title>U.S. Central Command</title></head><body>
        <main>
            <div class="release-item">
                <h3><a href="https://www.centcom.mil/MEDIA/PUBLIC-RELEASES/Article/4644120/centcom-forces-defend-regional-shipping-lanes/">
                    CENTCOM Forces Defend Regional Shipping Lanes
                </a></h3>
                <p>TAMPA, Fla. – U.S. Central Command forces conducted maritime security operations on Nov. 7, 2026.</p>
            </div>
            <div class="gallery-popup">
                <a href="https://www.centcom.mil/MEDIA/IMAGERY/igphoto/2004024001/">Photo 2004024001 Slide</a>
            </div>
        </main>
    </body></html>"""

    cusnc_html = """<!DOCTYPE html>
    <html><head><title>U.S. Naval Forces Central Command</title></head><body>
        <main>
            <article>
                <h3><a href="https://www.cusnc.navy.mil/Media/News/Display/Article/4638901/navcent-concludes-joint-maritime-exercise/">
                    NAVCENT Concludes Joint Maritime Exercise in Arabian Gulf
                </a></h3>
                <span>Nov. 1, 2026</span>
            </article>
        </main>
    </body></html>"""

    # Simulate Akamai HTTP/2 `RemoteProtocolError` in `httpx` -> seamless recovery via Layer 2 `curl_cffi`!
    async def _boom_httpx(self, url, **kwargs):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response (PROTOCOL_ERROR)")

    async def _ok_curl(self, url, **kwargs):
        if "cusnc.navy.mil" in url:
            return HttpFetchResponse(
                url="https://www.cusnc.navy.mil/",
                status_code=200,
                text=cusnc_html,
                headers={"content-type": "text/html; charset=utf-8"},
                via="curl_cffi:chrome",
            )
        return HttpFetchResponse(
            url="https://www.centcom.mil/",
            status_code=200,
            text=centcom_html,
            headers={"content-type": "text/html; charset=utf-8"},
            via="curl_cffi:chrome",
        )

    monkeypatch.setattr("app.collectors.http_fetcher.HAS_CURL_CFFI", True)
    monkeypatch.setattr(SmartHttpFetcher, "_fetch_httpx", _boom_httpx)
    monkeypatch.setattr(SmartHttpFetcher, "_fetch_curl_impersonate", _ok_curl)

    collector = WebSiteCollector()
    centcom_batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://www.centcom.mil/"},
        settings={**DEFAULT_SETTINGS, "browser_fallback": "0"},
    )
    assert centcom_batch.source_mode == "html"
    assert len(centcom_batch.posts) == 1
    assert "Shipping Lanes" in centcom_batch.posts[0].title
    # Ensure `igphoto` gallery popups were excluded
    assert all("igphoto" not in p.post_url for p in centcom_batch.posts)

    cusnc_batch = await collector.fetch_site_posts(
        site={"canonical_url": "https://www.cusnc.navy.mil/"},
        settings={**DEFAULT_SETTINGS, "browser_fallback": "0"},
    )
    assert len(cusnc_batch.posts) == 1
    assert "NAVCENT" in cusnc_batch.posts[0].title


async def test_mei_acf_research_cpt_and_overlay_card_fallback(monkeypatch):
    mei_research_json = json.dumps(
        [
            {
                "id": 61663,
                "date": "2026-11-11T09:42:03",
                "date_gmt": "2026-11-11T14:42:03",
                "guid": {"rendered": "https://mei.edu/?post_type=research&p=61663"},
                "link": "https://mei.edu/publication/trumps-midterm-loss-opens-a-window-for-iran-diplomacy/",
                "title": {"rendered": "Trump&#8217;s Midterm Loss Opens a Window for Iran Diplomacy"},
                "acf": {
                    "details": {
                        "abstract": "Eight months after launching the war, Washington faces key diplomatic choices."
                    },
                    "page_layout": [
                        {
                            "acf_fc_layout": "layout_wysiwyg",
                            "component_wysiwyg": {
                                "content": (
                                    "Eight months after launching the war, Washington faces key diplomatic choices.\r\n\r\n"
                                    "Regional mediators in Muscat and Doha are renewing diplomatic outreach.\r\n\r\n"
                                    '[tm_button url="https://en.majalla.com/node/334101" label="Read More in Al Majalla" target="true"]'
                                )
                            },
                        }
                    ],
                },
                "yoast_head": (
                    '<meta property="og:site_name" content="Middle East Institute" />'
                    '<meta property="og:image" content="https://mei.edu/wp-content/uploads/2026/11/cover.jpg" />'
                ),
            }
        ]
    )

    async def _mock_get(self, url, **kwargs):
        if "mei.edu/wp-json/wp/v2/research" in url:
            return HttpFetchResponse(
                url=url,
                status_code=200,
                text=mei_research_json,
                headers={"content-type": "application/json"},
            )
        return HttpFetchResponse(url=url, status_code=403, text="Just a moment...", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "http://www.mei.edu/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert batch.source_mode == "wp_api"
    assert batch.site_title == "Middle East Institute"
    assert len(batch.posts) == 1
    assert "Iran Diplomacy" in batch.posts[0].title
    assert "Regional mediators in Muscat" in batch.posts[0].content
    assert "[tm_button" not in batch.posts[0].content
    assert batch.posts[0].media_url == "https://mei.edu/wp-content/uploads/2026/11/cover.jpg"


async def test_washington_institute_paginated_sitemap_selects_highest_page_and_english_urls(monkeypatch):
    wi_sitemap_index = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>http://www.washingtoninstitute.org/sitemap.xml?page=1</loc><lastmod>2026-11-11T00:36:53-05:00</lastmod></sitemap>
        <sitemap><loc>http://www.washingtoninstitute.org/sitemap.xml?page=2</loc><lastmod>2026-11-11T00:36:53-05:00</lastmod></sitemap>
        <sitemap><loc>http://www.washingtoninstitute.org/sitemap.xml?page=12</loc><lastmod>2026-11-11T00:36:53-05:00</lastmod></sitemap>
    </sitemapindex>"""

    children = extract_child_sitemaps(wi_sitemap_index, max_items=2, prefer_https=True)
    assert children[0] == "https://www.washingtoninstitute.org/sitemap.xml?page=12"

    page12_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>http://www.washingtoninstitute.org/policy-analysis/item-one-maritime-security</loc><lastmod>2026-11-09T12:00:00-05:00</lastmod></url>
        <url><loc>http://www.washingtoninstitute.org/ar/policy-analysis/alitem-one-arabic-slug</loc><lastmod>2026-11-09T12:00:00-05:00</lastmod></url>
        <url><loc>http://www.washingtoninstitute.org/policy-analysis/item-two-energy-corridors</loc><lastmod>2026-11-08T12:00:00-05:00</lastmod></url>
        <url><loc>http://www.washingtoninstitute.org/policy-analysis/item-three-regional-deterrence</loc><lastmod>2026-11-07T12:00:00-05:00</lastmod></url>
        <url><loc>http://www.washingtoninstitute.org/policy-analysis/item-four-diplomatic-options</loc><lastmod>2026-11-06T12:00:00-05:00</lastmod></url>
        <url><loc>http://www.washingtoninstitute.org/policy-analysis/item-five-red-sea-shipping</loc><lastmod>2026-11-05T12:00:00-05:00</lastmod></url>
    </urlset>"""

    async def _mock_get(self, url, **kwargs):
        if url.endswith("sitemap.xml"):
            return HttpFetchResponse(url=url, status_code=200, text=wi_sitemap_index, headers={"content-type": "application/xml"})
        if "sitemap.xml?page=12" in url:
            return HttpFetchResponse(url=url, status_code=200, text=page12_xml, headers={"content-type": "application/xml"})
        return HttpFetchResponse(url=url, status_code=403, text="<html><title>Just a moment...</title></html>", headers={})

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()
    batch = await collector.fetch_site_posts(
        site={"canonical_url": "http://www.washingtoninstitute.org/"},
        settings=dict(DEFAULT_SETTINGS),
    )
    assert batch.source_mode == "sitemap"
    assert len(batch.posts) == 5
    assert all("/ar/" not in p.post_url for p in batch.posts)


async def test_israel_policy_forum_slashless_feed_rescue_and_vc_shortcode_stripping(monkeypatch):
    ipf_wp_json = json.dumps(
        [
            {
                "id": 112064,
                "date": "2026-11-05T12:50:44",
                "date_gmt": "2026-11-05T17:50:44",
                "guid": {"rendered": "https://israelpolicyforum.org/?p=112064"},
                "link": "https://israelpolicyforum.org/2026/11/05/three-unknowns-to-watch-in-the-blue-tsunamis-wake/",
                "title": {"rendered": "Three Unknowns to Watch in the Blue Tsunami&#8217;s Wake"},
                "content": {
                    "rendered": (
                        '[vc_row row_height_percent="0"][vc_column][vc_raw_html]JTNDaWZyYW1lJTNF[/vc_raw_html]'
                        '[vc_column_text]<p>Democratic control of both the House and Senate is going to bring real changes.</p>'
                        "[/vc_column_text][/vc_row]"
                    )
                },
            }
        ]
    )

    async def _mock_get(self, url, **kwargs):
        if "wp-json/wp/v2/posts" in url:
            return HttpFetchResponse(
                url=url, status_code=200, text=ipf_wp_json, headers={"content-type": "application/json"}
            )
        return HttpFetchResponse(
            url=url, status_code=403, text="<html><title>Just a moment...</title></html>", headers={}
        )

    monkeypatch.setattr(SmartHttpFetcher, "get", _mock_get)
    collector = WebSiteCollector()

    # Both `https://israelpolicyforum.org/feed` (with `feed_url` set!) and root `/` succeed cleanly
    feed_batch = await collector.fetch_site_posts(
        site={
            "canonical_url": "https://israelpolicyforum.org/feed",
            "feed_url": "https://israelpolicyforum.org/feed",
        },
        settings=dict(DEFAULT_SETTINGS),
    )
    assert feed_batch.source_mode == "wp_api"
    assert len(feed_batch.posts) == 1
    assert "[vc_row" not in feed_batch.posts[0].content
    assert "JTNDaWZyYW1l" not in feed_batch.posts[0].content
    assert "Democratic control of both the House and Senate" in feed_batch.posts[0].content


async def test_lawfare_chathamhouse_feeds_and_authenticated_http_proxy_cdp(monkeypatch):
    from app.collectors.browser_fetcher import _split_chromium_proxy_and_auth
    from app.collectors.web_collector import _resolve_known_public_feeds

    assert _resolve_known_public_feeds("https://www.lawfaremedia.org/") == [
        "https://www.lawfaremedia.org/feeds/articles"
    ]
    assert _resolve_known_public_feeds("https://www.chathamhouse.org/") == [
        "https://www.chathamhouse.org/path/whatsnew.xml"
    ]

    # Authenticated HTTP proxy splits cleanly for Chrome CLI + CDP Fetch.authRequired
    srv, auth, ok = _split_chromium_proxy_and_auth("http://alice:s3cr3t@127.0.0.1:8080")
    assert ok is True
    assert srv == "http://127.0.0.1:8080"
    assert auth == ("alice", "s3cr3t")




