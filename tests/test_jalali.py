from __future__ import annotations

from datetime import datetime, timezone

import jdatetime
import pytest

from app.utils.jalali import (
    parse_any_datetime,
    parse_filter_date_to_gregorian,
    split_datetime,
)


def test_split_datetime_produces_all_structured_fields():
    dt = datetime(2026, 11, 8, 12, 30, 45, tzinfo=timezone.utc)
    parts = split_datetime(dt, tz_name="Asia/Tehran")

    assert parts.published_time == "16:00:45"
    assert parts.published_date == "2026-11-08"
    assert parts.published_jalali_date == "1405/08/17"
    assert parts.published_at_utc == "2026-11-08T12:30:45Z"


def test_split_datetime_crosses_midnight_boundary():
    dt = datetime(2026, 11, 8, 21, 0, 0, tzinfo=timezone.utc)
    parts = split_datetime(dt, tz_name="Asia/Tehran")
    assert parts.published_time == "00:30:00"
    assert parts.published_date == "2026-11-09"
    assert parts.published_jalali_date == jdatetime.date.fromgregorian(
        date=datetime(2026, 11, 9)
    ).strftime("%Y/%m/%d")


def test_parse_filter_date_accepts_both_jalali_and_gregorian():
    assert parse_filter_date_to_gregorian("1405/08/17") == "2026-11-08"
    assert parse_filter_date_to_gregorian("۱۴۰۵-۰۸-۱۷") == "2026-11-08"
    assert parse_filter_date_to_gregorian("2026-11-08") == "2026-11-08"
    assert parse_filter_date_to_gregorian("") is None

    with pytest.raises(ValueError):
        parse_filter_date_to_gregorian("not-a-date")


def test_parse_any_datetime_supports_iso_rfc_and_persian_formats():
    # ISO-8601
    dt_iso = parse_any_datetime("2026-11-10T12:00:00Z", tz_name="Asia/Tehran")
    assert dt_iso == datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # RFC-2822 (RSS pubDate)
    dt_rfc = parse_any_datetime("Tue, 10 Nov 2026 10:00:00 +0000", tz_name="Asia/Tehran")
    assert dt_rfc == datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)

    # Numeric Jalali in Tehran local time (16:00 Tehran = 12:30 UTC)
    dt_jalali = parse_any_datetime("۱۴۰۵/۰۸/۱۷ - ۱۶:۰۰:۴۵", tz_name="Asia/Tehran")
    assert dt_jalali == datetime(2026, 11, 8, 12, 30, 45, tzinfo=timezone.utc)

    # Named Persian month (۱۷ آبان ۱۴۰۵ ساعت ۱۶:۰۰)
    dt_named = parse_any_datetime("منتشر شده در ۱۷ آبان ۱۴۰۵ ساعت ۱۶:۰۰", tz_name="Asia/Tehran")
    assert dt_named == datetime(2026, 11, 8, 12, 30, 0, tzinfo=timezone.utc)
