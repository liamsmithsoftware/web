from __future__ import annotations

import io
import json

from tests.conftest import make_post

BACKUP_FORMAT = "website-content-collector-backup"


def _seed(app_client, fake_web):
    fake_web.set_posts([make_post(1 + i, domain="bk-site.ir", hours_ago=5 - i) for i in range(3)])
    created = app_client.post(
        "/api/sites",
        json={
            "site_input": "https://bk-site.ir",
            "interval_value": 4,
            "interval_unit": "hours",
            "initial_limit": 10,
            "fetch_now": True,
        },
    ).json()["site"]
    app_client.put(f"/api/sites/{created['id']}", json={"title": "وب‌سایت پشتیبان"})
    app_client.post(
        "/api/sites",
        json={
            "site_input": "https://paused-site.ir/news",
            "interval_value": 2,
            "interval_unit": "days",
            "is_active": False,
            "fetch_now": False,
        },
    )
    app_client.put(
        "/api/settings",
        json={
            "proxy_enabled": True,
            "proxy_host": "127.0.0.1",
            "proxy_port": "10808",
            "proxy_password": "secret-proxy-pass",
            "custom_cookie": "auth=secret-cookie",
        },
    )


def test_export_backup_excludes_secrets_by_default_and_includes_when_requested(app_client, fake_web):
    _seed(app_client, fake_web)

    safe_res = app_client.get("/api/backup/export")
    assert safe_res.status_code == 200
    assert ".json" in safe_res.headers["content-disposition"]
    safe_payload = safe_res.json()
    assert safe_payload["format"] == BACKUP_FORMAT
    assert safe_payload["counts"]["sites"] == 2
    assert "proxy_password" not in safe_payload["settings"]
    assert "custom_cookie" not in safe_payload["settings"]

    sec_res = app_client.get("/api/backup/export?include_secrets=true")
    sec_payload = sec_res.json()
    assert sec_payload["secrets_included"] is True
    assert sec_payload["settings"]["proxy_password"] == "secret-proxy-pass"
    assert sec_payload["settings"]["custom_cookie"] == "auth=secret-cookie"


def test_validate_and_restore_backup_file_merge_and_replace(app_client, fake_web):
    _seed(app_client, fake_web)
    backup_bytes = app_client.get("/api/backup/export").content

    # Wipe everything so we restore onto a fresh empty database
    app_client.post("/api/posts/purge", json={"keep_sites": False})
    assert len(app_client.get("/api/sites").json()["items"]) == 0

    val_res = app_client.post(
        "/api/backup/validate-file",
        files={"file": ("backup.json", io.BytesIO(backup_bytes), "application/json")},
    )
    assert val_res.status_code == 200
    val_body = val_res.json()
    assert val_body["valid"] is True
    assert val_body["sites_in_file"] == 2
    assert val_body["new_sites"] == 2

    restore_res = app_client.post(
        "/api/backup/restore-file?mode=replace&restore_settings=true&overwrite_settings=true",
        files={"file": ("backup.json", io.BytesIO(backup_bytes), "application/json")},
    )
    assert restore_res.status_code == 200
    rbody = restore_res.json()
    assert rbody["added_sites"] == 2
    # Since archive was empty, cursor must NOT be restored and warning must be emitted
    assert rbody["cursor_restored"] == 0
    assert len(rbody["warnings"]) == 1

    sites = {s["site_key"]: s for s in app_client.get("/api/sites").json()["items"]}
    assert sites["bk-site.ir"]["title"] == "وب‌سایت پشتیبان"
    assert sites["bk-site.ir"]["initial_sync_done"] is False


def test_rejects_foreign_backup_format(app_client):
    bad = json.dumps({"format": "telegram-channel-collector-backup", "format_version": 1}).encode("utf-8")
    res = app_client.post(
        "/api/backup/validate-file",
        files={"file": ("bad.json", io.BytesIO(bad), "application/json")},
    )
    assert res.status_code == 400
