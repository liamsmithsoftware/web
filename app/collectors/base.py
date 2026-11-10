from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class FetchedPost:
    post_guid: str
    post_url: str
    title: str
    published_at: Optional[datetime]
    content: str
    media_type: str = "text"
    media_url: Optional[str] = None
    author: Optional[str] = None
    views: Optional[int] = None
    needs_article_fetch: bool = False


@dataclass
class FetchBatchResult:
    site_title: Optional[str]
    posts: List[FetchedPost]  # Ordered newest -> oldest as listed on website/feed
    source_mode: str  # 'rss' | 'html'
    discovered_feed_url: Optional[str] = None


class CollectorError(RuntimeError):
    """Raised when fetching articles from a website fails."""


def build_httpx_proxy_url(settings: Dict[str, str]) -> Optional[str]:
    if settings.get("proxy_enabled") != "1":
        return None
    host = (settings.get("proxy_host") or "").strip()
    port = (settings.get("proxy_port") or "").strip()
    if not host or not port:
        return None

    scheme = (settings.get("proxy_type") or "socks5").strip().lower()
    if scheme not in ("socks5", "http", "https"):
        scheme = "socks5"

    username = (settings.get("proxy_username") or "").strip()
    password = (settings.get("proxy_password") or "").strip()
    if username:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    else:
        auth = ""

    return f"{scheme}://{auth}{host}:{port}"


def build_request_headers(settings: Dict[str, str]) -> Dict[str, str]:
    ua = (settings.get("custom_user_agent") or "").strip() or DEFAULT_USER_AGENT
    headers: Dict[str, str] = {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/rss+xml,"
            "application/atom+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    cookie = (settings.get("custom_cookie") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers
