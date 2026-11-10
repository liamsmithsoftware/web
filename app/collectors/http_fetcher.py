from __future__ import annotations

import ssl
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.collectors.base import (
    CollectorError,
    build_httpx_proxy_url,
    build_request_headers,
)
from app.config import DEFAULT_REQUEST_TIMEOUT

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession  # type: ignore

    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover
    CurlAsyncSession = None  # type: ignore
    HAS_CURL_CFFI = False

try:
    import h2 as _h2_mod  # noqa: F401

    HAS_HTTP2 = True
except Exception:  # pragma: no cover
    HAS_HTTP2 = False

# Modern Chrome-like TLS cipher preference for standard OpenSSL / httpx
CHROME_LIKE_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"
)

WAF_CHALLENGE_STATUSES = {401, 403, 406, 429, 503}

WAF_BODY_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "__cf_chl_",
    "enable javascript and cookies to continue",
    "sucuri website firewall",
    "access denied |",
)


@dataclass
class HttpFetchResponse:
    url: str
    status_code: int
    text: str
    headers: Dict[str, str]
    via: str = "httpx"

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            if self.status_code == 403:
                raise CollectorError(
                    "سرور وب‌سایت دسترسی را مسدود کرد (HTTP 403 - فایروال/Cloudflare). "
                    "پیشنهاد: ۱) اگر سایت فیلتر یا محدود به IP است، پروکسی را در تنظیمات فعال کنید؛ "
                    "۲) یا کوکی مرورگر (cf_clearance) را در تنظیمات قرار دهید؛ "
                    "۳) یا آدرس مستقیم فید RSS سایت (مثلاً /feed/) را وارد کنید."
                )
            raise CollectorError(
                f"سرور وب‌سایت کد وضعیت ناموفق برگرداند (HTTP {self.status_code})."
            )


