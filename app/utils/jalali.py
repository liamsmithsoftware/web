from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime

from app.utils.site_parser import normalize_digits

PERSIAN_MONTHS = {
    "فروردین": 1,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "امرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "آبان": 8,
    "ابان": 8,
    "آذر": 9,
    "اذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
}

ENGLISH_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

JALALI_NUMERIC_RE = re.compile(
    r"(?<!\d)(1[34]\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})(?!\d)"
    r"(?:[\s،,\-–—|ساعتT]*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

GREGORIAN_NUMERIC_RE = re.compile(
    r"(?<!\d)(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})(?!\d)"
    r"(?:[\s,T\-–—|]*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

PERSIAN_NAMED_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s+("
    + "|".join(PERSIAN_MONTHS.keys())
    + r")\s+(1[34]\d{2})(?!\d)"
    r"(?:[\s،,\-–—|ساعت]*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

_EN_MONTH_PATTERN = "|".join(sorted(ENGLISH_MONTHS.keys(), key=len, reverse=True))

# Matches e.g. "November 6, 2026", "Nov. 9th, 2026 2:30 pm", "November 9th"
ENGLISH_MDY_RE = re.compile(
    rf"\b({_EN_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(20\d{{2}}))?(?!\d)"
    r"(?:[\s,\-–—|at]*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE,
)

# Matches e.g. "6 November 2026", "9th Nov 2026, 14:30"
ENGLISH_DMY_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_EN_MONTH_PATTERN})\.?(?:\s*,?\s*(20\d{{2}}))?(?!\d)"
    r"(?:[\s,\-–—|at]*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SplitDateTime:
    published_at_utc: str
    published_date: str
    published_jalali_date: str
    published_time: str


def get_zoneinfo(tz_name: str = "Asia/Tehran") -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def format_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_datetime(dt: datetime, tz_name: str = "Asia/Tehran") -> SplitDateTime:
    """
    Convert a datetime into structured fields:
    - published_at_utc: ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)
    - published_date: Gregorian date in target timezone (YYYY-MM-DD)
    - published_jalali_date: Solar Hijri (Shamsi) date in target timezone (YYYY/MM/DD)
    - published_time: Time in target timezone (HH:MM:SS)
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_utc = dt.astimezone(timezone.utc)
    local_tz = get_zoneinfo(tz_name)
    dt_local = dt_utc.astimezone(local_tz)

    jalali_dt = jdatetime.datetime.fromgregorian(datetime=dt_local)

    return SplitDateTime(
        published_at_utc=dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        published_date=dt_local.strftime("%Y-%m-%d"),
        published_jalali_date=jalali_dt.strftime("%Y/%m/%d"),
        published_time=dt_local.strftime("%H:%M:%S"),
    )


def format_local_display(iso_utc: Optional[str], tz_name: str = "Asia/Tehran") -> Optional[str]:
    if not iso_utc:
        return None
    try:
        cleaned = iso_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        parts = split_datetime(dt, tz_name=tz_name)
        return f"{parts.published_jalali_date} - {parts.published_time}"
    except ValueError:
        return iso_utc


def parse_filter_date_to_gregorian(date_str: Optional[str]) -> Optional[str]:
    """
    Accepts either Gregorian (2026-11-08 / 2026/11/08) or Jalali (1405/08/17 / ۱۴۰۵-۰۸-۱۷)
    and returns standard Gregorian 'YYYY-MM-DD' for SQL filtering.
    """
    if not date_str:
        return None
    cleaned = normalize_digits(date_str.strip()).replace("/", "-")
    if not cleaned:
        return None

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if not m:
        raise ValueError(
            f"فرمت تاریخ «{date_str}» معتبر نیست. لطفاً به صورت ۱۴۰۵/۰۸/۱۷ یا 2026-11-08 وارد کنید."
        )

    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1300 <= year <= 1599:
        try:
            greg = jdatetime.date(year, month, day).togregorian()
            return greg.strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"تاریخ شمسی «{date_str}» معتبر نیست.") from exc
    else:
        try:
            greg = datetime(year, month, day).date()
            return greg.strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"تاریخ میلادی «{date_str}» معتبر نیست.") from exc


def _build_english_datetime(
    *,
    year_str: Optional[str],
    month: int,
    day: int,
    hour_str: Optional[str],
    minute_str: Optional[str],
    second_str: Optional[str],
    ampm: Optional[str],
    local_tz: ZoneInfo,
) -> Optional[datetime]:
    now_utc = utc_now()
    inferred_year = year_str is None
    year = int(year_str) if year_str else now_utc.year
    hour = int(hour_str or 0)
    minute = int(minute_str or 0)
    second = int(second_str or 0)

    if ampm:
        ap = ampm.lower()
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0

    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=local_tz).astimezone(timezone.utc)
        if inferred_year and dt > now_utc + timedelta(days=3):
            dt = datetime(year - 1, month, day, hour, minute, second, tzinfo=local_tz).astimezone(
                timezone.utc
            )
        return dt
    except ValueError:
        return None


def parse_any_datetime(
    raw_value: Optional[str],
    tz_name: str = "Asia/Tehran",
) -> Optional[datetime]:
    """
    Universally parses timestamps encountered on Persian and international websites:
    - ISO-8601 (2026-11-10T12:30:00Z, 2026-11-10T16:00:00+03:30)
    - RFC-2822 / RSS pubDate (Tue, 10 Nov 2026 12:30:00 +0000)
    - Unix timestamps (seconds or milliseconds)
    - Jalali numeric (۱۴۰۵/۰۸/۱۹ - ۱۴:۳۰ or 1405-08-19 14:30:00)
    - Persian month names (۱۹ آبان ۱۴۰۵ ساعت ۱۴:۳۰)
    - English month names (November 6, 2026 / 6 Nov 2026 / November 9th)
    Returns a timezone-aware UTC datetime, or None if unparseable.
    """
    if raw_value is None:
        return None
    text = normalize_digits(str(raw_value)).strip()
    if not text:
        return None

    local_tz = get_zoneinfo(tz_name)

    # 1. Pure epoch seconds / milliseconds
    if re.fullmatch(r"\d{10}|\d{13}", text):
        try:
            ts = int(text)
            if len(text) == 13:
                ts = ts / 1000.0
            if 946684800 <= ts <= 4102444800:  # 2000..2100
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            pass

    # 2. Standard ISO-8601
    iso_candidate = text
    if iso_candidate.endswith("Z") or iso_candidate.endswith("z"):
        iso_candidate = iso_candidate[:-1] + "+00:00"
    iso_candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso_candidate)
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.year >= 1990:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz)
            return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # 3. RFC-2822 (RSS pubDate)
    try:
        dt_rfc = email.utils.parsedate_to_datetime(text)
        if dt_rfc is not None and dt_rfc.year >= 1990:
            if dt_rfc.tzinfo is None:
                dt_rfc = dt_rfc.replace(tzinfo=timezone.utc)
            return dt_rfc.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass

    # 4. Persian named month (e.g. ۱۹ آبان ۱۴۰۵ ساعت ۱۴:۳۰)
    m_named = PERSIAN_NAMED_DATE_RE.search(text)
    if m_named:
        day = int(m_named.group(1))
        month = PERSIAN_MONTHS.get(m_named.group(2), 0)
        year = int(m_named.group(3))
        hour = int(m_named.group(4) or 0)
        minute = int(m_named.group(5) or 0)
        second = int(m_named.group(6) or 0)
        if month:
            try:
                greg_date = jdatetime.date(year, month, day).togregorian()
                local_dt = datetime(
                    greg_date.year,
                    greg_date.month,
                    greg_date.day,
                    hour,
                    minute,
                    second,
                    tzinfo=local_tz,
                )
                return local_dt.astimezone(timezone.utc)
            except ValueError:
                pass

    # 5. Jalali numeric (e.g. 1405/08/19 14:30)
    m_jalali = JALALI_NUMERIC_RE.search(text)
    if m_jalali:
        year = int(m_jalali.group(1))
        month = int(m_jalali.group(2))
        day = int(m_jalali.group(3))
        hour = int(m_jalali.group(4) or 0)
        minute = int(m_jalali.group(5) or 0)
        second = int(m_jalali.group(6) or 0)
        try:
            greg_date = jdatetime.date(year, month, day).togregorian()
            local_dt = datetime(
                greg_date.year,
                greg_date.month,
                greg_date.day,
                hour,
                minute,
                second,
                tzinfo=local_tz,
            )
            return local_dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # 6. Gregorian numeric inside arbitrary text (e.g. "Published: 2026/11/10 - 14:30")
    m_greg = GREGORIAN_NUMERIC_RE.search(text)
    if m_greg:
        year = int(m_greg.group(1))
        month = int(m_greg.group(2))
        day = int(m_greg.group(3))
        hour = int(m_greg.group(4) or 0)
        minute = int(m_greg.group(5) or 0)
        second = int(m_greg.group(6) or 0)
        try:
            local_dt = datetime(year, month, day, hour, minute, second, tzinfo=local_tz)
            return local_dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # 7. English month-first ("November 6, 2026" / "November 9th")
    m_en_mdy = ENGLISH_MDY_RE.search(text)
    if m_en_mdy:
        month = ENGLISH_MONTHS.get(m_en_mdy.group(1).lower(), 0)
        if month:
            built = _build_english_datetime(
                year_str=m_en_mdy.group(3),
                month=month,
                day=int(m_en_mdy.group(2)),
                hour_str=m_en_mdy.group(4),
                minute_str=m_en_mdy.group(5),
                second_str=m_en_mdy.group(6),
                ampm=m_en_mdy.group(7),
                local_tz=local_tz,
            )
            if built is not None:
                return built

    # 8. English day-first ("6 November 2026" / "9th Nov")
    m_en_dmy = ENGLISH_DMY_RE.search(text)
    if m_en_dmy:
        month = ENGLISH_MONTHS.get(m_en_dmy.group(2).lower(), 0)
        if month:
            built = _build_english_datetime(
                year_str=m_en_dmy.group(3),
                month=month,
                day=int(m_en_dmy.group(1)),
                hour_str=m_en_dmy.group(4),
                minute_str=m_en_dmy.group(5),
                second_str=m_en_dmy.group(6),
                ampm=m_en_dmy.group(7),
                local_tz=local_tz,
            )
            if built is not None:
                return built

    return None
