"""روش اتصال اختصاصی وب‌سایت‌ها (پروکسی per-site) — تست‌های لایه دیتابیس، API و خزشگر."""

from __future__ import annotations

import sqlite3

import pytest

from app.collectors.http_fetcher import resolve_proxy_candidates
from app.collectors.manager import SyncManager
from app.database import (
    DatabaseManager,
    build_profile_proxy_url,
    mask_proxy_url,
    profile_connection_label,
)
from app.utils.site_parser import parse_site_input
from tests.conftest import FakeWebSiteCollector, make_post


def _add_site(db: DatabaseManager, url: str, title: str | None = None) -> dict:
    return db.create_site(parse_site_input(url), title=title, is_active=True)


# ----------------------------------------------------------------------
# Database layer
# ----------------------------------------------------------------------
def test_create_profile_builds_proxy_url_and_masks_secrets(db):
    profile = db.create_connection_profile(
        name="پروکسی v2ray",
        kind="proxy",
        proxy_type="socks5",
        proxy_host="127.0.0.1",
        proxy_port="10808",
        proxy_username="user",
        proxy_password="p@ssword",
        notes="برای سایت‌های تحریمی",
    )

    assert profile["id"] > 0
    assert profile["proxy_url"] == "socks5://user:p%40ssword@127.0.0.1:10808"
    assert profile["connection_label"] == "socks5://127.0.0.1:10808 (با نام کاربری)"

    public = DatabaseManager.public_profile(profile)
    assert "proxy_password" not in public
    assert public["proxy_password_set"] is True
    # آدرس پروکسی در پاسخ عمومی، نام کاربری و رمز را ندارد
    assert "user" not in public["proxy_url"]
    assert public["proxy_url"] == "socks5://127.0.0.1:10808"

    # خواندن مجدد به‌صورت عمومی هم باید پرچم «رمز تنظیم شده» را حفظ کند
    reread = db.get_connection_profile(int(profile["id"]), include_secrets=False)
    assert reread["proxy_password_set"] is True
    assert "proxy_password" not in reread
    listed = db.list_connection_profiles()
    assert listed[0]["proxy_password_set"] is True


def test_profile_validation_rules(db):
    with pytest.raises(ValueError):
        db.create_connection_profile(name="   ", proxy_host="127.0.0.1", proxy_port="1080")

    with pytest.raises(ValueError):
        db.create_connection_profile(name="بدون پورت", kind="proxy", proxy_host="127.0.0.1")

    with pytest.raises(ValueError):
        db.create_connection_profile(
            name="پورت بد", kind="proxy", proxy_host="127.0.0.1", proxy_port="70000"
        )

    with pytest.raises(ValueError):
        db.create_connection_profile(name="تست", kind="tunnel")

    db.create_connection_profile(name="تست", kind="proxy", proxy_host="1.2.3.4", proxy_port="8080")
    with pytest.raises(ValueError):
        db.create_connection_profile(name="تست", kind="proxy", proxy_host="1.2.3.4", proxy_port="8080")

    # مهلت زمانی خارج از بازه مجاز
    with pytest.raises(ValueError):
        db.create_connection_profile(
            name="مهلت بد", kind="proxy", proxy_host="1.2.3.4", proxy_port="8080", request_timeout=2
        )


def test_update_profile_keeps_password_when_masked(db):
    profile = db.create_connection_profile(
        name="پروکسی", kind="proxy", proxy_host="127.0.0.1", proxy_port="1080", proxy_password="secret"
    )

    updated = db.update_connection_profile(
        int(profile["id"]), proxy_port="2080", proxy_password="********"
    )
    assert updated["proxy_port"] == "2080"
    assert updated["proxy_password"] == "secret"
    assert updated["proxy_url"] == "socks5://127.0.0.1:2080"

    updated2 = db.update_connection_profile(int(profile["id"]), proxy_password="new-pass")
    assert updated2["proxy_password"] == "new-pass"


