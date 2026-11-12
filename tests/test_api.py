from __future__ import annotations

import io
import sqlite3

from openpyxl import load_workbook

from tests.conftest import make_post


def test_healthz_and_offline_rtl_root_page(app_client):
    health = app_client.get("/healthz")
    assert health.status_code == 200
    hjson = health.json()
    assert hjson["status"] == "ok"
    assert hjson["app"] == "website-content-collector"

    page = app_client.get("/")
    assert page.status_code == 200
    assert 'dir="rtl"' in page.text
    assert "آرشیو هوشمند مطالب وب‌سایت‌ها" in page.text
    # Must work 100% offline without external CDNs/fonts
    assert "cdn" not in page.text.lower()
    assert "fonts.googleapis" not in page.text.lower()

    assert app_client.get("/static/css/style.css").status_code == 200
    assert app_client.get("/static/js/app.js").status_code == 200


def test_end_to_end_site_lifecycle_filters_probe_demo_and_exports(app_client, fake_web):
    # 1. Overview starts empty
    ov = app_client.get("/api/overview").json()
    assert ov["stats"]["total_sites"] == 0
    assert ov["stats"]["total_posts"] == 0
    assert ov["collector_mode"] == "auto"

    # 2. Probe endpoint works
    fake_web.set_posts([make_post(1 + i, domain="zoomit.ir", hours_ago=30 - i) for i in range(25)])
    probe = app_client.post("/api/sites/probe", json={"site_input": "zoomit.ir"})
    assert probe.status_code == 200
    assert probe.json()["found_posts_count"] == 25

    # 3. Create website -> initial sync fetches strictly 10 latest posts
    created_resp = app_client.post(
        "/api/sites",
        json={
            "site_input": "https://www.zoomit.ir/",
            "interval_value": 4,
            "interval_unit": "hours",
            "initial_limit": 10,
            "fetch_now": True,
        },
    )
    assert created_resp.status_code == 200
    site = created_resp.json()["site"]
    assert created_resp.json()["sync_result"]["new_saved_count"] == 10
    assert site["posts_count"] == 10

    # Duplicate registration (even without www/https) is blocked
    dup = app_client.post("/api/sites", json={"site_input": "zoomit.ir", "fetch_now": False})
    assert dup.status_code == 400

    # 4. Posts pagination, search & ids endpoint
    posts_resp = app_client.get("/api/posts?page=1&page_size=5").json()
    assert posts_resp["total"] == 10
    assert len(posts_resp["items"]) == 5
    ids_resp = app_client.get("/api/posts/ids").json()
    assert ids_resp["total"] == 10

    # 5. Logs recorded
    logs = app_client.get("/api/logs").json()["items"]
    assert len(logs) == 1
    assert logs[0]["new_saved_count"] == 10

    # 6. Excel & SQLite HTTP downloads
    xlsx_resp = app_client.get("/api/exports/excel")
    assert xlsx_resp.status_code == 200
    wb = load_workbook(io.BytesIO(xlsx_resp.content))
    assert "مطالب وب‌سایت‌ها" in wb.sheetnames

    sqlite_resp = app_client.get("/api/exports/sqlite")
    assert sqlite_resp.status_code == 200
    assert sqlite_resp.content[:16] == b"SQLite format 3\x00"

    # 7. Demo seed & clear
    seed = app_client.post("/api/demo/seed").json()
    assert seed["seeded"] is True
    assert seed["inserted_posts"] == 15

    cleared = app_client.delete("/api/demo").json()
    assert cleared["cleared_sites"] == 2


def test_sites_filtering_and_pagination_10_20_30_and_all(app_client):
    # 1. Verify UI HTML includes filter controls & page size options (default 10, 20, 30, all)
    page_html = app_client.get("/").text
    assert 'id="siteFiltersForm"' in page_html
    assert 'id="siteFilterSearch"' in page_html
    assert 'id="siteFilterStatus"' in page_html
    assert 'id="siteFilterSource"' in page_html
    assert 'id="sitesPageSize"' in page_html
    assert '<option value="10" selected>' in page_html
    assert '<option value="20">' in page_html
    assert '<option value="30">' in page_html
    assert '<option value="all">همه</option>' in page_html

    # 2. Register 25 sites (some RSS feeds, some active, some inactive)
    for idx in range(1, 26):
        is_feed = idx % 5 == 0
        url = f"https://site-{idx:02d}.example.org/rss.xml" if is_feed else f"https://site-{idx:02d}.example.org/"
        res = app_client.post(
            "/api/sites",
            json={
                "site_input": url,
                "title": f"سایت خبری شماره {idx}",
                "interval_value": 6,
                "interval_unit": "hours",
                "initial_limit": 10,
                "is_active": idx != 25,
                "fetch_now": False,
            },
        )
        assert res.status_code == 200

    # Default page_size=10 when page=1 is passed
    p1 = app_client.get("/api/sites?page=1").json()
    assert p1["total"] == 25
    assert p1["page_size"] == 10
    assert p1["total_pages"] == 3
    assert len(p1["items"]) == 10

    # page_size=20 and page_size=30 and page_size=all
    p20 = app_client.get("/api/sites?page=1&page_size=20").json()
    assert len(p20["items"]) == 20
    assert p20["total_pages"] == 2

    p30 = app_client.get("/api/sites?page=1&page_size=30").json()
    assert len(p30["items"]) == 25
    assert p30["total_pages"] == 1

    pall = app_client.get("/api/sites?page=1&page_size=all").json()
    assert len(pall["items"]) == 25
    assert pall["page_size"] == "all"

    # Search (including Persian digits!), status, and source filters
    search_res = app_client.get("/api/sites?search=۱۲").json()
    assert search_res["total"] == 1
    assert "site-12.example.org" in search_res["items"][0]["canonical_url"]

    inactive_res = app_client.get("/api/sites?status=inactive").json()
    assert inactive_res["total"] == 1
    assert inactive_res["items"][0]["is_active"] == 0

    feed_res = app_client.get("/api/sites?source=has_feed").json()
    assert feed_res["total"] == 5

