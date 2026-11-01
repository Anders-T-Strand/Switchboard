"""Runs prompts through Claude Code in headless mode (`claude -p`).

Calls go through your Claude Code login, so on a Pro or Max plan they count
against the plan's usage limits rather than API billing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ..config import ClaudeCfg
from .base import Completion, ProviderError

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def install_locations() -> list[Path]:
    """Where Claude Code usually ends up when it isn't on PATH."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return [
            home / ".local" / "bin" / "claude.exe",  # native installer
            appdata / "npm" / "claude.cmd",  # npm install -g
            home / ".claude" / "local" / "claude.cmd",
        ]
    return [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        home / ".npm-global" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]


def parse_cli_output(stdout: str) -> dict:
    """Pull the result object out of `claude -p --output-format json` output.

    Normally a single JSON object. Some versions (or --verbose) print a JSON array
    of events or extra lines, so those shapes are handled too.
    """
    text = stdout.strip()
    if not text:
        raise ProviderError("claude produced no output")
    candidates: list = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    flat: list[dict] = []
    for c in candidates:
        if isinstance(c, list):
            flat.extend(x for x in c if isinstance(x, dict))
        elif isinstance(c, dict):
            flat.append(c)
    results = [x for x in flat if x.get("type") == "result"]
    if results:
        return results[-1]
    if flat:
        return flat[-1]
    raise ProviderError(f"could not parse claude output: {text[:300]}")


def completion_from_result(data: dict, fallback_model: str, latency_ms: int) -> Completion:
    subtype = data.get("subtype")
    if data.get("is_error") or (subtype and subtype != "success"):
        detail = data.get("result") or data.get("error") or subtype or "unknown error"
        if subtype == "error_max_turns":
            detail = "hit the max_turns limit before finishing"
        raise ProviderError(f"claude -p failed: {str(detail)[:300]}")

    usage = data.get("usage") or {}
    model_usage = data.get("modelUsage") or {}
    provider_model = fallback_model
    if model_usage:
        # Claude Code can call a small model internally. The main model is the one
        # that produced the most output.
        provider_model = max(
            model_usage.items(),
            key=lambda kv: (kv[1].get("outputTokens") or 0, kv[1].get("costUSD") or 0),
        )[0]

    cost = data.get("total_cost_usd")
    if cost is None:
        cost = data.get("cost_usd")
    return Completion(
        text=str(data.get("result") or "").strip(),
        provider_model=provider_model,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        latency_ms=latency_ms,
        cost_usd=float(cost) if cost is not None else None,
        extra={
            "session_id": data.get("session_id"),
            "num_turns": data.get("num_turns"),
            "duration_api_ms": data.get("duration_api_ms"),
        },
    )


class ClaudeCLI:
    def __init__(self, cfg: ClaudeCfg, workdir: Path):
        self.cfg = cfg
        self.workdir = workdir
        self._version: tuple[float, str | None] | None = None

    def resolve(self) -> str | None:
        """Find the claude executable: PATH first, then the usual install locations.

        The native installer puts claude in ~/.local/bin, which often isn't on
        PATH yet in a terminal that was open before the install.
        """
        found = shutil.which(self.cfg.command)
        if found:
            return found
        if Path(self.cfg.command).name != self.cfg.command:
            return None  # an explicit path that doesn't exist
        for candidate in install_locations():
            if candidate.is_file():
                return str(candidate)
        return None

    def _env(self) -> dict:
        env = os.environ.copy()
        if self.cfg.use_subscription:
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def version(self) -> str | None:
        """`claude --version`, cached for five minutes since Node startup is slow."""
        if self._version and time.monotonic() - self._version[0] < 300:
            return self._version[1]
        exe = self.resolve()
        ver = None
        if exe:
            try:
                out = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True, timeout=20,
                    env=self._env(), creationflags=_CREATE_NO_WINDOW,
                )
                ver = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
            except (OSError, subprocess.SubprocessError, IndexError):
                ver = None
        self._version = (time.monotonic(), ver)
        return ver

    def complete(self, model_alias: str, prompt: str, system_prompt: str | None = None) -> Completion:
        exe = self.resolve()
        if not exe:
            raise ProviderError(
                f"'{self.cfg.command}' was not found on PATH or in the usual install folders. "
                "Install the Claude Code CLI, or set claude.command in config.yaml to its full path."
            )
        self.workdir.mkdir(parents=True, exist_ok=True)

        args = [exe, "-p", "--output-format", "json", "--model", model_alias,
                "--max-turns", str(self.cfg.max_turns)]

        # The system prompt goes through a file so no user text ever lands on the
        # command line, where Windows .cmd shims mangle quotes and special characters.
        sp_file: Path | None = None
        sp = system_prompt or self.cfg.system_prompt
        if sp:
            sp_file = self.workdir / f"system-{uuid.uuid4().hex}.txt"
            sp_file.write_text(sp, encoding="utf-8")
            args += ["--system-prompt-file", str(sp_file)]
        if self.cfg.disallowed_tools:
            args += ["--disallowedTools", ",".join(self.cfg.disallowed_tools)]
        args += list(self.cfg.extra_args)

        stdin_text = prompt
        if self.cfg.prompt_via == "arg":
            args.append(prompt)
            stdin_text = None

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                args,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.workdir,
                env=self._env(),
                timeout=self.cfg.timeout_s,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"claude -p timed out after {self.cfg.timeout_s:.0f}s") from e
        except OSError as e:
            raise ProviderError(f"could not start claude: {e}") from e
        finally:
            if sp_file is not None:
                sp_file.unlink(missing_ok=True)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        try:
            data = parse_cli_output(proc.stdout)
        except ProviderError:
            err = (proc.stderr or proc.stdout or "").strip()
            raise ProviderError(f"claude exited with code {proc.returncode}: {err[:400]}") from None
        return completion_from_result(data, model_alias, latency_ms)
