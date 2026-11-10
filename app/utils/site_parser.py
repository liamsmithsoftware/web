from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

PERSIAN_DIGITS_MAP = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

INVISIBLE_CHARS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "spm",
}

DIRECT_FEED_EXTENSIONS = (".rss", ".atom", ".xml", ".rdf", ".jsonfeed")
DIRECT_FEED_SEGMENTS = {"rss", "feed", "feeds", "atom", "rss2", "rdf"}


@dataclass(frozen=True)
class ParsedSite:
    site_key: str
    site_input: str
    domain: str
    canonical_url: str
    feed_url: Optional[str]
    default_title: str
    is_direct_feed: bool


def normalize_digits(text: str) -> str:
    return (text or "").translate(PERSIAN_DIGITS_MAP)


def clean_raw_input(raw: str) -> str:
    cleaned = INVISIBLE_CHARS_RE.sub("", raw or "")
    return normalize_digits(cleaned).strip()


def _is_valid_hostname(hostname: str) -> bool:
    if not hostname:
        return False
    host = hostname.strip().lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    # Handle IDN (e.g. Persian domains) via punycode
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False

    if "." not in ascii_host or len(ascii_host) > 253:
        return False

    labels = ascii_host.split(".")
    tld = labels[-1]
    if len(tld) < 2 or tld.isdigit():
        return False

    return all(bool(DOMAIN_LABEL_RE.fullmatch(lbl)) for lbl in labels)


def _strip_www(host: str) -> str:
    lowered = host.lower()
    if lowered.startswith("www.") and len(lowered) > 4:
        return lowered[4:]
    return lowered


def _clean_query(query: str) -> str:
    if not query:
        return ""
    pairs = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=False)
        if k.lower() not in TRACKING_QUERY_PARAMS
    ]
    pairs.sort(key=lambda item: (item[0].lower(), item[1]))
    return urlencode(pairs, doseq=True)


def looks_like_feed_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower().rstrip("/")
    if not path:
        return False
    if path.endswith(DIRECT_FEED_EXTENSIONS):
        # Exclude sitemap.xml unless explicitly rss/feed/atom
        if path.endswith("sitemap.xml") or path.endswith("robots.txt"):
            return False
        return True
    segments = [seg for seg in path.split("/") if seg]
    if segments and segments[-1] in DIRECT_FEED_SEGMENTS:
        return True
    if "feed=" in (parsed.query or "").lower() or "format=rss" in (parsed.query or "").lower():
        return True
    return False


