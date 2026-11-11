from __future__ import annotations

import copy
import html as html_lib
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.collectors.base import CollectorError, FetchedPost
from app.utils.jalali import parse_any_datetime
from app.utils.site_parser import (
    compute_post_guid,
    extract_registrable_domain,
    normalize_article_url,
    normalize_digits,
)

SKIP_PATH_PATTERNS = re.compile(
    r"(?:^|/)(?:"
    r"login|signin|sign-in|signup|sign-up|register|logout|account|profile|cart|checkout|"
    r"contact|contact-us|about|about-us|privacy|privacy-policy|terms|faq|ads|advertise|"
    r"donate|subscribe|newsletter|careers|jobs|press-kit|capabilities|sectors|services|partners|"
    r"solutions|products|featured-insights|"
    r"search|wp-login\.php|wp-admin|wp-json|xmlrpc\.php|feed|rss|atom|sitemap|"
    r"tag|tags|issue|issues|topic|topics|vertical|verticals|category|categories|team|staff|experts|"
    r"author|authors|contributor|contributors|users|webusers|comment|comments|reply"
    r")(?:/|$|\?)",
    re.IGNORECASE,
)

SECTION_HUB_SLUGS = {
    "news",
    "articles",
    "article",
    "posts",
    "post",
    "blog",
    "reports",
    "report",
    "analysis",
    "research",
    "insights",
    "publications",
    "podcasts",
    "trackers",
    "analyst-reports",
    "webinars",
    "events",
    "press",
    "press-releases",
    "media",
    "videos",
    "opinions",
    "commentary",
    "briefs",
    "highlights",
    "archive",
    "archives",
}

EXCLUDED_SUBDOMAIN_PREFIXES = (
    "accounts.",
    "account.",
    "login.",
    "auth.",
    "sso.",
    "id.",
    "mail.",
    "shop.",
    "store.",
    "donate.",
    "careers.",
    "jobs.",
    "status.",
    "support.",
    "help.",
)

NAV_ANCHOR_TEXT_RE = re.compile(
    r"^(?:view\s+all|see\s+all|read\s+all|browse\s+all|show\s+all|explore\s+all|"
    r"all\s+articles|all\s+posts|all\s+news|all\s+reports|more\s+news|load\s+more|"
    r"read\s+more|learn\s+more|مشاهده\s+همه|همه\s+اخبار|همه\s+مطالب|بیشتر\s+بخوانید|ادامه\s+مطلب)\b",
    re.IGNORECASE,
)

LEADING_BADGE_RE = re.compile(
    r"^(?:article\s+|related:\s*|featured\s+|new:\s*)",
    re.IGNORECASE,
)

STATIC_ASSET_EXT_RE = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|svg|ico|css|js|mjs|woff|woff2|ttf|eot|mp4|mp3|zip|rar|exe|apk)$",
    re.IGNORECASE,
)

ARTICLE_HINT_URL_RE = re.compile(
    r"(?:/\d{4}/\d{1,2}/|/\d{4,}|"
    r"/(?:news|post|posts|article|articles|blog|story|content|fa/news|detail|view|"
    r"online-analysis|strategic-comments|military-balance|"
    r"trackers|tracker|analyst-reports|analyst-report|company-news|webinars|resources|"
    r"analysis|podcasts|podcast|in_the_news|research|reports|report|insights|insight|"
    r"publications|publication|commentary|opinions|opinion|briefs|brief|press-releases)/|"
    r"[?&](?:p|id|newsid)=\d+)",
    re.IGNORECASE,
)

NOISE_CONTAINER_SELECTOR = (
    "script, style, noscript, iframe, svg, form, nav, footer, "
    ".navbar, .menu, .main-menu, .site-footer, .breadcrumb, .breadcrumbs, "
    ".pagination, .pager, .comments, #comments, .share, .social-share, "
    ".related-posts, .related-news, .advertisement, .ads"
)

ARTICLE_BODY_SELECTORS = (
    '[itemprop="articleBody"]',
    "article .entry-content",
    "article .post-content",
    ".article-body",
    ".article-content",
    ".news-body",
    ".news-text",
    ".story-body",
    ".entry-content",
    ".post-content",
    ".single-content",
    ".item-body",
    ".content-body",
    "article",
    "main",
)


