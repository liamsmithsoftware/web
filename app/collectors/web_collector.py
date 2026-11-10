from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from app.collectors.base import (
    CollectorError,
    FetchBatchResult,
    FetchedPost,
)
from app.collectors.feed_parser import (
    discover_feed_urls_in_html,
    is_feed_document,
    parse_feed_content,
)
from app.collectors.html_scraper import extract_article_details, extract_listing_posts
from app.collectors.http_fetcher import HttpFetchResponse, SmartHttpFetcher
from app.collectors.wp_api_parser import (
    discover_wp_posts_api_url,
    is_wordpress_html,
    is_wp_posts_json,
    parse_wp_posts_json,
)
from app.config import DEFAULT_TIMEZONE
from app.utils.site_parser import looks_like_feed_url, parse_site_input

COMMON_ROOT_FEED_PATHS = (
    "/feed/",
    "/feed",
    "/rss.xml",
    "/rss",
    "/feed.xml",
    "/atom.xml",
)


def _is_root_like_url(url: str) -> bool:
    path = (urlparse(url).path or "/").rstrip("/").lower()
    return path in ("", "/", "/fa", "/en", "/index.html", "/index.php")


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return url


class WebSiteCollector:
    """
    Hybrid Anti-403 Website Collector supporting:
      - 'auto': Smart RSS/Atom + WordPress REST API + HTML scraping fallback
      - 'rss':  Strict RSS / Atom / JSON Feed reader
      - 'html': Direct HTML listing & article scraper
    Uses SmartHttpFetcher (HTTP/2 + TLS/JA3 browser impersonation + OS proxy support).
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

    async def _try_parse_feed_or_wp_response(
        self,
        resp: HttpFetchResponse,
        *,
        base_url: str,
        tz_name: str,
    ) -> Optional[FetchBatchResult]:
        if resp.status_code != 200 or not resp.text:
            return None

        if is_feed_document(resp.text, resp.content_type):
            title, posts = parse_feed_content(resp.text, base_url=base_url, tz_name=tz_name)
            if posts:
                return FetchBatchResult(
                    site_title=title,
                    posts=posts,
                    source_mode="rss",
                    discovered_feed_url=resp.url,
                )

        if is_wp_posts_json(resp.text, resp.content_type):
            title, posts = parse_wp_posts_json(resp.text, base_url=base_url, tz_name=tz_name)
            if posts:
                return FetchBatchResult(
                    site_title=title,
                    posts=posts,
                    source_mode="wp_api",
                    discovered_feed_url=resp.url,
                )

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
                parsed = await self._try_parse_feed_or_wp_response(
                    resp, base_url=base_url, tz_name=tz_name
                )
                if parsed is not None:
                    return parsed
            except CollectorError:
                continue
        return None

    def _build_blind_fallback_endpoints(self, url: str, *, is_root: bool) -> List[str]:
        """Endpoints probed when homepage returns 403/503 or hides feed tags."""
        endpoints: List[str] = []
        base_slash = url.rstrip("/") + "/"
        if is_root:
            for suffix in COMMON_ROOT_FEED_PATHS:
                endpoints.append(urljoin(base_slash, suffix.lstrip("/")))
            origin = _origin_of(url)
            endpoints.append(urljoin(origin, "wp-json/wp/v2/posts?per_page=20&_embed=1"))
        else:
            # WordPress & Ghost section feeds: https://site.com/section/feed/
            endpoints.append(urljoin(base_slash, "feed/"))
            endpoints.append(urljoin(base_slash, "rss"))
        return endpoints

    async def _fetch_via_html(
        self,
        fetcher: SmartHttpFetcher,
        canonical_url: str,
        tz_name: str,
    ) -> FetchBatchResult:
        resp = await fetcher.get(canonical_url)
        resp.raise_for_status()

        direct = await self._try_parse_feed_or_wp_response(
            resp, base_url=resp.url, tz_name=tz_name
        )
        if direct is not None:
            return direct

        title, posts = extract_listing_posts(resp.text, page_url=resp.url, tz_name=tz_name)
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

        resp = await fetcher.get(canonical_url)
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

        found = await self._probe_candidate_endpoints(
            fetcher, candidates[:7], base_url=final_url, tz_name=tz_name
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
    ) -> FetchBatchResult:
        # 1. Try previously discovered feed/API endpoint or explicit feed URL first
        if known_feed_url or looks_like_feed_url(canonical_url) or "/wp-json/" in canonical_url:
            target = known_feed_url or canonical_url
            accept_kind = "json" if "/wp-json/" in target else "document"
            resp = await fetcher.get(target, accept_kind=accept_kind, referer=_origin_of(canonical_url))
            direct = await self._try_parse_feed_or_wp_response(
                resp, base_url=canonical_url, tz_name=tz_name
            )
            if direct is not None:
                return direct
            if (looks_like_feed_url(canonical_url) or "/wp-json/" in canonical_url) and not known_feed_url:
                resp.raise_for_status()

        # 2. Fetch the main target URL
        resp = await fetcher.get(canonical_url)
        final_url = resp.url or canonical_url
        is_root = _is_root_like_url(final_url)

        # Anti-403 resilience: if the main HTML page is blocked by WAF (403/503),
        # automatically probe /feed/, /rss.xml, and /wp-json/wp/v2/posts before failing!
        if resp.status_code >= 400:
            rescued = await self._probe_candidate_endpoints(
                fetcher,
                self._build_blind_fallback_endpoints(final_url, is_root=is_root),
                base_url=final_url,
                tz_name=tz_name,
            )
            if rescued is not None:
                return rescued
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

        # 5. Proactive WordPress / hidden-feed discovery (e.g. fdd.org hides <link type="rss+xml">!)
        if not discovered_feeds and is_wordpress_html(resp.text):
            base_slash = final_url.rstrip("/") + "/"
            discovered_feeds.append(urljoin(base_slash, "feed/"))
            wp_api_url = discover_wp_posts_api_url(resp.text, final_url, is_root=is_root)
            if wp_api_url:
                discovered_feeds.append(wp_api_url)

        if discovered_feeds:
            found = await self._probe_candidate_endpoints(
                fetcher, discovered_feeds[:4], base_url=final_url, tz_name=tz_name
            )
            if found is not None:
                return found

        # 6. Scrape HTML listing directly
        try:
            title, posts = extract_listing_posts(resp.text, page_url=final_url, tz_name=tz_name)
            return FetchBatchResult(
                site_title=title,
                posts=posts,
                source_mode="html",
                discovered_feed_url=None,
            )
        except CollectorError:
            # 7. Last-resort fallback for JS/SPA homepages: probe common feeds + WP REST API
            rescued = await self._probe_candidate_endpoints(
                fetcher,
                self._build_blind_fallback_endpoints(final_url, is_root=is_root),
                base_url=final_url,
                tz_name=tz_name,
            )
            if rescued is not None:
                return rescued
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
        fetcher = SmartHttpFetcher(settings)

        async def _enrich_one(post: FetchedPost) -> None:
            async with sem:
                try:
                    resp = await fetcher.get(
                        post.post_url, referer=_origin_of(post.post_url)
                    )
                    if resp.status_code != 200:
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
