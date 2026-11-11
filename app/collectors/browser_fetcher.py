"""
لایه مرورگر واقعی بدون پنجره (Stealth Raw CDP over Local Chrome / Microsoft Edge)
برای عبور خودکار از چالش‌های جاوااسکریپتی Cloudflare (JS Managed Challenge) بدون
نیاز به دانلود مرورگر جداگانه (در ویندوز ۱۰ و ۱۱ به صورت پیش‌فرض از Edge یا Chrome
نصب‌شده روی سیستم استفاده می‌کند) و بدون فعال‌سازی Runtime.enable.
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from websockets.asyncio.client import connect as ws_connect  # type: ignore

    HAS_WEBSOCKETS = True
except ImportError:  # pragma: no cover
    try:
        from websockets import connect as ws_connect  # type: ignore

        HAS_WEBSOCKETS = True
    except ImportError:
        ws_connect = None  # type: ignore
        HAS_WEBSOCKETS = False


CHALLENGE_TITLE_MARKERS = (
    "just a moment...",
    "attention required!",
    "checking your browser",
    "ddos-guard",
    "security check",
    "one moment, please",
)


@dataclass
class ClearanceEntry:
    cookie_header: str
    user_agent: str
    expires_at: float


class DomainClearanceCache:
    """In-memory cache of solved Cloudflare cookies + matched User-Agent per (domain, proxy)."""

    TTL_SECONDS = 25 * 60

    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str], ClearanceEntry] = {}

    @staticmethod
    def _base_domain(url: str) -> str:
        host = (urlparse(url).hostname or "").lower().lstrip("www.")
        return host

    def get(self, url: str, proxy_url: Optional[str]) -> Optional[ClearanceEntry]:
        key = (self._base_domain(url), proxy_url or "direct")
        entry = self._store.get(key)
        if not entry:
            return None
        if time.time() >= entry.expires_at:
            self._store.pop(key, None)
            return None
        return entry

    def put(
        self,
        url: str,
        proxy_url: Optional[str],
        *,
        cookie_header: str,
        user_agent: str,
    ) -> None:
        domain = self._base_domain(url)
        if not domain or not cookie_header:
            return
        self._store[(domain, proxy_url or "direct")] = ClearanceEntry(
            cookie_header=cookie_header,
            user_agent=user_agent,
            expires_at=time.time() + self.TTL_SECONDS,
        )

    def clear(self) -> None:
        self._store.clear()


CLEARANCE_CACHE = DomainClearanceCache()
_BROWSER_SEMAPHORE = asyncio.Semaphore(2)


def find_local_browser_executable() -> Optional[str]:
    """Locate an installed Chromium-based browser (Chrome, Edge, Brave, Chromium)."""
    env_override = (os.getenv("WEB_COLLECTOR_BROWSER_PATH") or "").strip()
    if env_override and Path(env_override).is_file():
        return env_override

    if os.name == "nt":
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LocalAppData", ""),
        ]
        rel_candidates = (
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Chromium\Application\chrome.exe",
        )
        for root in roots:
            if not root:
                continue
            for rel in rel_candidates:
                full = Path(root) / rel
                if full.is_file():
                    return str(full)

        for cmd in ("chrome", "msedge", "brave"):
            found = shutil.which(cmd)
            if found:
                return found
        return None

    if sys.platform == "darwin":
        mac_paths = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
        for mp in mac_paths:
            if Path(mp).is_file():
                return mp

    for binary in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "brave-browser",
    ):
        found = shutil.which(binary)
        if found:
            return found

    return None


def _pick_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_get_local_json(port: int, path: str, timeout: float = 1.5) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "Connection": "close"})
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        return json.loads(resp.read(65536).decode("utf-8", "replace"))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _looks_like_js_challenge(html: str) -> bool:
    head = (html or "")[:6000].lower()
    if not head:
        return True
    if any(m in head for m in CHALLENGE_TITLE_MARKERS) and (
        "cloudflare" in head or "__cf_chl_" in head or "turnstile" in head or "challenge-platform" in head
    ):
        return True
    return False


class _RawCdpClient:
    """Minimal CDP client that tracks events without ever invoking Runtime.enable."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self.doc_status: int = 200
        self.doc_url: str = ""
        self.doc_headers: Dict[str, str] = {}
        self.doc_request_id: Optional[str] = None
        self.load_fired = asyncio.Event()
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            async for raw_msg in self._ws:
                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue

                if "id" in msg:
                    fut = self._pending.pop(int(msg["id"]), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue

                method = msg.get("method", "")
                params = msg.get("params") or {}
                if method == "Network.responseReceived":
                    if params.get("type") == "Document":
                        resp = params.get("response") or {}
                        url_val = str(resp.get("url") or "")
                        if url_val.startswith(("http://", "https://")):
                            self.doc_status = int(resp.get("status") or 200)
                            self.doc_url = url_val
                            raw_hdrs = resp.get("headers") or {}
                            self.doc_headers = {str(k).lower(): str(v) for k, v in raw_hdrs.items()}
                            self.doc_request_id = str(params.get("requestId") or "") or None
                elif method == "Page.loadEventFired":
                    self.load_fired.set()
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result({"error": {"message": "closed"}})

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 8.0) -> Dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}})
        await self._ws.send(payload)
        resp = await asyncio.wait_for(fut, timeout=timeout)
        return resp.get("result") or {}

    async def get_outer_html(self) -> str:
        try:
            doc = await self.call("DOM.getDocument", {"depth": 0})
            node_id = (doc.get("root") or {}).get("nodeId")
            if not node_id:
                return ""
            outer = await self.call("DOM.getOuterHTML", {"nodeId": node_id})
            return str(outer.get("outerHTML") or "")
        except Exception:
            return ""

    async def get_raw_response_body(self) -> Optional[str]:
        if not self.doc_request_id:
            return None
        try:
            res = await self.call("Network.getResponseBody", {"requestId": self.doc_request_id})
            body = res.get("body")
            if body is None:
                return None
            if res.get("base64Encoded"):
                return base64.b64decode(body).decode("utf-8", "replace")
            return str(body)
        except Exception:
            return None

    async def close(self) -> None:
        self._reader_task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


