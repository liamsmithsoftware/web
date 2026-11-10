from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Set, Tuple

from app.config import (
    DB_PATH,
    DEFAULT_INITIAL_LIMIT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_TIMEZONE,
)
from app.utils.jalali import format_iso_utc, split_datetime, utc_now, utc_now_iso
from app.utils.site_parser import ParsedSite, compute_post_guid

BACKUP_FORMAT_NAME = "website-content-collector-backup"
BACKUP_FORMAT_VERSION = 1

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

    def init_db(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
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
                    updated_at TEXT NOT NULL
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
    ) -> Dict[str, Any]:
        interval_minutes = compute_interval_minutes(interval_value, interval_unit)
        if initial_limit < 1 or initial_limit > 100:
            raise ValueError("تعداد مطالب دریافت اولیه باید بین ۱ تا ۱۰۰ باشد.")

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
                    interval_minutes, initial_limit, is_active, is_demo,
                    next_fetch_at, last_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
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
                    next_iso,
                    now_iso,
                    now_iso,
                ),
            )
            site_id = int(cur.lastrowid)

        site = self.get_site(site_id)
        assert site is not None
        return site

    def get_site(self, site_id: int) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    s.*,
                    COUNT(p.id) AS posts_count
                FROM sites s
                LEFT JOIN posts p ON p.site_id = s.id
                WHERE s.id = ?
                GROUP BY s.id
                """,
                (site_id,),
            ).fetchone()
        return dict(row) if row else None

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
                """
                SELECT
                    s.*,
                    COUNT(p.id) AS posts_count
                FROM sites s
                LEFT JOIN posts p ON p.site_id = s.id
                GROUP BY s.id
                ORDER BY s.id DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def update_site(
        self,
        site_id: int,
        *,
        title: Optional[str] = None,
        interval_value: Optional[float] = None,
        interval_unit: Optional[str] = None,
        initial_limit: Optional[int] = None,
        is_active: Optional[bool] = None,
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

        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sites
                SET title = ?,
                    custom_title = ?,
                    interval_value = ?,
                    interval_unit = ?,
                    interval_minutes = ?,
                    initial_limit = ?,
                    is_active = ?,
                    next_fetch_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    new_title,
                    new_custom_title,
                    new_val,
                    new_unit,
                    new_minutes,
                    new_limit,
                    1 if new_active else 0,
                    next_fetch,
                    now_iso,
                    site_id,
                ),
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
                }
            )

        payload: Dict[str, Any] = {
            "format": BACKUP_FORMAT_NAME,
            "format_version": BACKUP_FORMAT_VERSION,
            "app_version": app_version,
            "created_at": utc_now_iso(),
            "note": (note or "").strip(),
            "counts": {
                "sites": len(sites_payload),
                "settings": 0,
            },
            "sites": sites_payload,
            "settings": {},
        }

        if include_settings:
            backup_settings = self.get_backup_settings(include_secrets=include_secrets)
            payload["settings"] = backup_settings
            payload["counts"]["settings"] = len(backup_settings)

        payload["secrets_included"] = bool(include_settings and include_secrets)
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

        clean_sites: List[Dict[str, Any]] = []
        for index, raw in enumerate(sites, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"وب‌سایت شماره {index} در فایل پشتیبان معتبر نیست.")
            target = str(raw.get("site_input") or raw.get("canonical_url") or "").strip()
            if not target:
                raise ValueError(f"وب‌سایت شماره {index} آدرس اینترنتی ندارد.")
            clean_sites.append({**raw, "site_input": target})

        return {
            "format": BACKUP_FORMAT_NAME,
            "format_version": version,
            "created_at": data.get("created_at"),
            "note": data.get("note") or "",
            "sites": clean_sites,
            "settings": settings,
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
            "cursor_restored": 0,
            "errors": [],
            "warnings": [],
        }

        if mode == "replace":
            report["removed_sites"] = int(self.clear_all_sites())

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
