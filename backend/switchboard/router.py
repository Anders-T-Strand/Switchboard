"""Classifies prompts, picks which model answers them and how much it should think."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from .config import EFFORTS, Config, RuleCfg, clamp_effort
from .providers import ClaudeCLI, Completion, OllamaClient, ProviderError
from .providers.ollama import normalize_name

TASK_TYPES = ["chat", "factual", "writing", "code", "math", "analysis", "planning", "other"]

CLASSIFIER_SYSTEM = """You label prompts for a model router. Read the user's prompt and rate it. Do not answer it.

task_type is one of:
- chat: greetings, small talk
- factual: recalling or looking up a fact
- writing: drafting, editing or rewriting prose
- code: writing, reviewing, explaining or debugging code
- math: calculations, proofs, quantitative reasoning
- analysis: comparing options, evaluating, explaining something complex
- planning: plans, strategies, step-by-step designs
- other: anything else

complexity (1 to 5) is how capable the model has to be: depth of knowledge, nuance, judgment, and how high the quality bar is.
1 trivial: greeting, one-line fact, simple rephrase
2 easy: short explanation, small self-contained snippet, summary of a short text
3 moderate: multi-part answer, standard coding task, structured writing
4 hard: tricky debugging, system or architecture design, long writing where quality matters a lot
5 expert: specialist knowledge, research-level questions, high-stakes correctness

reasoning (1 to 5) is how much step-by-step thinking the answer needs before it can be written.
1 none: the answer can be written straight away
2 a little: one or two quick checks
3 some: work through a few steps
4 a lot: multi-step derivation, tracing code paths, weighing several options against each other
5 extensive: long proofs or calculations, many cases to check, subtle bugs

