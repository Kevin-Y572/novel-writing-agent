# -*- coding: utf-8 -*-
"""WebUI 启动入口 — python -m webapp.server [--host 127.0.0.1] [--port 8765]"""

import argparse


def main():
    parser = argparse.ArgumentParser(description="novel_agent WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    import uvicorn
    from .api import create_app
    app = create_app()
    print(f"\n  ✅ WebUI: http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
