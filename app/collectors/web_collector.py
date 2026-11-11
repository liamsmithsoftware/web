from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.collectors.base import (
    CollectorError,
    FetchBatchResult,
    FetchedPost,
)
from app.collectors.browser_fetcher import CLEARANCE_CACHE
from app.collectors.feed_parser import (
    discover_feed_urls_in_html,
    is_feed_document,
    is_placeholder_post,
    parse_feed_content,
)
from app.collectors.html_scraper import extract_article_details, extract_listing_posts
from app.collectors.http_fetcher import HttpFetchResponse, SmartHttpFetcher
from app.collectors.sitemap_parser import (
    extract_child_sitemaps,
    is_sitemap_xml,
    parse_sitemap_posts,
)
from app.collectors.wp_api_parser import (
    discover_wp_posts_api_url,
    is_wordpress_html,
    is_wp_posts_json,
    parse_wp_posts_json,
)
from app.config import DEFAULT_TIMEZONE
from app.utils.site_parser import (
    extract_registrable_domain,
    looks_like_feed_url,
    parse_site_input,
)

COMMON_ROOT_FEED_PATHS = (
    "/feed/",
    "/feed",
    "/rss.xml",
    "/rss",
    "/feed.xml",
    "/atom.xml",
)

COMMON_SITEMAP_PATHS = (
    "/news-sitemap.xml",
    "/post-sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap.xml",
)

SSO_AUTH_HOST_PREFIXES = (
    "accounts.",
    "account.",
    "login.",
    "auth.",
    "sso.",
    "id.",
)

SSO_IDP_DOMAINS = (
    "okta.com",
    "oktapreview.com",
    "auth0.com",
    "microsoftonline.com",
    "onelogin.com",
    "duosecurity.com",
)

SSO_AUTH_PATH_RE = re.compile(
    r"(?:^|/)(?:oauth2?|openid-connect|saml2?|sso|login|signin|sign-in|wp-login\.php)(?:/|$)",
    re.IGNORECASE,
)

WSJ_SECTION_FEEDS: Dict[str, str] = {
    "world": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "us-news": "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
    "politics": "https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed",
    "economy": "https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
    "business": "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "finance": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "tech": "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
    "opinion": "https://feeds.content.dowjones.io/public/rss/RSSOpinion",
}

WAR_GOV_SECTION_FEEDS: Dict[str, str] = {
    "news-stories": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=1&Site=945",
    "releases": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=9&Site=945",
    "advisories": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=2&Site=945",
    "speeches": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=11&Site=945",
    "transcripts": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=13&Site=945",
    "features": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=800&Site=945",
    "feature-stories": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=800&Site=945",
    "contracts": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=400&Site=945",
}

SPARSE_HOMEPAGE_HUB_PRIORITY = (
    "/articles",
    "/news",
    "/latest",
    "/analysis",
    "/reports",
    "/publications",
)


def _resolve_known_public_feeds(url: str) -> List[str]:
    """
    Returns known official external RSS feeds for sites that host feeds on a separate CDN
    behind DataDome/paywalls (`wsj.com`), AFPIMS `RSS.ashx` portals (`war.gov` / `defense.gov`),
    or vanity domains (`ipforum.org` -> `israelpolicyforum.org/feed/`).
    """
    parsed = urlparse(url or "")
    reg_domain = extract_registrable_domain(parsed.hostname or "")
    path_parts = [seg.lower() for seg in (parsed.path or "").split("/") if seg]

    if reg_domain == "wsj.com":
        if path_parts:
            first_seg = path_parts[0]
            if first_seg in WSJ_SECTION_FEEDS:
                return [WSJ_SECTION_FEEDS[first_seg]]
        return [
            "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
            "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
        ]

    if reg_domain in ("war.gov", "defense.gov"):
        host = "www.war.gov" if reg_domain == "war.gov" else "www.defense.gov"
        for seg in reversed(path_parts):
            if seg in WAR_GOV_SECTION_FEEDS:
                return [WAR_GOV_SECTION_FEEDS[seg].replace("www.war.gov", host)]
        if not path_parts or path_parts[0] == "news":
            return [
                f"https://{host}/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=1&Site=945",
                f"https://{host}/DesktopModules/ArticleCS/RSS.ashx?max=20&ContentType=9&Site=945",
            ]

    if reg_domain == "ipforum.org" and not path_parts:
        return ["https://israelpolicyforum.org/feed/"]

    return []


