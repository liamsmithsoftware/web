"""
راه‌انداز برنامه روی ویندوز.

این فایل توسط start_windows.bat فراخوانی می‌شود و کارهای زیر را انجام می‌دهد:
  1) ساخت محیط مجازی و نصب وابستگی‌ها (فقط از wheel آماده — بدون نیاز به کامپایلر C)
  2) تشخیص هوشمند اشغال بودن پورت (بدون اختلال از سمت پروکسی ویندوز یا سرویس‌های غیر HTTP)
     و انتقال خودکار به پورت آزاد بعدی در صورت اشغال بودن پورت توسط برنامه دیگر
  3) اجرای سرور و باز کردن خودکار مرورگر روی آدرس پنل مدیریت
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils.port_utils import (  # noqa: E402
    APP_ID,
    find_free_port,
    is_our_app_running,
    is_port_free,
)

VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MIN_PYTHON = (3, 10)
DEFAULT_HOST = os.getenv("WEB_COLLECTOR_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("WEB_COLLECTOR_PORT", "8000"))


def port_is_free(port: int, host: str = DEFAULT_HOST) -> bool:
    return is_port_free(port, host)


def pick_port(preferred: int, host: str = DEFAULT_HOST) -> int:
    if port_is_free(preferred, host):
        return preferred
    if is_our_app_running(preferred, host, app_id=APP_ID):
        return preferred

    new_port = find_free_port(preferred + 1, host=host)
    print(f"      پورت {preferred} توسط برنامه دیگری اشغال است؛ انتقال خودکار به پورت آزاد {new_port} ...")
    print(f"      Port {preferred} is occupied by another program -> using port {new_port} instead.")
    return new_port


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


def _extract_cli_options(args: list[str]) -> tuple[str, int | None, bool, list[str]]:
    """Parse --host, --port/-p, and --strict-port while preserving remaining args."""
    bind_host = DEFAULT_HOST
    explicit_port: int | None = None
    strict_port = False
    cleaned: list[str] = []

    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--strict-port":
            strict_port = True
            cleaned.append(arg)
        elif arg in ("--port", "-p") and idx + 1 < len(args):
            try:
                explicit_port = int(args[idx + 1])
                idx += 2
                continue
            except ValueError:
                cleaned.append(arg)
        elif arg.startswith("--port="):
            try:
                explicit_port = int(arg.split("=", 1)[1])
                idx += 1
                continue
            except ValueError:
                cleaned.append(arg)
        elif arg == "--host" and idx + 1 < len(args):
            bind_host = args[idx + 1]
            cleaned.extend([arg, bind_host])
            idx += 2
            continue
        elif arg.startswith("--host="):
            bind_host = arg.split("=", 1)[1]
            cleaned.append(arg)
        else:
            cleaned.append(arg)
        idx += 1

    return bind_host, explicit_port, strict_port, cleaned


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

    bind_host, explicit_port, strict_port, passthrough_args = _extract_cli_options(list(sys.argv[1:]))
    desired_port = explicit_port if explicit_port is not None else DEFAULT_PORT

    if strict_port:
        resolved_port = desired_port
        if port_is_free(resolved_port, bind_host) or is_our_app_running(resolved_port, bind_host):
            open_browser(resolved_port)
    else:
        resolved_port = pick_port(desired_port, bind_host)
        open_browser(resolved_port)

    run_args = [*passthrough_args, "--port", str(resolved_port)]

    print("[3/3] Starting server ...")
    print(f"      Address:  http://localhost:{resolved_port}")
    print("      The browser opens automatically in a few seconds.")
    print("      Press Ctrl + C to stop the program.")
    print()

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    try:
        return subprocess.run([python, str(ROOT / "run.py"), *run_args], cwd=str(ROOT), env=env).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
