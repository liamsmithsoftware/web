"""
لایه مرورگر واقعی بدون پنجره مزاحم (Stealth Raw CDP over Local Chrome / Microsoft Edge)
برای عبور خودکار از چالش‌های جاوااسکریپتی Cloudflare، Turnstile (شامل iframeهای مجزای
OOPIF در challenges.cloudflare.com)، Akamai و DataDome بدون نیاز به دانلود مرورگر جداگانه
و بدون فعال‌سازی Runtime.enable.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from app.utils.site_parser import extract_registrable_domain

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
    "verifying you are human",
    "verify you are human",
    "access denied",
)

ACTIVE_CHALLENGE_DOM_MARKERS = (
    "window._cf_chl_opt",
    'id="challenge-running"',
    'id="challenge-stage"',
    'id="cf-challenge-running"',
    'id="turnstile-wrapper"',
    'class="cf-turnstile"',
    "challenges.cloudflare.com/turnstile",
    "challenges.cloudflare.com/cdn-cgi/challenge-platform",
    "/cdn-cgi/challenge-platform/h/",
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
)

STEALTH_INIT_SCRIPT = """
(() => {
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => false,
            configurable: true
        });
        if (!window.chrome) {
            Object.defineProperty(window, 'chrome', {
                value: { runtime: {}, app: { isInstalled: false } },
                configurable: true
            });
        }
        if (!window.outerWidth || window.outerWidth === 0) {
            Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth || 1280, configurable: true });
        }
        if (!window.outerHeight || window.outerHeight === 0) {
            Object.defineProperty(window, 'outerHeight', { get: () => (window.innerHeight || 720) + 68, configurable: true });
        }
        if (!navigator.languages || navigator.languages.length === 0) {
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
        }
        if (document.visibilityState === 'hidden') {
            Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
            Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
        }
    } catch (_) {}
})();
"""


@dataclass
class ClearanceEntry:
    cookie_header: str
    user_agent: str
    expires_at: float
    via_browser: bool = True


class DomainClearanceCache:
    """In-memory cache of solved Cloudflare cookies + matched User-Agent per (domain, proxy)."""

    TTL_SECONDS = 25 * 60

    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str], ClearanceEntry] = {}

    @staticmethod
    def _base_domain(url: str) -> str:
        return extract_registrable_domain(url)

    def get(self, url: str, proxy_url: Optional[str]) -> Optional[ClearanceEntry]:
        domain = self._base_domain(url)
        if not domain:
            return None
        key = (domain, proxy_url or "direct")
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
        via_browser: bool = True,
    ) -> None:
        domain = self._base_domain(url)
        if not domain or not cookie_header:
            return
        self._store[(domain, proxy_url or "direct")] = ClearanceEntry(
            cookie_header=cookie_header,
            user_agent=user_agent,
            expires_at=time.time() + self.TTL_SECONDS,
            via_browser=via_browser,
        )

    def clear(self) -> None:
        self._store.clear()


CLEARANCE_CACHE = DomainClearanceCache()
# Serialize local browser launches (1 at a time) so persistent profile cookie jars never collide
_BROWSER_SEMAPHORE = asyncio.Semaphore(1)
# Domains whose strict Turnstile challenge required an off-screen non-headless window
_GUI_REQUIRED_DOMAINS: Set[str] = set()


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


def _can_use_gui_fallback() -> bool:
    """True when the OS has a desktop session where an off-screen unminimized window can run."""
    if (os.getenv("WEB_COLLECTOR_HEADLESS_ONLY") or "").strip() == "1":
        return False
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _get_persistent_profile_dir(*, headless: bool, proxy_url: Optional[str]) -> str:
    """
    Returns a reusable Chromium profile directory (safe under `_BROWSER_SEMAPHORE = Semaphore(1)`)
    so solved `cf_clearance` cookies, TLS session resumption tickets, and Cloudflare Private State
    Tokens persist across `/` -> `/feed/` -> `/wp-json/` and subsequent syncs.
    """
    proxy_tag = hashlib.sha1((proxy_url or "direct").encode("utf-8")).hexdigest()[:8]
    mode_tag = "hl" if headless else "gui"
    profile_path = Path(tempfile.gettempdir()) / f"wcc_cdp_profile_{mode_tag}_{proxy_tag}"
    profile_path.mkdir(parents=True, exist_ok=True)
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_file = profile_path / lock_name
        try:
            if lock_file.exists() or lock_file.is_symlink():
                lock_file.unlink()
        except Exception:
            pass
    return str(profile_path)


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
    raw = html or ""
    if not raw.strip():
        return True
    head = raw[:8192].lower()
    if any(m in head for m in CHALLENGE_TITLE_MARKERS) and (
        "cloudflare" in head
        or "__cf_chl_" in head
        or "_cf_chl_opt" in head
        or "turnstile" in head
        or "challenge-platform" in head
        or "ddos-guard" in head
        or "captcha-delivery.com" in head
        or "datadome" in head
        or "edgesuite.net" in head
    ):
        return True
    # Newer Cloudflare Turnstile / DataDome templates set <title> to the domain name
    if len(raw) < 48_000:
        lower_body = raw.lower()
        if any(marker in lower_body for marker in ACTIVE_CHALLENGE_DOM_MARKERS):
            return True
    return False


def _build_ua_override_params(raw_ua: str, browser_bin: str = "") -> Dict[str, Any]:
    """
    Constructs `Emulation.setUserAgentOverride` / `Network.setUserAgentOverride` parameters
    including full `userAgentMetadata` so `Sec-Ch-Ua` HTTP Client Hints AND
    `navigator.userAgentData` replace `HeadlessChrome` with genuine `Google Chrome` or `Microsoft Edge`.
    """
    clean_ua = (raw_ua or "").replace("HeadlessChrome/", "Chrome/").strip()
    if not clean_ua:
        clean_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

    m_chrome = re.search(r"Chrome/(\d+)\.(\d+\.\d+\.\d+)", clean_ua)
    chrome_major = m_chrome.group(1) if m_chrome else "131"
    chrome_full = f"{chrome_major}.{m_chrome.group(2)}" if m_chrome else "131.0.0.0"

    m_edge = re.search(r"Edg/(\d+)\.(\d+\.\d+\.\d+)", clean_ua)
    is_edge = bool(m_edge) or ("msedge" in (browser_bin or "").lower())
    edge_major = m_edge.group(1) if m_edge else chrome_major
    edge_full = f"{edge_major}.{m_edge.group(2)}" if m_edge else chrome_full

    if is_edge:
        brands = [
            {"brand": "Not(A:Brand", "version": "99"},
            {"brand": "Microsoft Edge", "version": edge_major},
            {"brand": "Chromium", "version": chrome_major},
        ]
        full_version_list = [
            {"brand": "Not(A:Brand", "version": "99.0.0.0"},
            {"brand": "Microsoft Edge", "version": edge_full},
            {"brand": "Chromium", "version": chrome_full},
        ]
        primary_full_version = edge_full
    else:
        brands = [
            {"brand": "Not(A:Brand", "version": "99"},
            {"brand": "Google Chrome", "version": chrome_major},
            {"brand": "Chromium", "version": chrome_major},
        ]
        full_version_list = [
            {"brand": "Not(A:Brand", "version": "99.0.0.0"},
            {"brand": "Google Chrome", "version": chrome_full},
            {"brand": "Chromium", "version": chrome_full},
        ]
        primary_full_version = chrome_full

    if os.name == "nt":
        nav_platform = "Win32"
        ch_platform = "Windows"
        ch_platform_ver = "15.0.0"
    elif sys.platform == "darwin":
        nav_platform = "MacIntel"
        ch_platform = "macOS"
        ch_platform_ver = "14.5.0"
    else:
        nav_platform = "Linux x86_64"
        ch_platform = "Linux"
        ch_platform_ver = "6.5.0"

    return {
        "userAgent": clean_ua,
        "acceptLanguage": "en-US,en;q=0.9,fa-IR;q=0.8",
        "platform": nav_platform,
        "userAgentMetadata": {
            "brands": brands,
            "fullVersionList": full_version_list,
            "fullVersion": primary_full_version,
            "platform": ch_platform,
            "platformVersion": ch_platform_ver,
            "architecture": "x86",
            "bitness": "64",
            "model": "",
            "mobile": False,
            "wow64": False,
        },
    }


def _normalize_chromium_proxy(proxy_url: Optional[str]) -> Optional[str]:
    if not proxy_url:
        return None
    if proxy_url.startswith("socks5h://"):
        return "socks5://" + proxy_url[len("socks5h://") :]
    return proxy_url


class _RawCdpClient:
    """
    Minimal CDP client that tracks main-frame responses AND automatically applies Client Hints +
    stealth init scripts to cross-origin OOPIF subtargets (e.g. `challenges.cloudflare.com`)
    without ever invoking `Runtime.enable`.
    """

    def __init__(self, ws: Any, *, ua_override: Optional[Dict[str, Any]] = None) -> None:
        self._ws = ws
        self._ua_override = ua_override
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self._bg_tasks: Set[asyncio.Task[Any]] = set()
        self.doc_status: int = 200
        self.doc_url: str = ""
        self.doc_headers: Dict[str, str] = {}
        self.doc_request_id: Optional[str] = None
        self.load_fired = asyncio.Event()
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _configure_child_target(self, session_id: str, waiting_for_debugger: bool) -> None:
        """Applies UA Client Hints & stealth script to cross-origin Turnstile iframes before they execute."""
        try:
            if self._ua_override:
                await self.call(
                    "Emulation.setUserAgentOverride",
                    self._ua_override,
                    timeout=3.0,
                    session_id=session_id,
                )
            await self.call("Page.enable", {}, timeout=3.0, session_id=session_id)
            await self.call(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": STEALTH_INIT_SCRIPT},
                timeout=3.0,
                session_id=session_id,
            )
        except Exception:
            pass
        finally:
            if waiting_for_debugger:
                try:
                    await self.call(
                        "Runtime.runIfWaitingForDebugger",
                        {},
                        timeout=3.0,
                        session_id=session_id,
                    )
                except Exception:
                    pass

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
                msg_session = msg.get("sessionId")

                if method == "Target.attachedToTarget":
                    child_sid = str(params.get("sessionId") or "")
                    waiting = bool(params.get("waitingForDebugger"))
                    if child_sid:
                        task = asyncio.create_task(self._configure_child_target(child_sid, waiting))
                        self._bg_tasks.add(task)
                        task.add_done_callback(self._bg_tasks.discard)
                    continue

                # Track main-frame document responses only (ignore child OOPIF session events)
                if msg_session:
                    continue

                if method == "Network.responseReceived":
                    if params.get("type") == "Document":
                        resp = params.get("response") or {}
                        url_val = str(resp.get("url") or "")
                        if url_val.startswith(("http://", "https://")) and "challenges.cloudflare.com" not in url_val:
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

    async def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 8.0,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        msg_obj: Dict[str, Any] = {"id": req_id, "method": method, "params": params or {}}
        if session_id:
            msg_obj["sessionId"] = session_id
        await self._ws.send(json.dumps(msg_obj))
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
        for t in list(self._bg_tasks):
            t.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


async def _nudge_turnstile_if_present(cdp: _RawCdpClient) -> None:
    """
    Provides natural pointer movement entropy for Cloudflare Managed Challenges and, if an
    interactive Turnstile checkbox iframe or host wrapper is present (even inside closed Shadow DOM),
    clicks its checkbox region. Uses ONLY Input & DOM CDP domains (never Runtime.enable).
    """
    try:
        for x, y in ((180, 220), (255, 270), (315, 305)):
            await cdp.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y, "button": "none", "pointerType": "mouse"},
                timeout=2.0,
            )
            await asyncio.sleep(0.06)

        flat = await cdp.call("DOM.getFlattenedDocument", {"depth": -1, "pierce": True}, timeout=3.0)
        nodes = flat.get("nodes") or []
        candidate_node_ids: List[int] = []

        for node in nodes:
            if not isinstance(node, dict):
                continue
            n_name = str(node.get("nodeName") or "").upper()
            attrs = node.get("attributes") or []
            attr_map = {
                str(attrs[i]).lower(): str(attrs[i + 1])
                for i in range(0, len(attrs) - 1, 2)
            }
            nid = node.get("nodeId")
            if not isinstance(nid, int):
                continue

            if n_name == "IFRAME":
                src = attr_map.get("src", "").lower()
                title = attr_map.get("title", "").lower()
                elem_id = attr_map.get("id", "").lower()
                if (
                    "challenges.cloudflare.com" in src
                    or "turnstile" in src
                    or "cf-chl-widget" in elem_id
                    or "cloudflare security challenge" in title
                ):
                    candidate_node_ids.insert(0, nid)
            elif n_name == "DIV":
                elem_id = attr_map.get("id", "").lower()
                elem_cls = attr_map.get("class", "").lower()
                if elem_id in ("turnstile-wrapper", "challenge-stage", "cf-turnstile") or "cf-turnstile" in elem_cls:
                    candidate_node_ids.append(nid)

        for target_nid in candidate_node_ids:
            try:
                box = await cdp.call("DOM.getBoxModel", {"nodeId": target_nid}, timeout=2.0)
                content_quad = (box.get("model") or {}).get("content")
                if not isinstance(content_quad, list) or len(content_quad) < 8:
                    continue
                left_x = float(content_quad[0])
                right_x = float(content_quad[2])
                top_y = float(content_quad[1])
                bottom_y = float(content_quad[5])
                width = right_x - left_x
                height = bottom_y - top_y
                if width < 40 or height < 20:
                    continue
                click_x = int(left_x + 28)
                click_y = int(top_y + min(34.0, height / 2.0))
                await cdp.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": click_x, "y": click_y, "button": "none", "pointerType": "mouse"},
                    timeout=2.0,
                )
                await asyncio.sleep(0.08)
                await cdp.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "x": click_x,
                        "y": click_y,
                        "button": "left",
                        "buttons": 1,
                        "clickCount": 1,
                        "pointerType": "mouse",
                    },
                    timeout=2.0,
                )
                await asyncio.sleep(0.06)
                await cdp.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": click_x,
                        "y": click_y,
                        "button": "left",
                        "buttons": 0,
                        "clickCount": 1,
                        "pointerType": "mouse",
                    },
                    timeout=2.0,
                )
                break
            except Exception:
                continue
    except Exception:
        return


async def _run_single_browser_attempt(
    browser_bin: str,
    url: str,
    *,
    headless: bool,
    proxy_url: Optional[str],
    verify_ssl: bool,
    custom_cookie: str,
    timeout_sec: float,
) -> Optional[Dict[str, Any]]:
    debug_port = _pick_ephemeral_port()
    profile_dir = _get_persistent_profile_dir(headless=headless, proxy_url=proxy_url)
    proc: Optional[subprocess.Popen[bytes]] = None
    chromium_proxy = _normalize_chromium_proxy(proxy_url)

    cmd: List[str] = [browser_bin]
    if headless:
        cmd.extend(["--headless=new", "--window-size=1366,768"])
    else:
        # Unminimized off-screen window so `document.visibilityState === 'visible'` and
        # `requestAnimationFrame` runs at full speed for Turnstile without stealing user focus!
        cmd.extend(
            [
                "--window-position=-3200,-3200",
                "--window-size=1280,800",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        )

    cmd.extend(
        [
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-sync",
            "--mute-audio",
            "--lang=en-US",
        ]
    )
    if not verify_ssl:
        cmd.append("--ignore-certificate-errors")
    if chromium_proxy:
        cmd.extend([f"--proxy-server={chromium_proxy}", "--disable-quic"])
    if os.name != "nt":
        cmd.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    cmd.append("about:blank")

    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        if headless:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        else:
            startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            # SW_SHOWNOACTIVATE = 4 (unminimized so visibilityState='visible', without stealing focus!)
            startupinfo.wShowWindow = 4
            popen_kwargs["startupinfo"] = startupinfo

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)

        ws_url: Optional[str] = None
        raw_ua = ""
        deadline = time.monotonic() + 7.5
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

        ua_override = _build_ua_override_params(raw_ua, browser_bin)
        clean_ua = str(ua_override["userAgent"])

        async with ws_connect(ws_url, max_size=32 * 1024 * 1024) as ws:
            cdp = _RawCdpClient(ws, ua_override=ua_override)
            try:
                await cdp.call("Network.enable")
                await cdp.call("Page.enable")
                await cdp.call("DOM.enable")
                await cdp.call("Network.setUserAgentOverride", ua_override)
                await cdp.call("Emulation.setUserAgentOverride", ua_override)
                await cdp.call(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": STEALTH_INIT_SCRIPT},
                )
                # Auto-attach to cross-origin OOPIF targets (e.g. challenges.cloudflare.com)
                try:
                    await cdp.call(
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                    )
                except Exception:
                    pass

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

                await asyncio.sleep(0.9)
                html = await cdp.get_outer_html()

                nudged = False
                wait_deadline = time.monotonic() + min(20.0, max(10.0, timeout_sec - 3.0))
                poll_idx = 0
                while time.monotonic() < wait_deadline and (
                    cdp.doc_status in (403, 429, 503) or _looks_like_js_challenge(html)
                ):
                    poll_idx += 1
                    if poll_idx in (2, 5, 9) or not nudged:
                        nudged = True
                        await _nudge_turnstile_if_present(cdp)
                    await asyncio.sleep(1.0)
                    html = await cdp.get_outer_html()
                    if cdp.doc_status == 200 and html and not _looks_like_js_challenge(html):
                        break

                raw_body = await cdp.get_raw_response_body()
                ctype = cdp.doc_headers.get("content-type", "").lower()
                if raw_body and (
                    any(t in ctype for t in ("xml", "json", "rss", "atom"))
                    or raw_body.lstrip().startswith(("<?xml", "<rss", "<feed", "{", "["))
                ):
                    final_text = raw_body
                else:
                    final_text = html or raw_body or ""

                is_solved = cdp.doc_status < 400 and bool(final_text) and not _looks_like_js_challenge(final_text)

                if is_solved:
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
                    if relevant_pairs:
                        CLEARANCE_CACHE.put(
                            cdp.doc_url or url,
                            proxy_url,
                            cookie_header="; ".join(relevant_pairs),
                            user_agent=clean_ua,
                            via_browser=True,
                        )

                # Ask Chromium to flush cookies cleanly to persistent profile before exit
                try:
                    await cdp.call("Browser.close", {}, timeout=1.5)
                except Exception:
                    pass

                return {
                    "url": cdp.doc_url or url,
                    "status_code": cdp.doc_status,
                    "text": final_text,
                    "headers": cdp.doc_headers,
                    "via": "browser_cdp" if headless else "browser_cdp_gui",
                    "solved": is_solved,
                }
            finally:
                await cdp.close()
    except Exception:
        return None
    finally:
        if proc is not None:
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
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
    Solves Cloudflare JS Managed Challenges, Cross-Origin Turnstile OOPIFs, Akamai, & DataDome
    using the locally installed Chrome/Edge/Brave browser:
      - Pass 1: Invisible `--headless=new` with OOPIF Client Hints (`Target.setAutoAttach`) + GPU.
      - Pass 2: If Turnstile still loops in headless mode and a desktop session is available,
                retries in an off-screen unminimized (`SW_SHOWNOACTIVATE`) window with persistent
                cookie/token profile so `/` and `/feed/` pass without disturbing the user.
    """
    if not HAS_WEBSOCKETS or ws_connect is None:
        return None

    # Chromium --proxy-server does not accept inline user:pass@ without an extension
    if proxy_url and "@" in proxy_url:
        return None

    browser_bin = find_local_browser_executable()
    if not browser_bin:
        return None

    target_domain = DomainClearanceCache._base_domain(url)
    can_gui = _can_use_gui_fallback()
    skip_headless = bool(can_gui and target_domain and target_domain in _GUI_REQUIRED_DOMAINS)

    async with _BROWSER_SEMAPHORE:
        res_headless: Optional[Dict[str, Any]] = None
        if not skip_headless:
            res_headless = await _run_single_browser_attempt(
                browser_bin,
                url,
                headless=True,
                proxy_url=proxy_url,
                verify_ssl=verify_ssl,
                custom_cookie=custom_cookie,
                timeout_sec=timeout_sec,
            )
            if res_headless and res_headless.get("solved"):
                return res_headless

            # Do not escalate to Pass 2 for non-challenge errors like 404 or 522
            if res_headless and not (
                int(res_headless.get("status_code") or 0) in (401, 403, 429, 503)
                or _looks_like_js_challenge(str(res_headless.get("text") or ""))
            ):
                return res_headless

        if can_gui:
            res_gui = await _run_single_browser_attempt(
                browser_bin,
                url,
                headless=False,
                proxy_url=proxy_url,
                verify_ssl=verify_ssl,
                custom_cookie=custom_cookie,
                timeout_sec=max(24.0, timeout_sec),
            )
            if res_gui is not None:
                if res_gui.get("solved") and target_domain:
                    _GUI_REQUIRED_DOMAINS.add(target_domain)
                return res_gui

        return res_headless