def parse_views_text(raw_views: Optional[str]) -> Optional[int]:
    if not raw_views:
        return None
    text = normalize_digits(raw_views).strip().upper().replace(",", "").replace("٬", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([KM])?", text)
    if not m:
        return None
    try:
        val = float(m.group(1))
        suffix = m.group(2)
        if suffix == "K":
            return int(val * 1_000)
        if suffix == "M":
            return int(val * 1_000_000)
        return int(val)
    except ValueError:
        return None


def _same_site_domain(url_a: str, url_b: str) -> bool:
    host_a = (urlparse(url_a).hostname or "").lower().rstrip(".")
    host_b = (urlparse(url_b).hostname or "").lower().rstrip(".")
    if not host_a or not host_b:
        return False
    bare_a = host_a[4:] if host_a.startswith("www.") else host_a
    bare_b = host_b[4:] if host_b.startswith("www.") else host_b
    if bare_a == bare_b:
        return True
    if bare_a != bare_b and bare_a.startswith(EXCLUDED_SUBDOMAIN_PREFIXES):
        return False
    reg_a = extract_registrable_domain(bare_a)
    reg_b = extract_registrable_domain(bare_b)
    return bool(reg_a and reg_a == reg_b)


def _is_candidate_article_url(candidate_url: str, page_url: str) -> bool:
    if not candidate_url:
        return False
    if candidate_url == page_url:
        return False
    if not _same_site_domain(candidate_url, page_url):
        return False

    parsed = urlparse(candidate_url)
    path = parsed.path or "/"
    if path in ("", "/") and not parsed.query:
        return False
    if STATIC_ASSET_EXT_RE.search(path):
        return False
    if SKIP_PATH_PATTERNS.search(path):
        return False
    # Skip pagination links like /page/2 or ?page=2
    if re.search(r"(?:/page/\d+/?$|[?&]page=\d+)", candidate_url, re.IGNORECASE):
        return False

    # Skip bare section index hubs like /reports, /analysis, /news, or /article/highlights
    segments = [seg.lower() for seg in path.split("/") if seg]
    if not parsed.query:
        if len(segments) == 1 and segments[0] in SECTION_HUB_SLUGS:
            return False
        if (
            len(segments) == 2
            and segments[0] in SECTION_HUB_SLUGS
            and segments[1] in ("highlights", "all", "archive", "archives", "latest")
        ):
            return False

    return True


def _extract_site_title(soup: BeautifulSoup) -> Optional[str]:
    og_site = soup.select_one('meta[property="og:site_name"]')
    if og_site and og_site.get("content"):
        val = str(og_site["content"]).strip()
        if val:
            return html_lib.unescape(val)

    title_el = soup.find("title")
    if title_el:
        raw = html_lib.unescape(title_el.get_text(" ", strip=True))
        if raw:
            # Strip trailing slogans after | or — if overly long
            parts = re.split(r"\s+[|–—]\s+", raw)
            return parts[0].strip() if len(parts[0].strip()) >= 3 else raw[:80]
    return None


def _extract_img_url(container: Tag, base_url: str) -> Optional[str]:
    img = container.find("img")
    if not img:
        return None
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        val = img.get(attr)
        if val and not str(val).strip().startswith("data:"):
            return urljoin(base_url, str(val).strip())
    srcset = img.get("srcset")
    if srcset:
        first = str(srcset).split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return urljoin(base_url, first)
    return None


def _is_single_card_box(box: Optional[Tag]) -> bool:
    if box is None:
        return False
    txt = box.get_text(" ", strip=True)
    if len(txt) > 800:
        return False
    if len(box.find_all(["h1", "h2", "h3", "h4"])) > 2:
        return False
    return len(box.find_all("a", href=True)) <= 5


def _clean_headline_text(raw_title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (raw_title or "")).strip()
    cleaned = LEADING_BADGE_RE.sub("", cleaned).strip()
    return cleaned


def _extract_clean_title_from_anchor(
    a_tag: Tag,
    fallback_container: Optional[Tag] = None,
) -> Tuple[str, bool, str]:
    """
    Returns (clean_title, is_emphasized_heading, full_anchor_text).
    Handles cards where <a> wraps badge + <h3>/<strong> + summary + date.
    """
    full_anchor_text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True)).strip()

    h_inside = a_tag.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    title_el = h_inside or a_tag.select_one('[class*="title"], [class*="headline"], [class*="heading"]')
    if not title_el:
        strong_el = a_tag.find(["strong", "b"])
        if strong_el and len(strong_el.get_text(" ", strip=True)) >= 10:
            title_el = strong_el

    if title_el and len(title_el.get_text(" ", strip=True)) >= 6:
        title = _clean_headline_text(title_el.get_text(" ", strip=True))
        return title, True, full_anchor_text

    if fallback_container is not None:
        h_card = fallback_container.find(["h1", "h2", "h3", "h4"])
        if h_card and len(h_card.get_text(" ", strip=True)) >= 6:
            title = _clean_headline_text(h_card.get_text(" ", strip=True))
            return title, True, full_anchor_text

    in_parent_heading = a_tag.find_parent(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None
    return _clean_headline_text(full_anchor_text), in_parent_heading, full_anchor_text


def _extract_best_card_datetime(
    a_tag: Tag,
    card_box: Optional[Tag],
    full_anchor_text: str,
    tz_name: str,
) -> Optional[datetime]:
    pub_dt = _extract_datetime_from_tag(a_tag, tz_name=tz_name)
    if pub_dt is None and card_box is not None and card_box is not a_tag:
        pub_dt = _extract_datetime_from_tag(card_box, tz_name=tz_name)
    if pub_dt is None and full_anchor_text:
        # Scan tail first (bylines like "...By Author 6th November 2026" or "...2 days ago"), then head
        tail_snippet = full_anchor_text[-140:] if len(full_anchor_text) > 140 else ""
        pub_dt = (
            parse_any_datetime(tail_snippet, tz_name=tz_name)
            or parse_any_datetime(full_anchor_text[:240], tz_name=tz_name)
        )
    if pub_dt is None and card_box is not None and card_box is not a_tag and _is_single_card_box(card_box):
        box_text = re.sub(r"\s+", " ", card_box.get_text(" ", strip=True)).strip()
        tail_box = box_text[-140:] if len(box_text) > 140 else ""
        pub_dt = (
            parse_any_datetime(tail_box, tz_name=tz_name)
            or parse_any_datetime(box_text[:260], tz_name=tz_name)
        )
    return pub_dt


def _extract_datetime_from_tag(container: Tag, tz_name: str) -> Optional[datetime]:
    time_el = container.find("time")
    if time_el:
        dt_attr = time_el.get("datetime") or time_el.get("content")
        if dt_attr:
            dt = parse_any_datetime(str(dt_attr), tz_name=tz_name)
            if dt:
                return dt
        dt_text = parse_any_datetime(time_el.get_text(" ", strip=True), tz_name=tz_name)
        if dt_text:
            return dt_text

    for selector in (
        '[itemprop="datePublished"]',
        '[class*="date"]',
        '[class*="time"]',
        '[class*="publish"]',
    ):
        el = container.select_one(selector)
        if el:
            raw = str(el.get("datetime") or el.get("content") or el.get_text(" ", strip=True))
            dt = parse_any_datetime(raw, tz_name=tz_name)
            if dt:
                return dt
    return None


def _extract_jsonld_items(
    soup: BeautifulSoup,
    page_url: str,
    tz_name: str,
) -> List[FetchedPost]:
    posts: List[FetchedPost] = []
    seen_guids = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, list):
            for entry in obj:
                _walk(entry)
            return
        if not isinstance(obj, dict):
            return

        if "@graph" in obj:
            _walk(obj["@graph"])

        raw_type = obj.get("@type")
        types = [str(t).lower() for t in raw_type] if isinstance(raw_type, list) else [str(raw_type or "").lower()]

        if "itemlist" in types and isinstance(obj.get("itemListElement"), list):
            for el in obj["itemListElement"]:
                if isinstance(el, dict) and "item" in el:
                    _walk(el["item"])
                else:
                    _walk(el)
            return

        if any(t in ("newsarticle", "article", "blogposting", "reportagenewsarticle") for t in types):
            raw_url = str(obj.get("url") or obj.get("mainEntityOfPage") or "")
            if isinstance(obj.get("mainEntityOfPage"), dict):
                raw_url = str(obj["mainEntityOfPage"].get("@id") or obj["mainEntityOfPage"].get("url") or "")
            norm_url = normalize_article_url(raw_url, base_url=page_url)
            if not _is_candidate_article_url(norm_url, page_url):
                return

            guid = compute_post_guid(norm_url, site_canonical_url=page_url)
            if not guid or guid in seen_guids:
                return
            seen_guids.add(guid)

            title = html_lib.unescape(str(obj.get("headline") or obj.get("name") or "").strip())
            body = html_lib.unescape(str(obj.get("articleBody") or obj.get("description") or "").strip())
            if not title and not body:
                return

            pub_dt = parse_any_datetime(
                str(obj.get("datePublished") or obj.get("dateCreated") or ""),
                tz_name=tz_name,
            )

            img_url: Optional[str] = None
            img_field = obj.get("image")
            if isinstance(img_field, str):
                img_url = urljoin(norm_url, img_field)
            elif isinstance(img_field, list) and img_field:
                first_img = img_field[0]
                if isinstance(first_img, str):
                    img_url = urljoin(norm_url, first_img)
                elif isinstance(first_img, dict) and first_img.get("url"):
                    img_url = urljoin(norm_url, str(first_img["url"]))
            elif isinstance(img_field, dict) and img_field.get("url"):
                img_url = urljoin(norm_url, str(img_field["url"]))

            posts.append(
                FetchedPost(
                    post_guid=guid,
                    post_url=norm_url,
                    title=title or body[:100],
                    published_at=pub_dt,
                    content=body or title,
                    media_type="photo" if img_url else "text",
                    media_url=img_url,
                    needs_article_fetch=len(body) < 260,
                )
            )

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw_json = (script.string or script.get_text() or "").strip()
        if not raw_json:
            continue
        try:
            parsed = json.loads(raw_json)
            _walk(parsed)
        except Exception:
            continue

    return posts


