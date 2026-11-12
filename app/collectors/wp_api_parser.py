from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.collectors.base import CollectorError, FetchedPost
from app.collectors.feed_parser import html_fragment_to_text
from app.utils.jalali import parse_any_datetime
from app.utils.site_parser import compute_post_guid, normalize_article_url

OG_SITE_NAME_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Page-builder shortcodes (WPBakery/Uncode `[vc_*]`, Divi `[et_pb_*]`, Avada `[fusion_*]`, MEI `[tm_*]`)
WP_RAW_BLOCK_SHORTCODE_RE = re.compile(
    r"\[(?:vc_raw_html|vc_raw_js|et_pb_code)[^\]]*\].*?\[/(?:vc_raw_html|vc_raw_js|et_pb_code)\]",
    re.IGNORECASE | re.DOTALL,
)
WP_SHORTCODE_TAG_RE = re.compile(
    r"\[/?(?:vc_[a-z0-9_]+|et_pb_[a-z0-9_]+|fusion_[a-z0-9_]+|avia_[a-z0-9_]+|tm_[a-z0-9_]+|uncode_[a-z0-9_]+)"
    r"(?:\s[^\]]*)?\]",
    re.IGNORECASE,
)

BUILTIN_NON_EDITORIAL_WP_TYPES = {
    "post",
    "posts",
    "page",
    "pages",
    "attachment",
    "media",
    "nav_menu_item",
    "menu-items",
    "wp_block",
    "blocks",
    "wp_template",
    "templates",
    "wp_template_part",
    "template-parts",
    "wp_global_styles",
    "global-styles",
    "wp_navigation",
    "navigation",
    "wp_font_family",
    "font-families",
    "wp_font_face",
    "font-faces",
    "person",
    "people",
    "staff",
    "expert",
    "experts",
    "author",
    "authors",
    "team",
    "event",
    "events",
    "project",
    "projects",
    "exhibition",
    "exhibitions",
}

EDITORIAL_CPT_PRIORITY_TOKENS = (
    "research",
    "publication",
    "article",
    "news",
    "analysis",
    "report",
    "commentary",
    "policy",
    "brief",
    "insight",
    "press",
    "podcast",
)


def strip_wp_shortcodes(raw_html: str) -> str:
    """Removes WPBakery/Uncode/Divi/Avada shortcodes while preserving inner prose paragraphs."""
    if not raw_html or "[" not in raw_html:
        return raw_html or ""
    cleaned = WP_RAW_BLOCK_SHORTCODE_RE.sub(" ", raw_html)
    cleaned = WP_SHORTCODE_TAG_RE.sub(" ", cleaned)
    return cleaned


def extract_wp_custom_type_endpoints(types_json_text: str, base_url: str) -> List[str]:
    """
    Parses `/wp-json/wp/v2/types` and returns prioritized editorial Custom Post Type
    collection URLs (e.g. `https://mei.edu/wp-json/wp/v2/research?per_page=20&_embed=1`)
    when a WordPress site stores articles outside default `posts`.
    """
    try:
        data = json.loads(types_json_text or "")
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    parsed_base = urlparse(base_url)
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}" if parsed_base.scheme and parsed_base.netloc else ""
    scored: List[Tuple[int, str]] = []

    for slug, info in data.items():
        if not isinstance(info, dict):
            continue
        slug_low = str(slug or "").strip().lower()
        rest_base = str(info.get("rest_base") or slug_low).strip().strip("/")
        rest_ns = str(info.get("rest_namespace") or "wp/v2").strip().strip("/")
        if not rest_base:
            continue
        if slug_low in BUILTIN_NON_EDITORIAL_WP_TYPES or rest_base.lower() in BUILTIN_NON_EDITORIAL_WP_TYPES:
            continue

        href = ""
        links = info.get("_links")
        if isinstance(links, dict):
            wp_items = links.get("wp:items")
            if isinstance(wp_items, list) and wp_items and isinstance(wp_items[0], dict):
                href = str(wp_items[0].get("href") or "").strip()
        if not href and origin:
            href = f"{origin}/wp-json/{rest_ns}/{rest_base}"
        if not href:
            continue

        combined_label = f"{slug_low} {rest_base.lower()} {str(info.get('name') or '').lower()}"
        rank = 50
        for idx, tok in enumerate(EDITORIAL_CPT_PRIORITY_TOKENS):
            if tok in combined_label:
                rank = idx
                break

        sep = "&" if "?" in href else "?"
        ep = f"{href}{sep}per_page=20&_embed=1"
        scored.append((rank, ep))

    scored.sort(key=lambda pair: pair[0])
    seen = set()
    result: List[str] = []
    for _, ep in scored:
        if ep not in seen:
            seen.add(ep)
            result.append(ep)
    return result[:4]


