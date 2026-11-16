"""یکپارچگی رابط کاربری: شناسه‌های مورد استفاده در app.js باید در index.html وجود داشته باشند.

این تست از تکرار خرابی قبلی جلوگیری می‌کند (تزریق کد در میانه شیء `state` که باعث
خطای نحوی در کل فایل جاوااسکریپت و از کار افتادن رابط کاربری شده بود).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "app" / "templates" / "index.html"
STYLE_CSS = ROOT / "app" / "static" / "css" / "style.css"


@pytest.fixture(scope="module")
def js_text() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html_text() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _referenced_ids(js_text: str) -> set[str]:
    refs = set(re.findall(r'\$\("#([A-Za-z0-9_]+)"\)', js_text))
    refs |= set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', js_text))
    return refs


def test_every_id_referenced_in_js_exists_in_html(js_text, html_text):
    html_ids = set(re.findall(r'id="([^"]+)"', html_text))
    missing = sorted(_referenced_ids(js_text) - html_ids)
    assert not missing, f"شناسه‌های زیر در جاوااسکریپت استفاده شده‌اند ولی در HTML وجود ندارند: {missing}"


def test_state_object_is_intact_and_legacy_broken_patch_removed(js_text):
    # شیء state باید سالم باشد (رگرسیون تزریق کد در میانه آن)
    assert "editingSiteId: null," in js_text
    assert "selectedPostIds: new Set()," in js_text

    # بخش قدیمی و ناقص «اختصاص پروکسی چندتایی» در زبانه پاک‌سازی حذف شده است
    assert "proxyMultiSiteSelect" not in js_text
    assert "btnApplyProxyMulti" not in js_text
    assert 'id="proxyMultiSiteSelect"' not in INDEX_HTML.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js برای بررسی نحو در دسترس نیست")
def test_app_js_has_valid_syntax():
    result = subprocess.run(
        [shutil.which("node"), "--check", str(APP_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"خطای نحوی در app.js:\n{result.stderr}"


def test_connection_profile_ui_present(html_text, js_text):
    required_ids = [
        "connectionProfileForm",
        "cpName",
        "cpType",
        "cpHost",
        "cpPort",
        "cpUser",
        "cpPass",
        "cpVerifySsl",
        "cpTimeout",
        "cpFallback",
        "cpEnabled",
        "cpNotes",
        "cpAssignProfile",
        "cpSiteSearch",
        "cpSiteList",
        "cpSelectedCount",
        "btnApplyConnectionProfile",
        "connectionProfilesBody",
        "cpTestResultBox",
        "btnReloadConnectionProfiles",
        "editSiteConnectionProfile",
        "probeConnectionProfile",
    ]
    missing = [ident for ident in required_ids if f'id="{ident}"' not in html_text]
    assert not missing, f"المان‌های رابط کاربری روش اتصال اختصاصی ناقص است: {missing}"

    # بخش جدید باید داخل زبانه تنظیمات (پروکسی و خزشگر وب) باشد
    settings_tab = html_text.split('<section id="tab-settings"')[1].split("</section>")[0]
    assert "روش اتصال اختصاصی وب‌سایت‌ها" in settings_tab
    assert 'id="connectionProfileForm"' in settings_tab

    for fn in (
        "loadConnectionProfiles",
        "renderConnectionProfilesTable",
        "renderConnectionSiteList",
        "applyConnectionProfileToSelected",
        "initConnectionProfileEvents",
        "testConnectionProfile",
    ):
        assert f"function {fn}" in js_text or f"async function {fn}" in js_text
        assert fn in js_text

    assert "initConnectionProfileEvents();" in js_text
    assert "await loadConnectionProfiles();" in js_text


def test_connection_profile_styles_exist(html_text):
    css = STYLE_CSS.read_text(encoding="utf-8")
    for cls in (
        ".connection-card",
        ".connection-layout",
        ".cp-site-list",
        ".cp-site-row",
        ".badge-proxy",
        ".ch-connection",
        ".advanced-details",
    ):
        assert cls in css, f"استایل {cls} در style.css تعریف نشده است"
