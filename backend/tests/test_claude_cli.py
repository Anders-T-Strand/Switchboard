import json

import pytest

from switchboard.providers import ClaudeCLI, ProviderError
from switchboard.providers.claude_cli import completion_from_result, parse_cli_output


def test_parse_single_object():
    data = parse_cli_output(json.dumps({"type": "result", "subtype": "success", "result": "hi"}))
    assert data["result"] == "hi"


def test_parse_event_array_and_extra_lines():
    events = [{"type": "system"}, {"type": "assistant"}, {"type": "result", "result": "done"}]
    assert parse_cli_output(json.dumps(events))["result"] == "done"
    noisy = "warning: something\n" + json.dumps({"type": "result", "result": "ok"})
    assert parse_cli_output(noisy)["result"] == "ok"


def test_parse_garbage_raises():
    with pytest.raises(ProviderError):
        parse_cli_output("not json at all")


def test_error_results_raise():
    with pytest.raises(ProviderError, match="max_turns"):
        completion_from_result({"type": "result", "subtype": "error_max_turns", "is_error": False}, "sonnet", 1)
    with pytest.raises(ProviderError, match="boom"):
        completion_from_result({"type": "result", "is_error": True, "result": "boom"}, "sonnet", 1)


def test_main_model_and_usage():
    comp = completion_from_result({
        "type": "result", "subtype": "success", "result": " answer ",
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6},
        "modelUsage": {"claude-haiku-5-5": {"outputTokens": 1}, "claude-opus-5-5": {"outputTokens": 99}},
    }, "opus", 1234)
    assert comp.text == "answer"
    assert comp.provider_model == "claude-opus-5-5"
    assert (comp.input_tokens, comp.output_tokens, comp.cache_read_tokens, comp.cache_write_tokens) == (3, 4, 5, 6)
    assert comp.cost_usd == 0.5 and comp.latency_ms == 1234


def test_runs_fake_cli_via_stdin(cfg):
    cli = ClaudeCLI(cfg.claude, cfg.claude_workdir)
    prompt = 'quotes " & percent % and a\nnewline'
    comp = cli.complete("sonnet", prompt)
    assert comp.text.startswith("claude[sonnet]")
    assert "key_present=False" in comp.text  # ANTHROPIC_API_KEY stripped for subscription billing
    assert "system_prompt=True" in comp.text
    assert repr(prompt) in comp.text  # prompt arrived intact through stdin
    assert comp.provider_model == "claude-sonnet-5-5"
    assert list(cfg.claude_workdir.glob("system-*.txt")) == []  # temp file cleaned up
    assert cli.version() == "9.9.9 (Claude Code)"


def test_cli_failure_surfaces_message(cfg):
    with pytest.raises(ProviderError, match="boom"):
        ClaudeCLI(cfg.claude, cfg.claude_workdir).complete("sonnet", "please FAIL")


def test_missing_cli(cfg):
    cfg.claude.command = "definitely-not-installed-claude"
    with pytest.raises(ProviderError, match="not found on PATH"):
        ClaudeCLI(cfg.claude, cfg.claude_workdir).complete("sonnet", "hi")


def test_finds_claude_outside_path(cfg, fake_claude, monkeypatch, tmp_path):
    import sys
    from pathlib import Path

    home = tmp_path / "home"
    name = "claude.exe" if sys.platform == "win32" else "claude"
    target = home / ".local" / "bin" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(fake_claude.read_bytes())
    target.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", "")
    cfg.claude.command = "claude"
    assert ClaudeCLI(cfg.claude, cfg.claude_workdir).resolve() == str(target)


def test_explicit_missing_path_is_not_guessed(cfg):
    cfg.claude.command = "/nope/claude"
    assert ClaudeCLI(cfg.claude, cfg.claude_workdir).resolve() is None
