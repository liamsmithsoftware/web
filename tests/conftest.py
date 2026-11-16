from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collectors.base import FetchBatchResult, FetchedPost  # noqa: E402
from app.database import DatabaseManager  # noqa: E402
from app.main import create_app  # noqa: E402
from app.utils.site_parser import compute_post_guid  # noqa: E402


def make_post(
    num: int,
    *,
    domain: str = "example.com",
    title: Optional[str] = None,
    text: Optional[str] = None,
    hours_ago: float = 1.0,
    media_type: str = "text",
    views: Optional[int] = 250,
) -> FetchedPost:
    url = f"https://{domain}/posts/{num}"
    guid = compute_post_guid(url, site_canonical_url=f"https://{domain}/")
    headline = title or f"عنوان مطلب شماره {num}"
    body = text or f"متن کامل مطلب آزمایشی شماره {num} در وب‌سایت {domain}."
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return FetchedPost(
        post_guid=guid,
        post_url=url,
        title=headline,
        published_at=dt,
        content=body,
        media_type=media_type,
        views=views,
        needs_article_fetch=False,
    )


class FakeWebSiteCollector:
    """Deterministic offline stand-in for WebSiteCollector during unit/API tests."""

    def __init__(
        self,
        posts: Optional[List[FetchedPost]] = None,
        title: str = "وب‌سایت آزمایشی",
        source_mode: str = "rss",
    ) -> None:
        # Stored newest -> oldest just like real feeds and HTML pages
        self.posts: List[FetchedPost] = posts or []
        self.title = title
        self.source_mode = source_mode
        self.calls: List[Dict[str, Any]] = []
        self.enrich_calls: List[Dict[str, str]] = []
        self.probe_calls: List[Dict[str, Any]] = []

    def set_posts(self, posts: List[FetchedPost]) -> None:
        """Accepts posts in any order and exposes them newest-first (by published_at desc)."""
        self.posts = sorted(
            posts,
            key=lambda p: p.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    async def fetch_site_posts(
        self,
        *,
        site: Dict[str, Any],
        settings: Dict[str, str],
    ) -> FetchBatchResult:
        self.calls.append(
            {
                "canonical_url": site.get("canonical_url"),
                "settings": dict(settings),
            }
        )
        return FetchBatchResult(
            site_title=self.title,
            posts=list(self.posts),
            source_mode=self.source_mode,
            discovered_feed_url=f"{str(site.get('canonical_url')).rstrip('/')}/rss.xml"
            if self.source_mode == "rss"
            else None,
        )

    async def enrich_posts_with_full_content(
        self,
        posts: List[FetchedPost],
        settings: Dict[str, str],
    ) -> List[FetchedPost]:
        self.enrich_calls.append(dict(settings))
        return posts

    async def probe_site(
        self,
        raw_url: str,
        settings: Dict[str, str],
    ) -> Dict[str, Any]:
        from app.collectors.base import CollectorError
        from app.utils.site_parser import parse_site_input

        self.probe_calls.append({"raw_url": raw_url, "settings": dict(settings)})

        try:
            parsed = parse_site_input(raw_url)
        except ValueError as exc:
            raise CollectorError(str(exc)) from exc
        return {
            "ok": True,
            "canonical_url": parsed.canonical_url,
            "domain": parsed.domain,
            "site_title": self.title,
            "source_mode": self.source_mode,
            "discovered_feed_url": f"{parsed.canonical_url.rstrip('/')}/rss.xml",
            "found_posts_count": len(self.posts),
            "sample_titles": [p.title for p in self.posts[:3]],
        }


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(db_path=tmp_path / "test_web.sqlite")


@pytest.fixture()
def fake_web() -> FakeWebSiteCollector:
    return FakeWebSiteCollector()


@pytest.fixture()
def app_client(tmp_path: Path, fake_web: FakeWebSiteCollector):
    application = create_app(
        db_path=tmp_path / "api_test.sqlite",
        enable_scheduler=False,
        web_collector=fake_web,
    )
    with TestClient(application) as client:
        yield client
