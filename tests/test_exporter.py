from __future__ import annotations

import io
import sqlite3

from openpyxl import load_workbook

from app.collectors.manager import SyncManager
from app.services.exporter import create_sqlite_export_snapshot, generate_excel_bytes
from app.utils.site_parser import parse_site_input
from tests.conftest import FakeWebSiteCollector, make_post


async def _seed(db, posts=None):
    web = FakeWebSiteCollector()
    web.set_posts(posts or [make_post(1 + i, hours_ago=10 - i) for i in range(5)])
    manager = SyncManager(db=db, web_collector=web)
    site = db.create_site(
        parsed=parse_site_input("https://export.example.com"),
        interval_value=6,
        interval_unit="hours",
        initial_limit=10,
        title="سایت خروجی",
        is_active=True,
    )
    await manager.sync_site(int(site["id"]))
    return int(site["id"])


async def test_excel_export_is_rtl_and_contains_structured_columns(db):
    await _seed(db, posts=[make_post(1, title="=SUM(1,2)", text="=HYPERLINK()", hours_ago=1)])
    posts = db.get_posts_for_export()
    sites = db.list_sites()

    content = generate_excel_bytes(posts=posts, sites=sites, tz_name="Asia/Tehran")
    assert content[:2] == b"PK"

    wb = load_workbook(io.BytesIO(content))
    assert wb.sheetnames == ["مطالب وب‌سایت‌ها", "لیست وب‌سایت‌ها"]
    ws = wb["مطالب وب‌سایت‌ها"]
    assert ws.sheet_view.rightToLeft is True

    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    for required in ["عنوان مطلب", "آدرس مستقیم مطلب", "تاریخ شمسی", "تاریخ میلادی", "ساعت انتشار", "متن مطلب"]:
        assert required in headers

    # Formula injection neutralized with ZWNJ prefix
    title_cell_val = ws.cell(row=2, column=5).value
    assert not str(title_cell_val).startswith("=")
    # Direct link has clickable hyperlink
    assert ws.cell(row=2, column=6).hyperlink is not None


async def test_sqlite_export_strips_secrets_and_supports_filtering(db):
    await _seed(db)
    db.update_settings({"proxy_password": "top-secret-password", "custom_cookie": "sid=secret123"})

    all_posts = db.get_posts_for_export()
    keep_two = [int(all_posts[0]["id"]), int(all_posts[1]["id"])]
    snap = create_sqlite_export_snapshot(db, filtered_post_ids=keep_two, strip_sensitive_settings=True)
    try:
        conn = sqlite3.connect(str(snap))
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            assert cnt == 2
            secrets = conn.execute(
                "SELECT value FROM settings WHERE key IN ('proxy_password', 'custom_cookie')"
            ).fetchall()
            assert secrets == []
            view_cnt = conn.execute("SELECT COUNT(*) FROM vw_posts_export").fetchone()[0]
            assert view_cnt == 2
        finally:
            conn.close()
    finally:
        snap.unlink(missing_ok=True)