def extract_listing_posts(
    html: str,
    page_url: str,
    tz_name: str = "Asia/Tehran",
) -> Tuple[Optional[str], List[FetchedPost]]:
    """
    Extracts article items from a website's homepage, blog index, or news section HTML.
    Preserves top-to-bottom DOM order (index 0 = newest item on the page).
    """
    if not html or not html.strip():
        raise CollectorError("پاسخ دریافت‌شده از وب‌سایت خالی است.")

    soup = BeautifulSoup(html, "html.parser")
    norm_page_url = normalize_article_url(page_url) or page_url
    site_title = _extract_site_title(soup)

    jsonld_posts = _extract_jsonld_items(soup, norm_page_url, tz_name=tz_name)
    posts_by_guid: Dict[str, FetchedPost] = {p.post_guid: p for p in jsonld_posts}
    ordered_guids: List[str] = [p.post_guid for p in jsonld_posts]

    # Work on a cleaned DOM copy so menus/footers/ads do not pollute extracted articles
    work = copy.copy(soup)
    for bad in work.select(NOISE_CONTAINER_SELECTOR):
        bad.decompose()

    # Pass 1: Semantic <article> & structured news/blog cards
    card_containers: List[Tag] = []
    seen_nodes = set()
    for sel in (
        "article",
        '[itemtype*="Article"]',
        '[itemtype*="BlogPosting"]',
        ".post-item",
        ".news-item",
        ".article-item",
        ".article-card",
        ".news-card",
        ".post-card",
        ".blog-post",
        ".entry",
        ".story",
        "li.news",
        "li.post",
    ):
        for el in work.select(sel):
            if id(el) not in seen_nodes:
                seen_nodes.add(id(el))
                card_containers.append(el)

    for card in card_containers:
        # Prefer heading anchor inside card, otherwise longest meaningful anchor
        heading_link = card.select_one("h1 a[href], h2 a[href], h3 a[href], h4 a[href], .title a[href], a.title[href]")
        chosen_a = heading_link
        if not chosen_a:
            anchors = [
                a
                for a in card.find_all("a", href=True)
                if len(a.get_text(" ", strip=True)) >= 8
            ]
            chosen_a = max(anchors, key=lambda a: len(a.get_text(" ", strip=True)), default=None)
        if not chosen_a:
            continue

        norm_url = normalize_article_url(str(chosen_a.get("href") or ""), base_url=norm_page_url)
        if not _is_candidate_article_url(norm_url, norm_page_url):
            continue

        guid = compute_post_guid(norm_url, site_canonical_url=norm_page_url)
        if not guid or guid in posts_by_guid:
            continue

        title, _, full_anchor_text = _extract_clean_title_from_anchor(chosen_a, fallback_container=card)
        if len(title) < 4 or (len(title) <= 45 and NAV_ANCHOR_TEXT_RE.search(title)):
            continue

        # Extract summary paragraphs inside card
        summary_parts: List[str] = []
        for p_el in card.select("p, .summary, .excerpt, .lead, .desc, .description"):
            txt = p_el.get_text(" ", strip=True)
            if txt and txt != title and len(txt) >= 15 and txt not in summary_parts:
                summary_parts.append(txt)
        summary = "\n".join(summary_parts[:2]).strip()
        content = f"{title}\n\n{summary}".strip() if summary else title

        pub_dt = _extract_best_card_datetime(chosen_a, card, full_anchor_text, tz_name=tz_name)
        img_url = _extract_img_url(card, norm_url)

        views_el = card.select_one('[class*="view"], [class*="visit"]')
        views = parse_views_text(views_el.get_text(" ", strip=True) if views_el else None)

        post = FetchedPost(
            post_guid=guid,
            post_url=norm_url,
            title=title[:240],
            published_at=pub_dt,
            content=content,
            media_type="photo" if img_url else "text",
            media_url=img_url,
            views=views,
            needs_article_fetch=len(summary) < 220,
        )
        posts_by_guid[guid] = post
        ordered_guids.append(guid)

    # Pass 2: Heading links (h1/h2/h3/h4 > a) or article-like URLs if page uses custom divs
    if len(ordered_guids) < 5:
        for a_tag in work.find_all("a", href=True):
            norm_url = normalize_article_url(str(a_tag.get("href") or ""), base_url=norm_page_url)
            if not _is_candidate_article_url(norm_url, norm_page_url):
                continue

            guid = compute_post_guid(norm_url, site_canonical_url=norm_page_url)
            if not guid or guid in posts_by_guid:
                continue

            text, in_heading, full_anchor_text = _extract_clean_title_from_anchor(a_tag)
            if len(text) <= 45 and NAV_ANCHOR_TEXT_RE.search(text):
                continue

            url_looks_article = bool(ARTICLE_HINT_URL_RE.search(norm_url)) or norm_url.count("/") >= 4

            # Require meaningful headline length so navigation items like "ورزشی" or "ادامه" are skipped
            min_len = 8 if in_heading else (14 if url_looks_article else 24)
            if len(text) < min_len:
                continue

            parent_box = a_tag.find_parent(["li", "div", "section"]) or a_tag
            single_box = _is_single_card_box(parent_box)

            summary_source = parent_box if (parent_box is not a_tag and single_box) else a_tag
            card_summary_parts = [
                p_el.get_text(" ", strip=True)
                for p_el in summary_source.find_all("p")
                if len(p_el.get_text(" ", strip=True)) >= 20
                and p_el.get_text(" ", strip=True) != text
            ]
            card_content = f"{text}\n\n{card_summary_parts[0]}".strip() if card_summary_parts else text

            pub_dt = _extract_best_card_datetime(a_tag, parent_box, full_anchor_text, tz_name=tz_name)
            img_url = _extract_img_url(parent_box if single_box else a_tag, norm_url)

            post = FetchedPost(
                post_guid=guid,
                post_url=norm_url,
                title=text[:240],
                published_at=pub_dt,
                content=card_content,
                media_type="photo" if img_url else "text",
                media_url=img_url,
                needs_article_fetch=len(card_content) < 240,
            )
            posts_by_guid[guid] = post
            ordered_guids.append(guid)

    result_posts = [posts_by_guid[g] for g in ordered_guids if g in posts_by_guid]
    if not result_posts:
        raise CollectorError(
            "هیچ مطلب یا لینک خبری در این صفحه شناسایی نشد. "
            "لطفاً آدرس صفحه اخبار/مقالات یا فید RSS وب‌سایت را وارد کنید."
        )

    return site_title, result_posts