def test_assign_profile_only_affects_selected_sites(db):
    target = _add_site(db, "https://blocked.example.com", "سایت مسدود")
    other = _add_site(db, "https://open.example.com", "سایت باز")

    profile = db.create_connection_profile(
        name="پروکسی اختصاصی", kind="proxy", proxy_host="127.0.0.1", proxy_port="10808"
    )
    result = db.assign_connection_profile([int(target["id"])], int(profile["id"]))

    assert result["updated_sites"] == 1
    assert result["profile_name"] == "پروکسی اختصاصی"

    refreshed_target = db.get_site(int(target["id"]))
    refreshed_other = db.get_site(int(other["id"]))

    assert refreshed_target["connection_profile_id"] == profile["id"]
    assert refreshed_target["connection_profile_name"] == "پروکسی اختصاصی"
    assert "پروکسی اختصاصی" in refreshed_target["connection_label"]

    # وب‌سایت انتخاب‌نشده همچنان در حالت عمومی برنامه است
    assert refreshed_other["connection_profile_id"] is None
    assert refreshed_other["connection_label"] == "حالت عمومی برنامه"

    summary = db.get_sites_connection_summary()
    assert summary["assigned_sites"] == 1
    assert summary["general_mode_sites"] == 1

    # بازگرداندن به حالت عمومی
    db.assign_connection_profile([int(target["id"])], None)
    assert db.get_site(int(target["id"]))["connection_profile_id"] is None


def test_assign_rejects_unknown_profile_or_sites(db):
    site = _add_site(db, "https://example.com")
    with pytest.raises(LookupError):
        db.assign_connection_profile([int(site["id"])], 999)
    with pytest.raises(LookupError):
        db.assign_connection_profile([4242], None)
    with pytest.raises(ValueError):
        db.assign_connection_profile([], None)


def test_delete_profile_returns_sites_to_general_mode(db):
    site = _add_site(db, "https://example.com")
    profile = db.create_connection_profile(
        name="موقت", kind="proxy", proxy_host="127.0.0.1", proxy_port="1080"
    )
    db.assign_connection_profile([int(site["id"])], int(profile["id"]))

    assert db.delete_connection_profile(int(profile["id"])) is True
    assert db.get_connection_profile(int(profile["id"])) is None
    assert db.get_site(int(site["id"]))["connection_profile_id"] is None
    assert db.get_site(int(site["id"]))["connection_label"] == "حالت عمومی برنامه"


def test_legacy_manual_proxy_still_supported(db):
    site = _add_site(db, "https://example.com")
    db.update_site(int(site["id"]), proxy_url="http://old-proxy:3128")
    refreshed = db.get_site(int(site["id"]))
    assert refreshed["proxy_url"] == "http://old-proxy:3128"
    assert "پروکسی دستی" in refreshed["connection_label"]

    effective = db.build_effective_settings(refreshed)
    assert effective["site_proxy_url"] == "http://old-proxy:3128"

    # پاک‌کردن پروکسی دستی با رشته خالی
    cleared = db.update_site(int(site["id"]), proxy_url="")
    assert cleared["proxy_url"] is None
    assert cleared["connection_label"] == "حالت عمومی برنامه"


def test_build_effective_settings_priority_and_overrides(db):
    site = _add_site(db, "https://blocked.example.com")
    profile = db.create_connection_profile(
        name="پروکسی با تنظیمات خاص",
        kind="proxy",
        proxy_type="http",
        proxy_host="10.0.0.5",
        proxy_port=8080,
        proxy_username="alice",
        proxy_password="wonder",
        verify_ssl=False,
        request_timeout=45,
        fallback_direct=False,
    )
    db.assign_connection_profile([int(site["id"])], int(profile["id"]))

    global_settings = db.get_settings()
    global_settings.update(
        {
            "proxy_enabled": "1",
            "proxy_host": "127.0.0.1",
            "proxy_port": "10808",
            "verify_ssl": "1",
            "request_timeout": "25",
        }
    )

    effective = db.build_effective_settings(db.get_site(int(site["id"])), global_settings)
    assert effective["site_proxy_url"] == "http://alice:wonder@10.0.0.5:8080"
    assert effective["site_proxy_no_fallback"] == "1"
    assert effective["verify_ssl"] == "0"
    assert effective["request_timeout"] == "45"
    assert effective["connection_profile_name"] == "پروکسی با تنظیمات خاص"
    # تنظیمات عمومی برنامه دست‌نخورده باقی می‌ماند
    assert global_settings["verify_ssl"] == "1"
    assert "site_proxy_url" not in global_settings

    # پروفایل «اتصال مستقیم» پروکسی عمومی را نادیده می‌گیرد
    direct = db.create_connection_profile(name="بدون پروکسی", kind="direct")
    db.assign_connection_profile([int(site["id"])], int(direct["id"]))
    direct_effective = db.build_effective_settings(db.get_site(int(site["id"])), global_settings)
    assert direct_effective["site_force_direct"] == "1"
    assert "site_proxy_url" not in direct_effective

    # پروفایل غیرفعال → همان حالت عمومی
    db.update_connection_profile(int(direct["id"]), is_enabled=False)
    disabled_effective = db.build_effective_settings(db.get_site(int(site["id"])), global_settings)
    assert disabled_effective.get("site_force_direct") != "1"
    assert "connection_profile_name" not in disabled_effective

    # وب‌سایت بدون پروفایل → تنظیمات عمومی بدون تغییر
    other = _add_site(db, "https://open.example.com")
    other_effective = db.build_effective_settings(db.get_site(int(other["id"])), global_settings)
    assert "site_proxy_url" not in other_effective
    assert other_effective["verify_ssl"] == "1"


