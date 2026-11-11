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