def parse_site_input(raw_input: str) -> ParsedSite:
    """
    Parse and validate any website / blog / news section / RSS feed URL:
    - https://www.zoomit.ir  or  zoomit.ir
    - https://www.isna.ir/service/economy
    - https://example.com/feed  or  https://example.com/rss.xml
    - http://localhost:8080/news (for local sites)
    """
    cleaned = clean_raw_input(raw_input)
    if not cleaned:
        raise ValueError("لطفاً آدرس وب‌سایت را وارد کنید.")

    if cleaned.startswith("@"):
        raise ValueError(
            "لطفاً به جای نام کاربری با @، آدرس کامل وب‌سایت (مانند https://example.com) را وارد کنید."
        )

    if any(ch.isspace() for ch in cleaned):
        raise ValueError("آدرس وب‌سایت نباید شامل فاصله (Space) باشد.")

    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", cleaned)
    if scheme_match and not re.match(r"^https?://", cleaned, flags=re.IGNORECASE):
        # Allow host:port like localhost:8000 or example.com:8080
        possible_scheme = scheme_match.group(1).lower()
        after_colon = cleaned[len(possible_scheme) + 1 :]
        if not (after_colon and after_colon.split("/", 1)[0].isdigit()):
            raise ValueError(
                "پروتکل آدرس معتبر نیست. فقط آدرس‌های وب با http:// یا https:// پشتیبانی می‌شوند."
            )
        url_candidate = f"http://{cleaned}" if possible_scheme == "localhost" else f"https://{cleaned}"
    elif not re.match(r"^https?://", cleaned, flags=re.IGNORECASE):
        default_scheme = "http" if cleaned.lower().startswith(("localhost", "127.0.0.1")) else "https"
        url_candidate = f"{default_scheme}://{cleaned}"
    else:
        url_candidate = cleaned

    parsed = urlparse(url_candidate)
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise ValueError("فقط لینک‌های وب با پروتکل http یا https مجاز هستند.")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not _is_valid_hostname(hostname):
        raise ValueError(
            "دامنه وب‌سایت معتبر نیست. لطفاً آدرس معتبری مانند https://example.com وارد کنید."
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("شماره پورت در آدرس وب‌سایت معتبر نیست.") from exc

    is_default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname if (port is None or is_default_port) else f"{hostname}:{port}"

    raw_path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path

    clean_path = "/" if raw_path == "/" else raw_path.rstrip("/")
    clean_query = _clean_query(parsed.query or "")

    canonical_url = urlunparse(
        (
            scheme,
            netloc,
            clean_path,
            "",
            clean_query,
            "",
        )
    )

    bare_domain = _strip_www(hostname)
    key_host = bare_domain if (port is None or is_default_port) else f"{bare_domain}:{port}"
    key_path = "" if clean_path == "/" else clean_path.lower()
    site_key = f"{key_host}{key_path}"
    if clean_query:
        site_key = f"{site_key}?{clean_query.lower()}"

    is_feed = looks_like_feed_url(canonical_url)
    feed_url = canonical_url if is_feed else None

    if clean_path == "/" and not clean_query:
        default_title = bare_domain
    else:
        short_path = clean_path.rstrip("/")
        if len(short_path) > 36:
            short_path = short_path[:33] + "..."
        default_title = f"{bare_domain}{short_path}"

    return ParsedSite(
        site_key=site_key,
        site_input=cleaned,
        domain=bare_domain,
        canonical_url=canonical_url,
        feed_url=feed_url,
        default_title=default_title,
        is_direct_feed=is_feed,
    )


def normalize_article_url(url: str, base_url: Optional[str] = None) -> str:
    """Resolve relative links, strip tracking params & fragments, and normalize URL."""
    raw = clean_raw_input(url)
    if not raw:
        return ""
    resolved = urljoin(base_url, raw) if base_url else raw
    parsed = urlparse(resolved)
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return ""

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return ""

    try:
        port = parsed.port
    except ValueError:
        port = None

    is_default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname if (port is None or is_default_port) else f"{hostname}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    if path != "/":
        path = path.rstrip("/")

    query = _clean_query(parsed.query or "")
    return urlunparse((scheme, netloc, path, "", query, ""))


def compute_post_guid(
    post_url: str,
    raw_guid: Optional[str] = None,
    site_canonical_url: Optional[str] = None,
) -> str:
    """
    Produces a deterministic, protocol/www-agnostic deduplication key for an article
    so both RSS feeds and HTML scrapers generate the exact same key for the same URL.
    """
    norm_url = normalize_article_url(post_url, base_url=site_canonical_url)
    if norm_url:
        parsed = urlparse(norm_url)
        host = _strip_www(parsed.hostname or "")
        try:
            port = parsed.port
        except ValueError:
            port = None
        is_default_port = port in (None, 80, 443)
        host_key = host if is_default_port else f"{host}:{port}"
        path_key = (parsed.path or "/").rstrip("/") or "/"
        url_key = f"{host_key}{path_key}"
        if parsed.query:
            url_key = f"{url_key}?{parsed.query}"

        if site_canonical_url:
            norm_site = normalize_article_url(site_canonical_url)
            site_parsed = urlparse(norm_site)
            site_host = _strip_www(site_parsed.hostname or "")
            site_path = (site_parsed.path or "/").rstrip("/") or "/"
            site_key = f"{site_host}{site_path}"
            if site_parsed.query:
                site_key = f"{site_key}?{site_parsed.query}"
            if url_key == site_key and raw_guid and raw_guid.strip():
                return f"{url_key}#guid:{raw_guid.strip()}"

        return url_key

    if raw_guid and raw_guid.strip():
        return f"guid:{raw_guid.strip()}"
    return ""