Rate complexity and reasoning separately. A polished speech in a famous writer's style can be complexity 5 and reasoning 1. Finding an off-by-one error in a short loop can be complexity 2 and reasoning 4.
Judge by how hard the prompt is to answer well, not by how long it is.
Reply with JSON only: {"task_type": ..., "complexity": ..., "reasoning": ..., "reason": "<one short sentence>"}"""

CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": TASK_TYPES},
        "complexity": {"type": "integer", "minimum": 1, "maximum": 5},
        "reasoning": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": ["task_type", "complexity", "reasoning", "reason"],
}


@dataclass
class Classification:
    task_type: str
    complexity: int
    reasoning: int
    reason: str
    source: str  # model | heuristic
    model: str | None = None
    ms: int = 0


@dataclass
class RoutePlan:
    mode: str  # auto | forced (model picked by hand)
    chosen: str
    routed: str
    rule: str | None = None
    classification: Classification | None = None
    notes: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    # Effort after rule pins and clamps, before any per-model cap.
    route_effort: str | None = None
    # Where route_effort came from: forced | rule | reasoning. None means Claude Code's own setting.
    effort_source: str | None = None
    # What the chosen model will actually run with.
    effort: str | None = None
    think: bool | None = None

    def to_dict(self, cfg: Config) -> dict:
        d = asdict(self)
        d["chosen_label"] = cfg.label(self.chosen)
        d["routed_label"] = cfg.label(self.routed)
        return d


# Heuristic fallback -----------------------------------------------------------

_CODE = re.compile(
    r"```|\bdef |\bclass \w|\bimport \w|\bfunction\b|=>|\bconst \w|\btraceback\b|\bexception\b|"
    r"\berror:|\bstack ?trace\b|\bdebug\b|\bbug\b|\bcompile|\bsql\b|\bregex\b|\bapi\b|\bpython\b|"
    r"\bjavascript\b|\btypescript\b|\bjava\b|\brust\b|\bc\+\+|\bdocker",
    re.IGNORECASE,
)
_MATH = re.compile(
    r"\bprove\b|\bproof\b|\bintegral\b|\bderivative\b|\bequation\b|\bprobability\b|\bmatrix\b|"
    r"\bsolve\b|\d+\s*[-+*/^]\s*\d+",
    re.IGNORECASE,
)
_WRITING = re.compile(r"\b(write|draft|rewrite|rephrase|email|essay|letter|post|proofread|edit)\b", re.IGNORECASE)
_PLANNING = re.compile(r"\b(plan|roadmap|schedule|strategy|itinerary|steps to)\b", re.IGNORECASE)
_ANALYSIS = re.compile(r"\b(compare|analy[sz]e|trade-?offs?|pros and cons|evaluate|versus|vs\.?)\b", re.IGNORECASE)
_CHAT = re.compile(r"^\s*(hi|hey|hello|thanks|thank you|good (morning|evening|night))\b", re.IGNORECASE)
_HARD = re.compile(
    r"\b(architect|architecture|distributed|concurren|race condition|optimi[sz]e|scalab|"
    r"prove|formal|security|threat model|end-to-end|production)\w*",
    re.IGNORECASE,
)


def heuristic_classify(prompt: str) -> Classification:
    p = prompt.strip()
    n = len(p)
    if _CHAT.match(p) and n < 60:
        task = "chat"
    elif _CODE.search(p):
        task = "code"
    elif _MATH.search(p):
        task = "math"
    elif _ANALYSIS.search(p):
        task = "analysis"
    elif _PLANNING.search(p):
        task = "planning"
    elif _WRITING.search(p):
        task = "writing"
    elif n < 120 and p.endswith("?"):
        task = "factual"
    else:
        task = "other"

    if n < 80:
        complexity = 1
    elif n < 400:
        complexity = 2
    elif n < 1500:
        complexity = 3
    else:
        complexity = 4
    if task in ("code", "math") and complexity < 2:
        complexity = 2
    if _HARD.search(p):
        complexity += 1
    complexity = max(1, min(5, complexity))

    if task in ("chat", "factual"):
        reasoning = 1
    elif task in ("code", "math", "analysis", "planning"):
        reasoning = complexity
    else:
        reasoning = max(1, complexity - 1)
    return Classification(task, complexity, reasoning, f"heuristic: {n} chars, keyword match", "heuristic")


def parse_classifier_json(text: str) -> tuple[str, int, int, str]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in classifier output")
    data = json.loads(match.group(0))
    task = str(data.get("task_type", "other")).lower().strip()
    if task not in TASK_TYPES:
        task = "other"
    complexity = max(1, min(5, int(data.get("complexity", 3))))
    reasoning = max(1, min(5, int(data.get("reasoning", complexity))))
    reason = str(data.get("reason", "")).strip()[:300]
    return task, complexity, reasoning, reason


# Router -----------------------------------------------------------------------


class Router:
    def __init__(self, cfg: Config, ollama: OllamaClient, claude: ClaudeCLI):
        self.cfg = cfg
        self.ollama = ollama
        self.claude = claude

    # Which configured local models are in VRAM right now.
    def loaded_local_keys(self) -> list[str]:
        loaded = self.ollama.loaded_models() or []
        loaded_set = set(loaded)
        return [
            key for key, m in self.cfg.models.items()
            if m.provider == "ollama" and normalize_name(m.model) in loaded_set
        ]

    def pick_rule(self, task_type: str, complexity: int, reasoning: int) -> RuleCfg | None:
        for rule in self.cfg.routing.rules:
            if rule.matches(task_type, complexity, reasoning):
                return rule
        return None

    def classify(self, prompt: str, loaded_keys: list[str]) -> Classification:
        ccfg = self.cfg.routing.classifier
        key = ccfg.model
        if ccfg.use_loaded_model and loaded_keys:
            key = loaded_keys[0]
        model = self.cfg.models[key]
        snippet = prompt[: ccfg.max_prompt_chars]
        t0 = time.perf_counter()
        try:
            comp = self.ollama.chat(
                model.model,
                [
                    {"role": "system", "content": CLASSIFIER_SYSTEM},
                    {"role": "user", "content": f"Prompt to classify:\n<<<\n{snippet}\n>>>"},
                ],
                think=ccfg.think,
                fmt=CLASSIFIER_SCHEMA,
                options={"temperature": 0},
                timeout=ccfg.timeout_s,
            )
            task, complexity, reasoning, reason = parse_classifier_json(comp.text)
            return Classification(task, complexity, reasoning, reason, "model", key,
                                  int((time.perf_counter() - t0) * 1000))
        except (ProviderError, ValueError) as e:
            c = heuristic_classify(prompt)
            c.reason = f"{c.reason} (classifier unavailable: {str(e)[:120]})"
            c.ms = int((time.perf_counter() - t0) * 1000)
            return c

    # Effort ------------------------------------------------------------------------

    def route_effort(self, rule: RuleCfg | None, c: Classification | None,
                     forced: str | None, notes: list[str]) -> tuple[str | None, str | None]:
        """Effort for this route, before per-model caps."""
        if forced:
            return forced, "forced"
        level, source = None, None
        if rule and rule.effort:
            level, source = rule.effort, "rule"
        elif c is not None:
            level = self.cfg.routing.effort.by_reasoning.get(c.reasoning)
            source = "reasoning" if level else None
        if level and rule and (rule.min_effort or rule.max_effort):
            clamped = clamp_effort(level, rule.min_effort, rule.max_effort)
            if clamped != level:
                verb = "raised" if EFFORTS.index(clamped) > EFFORTS.index(level) else "capped"
                notes.append(f"effort {verb} from {level} to {clamped} by rule {rule.name}")
                level = clamped
        return level, source

    def effort_for(self, key: str, plan: RoutePlan) -> str | None:
        """The effort a given model runs at for this plan, after its own cap."""
        level = plan.route_effort
        cap = self.cfg.models[key].max_effort
        if level and cap and plan.effort_source != "forced":
            level = clamp_effort(level, None, cap)
        return level

    def think_for(self, key: str, effort: str | None) -> bool | None:
        """Thinking on or off for a local model. None leaves Ollama's default alone."""
        m = self.cfg.models[key]
        if m.provider != "ollama":
            return None
        if m.think is not None:
            return m.think
        if effort is None:
            return None
        return effort != "low"

    # Planning ------------------------------------------------------------------------

    def plan(self, prompt: str, forced_model: str | None = None, forced_effort: str | None = None) -> RoutePlan:
        model_forced = bool(forced_model) and forced_model != "auto"
        effort_forced = bool(forced_effort) and forced_effort != "auto"
        if model_forced and forced_model not in self.cfg.models:
            raise KeyError(f"unknown model '{forced_model}'")
        if effort_forced and forced_effort not in EFFORTS:
            raise KeyError(f"unknown effort '{forced_effort}', use one of {', '.join(EFFORTS)} or auto")

        loaded = self.loaded_local_keys()
        # The classifier still runs when only the model is forced, because it decides the effort.
        c = None if (model_forced and effort_forced) else self.classify(prompt, loaded)

        rule = None
        if model_forced:
            plan = RoutePlan(mode="forced", chosen=forced_model, routed=forced_model,
                             classification=c, loaded=loaded)
        else:
            rule = self.pick_rule(c.task_type, c.complexity, c.reasoning)
            routed = rule.model if rule else (self.cfg.routing.fallback_order or list(self.cfg.models))[0]
            plan = RoutePlan(mode="auto", chosen=routed, routed=routed,
                             rule=rule.name if rule else None, classification=c, loaded=loaded)
            target = self.cfg.models[routed]
            if (
                self.cfg.routing.prefer_loaded_local
                and target.provider == "ollama"
                and loaded
                and routed not in loaded
            ):
                plan.chosen = loaded[0]
                plan.notes.append(
                    f"used {self.cfg.label(loaded[0])} (already loaded) instead of swapping to {self.cfg.label(routed)}"
                )

        plan.route_effort, plan.effort_source = self.route_effort(
            rule, c, forced_effort if effort_forced else None, plan.notes
        )
        plan.effort = self.effort_for(plan.chosen, plan)
        if plan.effort != plan.route_effort and plan.effort:
            plan.notes.append(f"effort capped at {plan.effort} for {self.cfg.label(plan.chosen)}")
        plan.think = self.think_for(plan.chosen, plan.effort)
        return plan

    # Running -------------------------------------------------------------------------

    def run_model(self, key: str, prompt: str, system_prompt: str | None,
                  effort: str | None = None) -> Completion:
        m = self.cfg.models[key]
        if m.provider == "ollama":
            think = self.think_for(key, effort)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            comp = self.ollama.chat(m.model, messages, think=think, options=m.options or None)
            comp.extra["think"] = think
        else:
            comp = self.claude.complete(m.model, prompt, system_prompt, effort)
        comp.extra["effort"] = effort
        return comp

    def execute(self, plan: RoutePlan, prompt: str, system_prompt: str | None) -> tuple[Completion, str, list[str]]:
        """Run the plan. Falls back through routing.fallback_order on failure (auto mode only)."""
        attempts = [plan.chosen]
        if plan.mode == "auto":
            attempts += [k for k in self.cfg.routing.fallback_order if k != plan.chosen]
        errors: list[str] = []
        for key in attempts:
            try:
                return self.run_model(key, prompt, system_prompt, self.effort_for(key, plan)), key, errors
            except ProviderError as e:
                errors.append(f"{self.cfg.label(key)}: {e}")
        raise ProviderError(" | ".join(errors))
