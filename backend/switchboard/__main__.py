"""Command line entry point.

    python -m switchboard                 start the server and dashboard
    python -m switchboard ask "prompt"    send a prompt to the running server
    python -m switchboard sync            read Claude Code logs once and exit
"""

from __future__ import annotations

import argparse
import json
import sys


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _open_when_ready(url: str) -> None:
    """Open the browser once the server answers, without blocking startup."""
    import threading
    import time
    import urllib.request
    import webbrowser

    def wait_and_open() -> None:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{url}/api/health", timeout=1)
                webbrowser.open(url)
                return
            except OSError:
                time.sleep(0.5)

    threading.Thread(target=wait_and_open, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(prog="switchboard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open", action="store_true", help="open the dashboard in your browser once it starts")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="start the server (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--open", action="store_true", default=argparse.SUPPRESS,
                       help="open the dashboard in your browser once it starts")

    ask = sub.add_parser("ask", help="send a prompt to the running server")
    ask.add_argument("prompt", nargs="+", help='prompt text, or "-" to read it from stdin')
    ask.add_argument("--model", default="auto", help="model key from config.yaml, or auto")
    ask.add_argument("--task", type=int, help="attach the request to a task id")
    ask.add_argument("--json", action="store_true", help="print the full JSON response")

    sub.add_parser("sync", help="read Claude Code logs once and print a summary")

    args = parser.parse_args(argv)

    from .config import get_config

    cfg = get_config()

    if args.cmd in (None, "serve"):
        import uvicorn

        from .main import create_app

        host = getattr(args, "host", None) or cfg.server.host
        port = getattr(args, "port", None) or cfg.server.port
        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
        print(f"Switchboard on {url}")
        if args.open:
            _open_when_ready(url)
        uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")
        return 0

    if args.cmd == "ask":
        import httpx

        prompt = sys.stdin.read() if args.prompt == ["-"] else " ".join(args.prompt)
        host = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
        url = f"http://{host}:{cfg.server.port}/api/prompt"
        try:
            r = httpx.post(url, json={"prompt": prompt, "model": args.model, "task_id": args.task}, timeout=None)
        except httpx.HTTPError as e:
            print(f"Could not reach the server at {url}. Is it running? ({e})", file=sys.stderr)
            return 1
        data = r.json()
        if r.status_code != 200:
            print(f"Error: {data.get('detail', data)}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        routing = data.get("routing") or {}
        c = routing.get("classification") or {}
        bits = [f"-> {data.get('model_label') or 'none'}"]
        if c:
            bits.append(f"{c.get('task_type')} / complexity {c.get('complexity')}")
        bits.append(f"{data.get('input_tokens', 0)} in / {data.get('output_tokens', 0)} out")
        if data.get("latency_ms") is not None:
            bits.append(f"{data['latency_ms'] / 1000:.1f}s")
        print(f"[{' | '.join(bits)}]", file=sys.stderr)
        if data.get("route_note"):
            print(f"[{data['route_note']}]", file=sys.stderr)
        if data.get("status") != "ok":
            print(f"Error: {data.get('error')}", file=sys.stderr)
            return 1
        print(data.get("response") or "")
        return 0

    if args.cmd == "sync":
        from . import claude_logs
        from .db import init_db

        init_db(cfg.db_path)
        print(json.dumps(claude_logs.sync(cfg), indent=2))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
