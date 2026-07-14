from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Launch the local AI-cutting Web app")
    parser.add_argument("--check", action="store_true", help="validate launcher without starting the server")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        print("[错误] AI-cutting 需要 Python 3.10 或更高版本。")
        return 1
    try:
        import uvicorn
        from scripts.find_web_port import port_available
    except ImportError as exc:
        print(f"[错误] 缺少 Python 依赖：{exc.name}")
        print(f'请运行："{sys.executable}" -m pip install -r requirements.txt')
        return 1

    port = next((candidate for candidate in range(8000, 8010) if port_available(candidate)), None)
    if port is None:
        print("[错误] 8000-8009 端口都已占用。")
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"Python: {sys.executable}")
    print(f"页面: {url}")
    if args.check:
        print("启动器检查通过。")
        return 0
    print("关闭此窗口会停止服务。")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("web.app:app", host="127.0.0.1", port=port, app_dir=str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