def _is_loopback_target(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _normalize_system_proxy_url(raw_proxy: str) -> Optional[str]:
    val = (raw_proxy or "").strip()
    if not val:
        return None
    if "://" not in val:
        val = f"http://{val}"
    parsed = urlparse(val)
    if not parsed.hostname or not parsed.port:
        return None
    scheme = (parsed.scheme or "http").lower()
    if scheme == "socks":
        scheme = "socks5"
    if scheme not in ("http", "https", "socks5", "socks5h"):
        return None
    return f"{scheme}://{parsed.netloc}"


def detect_os_system_proxy() -> Optional[str]:
    """
    Detects active OS / Windows Internet Settings proxy (e.g. v2rayN, Hiddify,
    Nekoray, Clash on Windows Registry or HTTP_PROXY / HTTPS_PROXY env vars).
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None

    for key in ("https", "http", "socks", "all"):
        if key in proxies:
            norm = _normalize_system_proxy_url(proxies[key])
            if norm:
                return norm
    return None


def resolve_proxy_candidates(
    settings: Dict[str, str],
    target_url: str,
) -> List[Tuple[Optional[str], bool]]:
    """
    Returns ordered list of (proxy_url, is_explicit_user_proxy).
    - Never uses a proxy for localhost / 127.0.0.1 targets.
    - If user explicitly configured a proxy in Settings, uses ONLY that proxy.
    - Otherwise, if an OS/Windows System Proxy is active, tries the System Proxy
      first and falls back to direct connection (`None`) if the proxy port is closed.
    """
    if _is_loopback_target(target_url):
        return [(None, False)]

    explicit = build_httpx_proxy_url(settings)
    if explicit:
        return [(explicit, True)]

    if settings.get("use_system_proxy", "1") != "0":
        sys_proxy = detect_os_system_proxy()
        if sys_proxy:
            return [(sys_proxy, False), (None, False)]

    return [(None, False)]


def _to_curl_proxy(proxy_url: Optional[str]) -> Optional[str]:
    """Use socks5h:// in libcurl so DNS is resolved remotely through the SOCKS5 tunnel."""
    if not proxy_url:
        return None
    if proxy_url.startswith("socks5://"):
        return "socks5h://" + proxy_url[len("socks5://") :]
    return proxy_url


def _build_ssl_context(verify_ssl: bool) -> ssl.SSLContext | bool:
    if not verify_ssl:
        return False
    try:
        ctx = ssl.create_default_context()
        ctx.set_ciphers(CHROME_LIKE_CIPHERS)
        return ctx
    except Exception:
        return True


def is_waf_challenge(status_code: int, text: str, headers: Dict[str, str]) -> bool:
    if status_code in WAF_CHALLENGE_STATUSES:
        return True
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    head = (text or "")[:4096].lower()
    if "<title>just a moment..." in head or "attention required! | cloudflare" in head:
        return True
    if status_code >= 400 and any(marker in head for marker in WAF_BODY_MARKERS):
        return True
    return False


class SmartHttpFetcher:
    """
    Resilient HTTP GET client designed for public news sites, RSS feeds, and
    Cloudflare/WAF-protected websites:
      1) Fast HTTP/2 + Chrome-cipher `httpx` request
      2) Automatic escalation to `curl_cffi` TLS/JA3/JA4/HTTP2 browser impersonation
         (`chrome` -> `safari` -> `firefox`, with root cookie warmup) on 403/429/503
      3) Automatic Windows System Proxy detection with graceful direct fallback
    """

    def __init__(self, settings: Dict[str, str]) -> None:
        self.settings = settings
        self.verify_ssl = settings.get("verify_ssl", "1") != "0"
        try:
            t = float(settings.get("request_timeout") or DEFAULT_REQUEST_TIMEOUT)
            self.timeout_sec = max(5.0, min(120.0, t))
        except (TypeError, ValueError):
            self.timeout_sec = float(DEFAULT_REQUEST_TIMEOUT)

    async def get(
        self,
        url: str,
        *,
        accept_kind: str = "document",
        referer: Optional[str] = None,
    ) -> HttpFetchResponse:
        candidates = resolve_proxy_candidates(self.settings, url)
        last_error: Optional[Exception] = None

        for idx, (proxy_url, is_explicit) in enumerate(candidates):
            try:
                return await self._get_with_proxy(
                    url,
                    proxy_url=proxy_url,
                    accept_kind=accept_kind,
                    referer=referer,
                )
            except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                # If auto-detected OS system proxy failed (e.g. VPN app was closed), fall back to direct
                if not is_explicit and idx + 1 < len(candidates):
                    continue
                self._raise_translated_network_error(exc, url, is_explicit_proxy=is_explicit)
            except CollectorError:
                raise
            except Exception as exc:
                last_error = exc
                if not is_explicit and idx + 1 < len(candidates):
                    continue
                self._raise_translated_network_error(exc, url, is_explicit_proxy=is_explicit)

        if last_error is not None:
            self._raise_translated_network_error(last_error, url, is_explicit_proxy=False)
        raise CollectorError(f"عدم امکان دریافت آدرس: {url}")

    async def _get_with_proxy(
        self,
        url: str,
        *,
        proxy_url: Optional[str],
        accept_kind: str,
        referer: Optional[str],
    ) -> HttpFetchResponse:
        httpx_resp: Optional[HttpFetchResponse] = None
        httpx_exc: Optional[Exception] = None

        # Pass 1: Standard httpx (HTTP/2 enabled for HTTPS when not constrained)
        try:
            httpx_resp = await self._fetch_httpx(
                url,
                proxy_url=proxy_url,
                accept_kind=accept_kind,
                referer=referer,
            )
            if not is_waf_challenge(httpx_resp.status_code, httpx_resp.text, httpx_resp.headers):
                return httpx_resp
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            httpx_exc = exc

        # Pass 2: Escalate to curl_cffi real browser TLS/HTTP2 impersonation on WAF 403/503 or TLS fingerprint resets
        if HAS_CURL_CFFI and not _is_loopback_target(url):
            impersonation_resp = await self._fetch_curl_impersonate(
                url,
                proxy_url=proxy_url,
                accept_kind=accept_kind,
                referer=referer,
            )
            if impersonation_resp is not None:
                if (
                    httpx_resp is None
                    or impersonation_resp.status_code < 400
                    or not is_waf_challenge(
                        impersonation_resp.status_code,
                        impersonation_resp.text,
                        impersonation_resp.headers,
                    )
                ):
                    return impersonation_resp
                httpx_resp = impersonation_resp

        # Pass 3: If target is a feed/API and document headers hit 403/406, retry with explicit feed headers + origin referer
        if (
            httpx_resp is not None
            and httpx_resp.status_code in (403, 406)
            and accept_kind != "feed"
        ):
            parsed = urlparse(url)
            origin_ref = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                alt_resp = await self._fetch_httpx(
                    url,
                    proxy_url=proxy_url,
                    accept_kind="feed",
                    referer=origin_ref,
                )
                if alt_resp.status_code < 400:
                    return alt_resp
            except Exception:
                pass

        if httpx_resp is not None:
            return httpx_resp
        if httpx_exc is not None:
            raise httpx_exc
        raise CollectorError(f"پاسخی از وب‌سایت دریافت نشد ({url}).")

    async def _fetch_httpx(
        self,
        url: str,
        *,
        proxy_url: Optional[str],
        accept_kind: str,
        referer: Optional[str],
    ) -> HttpFetchResponse:
        headers = build_request_headers(
            self.settings, referer=referer, accept_kind=accept_kind
        )
        use_h2 = HAS_HTTP2 and url.lower().startswith("https://") and not (
            proxy_url and proxy_url.startswith("socks")
        )
        verify_arg = _build_ssl_context(self.verify_ssl) if url.lower().startswith("https://") else self.verify_ssl

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout_sec, connect=min(15.0, self.timeout_sec)),
            proxy=proxy_url,
            verify=verify_arg,
            http2=use_h2,
            trust_env=False,
        ) as client:
            resp = await client.get(url)
            lower_headers = {k.lower(): v for k, v in resp.headers.items()}
            return HttpFetchResponse(
                url=str(resp.url),
                status_code=resp.status_code,
                text=resp.text,
                headers=lower_headers,
                via="httpx",
            )

    async def _fetch_curl_impersonate(
        self,
        url: str,
        *,
        proxy_url: Optional[str],
        accept_kind: str,
        referer: Optional[str],
    ) -> Optional[HttpFetchResponse]:
        if not HAS_CURL_CFFI or CurlAsyncSession is None:
            return None

        curl_proxy = _to_curl_proxy(proxy_url)
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        is_subpath = (parsed.path or "/") not in ("", "/")

        extra_headers: Dict[str, str] = {}
        custom_ua = (self.settings.get("custom_user_agent") or "").strip()
        if custom_ua:
            extra_headers["User-Agent"] = custom_ua
        cookie = (self.settings.get("custom_cookie") or "").strip()
        if cookie:
            extra_headers["Cookie"] = cookie
        if referer:
            extra_headers["Referer"] = referer
        elif is_subpath:
            extra_headers["Referer"] = origin
        if accept_kind == "json":
            extra_headers["Accept"] = "application/json,text/plain;q=0.9,*/*;q=0.8"

        best_resp: Optional[HttpFetchResponse] = None

        for profile in ("chrome", "safari", "firefox"):
            try:
                async with CurlAsyncSession(
                    impersonate=profile,
                    verify=self.verify_ssl,
                    proxy=curl_proxy,
                    timeout=self.timeout_sec,
                    allow_redirects=True,
                    max_redirects=8,
                ) as session:
                    # Warm session cookies on origin first when requesting a protected subpath (e.g. /feed/ or article)
                    if is_subpath and profile == "safari":
                        try:
                            await session.get(origin, timeout=min(10.0, self.timeout_sec))
                        except Exception:
                            pass

                    resp = await session.get(url, headers=extra_headers or None)
                    lower_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                    candidate = HttpFetchResponse(
                        url=str(resp.url),
                        status_code=int(resp.status_code),
                        text=resp.text,
                        headers=lower_headers,
                        via=f"curl_cffi:{profile}",
                    )
                    best_resp = candidate
                    if candidate.status_code < 400 and not is_waf_challenge(
                        candidate.status_code, candidate.text, candidate.headers
                    ):
                        return candidate
            except Exception:
                continue

        return best_resp

    def _raise_translated_network_error(
        self,
        exc: Exception,
        url: str,
        *,
        is_explicit_proxy: bool,
    ) -> None:
        if isinstance(exc, CollectorError):
            raise exc
        if isinstance(exc, httpx.ProxyError) or (is_explicit_proxy and "proxy" in str(exc).lower()):
            raise CollectorError(
                f"خطا در اتصال به پروکسی تنظیم‌شده: {exc}. لطفاً آدرس و پورت پروکسی را در تنظیمات بررسی کنید."
            ) from exc
        if isinstance(exc, httpx.ConnectTimeout):
            raise CollectorError(
                "مهلت زمانی اتصال به وب‌سایت به پایان رسید (Timeout). اگر وب‌سایت در شبکه شما فیلتر است، پروکسی را در زبانه تنظیمات فعال کنید."
            ) from exc
        if isinstance(exc, httpx.ReadTimeout):
            raise CollectorError("وب‌سایت در زمان مقرر پاسخ نداد (Read Timeout).") from exc

        err_str = str(exc)
        if "SSL" in err_str.upper() or "CERTIFICATE" in err_str.upper():
            raise CollectorError(
                "خطای گواهی امنیتی SSL در اتصال به وب‌سایت. می‌توانید در زبانه تنظیمات گزینه «بررسی گواهی SSL» را غیرفعال کنید."
            ) from exc

        raise CollectorError(f"عدم دسترسی شبکه به وب‌سایت ({url}): {exc}") from exc
