from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

from app.config import (
    DB_PATH,
    DEFAULT_INITIAL_LIMIT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_TIMEZONE,
)
from app.utils.jalali import format_iso_utc, split_datetime, utc_now, utc_now_iso
from app.utils.site_parser import ParsedSite, compute_post_guid, normalize_digits

BACKUP_FORMAT_NAME = "website-content-collector-backup"
# نسخه ۲: بخش «روش‌های اتصال اختصاصی وب‌سایت‌ها» (connection_profiles) به پشتیبان اضافه شد
BACKUP_FORMAT_VERSION = 2

# تنظیمات حساس که به‌صورت پیش‌فرض در فایل پشتیبان یا خروجی عمومی قرار نمی‌گیرند
BACKUP_SECRET_SETTING_KEYS = {
    "proxy_username",
    "proxy_password",
    "custom_cookie",
}

DEFAULT_SETTINGS: Dict[str, str] = {
    "collector_mode": "auto",  # 'auto' | 'rss' | 'html'
    "timezone": DEFAULT_TIMEZONE,
    "default_initial_limit": str(DEFAULT_INITIAL_LIMIT),
    "fetch_full_content": "1",
    "verify_ssl": "1",
    "request_timeout": str(DEFAULT_REQUEST_TIMEOUT),
    "custom_user_agent": "",
    "custom_cookie": "",
    "proxy_enabled": "0",
    "proxy_type": "socks5",  # 'socks5' | 'http'
    "proxy_host": "",
    "proxy_port": "",
    "proxy_username": "",
    "proxy_password": "",
}

# ----------------------------------------------------------------------
# روش‌های اتصال اختصاصی وب‌سایت‌ها (Connection Profiles)
# ----------------------------------------------------------------------
# هر «روش اتصال» یک پروفایل نام‌دار است که می‌تواند به یک یا چند وب‌سایت
# نسبت داده شود. وب‌سایت‌های بدون پروفایل، همان تنظیمات عمومی برنامه
# (پروکسی سراسری / پروکسی سیستم‌عامل / اتصال مستقیم) را استفاده می‌کنند.
CONNECTION_KINDS = ("proxy", "direct")
CONNECTION_PROXY_TYPES = ("socks5", "http", "https")

# کلیدهایی از پروفایل که در فایل پشتیبان یا پاسخ عمومی API نوشته نمی‌شوند
CONNECTION_PROFILE_SECRET_FIELDS = ("proxy_password",)

# نشانه «فیلد ارسال نشده» تا از مقدار None (به معنی «پاک کردن») قابل تفکیک باشد
UNSET = "__unset__"

DEFAULT_CONNECTION_PROFILE: Dict[str, Any] = {
    "kind": "proxy",
    "proxy_type": "socks5",
    "proxy_host": "",
    "proxy_port": "",
    "proxy_username": "",
    "proxy_password": "",
    "verify_ssl": None,  # None = همان تنظیمات عمومی برنامه
    "request_timeout": None,  # None = همان تنظیمات عمومی برنامه
    "fallback_direct": True,
    "notes": "",
    "is_enabled": True,
}


def normalize_connection_port(raw: Any) -> str:
    """پورت پروکسی را به رشته عددی معتبر تبدیل می‌کند (ورودی فارسی هم پذیرفته می‌شود)."""
    value = normalize_digits(str(raw if raw is not None else "")).strip()
    if not value:
        return ""
    if not value.isdigit() or not (1 <= int(value) <= 65535):
        raise ValueError("پورت پروکسی باید عددی بین ۱ تا ۶۵۵۳۵ باشد.")
    return value


