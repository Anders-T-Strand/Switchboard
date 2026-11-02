import pytest

from switchboard.config import Config
from switchboard.providers import ClaudeCLI, OllamaClient
from switchboard.router import Router, heuristic_classify, parse_classifier_json


def make_router(cfg: Config) -> Router:
    return Router(cfg, OllamaClient(cfg.ollama), ClaudeCLI(cfg.claude, cfg.claude_workdir))


def test_rules_first_match_wins(cfg):
    r = make_router(cfg)
    assert r.pick_rule("analysis", 5, 1).model == "claude-opus"
    assert r.pick_rule("code", 4, 2).model == "claude-sonnet"
    assert r.pick_rule("chat", 1, 1).model == "qwen-moe"
    assert r.pick_rule("code", 3, 4).model == "qwen-moe"
    assert r.pick_rule("code", 3, 5).name == "deep-reasoning"


def test_task_type_condition():
    from switchboard.config import RuleCfg

    rule = RuleCfg(name="code", model="x", task_types=["code"], min_complexity=3)
    assert rule.matches("code", 3, 1)
    assert not rule.matches("code", 2, 1)
    assert not rule.matches("writing", 5, 5)


def test_invalid_config_is_rejected(cfg):
    raw = cfg.model_dump()
    raw["routing"]["rules"].append({"name": "bad", "model": "nope"})
    with pytest.raises(ValueError, match="unknown model 'nope'"):
        Config.model_validate(raw)


def test_parse_classifier_json_tolerates_noise():
    task, cx, rs, reason = parse_classifier_json(
        'Sure:\n```json\n{"task_type": "CODE", "complexity": 9, "reasoning": 2, "reason": "x"}\n```'
    )
    assert (task, cx, rs, reason) == ("code", 5, 2, "x")
    task, cx, rs, _ = parse_classifier_json('{"task_type": "weird", "complexity": 0, "reason": ""}')
    assert (task, cx, rs) == ("other", 1, 1)  # missing reasoning falls back to complexity


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
    assert call["format"]["required"] == ["task_type", "complexity", "reasoning", "reason"]


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
    plan = make_router(cfg).plan("anything", "claude-opus", "high")
    assert plan.mode == "forced" and plan.chosen == "claude-opus"
    assert plan.classification is None  # nothing left to decide, classifier skipped
    assert (plan.effort, plan.effort_source) == ("high", "forced")
    with pytest.raises(KeyError):
        make_router(cfg).plan("anything", "nope")
    with pytest.raises(KeyError, match="unknown effort"):
        make_router(cfg).plan("anything", "auto", "turbo")


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


# Effort ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt, model, effort, think",
    [
        ("C5 R1", "claude-opus", "low", None),      # needs Opus, barely needs to think
        ("C5 R5", "claude-opus", "xhigh", None),
        ("C4 R4", "claude-sonnet", "high", None),
        ("C4 R5", "claude-sonnet", "xhigh", None),  # Sonnet thinking hard
        ("C3 R5", "claude-sonnet", "xhigh", None),  # deep-reasoning rule
        ("C2 R1", "qwen-moe", "low", False),        # local, thinking off
        ("C2 R4", "qwen-moe", "high", True),        # local, thinking on
    ],
)
def test_effort_follows_reasoning(cfg, fake_ollama, prompt, model, effort, think):
    plan = make_router(cfg).plan(prompt)
    assert plan.chosen == model
    assert plan.effort == effort and plan.effort_source == "reasoning"
    assert plan.think is think


def test_rule_can_pin_or_clamp_effort(cfg, fake_ollama):
    cfg.routing.rules[0].max_effort = "high"  # expert: cap Opus at high
    plan = make_router(cfg).plan("C5 R5")
    assert plan.effort == "high"
    assert "capped from xhigh to high by rule expert" in plan.notes[0]

    cfg.routing.rules[1].min_effort = "medium"  # hard: never below medium
    assert make_router(cfg).plan("C4 R1").effort == "medium"

    cfg.routing.rules[1].effort = "max"
    plan = make_router(cfg).plan("C4 R1")
    assert (plan.effort, plan.effort_source) == ("max", "rule")


def test_model_cap_applies_unless_forced(cfg, fake_ollama):
    cfg.models["claude-opus"].max_effort = "medium"
    plan = make_router(cfg).plan("C5 R5")
    assert plan.route_effort == "xhigh" and plan.effort == "medium"
    assert "capped at medium for Claude Opus" in plan.notes[-1]
    assert make_router(cfg).plan("C5 R5", "auto", "max").effort == "max"


def test_forced_effort_with_auto_model(cfg, fake_ollama):
    plan = make_router(cfg).plan("C5 R5", "auto", "low")
    assert plan.chosen == "claude-opus" and plan.effort == "low" and plan.effort_source == "forced"


def test_forced_model_still_classifies_for_effort(cfg, fake_ollama):
    plan = make_router(cfg).plan("C2 R4", "claude-sonnet")
    assert plan.classification is not None and plan.rule is None
    assert plan.effort == "high"


def test_missing_effort_level_leaves_claude_default(cfg, fake_ollama):
    cfg.routing.effort.by_reasoning = {5: "xhigh"}
    plan = make_router(cfg).plan("C4 R2")
    assert plan.effort is None and plan.effort_source is None


def test_effort_reaches_claude_and_ollama(cfg, fake_ollama):
    r = make_router(cfg)
    comp, used, _ = r.execute(r.plan("C4 R5"), "x", None)
    assert used == "claude-sonnet" and "effort=xhigh" in comp.text
    comp, used, _ = r.execute(r.plan("C2 R1"), "x", None)
    assert fake_ollama.chat_calls[-1]["think"] is False and comp.extra["think"] is False
    r.execute(r.plan("C2 R4"), "x", None)
    assert fake_ollama.chat_calls[-1]["think"] is True


def test_pinned_think_wins(cfg, fake_ollama):
    cfg.models["qwen-moe"].think = True
    assert make_router(cfg).plan("C1 R1").think is True


def test_fallback_keeps_effort(cfg, fake_ollama):
    r = make_router(cfg)
    plan = r.plan("C2 R4")
    fake_ollama.down = True
    comp, used, _ = r.execute(plan, "x", None)
    assert used == "claude-sonnet" and "effort=high" in comp.text


def test_invalid_effort_config_rejected(cfg):
    raw = cfg.model_dump()
    raw["routing"]["effort"]["by_reasoning"] = {7: "low"}
    with pytest.raises(ValueError, match="keys must be 1-5"):
        Config.model_validate(raw)
    raw = cfg.model_dump()
    raw["routing"]["rules"][0]["effort"] = "turbo"
    with pytest.raises(ValueError):
        Config.model_validate(raw)
