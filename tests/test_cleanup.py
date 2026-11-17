from __future__ import annotations

from tests.conftest import make_post


def _seed_two_sites(app_client, fake_web):
    fake_web.set_posts(
        [
            make_post(1, domain="site-a.ir", text="خبر اول", hours_ago=48, media_type="text"),
            make_post(2, domain="site-a.ir", text="تبلیغ ویژه", hours_ago=36, media_type="photo"),
            make_post(3, domain="site-a.ir", text="خبر سوم", hours_ago=24, media_type="video"),
            make_post(4, domain="site-a.ir", text="خبر چهارم", hours_ago=12, media_type="text"),
        ]
    )
    res_a = app_client.post(
        "/api/sites",
        json={"site_input": "https://site-a.ir", "interval_value": 6, "interval_unit": "hours", "fetch_now": True},
    ).json()["site"]

    fake_web.set_posts(
        [
            make_post(10, domain="site-b.ir", text="مطلب ب۱", hours_ago=30, media_type="text"),
            make_post(11, domain="site-b.ir", text="مطلب ب۲", hours_ago=2, media_type="document"),
        ]
    )
    res_b = app_client.post(
        "/api/sites",
        json={"site_input": "https://site-b.ir", "interval_value": 12, "interval_unit": "hours", "fetch_now": True},
    ).json()["site"]

    return res_a, res_b


def test_bulk_delete_by_site_and_cursor_reset_behavior(app_client, fake_web):
    site_a, site_b = _seed_two_sites(app_client, fake_web)
    assert app_client.get("/api/posts").json()["total"] == 6

    body = app_client.post(
        "/api/posts/bulk-delete",
        json={"site_id": site_a["id"], "reset_site_cursors": True},
    ).json()
    assert body["deleted_posts"] == 4
    assert body["affected_sites"] == [site_a["id"]]
    assert body["sites_reset"] == 1
    assert "۴ مطلب حذف شد" in body["message"]
    assert app_client.get("/api/posts").json()["total"] == 2

    # Because cursor was reset, syncing site_a again re-fetches its posts!
    fake_web.set_posts(
        [
            make_post(1, domain="site-a.ir", text="خبر اول", hours_ago=48),
            make_post(2, domain="site-a.ir", text="تبلیغ ویژه", hours_ago=36),
        ]
    )
    resync = app_client.post(f"/api/sites/{site_a['id']}/sync").json()["sync_result"]
    assert resync["new_saved_count"] == 2


def test_delete_selected_post_ids_without_cursor_reset_prevents_redownload(app_client, fake_web):
    site_a, _ = _seed_two_sites(app_client, fake_web)
    posts_a = app_client.get(f"/api/posts?site_id={site_a['id']}").json()["items"]
    target_id = posts_a[0]["id"]

    del_res = app_client.post(
        "/api/posts/bulk-delete",
        json={"post_ids": [target_id], "reset_site_cursors": False},
    ).json()
    assert del_res["deleted_posts"] == 1
    assert del_res["sites_reset"] == 0

    # Syncing again with the same 4 items MUST NOT re-download the deleted post!
    fake_web.set_posts(
        [
            make_post(1, domain="site-a.ir", text="خبر اول", hours_ago=48, media_type="text"),
            make_post(2, domain="site-a.ir", text="تبلیغ ویژه", hours_ago=36, media_type="photo"),
            make_post(3, domain="site-a.ir", text="خبر سوم", hours_ago=24, media_type="video"),
            make_post(4, domain="site-a.ir", text="خبر چهارم", hours_ago=12, media_type="text"),
        ]
    )
    resync = app_client.post(f"/api/sites/{site_a['id']}/sync").json()["sync_result"]
    assert resync["new_saved_count"] == 0


def test_purge_keep_sites_vs_factory_reset(app_client, fake_web):
    _seed_two_sites(app_client, fake_web)

    keep = app_client.post("/api/posts/purge", json={"keep_sites": True}).json()
    assert keep["deleted_posts"] == 6
    assert keep["sites_kept"] is True
    assert len(app_client.get("/api/sites").json()["items"]) == 2

    wipe = app_client.post("/api/posts/purge", json={"keep_sites": False}).json()
    assert wipe["deleted_sites"] == 2
    assert len(app_client.get("/api/sites").json()["items"]) == 0