def test_resolve_proxy_candidates_honours_site_connection(db):
    profile = db.create_connection_profile(
        name="پروکسی", kind="proxy", proxy_host="127.0.0.1", proxy_port="10808", fallback_direct=False
    )
    site = _add_site(db, "https://blocked.example.com")
    db.assign_connection_profile([int(site["id"])], int(profile["id"]))

    global_settings = db.get_settings()
    global_settings.update(
        {"proxy_enabled": "1", "proxy_host": "9.9.9.9", "proxy_port": "1080", "use_system_proxy": "1"}
    )
    effective = db.build_effective_settings(db.get_site(int(site["id"])), global_settings)

    candidates = resolve_proxy_candidates(effective, "https://blocked.example.com/news")
    assert candidates == [("socks5://127.0.0.1:10808", True)]

    # سایت داخلی .ir بدون پروفایل → اول اتصال مستقیم
    general = resolve_proxy_candidates(global_settings, "https://isna.ir/")
    assert general[0] == (None, False)

    # پروفایل «اتصال مستقیم» حتی برای سایت خارجی هم پروکسی عمومی را دور می‌زند
    direct = db.create_connection_profile(name="مستقیم", kind="direct")
    db.assign_connection_profile([int(site["id"])], int(direct["id"]))
    direct_effective = db.build_effective_settings(db.get_site(int(site["id"])), global_settings)
    assert resolve_proxy_candidates(direct_effective, "https://blocked.example.com") == [(None, False)]

    # localhost همیشه مستقیم است
    assert resolve_proxy_candidates(effective, "http://127.0.0.1:8000/x") == [(None, False)]


