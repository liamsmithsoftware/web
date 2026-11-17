from __future__ import annotations

from app.collectors.manager import SyncManager
from app.collectors.http_fetcher import resolve_proxy_candidates
from app.utils.site_parser import parse_site_input
from tests.conftest import FakeWebSiteCollector, make_post


def _create_site(db, raw: str = "https://news.example.com", initial_limit: int = 10, title=None):
    return db.create_site(
        parsed=parse_site_input(raw),
        interval_value=6,
        interval_unit="hours",
        initial_limit=initial_limit,
        title=title,
        is_active=True,
    )


async def test_first_sync_stores_only_latest_ten_and_marks_older_items_seen(db):
    web = FakeWebSiteCollector()
    # Website page has 30 articles (101..130 where 130 is newest)
    web.set_posts([make_post(101 + i, hours_ago=30 - i) for i in range(30)])
    manager = SyncManager(db=db, web_collector=web)

    site = _create_site(db)
    assert site["initial_sync_done"] == 0

    first = await manager.sync_site(int(site["id"]), trigger_type="manual")
    assert first["status"] == "success"
    assert first["new_saved_count"] == 10
    assert first["new_last_item_seq"] == 10

    stored = db.list_posts(page_size=100)
    assert stored["total"] == 10
    urls = {p["post_url"] for p in stored["items"]}
    # Must contain strictly the newest 10 (121..130), NOT 101..120
    assert urls == {f"https://example.com/posts/{n}" for n in range(121, 131)}

    # Second sync with the exact same 30 items on page MUST NOT backfill 101..120!
    second = await manager.sync_site(int(site["id"]), trigger_type="scheduled")
    assert second["status"] == "success"
    assert second["new_saved_count"] == 0
    assert db.list_posts(page_size=100)["total"] == 10


async def test_incremental_sync_saves_only_newly_published_articles(db):
    web = FakeWebSiteCollector()
    web.set_posts([make_post(1 + i, hours_ago=20 - i) for i in range(15)])
    manager = SyncManager(db=db, web_collector=web)

    site = _create_site(db, initial_limit=10)
    await manager.sync_site(int(site["id"]))
    assert db.list_posts(page_size=100)["total"] == 10

    # Now 3 brand-new articles (16, 17, 18) are published at the top of the website
    web.set_posts([make_post(1 + i, hours_ago=25 - i) for i in range(18)])
    inc = await manager.sync_site(int(site["id"]))
    assert inc["status"] == "success"
    assert inc["new_saved_count"] == 3
    assert inc["previous_last_item_seq"] == 10
    assert inc["new_last_item_seq"] == 13
    assert db.list_posts(page_size=100)["total"] == 13


async def test_custom_title_is_preserved_while_default_title_updates_automatically(db):
    web = FakeWebSiteCollector(title="عنوان واقعی سایت")
    web.set_posts([make_post(1, hours_ago=1)])
    manager = SyncManager(db=db, web_collector=web)

    auto_site = _create_site(db, "https://auto.example.com", title=None)
    await manager.sync_site(int(auto_site["id"]))
    assert db.get_site(int(auto_site["id"]))["title"] == "عنوان واقعی سایت"

    custom_site = _create_site(db, "https://custom.example.com", title="عنوان دلخواه من")
    await manager.sync_site(int(custom_site["id"]))
    assert db.get_site(int(custom_site["id"]))["title"] == "عنوان دلخواه من"


async def test_site_proxy_is_used_for_full_content_and_clearing_uses_global_proxy(db):
    web = FakeWebSiteCollector()
    web.set_posts([make_post(1)])
    enrichment_settings = []

    async def record_enrichment_settings(posts, settings):
        enrichment_settings.append(dict(settings))
        return posts

    web.enrich_posts_with_full_content = record_enrichment_settings
    manager = SyncManager(db=db, web_collector=web)
    site = _create_site(db, "https://proxy.example.com")
    db.update_site(int(site["id"]), proxy_url="socks5://127.0.0.1:1080")

    first = await manager.sync_site(int(site["id"]))
    assert first["status"] == "success"
    assert enrichment_settings[-1]["site_proxy_url"] == "socks5://127.0.0.1:1080"

    db.update_site(int(site["id"]), proxy_url="")
    db.update_settings(
        {
            "proxy_enabled": "1",
            "proxy_type": "http",
            "proxy_host": "general-proxy",
            "proxy_port": "8080",
        }
    )
    web.set_posts([make_post(2), make_post(1)])

    second = await manager.sync_site(int(site["id"]))
    assert second["status"] == "success"
    cleared_settings = enrichment_settings[-1]
    assert "site_proxy_url" not in cleared_settings
    assert resolve_proxy_candidates(
        cleared_settings, "https://proxy.example.com/"
    ) == [("http://general-proxy:8080", True)]
