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
from app.collectors.browser_fetcher import (
    CLEARANCE_CACHE,
    _looks_like_js_challenge,
    fetch_via_local_browser,
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

CHROME_LIKE_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA"
)

WAF_CHALLENGE_STATUSES = {401, 403, 406, 429, 503}
CLOUDFLARE_ORIGIN_DOWN_STATUSES = {502, 504, 520, 521, 522, 523, 524, 525, 526}

WAF_BODY_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "__cf_chl_",
    "enable javascript and cookies to continue",
    "sucuri website firewall",
    "access denied |",
    "<title>access denied</title>",
    "you don't have permission to access",
    "edgesuite.net",
    "reference #18.",
    "reference #",
    "arvancloud",
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
            if self.status_code in CLOUDFLARE_ORIGIN_DOWN_STATUSES:
                raise CollectorError(
                    f"سرور میزبانی اصلی این وب‌سایت در حال حاضر پاسخ نمی‌دهد یا موقتاً از دسترس خارج است "
                    f"(خطای سرور مبدأ Cloudflare HTTP {self.status_code}). لطفاً ابتدا باز شدن سایت در مرورگر را بررسی کنید."
                )
            if self.status_code == 403:
                raise CollectorError(
                    "سرور وب‌سایت دسترسی را مسدود کرد (HTTP 403 - فایروال/Cloudflare). "
                    "پیشنهاد: ۱) اگر VPN شما IP دیتاسنتری دارد، سرور VPN را عوض کنید؛ "
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


def _is_domestic_ir_target(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host.endswith(".ir")


def resolve_proxy_candidates(
    settings: Dict[str, str],
    target_url: str,
) -> List[Tuple[Optional[str], bool]]:
    """
    Returns ordered list of (proxy_url, is_explicit_user_proxy).
    - Never uses a proxy for localhost / 127.0.0.1 targets.
    - Site-specific connection method wins over the global proxy settings:
        * `site_force_direct` = "1"  -> always a plain direct connection (no global/OS proxy)
        * `site_proxy_url`            -> that proxy is used for this website only
    - For domestic `.ir` sites (e.g. `ipis.ir`, `mfa.gov.ir`, `isna.ir`), tries direct
      connection (`None`) FIRST because Iranian ministry/domestic firewalls frequently
      drop or block foreign VPN IPs, keeping the proxy as secondary fallback.
    - For international sites: tries explicit/OS proxy first and falls back to direct (`None`).
    """
    if _is_loopback_target(target_url):
        return [(None, False)]

    # روش اتصال اختصاصی این وب‌سایت (پروفایل اتصال یا پروکسی دستی) بر تنظیمات عمومی مقدم است
    if str(settings.get("site_force_direct") or "") == "1" and not settings.get("site_proxy_url"):
        return [(None, False)]

    site_proxy = str(settings.get("site_proxy_url") or "").strip()
    if site_proxy:
        return [(site_proxy, True)]

    is_ir = _is_domestic_ir_target(target_url)

    explicit = build_httpx_proxy_url(settings)
    if explicit:
        if is_ir:
            return [(None, False), (explicit, True)]
        return [(explicit, True)]

    if settings.get("use_system_proxy", "1") != "0":
        sys_proxy = detect_os_system_proxy()
        if sys_proxy:
            if is_ir:
                return [(None, False), (sys_proxy, False)]
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
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
            return ctx
        except Exception:
            return False
    try:
        ctx = ssl.create_default_context()
        ctx.set_ciphers(CHROME_LIKE_CIPHERS)
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
        return ctx
    except Exception:
        return True


def is_waf_challenge(status_code: int, text: str, headers: Dict[str, str]) -> bool:
    if status_code in WAF_CHALLENGE_STATUSES:
        return True
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if text and text.strip():
        head = text[:4096].lower()
        if "<title>just a moment..." in head or "attention required! | cloudflare" in head:
            return True
        if status_code >= 400 and any(marker in head for marker in WAF_BODY_MARKERS):
            return True
        if len(text) < 48_000 and _looks_like_js_challenge(text):
            return True
    return False


def _merge_cookies(user_cookie: str, cached_cookie: str) -> str:
    parts = [p.strip() for p in (cached_cookie, user_cookie) if p and p.strip()]
    return "; ".join(parts)


def _is_ssl_cert_error(exc: Exception) -> bool:
    if isinstance(exc, ssl.SSLError) or isinstance(getattr(exc, "__cause__", None), ssl.SSLError):
        return True
    msg = str(exc).upper()
    return "SSL" in msg or "CERTIFICATE" in msg or "CERT_" in msg


def _uses_non_public_ca(url: str) -> bool:
    """
    U.S. Department of Defense `.mil` websites sign certificates with the DoD PKI Root CA,
    which is never shipped inside Python `certifi` or `curl_cffi` Mozilla CA bundles.
    """
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    return host == "mil" or host.endswith(".mil")


class SmartHttpFetcher:
    """
    4-Layer Resilient HTTP Client:
      Layer 1: Fast HTTP/2 + Chrome-cipher `httpx` (even across http:// -> https:// redirects)
      Layer 2: `curl_cffi` TLS/JA3/JA4/HTTP2 browser impersonation (`chrome`/`safari`/`firefox`)
      Layer 3: Real Headless Browser CDP (`Edge` / `Chrome` installed on Windows/Linux/macOS)
               to solve Cloudflare JS Managed Challenges + 25-min `cf_clearance` cache
      Layer 4: Smart Proxy <-> Direct failover when VPN datacenter IP gets 403 or 522
    """

    def __init__(self, settings: Dict[str, str], *, allow_browser_fallback: bool = True) -> None:
        self.settings = settings
        self.verify_ssl = settings.get("verify_ssl", "1") != "0"
        self.allow_browser_fallback = allow_browser_fallback and settings.get("browser_fallback", "1") != "0"
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
        candidates = list(resolve_proxy_candidates(self.settings, url))
        # Also allow direct rescue if explicit proxy succeeded at TCP level but got 403/522 IP block
        has_explicit = len(candidates) == 1 and candidates[0][1] is True and candidates[0][0] is not None
        # A per-site connection profile can opt out of the direct rescue route
        site_only = str(self.settings.get("site_proxy_no_fallback") or "") == "1"
        if has_explicit and not site_only:
            candidates.append((None, False))

        primary_resp: Optional[HttpFetchResponse] = None
        last_error: Optional[Exception] = None

        for idx, (proxy_url, is_explicit) in enumerate(candidates):
            is_secondary_direct_rescue = idx > 0 and proxy_url is None
            try:
                resp = await self._get_with_proxy(
                    url,
                    proxy_url=proxy_url,
                    accept_kind=accept_kind,
                    referer=referer,
                    fast_timeout=8.0 if is_secondary_direct_rescue else None,
                )
                if primary_resp is None:
                    primary_resp = resp

                # If this route succeeded cleanly, return immediately!
                if resp.status_code < 400 and not is_waf_challenge(
                    resp.status_code, resp.text, resp.headers
                ):
                    return resp

                # If blocked by WAF (403/429/503) or Cloudflare origin 520..526 via VPN proxy,
                # try the next route (direct connection) in case the VPN IP was blacklisted!
                if idx + 1 < len(candidates) and (
                    is_waf_challenge(resp.status_code, resp.text, resp.headers)
                    or resp.status_code in CLOUDFLARE_ORIGIN_DOWN_STATUSES
                ):
                    continue

                return primary_resp

            except httpx.ProxyError as exc:
                last_error = exc
                if is_explicit:
                    self._raise_translated_network_error(exc, url, is_explicit_proxy=True)
                if primary_resp is not None:
                    return primary_resp
                if idx + 1 < len(candidates):
                    continue
                self._raise_translated_network_error(exc, url, is_explicit_proxy=False)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                if is_explicit and "proxy" in str(exc).lower():
                    self._raise_translated_network_error(exc, url, is_explicit_proxy=True)
                # If secondary direct rescue timed out (because site is filtered without VPN), return primary_resp!
                if primary_resp is not None:
                    return primary_resp
                if idx + 1 < len(candidates):
                    continue
                self._raise_translated_network_error(exc, url, is_explicit_proxy=False)
            except CollectorError:
                if primary_resp is not None:
                    return primary_resp
                raise
            except Exception as exc:
                last_error = exc
                if is_explicit and "proxy" in str(exc).lower():
                    self._raise_translated_network_error(exc, url, is_explicit_proxy=True)
                if primary_resp is not None:
                    return primary_resp
                if idx + 1 < len(candidates):
                    continue
                self._raise_translated_network_error(exc, url, is_explicit_proxy=False)

        if primary_resp is not None:
            return primary_resp
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
        fast_timeout: Optional[float] = None,
    ) -> HttpFetchResponse:
        effective_timeout = fast_timeout or self.timeout_sec
        best_resp: Optional[HttpFetchResponse] = None
        httpx_exc: Optional[Exception] = None
        ssl_verify_for_target = self.verify_ssl

        # Layer 1: Standard httpx (ALWAYS with HTTP/2 + Chrome ciphers so http:// -> https:// redirects work!)
        try:
            httpx_resp = await self._fetch_httpx(
                url,
                proxy_url=proxy_url,
                accept_kind=accept_kind,
                referer=referer,
                timeout_sec=effective_timeout,
                verify_override=ssl_verify_for_target,
            )
            best_resp = httpx_resp
            if not is_waf_challenge(httpx_resp.status_code, httpx_resp.text, httpx_resp.headers):
                return httpx_resp
        except httpx.ProxyError as exc:
            # Fail fast when the user's local proxy port itself is unreachable
            raise exc
        except (httpx.HTTPError, ssl.SSLError) as exc:
            httpx_exc = exc
            # Recover automatically when a site uses DoD/national Root CAs (e.g. .mil), incomplete
            # intermediate chains, or Akamai HTTP/2 RemoteProtocolError / ReadError stream resets.
            # One cheap HTTP/1.1 retry (with verification relaxed for TLS failures) usually
            # succeeds where HTTP/2 ALPN negotiation was reset by the edge server.
            if not _is_loopback_target(url):
                h1_verify = (
                    False
                    if (ssl_verify_for_target and _is_ssl_cert_error(exc))
                    else ssl_verify_for_target
                )
                try:
                    h1_resp = await self._fetch_httpx(
                        url,
                        proxy_url=proxy_url,
                        accept_kind=accept_kind,
                        referer=referer,
                        timeout_sec=effective_timeout,
                        verify_override=h1_verify,
                        force_http1=True,
                    )
                    best_resp = h1_resp
                    if not is_waf_challenge(h1_resp.status_code, h1_resp.text, h1_resp.headers):
                        return h1_resp
                except httpx.ProxyError as exc_proxy:
                    raise exc_proxy
                except (httpx.HTTPError, ssl.SSLError) as exc_h1:
                    httpx_exc = exc_h1

            if ssl_verify_for_target and _is_ssl_cert_error(exc):
                ssl_verify_for_target = False
                try:
                    httpx_resp = await self._fetch_httpx(
                        url,
                        proxy_url=proxy_url,
                        accept_kind=accept_kind,
                        referer=referer,
                        timeout_sec=effective_timeout,
                        verify_override=False,
                    )
                    best_resp = httpx_resp
                    if not is_waf_challenge(httpx_resp.status_code, httpx_resp.text, httpx_resp.headers):
                        return httpx_resp
                except httpx.ProxyError as exc_proxy:
                    raise exc_proxy
                except (httpx.HTTPError, ssl.SSLError) as exc2:
                    httpx_exc = exc2

        # Layer 2: Escalate to curl_cffi real browser TLS/JA3/JA4/HTTP2 impersonation
        if HAS_CURL_CFFI and not _is_loopback_target(url):
            imp_resp = await self._fetch_curl_impersonate(
                url,
                proxy_url=proxy_url,
                accept_kind=accept_kind,
                referer=referer,
                timeout_sec=effective_timeout,
                verify_override=ssl_verify_for_target,
            )
            if imp_resp is not None:
                best_resp = imp_resp
                if imp_resp.status_code < 400 and not is_waf_challenge(
                    imp_resp.status_code, imp_resp.text, imp_resp.headers
                ):
                    return imp_resp

        # Layer 3: Feed-header + origin-referer retry for strict RSS endpoints
        if (
            best_resp is not None
            and best_resp.status_code in (403, 406)
            and accept_kind != "feed"
        ):
            parsed = urlparse(best_resp.url or url)
            origin_ref = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                alt_resp = await self._fetch_httpx(
                    best_resp.url or url,
                    proxy_url=proxy_url,
                    accept_kind="feed",
                    referer=origin_ref,
                    timeout_sec=effective_timeout,
                    verify_override=ssl_verify_for_target,
                )
                if alt_resp.status_code < 400 and not is_waf_challenge(
                    alt_resp.status_code, alt_resp.text, alt_resp.headers
                ):
                    return alt_resp
            except Exception:
                pass

        # Layer 4: Real Local Browser CDP (Edge / Chrome) for Cloudflare JS Managed Challenges!
        # On secondary direct failover (`fast_timeout` set), run browser ONLY if direct TCP/TLS actually
        # reached the server (`best_resp is not None`), so filtered sites don't waste time launching a browser.
        if (
            self.allow_browser_fallback
            and not _is_loopback_target(url)
            and (fast_timeout is None or best_resp is not None)
            and (
                best_resp is None
                or is_waf_challenge(best_resp.status_code, best_resp.text, best_resp.headers)
            )
        ):
            target_for_browser = best_resp.url if (best_resp and best_resp.url) else url
            clearance = CLEARANCE_CACHE.get(target_for_browser, proxy_url)
            merged_browser_cookie = _merge_cookies(
                (self.settings.get("custom_cookie") or "").strip(),
                clearance.cookie_header if clearance else "",
            )
            browser_res = await fetch_via_local_browser(
                target_for_browser,
                proxy_url=proxy_url,
                verify_ssl=ssl_verify_for_target and not _uses_non_public_ca(target_for_browser),
                custom_cookie=merged_browser_cookie,
                timeout_sec=max(18.0, self.timeout_sec),
            )
            if browser_res is not None:
                b_resp = HttpFetchResponse(
                    url=str(browser_res["url"]),
                    status_code=int(browser_res["status_code"]),
                    text=str(browser_res["text"]),
                    headers=dict(browser_res["headers"]),
                    via=str(browser_res.get("via") or "browser_cdp"),
                )
                if b_resp.status_code < 400 and not is_waf_challenge(
                    b_resp.status_code, b_resp.text, b_resp.headers
                ):
                    return b_resp
                if best_resp is None:
                    best_resp = b_resp

        if best_resp is not None:
            return best_resp
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
        timeout_sec: float,
        verify_override: Optional[bool] = None,
        force_http1: bool = False,
    ) -> HttpFetchResponse:
        effective_settings = dict(self.settings)
        clearance = CLEARANCE_CACHE.get(url, proxy_url)
        if clearance:
            if not effective_settings.get("custom_user_agent"):
                effective_settings["custom_user_agent"] = clearance.user_agent
            effective_settings["custom_cookie"] = _merge_cookies(
                effective_settings.get("custom_cookie", ""),
                clearance.cookie_header,
            )

        headers = build_request_headers(
            effective_settings, referer=referer, accept_kind=accept_kind
        )
        # Always enable HTTP/2 and Chrome TLS ciphers (even on http:// URLs that 301-redirect to https://!),
        # except for the explicit HTTP/1.1 retry used against Akamai/.mil ALPN resets.
        use_h2 = HAS_HTTP2 and not force_http1 and not (proxy_url and proxy_url.startswith("socks"))
        use_verify = self.verify_ssl if verify_override is None else verify_override
        verify_arg = _build_ssl_context(use_verify)

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_sec, connect=min(12.0, timeout_sec)),
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
        timeout_sec: float,
        verify_override: Optional[bool] = None,
    ) -> Optional[HttpFetchResponse]:
        if not HAS_CURL_CFFI or CurlAsyncSession is None:
            return None

        use_verify = self.verify_ssl if verify_override is None else verify_override

        curl_proxy = _to_curl_proxy(proxy_url)
        # Upgrade initial http:// to https:// probe candidate only on redirect; let curl follow naturally
        parsed = urlparse(url)
        https_scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
        origin = f"{https_scheme}://{parsed.netloc}/"
        is_subpath = (parsed.path or "/") not in ("", "/")

        clearance = CLEARANCE_CACHE.get(url, proxy_url)
        user_cookie = (self.settings.get("custom_cookie") or "").strip()
        merged_cookie = _merge_cookies(
            user_cookie, clearance.cookie_header if clearance else ""
        )

        extra_headers: Dict[str, str] = {}
        custom_ua = (self.settings.get("custom_user_agent") or "").strip()
        if custom_ua:
            extra_headers["User-Agent"] = custom_ua
        elif clearance and clearance.user_agent:
            extra_headers["User-Agent"] = clearance.user_agent
        if merged_cookie:
            extra_headers["Cookie"] = merged_cookie
        if referer:
            extra_headers["Referer"] = referer
        elif is_subpath:
            extra_headers["Referer"] = origin
        if accept_kind == "json":
            extra_headers["Accept"] = "application/json,text/plain;q=0.9,*/*;q=0.8"

        best_resp: Optional[HttpFetchResponse] = None

        for profile in ("chrome", "safari", "firefox"):
            verify_attempts = [use_verify, False] if use_verify else [False]
            for v_flag in verify_attempts:
                try:
                    async with CurlAsyncSession(
                        impersonate=profile,
                        verify=v_flag,
                        proxy=curl_proxy,
                        timeout=timeout_sec,
                        allow_redirects=True,
                        max_redirects=10,
                    ) as session:
                        if is_subpath and profile in ("chrome", "safari") and not clearance:
                            try:
                                await session.get(origin, timeout=min(8.0, timeout_sec))
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
                        break
                except Exception as exc_curl:
                    if v_flag and _is_ssl_cert_error(exc_curl):
                        use_verify = False
                        continue
                    break

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