def test_migration_adds_connection_columns_to_old_database(tmp_path):
    legacy_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(legacy_path))
    conn.executescript(
        """
        CREATE TABLE sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_key TEXT NOT NULL UNIQUE,
            site_input TEXT NOT NULL,
            domain TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            feed_url TEXT,
            title TEXT NOT NULL,
            custom_title INTEGER NOT NULL DEFAULT 0,
            is_direct_feed INTEGER NOT NULL DEFAULT 0,
            interval_value REAL NOT NULL DEFAULT 6,
            interval_unit TEXT NOT NULL DEFAULT 'hours',
            interval_minutes INTEGER NOT NULL DEFAULT 360,
            initial_limit INTEGER NOT NULL DEFAULT 10,
            initial_sync_done INTEGER NOT NULL DEFAULT 0,
            last_item_seq INTEGER,
            last_post_guid TEXT,
            last_post_url TEXT,
            last_post_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_demo INTEGER NOT NULL DEFAULT 0,
            last_fetched_at TEXT,
            next_fetch_at TEXT,
            last_status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL,
            site_domain TEXT NOT NULL,
            site_title TEXT NOT NULL,
            item_seq INTEGER NOT NULL,
            post_guid TEXT NOT NULL,
            post_url TEXT NOT NULL,
            title TEXT NOT NULL,
            published_date TEXT NOT NULL,
            published_jalali_date TEXT NOT NULL,
            published_time TEXT NOT NULL,
            published_at_utc TEXT NOT NULL,
            content TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'text',
            media_url TEXT,
            views INTEGER,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE seen_items (
            site_id INTEGER NOT NULL,
            post_guid TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (site_id, post_guid)
        );
        CREATE TABLE fetch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            site_title TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            source_mode TEXT NOT NULL,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            new_saved_count INTEGER NOT NULL DEFAULT 0,
            previous_last_item_seq INTEGER,
            new_last_item_seq INTEGER,
            status TEXT NOT NULL,
            message TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO sites (
            site_key, site_input, domain, canonical_url, title, created_at, updated_at
        ) VALUES ('example.com|https://example.com/', 'https://example.com', 'example.com',
                  'https://example.com/', 'سایت قدیمی', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    migrated = DatabaseManager(db_path=legacy_path)
    sites = migrated.list_sites()
    assert len(sites) == 1
    assert sites[0]["title"] == "سایت قدیمی"
    assert sites[0]["connection_profile_id"] is None
    assert sites[0]["connection_label"] == "حالت عمومی برنامه"

    profile = migrated.create_connection_profile(
        name="پروکسی جدید", kind="proxy", proxy_host="127.0.0.1", proxy_port="1080"
    )
    migrated.assign_connection_profile([int(sites[0]["id"])], int(profile["id"]))
    assert migrated.get_site(int(sites[0]["id"]))["connection_profile_name"] == "پروکسی جدید"


def test_helpers_mask_credentials():
    assert mask_proxy_url("socks5://user:pass@1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert mask_proxy_url("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert mask_proxy_url(None) == ""
    assert build_profile_proxy_url({"kind": "direct"}) is None
    assert profile_connection_label({"kind": "direct"}) == "اتصال مستقیم (بدون پروکسی)"


# ----------------------------------------------------------------------
# Collector / SyncManager integration
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_manager_passes_site_connection_to_collector(db):
    fake = FakeWebSiteCollector(posts=[make_post(i, domain="blocked.example.com", hours_ago=5 - i) for i in range(1, 4)])
    manager = SyncManager(db=db, web_collector=fake)

    site = _add_site(db, "https://blocked.example.com", "سایت مسدود")
    other = _add_site(db, "https://open.example.com", "سایت باز")
    profile = db.create_connection_profile(
        name="پروکسی اختصاصی", kind="proxy", proxy_host="127.0.0.1", proxy_port="10808"
    )
    db.assign_connection_profile([int(site["id"])], int(profile["id"]))

    db.update_settings({"fetch_full_content": "1"})
    for post in fake.posts:
        post.needs_article_fetch = True

    result = await manager.sync_site(int(site["id"]), trigger_type="manual")
    assert result["status"] == "success"

    used_settings = fake.calls[-1]["settings"]
    assert used_settings["site_proxy_url"] == "socks5://127.0.0.1:10808"
    # دریافت متن کامل مقاله هم از همان پروکسی عبور می‌کند
    assert fake.enrich_calls, "متن کامل مقالات باید دریافت شده باشد"
    assert fake.enrich_calls[-1]["site_proxy_url"] == "socks5://127.0.0.1:10808"

    await manager.sync_site(int(other["id"]), trigger_type="manual")
    general_settings = fake.calls[-1]["settings"]
    assert "site_proxy_url" not in general_settings
    assert "site_force_direct" not in general_settings


# ----------------------------------------------------------------------
# API layer
# ----------------------------------------------------------------------
def _seed_two_sites(app_client):
    first = app_client.post(
        "/api/sites", json={"site_input": "https://blocked.example.com", "fetch_now": False}
    ).json()["site"]
    second = app_client.post(
        "/api/sites", json={"site_input": "https://open.example.com", "fetch_now": False}
    ).json()["site"]
    return first, second


def test_api_connection_profile_crud_and_assignment(app_client):
    first, second = _seed_two_sites(app_client)

    created = app_client.post(
        "/api/connection-profiles",
        json={
            "name": "پروکسی v2ray محلی",
            "kind": "proxy",
            "proxy_type": "socks5",
            "proxy_host": "127.0.0.1",
            "proxy_port": "10808",
            "proxy_username": "user",
            "proxy_password": "top-secret",
            "fallback_direct": False,
            "notes": "فقط سایت‌های تحریمی",
        },
    )
    assert created.status_code == 200
    profile = created.json()["profile"]
    assert profile["name"] == "پروکسی v2ray محلی"
    assert "proxy_password" not in profile
    assert profile["proxy_password_set"] is True
    assert "top-secret" not in created.text

    # خطای اعتبارسنجی
    bad = app_client.post(
        "/api/connection-profiles", json={"name": "بدون پورت", "kind": "proxy", "proxy_host": "1.2.3.4"}
    )
    assert bad.status_code == 400

    duplicate = app_client.post(
        "/api/connection-profiles",
        json={"name": "پروکسی v2ray محلی", "kind": "proxy", "proxy_host": "1.2.3.4", "proxy_port": "1"},
    )
    assert duplicate.status_code == 400

    listing = app_client.get("/api/connection-profiles").json()
    assert listing["summary"]["profiles"] == 1
    assert listing["summary"]["assigned_sites"] == 0
    assert listing["items"][0]["connection_label"].startswith("socks5://127.0.0.1:10808")
    assert {s["id"] for s in listing["sites"]} == {first["id"], second["id"]}

    # اعمال روی یک وب‌سایت انتخاب‌شده
    assigned = app_client.post(
        "/api/connection-profiles/assign",
        json={"profile_id": profile["id"], "site_ids": [first["id"]]},
    )
    assert assigned.status_code == 200
    body = assigned.json()
    assert body["updated_sites"] == 1
    assert "top-secret" not in assigned.text

    sites = {s["id"]: s for s in app_client.get("/api/sites").json()["items"]}
    assert sites[first["id"]]["connection_profile_id"] == profile["id"]
    assert "پروکسی v2ray محلی" in sites[first["id"]]["connection_label"]
    assert sites[first["id"]]["connection_is_general"] is False
    # وب‌سایت انتخاب‌نشده در حالت عمومی باقی می‌ماند
    assert sites[second["id"]]["connection_profile_id"] is None
    assert sites[second["id"]]["connection_is_general"] is True
    assert sites[second["id"]]["connection_label"] == "حالت عمومی برنامه"

    summary = app_client.get("/api/connection-profiles").json()["summary"]
    assert summary["assigned_sites"] == 1
    assert summary["general_mode_sites"] == 1

    # ویرایش پروفایل
    updated = app_client.put(
        f"/api/connection-profiles/{profile['id']}", json={"proxy_port": "2080", "is_enabled": True}
    )
    assert updated.status_code == 200
    assert updated.json()["profile"]["proxy_port"] == "2080"

    # بازگرداندن یک وب‌سایت به حالت عمومی
    reverted = app_client.post(
        "/api/connection-profiles/assign", json={"profile_id": None, "site_ids": [first["id"]]}
    )
    assert reverted.status_code == 200
    assert reverted.json()["profile_id"] is None
    sites_after = {s["id"]: s for s in app_client.get("/api/sites").json()["items"]}
    assert sites_after[first["id"]]["connection_profile_id"] is None

    # خطاها
    assert app_client.post("/api/connection-profiles/assign", json={"profile_id": None, "site_ids": []}).status_code == 400
    assert app_client.post(
        "/api/connection-profiles/assign", json={"profile_id": 4242, "site_ids": [first["id"]]}
    ).status_code == 404
    assert app_client.post(
        "/api/connection-profiles/assign", json={"profile_id": profile["id"], "site_ids": [4242]}
    ).status_code == 404
    assert app_client.put("/api/connection-profiles/4242", json={"name": "x"}).status_code == 404
    assert app_client.delete("/api/connection-profiles/4242").status_code == 404

    # حذف پروفایل
    app_client.post(
        "/api/connection-profiles/assign", json={"profile_id": profile["id"], "site_ids": [first["id"]]}
    )
    deleted = app_client.delete(f"/api/connection-profiles/{profile['id']}")
    assert deleted.status_code == 200
    assert "بازگشتند" in deleted.json()["message"]
    sites_final = {s["id"]: s for s in app_client.get("/api/sites").json()["items"]}
    assert sites_final[first["id"]]["connection_profile_id"] is None
    assert app_client.get("/api/connection-profiles").json()["items"] == []


def test_api_site_create_and_update_with_connection_profile(app_client):
    profile = app_client.post(
        "/api/connection-profiles",
        json={"name": "پروکسی سایت جدید", "kind": "proxy", "proxy_host": "127.0.0.1", "proxy_port": "1080"},
    ).json()["profile"]

    created = app_client.post(
        "/api/sites",
        json={
            "site_input": "https://new-site.example.com",
            "fetch_now": False,
            "connection_profile_id": profile["id"],
        },
    )
    assert created.status_code == 200
    site = created.json()["site"]
    assert site["connection_profile_id"] == profile["id"]
    assert site["connection_profile_name"] == "پروکسی سایت جدید"

    # ویرایش: بازگشت به حالت عمومی با ارسال صریح null
    updated = app_client.put(
        f"/api/sites/{site['id']}", json={"title": "عنوان تازه", "connection_profile_id": None}
    )
    assert updated.status_code == 200
    assert updated.json()["site"]["connection_profile_id"] is None
    assert updated.json()["site"]["title"] == "عنوان تازه"

    # ویرایش بدون فیلد پروفایل → روش اتصال تغییر نمی‌کند
    app_client.put(f"/api/sites/{site['id']}", json={"connection_profile_id": profile["id"]})
    untouched = app_client.put(f"/api/sites/{site['id']}", json={"title": "فقط عنوان"})
    assert untouched.json()["site"]["connection_profile_id"] == profile["id"]

    # شناسه نامعتبر
    assert app_client.put(
        f"/api/sites/{site['id']}", json={"connection_profile_id": 4242}
    ).status_code == 404


def test_api_probe_with_connection_profile(app_client, fake_web):
    profile = app_client.post(
        "/api/connection-profiles",
        json={
            "name": "پروکسی تست",
            "kind": "proxy",
            "proxy_type": "http",
            "proxy_host": "127.0.0.1",
            "proxy_port": "8080",
        },
    ).json()["profile"]

    fake_web.set_posts([make_post(1, domain="example.com")])
    res = app_client.post(
        "/api/sites/probe",
        json={"site_input": "https://example.com", "connection_profile_id": profile["id"]},
    )
    assert res.status_code == 200
    assert "پروکسی تست" in res.json()["connection_label"]
    assert fake_web.probe_calls[-1]["settings"]["site_proxy_url"] == "http://127.0.0.1:8080"

    # حالت عمومی (بدون پروفایل)
    res_general = app_client.post("/api/sites/probe", json={"site_input": "https://example.com"})
    assert res_general.json()["connection_label"] == "حالت عمومی برنامه"
    assert "site_proxy_url" not in fake_web.probe_calls[-1]["settings"]

    assert app_client.post(
        "/api/sites/probe", json={"site_input": "https://example.com", "connection_profile_id": 4242}
    ).status_code == 404


def test_backup_and_restore_carries_connection_profiles(app_client, tmp_path):
    first, second = _seed_two_sites(app_client)
    profile = app_client.post(
        "/api/connection-profiles",
        json={
            "name": "پروکسی پشتیبان",
            "kind": "proxy",
            "proxy_host": "127.0.0.1",
            "proxy_port": "10808",
            "proxy_password": "backup-secret",
        },
    ).json()["profile"]
    app_client.post(
        "/api/connection-profiles/assign", json={"profile_id": profile["id"], "site_ids": [first["id"]]}
    )

    safe_payload = app_client.get("/api/backup/export").json()
    assert safe_payload["counts"]["connection_profiles"] == 1
    assert "backup-secret" not in str(safe_payload)
    assert safe_payload["connection_profiles"][0]["name"] == "پروکسی پشتیبان"
    assigned_sites = [s for s in safe_payload["sites"] if s.get("connection_profile")]
    assert len(assigned_sites) == 1
    assert assigned_sites[0]["connection_profile"] == "پروکسی پشتیبان"

    secret_payload = app_client.get("/api/backup/export?include_secrets=true").json()
    assert secret_payload["connection_profiles"][0]["proxy_password"] == "backup-secret"

    restored = app_client.post(
        "/api/backup/restore", json={"mode": "merge", "payload": secret_payload}
    ).json()
    assert restored["assigned_profiles"] == 1
    assert "روش اتصال" in restored["message"]
    sites = {s["title"]: s for s in app_client.get("/api/sites").json()["items"]}
    profiles = app_client.get("/api/connection-profiles").json()["items"]
    assert len(profiles) == 1
    assert sites["blocked.example.com"]["connection_profile_id"] == profiles[0]["id"]
    assert sites["open.example.com"]["connection_profile_id"] is None
