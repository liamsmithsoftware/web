from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.site_parser import (
    compute_post_guid,
    normalize_article_url,
    normalize_digits,
    parse_site_input,
)


@pytest.mark.parametrize(
    "raw, expected_key, expected_domain, is_feed",
    [
        ("https://zoomit.ir", "zoomit.ir", "zoomit.ir", False),
        ("zoomit.ir", "zoomit.ir", "zoomit.ir", False),
        ("https://www.zoomit.ir/", "zoomit.ir", "zoomit.ir", False),
        ("  HTTPS://WWW.Zoomit.ir  ", "zoomit.ir", "zoomit.ir", False),
        ("https://www.isna.ir/service/economy/", "isna.ir/service/economy", "isna.ir", False),
        ("isna.ir/service/economy", "isna.ir/service/economy", "isna.ir", False),
        ("https://example.com/rss.xml", "example.com/rss.xml", "example.com", True),
        ("https://example.com/feed/", "example.com/feed", "example.com", True),
        ("http://localhost:8080/blog", "localhost:8080/blog", "localhost", False),
    ],
)
def test_parse_valid_website_variants(
    raw: str, expected_key: str, expected_domain: str, is_feed: bool
):
    parsed = parse_site_input(raw)
    assert parsed.site_key == expected_key
    assert parsed.domain == expected_domain
    assert parsed.is_direct_feed is is_feed
    assert parsed.canonical_url.startswith(("http://", "https://"))


def test_different_sections_of_same_domain_get_distinct_keys():
    econ = parse_site_input("https://isna.ir/service/economy")
    sport = parse_site_input("https://isna.ir/service/sports")
    assert econ.site_key != sport.site_key


def test_tracking_utm_params_are_stripped_from_urls_and_guids():
    clean = normalize_article_url(
        "https://www.example.com/news/42/?utm_source=rss&utm_medium=web&id=7#comments"
    )
    assert clean == "https://www.example.com/news/42?id=7"

    g1 = compute_post_guid("http://example.com/news/42?utm_source=feed")
    g2 = compute_post_guid("https://www.example.com/news/42/")
    assert g1 == g2


@pytest.mark.parametrize(
    "invalid_raw",
    [
        "",
        "   ",
        "@telegram_channel",
        "ftp://files.example.com/pub",
        "javascript:alert(1)",
        "just_a_word_without_tld",
        "https://has space.com/news",
    ],
)
def test_rejects_invalid_site_inputs(invalid_raw: str):
    with pytest.raises(ValueError):
        parse_site_input(invalid_raw)


def test_persian_and_arabic_digits_normalized():
    assert normalize_digits("۱۴۰۵/۰۸/۱۹ - ١٢:٣٠") == "1405/08/19 - 12:30"


def test_windows_batch_files_are_pure_ascii_with_crlf():
    root = Path(__file__).resolve().parent.parent
    for name in ("start_windows.bat", "stop_windows.bat"):
        raw_bytes = (root / name).read_bytes()
        # Must decode strictly as ASCII so Windows cmd.exe never breaks
        text = raw_bytes.decode("ascii")
        assert "\r\n" in text
        # Ensure no bare LF without preceding CR
        assert "\n" not in text.replace("\r\n", "")
