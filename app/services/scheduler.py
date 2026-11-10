from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.collectors.manager import SyncManager
from app.config import SCHEDULER_TICK_SECONDS
from app.database import DatabaseManager
from app.utils.jalali import utc_now_iso

logger = logging.getLogger("web_collector.scheduler")


class SiteSchedulerService:
    """
    Periodically inspects `sites` for due `next_fetch_at` timestamps and
    dispatches incremental website syncs according to each site's own interval.
    """

    def __init__(
        self,
        db: DatabaseManager,
        sync_manager: SyncManager,
        tick_seconds: int = SCHEDULER_TICK_SECONDS,
    ) -> None:
        self.db = db
        self.sync_manager = sync_manager
        self.tick_seconds = max(10, int(tick_seconds))
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._tick_lock = asyncio.Lock()
        self.last_tick_at: Optional[str] = None
        self.currently_syncing_site_id: Optional[int] = None

    @property
    def is_running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    def start(self) -> None:
        if self._scheduler and self._scheduler.running:
            return
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._scheduler.add_job(
            self.run_due_checks,
            trigger="interval",
            seconds=self.tick_seconds,
            id="due_sites_poller",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        self._scheduler.start()
        logger.info("SiteSchedulerService started (tick=%ss)", self.tick_seconds)

    def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("SiteSchedulerService stopped")

    async def run_due_checks(self) -> int:
        if self._tick_lock.locked():
            return 0

        async with self._tick_lock:
            self.last_tick_at = utc_now_iso()
            due_sites = self.db.get_due_sites(self.last_tick_at)
            processed = 0
            for idx, site in enumerate(due_sites):
                if idx > 0:
                    await asyncio.sleep(1.0)
                site_id = int(site["id"])
                self.currently_syncing_site_id = site_id
                try:
                    await self.sync_manager.sync_site(site_id, trigger_type="scheduled")
                    processed += 1
                except Exception as exc:
                    logger.exception("Scheduled sync failed for site %s: %s", site_id, exc)
                finally:
                    self.currently_syncing_site_id = None
            return processed

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running,
            "tick_seconds": self.tick_seconds,
            "last_tick_at": self.last_tick_at,
            "currently_syncing_site_id": self.currently_syncing_site_id,
        }
