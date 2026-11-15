"""Verified per-site endpoint profiles for hard-to-collect websites.

The generic collector (RSS discovery -> WP REST API -> HTML scraping) is enough
for most sites, but a handful of publishers either:

* protect their *homepage* with Cloudflare/Turnstile while their REST API and
  feeds stay reachable (``bicom.org.uk``, ``israelpolicyforum.org``),
* publish an **empty** RSS document (``lawfaremedia.org/feeds/articles``),
* redirect ``/rss.xml`` back to a JS carousel homepage with truncated headlines
  (``memri.org``),
* serve DoD PKI certificates and Akamai HTTP/2 resets, while their real articles
  live on a plain ``/News/`` or ``/Media/News/`` listing (``*.mil`` sites).

The entries below were checked against the live sites.  Each profile lists
official feeds/APIs that are probed *before* the homepage, plus clean HTML
listing pages that can be used instead of (or as a rescue for) a noisy
homepage.  Keeping this in one small registry stops the collector from
accumulating ad-hoc ``if domain == ...`` branches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.utils.site_parser import extract_registrable_domain

# Compact WordPress REST response: enough for titles, dates, links and full
# bodies, but without the multi-megabyte `_embedded` blobs of `?_embed=1`.
WP_COMPACT_FIELDS = "id,date,date_gmt,link,guid,title,excerpt,content"

# AFPiMS/DNN portals expose an aggregated ArticleCS feed.  It contains articles
# from *every* command on the platform, so posts are filtered back to the
# monitored host (see WebSiteCollector._probe_known_public_feeds).
AFPiMS_FEED_QUERY = "ContentType=1&max=100"

ARTICLE_PATH_RE = re.compile(
    r"(?:/(?:article|articles|reports|report|press-releases|display)/|"
    r"/\d{4}/\d{1,2}/|/\d{6,}/)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SiteProfile:
    domain: str
    feeds: Tuple[str, ...] = ()
    listing_pages: Tuple[str, ...] = ()
    prefer_listing_pages: bool = False
    filter_feed_posts_to_domain: bool = False
    # Only apply the profile to index/section URLs, never to a single article.
    allowed_first_segments: Tuple[str, ...] = ()
    proxy_url: Optional[str] = None  # <--- جدید: پروکسی اختصاصی سایت (مثلاً http://proxy1:8080)
    notes: str = ""


SITE_PROFILES: Dict[str, SiteProfile] = {
    "afcent.af.mil": SiteProfile(
        domain="afcent.af.mil",
        feeds=(
            f"https://www.afcent.af.mil/DesktopModules/ArticleCS/RSS.ashx?{AFPiMS_FEED_QUERY}",
        ),
        listing_pages=(
            "https://www.afcent.af.mil/News/Article-List/",
            "https://www.afcent.af.mil/News/",
        ),
        filter_feed_posts_to_domain=True,
        allowed_first_segments=("news",),
        proxy_url=None,  # <--- گروه ۱ (مثلاً http://proxy1:8080)
        notes="DoD PKI TLS + Akamai; /News is a carousel, RSS.ashx is aggregated.",
    ),
    "cusnc.navy.mil": SiteProfile(
        domain="cusnc.navy.mil",
        feeds=(
            f"https://www.cusnc.navy.mil/DesktopModules/ArticleCS/RSS.ashx?{AFPiMS_FEED_QUERY}",
        ),
        listing_pages=(
            "https://www.cusnc.navy.mil/Media/News/",
            "https://www.cusnc.navy.mil/",
        ),
        filter_feed_posts_to_domain=True,
        allowed_first_segments=("media",),
        proxy_url=None,  # <--- گروه ۱ (مثلاً http://proxy1:8080)
        notes="Homepage is a slider; /Media/News/ is a plain paginated listing.",
    ),
    "lawfaremedia.org": SiteProfile(
        domain="lawfaremedia.org",
        # /feeds/articles, /feeds/*, /feed/ and /rss.xml are 404 or return an
        # RSS <channel> with ZERO <item> nodes.  Never trust them as the source.
        feeds=(),
        listing_pages=(
            "https://www.lawfaremedia.org/article/highlights",
            "https://www.lawfaremedia.org/",
        ),
        prefer_listing_pages=True,
        allowed_first_segments=("article", "feeds", "feed"),
        proxy_url=None,  # <--- گروه ۲ (مثلاً http://proxy2:9090)
        notes="Official RSS is empty; use the clean Highlights listing instead.",
    ),
    "memri.org": SiteProfile(
        domain="memri.org",
        # /rss.xml 302-redirects to the homepage; /sitemap.xml is a 404 page.
        feeds=(),
        listing_pages=(
            "https://www.memri.org/archives",
            "https://www.memri.org/",
        ),
        prefer_listing_pages=True,
        allowed_first_segments=("archives", "reports"),
        proxy_url=None,  # <--- گروه ۲ (مثلاً http://proxy2:9090)
        notes="Homepage ticker repeats items and truncates headlines; /archives has full ones.",
    ),
    "bicom.org.uk": SiteProfile(
        domain="bicom.org.uk",
        feeds=(
            f"https://www.bicom.org.uk/wp-json/wp/v2/posts?per_page=20&_fields={WP_COMPACT_FIELDS}",
            "https://www.bicom.org.uk/wp-json/wp/v2/posts?per_page=20",
            "https://www.bicom.org.uk/feed/",
        ),
        listing_pages=("https://www.bicom.org.uk/",),
        allowed_first_segments=("news", "feed", "wp-json"),
        proxy_url=None,  # <--- بدون پروکسی
        notes="Cloudflare Turnstile on `/`; REST API and /feed/ stay public.",
    ),
    "israelpolicyforum.org": SiteProfile(
        domain="israelpolicyforum.org",
        feeds=(
            f"https://israelpolicyforum.org/wp-json/wp/v2/posts?per_page=20&_fields={WP_COMPACT_FIELDS}",
            "https://israelpolicyforum.org/wp-json/wp/v2/posts?per_page=20",
            "https://israelpolicyforum.org/feed/",
        ),
        listing_pages=("https://israelpolicyforum.org/",),
        allowed_first_segments=("feed", "wp-json", "category", "op-eds"),
        proxy_url=None,  # <--- بدون پروکسی
        notes="WP REST API is authoritative; `_embed=1` responses are very large.",
    ),
}


def get_site_profile(url: str) -> Optional[SiteProfile]:
    """Return the profile for an index/section URL (never for a single article)."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    profile = SITE_PROFILES.get(extract_registrable_domain(host))
    if profile is None:
        return None

    path = (parsed.path or "/").strip("/")
    if ARTICLE_PATH_RE.search("/" + path + "/") and not path.lower().startswith("article/highlights"):
        return None
    segments = [seg.lower() for seg in path.split("/") if seg]
    if segments and segments[0] not in profile.allowed_first_segments:
        return None
    return profile


def known_feeds_for(url: str) -> List[str]:
    profile = get_site_profile(url)
    return list(profile.feeds) if profile else []


def known_listing_pages_for(url: str) -> List[str]:
    profile = get_site_profile(url)
    return list(profile.listing_pages) if profile else []
