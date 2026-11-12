from __future__ import annotations

import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.collectors.base import CollectorError, FetchedPost
from app.utils.jalali import parse_any_datetime
from app.utils.site_parser import compute_post_guid, normalize_article_url

XML_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

FEED_MIME_HINTS = (
    "application/rss+xml",
    "application/atom+xml",
    "application/rdf+xml",
    "application/feed+json",
    "application/xml",
    "text/xml",
)


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    if ":" in tag:
        return tag.rsplit(":", 1)[1].lower()
    return tag.lower()


def html_fragment_to_text(raw_html: str) -> Tuple[str, Optional[str], str]:
    """
    Clean an HTML snippet (from RSS description / content:encoded) into readable
    plain text while also detecting embedded media (image/video/audio).
    Returns (clean_text, media_url, media_type).
    """
    if not raw_html:
        return "", None, "text"

    unescaped = html_lib.unescape(raw_html)
    soup = BeautifulSoup(unescaped, "html.parser")

    media_url: Optional[str] = None
    media_type = "text"

    video = soup.find("video")
    if video:
        media_type = "video"
        src = video.get("src")
        if not src:
            source = video.find("source")
            src = source.get("src") if source else None
        if src:
            media_url = str(src).strip()

    if media_type == "text":
        audio = soup.find("audio")
        if audio:
            media_type = "voice"
            src = audio.get("src")
            if not src:
                source = audio.find("source")
                src = source.get("src") if source else None
            if src:
                media_url = str(src).strip()

    if media_type == "text":
        img = soup.find("img")
        if img:
            src = img.get("src") or img.get("data-src")
            if src and not str(src).startswith("data:"):
                media_type = "photo"
                media_url = str(src).strip()

    for bad in soup.find_all(["script", "style", "noscript", "iframe"]):
        bad.decompose()

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for li in soup.find_all("li"):
        li.insert_before("• ")
        li.append("\n")

    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "blockquote"]):
        block.append("\n\n")

    raw_text = soup.get_text()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_text.replace("\r\n", "\n").split("\n")]
    joined = "\n".join(lines).strip()
    clean = re.sub(r"\n{3,}", "\n\n", joined)
    return clean, media_url, media_type


def _classify_mime_or_url(mime: str, url: str) -> str:
    m = (mime or "").lower()
    u = (url or "").lower().split("?", 1)[0]
    if m.startswith("image/") or u.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "photo"
    if m.startswith("video/") or u.endswith((".mp4", ".webm", ".mkv", ".mov")):
        return "video"
    if m.startswith("audio/") or u.endswith((".mp3", ".ogg", ".wav", ".m4a")):
        return "voice"
    if m in ("application/pdf", "application/zip") or u.endswith((".pdf", ".zip", ".rar", ".docx", ".xlsx")):
        return "document"
    return "text"


def is_feed_document(text: str, content_type: str = "") -> bool:
    ctype = (content_type or "").lower()
    head = (text or "").lstrip()[:1200]
    if not head:
        return False

    if "application/feed+json" in ctype or head.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "jsonfeed.org" in str(obj.get("version", "")):
                return True
        except Exception:
            return False

    lower_head = head.lower()
    if "<html" in lower_head and "<rss" not in lower_head and "<feed" not in lower_head:
        return False
    if any(tag in lower_head for tag in ("<rss", "<feed", "<rdf:rdf", "<channel>")):
        return True
    if any(h in ctype for h in ("rss+xml", "atom+xml", "rdf+xml")):
        return True
    return False