def extract_article_details(
    html: str,
    article_url: str,
    tz_name: str = "Asia/Tehran",
) -> Dict[str, Any]:
    """
    Extracts the full clean body text, headline, publication timestamp, and media
    from an individual article's HTML page.
    """
    soup = BeautifulSoup(html, "html.parser")

    title: Optional[str] = None
    published_at: Optional[datetime] = None
    body_text: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    author: Optional[str] = None

    # 1. Check JSON-LD NewsArticle / Article / BlogPosting
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw_json = (script.string or script.get_text() or "").strip()
        if not raw_json:
            continue
        try:
            data = json.loads(raw_json)
        except Exception:
            continue
        candidates = data if isinstance(data, list) else (data.get("@graph", [data]) if isinstance(data, dict) else [])
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            raw_type = obj.get("@type")
            types = [str(t).lower() for t in raw_type] if isinstance(raw_type, list) else [str(raw_type or "").lower()]
            if any("article" in t or "posting" in t for t in types):
                if not title and obj.get("headline"):
                    title = html_lib.unescape(str(obj["headline"]).strip())
                if not published_at:
                    published_at = parse_any_datetime(
                        str(obj.get("datePublished") or obj.get("dateCreated") or ""),
                        tz_name=tz_name,
                    )
                if not body_text and obj.get("articleBody"):
                    candidate_body = html_lib.unescape(str(obj["articleBody"]).strip())
                    if len(candidate_body) >= 80:
                        body_text = candidate_body

    # 2. Meta tags (OpenGraph / article:published_time / description)
    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(" ", strip=True):
            title = h1.get_text(" ", strip=True)
    if not title:
        og_title = soup.select_one('meta[property="og:title"], meta[name="twitter:title"]')
        if og_title and og_title.get("content"):
            title = html_lib.unescape(str(og_title["content"]).strip())

    if not published_at:
        for meta_sel in (
            'meta[property="article:published_time"]',
            'meta[name="pubdate"]',
            'meta[name="publishdate"]',
            'meta[name="publish-date"]',
            'meta[itemprop="datePublished"]',
            'meta[name="DC.date.issued"]',
        ):
            m_el = soup.select_one(meta_sel)
            if m_el and m_el.get("content"):
                published_at = parse_any_datetime(str(m_el["content"]), tz_name=tz_name)
                if published_at:
                    break

    if not published_at:
        published_at = _extract_datetime_from_tag(soup, tz_name=tz_name)

    # Media detection (video > audio > document > og:image)
    og_video = soup.select_one('meta[property="og:video"], meta[property="og:video:url"]')
    if og_video and og_video.get("content"):
        media_type = "video"
        media_url = urljoin(article_url, str(og_video["content"]).strip())
    elif soup.select_one("article video, main video"):
        v_el = soup.select_one("article video, main video")
        src = (v_el.get("src") if v_el else None) or (
            v_el.find("source").get("src") if v_el and v_el.find("source") else None
        )
        media_type = "video"
        media_url = urljoin(article_url, str(src)) if src else None
    elif soup.select_one("article audio, main audio"):
        a_el = soup.select_one("article audio, main audio")
        src = (a_el.get("src") if a_el else None) or (
            a_el.find("source").get("src") if a_el and a_el.find("source") else None
        )
        media_type = "voice"
        media_url = urljoin(article_url, str(src)) if src else None
    else:
        doc_link = soup.select_one('article a[href$=".pdf"], main a[href$=".pdf"], article a[href$=".zip"]')
        if doc_link and doc_link.get("href"):
            media_type = "document"
            media_url = urljoin(article_url, str(doc_link["href"]).strip())
        else:
            og_img = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
            if og_img and og_img.get("content"):
                media_type = "photo"
                media_url = urljoin(article_url, str(og_img["content"]).strip())

    # 3. Extract main article text paragraphs
    if not body_text:
        work = copy.copy(soup)
        for bad in work.select(NOISE_CONTAINER_SELECTOR):
            bad.decompose()

        best_text = ""
        for sel in ARTICLE_BODY_SELECTORS:
            for container in work.select(sel):
                blocks: List[str] = []
                # Include lead/subtitle + paragraphs + subheadings + list items
                for node in container.find_all(["p", "h2", "h3", "blockquote", "li"]):
                    txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                    if len(txt) >= 20 and txt not in blocks:
                        if node.name == "li":
                            txt = f"• {txt}"
                        blocks.append(txt)
                candidate = "\n\n".join(blocks).strip()
                if not candidate:
                    raw_c = container.get_text("\n", strip=True)
                    lines = [ln.strip() for ln in raw_c.splitlines() if len(ln.strip()) >= 25]
                    candidate = "\n\n".join(lines)
                if len(candidate) > len(best_text):
                    best_text = candidate
            if len(best_text) >= 250:
                break

        if best_text:
            body_text = best_text

    if not body_text:
        desc_meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
        if desc_meta and desc_meta.get("content"):
            body_text = html_lib.unescape(str(desc_meta["content"]).strip())

    return {
        "title": title,
        "published_at": published_at,
        "content": body_text,
        "media_type": media_type,
        "media_url": media_url,
        "author": author,
    }
