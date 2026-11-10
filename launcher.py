"""
راه‌انداز برنامه روی ویندوز.

این فایل توسط start_windows.bat فراخوانی می‌شود و کارهای زیر را انجام می‌دهد:
  1) ساخت محیط مجازی و نصب وابستگی‌ها (فقط از wheel آماده — بدون نیاز به کامپایلر C)
  2) انتخاب خودکار پورت آزاد (در صورت اشغال بودن پورت پیش‌فرض)
  3) اجرای سرور و باز کردن خودکار مرورگر روی آدرس پنل مدیریت

چرا این فایل وجود دارد؟ منطق کنسول ویندوز (cmd.exe) برای متن فارسی و
پرانتزهای داخل if/else بسیار شکننده است؛ بنابراین تمام منطق به پایتون منتقل
شده تا هیچ دستور پیچیده‌ای در فایل .bat باقی نماند.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MIN_PYTHON = (3, 10)
DEFAULT_PORT = int(os.getenv("WEB_COLLECTOR_PORT", "8000"))
APP_ID = "website-content-collector"


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def is_our_app_running(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2) as response:
            body = response.read(300).decode("utf-8", "replace")
        return APP_ID in body and "ok" in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pick_port(preferred: int) -> int:
    if port_is_free(preferred):
        return preferred
    if is_our_app_running(preferred):
        return preferred
    print(f"      Port {preferred} is already in use, looking for a free one ...")
    for candidate in range(preferred + 1, preferred + 21):
        if port_is_free(candidate):
            print(f"      Using port {candidate} instead.")
            return candidate
    return preferred


def open_browser(port: int) -> None:
    def _open() -> None:
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass

    threading.Timer(3.5, _open).start()


def hr() -> None:
    print("=" * 62)


def fail(message: str) -> int:
    print()
    print("[ERROR] " + message)
    print()
    return 1


def find_system_python() -> str | None:
    """Find a usable Python interpreter *before* .venv exists."""
    for candidate in ("py", "python", "python3"):
        try:
            result = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue
        if result.returncode != 0:
            continue
        try:
            major, minor = (int(x) for x in result.stdout.split()[:2])
        except ValueError:
            continue
        if (major, minor) >= MIN_PYTHON:
            if candidate == "py":
                return "py -3"
            return candidate
    return None


def venv_python() -> str:
    return str(VENV_PY)


def ensure_venv() -> tuple[bool, str]:
    if VENV_PY.exists():
        print("[1/3] Virtual environment found: .venv")
        return True, ""

    print("[1/3] Creating virtual environment (.venv) ...")
    system_python = find_system_python()
    if not system_python:
        return False, (
            "Python 3.10+ not found.\n"
            "        Install it from https://www.python.org/downloads/\n"
            '        IMPORTANT: tick "Add python.exe to PATH" during setup.'
        )

    parts = system_python.split()
    result = subprocess.run([*parts, "-m", "venv", str(VENV_DIR)], cwd=str(ROOT))
    if result.returncode != 0 or not VENV_PY.exists():
        return False, "Failed to create the virtual environment."
    return True, ""


def pip_install(python: str, args: list[str]) -> int:
    cmd = [python, "-m", "pip", "install", "--prefer-binary", "--retries", "5", "--timeout", "60", *args]
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def install_dependencies(python: str) -> tuple[bool, str]:
    print("[2/3] Installing dependencies (no C++ compiler needed) ...")
    subprocess.run(
        [python, "-m", "pip", "install", "--upgrade", "pip"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if pip_install(python, ["-r", "requirements.txt"]) != 0:
        print()
        print("[ERROR] Dependency installation failed.")
        print("        - Check your internet / proxy connection.")
        print("        - Manual retry:")
        print("            .venv\\Scripts\\python.exe -m pip install --prefer-binary -r requirements.txt")
        print()
        return False, "dependencies"

    return True, ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    hr()
    print("  Website Content Collector")
    print("  سامانه هوشمند جمع‌آوری و آرشیو مطالب وب‌سایت‌ها")
    hr()
    print()

    ok, error = ensure_venv()
    if not ok:
        return fail(error)

    python = venv_python()

    ok, error = install_dependencies(python)
    if not ok:
        return fail(error)

    args = list(sys.argv[1:])

    explicit_port: int | None = None
    for idx, arg in enumerate(args):
        if arg in ("--port", "-p") and idx + 1 < len(args):
            try:
                explicit_port = int(args[idx + 1])
            except ValueError:
                explicit_port = None
        elif arg.startswith("--port="):
            try:
                explicit_port = int(arg.split("=", 1)[1])
            except ValueError:
                explicit_port = None

    if explicit_port is not None:
        resolved_port = explicit_port
        if port_is_free(explicit_port) or is_our_app_running(explicit_port):
            open_browser(resolved_port)
    else:
        resolved_port = pick_port(DEFAULT_PORT)
        args += ["--port", str(resolved_port)]
        open_browser(resolved_port)

    print("[3/3] Starting server ...")
    print(f"      Address:  http://localhost:{resolved_port}")
    print("      The browser opens automatically in a few seconds.")
    print("      Press Ctrl + C to stop the program.")
    print()

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    try:
        return subprocess.run([python, str(ROOT / "run.py"), *args], cwd=str(ROOT), env=env).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