def is_wordpress_html(html: str) -> bool:
    head = (html or "")[:25000].lower()
    return any(
        marker in head
        for marker in (
            "api.w.org",
            "/wp-content/",
            "/wp-includes/",
            "/wp-json/",
            'content="wordpress',
        )
    )


def discover_wp_posts_api_url(
    html: str,
    page_url: str,
    *,
    is_root: bool = True,
) -> Optional[str]:
    """
    Discovers the WordPress REST API posts endpoint for either a WordPress root site
    or a WordPress category/tag archive page (via <link rel="alternate" type="application/json">).
    """
    if not html:
        return None

    soup = BeautifulSoup(html[:40000], "html.parser")
    api_root: Optional[str] = None

    for link in soup.find_all("link"):
        rels = [r.lower() for r in (link.get("rel") or [])]
        href = str(link.get("href") or "").strip()
        ltype = str(link.get("type") or "").lower()
        if not href:
            continue

        if "https://api.w.org/" in rels:
            api_root = urljoin(page_url, href).rstrip("/") + "/"

        # Category / tag term JSON link on archive pages: .../wp-json/wp/v2/categories/12
        if not is_root and "alternate" in rels and "application/json" in ltype and "/wp/v2/" in href:
            full_term = urljoin(page_url, href)
            m_cat = re.search(r"(.*/wp/v2)/(categories|tags)/(\d+)", full_term)
            if m_cat:
                base_v2, tax, term_id = m_cat.group(1), m_cat.group(2), m_cat.group(3)
                return f"{base_v2}/posts?{tax}={term_id}&per_page=20&_embed=1"

    if not is_root:
        return None

    if api_root:
        return urljoin(api_root, "wp/v2/posts?per_page=20&_embed=1")

    if is_wordpress_html(html):
        parsed = urlparse(page_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/wp-json/wp/v2/posts?per_page=20&_embed=1"

    return None


def is_wp_posts_json(text: str, content_type: str = "") -> bool:
    stripped = (text or "").lstrip()
    if not stripped.startswith("["):
        return False
    try:
        arr = json.loads(stripped)
    except Exception:
        return False
    if not isinstance(arr, list) or not arr:
        return False
    first = arr[0]
    return (
        isinstance(first, dict)
        and "link" in first
        and isinstance(first.get("title"), dict)
    )


def _strip_tags_inline(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(html_lib.unescape(raw_html), "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _extract_wp_media_and_site_title(
    item: Dict[str, Any], post_url: str
) -> Tuple[Optional[str], Optional[str]]:
    media_url: Optional[str] = None
    site_title: Optional[str] = None

    embedded = item.get("_embedded")
    if isinstance(embedded, dict):
        media_list = embedded.get("wp:featuredmedia")
        if isinstance(media_list, list) and media_list and isinstance(media_list[0], dict):
            src = str(media_list[0].get("source_url") or "").strip()
            if src:
                media_url = urljoin(post_url, src)

    yoast_json = item.get("yoast_head_json")
    if isinstance(yoast_json, dict):
        if not site_title and yoast_json.get("og_site_name"):
            site_title = html_lib.unescape(str(yoast_json["og_site_name"]).strip())
        if not media_url:
            og_imgs = yoast_json.get("og_image")
            if isinstance(og_imgs, list) and og_imgs and isinstance(og_imgs[0], dict):
                src = str(og_imgs[0].get("url") or "").strip()
                if src:
                    media_url = urljoin(post_url, src)

    yoast_head = str(item.get("yoast_head") or "")
    if yoast_head:
        if not site_title:
            m_site = OG_SITE_NAME_RE.search(yoast_head)
            if m_site:
                site_title = html_lib.unescape(m_site.group(1).strip())
        if not media_url:
            m_img = OG_IMAGE_RE.search(yoast_head)
            if m_img:
                media_url = urljoin(post_url, html_lib.unescape(m_img.group(1).strip()))

    return media_url, site_title


def _extract_acf_rich_html(item: Dict[str, Any]) -> str:
    """
    Extracts article body/abstract HTML stored in Advanced Custom Fields (`acf`) on
    headless/custom WordPress sites such as Middle East Institute (`mei.edu/wp-json/wp/v2/research`).
    """
    acf = item.get("acf")
    if not isinstance(acf, dict):
        return ""

    chunks: List[str] = []
    details = acf.get("details")
    if isinstance(details, dict):
        abstract = str(details.get("abstract") or details.get("summary") or "").strip()
        if abstract:
            chunks.append(f"<p>{abstract}</p>")

    page_layout = acf.get("page_layout")
    if isinstance(page_layout, list):
        for block in page_layout:
            if not isinstance(block, dict):
                continue
            for val in block.values():
                if isinstance(val, dict):
                    wysiwyg = str(val.get("content") or val.get("text") or val.get("body") or "").strip()
                    if len(wysiwyg) >= 25:
                        chunks.append(wysiwyg)
                elif isinstance(val, str) and len(val.strip()) >= 80:
                    chunks.append(val.strip())

    for key in ("content", "body", "article_body", "abstract", "summary", "description"):
        top_val = acf.get(key)
        if isinstance(top_val, str) and len(top_val.strip()) >= 25:
            chunks.append(top_val.strip())

    # Deduplicate identical paragraphs (e.g. when `abstract` repeats paragraph 1 of `component_wysiwyg`)
    if len(chunks) >= 2 and chunks[0].strip("<p></p> ")[:80] in chunks[1]:
        chunks = chunks[1:]

    return "\n\n".join(chunks)


def parse_wp_posts_json(
    raw_text: str,
    base_url: str,
    tz_name: str = "Asia/Tehran",
) -> Tuple[Optional[str], List[FetchedPost]]:
    """Parses WordPress REST API `/wp-json/wp/v2/posts` (or CPT) JSON array into FetchedPost items."""
    try:
        items = json.loads(raw_text)
    except Exception as exc:
        raise CollectorError(f"ساختار JSON وب‌سرویس وردپرس معتبر نیست: {exc}") from exc

    if not isinstance(items, list):
        raise CollectorError("پاسخ وب‌سرویس وردپرس آرایه‌ای از مطالب نیست.")

    detected_site_title: Optional[str] = None
    posts: List[FetchedPost] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_link = str(item.get("link") or "").strip()
        post_url = normalize_article_url(raw_link, base_url=base_url) or base_url

        guid_obj = item.get("guid")
        raw_guid = ""
        if isinstance(guid_obj, dict):
            raw_guid = str(guid_obj.get("rendered") or "").strip()
        elif isinstance(guid_obj, str):
            raw_guid = guid_obj.strip()
        if not raw_guid and item.get("id"):
            raw_guid = f"wp-post-{item['id']}"

        guid = compute_post_guid(post_url, raw_guid=raw_guid, site_canonical_url=base_url)
        if not guid:
            continue

        title_obj = item.get("title")
        raw_title = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj or "")
        title = _strip_tags_inline(strip_wp_shortcodes(str(raw_title)))

        content_obj = item.get("content")
        raw_content = content_obj.get("rendered", "") if isinstance(content_obj, dict) else ""
        excerpt_obj = item.get("excerpt")
        raw_excerpt = excerpt_obj.get("rendered", "") if isinstance(excerpt_obj, dict) else ""
        acf_html = _extract_acf_rich_html(item) if not str(raw_content or "").strip() else ""

        primary_html = strip_wp_shortcodes(str(raw_content or acf_html or raw_excerpt or ""))
        body_text, inline_media_url, inline_media_type = html_fragment_to_text(primary_html)
        if not body_text and raw_excerpt:
            body_text, _, _ = html_fragment_to_text(strip_wp_shortcodes(str(raw_excerpt)))

        feat_media_url, item_site_title = _extract_wp_media_and_site_title(item, post_url)
        if item_site_title and not detected_site_title:
            detected_site_title = item_site_title

        media_url = feat_media_url or (urljoin(post_url, inline_media_url) if inline_media_url else None)
        media_type = "photo" if feat_media_url else inline_media_type

        # date_gmt in WordPress REST API is always UTC
        published_at = None
        date_gmt = str(item.get("date_gmt") or "").strip()
        if date_gmt:
            if not date_gmt.endswith("Z") and "+" not in date_gmt:
                date_gmt = date_gmt + "Z"
            published_at = parse_any_datetime(date_gmt, tz_name="UTC")
        if published_at is None:
            published_at = parse_any_datetime(str(item.get("date") or ""), tz_name=tz_name)

        if not title and body_text:
            title = body_text.split("\n", 1)[0][:140]
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
                media_url=media_url,
                needs_article_fetch=len(body_text) < 220 and post_url != base_url,
            )
        )

    return detected_site_title, posts