def _discover_section_hub_url(html: str, page_url: str) -> Optional[str]:
    """
    When a root homepage only displays a sparse hero/book promo (e.g. `en.idi.org.il/` with 2 cards)
    while linking to a full `/articles/` or `/news/` listing on the same host, returns that hub URL.
    """
    if not html or not page_url:
        return None
    base_parsed = urlparse(page_url)
    base_host = (base_parsed.hostname or "").lower().rstrip(".")
    if not base_host:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    found_by_slug: Dict[str, str] = {}
    for a_tag in soup.find_all("a", href=True):
        raw_href = str(a_tag.get("href") or "").strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        resolved = urljoin(page_url, raw_href)
        p = urlparse(resolved)
        if p.scheme not in ("http", "https"):
            continue
        if (p.hostname or "").lower().rstrip(".") != base_host:
            continue
        norm_path = (p.path or "").rstrip("/").lower()
        if norm_path in SPARSE_HOMEPAGE_HUB_PRIORITY and norm_path not in found_by_slug:
            found_by_slug[norm_path] = urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

    for slug in SPARSE_HOMEPAGE_HUB_PRIORITY:
        if slug in found_by_slug:
            return found_by_slug[slug]
    return None


def _is_root_like_url(url: str) -> bool:
    path = (urlparse(url).path or "/").rstrip("/").lower()
    return path in ("", "/", "/fa", "/en", "/index.html", "/index.php")


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return url


