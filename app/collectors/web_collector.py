from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.collectors.base import (
    CollectorError,
    FetchBatchResult,
    FetchedPost,
    build_httpx_proxy_url,
    build_request_headers,
)
from app.collectors.feed_parser import (
    discover_feed_urls_in_html,
    is_feed_document,
    parse_feed_content,
)
from app.collectors.html_scraper import extract_article_details, extract_listing_posts
from app.config import DEFAULT_REQUEST_TIMEOUT, DEFAULT_TIMEZONE
from app.utils.site_parser import looks_like_feed_url, parse_site_input

COMMON_ROOT_FEED_PATHS = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
)


def _is_root_like_url(url: str) -> bool:
    path = (urlparse(url).path or "/").rstrip("/").lower()
    return path in ("", "/", "/fa", "/en", "/index.html", "/index.php")


class WebSiteCollector:
    """
    Hybrid Website Collector supporting:
      - 'auto': Smart RSS/Atom discovery + HTML page scraping fallback
      - 'rss':  Strict RSS / Atom / JSON Feed reader
      - 'html': Direct HTML listing & article scraper
    """

    MAX_CONCURRENT_ARTICLE_FETCHES = 4

    def _client_kwargs(self, settings: Dict[str, str]) -> Dict[str, Any]:
        proxy_url = build_httpx_proxy_url(settings)
        headers = build_request_headers(settings)
        verify_ssl = settings.get("verify_ssl", "1") != "0"
        try:
            timeout_sec = float(settings.get("request_timeout") or DEFAULT_REQUEST_TIMEOUT)
            timeout_sec = max(5.0, min(120.0, timeout_sec))
        except (TypeError, ValueError):
            timeout_sec = float(DEFAULT_REQUEST_TIMEOUT)

        return {
            "headers": headers,
            "follow_redirects": True,
            "timeout": httpx.Timeout(timeout_sec, connect=min(15.0, timeout_sec)),
            "proxy": proxy_url,
            "verify": verify_ssl,
        }

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

        try:
            async with httpx.AsyncClient(**self._client_kwargs(settings)) as client:
                if mode == "html":
                    return await self._fetch_via_html(client, canonical_url, tz_name)

                if mode == "rss":
                    return await self._fetch_via_rss_strict(
                        client, canonical_url, known_feed_url, tz_name
                    )

                # Default: 'auto' (smart hybrid)
                return await self._fetch_via_auto(
                    client, canonical_url, known_feed_url, tz_name
                )

        except CollectorError:
            raise
        except httpx.ProxyError as exc:
            raise CollectorError(
                f"خطا در اتصال به پروکسی تنظیم‌شده: {exc}. لطفاً آدرس و پورت پروکسی را در تنظیمات بررسی کنید."
            ) from exc
        except httpx.ConnectTimeout as exc:
            raise CollectorError(
                "مهلت زمانی اتصال به وب‌سایت به پایان رسید (Timeout). در صورت محدودیت شبکه، پروکسی یا مهلت زمانی را در تنظیمات بررسی کنید."
            ) from exc
        except httpx.ReadTimeout as exc:
            raise CollectorError(
                "وب‌سایت در زمان مقرر پاسخ نداد (Read Timeout)."
            ) from exc
        except httpx.ConnectError as exc:
            err_str = str(exc)
            if "SSL" in err_str.upper() or "CERTIFICATE" in err_str.upper():
                raise CollectorError(
                    "خطای گواهی امنیتی SSL در اتصال به وب‌سایت. می‌توانید در زبانه تنظیمات گزینه «بررسی گواهی SSL» را غیرفعال کنید."
                ) from exc
            raise CollectorError(
                f"عدم دسترسی شبکه به وب‌سایت ({canonical_url}): {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise CollectorError(
                f"سرور وب‌سایت کد وضعیت ناموفق برگرداند (HTTP {exc.response.status_code})."
            ) from exc
        except Exception as exc:
            raise CollectorError(f"خطای غیرمنتظره هنگام دریافت وب‌سایت: {exc}") from exc

    async def _fetch_via_html(
        self,
        client: httpx.AsyncClient,
        canonical_url: str,
        tz_name: str,
    ) -> FetchBatchResult:
        resp = await client.get(canonical_url)
        resp.raise_for_status()
        final_url = str(resp.url)

        # Even in HTML mode, if the user explicitly registered an XML feed URL, handle it cleanly
        if is_feed_document(resp.text, resp.headers.get("content-type", "")):
            title, posts = parse_feed_content(resp.text, base_url=final_url, tz_name=tz_name)
            return FetchBatchResult(
                site_title=title,
                posts=posts,
                source_mode="rss",
                discovered_feed_url=final_url,
            )

        title, posts = extract_listing_posts(resp.text, page_url=final_url, tz_name=tz_name)
        return FetchBatchResult(
            site_title=title,
            posts=posts,
            source_mode="html",
            discovered_feed_url=None,
        )

    async def _fetch_via_rss_strict(
        self,
        client: httpx.AsyncClient,
        canonical_url: str,
        known_feed_url: Optional[str],
        tz_name: str,
    ) -> FetchBatchResult:
        # 1. Try known feed URL first
        if known_feed_url:
            resp = await client.get(known_feed_url)
            if resp.status_code == 200 and is_feed_document(
                resp.text, resp.headers.get("content-type", "")
            ):
                title, posts = parse_feed_content(resp.text, base_url=canonical_url, tz_name=tz_name)
                if posts:
                    return FetchBatchResult(
                        site_title=title,
                        posts=posts,
                        source_mode="rss",
                        discovered_feed_url=str(resp.url),
                    )

        # 2. Fetch main URL
        resp = await client.get(canonical_url)
        resp.raise_for_status()
        final_url = str(resp.url)

        if is_feed_document(resp.text, resp.headers.get("content-type", "")):
            title, posts = parse_feed_content(resp.text, base_url=final_url, tz_name=tz_name)
            return FetchBatchResult(
                site_title=title,
                posts=posts,
                source_mode="rss",
                discovered_feed_url=final_url,
            )

        # 3. Discover feed links in HTML <head> + common feed endpoints
        candidates = discover_feed_urls_in_html(resp.text, final_url, section_strict=False)
        for suffix in COMMON_ROOT_FEED_PATHS:
            probe_url = urljoin(final_url.rstrip("/") + "/", suffix.lstrip("/"))
            if probe_url not in candidates:
                candidates.append(probe_url)

        for feed_candidate in candidates[:6]:
            try:
                f_resp = await client.get(feed_candidate)
            except httpx.HTTPError:
                continue
            if f_resp.status_code == 200 and is_feed_document(
                f_resp.text, f_resp.headers.get("content-type", "")
            ):
                title, posts = parse_feed_content(f_resp.text, base_url=final_url, tz_name=tz_name)
                if posts:
                    return FetchBatchResult(
                        site_title=title,
                        posts=posts,
                        source_mode="rss",
                        discovered_feed_url=str(f_resp.url),
                    )

        raise CollectorError(
            "فید RSS یا Atom برای این وب‌سایت یافت نشد. در زبانه تنظیمات حالت دریافت را روی «هوشمند ترکیبی» یا «خزشگر HTML» قرار دهید."
        )

    async def _fetch_via_auto(
        self,
        client: httpx.AsyncClient,
        canonical_url: str,
        known_feed_url: Optional[str],
        tz_name: str,
    ) -> FetchBatchResult:
        # 1. If we already discovered/stored a feed URL for this site, try it directly!
        if known_feed_url or looks_like_feed_url(canonical_url):
            target_feed = known_feed_url or canonical_url
            try:
                f_resp = await client.get(target_feed)
                if f_resp.status_code == 200 and is_feed_document(
                    f_resp.text, f_resp.headers.get("content-type", "")
                ):
                    title, posts = parse_feed_content(
                        f_resp.text, base_url=canonical_url, tz_name=tz_name
                    )
                    if posts:
                        return FetchBatchResult(
                            site_title=title,
                            posts=posts,
                            source_mode="rss",
                            discovered_feed_url=str(f_resp.url),
                        )
            except (httpx.HTTPError, CollectorError):
                if looks_like_feed_url(canonical_url) and not known_feed_url:
                    raise

        # 2. Fetch the target page
        resp = await client.get(canonical_url)
        resp.raise_for_status()
        final_url = str(resp.url)
        ctype = resp.headers.get("content-type", "")

        if is_feed_document(resp.text, ctype):
            title, posts = parse_feed_content(resp.text, base_url=final_url, tz_name=tz_name)
            if posts:
                return FetchBatchResult(
                    site_title=title,
                    posts=posts,
                    source_mode="rss",
                    discovered_feed_url=final_url,
                )

        # 3. Check if HTML <head> advertises a matching RSS/Atom feed
        is_root = _is_root_like_url(final_url)
        discovered_feeds = discover_feed_urls_in_html(
            resp.text, final_url, section_strict=not is_root
        )
        for feed_candidate in discovered_feeds[:3]:
            try:
                f_resp = await client.get(feed_candidate)
                if f_resp.status_code == 200 and is_feed_document(
                    f_resp.text, f_resp.headers.get("content-type", "")
                ):
                    title, posts = parse_feed_content(
                        f_resp.text, base_url=final_url, tz_name=tz_name
                    )
                    if posts:
                        return FetchBatchResult(
                            site_title=title,
                            posts=posts,
                            source_mode="rss",
                            discovered_feed_url=str(f_resp.url),
                        )
            except (httpx.HTTPError, CollectorError):
                continue

        # 4. Scrape HTML listing directly
        try:
            title, posts = extract_listing_posts(resp.text, page_url=final_url, tz_name=tz_name)
            return FetchBatchResult(
                site_title=title,
                posts=posts,
                source_mode="html",
                discovered_feed_url=None,
            )
        except CollectorError:
            # Last-resort fallback on root sites (e.g., SPA frontpage that still exposes /feed or /rss)
            if is_root:
                for suffix in COMMON_ROOT_FEED_PATHS:
                    probe_url = urljoin(final_url.rstrip("/") + "/", suffix.lstrip("/"))
                    try:
                        f_resp = await client.get(probe_url)
                        if f_resp.status_code == 200 and is_feed_document(
                            f_resp.text, f_resp.headers.get("content-type", "")
                        ):
                            title, posts = parse_feed_content(
                                f_resp.text, base_url=final_url, tz_name=tz_name
                            )
                            if posts:
                                return FetchBatchResult(
                                    site_title=title,
                                    posts=posts,
                                    source_mode="rss",
                                    discovered_feed_url=str(f_resp.url),
                                )
                    except (httpx.HTTPError, CollectorError):
                        continue
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

        async with httpx.AsyncClient(**self._client_kwargs(settings)) as client:

            async def _enrich_one(post: FetchedPost) -> None:
                async with sem:
                    try:
                        resp = await client.get(post.post_url)
                        if resp.status_code != 200:
                            return
                        details = extract_article_details(
                            resp.text, article_url=str(resp.url), tz_name=tz_name
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
                        # Keep existing summary/headline if article page couldn't be fetched
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
