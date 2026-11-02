import type { RequestDetail, RoutePlan } from "../api";
import { capitalize, compact, duration, effortLabel, usd } from "../format";
import { useApp } from "../hooks";
import { ModelTag } from "./bits";

export type RouteView = {
  mode: "auto" | "forced";
  taskType: string | null;
  complexity: number | null;
  reasoning: number | null;
  source: string | null;
  classifierModel: string | null;
  classifierMs: number | null;
  reason: string | null;
  rule: string | null;
  routed: string | null;
  answeredBy: string | null;
  effort: string | null;
  effortSource: string | null;
  think: boolean | null;
  note: string | null;
};

export function fromPlan(p: RoutePlan): RouteView {
  const c = p.classification;
  return {
    mode: p.mode,
    taskType: c?.task_type ?? null,
    complexity: c?.complexity ?? null,
    reasoning: c?.reasoning ?? null,
    source: c?.source ?? null,
    classifierModel: c?.model ?? null,
    classifierMs: c?.ms ?? null,
    reason: c?.reason ?? null,
    rule: p.rule,
    routed: p.routed,
    answeredBy: p.chosen,
    effort: p.effort,
    effortSource: p.effort_source,
    think: p.think,
    note: p.notes.join("; ") || null,
  };
}

export function fromDetail(d: RequestDetail): RouteView {
  return {
    mode: d.route_mode,
    taskType: d.task_type,
    complexity: d.complexity,
    reasoning: d.reasoning,
    source: d.classifier_source,
    classifierModel: d.classifier_model,
    classifierMs: d.classifier_ms,
    reason: d.classifier_reason,
    rule: d.rule,
    routed: d.routed_model,
    answeredBy: d.model_key,
    effort: d.effort,
    effortSource: d.effort_source,
    think: d.think === null || d.think === undefined ? null : Boolean(d.think),
    note: d.route_note,
  };
}

/** What the chosen model was told about thinking, in plain words. */
export function effortText(provider: string | null | undefined, effort: string | null, think: boolean | null): string {
  if (provider === "ollama") return think === null ? "Default" : think ? "Thinking on" : "Thinking off";
  return effortLabel(effort);
}

function effortWhy(view: RouteView, provider: string | undefined): string {
  if (view.effortSource === "forced") return "picked by hand";
  if (view.effortSource === "rule") return `pinned by rule ${view.rule ?? ""}`.trim();
  if (view.effortSource === "reasoning" && view.reasoning) {
    return provider === "ollama"
      ? `reasoning ${view.reasoning} maps to ${effortLabel(view.effort).toLowerCase()}`
      : `from reasoning ${view.reasoning}`;
  }
  return provider === "ollama" ? "" : "your own /effort setting";
}

/** The routing decision as the steps it actually went through. */
export function RouteSteps({ view, pending = false }: { view: RouteView; pending?: boolean }) {
  const { label, config } = useApp();
  const target = view.answeredBy ?? view.routed;
  const provider = target ? config?.models[target]?.provider : undefined;
  const why = effortWhy(view, provider);

  return (
    <ol className="route-steps">
      <li>
        <span className="step-name">Classified</span>
        {view.complexity === null ? (
          <span className="muted">Skipped, model and effort picked by hand</span>
        ) : (
          <span>
            <strong>{capitalize(view.taskType ?? "other")}</strong>, complexity <strong>{view.complexity}</strong>,
            reasoning <strong>{view.reasoning ?? "–"}</strong>{" "}
            <span className="muted">
              {view.source === "heuristic"
                ? "by keyword heuristic"
                : `by ${label(view.classifierModel)} in ${duration(view.classifierMs)}`}
            </span>
            {view.reason && <span className="reason">{view.reason}</span>}
          </span>
        )}
      </li>
      <li>
        <span className="step-name">Routed</span>
        <span>
          {view.mode === "forced" ? (
            <>
              Picked by hand: <ModelTag modelKey={view.routed} />
            </>
          ) : (
            <>
              Rule <code>{view.rule ?? "none"}</code> picked <ModelTag modelKey={view.routed} />
            </>
          )}
        </span>
      </li>
      <li>
        <span className="step-name">Effort</span>
        <span>
          <strong>{effortText(provider, view.effort, view.think)}</strong>
          {why && <span className="muted">, {why}</span>}
        </span>
      </li>
      <li>
        <span className="step-name">{pending ? "Will answer" : "Answered"}</span>
        <span>
          {view.answeredBy ? <ModelTag modelKey={view.answeredBy} /> : <span className="error-text">No model could answer</span>}
          {view.note && <span className="reason">{view.note}</span>}
        </span>
      </li>
    </ol>
  );
}

export function UsageLine({ d }: { d: RequestDetail }) {
  const parts = [
    ...(d.context_turns ? [`${d.context_turns} earlier ${d.context_turns === 1 ? "turn" : "turns"} sent along`] : []),
    `${compact(d.input_tokens + d.cache_read_tokens + d.cache_write_tokens)} tokens in`,
    `${compact(d.output_tokens)} out`,
    duration(d.latency_ms),
  ];
  if (d.load_ms && d.load_ms > 500) parts.push(`${duration(d.load_ms)} loading the model`);
  if (d.provider === "claude_cli") parts.push(`${usd(d.cost_usd)} at API rates`);
  if (d.provider === "ollama" && d.avoided_usd > 0) parts.push(`≈ ${usd(d.avoided_usd)} kept off Claude`);
  return <p className="usage-line">{parts.join(", ")}</p>;
}