def discover_feed_urls_in_html(
    html: str,
    page_url: str,
    *,
    section_strict: bool = False,
) -> List[str]:
    """
    Finds RSS / Atom / JSON feed links advertised in HTML <head>.
    When `section_strict` is True (e.g., user added `https://site.ir/service/economy`),
    site-wide root feeds (`/rss`, `/feed`) are ignored unless they match the section path,
    so we don't mix unrelated categories into a specific section monitor.
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed_page = urlparse(page_url)
    page_path = (parsed_page.path or "/").rstrip("/").lower()

    candidates: List[str] = []
    seen = set()

    for link in soup.find_all("link"):
        rels = [r.lower() for r in (link.get("rel") or [])]
        ltype = str(link.get("type") or "").lower()
        href = str(link.get("href") or "").strip()
        if not href:
            continue
        if "alternate" not in rels and not any(t in ltype for t in ("rss", "atom", "feed+json")):
            continue
        if not any(t in ltype for t in ("rss", "atom", "xml", "feed+json")):
            continue

        full_url = urljoin(page_url, href)
        parsed_feed = urlparse(full_url)
        if parsed_feed.scheme not in ("http", "https"):
            continue

        if section_strict and page_path and page_path not in ("", "/", "/fa", "/en", "/index.html"):
            feed_path = (parsed_feed.path or "/").lower()
            feed_query = (parsed_feed.query or "").lower()
            # Keep feed only if it belongs to or references this section
            section_tokens = [seg for seg in page_path.split("/") if len(seg) >= 3]
            if not (
                feed_path.startswith(page_path)
                or any(tok in feed_path or tok in feed_query for tok in section_tokens)
            ):
                continue

        if full_url not in seen:
            seen.add(full_url)
            candidates.append(full_url)

    # Fallback for US DoD / AFPIMS (.gov / .mil) portals (`RSS.ashx`) & RSS directory pages (`/rss-feeds`)
    if not candidates:
        base_host = (parsed_page.hostname or "").lower().rstrip(".")
        is_rss_directory_page = any(
            tok in page_path for tok in ("/rss", "/feed", "/feeds")
        )
        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full_url = urljoin(page_url, href)
            parsed_feed = urlparse(full_url)
            if parsed_feed.scheme not in ("http", "https"):
                continue
            cand_host = (parsed_feed.hostname or "").lower().rstrip(".")
            f_path = (parsed_feed.path or "").lower()
            if "sitemap" in f_path:
                continue

            is_ashx = "rss.ashx" in f_path
            is_same_host = bool(base_host and cand_host == base_host)
            is_known_xml_or_feed = is_same_host and (
                f_path.endswith((".rss", ".atom", "/feed.xml", "/rss.xml", "/whatsnew.xml"))
                or f_path.rstrip("/") == "/feeds/articles"
                or (is_rss_directory_page and f_path.endswith(".xml"))
            )
            if (is_ashx or is_known_xml_or_feed) and full_url not in seen:
                seen.add(full_url)
                candidates.append(full_url)

    return candidates


def is_placeholder_post(post: FetchedPost) -> bool:
    """
    Detects unedited CMS installation placeholder posts (e.g. WordPress default 'Hello world!'
    on sites like `mei.edu` that store real articles in Custom Post Types).
    """
    url_path = (urlparse(post.post_url or "").path or "").rstrip("/").lower()
    title_low = (post.title or "").strip().lower()
    body_low = (post.content or "").lower()
    if url_path.endswith("/hello-world") and (
        title_low in ("hello world!", "hello world", "سلام دنیا!")
        or "welcome to wordpress" in body_low
        or "this is your first post" in body_low
    ):
        return True
    if title_low in ("hello world!", "سلام دنیا!") and (
        "welcome to wordpress" in body_low or "this is your first post" in body_low
    ):
        return True
    return False


def _parse_json_feed(
    raw_text: str,
    base_url: str,
    tz_name: str,
) -> Tuple[Optional[str], List[FetchedPost]]:
    data = json.loads(raw_text)
    feed_title = str(data.get("title") or "").strip() or None
    items = data.get("items") or []
    posts: List[FetchedPost] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("url") or item.get("external_url") or "").strip()
        post_url = normalize_article_url(raw_url, base_url=base_url) or base_url
        raw_id = str(item.get("id") or "").strip()
        guid = compute_post_guid(post_url, raw_guid=raw_id, site_canonical_url=base_url)
        if not guid:
            continue

        title = html_lib.unescape(str(item.get("title") or "").strip())
        html_body = str(item.get("content_html") or "")
        text_body = str(item.get("content_text") or item.get("summary") or "").strip()
        clean_html_text, media_url, media_type = html_fragment_to_text(html_body)
        body = clean_html_text or text_body

        image = str(item.get("image") or item.get("banner_image") or "").strip()
        if image and not media_url:
            media_url = urljoin(post_url, image)
            media_type = "photo"

        pub_str = str(item.get("date_published") or item.get("date_modified") or "").strip()
        published_at = parse_any_datetime(pub_str, tz_name=tz_name)

        if not title and body:
            title = body.split("\n", 1)[0][:120]
        if not body:
            body = title or "[مطلب بدون متن]"

        posts.append(
            FetchedPost(
                post_guid=guid,
                post_url=post_url,
                title=title or "بدون عنوان",
                published_at=published_at,
                content=body,
                media_type=media_type,
                media_url=urljoin(post_url, media_url) if media_url else None,
                needs_article_fetch=len(body) < 260 and post_url != base_url,
            )
        )

    return feed_title, posts


def parse_feed_content(
    raw_text: str,
    base_url: str,
    tz_name: str = "Asia/Tehran",
) -> Tuple[Optional[str], List[FetchedPost]]:
    """
    Parses RSS 2.0, RSS 1.0/RDF, Atom 1.0, or JSON Feed into structured FetchedPost items
    preserving feed order (index 0 = newest).
    """
    cleaned = XML_CONTROL_CHARS_RE.sub("", (raw_text or "").lstrip("\ufeff").strip())
    if not cleaned:
        raise CollectorError("محتوای فید وب‌سایت خالی است.")

    if cleaned.startswith("{"):
        try:
            return _parse_json_feed(cleaned, base_url=base_url, tz_name=tz_name)
        except Exception as exc:
            raise CollectorError(f"ساختار JSON Feed معتبر نیست: {exc}") from exc

    # Parse XML with ElementTree, falling back to BeautifulSoup if XML has minor syntax errors
    try:
        root = ET.fromstring(cleaned)
        return _parse_xml_element_tree(root, base_url=base_url, tz_name=tz_name)
    except ET.ParseError:
        return _parse_broken_xml_with_bs4(cleaned, base_url=base_url, tz_name=tz_name)


def _child_text(elem: ET.Element, names: Tuple[str, ...]) -> str:
    for child in elem:
        if _local_tag(child.tag) in names:
            txt = "".join(child.itertext()).strip()
            if txt:
                return txt
    return ""


def _parse_xml_element_tree(
    root: ET.Element,
    base_url: str,
    tz_name: str,
) -> Tuple[Optional[str], List[FetchedPost]]:
    root_name = _local_tag(root.tag)
    feed_title: Optional[str] = None
    item_nodes: List[ET.Element] = []

    if root_name == "rss":
        channel = next((c for c in root if _local_tag(c.tag) == "channel"), None)
        if channel is not None:
            feed_title = _child_text(channel, ("title",)) or None
            item_nodes = [c for c in channel if _local_tag(c.tag) == "item"]
    elif root_name == "feed":
        feed_title = _child_text(root, ("title",)) or None
        item_nodes = [c for c in root if _local_tag(c.tag) == "entry"]
    elif root_name in ("rdf", "channel"):
        channel = next((c for c in root if _local_tag(c.tag) == "channel"), root)
        feed_title = _child_text(channel, ("title",)) or None
        item_nodes = [c for c in root.iter() if _local_tag(c.tag) in ("item", "entry")]
    else:
        raise CollectorError("ساختار فید RSS/Atom در این آدرس شناسایی نشد.")

    if feed_title:
        feed_title = html_lib.unescape(feed_title).strip()

    posts: List[FetchedPost] = []
    for node in item_nodes:
        title = html_lib.unescape(_child_text(node, ("title",))).strip()

        # Extract link
        raw_link = ""
        for child in node:
            if _local_tag(child.tag) == "link":
                href = (child.attrib.get("href") or "").strip()
                rel = (child.attrib.get("rel") or "alternate").lower()
                if href:
                    if rel == "alternate" or not raw_link:
                        raw_link = href
                else:
                    txt = "".join(child.itertext()).strip()
                    if txt:
                        raw_link = txt

        raw_guid = _child_text(node, ("guid", "id"))
        if not raw_link and raw_guid.startswith(("http://", "https://")):
            raw_link = raw_guid

        post_url = normalize_article_url(raw_link, base_url=base_url) or base_url
        guid = compute_post_guid(post_url, raw_guid=raw_guid, site_canonical_url=base_url)
        if not guid:
            continue

        # Extract rich content > description > summary
        encoded_html = ""
        summary_html = ""
        for child in node:
            lname = _local_tag(child.tag)
            if lname in ("encoded", "content"):
                txt = "".join(child.itertext()).strip()
                if txt and len(txt) > len(encoded_html):
                    encoded_html = txt
            elif lname in ("description", "summary"):
                txt = "".join(child.itertext()).strip()
                if txt and len(txt) > len(summary_html):
                    summary_html = txt

        chosen_raw = encoded_html or summary_html
        body_text, inline_media_url, inline_media_type = html_fragment_to_text(chosen_raw)

        # Check explicit media enclosures (<enclosure>, <media:content>, <media:thumbnail>)
        media_url = inline_media_url
        media_type = inline_media_type
        for sub in node.iter():
            sname = _local_tag(sub.tag)
            if sname in ("enclosure", "content", "thumbnail"):
                m_url = (sub.attrib.get("url") or sub.attrib.get("href") or "").strip()
                m_type = (sub.attrib.get("type") or sub.attrib.get("medium") or "").strip()
                if m_url:
                    classified = _classify_mime_or_url(
                        m_type, m_url
                    ) if sname != "thumbnail" else "photo"
                    if classified != "text":
                        media_url = m_url
                        media_type = classified
                        break

        pub_raw = _child_text(node, ("pubdate", "published", "updated", "date"))
        published_at = parse_any_datetime(pub_raw, tz_name=tz_name)
        author = _child_text(node, ("creator", "author")) or None

        if not title and body_text:
            title = body_text.split("\n", 1)[0][:120]
        if not body_text:
            body_text = title or "[مطلب بدون متن]"

        posts.append(
            FetchedPost(
                post_guid=guid,
                post_url=post_url,
                title=title or "بدون عنوان",
                published_at=published_at,
                content=body_text,
                media_type=media_type,
                media_url=urljoin(post_url, media_url) if media_url else None,
                author=author,
                needs_article_fetch=len(body_text) < 260 and post_url != base_url,
            )
        )

    return feed_title, posts


def _parse_broken_xml_with_bs4(
    raw_xml: str,
    base_url: str,
    tz_name: str,
) -> Tuple[Optional[str], List[FetchedPost]]:
    soup = BeautifulSoup(raw_xml, "html.parser")
    if not (soup.find("rss") or soup.find("feed") or soup.find("item") or soup.find("entry")):
        raise CollectorError("صفحه دریافت‌شده یک فید معتبر RSS یا Atom نیست.")

    feed_title: Optional[str] = None
    channel_el = soup.find("channel") or soup.find("feed")
    if channel_el:
        t_el = channel_el.find("title", recursive=False)
        if t_el:
            feed_title = html_lib.unescape(t_el.get_text(strip=True)) or None

    items = soup.find_all("item") or soup.find_all("entry")
    posts: List[FetchedPost] = []

    for item in items:
        t_el = item.find("title")
        title = html_lib.unescape(t_el.get_text(strip=True)) if t_el else ""

        raw_link = ""
        link_el = item.find("link")
        if link_el:
            raw_link = str(link_el.get("href") or "").strip()
            if not raw_link:
                raw_link = link_el.get_text(strip=True)
                if not raw_link and link_el.next_sibling and isinstance(link_el.next_sibling, str):
                    raw_link = link_el.next_sibling.strip()

        guid_el = item.find("guid") or item.find("id")
        raw_guid = guid_el.get_text(strip=True) if guid_el else ""
        if not raw_link and raw_guid.startswith(("http://", "https://")):
            raw_link = raw_guid

        post_url = normalize_article_url(raw_link, base_url=base_url) or base_url
        guid = compute_post_guid(post_url, raw_guid=raw_guid, site_canonical_url=base_url)
        if not guid:
            continue

        content_el = item.find("content:encoded") or item.find("content") or item.find("description") or item.find("summary")
        raw_body = content_el.decode_contents() if content_el else ""
        body_text, media_url, media_type = html_fragment_to_text(raw_body)

        enc_el = item.find("enclosure")
        if enc_el and enc_el.get("url"):
            m_url = str(enc_el.get("url")).strip()
            m_type = _classify_mime_or_url(str(enc_el.get("type") or ""), m_url)
            if m_type != "text":
                media_url = m_url
                media_type = m_type

        pub_el = item.find("pubdate") or item.find("published") or item.find("updated") or item.find("dc:date")
        published_at = parse_any_datetime(pub_el.get_text(strip=True) if pub_el else None, tz_name=tz_name)

        if not title and body_text:
            title = body_text.split("\n", 1)[0][:120]
        if not body_text:
            body_text = title or "[مطلب بدون متن]"

        posts.append(
            FetchedPost(
                post_guid=guid,
                post_url=post_url,
                title=title or "بدون عنوان",
                published_at=published_at,
                content=body_text,
                media_type=media_type,
                media_url=urljoin(post_url, media_url) if media_url else None,
                needs_article_fetch=len(body_text) < 260 and post_url != base_url,
            )
        )

    return feed_title, posts
