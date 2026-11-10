from __future__ import annotations

from datetime import timedelta

from app.collectors.manager import SyncManager
from app.services.scheduler import SiteSchedulerService
from app.utils.jalali import format_iso_utc, utc_now
from app.utils.site_parser import parse_site_input
from tests.conftest import FakeWebSiteCollector, make_post


def _make_site(db, raw: str, active: bool = True):
    return db.create_site(
        parsed=parse_site_input(raw),
        interval_value=1,
        interval_unit="hours",
        initial_limit=10,
        title=None,
        is_active=active,
    )


def test_due_sites_only_returns_active_and_due(db):
    due = _make_site(db, "https://due.example.com")
    future = _make_site(db, "https://future.example.com")
    paused = _make_site(db, "https://paused.example.com", active=False)

    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sites SET next_fetch_at = ? WHERE id = ?",
            (format_iso_utc(now - timedelta(minutes=5)), int(due["id"])),
        )
        conn.execute(
            "UPDATE sites SET next_fetch_at = ? WHERE id = ?",
            (format_iso_utc(now + timedelta(hours=3)), int(future["id"])),
        )
        conn.execute(
            "UPDATE sites SET next_fetch_at = ? WHERE id = ?",
            (format_iso_utc(now - timedelta(hours=5)), int(paused["id"])),
        )

    due_ids = [int(s["id"]) for s in db.get_due_sites(format_iso_utc(now))]
    assert due_ids == [int(due["id"])]


async def test_scheduler_tick_syncs_due_sites_and_reschedules(db):
    web = FakeWebSiteCollector()
    web.set_posts([make_post(1 + i, hours_ago=5 - i) for i in range(3)])
    manager = SyncManager(db=db, web_collector=web)
    scheduler = SiteSchedulerService(db=db, sync_manager=manager, tick_seconds=30)

    site = _make_site(db, "https://due.example.com")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sites SET next_fetch_at = ? WHERE id = ?",
            (format_iso_utc(utc_now() - timedelta(minutes=2)), int(site["id"])),
        )

    processed = await scheduler.run_due_checks()
    assert processed == 1
    assert db.list_posts()["total"] == 3
    refreshed = db.get_site(int(site["id"]))
    assert refreshed["next_fetch_at"] > format_iso_utc(utc_now())
