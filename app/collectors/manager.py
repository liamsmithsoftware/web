from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from app.collectors.base import CollectorError, FetchedPost
from app.collectors.web_collector import WebSiteCollector
from app.config import DEFAULT_INITIAL_LIMIT, DEFAULT_TIMEZONE
from app.database import DatabaseManager
from app.utils.jalali import split_datetime, utc_now


class SyncManager:
    def __init__(
        self,
        db: DatabaseManager,
        web_collector: Optional[WebSiteCollector] = None,
    ) -> None:
        self.db = db
        self.web_collector = web_collector or WebSiteCollector()
        self._site_locks: Dict[int, asyncio.Lock] = {}

    def _get_lock(self, site_id: int) -> asyncio.Lock:
        if site_id not in self._site_locks:
            self._site_locks[site_id] = asyncio.Lock()
        return self._site_locks[site_id]

    @staticmethod
    def select_new_posts(
        raw_posts: List[FetchedPost],
        *,
        seen_guids: Set[str],
        initial_sync_done: bool,
        initial_limit: int,
    ) -> Tuple[List[FetchedPost], List[str]]:
        """
        Deduplicates within batch (preserving website/feed newest-first listing order)
        and applies two-phase deduplication:
          - First sync (`initial_sync_done == False`):
              Selects ONLY the newest `initial_limit` (default 10) items to store,
              while returning ALL observed item GUIDs on the page so older items
              already present on the page are marked seen and never backfilled later.
          - Subsequent syncs (`initial_sync_done == True`):
              Selects ONLY items whose `post_guid` has never been seen before.
        """
        unique_posts: List[FetchedPost] = []
        batch_seen: Set[str] = set()
        for post in raw_posts:
            guid = (post.post_guid or "").strip()
            if not guid or guid in batch_seen:
                continue
            batch_seen.add(guid)
            unique_posts.append(post)

        all_observed_guids = [p.post_guid for p in unique_posts]

        if not initial_sync_done:
            limit = max(1, int(initial_limit))
            selected_newest_first = unique_posts[:limit]
        else:
            selected_newest_first = [p for p in unique_posts if p.post_guid not in seen_guids]

        return selected_newest_first, all_observed_guids

    @staticmethod
    def prepare_posts_for_storage(
        selected_newest_first: List[FetchedPost],
        *,
        tz_name: str,
        reference_now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Converts selected newest-first posts into chronological (oldest -> newest)
        storage records with split Jalali date, Gregorian date, and local time.
        """
        if not selected_newest_first:
            return []

        now_utc = reference_now or utc_now()
        total = len(selected_newest_first)

        # Reverse so oldest new item gets inserted first (item_seq 1, 2, ..., N)
        oldest_first = list(reversed(selected_newest_first))
        prepared: List[Dict[str, Any]] = []

        for idx, post in enumerate(oldest_first):
            # If the website did not expose a timestamp on the item, assign a stable
            # chronological timestamp preserving the exact listing order on the page.
            dt = post.published_at
            if dt is None:
                dt = now_utc - timedelta(seconds=(total - 1 - idx))

            dt_parts = split_datetime(dt, tz_name=tz_name)
            title = (post.title or "").strip() or "بدون عنوان"
            body = (post.content or "").strip() or title

            # Ensure full text includes headline context if body doesn't already start with it
            if title and body != title and not body.startswith(title):
                combined_content = f"{title}\n\n{body}"
            else:
                combined_content = body

            prepared.append(
                {
                    "post_guid": post.post_guid,
                    "post_url": post.post_url,
                    "title": title,
                    "published_date": dt_parts.published_date,
                    "published_jalali_date": dt_parts.published_jalali_date,
                    "published_time": dt_parts.published_time,
                    "published_at_utc": dt_parts.published_at_utc,
                    "content": combined_content,
                    "media_type": post.media_type or "text",
                    "media_url": post.media_url,
                    "views": post.views,
                }
            )

        return prepared

    async def sync_site(
        self,
        site_id: int,
        *,
        trigger_type: str = "manual",
    ) -> Dict[str, Any]:
        lock = self._get_lock(site_id)
        async with lock:
            site = self.db.get_site(site_id)
            if not site:
                raise LookupError("وب‌سایت مورد نظر یافت نشد.")

            if site.get("is_demo"):
                return {
                    "site_id": site_id,
                    "site_title": site["title"],
                    "fetched_count": 0,
                    "new_saved_count": 0,
                    "previous_last_item_seq": site["last_item_seq"],
                    "new_last_item_seq": site["last_item_seq"],
                    "status": "success",
                    "message": "این یک وب‌سایت نمونه آزمایشی است (برای دریافت واقعی، آدرس وب‌سایت واقعی اضافه کنید).",
                }

            settings = self.db.get_settings()
            tz_name = settings.get("timezone") or DEFAULT_TIMEZONE
            configured_mode = settings.get("collector_mode") or "auto"

            self.db.mark_site_syncing(site_id)
            started = time.perf_counter()

            try:
                batch = await self.web_collector.fetch_site_posts(
                    site=site,
                    settings=settings,
                )
                seen_guids = self.db.get_seen_guids(site_id)
                selected_newest_first, observed_guids = self.select_new_posts(
                    batch.posts,
                    seen_guids=seen_guids,
                    initial_sync_done=bool(site.get("initial_sync_done")),
                    initial_limit=int(site.get("initial_limit") or DEFAULT_INITIAL_LIMIT),
                )

                if selected_newest_first:
                    await self.web_collector.enrich_posts_with_full_content(
                        selected_newest_first,
                        settings=settings,
                    )

                prepared = self.prepare_posts_for_storage(
                    selected_newest_first,
                    tz_name=tz_name,
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                return self.db.save_sync_result(
                    site_id=site_id,
                    resolved_title=batch.site_title,
                    discovered_feed_url=batch.discovered_feed_url,
                    prepared_posts=prepared,
                    observed_guids=observed_guids,
                    trigger_type=trigger_type,
                    source_mode=batch.source_mode,
                    duration_ms=elapsed_ms,
                )
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                err_text = str(exc) if isinstance(exc, CollectorError) else f"خطا در دریافت وب‌سایت: {exc}"
                return self.db.save_sync_error(
                    site_id=site_id,
                    error_message=err_text,
                    trigger_type=trigger_type,
                    source_mode=configured_mode,
                    duration_ms=elapsed_ms,
                )

    async def sync_all_active(self, *, trigger_type: str = "manual") -> Dict[str, Any]:
        sites = [
            s for s in self.db.list_sites()
            if s.get("is_active") and not s.get("is_demo")
        ]
        results: List[Dict[str, Any]] = []
        total_new = 0
        success_count = 0
        error_count = 0

        for idx, site in enumerate(sites):
            if idx > 0:
                await asyncio.sleep(0.8)
            res = await self.sync_site(int(site["id"]), trigger_type=trigger_type)
            results.append(res)
            if res["status"] == "success":
                success_count += 1
                total_new += int(res["new_saved_count"])
            else:
                error_count += 1

        return {
            "checked_sites": len(sites),
            "success_count": success_count,
            "error_count": error_count,
            "total_new_posts": total_new,
            "results": results,
        }