def _https_upgrade_candidate(url: str) -> Optional[str]:
    """If `url` uses plain `http://` on a public domain (not localhost/IP), return its `https://` twin."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "http":
        return None
    host = (parsed.hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "::1") or host.startswith("127."):
        return None
    if parsed.port and parsed.port != 80:
        return None
    netloc = parsed.hostname or ""
    return urlunparse(("https", netloc, parsed.path or "/", parsed.params, parsed.query, ""))


def _is_sso_login_wall(resp: HttpFetchResponse, requested_url: str) -> bool:
    """Detects when a URL redirected to an OAuth/Okta/SSO enterprise login gate."""
    final_u = (resp.url or "").strip()
    req_u = (requested_url or "").strip()
    if not final_u or not req_u:
        return False

    p_final = urlparse(final_u)
    p_req = urlparse(req_u)
    final_host = (p_final.hostname or "").lower().rstrip(".")
    req_host = (p_req.hostname or "").lower().rstrip(".")
    final_path = (p_final.path or "/").rstrip("/").lower() or "/"
    req_path = (p_req.path or "/").rstrip("/").lower() or "/"

    # Only flag when the server actually redirected away from the requested location
    if final_host == req_host and final_path == req_path:
        return False

    if final_host != req_host and (
        final_host.startswith(SSO_AUTH_HOST_PREFIXES)
        or any(final_host == d or final_host.endswith("." + d) for d in SSO_IDP_DOMAINS)
    ):
        return True

    query_low = (p_final.query or "").lower()
    if "client_id=" in query_low and ("redirect_uri=" in query_low or "response_type=" in query_low):
        return True

    if SSO_AUTH_PATH_RE.search(final_path):
        return True

    return False


def _public_portal_candidates_for_sso(requested_url: str) -> List[str]:
    """
    When a private/enterprise subdomain (e.g. `intel.morningconsult.com`) redirects to SSO,
    returns likely public sibling/root portals (`pro.<domain>`, `<domain>`).
    """
    parsed = urlparse(requested_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    reg_domain = extract_registrable_domain(host)
    if not host or not reg_domain:
        return []

    bare_host = host[4:] if host.startswith("www.") else host
    # Only try sibling/root portals when the user entered a specific subdomain like intel.<domain>
    if bare_host == reg_domain:
        return []

    candidates: List[str] = []
    for cand_host in (f"pro.{reg_domain}", reg_domain):
        if cand_host != bare_host:
            candidates.append(f"https://{cand_host}/")
    return candidates


def _raise_sso_login_error() -> None:
    raise CollectorError(
        "این آدرس به صفحه ورود یا اشتراک سازمانی (SSO/Login) هدایت شد و بدون حساب کاربری قابل دسترس نیست. "
        "لطفاً آدرس بخش عمومی وب‌سایت را وارد کنید."
    )


class WebSiteCollector:
    """
    Hybrid Anti-403 Website Collector supporting:
      - 'auto': Smart RSS/Atom + WordPress REST API + HTML scraping + Sitemap + Root/Public Portal rescue
      - 'rss':  Strict RSS / Atom / JSON Feed reader
      - 'html': Direct HTML listing & article scraper
    Powered by SmartHttpFetcher (HTTP/2 + curl_cffi TLS/JA3 + Local Edge/Chrome CDP + Proxy/Direct failover).
    """

    MAX_CONCURRENT_ARTICLE_FETCHES = 4

    async def fetch_site_posts(
        self,
        *,
        site: Dict[str, Any],
        settings: Dict[str, str],
    ) -> FetchBatchResult:
        mode = (settings.get("collector_mode") or "auto").strip().lower()
        tz_name = settings.get("timezone") or DEFAULT_TIMEZONE
        canonical_url = str(site.get("canonical_url") or "").strip()
        known_feed_url = str(site.get("feed_url") or "").strip() or None

        if not canonical_url:
            raise CollectorError("آدرس وب‌سایت نامعتبر یا خالی است.")

        fetcher = SmartHttpFetcher(settings)

        if mode == "html":
            return await self._fetch_via_html(fetcher, canonical_url, tz_name)

        if mode == "rss":
            return await self._fetch_via_rss_strict(
                fetcher, canonical_url, known_feed_url, tz_name
            )

        return await self._fetch_via_auto(
            fetcher, canonical_url, known_feed_url, tz_name
        )

    async def _get_with_https_upgrade(
        self,
        fetcher: SmartHttpFetcher,
        url: str,
        *,
        accept_kind: str = "document",
        referer: Optional[str] = None,
    ) -> HttpFetchResponse:
        """
        Prefers `https://` first for plain `http://` public URLs (avoiding plain-HTTP DPI
        and Cloudflare HTTP bot penalties), falling back to `http://` only if HTTPS fails to connect.
        """
        https_twin = _https_upgrade_candidate(url)
        if https_twin:
            try:
                return await fetcher.get(https_twin, accept_kind=accept_kind, referer=referer)
            except CollectorError:
                return await fetcher.get(url, accept_kind=accept_kind, referer=referer)
        return await fetcher.get(url, accept_kind=accept_kind, referer=referer)

    async def _try_parse_feed_or_wp_response(
        self,
        resp: HttpFetchResponse,
        *,
        base_url: str,
        tz_name: str,
    ) -> Optional[FetchBatchResult]:
        if resp.status_code != 200 or not resp.text:
            return None

        effective_base = resp.url or base_url

        if is_feed_document(resp.text, resp.content_type):
            title, posts = parse_feed_content(resp.text, base_url=effective_base, tz_name=tz_name)
            posts = [p for p in posts if not is_placeholder_post(p)]
            if posts:
                return FetchBatchResult(
                    site_title=title,
                    posts=posts,
                    source_mode="rss",
                    discovered_feed_url=resp.url,
                )

        if is_wp_posts_json(resp.text, resp.content_type):
            title, posts = parse_wp_posts_json(resp.text, base_url=effective_base, tz_name=tz_name)
            posts = [p for p in posts if not is_placeholder_post(p)]
            if posts:
                return FetchBatchResult(
                    site_title=title,
                    posts=posts,
                    source_mode="wp_api",
                    discovered_feed_url=resp.url,
                )

        return None

    async def _try_sparse_homepage_hub_upgrade(
        self,
        fetcher: SmartHttpFetcher,
        html: str,
        page_url: str,
        tz_name: str,
        *,
        existing_posts: List[FetchedPost],
        existing_title: str = "",
    ) -> Optional[FetchBatchResult]:
        if not _is_root_like_url(page_url) or len(existing_posts) >= 4:
            return None

        hub_url = _discover_section_hub_url(html, page_url)
        if not hub_url:
            return None

        try:
            hub_resp = await self._get_with_https_upgrade(fetcher, hub_url, referer=page_url)
            if hub_resp.status_code != 200 or _is_sso_login_wall(hub_resp, hub_url):
                return None
            hub_title, hub_posts = extract_listing_posts(
                hub_resp.text, page_url=hub_resp.url or hub_url, tz_name=tz_name
            )
            if len(hub_posts) > len(existing_posts):
                return FetchBatchResult(
                    site_title=existing_title or hub_title,
                    posts=hub_posts,
                    source_mode="html",
                    discovered_feed_url=None,
                )
        except CollectorError:
            return None
        return None

    async def _probe_candidate_endpoints(
        self,
        fetcher: SmartHttpFetcher,
        candidates: List[str],
        *,
        base_url: str,
        tz_name: str,
    ) -> Optional[FetchBatchResult]:
        origin = _origin_of(base_url)
        seen = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            accept_kind = "json" if "/wp-json/" in candidate else "document"
            try:
                resp = await fetcher.get(candidate, accept_kind=accept_kind, referer=origin)
                if _is_sso_login_wall(resp, candidate):
                    continue
                parsed = await self._try_parse_feed_or_wp_response(
                    resp, base_url=base_url, tz_name=tz_name
                )
                if parsed is not None:
                    return parsed
            except CollectorError:
                continue
        return None

    async def _probe_sitemap_rescue(
        self,
        settings: Dict[str, str],
        base_url: str,
        tz_name: str,
    ) -> Optional[FetchBatchResult]:
        """
        Probes News/Post XML sitemaps (often whitelisted on Cloudflare for SEO bots even
        when `/` and `/feed/` challenge visitors) without triggering browser launches.
        """
        fast_fetcher = SmartHttpFetcher(settings, allow_browser_fallback=False)
        preferred_base = _https_upgrade_candidate(base_url) or base_url
        origin = _origin_of(preferred_base)

        for suffix in COMMON_SITEMAP_PATHS:
            sm_url = urljoin(origin, suffix.lstrip("/"))
            try:
                resp = await fast_fetcher.get(sm_url, referer=origin)
            except CollectorError:
                continue
            if resp.status_code != 200 or not is_sitemap_xml(resp.text, resp.content_type):
                continue

            # Direct <urlset>
            title, posts = parse_sitemap_posts(resp.text, base_url=preferred_base, tz_name=tz_name)
            if posts:
                return FetchBatchResult(
                    site_title=title,
                    posts=posts,
                    source_mode="sitemap",
                    discovered_feed_url=None,
                )

            # <sitemapindex> -> inspect prioritized news/post child sitemaps
            for child_url in extract_child_sitemaps(resp.text, max_items=3):
                try:
                    c_resp = await fast_fetcher.get(child_url, referer=origin)
                except CollectorError:
                    continue
                if c_resp.status_code == 200 and is_sitemap_xml(c_resp.text, c_resp.content_type):
                    c_title, c_posts = parse_sitemap_posts(
                        c_resp.text, base_url=preferred_base, tz_name=tz_name
                    )
                    if c_posts:
                        return FetchBatchResult(
                            site_title=c_title,
                            posts=c_posts,
                            source_mode="sitemap",
                            discovered_feed_url=None,
                        )
        return None

    def _build_blind_fallback_endpoints(self, url: str, *, is_root: bool) -> List[str]:
        """Endpoints probed when homepage returns 403/503 or hides feed tags."""
        preferred = _https_upgrade_candidate(url) or url
        endpoints: List[str] = []
        base_slash = preferred.rstrip("/") + "/"
        if is_root:
            endpoints.append(urljoin(base_slash, "feed/"))
            origin = _origin_of(preferred)
            endpoints.append(urljoin(origin, "wp-json/wp/v2/posts?per_page=20&_embed=1"))
            for suffix in COMMON_ROOT_FEED_PATHS:
                ep = urljoin(base_slash, suffix.lstrip("/"))
                if ep not in endpoints:
                    endpoints.append(ep)
        else:
            endpoints.append(urljoin(base_slash, "feed/"))
            endpoints.append(urljoin(base_slash, "rss"))
        return endpoints

    async def _rescue_sso_public_portal(
        self,
        fetcher: SmartHttpFetcher,
        requested_url: str,
        tz_name: str,
        *,
        html_only: bool = False,
    ) -> Optional[FetchBatchResult]:
        for portal_url in _public_portal_candidates_for_sso(requested_url):
            try:
                if html_only:
                    p_resp = await self._get_with_https_upgrade(fetcher, portal_url)
                    if p_resp.status_code == 200 and not _is_sso_login_wall(p_resp, portal_url):
                        title, posts = extract_listing_posts(
                            p_resp.text, page_url=p_resp.url or portal_url, tz_name=tz_name
                        )
                        if posts:
                            return FetchBatchResult(
                                site_title=title,
                                posts=posts,
                                source_mode="html",
                                discovered_feed_url=None,
                            )
                else:
                    batch = await self._fetch_via_auto(
                        fetcher, portal_url, None, tz_name, _allow_sso_portal_rescue=False
                    )
                    if batch.posts:
                        return batch
            except CollectorError:
                continue
        return None

    async def _fetch_via_html(
        self,
        fetcher: SmartHttpFetcher,
        canonical_url: str,
        tz_name: str,
    ) -> FetchBatchResult:
        resp = await self._get_with_https_upgrade(fetcher, canonical_url)

        if _is_sso_login_wall(resp, canonical_url):
            rescued = await self._rescue_sso_public_portal(
                fetcher, canonical_url, tz_name, html_only=True
            )
            if rescued is not None:
                return rescued
            _raise_sso_login_error()

        # If a guessed subpath like /news returns 404/410, rescue from the root homepage
        if resp.status_code in (404, 410) and not _is_root_like_url(canonical_url):
            origin = _origin_of(resp.url or canonical_url)
            root_resp = await self._get_with_https_upgrade(fetcher, origin)
            if root_resp.status_code == 200 and not _is_sso_login_wall(root_resp, origin):
                resp = root_resp

        if resp.status_code in (401, 403, 429, 503):
            public_feeds = _resolve_known_public_feeds(canonical_url)
            if public_feeds:
                fast_probe = SmartHttpFetcher(fetcher.settings, allow_browser_fallback=False)
                rescued_feed = await self._probe_candidate_endpoints(
                    fast_probe, public_feeds, base_url=canonical_url, tz_name=tz_name
                )
                if rescued_feed is not None:
                    return rescued_feed

        resp.raise_for_status()

        direct = await self._try_parse_feed_or_wp_response(
            resp, base_url=resp.url, tz_name=tz_name
        )
        if direct is not None:
            return direct

        try:
            title, posts = extract_listing_posts(resp.text, page_url=resp.url, tz_name=tz_name)
        except CollectorError:
            upgraded_empty = await self._try_sparse_homepage_hub_upgrade(
                fetcher, resp.text, resp.url, tz_name, existing_posts=[]
            )
            if upgraded_empty is not None:
                return upgraded_empty
            raise

        upgraded = await self._try_sparse_homepage_hub_upgrade(
            fetcher, resp.text, resp.url, tz_name, existing_posts=posts, existing_title=title
        )
        if upgraded is not None:
            return upgraded

        return FetchBatchResult(
            site_title=title,
            posts=posts,
            source_mode="html",
            discovered_feed_url=None,
        )

    async def _fetch_via_rss_strict(
        self,
        fetcher: SmartHttpFetcher,
        canonical_url: str,
        known_feed_url: Optional[str],
        tz_name: str,
    ) -> FetchBatchResult:
        if known_feed_url:
            res = await self._probe_candidate_endpoints(
                fetcher, [known_feed_url], base_url=canonical_url, tz_name=tz_name
            )
            if res is not None:
                return res

        public_feeds = _resolve_known_public_feeds(canonical_url)
        if public_feeds:
            fast_probe = SmartHttpFetcher(fetcher.settings, allow_browser_fallback=False)
            pub_res = await self._probe_candidate_endpoints(
                fast_probe, public_feeds, base_url=canonical_url, tz_name=tz_name
            )
            if pub_res is not None:
                return pub_res

        resp = await self._get_with_https_upgrade(fetcher, canonical_url)
        if _is_sso_login_wall(resp, canonical_url):
            _raise_sso_login_error()

        final_url = resp.url or canonical_url
        is_root = _is_root_like_url(final_url)

        if resp.status_code == 200:
            direct = await self._try_parse_feed_or_wp_response(
                resp, base_url=final_url, tz_name=tz_name
            )
            if direct is not None:
                return direct

            candidates = discover_feed_urls_in_html(resp.text, final_url, section_strict=not is_root)
        else:
            candidates = []

        for ep in self._build_blind_fallback_endpoints(final_url, is_root=is_root):
            if "/wp-json/" not in ep and ep not in candidates:
                candidates.append(ep)

        # If a guessed section path like /news returned 404/410, also check root feeds (/feed/, /rss.xml)
        if not is_root and resp.status_code in (404, 410):
            origin = _origin_of(final_url)
            for ep in self._build_blind_fallback_endpoints(origin, is_root=True):
                if "/wp-json/" not in ep and ep not in candidates:
                    candidates.append(ep)

        found = await self._probe_candidate_endpoints(
            fetcher, candidates[:8], base_url=final_url, tz_name=tz_name
        )
        if found is not None:
            return found

        if resp.status_code >= 400:
            resp.raise_for_status()

        raise CollectorError(
            "فید RSS یا Atom برای این وب‌سایت یافت نشد. در زبانه تنظیمات حالت دریافت را روی «هوشمند ترکیبی» یا «خزشگر HTML» قرار دهید."
        )

    async def _fetch_via_auto(
        self,
        fetcher: SmartHttpFetcher,
        canonical_url: str,
        known_feed_url: Optional[str],
        tz_name: str,
        *,
        _allow_sso_portal_rescue: bool = True,
    ) -> FetchBatchResult:
        # 1. Try previously discovered feed/API endpoint or explicit feed URL first
        if known_feed_url or looks_like_feed_url(canonical_url) or "/wp-json/" in canonical_url:
            target = known_feed_url or canonical_url
            accept_kind = "json" if "/wp-json/" in target else "document"
            resp = await self._get_with_https_upgrade(
                fetcher, target, accept_kind=accept_kind, referer=_origin_of(canonical_url)
            )
            direct = await self._try_parse_feed_or_wp_response(
                resp, base_url=canonical_url, tz_name=tz_name
            )
            if direct is not None:
                return direct
            if (looks_like_feed_url(canonical_url) or "/wp-json/" in canonical_url) and not known_feed_url:
                resp.raise_for_status()

        # 1b. Known official external feeds (wsj.com, war.gov, ipforum.org)
        public_feeds = _resolve_known_public_feeds(canonical_url)
        if public_feeds:
            fast_probe = SmartHttpFetcher(fetcher.settings, allow_browser_fallback=False)
            pub_res = await self._probe_candidate_endpoints(
                fast_probe, public_feeds, base_url=canonical_url, tz_name=tz_name
            )
            if pub_res is not None:
                return pub_res
            if fetcher.allow_browser_fallback:
                pub_browser_res = await self._probe_candidate_endpoints(
                    fetcher, public_feeds[:1], base_url=canonical_url, tz_name=tz_name
                )
                if pub_browser_res is not None:
                    return pub_browser_res

        # 2. Fetch the main target URL (preferring https:// for plain http:// public URLs)
        resp = await self._get_with_https_upgrade(fetcher, canonical_url)

        # 2a. Detect enterprise SSO/OAuth login redirects (e.g. intel.morningconsult.com -> Okta)
        if _is_sso_login_wall(resp, canonical_url):
            if _allow_sso_portal_rescue:
                rescued_portal = await self._rescue_sso_public_portal(
                    fetcher, canonical_url, tz_name, html_only=False
                )
                if rescued_portal is not None:
                    return rescued_portal
            _raise_sso_login_error()

        final_url = resp.url or canonical_url
        is_root = _is_root_like_url(final_url)

        # 2b. Anti-403 & Dead-Subpath (404) resilience
        if resp.status_code >= 400:
            blind_eps = self._build_blind_fallback_endpoints(final_url, is_root=is_root)
            fast_probe_fetcher = SmartHttpFetcher(fetcher.settings, allow_browser_fallback=False)

            # First try fast non-browser probes across all blind endpoints (often `/wp-json/` or `/feed/` is unblocked!)
            rescued = await self._probe_candidate_endpoints(
                fast_probe_fetcher,
                blind_eps,
                base_url=final_url,
                tz_name=tz_name,
            )
            if rescued is not None:
                return rescued

            # If still blocked, allow browser fallback on primary `/feed/` and `/wp-json/`
            if (
                blind_eps
                and resp.status_code in (401, 403, 406, 429, 503)
                and fetcher.allow_browser_fallback
            ):
                browser_rescued = await self._probe_candidate_endpoints(
                    fetcher, blind_eps[:2], base_url=final_url, tz_name=tz_name
                )
                if browser_rescued is not None:
                    return browser_rescued

            # Dead subpath rescue (e.g. https://www.navaltoday.com/news -> 404 -> rescue from root /feed/ or /)
            if not is_root and resp.status_code in (404, 410):
                origin = _origin_of(final_url)
                root_feed_rescued = await self._probe_candidate_endpoints(
                    fast_probe_fetcher,
                    self._build_blind_fallback_endpoints(origin, is_root=True),
                    base_url=origin,
                    tz_name=tz_name,
                )
                if root_feed_rescued is not None:
                    return root_feed_rescued
                try:
                    return await self._fetch_via_auto(
                        fetcher, origin, None, tz_name, _allow_sso_portal_rescue=False
                    )
                except CollectorError:
                    pass

            if is_root and resp.status_code in (401, 403, 406, 429, 503):
                sm_rescued = await self._probe_sitemap_rescue(fetcher.settings, final_url, tz_name)
                if sm_rescued is not None:
                    return sm_rescued

            resp.raise_for_status()

        # 3. Direct feed or WP REST JSON document
        direct = await self._try_parse_feed_or_wp_response(
            resp, base_url=final_url, tz_name=tz_name
        )
        if direct is not None:
            return direct

        # 4. Check advertised <link rel="alternate"> feeds in HTML <head>
        discovered_feeds = discover_feed_urls_in_html(
            resp.text, final_url, section_strict=not is_root
        )

        # 5. Proactive WordPress / hidden-feed discovery (fdd.org, bicom.org.uk, rasmussenglobal.com)
        if is_wordpress_html(resp.text):
            base_slash = final_url.rstrip("/") + "/"
            feed_ep = urljoin(base_slash, "feed/")
            if not discovered_feeds:
                discovered_feeds.append(feed_ep)
            wp_api_url = discover_wp_posts_api_url(resp.text, final_url, is_root=is_root)
            if wp_api_url and wp_api_url not in discovered_feeds:
                discovered_feeds.append(wp_api_url)

        if discovered_feeds:
            found = await self._probe_candidate_endpoints(
                fetcher, discovered_feeds[:4], base_url=final_url, tz_name=tz_name
            )
            if found is not None:
                return found

        # 6. Scrape HTML listing directly (handles custom portals like iiss.org, lawfaremedia.org, criticalthreats.org, en.idi.org.il)
        try:
            title, posts = extract_listing_posts(resp.text, page_url=final_url, tz_name=tz_name)
            upgraded = await self._try_sparse_homepage_hub_upgrade(
                fetcher, resp.text, final_url, tz_name, existing_posts=posts, existing_title=title
            )
            if upgraded is not None:
                return upgraded
            return FetchBatchResult(
                site_title=title,
                posts=posts,
                source_mode="html",
                discovered_feed_url=None,
            )
        except CollectorError:
            # 7. Last-resort fallback for JS/SPA or promo-only homepages: probe common feeds + WP REST API + section hubs + sitemaps
            fast_probe_fetcher = SmartHttpFetcher(fetcher.settings, allow_browser_fallback=False)
            rescued = await self._probe_candidate_endpoints(
                fast_probe_fetcher,
                self._build_blind_fallback_endpoints(final_url, is_root=is_root),
                base_url=final_url,
                tz_name=tz_name,
            )
            if rescued is not None:
                return rescued
            if is_root:
                upgraded_empty = await self._try_sparse_homepage_hub_upgrade(
                    fetcher, resp.text, final_url, tz_name, existing_posts=[]
                )
                if upgraded_empty is not None:
                    return upgraded_empty
                sm_rescued = await self._probe_sitemap_rescue(fetcher.settings, final_url, tz_name)
                if sm_rescued is not None:
                    return sm_rescued
            raise

    async def enrich_posts_with_full_content(
        self,
        posts: List[FetchedPost],
        settings: Dict[str, str],
    ) -> List[FetchedPost]:
        """
        Fetches individual article pages ONLY for newly selected posts when
        `fetch_full_content` is enabled, enriching short summaries with the full text,
        accurate publication timestamp, and media URL.
        """
        if settings.get("fetch_full_content", "1") == "0" or not posts:
            return posts

        targets = [p for p in posts if p.needs_article_fetch and p.post_url.startswith("http")]
        if not targets:
            return posts

        tz_name = settings.get("timezone") or DEFAULT_TIMEZONE
        sem = asyncio.Semaphore(self.MAX_CONCURRENT_ARTICLE_FETCHES)
        # Reuses cached cf_clearance from SmartHttpFetcher without spawning 10 headless browsers
        fetcher = SmartHttpFetcher(settings, allow_browser_fallback=False)

        async def _enrich_one(post: FetchedPost) -> None:
            async with sem:
                try:
                    resp = await self._get_with_https_upgrade(
                        fetcher, post.post_url, referer=_origin_of(post.post_url)
                    )
                    if resp.status_code != 200 or _is_sso_login_wall(resp, post.post_url):
                        return
                    details = extract_article_details(
                        resp.text, article_url=resp.url, tz_name=tz_name
                    )
                    if details.get("title") and (
                        not post.title or len(str(details["title"])) > len(post.title)
                    ):
                        post.title = str(details["title"])[:300]
                    if details.get("published_at") and post.published_at is None:
                        post.published_at = details["published_at"]
                    full_body = str(details.get("content") or "").strip()
                    if full_body and len(full_body) > len(post.content):
                        post.content = full_body
                    if details.get("media_type") and post.media_type == "text":
                        post.media_type = str(details["media_type"])
                    if details.get("media_url") and not post.media_url:
                        post.media_url = str(details["media_url"])
                except Exception:
                    return

        await asyncio.gather(*(_enrich_one(p) for p in targets))
        return posts

    async def probe_site(
        self,
        raw_url: str,
        settings: Dict[str, str],
    ) -> Dict[str, Any]:
        """Diagnostic check for testing connectivity & feed/HTML extraction before adding a site."""
        try:
            parsed = parse_site_input(raw_url)
        except ValueError as exc:
            raise CollectorError(str(exc)) from exc

        batch = await self.fetch_site_posts(
            site={
                "canonical_url": parsed.canonical_url,
                "feed_url": parsed.feed_url,
            },
            settings=settings,
        )
        return {
            "ok": True,
            "canonical_url": parsed.canonical_url,
            "domain": parsed.domain,
            "site_title": batch.site_title or parsed.default_title,
            "source_mode": batch.source_mode,
            "discovered_feed_url": batch.discovered_feed_url,
            "found_posts_count": len(batch.posts),
            "sample_titles": [p.title for p in batch.posts[:5]],
        }
