"""Command line entry point.

    python -m switchboard                 start the server and dashboard
    python -m switchboard app             start it as a desktop app: its own window and a tray icon
    python -m switchboard ask "prompt"    send a prompt to the running server
    python -m switchboard brief           build today's morning brief (also: brief status, brief connect canvas)
    python -m switchboard speech connect  save your ElevenLabs key for read aloud (also: speech forget, speech status)
    python -m switchboard sync            read Claude Code logs once and exit
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchboard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open", action="store_true", help="open the dashboard in your browser once it starts")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="start the server (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--open", action="store_true", default=argparse.SUPPRESS,
                       help="open the dashboard in your browser once it starts")

    app = sub.add_parser("app", help="start the server in its own window, with a tray icon")
    app.add_argument("--host")
    app.add_argument("--port", type=int)
    app.add_argument("--hidden", action="store_true", help="start in the tray without showing the window")
    app.add_argument("--devtools", action="store_true",
                     help="open the web inspector, and turn on the right-click menu and browser keys such as F5")

    ask = sub.add_parser("ask", help="send a prompt to the running server")
    ask.add_argument("prompt", nargs="+", help='prompt text, or "-" to read it from stdin')
    ask.add_argument("--model", default="auto", help="model key from config.yaml, or auto")
    ask.add_argument("--effort", default="auto", help="low, medium, high, xhigh, max, or auto")
    ask.add_argument("--task", type=int, help="attach the request to a task id")
    ask.add_argument("--chat", help='continue a chat: its id, or "last" for the most recent one')
    ask.add_argument("--json", action="store_true", help="print the full JSON response")

    brief = sub.add_parser("brief", help="build today's morning brief, or look at the last one")
    brief.add_argument("action", nargs="?", default="build",
                       choices=["build", "status", "demo", "connect", "disconnect"],
                       help="build it now (the default), print the last build as counts, fill a test database "
                            "with made-up items, or connect and disconnect a source")
    brief.add_argument("target", nargs="?", help="what to connect or disconnect: canvas or outlook")

    speech = sub.add_parser("speech", help="read aloud with ElevenLabs: save or forget the API key, or see the numbers")
    speech.add_argument("action", nargs="?", default="status", choices=["connect", "forget", "status"],
                        help="save the API key at a hidden prompt, remove it, or print plays and timings (the default)")

    sub.add_parser("sync", help="read Claude Code logs once and print a summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "app":
        # Before the config loads: the app may have no console, so it reports a bad config itself.
        from . import desktop

        return desktop.run(args)

    from .config import ConfigError, get_config

    try:
        cfg = get_config()
    except ConfigError as e:  # what's wrong and where, without a traceback in front of it
        print(f"Switchboard's config didn't load.\n\n{e}", file=sys.stderr)
        return 1

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

    if args.cmd == "brief":
        from .brief import cli as brief_cli

        return brief_cli.run(args, cfg)

    if args.cmd == "speech":
        from .speech import cli as speech_cli

        return speech_cli.run(args, cfg)

    if args.cmd == "ask":
        import httpx

        prompt = sys.stdin.read() if args.prompt == ["-"] else " ".join(args.prompt)
        host = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
        base = f"http://{host}:{cfg.server.port}"
        url = f"{base}/api/prompt"
        try:
            chat_id = None
            if args.chat == "last":
                latest = httpx.get(f"{base}/api/conversations", params={"limit": 1}, timeout=10).json()["items"]
                chat_id = latest[0]["id"] if latest else None
            elif args.chat:
                chat_id = int(args.chat)
            r = httpx.post(
                url,
                json={"prompt": prompt, "model": args.model, "effort": args.effort, "task_id": args.task,
                      "conversation_id": chat_id},
                timeout=None,
            )
        except httpx.HTTPError as e:
            print(f"Could not reach the server at {url}. Is it running? ({e})", file=sys.stderr)
            return 1
        except ValueError:
            print('--chat takes a chat id or "last"', file=sys.stderr)
            return 2
        data = r.json()
        if r.status_code != 200:
            print(f"Error: {data.get('detail', data)}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        routing = data.get("routing") or {}
        c = routing.get("classification") or {}
        answered = data.get("model_label") or "none"
        if data.get("provider") == "ollama" and data.get("think") is not None:
            answered += f", thinking {'on' if data['think'] else 'off'}"
        elif data.get("effort"):
            answered += f" at {data['effort']} effort"
        bits = [f"-> {answered}"]
        if c:
            bits.append(f"{c.get('task_type')} / complexity {c.get('complexity')} / reasoning {c.get('reasoning')}")
        bits.append(f"{data.get('input_tokens', 0)} in / {data.get('output_tokens', 0)} out")
        if data.get("latency_ms") is not None:
            bits.append(f"{data['latency_ms'] / 1000:.1f}s")
        print(f"[{' | '.join(bits)}]", file=sys.stderr)
        if data.get("route_note"):
            print(f"[{data['route_note']}]", file=sys.stderr)
        print(f"[chat {data.get('conversation_id')}, turn {data.get('turn')}: {data.get('conversation_title')}. "
              f"Continue with --chat {data.get('conversation_id')}]", file=sys.stderr)
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
