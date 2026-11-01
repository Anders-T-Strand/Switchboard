import type { RequestDetail, RoutePlan } from "../api";
import { capitalize, compact, duration, usd } from "../format";
import { useApp } from "../hooks";
import { ModelTag } from "./bits";

export type RouteView = {
  mode: "auto" | "forced";
  taskType: string | null;
  complexity: number | null;
  source: string | null;
  classifierModel: string | null;
  classifierMs: number | null;
  reason: string | null;
  rule: string | null;
  routed: string | null;
  answeredBy: string | null;
  note: string | null;
};

export function fromPlan(p: RoutePlan): RouteView {
  const c = p.classification;
  return {
    mode: p.mode,
    taskType: c?.task_type ?? null,
    complexity: c?.complexity ?? null,
    source: c?.source ?? null,
    classifierModel: c?.model ?? null,
    classifierMs: c?.ms ?? null,
    reason: c?.reason ?? null,
    rule: p.rule,
    routed: p.routed,
    answeredBy: p.chosen,
    note: p.notes.join("; ") || null,
  };
}

export function fromDetail(d: RequestDetail): RouteView {
  return {
    mode: d.route_mode,
    taskType: d.task_type,
    complexity: d.complexity,
    source: d.classifier_source,
    classifierModel: d.classifier_model,
    classifierMs: d.classifier_ms,
    reason: d.classifier_reason,
    rule: d.rule,
    routed: d.routed_model,
    answeredBy: d.model_key,
    note: d.route_note,
  };
}

/** The routing decision as the three steps it actually went through. */
export function RouteSteps({ view, pending = false }: { view: RouteView; pending?: boolean }) {
  const { label } = useApp();
  return (
    <ol className="route-steps">
      <li>
        <span className="step-name">Classified</span>
        {view.mode === "forced" ? (
          <span className="muted">Skipped, model picked by hand</span>
        ) : (
          <span>
            <strong>{capitalize(view.taskType ?? "other")}</strong>, complexity <strong>{view.complexity}</strong> of 5{" "}
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
            <ModelTag modelKey={view.routed} />
          ) : (
            <>
              Rule <code>{view.rule ?? "none"}</code> picked <ModelTag modelKey={view.routed} />
            </>
          )}
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
    `${compact(d.input_tokens + d.cache_read_tokens + d.cache_write_tokens)} tokens in`,
    `${compact(d.output_tokens)} out`,
    duration(d.latency_ms),
  ];
  if (d.load_ms && d.load_ms > 500) parts.push(`${duration(d.load_ms)} loading the model`);
  if (d.provider === "claude_cli") parts.push(`${usd(d.cost_usd)} at API rates`);
  if (d.provider === "ollama" && d.avoided_usd > 0) parts.push(`≈ ${usd(d.avoided_usd)} kept off Claude`);
  return <p className="usage-line">{parts.join(", ")}</p>;
}