def build_profile_proxy_url(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    """ساخت آدرس کامل پروکسی (`scheme://[user:pass@]host:port`) از روی یک پروفایل اتصال."""
    if not profile:
        return None
    if str(profile.get("kind") or "proxy").strip().lower() != "proxy":
        return None

    host = str(profile.get("proxy_host") or "").strip()
    port = str(profile.get("proxy_port") or "").strip()
    if not host or not port:
        return None

    scheme = str(profile.get("proxy_type") or "socks5").strip().lower()
    if scheme not in CONNECTION_PROXY_TYPES:
        scheme = "socks5"

    username = str(profile.get("proxy_username") or "").strip()
    password = str(profile.get("proxy_password") or "")
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
    return f"{scheme}://{auth}{host}:{port}"


def profile_connection_label(profile: Optional[Dict[str, Any]]) -> str:
    """برچسب نمایشی کوتاه و بدون اطلاعات محرک (نام کاربری/رمز) برای یک پروفایل اتصال."""
    if not profile:
        return ""
    kind = str(profile.get("kind") or "proxy").strip().lower()
    if kind == "direct":
        return "اتصال مستقیم (بدون پروکسی)"

    host = str(profile.get("proxy_host") or "").strip()
    port = str(profile.get("proxy_port") or "").strip()
    scheme = str(profile.get("proxy_type") or "socks5").strip().lower()
    if not host or not port:
        return "پروکسی (تنظیمات ناقص)"
    label = f"{scheme}://{host}:{port}"
    if str(profile.get("proxy_username") or "").strip():
        label += " (با نام کاربری)"
    return label


def mask_proxy_url(proxy_url: Optional[str]) -> str:
    """حذف نام کاربری و رمز از آدرس پروکسی برای نمایش ایمن در رابط کاربری و گزارش‌ها."""
    raw = str(proxy_url or "").strip()
    if not raw or "@" not in raw or "://" not in raw:
        return raw
    scheme, _, rest = raw.partition("://")
    _, _, host_part = rest.rpartition("@")
    return f"{scheme}://{host_part}"


def site_connection_label(site: Optional[Dict[str, Any]]) -> str:
    """توضیح روش اتصال مؤثر یک وب‌سایت (پروفایل اختصاصی، پروکسی دستی قدیمی یا حالت عمومی)."""
    if not site:
        return ""
    profile_name = str(site.get("connection_profile_name") or "").strip()
    if profile_name:
        label = str(site.get("connection_profile_label") or "").strip()
        suffix = "" if site.get("connection_profile_enabled", True) else " (غیرفعال)"
        return f"{profile_name} — {label}{suffix}" if label else f"{profile_name}{suffix}"
    manual = mask_proxy_url(site.get("proxy_url"))
    if manual:
        return f"پروکسی دستی — {manual}"
    return "حالت عمومی برنامه"


# ستون‌های نمایشی «روش اتصال» که به هر ردیف وب‌سایت چسبانده می‌شود
# (رمز پروکسی عمداً انتخاب نمی‌شود تا هرگز به مرورگر نرسد)
SITE_CONNECTION_COLUMNS = """
                    cp.name AS connection_profile_name,
                    cp.kind AS connection_profile_kind,
                    cp.is_enabled AS connection_profile_enabled,
                    cp.proxy_type AS connection_proxy_type,
                    cp.proxy_host AS connection_proxy_host,
                    cp.proxy_port AS connection_proxy_port,
                    cp.proxy_username AS connection_proxy_username
"""



def compute_interval_minutes(interval_value: float, interval_unit: str) -> int:
    unit = (interval_unit or "hours").lower().strip()
    if interval_value <= 0:
        raise ValueError("مقدار بازه زمانی باید بزرگ‌تر از صفر باشد.")

    if unit == "minutes":
        minutes = round(interval_value)
    elif unit == "hours":
        minutes = round(interval_value * 60)
    elif unit == "days":
        minutes = round(interval_value * 1440)
    else:
        raise ValueError("واحد زمانی معتبر نیست (فقط minutes، hours یا days مجاز است).")

    if minutes < 1:
        minutes = 1
    if minutes > 60 * 24 * 365:
        raise ValueError("بازه زمانی نمی‌تواند بیشتر از ۳۶۵ روز باشد.")
    return int(minutes)


class DatabaseManager:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        with self._write_lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
        """افزودن ستون جدید به جدول موجود (Migration سبک و بدون حذف داده)."""
        cur = conn.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in cur.fetchall()}
        if column in existing:
            return False
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """هم‌سان‌سازی ساختار دیتابیس نسخه‌های قدیمی‌تر با نسخه فعلی."""
        # پروکسی دستی هر وب‌سایت (ستون قدیمی — همچنان پشتیبانی می‌شود)
        self._ensure_column(conn, "sites", "proxy_url", "TEXT")
        # روش اتصال (پروفایل) اختصاصی هر وب‌سایت
        self._ensure_column(
            conn,
            "sites",
            "connection_profile_id",
            "INTEGER REFERENCES connection_profiles(id) ON DELETE SET NULL",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sites_connection_profile "
            "ON sites(connection_profile_id)"
        )

        # ستون‌های جدید جدول پروفایل‌های اتصال (برای دیتابیس‌های قدیمی‌تر)
        profile_columns = {
            "kind": "TEXT NOT NULL DEFAULT 'proxy'",
            "proxy_type": "TEXT NOT NULL DEFAULT 'socks5'",
            "proxy_host": "TEXT NOT NULL DEFAULT ''",
            "proxy_port": "TEXT NOT NULL DEFAULT ''",
            "proxy_username": "TEXT NOT NULL DEFAULT ''",
            "proxy_password": "TEXT NOT NULL DEFAULT ''",
            "verify_ssl": "TEXT",
            "request_timeout": "TEXT",
            "fallback_direct": "INTEGER NOT NULL DEFAULT 1",
            "notes": "TEXT NOT NULL DEFAULT ''",
            "is_enabled": "INTEGER NOT NULL DEFAULT 1",
            "last_test_at": "TEXT",
            "last_test_ok": "INTEGER",
            "last_test_message": "TEXT",
        }
        for column, ddl in profile_columns.items():
            self._ensure_column(conn, "connection_profiles", column, ddl)

    def init_db(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                -- روش‌های اتصال اختصاصی (پروکسی نام‌دار) که به یک یا چند وب‌سایت نسبت داده می‌شوند.
                -- وب‌سایت‌هایی که پروفایل ندارند از تنظیمات عمومی برنامه استفاده می‌کنند.
                CREATE TABLE IF NOT EXISTS connection_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL DEFAULT 'proxy',          -- 'proxy' | 'direct'
                    proxy_type TEXT NOT NULL DEFAULT 'socks5',   -- 'socks5' | 'http' | 'https'
                    proxy_host TEXT NOT NULL DEFAULT '',
                    proxy_port TEXT NOT NULL DEFAULT '',
                    proxy_username TEXT NOT NULL DEFAULT '',
                    proxy_password TEXT NOT NULL DEFAULT '',
                    verify_ssl TEXT,            -- NULL = همان تنظیمات عمومی برنامه
                    request_timeout TEXT,       -- NULL = همان تنظیمات عمومی برنامه
                    fallback_direct INTEGER NOT NULL DEFAULT 1,
                    notes TEXT NOT NULL DEFAULT '',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    last_test_at TEXT,
                    last_test_ok INTEGER,
                    last_test_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sites (
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
                    updated_at TEXT NOT NULL,
                    proxy_url TEXT,  -- پروکسی دستی قدیمی (سازگاری با نسخه‌های قبل)
                    connection_profile_id INTEGER REFERENCES connection_profiles(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
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
                    fetched_at TEXT NOT NULL,
                    UNIQUE(site_id, post_guid),
                    UNIQUE(site_id, post_url)
                );

                CREATE INDEX IF NOT EXISTS idx_posts_site_seq
                    ON posts(site_id, item_seq DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_published_utc
                    ON posts(published_at_utc DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_published_date
                    ON posts(published_date);

                CREATE TABLE IF NOT EXISTS seen_items (
                    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    post_guid TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (site_id, post_guid)
                );

                CREATE TABLE IF NOT EXISTS fetch_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id INTEGER REFERENCES sites(id) ON DELETE CASCADE,
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

                CREATE INDEX IF NOT EXISTS idx_fetch_logs_created
                    ON fetch_logs(created_at DESC);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE VIEW IF NOT EXISTS vw_posts_export AS
                SELECT
                    p.id AS post_row_id,
                    s.title AS site_title,
                    s.canonical_url AS site_url,
                    s.domain AS site_domain,
                    p.item_seq AS item_seq,
                    p.title AS post_title,
                    p.post_url AS post_url,
                    p.published_jalali_date AS published_jalali_date,
                    p.published_date AS published_gregorian_date,
                    p.published_time AS published_time,
                    p.published_at_utc AS published_at_utc,
                    p.media_type AS media_type,
                    p.views AS views,
                    p.content AS content,
                    p.fetched_at AS fetched_at
                FROM posts p
                JOIN sites s ON s.id = p.site_id
                ORDER BY p.published_at_utc DESC, p.item_seq DESC;
                """
            )
            # Migration: افزودن ستون‌های جدید (پروکسی دستی و پروفایل اتصال) به دیتابیس‌های قدیمی
            self._migrate_schema(conn)

            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> Dict[str, str]:
        with self.connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        merged = dict(DEFAULT_SETTINGS)
        for row in rows:
            merged[row["key"]] = row["value"]
        return merged

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, str]:
        with self.transaction() as conn:
            for key, val in updates.items():
                if key in DEFAULT_SETTINGS and val is not None:
                    conn.execute(
                        """
                        INSERT INTO settings (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (key, str(val)),
                    )
        return self.get_settings()

    # ------------------------------------------------------------------
    # Connection Profiles (روش‌های اتصال اختصاصی وب‌سایت‌ها)
    # ------------------------------------------------------------------
    @staticmethod
    def _profile_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        profile = dict(row)
        profile["kind"] = str(profile.get("kind") or "proxy").lower()
        profile["is_enabled"] = bool(profile.get("is_enabled"))
        profile["fallback_direct"] = bool(profile.get("fallback_direct"))
        profile["proxy_password_set"] = bool(str(profile.get("proxy_password") or ""))
        profile["proxy_url"] = build_profile_proxy_url(profile)
        profile["connection_label"] = profile_connection_label(profile)
        profile["verify_ssl"] = (
            None if profile.get("verify_ssl") in (None, "") else str(profile.get("verify_ssl")) == "1"
        )
        timeout_raw = profile.get("request_timeout")
        try:
            profile["request_timeout"] = int(timeout_raw) if timeout_raw not in (None, "") else None
        except (TypeError, ValueError):
            profile["request_timeout"] = None
        profile["last_test_ok"] = (
            None if profile.get("last_test_ok") is None else bool(profile.get("last_test_ok"))
        )
        return profile

    @staticmethod
    def public_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
        """پاسخ امن API: رمز پروکسی هرگز به مرورگر فرستاده نمی‌شود."""
        out = {k: v for k, v in profile.items() if k not in CONNECTION_PROFILE_SECRET_FIELDS}
        # اگر ورودی از قبل عمومی (بدون رمز) بوده، همان پرچم قبلی حفظ می‌شود
        out["proxy_password_set"] = (
            bool(profile["proxy_password"])
            if "proxy_password" in profile
            else bool(profile.get("proxy_password_set"))
        )
        out["proxy_url"] = mask_proxy_url(profile.get("proxy_url"))
        return out

    def list_connection_profiles(self, *, include_secrets: bool = False) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    cp.*,
                    COUNT(s.id) AS assigned_sites
                FROM connection_profiles cp
                LEFT JOIN sites s ON s.connection_profile_id = cp.id
                GROUP BY cp.id
                ORDER BY cp.id ASC
                """
            ).fetchall()
        profiles = [self._profile_row_to_dict(r) for r in rows]
        return [
            p if include_secrets else self.public_profile(p)
            for p in profiles
        ]

    def get_connection_profile(
        self, profile_id: int, *, include_secrets: bool = True
    ) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM connection_profiles WHERE id = ?", (int(profile_id),)
            ).fetchone()
        if not row:
            return None
        profile = self._profile_row_to_dict(row)
        return profile if include_secrets else self.public_profile(profile)

    def get_connection_profile_by_name(
        self, name: str, *, include_secrets: bool = True
    ) -> Optional[Dict[str, Any]]:
        clean = str(name or "").strip().lower()
        if not clean:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM connection_profiles WHERE LOWER(name) = ?", (clean,)
            ).fetchone()
        return self.get_connection_profile(int(row["id"]), include_secrets=include_secrets) if row else None

    def _validate_profile_fields(
        self,
        *,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        proxy_type: Optional[str] = None,
        proxy_host: Optional[str] = None,
        proxy_port: Optional[Any] = None,
        verify_ssl: Any = UNSET,
        request_timeout: Any = UNSET,
    ) -> Dict[str, Any]:
        clean: Dict[str, Any] = {}

        if name is not None:
            clean_name = str(name).strip()
            if not clean_name:
                raise ValueError("نام روش اتصال نمی‌تواند خالی باشد.")
            if len(clean_name) > 80:
                raise ValueError("نام روش اتصال نمی‌تواند بیشتر از ۸۰ نویسه باشد.")
            clean["name"] = clean_name

        if kind is not None:
            clean_kind = str(kind).strip().lower()
            if clean_kind not in CONNECTION_KINDS:
                raise ValueError("نوع روش اتصال باید «پروکسی» یا «اتصال مستقیم» باشد.")
            clean["kind"] = clean_kind

        if proxy_type is not None:
            clean_type = str(proxy_type).strip().lower()
            if clean_type == "socks":
                clean_type = "socks5"
            if clean_type not in CONNECTION_PROXY_TYPES:
                raise ValueError("نوع پروکسی باید یکی از SOCKS5، HTTP یا HTTPS باشد.")
            clean["proxy_type"] = clean_type

        if proxy_host is not None:
            host = str(proxy_host).strip()
            # حذف پیشوند scheme در صورتی که کاربر آدرس کامل را وارد کرده باشد
            if "://" in host:
                scheme, _, rest = host.partition("://")
                scheme = scheme.strip().lower()
                if scheme in CONNECTION_PROXY_TYPES and "proxy_type" not in clean:
                    clean["proxy_type"] = scheme
                host = rest
            host = host.split("/", 1)[0].strip()
            if "@" in host:  # user:pass@host — جداسازی اطلاعات احراز هویت
                auth, _, host = host.rpartition("@")
                if ":" in auth and "proxy_username" not in clean:
                    user, _, pwd = auth.partition(":")
                    clean["proxy_username"] = user.strip()
                    clean["proxy_password"] = pwd
            if len(host) > 253:
                raise ValueError("آدرس میزبان پروکسی معتبر نیست.")
            clean["proxy_host"] = host

        if proxy_port is not None:
            clean["proxy_port"] = normalize_connection_port(proxy_port)

        if clean.get("kind") == "proxy":
            host_value = clean.get("proxy_host")
            port_value = clean.get("proxy_port")
            if (host_value is not None and not host_value) or (
                port_value is not None and not port_value
            ):
                raise ValueError("برای روش اتصال پروکسی، وارد کردن آدرس میزبان و پورت الزامی است.")

        if verify_ssl != UNSET:
            clean["verify_ssl"] = None if verify_ssl is None else ("1" if verify_ssl else "0")

        if request_timeout != UNSET:
            if request_timeout is None or str(request_timeout).strip() == "":
                clean["request_timeout"] = None
            else:
                try:
                    timeout_value = int(float(str(request_timeout).strip()))
                except (TypeError, ValueError) as exc:
                    raise ValueError("مهلت زمانی باید عددی بین ۵ تا ۱۲۰ ثانیه باشد.") from exc
                if not 5 <= timeout_value <= 120:
                    raise ValueError("مهلت زمانی باید عددی بین ۵ تا ۱۲۰ ثانیه باشد.")
                clean["request_timeout"] = str(timeout_value)

        return clean

    def create_connection_profile(
        self,
        *,
        name: str,
        kind: str = "proxy",
        proxy_type: str = "socks5",
        proxy_host: str = "",
        proxy_port: Any = "",
        proxy_username: str = "",
        proxy_password: str = "",
        verify_ssl: Any = UNSET,
        request_timeout: Any = UNSET,
        fallback_direct: bool = True,
        notes: str = "",
        is_enabled: bool = True,
    ) -> Dict[str, Any]:
        clean = self._validate_profile_fields(
            name=name,
            kind=kind,
            proxy_type=proxy_type,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            verify_ssl=verify_ssl,
            request_timeout=request_timeout,
        )
        resolved_kind = clean["kind"]
        if resolved_kind == "proxy" and (not clean.get("proxy_host") or not clean.get("proxy_port")):
            raise ValueError("برای روش اتصال پروکسی، وارد کردن آدرس میزبان و پورت الزامی است.")

        now_iso = utc_now_iso()
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT id FROM connection_profiles WHERE LOWER(name) = LOWER(?)",
                (clean["name"],),
            ).fetchone()
            if exists:
                raise ValueError("روش اتصالی با این نام قبلاً ثبت شده است.")

            cur = conn.execute(
                """
                INSERT INTO connection_profiles (
                    name, kind, proxy_type, proxy_host, proxy_port,
                    proxy_username, proxy_password, verify_ssl, request_timeout,
                    fallback_direct, notes, is_enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean["name"],
                    resolved_kind,
                    clean.get("proxy_type") or "socks5",
                    clean.get("proxy_host") or "",
                    clean.get("proxy_port") or "",
                    str(proxy_username or "").strip(),
                    str(proxy_password or "") if proxy_password is not None else "",
                    clean.get("verify_ssl"),
                    clean.get("request_timeout"),
                    1 if fallback_direct else 0,
                    str(notes or "").strip()[:500],
                    1 if is_enabled else 0,
                    now_iso,
                    now_iso,
                ),
            )
            profile_id = int(cur.lastrowid)

        profile = self.get_connection_profile(profile_id)
        assert profile is not None
        return profile

    def update_connection_profile(
        self,
        profile_id: int,
        *,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        proxy_type: Optional[str] = None,
        proxy_host: Optional[str] = None,
        proxy_port: Any = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
        verify_ssl: Any = UNSET,
        request_timeout: Any = UNSET,
        fallback_direct: Optional[bool] = None,
        notes: Optional[str] = None,
        is_enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        current = self.get_connection_profile(int(profile_id))
        if not current:
            raise LookupError("روش اتصال مورد نظر یافت نشد.")

        clean = self._validate_profile_fields(
            name=name,
            kind=kind,
            proxy_type=proxy_type,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            verify_ssl=verify_ssl,
            request_timeout=request_timeout,
        )

        new_kind = clean.get("kind", current["kind"])
        new_host = clean.get("proxy_host", current.get("proxy_host") or "")
        new_port = clean.get("proxy_port", current.get("proxy_port") or "")
        if new_kind == "proxy" and (not str(new_host).strip() or not str(new_port).strip()):
            raise ValueError("برای روش اتصال پروکسی، وارد کردن آدرس میزبان و پورت الزامی است.")

        new_name = clean.get("name", current["name"])
        if str(new_name).lower() != str(current["name"]).lower():
            duplicate = self.get_connection_profile_by_name(new_name)
            if duplicate and int(duplicate["id"]) != int(profile_id):
                raise ValueError("روش اتصالی با این نام قبلاً ثبت شده است.")

        updates: Dict[str, Any] = {
            "name": new_name,
            "kind": new_kind,
            "proxy_type": clean.get("proxy_type", current.get("proxy_type") or "socks5"),
            "proxy_host": new_host,
            "proxy_port": new_port,
            "proxy_username": (
                str(proxy_username).strip() if proxy_username is not None
                else str(current.get("proxy_username") or "")
            ),
            "fallback_direct": (
                1 if fallback_direct else 0
            ) if fallback_direct is not None else (1 if current.get("fallback_direct") else 0),
            "notes": str(notes).strip()[:500] if notes is not None else str(current.get("notes") or ""),
            "is_enabled": (
                1 if is_enabled else 0
            ) if is_enabled is not None else (1 if current.get("is_enabled") else 0),
            "updated_at": utc_now_iso(),
        }

        # رمز پروکسی: مقدار «********» یعنی «بدون تغییر» (همان قرارداد بخش تنظیمات عمومی)
        if proxy_password is not None and proxy_password != "********":
            updates["proxy_password"] = str(proxy_password)
        else:
            updates["proxy_password"] = str(current.get("proxy_password") or "")

        updates["verify_ssl"] = (
            clean["verify_ssl"] if "verify_ssl" in clean
            else (
                None if current.get("verify_ssl") is None
                else ("1" if current.get("verify_ssl") else "0")
            )
        )
        updates["request_timeout"] = (
            clean["request_timeout"] if "request_timeout" in clean
            else (
                None if current.get("request_timeout") is None
                else str(current.get("request_timeout"))
            )
        )

        columns = ", ".join(f"{key} = ?" for key in updates)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE connection_profiles SET {columns} WHERE id = ?",
                (*updates.values(), int(profile_id)),
            )

        updated = self.get_connection_profile(int(profile_id))
        assert updated is not None
        return updated

    def delete_connection_profile(self, profile_id: int) -> bool:
        """حذف روش اتصال؛ وب‌سایت‌های وابسته به‌صورت خودکار به حالت عمومی برمی‌گردند."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sites SET connection_profile_id = NULL, updated_at = ? "
                "WHERE connection_profile_id = ?",
                (utc_now_iso(), int(profile_id)),
            )
            cur = conn.execute("DELETE FROM connection_profiles WHERE id = ?", (int(profile_id),))
            return cur.rowcount > 0

    def record_connection_profile_test(
        self, profile_id: int, *, ok: bool, message: str
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE connection_profiles
                SET last_test_at = ?, last_test_ok = ?, last_test_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (utc_now_iso(), 1 if ok else 0, str(message or "")[:500], utc_now_iso(), int(profile_id)),
            )

    def assign_connection_profile(
        self,
        site_ids: Iterable[int],
        profile_id: Optional[int],
        *,
        clear_manual_proxy: bool = True,
    ) -> Dict[str, Any]:
        """
        نسبت‌دادن یک روش اتصال به یک یا چند وب‌سایت.

        `profile_id = None` یعنی بازگرداندن وب‌سایت‌ها به «حالت عمومی برنامه».
        وب‌سایت‌هایی که در این فراخوانی انتخاب نشده‌اند هیچ تغییری نمی‌کنند.
        """
        ids = []
        seen: Set[int] = set()
        for raw_id in site_ids or []:
            try:
                sid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if sid in seen:
                continue
            seen.add(sid)
            ids.append(sid)

        if not ids:
            raise ValueError("هیچ وب‌سایتی برای اعمال روش اتصال انتخاب نشده است.")

        profile = None
        if profile_id is not None:
            profile = self.get_connection_profile(int(profile_id))
            if not profile:
                raise LookupError("روش اتصال انتخاب‌شده یافت نشد.")
            if profile["kind"] == "proxy" and not profile.get("proxy_url"):
                raise ValueError(
                    "این روش اتصال پروکسی، آدرس میزبان یا پورت ندارد؛ ابتدا آن را ویرایش و کامل کنید."
                )

        now_iso = utc_now_iso()
        placeholders = ", ".join("?" for _ in ids)
        with self.transaction() as conn:
            rows = conn.execute(
                f"SELECT id, title FROM sites WHERE id IN ({placeholders})", ids
            ).fetchall()
            found = {int(r["id"]): r["title"] for r in rows}
            missing = [sid for sid in ids if sid not in found]
            if missing:
                raise LookupError(
                    f"وب‌سایت(های) با شناسه {', '.join(str(m) for m in missing)} یافت نشد."
                )

            if clear_manual_proxy and profile is not None:
                conn.execute(
                    f"""
                    UPDATE sites
                    SET connection_profile_id = ?,
                        proxy_url = NULL,
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (int(profile["id"]), now_iso, *ids),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE sites
                    SET connection_profile_id = ?,
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (int(profile["id"]) if profile else None, now_iso, *ids),
                )

        return {
            "profile_id": int(profile["id"]) if profile else None,
            "profile_name": profile["name"] if profile else None,
            "connection_label": profile["connection_label"] if profile else "حالت عمومی برنامه",
            "updated_sites": len(ids),
            "site_ids": ids,
            "site_titles": [found[sid] for sid in ids],
        }

    def get_sites_connection_summary(self) -> Dict[str, Any]:
        """خلاصه وضعیت روش اتصال وب‌سایت‌ها برای نمایش در رابط کاربری."""
        sites = self.list_sites()
        profiles = self.list_connection_profiles()
        assigned = [s for s in sites if s.get("connection_profile_id")]
        manual = [
            s for s in sites
            if not s.get("connection_profile_id") and str(s.get("proxy_url") or "").strip()
        ]
        return {
            "total_sites": len(sites),
            "assigned_sites": len(assigned),
            "manual_proxy_sites": len(manual),
            "general_mode_sites": len(sites) - len(assigned) - len(manual),
            "profiles": len(profiles),
        }

    def build_effective_settings(
        self,
        site: Dict[str, Any],
        settings: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """
        ساخت تنظیمات مؤثر شبکه برای یک وب‌سایت خاص:

        ۱. اگر وب‌سایت «روش اتصال» (پروفایل) داشته باشد → همان پروکسی/اتصال مستقیم استفاده می‌شود
           (به‌همراه بازنویسی اختیاری SSL و مهلت زمانی).
        ۲. در غیر این صورت اگر پروکسی دستی قدیمی (`sites.proxy_url`) تنظیم شده باشد → همان استفاده می‌شود.
        ۳. در غیر این صورت → تنظیمات عمومی برنامه (پروکسی سراسری یا پروکسی سیستم‌عامل) بدون تغییر می‌ماند.
        """
        effective: Dict[str, str] = dict(settings if settings is not None else self.get_settings())

        profile: Optional[Dict[str, Any]] = None
        profile_id = site.get("connection_profile_id") if site else None
        if profile_id:
            profile = self.get_connection_profile(int(profile_id))
            if profile and not profile.get("is_enabled"):
                profile = None  # پروفایل غیرفعال → رفتار همان حالت عمومی

        if profile:
            if profile["kind"] == "direct":
                effective["site_force_direct"] = "1"
                effective.pop("site_proxy_url", None)
            else:
                proxy_url = build_profile_proxy_url(profile)
                if proxy_url:
                    effective["site_proxy_url"] = proxy_url
                    effective["site_force_direct"] = "0"
                    effective["site_proxy_no_fallback"] = "0" if profile.get("fallback_direct") else "1"

            if profile.get("verify_ssl") is not None:
                effective["verify_ssl"] = "1" if profile.get("verify_ssl") else "0"
            if profile.get("request_timeout"):
                effective["request_timeout"] = str(profile["request_timeout"])
            effective["connection_profile_name"] = str(profile.get("name") or "")
            return effective

        manual_proxy = str((site or {}).get("proxy_url") or "").strip()
        if manual_proxy:
            effective["site_proxy_url"] = manual_proxy
            effective["site_force_direct"] = "0"
        return effective

    # ------------------------------------------------------------------
    # Websites (Sites) CRUD
    # ------------------------------------------------------------------
    def create_site(
        self,
        parsed: ParsedSite,
        interval_value: float = 6.0,
        interval_unit: str = "hours",
        initial_limit: int = DEFAULT_INITIAL_LIMIT,
        title: Optional[str] = None,
        is_active: bool = True,
        is_demo: bool = False,
        proxy_url: Optional[str] = None,  # پروکسی دستی (سازگاری با نسخه‌های قبل)
        connection_profile_id: Optional[int] = None,  # روش اتصال اختصاصی وب‌سایت
    ) -> Dict[str, Any]:
        interval_minutes = compute_interval_minutes(interval_value, interval_unit)
        if initial_limit < 1 or initial_limit > 100:
            raise ValueError("تعداد مطالب دریافت اولیه باید بین ۱ تا ۱۰۰ باشد.")

        resolved_profile_id: Optional[int] = None
        if connection_profile_id not in (None, ""):
            try:
                resolved_profile_id = int(connection_profile_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("شناسه روش اتصال معتبر نیست.") from exc
            if not self.get_connection_profile(resolved_profile_id, include_secrets=False):
                raise LookupError("روش اتصال انتخاب‌شده یافت نشد.")

        now_dt = utc_now()
        now_iso = format_iso_utc(now_dt)
        next_iso = format_iso_utc(now_dt + timedelta(minutes=interval_minutes)) if is_active else None
        has_custom_title = bool(title and title.strip())
        final_title = title.strip() if has_custom_title and title else parsed.default_title

        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM sites WHERE site_key = ?",
                (parsed.site_key,),
            ).fetchone()
            if existing:
                raise ValueError("این وب‌سایت قبلاً در لیست برنامه ثبت شده است.")

            cur = conn.execute(
                """
                INSERT INTO sites (
                    site_key, site_input, domain, canonical_url, feed_url,
                    title, custom_title, is_direct_feed, interval_value, interval_unit,
                    interval_minutes, initial_limit, is_active, is_demo, proxy_url,
                    connection_profile_id,
                    next_fetch_at, last_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    parsed.site_key,
                    parsed.site_input,
                    parsed.domain,
                    parsed.canonical_url,
                    parsed.feed_url,
                    final_title,
                    1 if has_custom_title else 0,
                    1 if parsed.is_direct_feed else 0,
                    float(interval_value),
                    interval_unit.lower().strip(),
                    interval_minutes,
                    int(initial_limit),
                    1 if is_active else 0,
                    1 if is_demo else 0,
                    proxy_url,  # پروکسی دستی قدیمی
                    resolved_profile_id,  # روش اتصال اختصاصی
                    next_iso,
                    now_iso,
                    now_iso,
                ),
            )
            site_id = int(cur.lastrowid)

        site = self.get_site(site_id)
        assert site is not None
        return site

    @staticmethod
    def _decorate_site_row(row: sqlite3.Row) -> Dict[str, Any]:
        """افزودن فیلدهای نمایشی «روش اتصال» به ردیف وب‌سایت (بدون افشای رمز پروکسی)."""
        site = dict(row)
        has_profile = bool(site.get("connection_profile_id")) and bool(
            site.get("connection_profile_name")
        )
        site["connection_profile_enabled"] = (
            bool(site.get("connection_profile_enabled")) if has_profile else None
        )
        site["connection_profile_label"] = (
            profile_connection_label(
                {
                    "kind": site.get("connection_profile_kind"),
                    "proxy_type": site.get("connection_proxy_type"),
                    "proxy_host": site.get("connection_proxy_host"),
                    "proxy_port": site.get("connection_proxy_port"),
                    "proxy_username": site.get("connection_proxy_username"),
                }
            )
            if has_profile
            else ""
        )
        if not has_profile:
            site["connection_profile_name"] = None
        site["proxy_url_masked"] = mask_proxy_url(site.get("proxy_url"))
        site["connection_label"] = site_connection_label(site)
        return site

    def get_site(self, site_id: int) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            row = conn.execute(
                f"""
                SELECT
                    s.*,
                    COUNT(p.id) AS posts_count,
                    {SITE_CONNECTION_COLUMNS}
                FROM sites s
                LEFT JOIN posts p ON p.site_id = s.id
                LEFT JOIN connection_profiles cp ON cp.id = s.connection_profile_id
                WHERE s.id = ?
                GROUP BY s.id
                """,
                (site_id,),
            ).fetchone()
        return self._decorate_site_row(row) if row else None

    def get_site_by_key(self, site_key: str) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM sites WHERE site_key = ?", (site_key,)
            ).fetchone()
        if not row:
            return None
        return self.get_site(int(row["id"]))

    def list_sites(self) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    s.*,
                    COUNT(p.id) AS posts_count,
                    {SITE_CONNECTION_COLUMNS}
                FROM sites s
                LEFT JOIN posts p ON p.site_id = s.id
                LEFT JOIN connection_profiles cp ON cp.id = s.connection_profile_id
                GROUP BY s.id
                ORDER BY s.id DESC
                """
            ).fetchall()
        return [self._decorate_site_row(r) for r in rows]

    def update_site(
        self,
        site_id: int,
        *,
        title: Optional[str] = None,
        interval_value: Optional[float] = None,
        interval_unit: Optional[str] = None,
        initial_limit: Optional[int] = None,
        is_active: Optional[bool] = None,
        proxy_url: Optional[str] = None,  # None = بدون تغییر، "" = پاک کردن پروکسی دستی
        connection_profile_id: Any = UNSET,  # UNSET = بدون تغییر، None = حالت عمومی، int = پروفایل
    ) -> Dict[str, Any]:
        current = self.get_site(site_id)
        if not current:
            raise LookupError("وب‌سایت مورد نظر یافت نشد.")

        has_new_title = title is not None and bool(title.strip())
        new_title = title.strip() if has_new_title and title else current["title"]
        new_custom_title = 1 if has_new_title else int(current.get("custom_title") or 0)

        new_val = float(interval_value) if interval_value is not None else float(current["interval_value"])
        new_unit = interval_unit.lower().strip() if interval_unit is not None else current["interval_unit"]
        new_minutes = compute_interval_minutes(new_val, new_unit)

        new_limit = int(initial_limit) if initial_limit is not None else int(current["initial_limit"])
        if new_limit < 1 or new_limit > 100:
            raise ValueError("تعداد مطالب دریافت اولیه باید بین ۱ تا ۱۰۰ باشد.")

        new_active = bool(is_active) if is_active is not None else bool(current["is_active"])
        now_dt = utc_now()
        now_iso = format_iso_utc(now_dt)

        if not new_active:
            next_fetch = None
        else:
            if current["last_fetched_at"]:
                try:
                    base_dt = datetime.fromisoformat(current["last_fetched_at"].replace("Z", "+00:00"))
                    candidate_dt = base_dt + timedelta(minutes=new_minutes)
                    if candidate_dt < now_dt:
                        candidate_dt = now_dt + timedelta(seconds=5)
                    next_fetch = format_iso_utc(candidate_dt)
                except ValueError:
                    next_fetch = format_iso_utc(now_dt + timedelta(minutes=new_minutes))
            else:
                next_fetch = format_iso_utc(now_dt + timedelta(minutes=new_minutes))

        updates: Dict[str, Any] = {
            "title": new_title,
            "custom_title": new_custom_title,
            "interval_value": new_val,
            "interval_unit": new_unit,
            "interval_minutes": new_minutes,
            "initial_limit": new_limit,
            "is_active": 1 if new_active else 0,
            "next_fetch_at": next_fetch,
            "updated_at": now_iso,
        }

        # پروکسی دستی (فیلد قدیمی): None = بدون تغییر، رشته خالی = پاک کردن
        if proxy_url is not None:
            clean_proxy = str(proxy_url).strip()
            updates["proxy_url"] = clean_proxy or None

        # روش اتصال اختصاصی (پروفایل)
        if connection_profile_id is not UNSET:
            if connection_profile_id is None or str(connection_profile_id).strip() == "":
                updates["connection_profile_id"] = None
            else:
                try:
                    pid = int(connection_profile_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("شناسه روش اتصال معتبر نیست.") from exc
                profile = self.get_connection_profile(pid, include_secrets=False)
                if not profile:
                    raise LookupError("روش اتصال انتخاب‌شده یافت نشد.")
                updates["connection_profile_id"] = pid
                # وقتی روش اتصال اختصاصی فعال است، پروکسی دستی قدیمی پاک می‌شود تا تداخل نکند
                updates["proxy_url"] = None

        columns = ", ".join(f"{key} = ?" for key in updates)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE sites SET {columns} WHERE id = ?",
                (*updates.values(), site_id),
            )
            if new_title != current["title"]:
                conn.execute(
                    "UPDATE posts SET site_title = ? WHERE site_id = ?",
                    (new_title, site_id),
                )

        updated = self.get_site(site_id)
        assert updated is not None
        return updated

    def delete_site(self, site_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
            return cur.rowcount > 0

    def reset_site_cursor(self, site_id: int, delete_posts: bool = False) -> Dict[str, Any]:
        current = self.get_site(site_id)
        if not current:
            raise LookupError("وب‌سایت مورد نظر یافت نشد.")
        now_iso = utc_now_iso()
        with self.transaction() as conn:
            if delete_posts:
                conn.execute("DELETE FROM posts WHERE site_id = ?", (site_id,))
            conn.execute("DELETE FROM seen_items WHERE site_id = ?", (site_id,))
            conn.execute(
                """
                UPDATE sites
                SET initial_sync_done = 0,
                    last_item_seq = NULL,
                    last_post_guid = NULL,
                    last_post_url = NULL,
                    last_post_at = NULL,
                    last_status = 'pending',
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso, site_id),
            )
        updated = self.get_site(site_id)
        assert updated is not None
        return updated

    def mark_site_syncing(self, site_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sites SET last_status = 'syncing', updated_at = ? WHERE id = ?",
                (utc_now_iso(), site_id),
            )

    def get_due_sites(self, now_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        ref_iso = now_iso or utc_now_iso()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM sites
                WHERE is_active = 1
                  AND is_demo = 0
                  AND (next_fetch_at IS NULL OR next_fetch_at <= ?)
                ORDER BY COALESCE(next_fetch_at, '') ASC, id ASC
                """,
                (ref_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_seen_guids(self, site_id: int) -> Set[str]:
        with self.connection() as conn:
            seen_rows = conn.execute(
                "SELECT post_guid FROM seen_items WHERE site_id = ?",
                (site_id,),
            ).fetchall()
            post_rows = conn.execute(
                "SELECT post_guid FROM posts WHERE site_id = ?",
                (site_id,),
            ).fetchall()
        out = {str(r["post_guid"]) for r in seen_rows}
        out.update(str(r["post_guid"]) for r in post_rows)
        return out

    # ------------------------------------------------------------------
    # Posts Storage & Atomic Deduplication
    # ------------------------------------------------------------------
    def save_sync_result(
        self,
        *,
        site_id: int,
        resolved_title: Optional[str],
        discovered_feed_url: Optional[str],
        prepared_posts: List[Dict[str, Any]],
        observed_guids: Iterable[str],
        trigger_type: str,
        source_mode: str,
        duration_ms: int,
    ) -> Dict[str, Any]:
        """
        Atomically inserts new posts (skipping duplicates via UNIQUE(site_id, post_guid)
        and UNIQUE(site_id, post_url)), records all observed page GUIDs into `seen_items`,
        advances `last_item_seq` / `last_post_guid`, schedules `next_fetch_at`,
        and writes a `fetch_logs` entry.
        """
        now_dt = utc_now()
        now_iso = format_iso_utc(now_dt)

        with self.transaction() as conn:
            site_row = conn.execute(
                "SELECT * FROM sites WHERE id = ?", (site_id,)
            ).fetchone()
            if not site_row:
                raise LookupError("وب‌سایت یافت نشد.")

            prev_last_seq: Optional[int] = site_row["last_item_seq"]
            max_seq_row = conn.execute(
                "SELECT MAX(item_seq) AS m FROM posts WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            next_seq = int(max_seq_row["m"] or prev_last_seq or 0)

            if site_row["custom_title"]:
                site_title = site_row["title"]
            else:
                site_title = (resolved_title or site_row["title"] or "").strip() or site_row["title"]

            site_domain = site_row["domain"]
            feed_url = discovered_feed_url or site_row["feed_url"]

            inserted_count = 0
            latest_seq = prev_last_seq
            latest_guid = site_row["last_post_guid"]
            latest_post_url = site_row["last_post_url"]
            latest_post_at = site_row["last_post_at"]

            for post in prepared_posts:
                candidate_seq = next_seq + 1
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO posts (
                        site_id, site_domain, site_title, item_seq, post_guid,
                        post_url, title, published_date, published_jalali_date,
                        published_time, published_at_utc, content, media_type,
                        media_url, views, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        site_id,
                        site_domain,
                        site_title,
                        candidate_seq,
                        post["post_guid"],
                        post["post_url"],
                        post["title"],
                        post["published_date"],
                        post["published_jalali_date"],
                        post["published_time"],
                        post["published_at_utc"],
                        post["content"],
                        post.get("media_type") or "text",
                        post.get("media_url"),
                        post.get("views"),
                        now_iso,
                    ),
                )
                if cur.rowcount > 0:
                    next_seq = candidate_seq
                    inserted_count += 1
                    latest_seq = next_seq
                    latest_guid = post["post_guid"]
                    latest_post_url = post["post_url"]
                    latest_post_at = post["published_at_utc"]

            # Record all observed item GUIDs in `seen_items` so older items on the page
            # that exceeded `initial_limit` on the first run are never re-imported later.
            guid_rows = [(site_id, g, now_iso) for g in observed_guids if g]
            for post in prepared_posts:
                if post.get("post_guid"):
                    guid_rows.append((site_id, post["post_guid"], now_iso))
            if guid_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO seen_items (site_id, post_guid, first_seen_at) VALUES (?, ?, ?)",
                    guid_rows,
                )

            if site_title != site_row["title"]:
                conn.execute(
                    "UPDATE posts SET site_title = ? WHERE site_id = ?",
                    (site_title, site_id),
                )

            interval_minutes = int(site_row["interval_minutes"])
            next_fetch_at = (
                format_iso_utc(now_dt + timedelta(minutes=interval_minutes))
                if site_row["is_active"]
                else None
            )

            if latest_seq is None:
                latest_seq = 0

            conn.execute(
                """
                UPDATE sites
                SET title = ?,
                    feed_url = ?,
                    initial_sync_done = 1,
                    last_item_seq = ?,
                    last_post_guid = ?,
                    last_post_url = ?,
                    last_post_at = ?,
                    last_fetched_at = ?,
                    next_fetch_at = ?,
                    last_status = 'ok',
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    site_title,
                    feed_url,
                    latest_seq,
                    latest_guid,
                    latest_post_url,
                    latest_post_at,
                    now_iso,
                    next_fetch_at,
                    now_iso,
                    site_id,
                ),
            )

            if inserted_count > 0:
                log_msg = f"{inserted_count} مطلب جدید از وب‌سایت دریافت و ذخیره شد."
            else:
                log_msg = "بررسی انجام شد؛ مطلب جدیدی نسبت به آخرین پایش منتشر نشده بود."

            conn.execute(
                """
                INSERT INTO fetch_logs (
                    site_id, site_title, trigger_type, source_mode,
                    fetched_count, new_saved_count, previous_last_item_seq,
                    new_last_item_seq, status, message, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?)
                """,
                (
                    site_id,
                    site_title,
                    trigger_type,
                    source_mode,
                    len(prepared_posts),
                    inserted_count,
                    prev_last_seq,
                    latest_seq,
                    log_msg,
                    int(duration_ms),
                    now_iso,
                ),
            )

        return {
            "site_id": site_id,
            "site_title": site_title,
            "fetched_count": len(prepared_posts),
            "new_saved_count": inserted_count,
            "previous_last_item_seq": prev_last_seq,
            "new_last_item_seq": latest_seq,
            "status": "success",
            "message": log_msg,
        }

    def save_sync_error(
        self,
        *,
        site_id: int,
        error_message: str,
        trigger_type: str,
        source_mode: str,
        duration_ms: int,
    ) -> Dict[str, Any]:
        now_dt = utc_now()
        now_iso = format_iso_utc(now_dt)

        with self.transaction() as conn:
            site_row = conn.execute(
                "SELECT * FROM sites WHERE id = ?", (site_id,)
            ).fetchone()
            if not site_row:
                raise LookupError("وب‌سایت یافت نشد.")

            retry_minutes = min(int(site_row["interval_minutes"]), 15)
            next_fetch_at = (
                format_iso_utc(now_dt + timedelta(minutes=max(1, retry_minutes)))
                if site_row["is_active"]
                else None
            )

            conn.execute(
                """
                UPDATE sites
                SET last_fetched_at = ?,
                    next_fetch_at = ?,
                    last_status = 'error',
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso, next_fetch_at, error_message, now_iso, site_id),
            )

            conn.execute(
                """
                INSERT INTO fetch_logs (
                    site_id, site_title, trigger_type, source_mode,
                    fetched_count, new_saved_count, previous_last_item_seq,
                    new_last_item_seq, status, message, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, 'error', ?, ?, ?)
                """,
                (
                    site_id,
                    site_row["title"],
                    trigger_type,
                    source_mode,
                    site_row["last_item_seq"],
                    site_row["last_item_seq"],
                    error_message,
                    int(duration_ms),
                    now_iso,
                ),
            )

        return {
            "site_id": site_id,
            "site_title": site_row["title"],
            "fetched_count": 0,
            "new_saved_count": 0,
            "previous_last_item_seq": site_row["last_item_seq"],
            "new_last_item_seq": site_row["last_item_seq"],
            "status": "error",
            "message": error_message,
        }

    # ------------------------------------------------------------------
    # Querying Posts & Logs
    # ------------------------------------------------------------------
    def _build_post_filters(
        self,
        *,
        site_id: Optional[int] = None,
        search: Optional[str] = None,
        date_from_greg: Optional[str] = None,
        date_to_greg: Optional[str] = None,
        media_type: Optional[str] = None,
        post_ids: Optional[List[int]] = None,
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []

        if post_ids is not None:
            clean_ids = [int(pid) for pid in post_ids]
            if not clean_ids:
                return "WHERE 0 = 1", []
            placeholders = ",".join("?" for _ in clean_ids)
            clauses.append(f"p.id IN ({placeholders})")
            params.extend(clean_ids)

        if site_id is not None:
            clauses.append("p.site_id = ?")
            params.append(int(site_id))

        if search and search.strip():
            q = f"%{search.strip()}%"
            clauses.append(
                "(p.title LIKE ? OR p.content LIKE ? OR p.site_title LIKE ? OR p.post_url LIKE ?)"
            )
            params.extend([q, q, q, q])

        if date_from_greg:
            clauses.append("p.published_date >= ?")
            params.append(date_from_greg)

        if date_to_greg:
            clauses.append("p.published_date <= ?")
            params.append(date_to_greg)

        if media_type and media_type.strip() and media_type != "all":
            clauses.append("p.media_type = ?")
            params.append(media_type.strip())

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def list_posts(
        self,
        *,
        site_id: Optional[int] = None,
        search: Optional[str] = None,
        date_from_greg: Optional[str] = None,
        date_to_greg: Optional[str] = None,
        media_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size

        where_sql, params = self._build_post_filters(
            site_id=site_id,
            search=search,
            date_from_greg=date_from_greg,
            date_to_greg=date_to_greg,
            media_type=media_type,
        )

        with self.connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM posts p {where_sql}",
                params,
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"""
                SELECT
                    p.*,
                    s.canonical_url AS site_url
                FROM posts p
                JOIN sites s ON s.id = p.site_id
                {where_sql}
                ORDER BY p.published_at_utc DESC, p.item_seq DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

        total_pages = max(1, (total + page_size - 1) // page_size)
        return {
            "items": [dict(r) for r in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def get_posts_for_export(
        self,
        *,
        site_id: Optional[int] = None,
        search: Optional[str] = None,
        date_from_greg: Optional[str] = None,
        date_to_greg: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where_sql, params = self._build_post_filters(
            site_id=site_id,
            search=search,
            date_from_greg=date_from_greg,
            date_to_greg=date_to_greg,
            media_type=media_type,
        )
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    p.*,
                    s.canonical_url AS site_url
                FROM posts p
                JOIN sites s ON s.id = p.site_id
                {where_sql}
                ORDER BY p.published_at_utc DESC, p.item_seq DESC
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Bulk deletion / purge (archive maintenance)
    # ------------------------------------------------------------------
    def count_posts_for_scope(
        self,
        *,
        site_id: Optional[int] = None,
        date_from_greg: Optional[str] = None,
        date_to_greg: Optional[str] = None,
        media_type: Optional[str] = None,
        search: Optional[str] = None,
        post_ids: Optional[List[int]] = None,
    ) -> int:
        if post_ids is not None and not post_ids:
            return 0
        where_sql, params = self._build_post_filters(
            site_id=site_id,
            search=search,
            date_from_greg=date_from_greg,
            date_to_greg=date_to_greg,
            media_type=media_type,
            post_ids=post_ids,
        )
        with self.connection() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS cnt FROM posts p {where_sql}", params).fetchone()
        return int(row["cnt"] or 0)

    def delete_posts_by_scope(
        self,
        *,
        site_id: Optional[int] = None,
        date_from_greg: Optional[str] = None,
        date_to_greg: Optional[str] = None,
        media_type: Optional[str] = None,
        search: Optional[str] = None,
        post_ids: Optional[List[int]] = None,
        reset_site_cursors: bool = True,
    ) -> Dict[str, Any]:
        """
        Deletes every post matching the given scope in one atomic transaction.
        When `reset_site_cursors` is True, `seen_items` and cursor markers of affected
        websites are cleared so the next sync starts fresh (latest `initial_limit` posts).
        When False, `seen_items` stays intact so deleted posts are NOT re-downloaded.
        """
        if post_ids is not None and not post_ids:
            return {
                "deleted_posts": 0,
                "affected_sites": [],
                "sites_reset": 0,
                "requested_ids": 0,
            }

        where_sql, params = self._build_post_filters(
            site_id=site_id,
            search=search,
            date_from_greg=date_from_greg,
            date_to_greg=date_to_greg,
            media_type=media_type,
            post_ids=post_ids,
        )
        now_iso = utc_now_iso()

        with self.transaction() as conn:
            affected_rows = conn.execute(
                f"SELECT DISTINCT p.site_id AS sid FROM posts p {where_sql}",
                params,
            ).fetchall()
            affected_ids = [int(row["sid"]) for row in affected_rows]

            deleted = conn.execute(
                f"DELETE FROM posts WHERE id IN (SELECT p.id FROM posts p {where_sql})",
                params,
            ).rowcount

            cursors_reset = 0
            if reset_site_cursors and affected_ids:
                placeholders = ",".join("?" for _ in affected_ids)
                conn.execute(
                    f"DELETE FROM seen_items WHERE site_id IN ({placeholders})",
                    affected_ids,
                )
                cursors_reset = conn.execute(
                    f"""
                    UPDATE sites
                    SET initial_sync_done = 0,
                        last_item_seq = NULL,
                        last_post_guid = NULL,
                        last_post_url = NULL,
                        last_post_at = NULL,
                        last_status = 'pending',
                        last_error = NULL,
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now_iso, *affected_ids],
                ).rowcount

        return {
            "deleted_posts": int(deleted),
            "affected_sites": affected_ids,
            "sites_reset": int(cursors_reset),
            "requested_ids": len(post_ids) if post_ids else 0,
        }

    def delete_all_posts(self, reset_site_cursors: bool = True) -> Dict[str, Any]:
        return self.delete_posts_by_scope(reset_site_cursors=reset_site_cursors)

    def purge_everything(self, *, keep_sites: bool = True) -> Dict[str, Any]:
        """
        Wipes the archive in one atomic transaction.
          keep_sites=True  -> posts + logs + seen_items removed, websites kept with reset cursors.
          keep_sites=False -> posts, logs, seen_items AND websites removed.
        User settings (proxy / timezone / headers) are preserved.
        """
        now_iso = utc_now_iso()
        with self.transaction() as conn:
            deleted_posts = conn.execute("DELETE FROM posts").rowcount
            deleted_logs = conn.execute("DELETE FROM fetch_logs").rowcount
            conn.execute("DELETE FROM seen_items")
            deleted_sites = 0
            if keep_sites:
                conn.execute(
                    """
                    UPDATE sites
                    SET initial_sync_done = 0,
                        last_item_seq = NULL,
                        last_post_guid = NULL,
                        last_post_url = NULL,
                        last_post_at = NULL,
                        last_status = 'pending',
                        last_error = NULL,
                        updated_at = ?
                    """,
                    (now_iso,),
                )
            else:
                deleted_sites = conn.execute("DELETE FROM sites").rowcount
        return {
            "deleted_posts": int(deleted_posts),
            "deleted_logs": int(deleted_logs),
            "deleted_sites": int(deleted_sites),
            "sites_kept": bool(keep_sites),
        }

    # ------------------------------------------------------------------
    # Backup / Restore (portable JSON snapshot of sites + settings)
    # ------------------------------------------------------------------
    def get_backup_settings(self, *, include_secrets: bool = False) -> Dict[str, str]:
        settings = self.get_settings()
        snapshot: Dict[str, str] = {}
        for key, value in settings.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if not include_secrets and key in BACKUP_SECRET_SETTING_KEYS:
                continue
            snapshot[key] = str(value or "")
        return snapshot

    def create_backup_payload(
        self,
        *,
        include_settings: bool = True,
        include_secrets: bool = False,
        only_active: bool = False,
        site_ids: Optional[List[int]] = None,
        note: Optional[str] = None,
        app_version: str = "1.0.0",
    ) -> Dict[str, Any]:
        selected = set(int(sid) for sid in site_ids) if site_ids else None

        sites_payload: List[Dict[str, Any]] = []
        for s in self.list_sites():
            if selected is not None and int(s["id"]) not in selected:
                continue
            if only_active and not s.get("is_active"):
                continue
            sites_payload.append(
                {
                    "site_input": s.get("site_input") or s.get("canonical_url"),
                    "site_key": s.get("site_key"),
                    "domain": s.get("domain"),
                    "canonical_url": s.get("canonical_url"),
                    "feed_url": s.get("feed_url"),
                    "title": s.get("title"),
                    "custom_title": bool(s.get("custom_title")),
                    "interval_value": s.get("interval_value"),
                    "interval_unit": s.get("interval_unit"),
                    "initial_limit": s.get("initial_limit"),
                    "is_active": bool(s.get("is_active")),
                    "is_demo": bool(s.get("is_demo")),
                    "initial_sync_done": bool(s.get("initial_sync_done")),
                    "last_item_seq": s.get("last_item_seq"),
                    "last_post_guid": s.get("last_post_guid"),
                    "last_status": s.get("last_status"),
                    "created_at": s.get("created_at"),
                    # روش اتصال اختصاصی وب‌سایت (با نام پروفایل تا در سیستم مقصد قابل بازسازی باشد)
                    "connection_profile": s.get("connection_profile_name"),
                    "proxy_url": mask_proxy_url(s.get("proxy_url")) or None,
                }
            )

        # روش‌های اتصال (پروفایل‌های پروکسی) — بدون رمز مگر آنکه کاربر صراحتاً بخواهد
        profiles_payload: List[Dict[str, Any]] = []
        for profile in self.list_connection_profiles(include_secrets=True):
            entry = {
                "name": profile.get("name"),
                "kind": profile.get("kind"),
                "proxy_type": profile.get("proxy_type"),
                "proxy_host": profile.get("proxy_host"),
                "proxy_port": profile.get("proxy_port"),
                "proxy_username": profile.get("proxy_username"),
                "verify_ssl": profile.get("verify_ssl"),
                "request_timeout": profile.get("request_timeout"),
                "fallback_direct": bool(profile.get("fallback_direct")),
                "notes": profile.get("notes"),
                "is_enabled": bool(profile.get("is_enabled")),
            }
            if include_secrets:
                entry["proxy_password"] = profile.get("proxy_password") or ""
            profiles_payload.append(entry)

        payload: Dict[str, Any] = {
            "format": BACKUP_FORMAT_NAME,
            "format_version": BACKUP_FORMAT_VERSION,
            "app_version": app_version,
            "created_at": utc_now_iso(),
            "note": (note or "").strip(),
            "counts": {
                "sites": len(sites_payload),
                "settings": 0,
                "connection_profiles": len(profiles_payload),
            },
            "sites": sites_payload,
            "settings": {},
            "connection_profiles": profiles_payload,
        }

        if include_settings:
            backup_settings = self.get_backup_settings(include_secrets=include_secrets)
            payload["settings"] = backup_settings
            payload["counts"]["settings"] = len(backup_settings)

        payload["secrets_included"] = bool(include_secrets)
        return payload

    @staticmethod
    def validate_backup_payload(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("فایل پشتیبان معتبر نیست: ساختار JSON باید یک شیء باشد.")

        if data.get("format") != BACKUP_FORMAT_NAME:
            raise ValueError(
                "این فایل، پشتیبان این برنامه نیست (شناسه format پیدا نشد یا اشتباه است)."
            )

        version = int(data.get("format_version") or 0)
        if version < 1 or version > BACKUP_FORMAT_VERSION:
            raise ValueError(
                f"نسخه فایل پشتیبان ({version}) توسط این نسخه از برنامه پشتیبانی نمی‌شود."
            )

        sites = data.get("sites")
        if sites is None:
            sites = []
        if not isinstance(sites, list):
            raise ValueError("بخش «وب‌سایت‌ها» در فایل پشتیبان معتبر نیست.")

        settings = data.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError("بخش «تنظیمات» در فایل پشتیبان معتبر نیست.")

        profiles = data.get("connection_profiles")
        if profiles is None:
            profiles = []
        if not isinstance(profiles, list):
            raise ValueError("بخش «روش‌های اتصال» در فایل پشتیبان معتبر نیست.")

        clean_sites: List[Dict[str, Any]] = []
        for index, raw in enumerate(sites, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"وب‌سایت شماره {index} در فایل پشتیبان معتبر نیست.")
            target = str(raw.get("site_input") or raw.get("canonical_url") or "").strip()
            if not target:
                raise ValueError(f"وب‌سایت شماره {index} آدرس اینترنتی ندارد.")
            clean_sites.append({**raw, "site_input": target})

        clean_profiles: List[Dict[str, Any]] = []
        for index, raw in enumerate(profiles, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"روش اتصال شماره {index} در فایل پشتیبان معتبر نیست.")
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError(f"روش اتصال شماره {index} نام ندارد.")
            clean_profiles.append({**raw, "name": name})

        return {
            "format": BACKUP_FORMAT_NAME,
            "format_version": version,
            "created_at": data.get("created_at"),
            "note": data.get("note") or "",
            "sites": clean_sites,
            "settings": settings,
            "connection_profiles": clean_profiles,
        }

    def restore_from_backup(
        self,
        data: Any,
        *,
        mode: str = "merge",
        restore_settings: bool = True,
        overwrite_settings: bool = False,
    ) -> Dict[str, Any]:
        from app.utils.site_parser import parse_site_input

        if mode not in ("merge", "replace"):
            raise ValueError("حالت بازیابی باید merge یا replace باشد.")

        payload = self.validate_backup_payload(data)
        report: Dict[str, Any] = {
            "mode": mode,
            "added_sites": 0,
            "updated_sites": 0,
            "skipped_sites": 0,
            "removed_sites": 0,
            "restored_settings": 0,
            "restored_profiles": 0,
            "assigned_profiles": 0,
            "cursor_restored": 0,
            "errors": [],
            "warnings": [],
        }

        if mode == "replace":
            report["removed_sites"] = int(self.clear_all_sites())

        # بازگردانی «روش‌های اتصال اختصاصی» (پروفایل‌های پروکسی) پیش از وب‌سایت‌ها
        profile_ids_by_name: Dict[str, int] = {}
        for raw_profile in payload.get("connection_profiles") or []:
            profile_name = str(raw_profile.get("name") or "").strip()
            if not profile_name:
                continue

            has_secret = bool(str(raw_profile.get("proxy_password") or ""))
            existing_profile = self.get_connection_profile_by_name(profile_name)
            fields: Dict[str, Any] = {
                "kind": str(raw_profile.get("kind") or "proxy"),
                "proxy_type": str(raw_profile.get("proxy_type") or "socks5"),
                "proxy_host": str(raw_profile.get("proxy_host") or ""),
                "proxy_port": raw_profile.get("proxy_port") or "",
                "proxy_username": str(raw_profile.get("proxy_username") or ""),
                # اگر رمز در فایل نبود، رمز فعلی دست‌نخورده می‌ماند (قرارداد «********»)
                "proxy_password": str(raw_profile.get("proxy_password") or "")
                if has_secret or not existing_profile
                else "********",
                "verify_ssl": raw_profile.get("verify_ssl")
                if "verify_ssl" in raw_profile
                else UNSET,
                "request_timeout": raw_profile.get("request_timeout")
                if "request_timeout" in raw_profile
                else UNSET,
                "fallback_direct": bool(raw_profile.get("fallback_direct", True)),
                "notes": str(raw_profile.get("notes") or ""),
                "is_enabled": bool(raw_profile.get("is_enabled", True)),
            }

            try:
                if existing_profile:
                    saved_profile = self.update_connection_profile(
                        int(existing_profile["id"]), name=profile_name, **fields
                    )
                else:
                    saved_profile = self.create_connection_profile(name=profile_name, **fields)
                    report["restored_profiles"] += 1
                profile_ids_by_name[profile_name.lower()] = int(saved_profile["id"])
            except (ValueError, LookupError) as exc:
                report["errors"].append(f"روش اتصال «{profile_name}»: {exc}")

        for raw in payload["sites"]:
            try:
                parsed = parse_site_input(raw["site_input"])
            except ValueError as exc:
                report["skipped_sites"] += 1
                report["errors"].append(f"{raw['site_input']}: {exc}")
                continue

            interval_value = raw.get("interval_value")
            interval_unit = str(raw.get("interval_unit") or "hours").lower().strip()
            if interval_unit not in ("minutes", "hours", "days"):
                interval_unit = "hours"
            try:
                interval_value = float(interval_value) if interval_value is not None else 6.0
                if interval_value <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                interval_value = 6.0

            initial_limit = raw.get("initial_limit")
            try:
                initial_limit = int(initial_limit) if initial_limit is not None else DEFAULT_INITIAL_LIMIT
                if not 1 <= initial_limit <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                initial_limit = DEFAULT_INITIAL_LIMIT

            # <--- جدید: پشتیبانی از پروکسی اختصاصی در بک‌آپ (اختیاری)
            proxy_url = raw.get("proxy_url")

            existing = self.get_site_by_key(parsed.site_key)
            title = (raw.get("title") or "").strip() or None

            if existing:
                site_id = int(existing["id"])
                self.update_site(
                    site_id,
                    title=title or existing["title"],
                    interval_value=interval_value,
                    interval_unit=interval_unit,
                    initial_limit=initial_limit,
                    is_active=bool(raw.get("is_active", True)),
                    proxy_url=proxy_url,
                )
                report["updated_sites"] += 1
            else:
                try:
                    created = self.create_site(
                        parsed=parsed,
                        interval_value=interval_value,
                        interval_unit=interval_unit,
                        initial_limit=initial_limit,
                        title=title,
                        is_active=bool(raw.get("is_active", True)),
                        is_demo=bool(raw.get("is_demo", False)),
                        proxy_url=proxy_url,
                    )
                except ValueError as exc:
                    report["skipped_sites"] += 1
                    report["errors"].append(f"{raw['site_input']}: {exc}")
                    continue
                site_id = int(created["id"])
                report["added_sites"] += 1

            if raw.get("feed_url"):
                with self.transaction() as conn:
                    conn.execute(
                        "UPDATE sites SET feed_url = ? WHERE id = ? AND feed_url IS NULL",
                        (str(raw["feed_url"]).strip(), site_id),
                    )

            # بازگردانی نسبت‌دادن «روش اتصال اختصاصی» به این وب‌سایت
            wanted_profile = str(raw.get("connection_profile") or "").strip()
            if wanted_profile:
                restored_profile_id = profile_ids_by_name.get(wanted_profile.lower())
                if restored_profile_id:
                    try:
                        self.update_site(site_id, connection_profile_id=restored_profile_id)
                        report["assigned_profiles"] += 1
                    except (ValueError, LookupError) as exc:
                        report["warnings"].append(
                            f"{parsed.site_key}: روش اتصال «{wanted_profile}» اعمال نشد ({exc})"
                        )
                else:
                    report["warnings"].append(
                        f"{parsed.site_key}: روش اتصال «{wanted_profile}» در فایل پشتیبان معتبر نبود "
                        "و وب‌سایت در حالت عمومی باقی ماند."
                    )

            # بازگردانی نشانگر آخرین مطلب فقط اگر مطالب آن وب‌سایت در آرشیو موجود باشد
            last_seq = raw.get("last_item_seq")
            last_guid = raw.get("last_post_guid")
            if last_seq is not None or last_guid:
                has_posts = self.count_posts_for_scope(site_id=site_id) > 0
                if has_posts:
                    with self.transaction() as conn:
                        conn.execute(
                            """
                            UPDATE sites
                            SET initial_sync_done = 1,
                                last_item_seq = ?,
                                last_post_guid = ?,
                                last_status = 'ok',
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                int(last_seq) if last_seq is not None else None,
                                str(last_guid) if last_guid else None,
                                utc_now_iso(),
                                site_id,
                            ),
                        )
                    report["cursor_restored"] += 1
                else:
                    report["warnings"].append(
                        f"{parsed.site_key}: نشانگر آخرین مطلب بازگردانی نشد چون هیچ مطلبی "
                        "از این وب‌سایت در آرشیو نیست (دریافت بعدی از ابتدا انجام می‌شود)."
                    )

        if restore_settings and payload["settings"]:
            incoming = payload["settings"]
            allowed = {k: str(v) for k, v in incoming.items() if k in DEFAULT_SETTINGS and v is not None}

            if not allowed:
                pass
            elif overwrite_settings:
                self.update_settings(allowed)
                report["restored_settings"] = len(allowed)
            else:
                current = self.get_settings()
                fillable = {
                    key: value
                    for key, value in allowed.items()
                    if key not in BACKUP_SECRET_SETTING_KEYS
                    and not str(current.get(key, "")).strip()
                    and str(value).strip()
                }
                if fillable:
                    self.update_settings(fillable)
                report["restored_settings"] = len(fillable)

        return report

    def clear_all_sites(self) -> int:
        with self.transaction() as conn:
            removed = conn.execute("DELETE FROM sites").rowcount
        return int(removed)

    def delete_post(self, post_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            return cur.rowcount > 0

    def list_fetch_logs(self, limit: int = 100, site_id: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self.connection() as conn:
            if site_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM fetch_logs
                    WHERE site_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(site_id), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fetch_logs ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def clear_fetch_logs(self) -> int:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM fetch_logs")
            return cur.rowcount

    # ------------------------------------------------------------------
    # Dashboard Stats & SQLite Backup Export
    # ------------------------------------------------------------------
    def get_dashboard_stats(self) -> Dict[str, Any]:
        with self.connection() as conn:
            site_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_sites,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_sites,
                    SUM(CASE WHEN is_demo = 1 THEN 1 ELSE 0 END) AS demo_sites,
                    MIN(CASE WHEN is_active = 1 THEN next_fetch_at ELSE NULL END) AS nearest_next_fetch
                FROM sites
                """
            ).fetchone()

            post_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_posts,
                    MAX(published_at_utc) AS latest_post_utc,
                    MAX(fetched_at) AS latest_fetched_utc
                FROM posts
                """
            ).fetchone()

        db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "total_sites": int(site_stats["total_sites"] or 0),
            "active_sites": int(site_stats["active_sites"] or 0),
            "demo_sites": int(site_stats["demo_sites"] or 0),
            "nearest_next_fetch": site_stats["nearest_next_fetch"],
            "total_posts": int(post_stats["total_posts"] or 0),
            "latest_post_utc": post_stats["latest_post_utc"],
            "latest_fetched_utc": post_stats["latest_fetched_utc"],
            "db_size_bytes": db_size_bytes,
        }

    def backup_sqlite_to_file(self, target_path: Path) -> Path:
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()

        with self._write_lock:
            src_conn = self._connect()
            dst_conn = sqlite3.connect(str(target_path))
            try:
                src_conn.backup(dst_conn)
                dst_conn.execute("PRAGMA journal_mode = DELETE;")
                dst_conn.commit()
            finally:
                dst_conn.close()
                src_conn.close()
        return target_path

    # ------------------------------------------------------------------
    # Demo Data Helper (for offline testing of UI, filters, Excel & SQLite exports)
    # ------------------------------------------------------------------
    def seed_demo_data(self, tz_name: str = DEFAULT_TIMEZONE) -> Dict[str, int]:
        from app.utils.site_parser import parse_site_input

        demo_specs = [
            {
                "input": "https://tech-news-demo.example.com",
                "title": "رسانه فناوری و هوش مصنوعی (نمونه آزمایشی)",
                "interval_value": 4,
                "interval_unit": "hours",
                "posts": [
                    (
                        "معرفی نسل جدید مدل‌های زبانی سبک برای اجرا روی سرورهای محلی",
                        "نسل تازه مدل‌های هوش مصنوعی متن‌باز امکان پردازش دقیق متون فارسی را با مصرف رم کمتر از ۸ گیگابایت روی سیستم‌های شخصی فراهم می‌کند.",
                        "photo",
                        14200,
                    ),
                    (
                        "راهنمای بهینه‌سازی پایگاه داده SQLite در حالت WAL برای سامانه‌های آرشیو",
                        "با فعال‌سازی حالت Write-Ahead Logging در SQLite، خواندن و نوشتن هم‌زمان هزاران رکورد بدون قفل شدن فایل دیتابیس انجام می‌شود.",
                        "text",
                        9850,
                    ),
                    (
                        "انتشار نسخه جدید پایتون با بهبود چشمگیر سرعت در خزشگرهای وب ناهمگام",
                        "قابلیت‌های جدید AsyncIO در نسخه تازه پایتون سرعت دریافت هم‌زمان صفحات وب و فیدهای خبری را تا ۴۰ درصد افزایش داده است.",
                        "document",
                        18400,
                    ),
                    (
                        "مقایسه استانداردهای RSS، Atom و JSON Feed در وب‌سایت‌های خبری مدرن",
                        "بررسی ساختارهای استاندارد انتشار محتوا در وب و روش‌های تشخیص خودکار فید در سیستم‌های مدیریت محتوا.",
                        "text",
                        7600,
                    ),
                    (
                        "پنج نکته کلیدی امنیت شبکه هنگام پایش خودکار وب‌سایت‌ها",
                        "مدیریت صحیح گواهی‌های SSL، تنظیم مهلت زمانی اتصال و استفاده امن از پروکسی‌های SOCKS5 و HTTP.",
                        "photo",
                        11300,
                    ),
                    (
                        "ویدیوی آموزشی ساخت گزارش‌های تحلیلی و خروجی خودکار Excel راست‌به‌چپ",
                        "نحوه دسته‌بندی خودکار اخبار بر اساس تاریخ شمسی و میلادی و آماده‌سازی فایل‌های اکسل مدیریتی.",
                        "video",
                        21500,
                    ),
                    (
                        "گزارش هفتگی تحولات زیرساخت‌های ابری و ابزارهای متن‌باز",
                        "مروری بر مهم‌ترین پروژه‌های نرم‌افزاری معرفی‌شده در هفته گذشته برای تیم‌های مهندسی داده.",
                        "text",
                        8900,
                    ),
                    (
                        "روش‌های استخراج متن تمیز مقالات از صفحات HTML پیچیده",
                        "حذف خودکار منوها، تبلیغات و بخش‌های جانبی صفحه برای ذخیره‌سازی خالص متن اصلی خبر در آرشیو.",
                        "text",
                        6400,
                    ),
                    (
                        "ابزارهای پیشنهادی برای زمان‌بندی خودکار وظایف در بک‌اندهای پایتونی",
                        "بررسی کتابخانه APScheduler برای اجرای پایدار پایش‌های ساعتی و روزانه بدون نیاز به سرویس خارجی.",
                        "photo",
                        10200,
                    ),
                    (
                        "جمع‌بندی کنفرانس سالانه توسعه‌دهندگان وب و استانداردهای نوین داده",
                        "نگاهی به آینده وب معنایی، داده‌های ساختاریافته JSON-LD و نقش آن‌ها در دسترسی‌پذیری اطلاعات.",
                        "text",
                        15900,
                    ),
                ],
            },
            {
                "input": "https://economy-daily-demo.example.com/news",
                "title": "پایگاه خبری اقتصاد روز (نمونه آزمایشی)",
                "interval_value": 1,
                "interval_unit": "days",
                "posts": [
                    (
                        "خلاصه وضعیت بازارهای جهانی در آغاز معاملات هفتگی",
                        "بررسی تغییرات شاخص‌های کلیدی بازارهای کالایی و انرژی در اولین روز کاری هفته.",
                        "text",
                        31200,
                    ),
                    (
                        "تحلیل نموداری روند تقاضای انرژی در صنایع بزرگ",
                        "گزارش تصویری و آماری از میزان مصرف و بهره‌وری انرژی در بخش‌های صنعتی طی سه ماه گذشته.",
                        "photo",
                        27400,
                    ),
                    (
                        "فایل PDF گزارش شاخص مدیران خرید (PMI) منتشر شد",
                        "متن کامل گزارش ماهانه مرکز پژوهش‌ها درباره وضعیت سفارش‌های جدید و تولید صنعتی.",
                        "document",
                        19800,
                    ),
                    (
                        "بررسی راهکارهای نوین فین‌تک در تسویه سریع مبادلات تجاری",
                        "زیرساخت‌های پرداخت الکترونیکی نسل جدید زمان تسویه بین‌بانکی را به کمتر از چند ثانیه رسانده‌اند.",
                        "text",
                        22100,
                    ),
                    (
                        "بخشنامه جدید تسهیلات سرمایه در گردش واحدهای فناور",
                        "جزئیات شرایط بهره‌مندی شرکت‌های دانش‌بنیان و تولیدی از بسته‌های اعتباری فصل جدید.",
                        "text",
                        41500,
                    ),
                ],
            },
        ]

        created_sites = 0
        inserted_posts = 0
        now_dt = utc_now()

        for s_idx, spec in enumerate(demo_specs):
            parsed = parse_site_input(spec["input"])
            existing = self.get_site_by_key(parsed.site_key)

            if existing:
                site_id = int(existing["id"])
            else:
                site = self.create_site(
                    parsed=parsed,
                    interval_value=spec["interval_value"],
                    interval_unit=spec["interval_unit"],
                    initial_limit=10,
                    title=spec["title"],
                    is_active=False,
                    is_demo=True,
                )
                site_id = int(site["id"])
                created_sites += 1

            prepared: List[Dict[str, Any]] = []
            observed: List[str] = []
            total_items = len(spec["posts"])
            for idx, (headline, body, mtype, views) in enumerate(spec["posts"]):
                slug_id = 1001 + s_idx * 100 + idx
                post_url = f"{parsed.canonical_url.rstrip('/')}/article/{slug_id}"
                guid = compute_post_guid(post_url, site_canonical_url=parsed.canonical_url)
                observed.append(guid)

                post_dt = now_dt - timedelta(hours=(total_items - idx) * 3 + s_idx * 2, minutes=idx * 7)
                dt_parts = split_datetime(post_dt, tz_name=tz_name)
                prepared.append(
                    {
                        "post_guid": guid,
                        "post_url": post_url,
                        "title": headline,
                        "published_date": dt_parts.published_date,
                        "published_jalali_date": dt_parts.published_jalali_date,
                        "published_time": dt_parts.published_time,
                        "published_at_utc": dt_parts.published_at_utc,
                        "content": f"{headline}\n\n{body}",
                        "media_type": mtype,
                        "media_url": None,
                        "views": views,
                    }
                )

            res = self.save_sync_result(
                site_id=site_id,
                resolved_title=spec["title"],
                discovered_feed_url=f"{parsed.canonical_url.rstrip('/')}/rss.xml",
                prepared_posts=prepared,
                observed_guids=observed,
                trigger_type="demo",
                source_mode="rss",
                duration_ms=38,
            )
            inserted_posts += int(res["new_saved_count"])

        return {"created_sites": created_sites, "inserted_posts": inserted_posts}

    def clear_demo_data(self) -> int:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM sites WHERE is_demo = 1")
            return cur.rowcount