async def fetch_via_local_browser(
    url: str,
    *,
    proxy_url: Optional[str] = None,
    verify_ssl: bool = True,
    custom_cookie: str = "",
    timeout_sec: float = 25.0,
) -> Optional[Dict[str, Any]]:
    """
    Launches a local Chromium/Edge/Chrome browser in `--headless=new` mode to pass
    Cloudflare JS Managed Challenges and returns `{url, status_code, text, headers}`.
    Returns None if no local browser is available or if proxy uses inline credentials.
    """
    if not HAS_WEBSOCKETS or ws_connect is None:
        return None

    # Chromium --proxy-server does not accept inline user:pass@ without an extension
    if proxy_url and "@" in proxy_url:
        return None

    browser_bin = find_local_browser_executable()
    if not browser_bin:
        return None

    async with _BROWSER_SEMAPHORE:
        debug_port = _pick_ephemeral_port()
        profile_dir = tempfile.mkdtemp(prefix="wcc_cdp_")
        proc: Optional[subprocess.Popen[bytes]] = None

        cmd: List[str] = [
            browser_bin,
            "--headless=new",
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-gpu",
            "--mute-audio",
            "--window-size=1366,768",
            "--lang=en-US",
        ]
        if not verify_ssl:
            cmd.append("--ignore-certificate-errors")
        if proxy_url:
            cmd.append(f"--proxy-server={proxy_url}")
        if os.name != "nt":
            cmd.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        cmd.append("about:blank")

        popen_kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)

            # Wait for DevTools HTTP endpoint
            ws_url: Optional[str] = None
            raw_ua = ""
            deadline = time.monotonic() + 7.0
            while time.monotonic() < deadline:
                ver_info = await asyncio.to_thread(_http_get_local_json, debug_port, "/json/version")
                if isinstance(ver_info, dict):
                    raw_ua = str(ver_info.get("User-Agent") or "")
                    tabs = await asyncio.to_thread(_http_get_local_json, debug_port, "/json/list")
                    if isinstance(tabs, list):
                        for tab in tabs:
                            if isinstance(tab, dict) and tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                                ws_url = str(tab["webSocketDebuggerUrl"])
                                break
                if ws_url:
                    break
                await asyncio.sleep(0.15)

            if not ws_url:
                return None

            clean_ua = raw_ua.replace("HeadlessChrome/", "Chrome/") or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )

            async with ws_connect(ws_url, max_size=32 * 1024 * 1024) as ws:
                cdp = _RawCdpClient(ws)
                try:
                    await cdp.call("Network.enable")
                    await cdp.call("Page.enable")
                    await cdp.call("DOM.enable")
                    await cdp.call(
                        "Network.setUserAgentOverride",
                        {
                            "userAgent": clean_ua,
                            "acceptLanguage": "en-US,en;q=0.9,fa-IR;q=0.8",
                            "platform": "Win32" if os.name == "nt" else "Linux x86_64",
                        },
                    )

                    # Inject any user-configured cookies onto target domain before navigating
                    if custom_cookie:
                        parsed_target = urlparse(url)
                        domain_host = parsed_target.hostname or ""
                        for pair in custom_cookie.split(";"):
                            if "=" in pair and domain_host:
                                k, v = pair.split("=", 1)
                                try:
                                    await cdp.call(
                                        "Network.setCookie",
                                        {
                                            "name": k.strip(),
                                            "value": v.strip(),
                                            "domain": domain_host,
                                            "path": "/",
                                        },
                                    )
                                except Exception:
                                    pass

                    await cdp.call("Page.navigate", {"url": url})
                    try:
                        await asyncio.wait_for(cdp.load_fired.wait(), timeout=min(15.0, timeout_sec))
                    except asyncio.TimeoutError:
                        pass

                    # Give initial scripts a brief beat, then poll if Cloudflare JS challenge is active
                    await asyncio.sleep(0.8)
                    html = await cdp.get_outer_html()

                    wait_deadline = time.monotonic() + min(15.0, max(8.0, timeout_sec - 4.0))
                    while time.monotonic() < wait_deadline and (
                        cdp.doc_status in (403, 429, 503) or _looks_like_js_challenge(html)
                    ):
                        await asyncio.sleep(1.0)
                        html = await cdp.get_outer_html()
                        if cdp.doc_status == 200 and html and not _looks_like_js_challenge(html):
                            break

                    # Prefer raw network body for XML feeds / JSON APIs; fall back to rendered DOM for SPAs/HTML
                    raw_body = await cdp.get_raw_response_body()
                    ctype = cdp.doc_headers.get("content-type", "").lower()
                    if raw_body and (
                        any(t in ctype for t in ("xml", "json", "rss", "atom"))
                        or raw_body.lstrip().startswith(("<?xml", "<rss", "<feed", "{", "["))
                    ):
                        final_text = raw_body
                    else:
                        final_text = html or raw_body or ""

                    # Cache cookies (including cf_clearance) for fast subsequent requests
                    cookies_res = await cdp.call("Network.getAllCookies")
                    cookies_list = cookies_res.get("cookies") or []
                    target_base = DomainClearanceCache._base_domain(cdp.doc_url or url)
                    relevant_pairs = [
                        f"{c['name']}={c['value']}"
                        for c in cookies_list
                        if isinstance(c, dict)
                        and c.get("name")
                        and target_base
                        and target_base in str(c.get("domain") or "").lower().lstrip(".")
                    ]
                    if relevant_pairs and cdp.doc_status < 400 and not _looks_like_js_challenge(final_text):
                        CLEARANCE_CACHE.put(
                            cdp.doc_url or url,
                            proxy_url,
                            cookie_header="; ".join(relevant_pairs),
                            user_agent=clean_ua,
                        )

                    return {
                        "url": cdp.doc_url or url,
                        "status_code": cdp.doc_status,
                        "text": final_text,
                        "headers": cdp.doc_headers,
                        "via": "browser_cdp",
                    }
                finally:
                    await cdp.close()
        except Exception:
            return None
        finally:
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=4)
                except Exception:
                    pass
            shutil.rmtree(profile_dir, ignore_errors=True)
