import pytest

from switchboard.config import Config
from switchboard.providers import ClaudeCLI, OllamaClient
from switchboard.router import Router, heuristic_classify, parse_classifier_json


def make_router(cfg: Config) -> Router:
    return Router(cfg, OllamaClient(cfg.ollama), ClaudeCLI(cfg.claude, cfg.claude_workdir))


def test_rules_first_match_wins(cfg):
    r = make_router(cfg)
    assert r.pick_rule("analysis", 5).model == "claude-opus"
    assert r.pick_rule("code", 4).model == "claude-sonnet"
    assert r.pick_rule("chat", 1).model == "qwen-moe"
    assert r.pick_rule("code", 3).model == "qwen-moe"


def test_task_type_condition():
    from switchboard.config import RuleCfg

    rule = RuleCfg(name="code", model="x", task_types=["code"], min_complexity=3)
    assert rule.matches("code", 3)
    assert not rule.matches("code", 2)
    assert not rule.matches("writing", 5)


def test_invalid_config_is_rejected(cfg):
    raw = cfg.model_dump()
    raw["routing"]["rules"].append({"name": "bad", "model": "nope"})
    with pytest.raises(ValueError, match="unknown model 'nope'"):
        Config.model_validate(raw)


def test_parse_classifier_json_tolerates_noise():
    task, cx, reason = parse_classifier_json('Sure:\n```json\n{"task_type": "CODE", "complexity": 9, "reason": "x"}\n```')
    assert (task, cx, reason) == ("code", 5, "x")
    task, cx, _ = parse_classifier_json('{"task_type": "weird", "complexity": 0, "reason": ""}')
    assert (task, cx) == ("other", 1)


def test_heuristic_classifier():
    assert heuristic_classify("hi there").task_type == "chat"
    c = heuristic_classify("Why does this Python code raise a KeyError?\n```\nd = {}\nd['x']\n```")
    assert c.task_type == "code" and c.complexity >= 2
    long_design = "Design a distributed architecture for " + "an event pipeline with strict ordering " * 40
    assert heuristic_classify(long_design).complexity >= 4


def test_plan_auto_uses_classifier(cfg, fake_ollama):
    r = make_router(cfg)
    plan = r.plan("hello")
    assert plan.mode == "auto"
    assert plan.classification.source == "model"
    assert plan.chosen == "qwen-moe" and plan.rule == "default"
    plan = r.plan("HARD: fix my race condition")
    assert plan.chosen == "claude-sonnet" and plan.rule == "hard"
    plan = r.plan("EXPERT question")
    assert plan.chosen == "claude-opus"


def test_classifier_uses_loaded_model_and_disables_thinking(cfg, fake_ollama):
    fake_ollama.loaded = ["qwen3.8:27b"]
    make_router(cfg).plan("hello")
    call = fake_ollama.chat_calls[-1]
    assert call["model"] == "qwen3.8:27b"
    assert call["think"] is False
    assert call["format"]["required"] == ["task_type", "complexity", "reason"]


def test_prefer_loaded_local_avoids_swap(cfg, fake_ollama):
    fake_ollama.loaded = ["qwen3.8:27b"]
    plan = make_router(cfg).plan("hello")
    assert plan.routed == "qwen-moe"
    assert plan.chosen == "qwen-dense"
    assert "instead of swapping" in plan.notes[0]


def test_heuristic_fallback_when_ollama_down(cfg, fake_ollama):
    fake_ollama.down = True
    plan = make_router(cfg).plan("hi")
    assert plan.classification.source == "heuristic"
    assert "classifier unavailable" in plan.classification.reason


def test_forced_model(cfg, fake_ollama):
    plan = make_router(cfg).plan("anything", "claude-opus")
    assert plan.mode == "forced" and plan.chosen == "claude-opus" and plan.classification is None
    with pytest.raises(KeyError):
        make_router(cfg).plan("anything", "nope")


def test_execute_falls_back_when_local_fails(cfg, fake_ollama):
    r = make_router(cfg)
    plan = r.plan("hello")
    fake_ollama.down = True
    comp, used, errors = r.execute(plan, "hello", None)
    assert used == "claude-sonnet"
    assert errors and "Ollama unreachable" in errors[0]
    assert comp.text.startswith("claude[sonnet]")


def test_forced_model_does_not_fall_back(cfg, fake_ollama):
    from switchboard.providers import ProviderError

    r = make_router(cfg)
    plan = r.plan("hello", "qwen-moe")
    fake_ollama.down = True
    with pytest.raises(ProviderError):
        r.execute(plan, "hello", None)


def test_think_blocks_are_stripped(cfg, fake_ollama):
    comp = make_router(cfg).run_model("qwen-moe", "hello", None)
    assert comp.text == "local answer from qwen3.6:35b-a3b"
    assert comp.input_tokens == 42 and comp.output_tokens == 7 and comp.load_ms == 2
