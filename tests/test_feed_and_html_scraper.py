from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.collectors.feed_parser import (
    discover_feed_urls_in_html,
    is_feed_document,
    parse_feed_content,
)
from app.collectors.html_scraper import extract_article_details, extract_listing_posts

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_rss_feed_extracts_items_content_and_media():
    raw_xml = (FIXTURES / "sample_rss.xml").read_text(encoding="utf-8")
    assert is_feed_document(raw_xml, "application/rss+xml") is True

    title, posts = parse_feed_content(raw_xml, base_url="https://techsample.ir/", tz_name="Asia/Tehran")
    assert title == "خبرگزاری فناوری نمونه"
    assert len(posts) == 3

    # Preserves feed order (newest first) and strips utm_source
    newest = posts[0]
    assert newest.title == "خبر سوم و جدیدترین مطلب"
    assert newest.post_url == "https://techsample.ir/news/103"
    assert "متن کامل خبر سوم" in newest.content
    assert "پاراگراف دوم خبر سوم" in newest.content
    assert newest.media_type == "photo"
    assert newest.media_url == "https://techsample.ir/img/103.jpg"
    assert newest.published_at == datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)

    second = posts[1]
    assert second.media_type == "video"
    assert second.media_url == "https://techsample.ir/media/102.mp4"


def test_parse_atom_feed():
    raw_atom = (FIXTURES / "sample_atom.xml").read_text(encoding="utf-8")
    assert is_feed_document(raw_atom, "application/atom+xml") is True

    title, posts = parse_feed_content(raw_atom, base_url="https://devblog.example.com/")
    assert title == "وبلاگ مهندسی نرم‌افزار"
    assert len(posts) == 1
    assert posts[0].post_url == "https://devblog.example.com/posts/modular-python"
    assert "راهنمای کاربردی طراحی ماژولار" in posts[0].content


def test_feed_discovery_respects_section_strictness():
    html = (FIXTURES / "sample_listing.html").read_text(encoding="utf-8")

    # On root page, site-wide /rss.xml is discovered
    root_feeds = discover_feed_urls_in_html(html, "https://newssite.ir/", section_strict=False)
    assert root_feeds == ["https://newssite.ir/rss.xml"]

    # On a specific section page (/service/sports), root /rss.xml is skipped so we don't mix categories
    section_feeds = discover_feed_urls_in_html(
        html, "https://newssite.ir/service/sports", section_strict=True
    )
    assert section_feeds == []


def test_html_listing_and_article_body_extraction():
    listing_html = (FIXTURES / "sample_listing.html").read_text(encoding="utf-8")
    site_title, posts = extract_listing_posts(
        listing_html, page_url="https://newssite.ir/", tz_name="Asia/Tehran"
    )
    assert site_title == "پایگاه خبری نمونه"
    # Navbar (/login, /about) and footer (/contact) links must NOT appear in posts
    assert len(posts) == 3
    assert posts[0].post_url == "https://newssite.ir/news/5003/latest-report"
    assert posts[0].views == 1500
    assert posts[0].media_type == "photo"
    # Second item parsed its Persian numeric date!
    assert posts[1].published_at is not None

    article_html = (FIXTURES / "sample_article.html").read_text(encoding="utf-8")
    details = extract_article_details(
        article_html,
        article_url="https://newssite.ir/news/5003/latest-report",
        tz_name="Asia/Tehran",
    )
    assert details["title"] == "گزارش جامع تحولات بازار فناوری در آبان ماه"
    assert "شاخص‌های رشد بازار فناوری اطلاعات" in details["content"]
    assert details["media_url"] == "https://newssite.ir/images/5003-large.jpg"
