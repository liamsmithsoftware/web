from __future__ import annotations

import html as html_lib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

from app.collectors.base import FetchedPost
from app.collectors.html_scraper import _is_candidate_article_url
from app.utils.jalali import parse_any_datetime
from app.utils.site_parser import compute_post_guid, normalize_article_url

XML_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    if ":" in tag:
        return tag.rsplit(":", 1)[1].lower()
    return tag.lower()


def is_sitemap_xml(text: str, content_type: str = "") -> bool:
    head = (text or "").lstrip()[:1500].lower()
    return "<urlset" in head or "<sitemapindex" in head


def extract_child_sitemaps(text: str, *, max_items: int = 4) -> List[str]:
    """If sitemap.xml is a <sitemapindex>, prioritize post/news/article child sitemaps."""
    cleaned = XML_CTRL_RE.sub("", (text or "").lstrip("\ufeff").strip())
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return []
    if _local_name(root.tag) != "sitemapindex":
        return []

    locs: List[str] = []
    for sm in root:
        if _local_name(sm.tag) != "sitemap":
            continue
        for child in sm:
            if _local_name(child.tag) == "loc":
                loc = "".join(child.itertext()).strip()
                if loc.startswith(("http://", "https://")) and not loc.endswith(".gz"):
                    locs.append(loc)

    def _priority(u: str) -> int:
        low = u.lower()
        if "news" in low:
            return 0
        if "post" in low or "article" in low or "analysis" in low:
            return 1
        if "page" in low or "category" in low or "tag" in low or "author" in low:
            return 9
        return 5

    locs.sort(key=_priority)
    return locs[:max_items]


def _slug_to_title(url: str) -> str:
    path = unquote(urlparse(url).path or "").strip("/")
    if not path:
        return url
    segments = [s for s in path.split("/") if s and s.lower() not in ("transcript", "amp")]
    slug = segments[-1] if segments else path
    slug = re.sub(r"\.(?:html?|php|aspx?)$", "", slug, flags=re.IGNORECASE)
    words = slug.replace("-", " ").replace("_", " ").strip()
    if len(words) < 4:
        return url
    return words[:1].upper() + words[1:]


def parse_sitemap_posts(
    raw_xml: str,
    base_url: str,
    tz_name: str = "Asia/Tehran",
    *,
    max_posts: int = 30,
) -> Tuple[Optional[str], List[FetchedPost]]:
    """
    Extracts recent article entries from a `<urlset>` sitemap (Google News sitemap
    or standard sitemap with `<lastmod>`), sorted newest-first.
    """
    cleaned = XML_CTRL_RE.sub("", (raw_xml or "").lstrip("\ufeff").strip())
    if not cleaned:
        return None, []

    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return None, []

    if _local_name(root.tag) != "urlset":
        return None, []

    site_title: Optional[str] = None
    candidates: List[Tuple[datetime, FetchedPost]] = []
    seen_guids = set()

    for url_node in root:
        if _local_name(url_node.tag) != "url":
            continue

        loc = ""
        lastmod_str = ""
        news_title = ""
        news_pub_date = ""
        news_site_name = ""

        for child in url_node:
            cname = _local_name(child.tag)
            if cname == "loc":
                loc = "".join(child.itertext()).strip()
            elif cname == "lastmod":
                lastmod_str = "".join(child.itertext()).strip()
            elif cname == "news":
                for nchild in child:
                    nname = _local_name(nchild.tag)
                    if nname == "title":
                        news_title = html_lib.unescape("".join(nchild.itertext()).strip())
                    elif nname == "publication_date":
                        news_pub_date = "".join(nchild.itertext()).strip()
                    elif nname == "publication":
                        for pub_sub in nchild:
                            if _local_name(pub_sub.tag) == "name":
                                news_site_name = html_lib.unescape("".join(pub_sub.itertext()).strip())

        if news_site_name and not site_title:
            site_title = news_site_name

        norm_url = normalize_article_url(loc, base_url=base_url)
        if not _is_candidate_article_url(norm_url, base_url):
            continue
        # Skip auxiliary transcript subpages in sitemaps when main episode URLs exist
        if norm_url.rstrip("/").lower().endswith("/transcript"):
            continue

        pub_dt = parse_any_datetime(news_pub_date or lastmod_str, tz_name=tz_name)
        # Only use sitemap items that carry a real timestamp or at least 2 path segments
        path_depth = len([s for s in (urlparse(norm_url).path or "").split("/") if s])
        if pub_dt is None and path_depth < 2:
            continue

        guid = compute_post_guid(norm_url, site_canonical_url=base_url)
        if not guid or guid in seen_guids:
            continue
        seen_guids.add(guid)

        title = news_title or _slug_to_title(norm_url)
        sort_dt = pub_dt or datetime(1970, 1, 1, tzinfo=timezone.utc)

        candidates.append(
            (
                sort_dt,
                FetchedPost(
                    post_guid=guid,
                    post_url=norm_url,
                    title=title[:260],
                    published_at=pub_dt,
                    content=title,
                    media_type="text",
                    needs_article_fetch=True,
                ),
            )
        )

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return site_title, [post for _, post in candidates[:max_posts]]
