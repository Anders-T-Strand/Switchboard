import { ThumbsDown, ThumbsUp } from "lucide-react";
import type { FollowUp, MemoryRef, Neighbor, RequestDetail, RoutePlan } from "../api";
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
  followUp: FollowUp | null;
  rule: string | null;
  routed: string | null;
  answeredBy: string | null;
  effort: string | null;
  effortSource: string | null;
  think: boolean | null;
  note: string | null;
  neighbors: Neighbor[];
  memory: MemoryRef[];
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
    followUp: c?.follow_up ?? null,
    rule: p.rule,
    routed: p.routed,
    answeredBy: p.chosen,
    effort: p.effort,
    effortSource: p.effort_source,
    think: p.think,
    note: p.notes.join("; ") || null,
    neighbors: p.neighbors ?? [],
    memory: [],
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
    followUp: d.follow_up ?? null,
    rule: d.rule,
    routed: d.routed_model,
    answeredBy: d.model_key,
    effort: d.effort,
    effortSource: d.effort_source,
    think: d.think === null || d.think === undefined ? null : Boolean(d.think),
    note: d.route_note,
    neighbors: d.neighbors ?? [],
    memory: d.memory ?? [],
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
            {(view.followUp === "correction" || view.followUp === "approval") && (
              <span className={`reason follow-up ${view.followUp === "correction" ? "bad" : "good"}`}>
                {view.followUp === "correction"
                  ? "Read as a correction of the previous answer"
                  : "Read as approval of the previous answer"}
                , which counts toward routing similar prompts
              </span>
            )}
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
      {view.neighbors.length > 0 && (
        <li>
          <span className="step-name">Similar</span>
          <span>
            <span className="muted">Closest past prompts, and how their answers went</span>
            <ul className="neighbors">
              {view.neighbors.slice(0, 3).map((n) => (
                <li key={n.request_id}>
                  <span className="nb-score" title="Cosine similarity">{n.score.toFixed(2)}</span>
                  <ModelTag modelKey={n.model_key} />
                  <Rated rating={n.rating} implicit={n.implicit_rating} />
                  <a className="nb-prompt" href={`#/console?c=${n.conversation_id}`} title={n.prompt}>{n.prompt}</a>
                </li>
              ))}
            </ul>
          </span>
        </li>
      )}
      <li>
        <span className="step-name">Effort</span>
        <span>
          <strong>{effortText(provider, view.effort, view.think)}</strong>
          {why && <span className="muted">, {why}</span>}
        </span>
      </li>
      {view.memory.length > 0 && (
        <li>
          <span className="step-name">Memory</span>
          <span>
            {view.memory.length === 1 ? "1 turn" : `${view.memory.length} turns`} from other chats went along:{" "}
            {view.memory.map((m, i) => (
              <span key={m.request_id}>
                {i > 0 && ", "}
                <a href={`#/console?c=${m.conversation_id}`}>{m.title ?? `chat ${m.conversation_id}`}</a>
              </span>
            ))}
          </span>
        </li>
      )}
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

function Rated({ rating, implicit }: { rating: number | null; implicit?: number | null }) {
  if (rating === 1) return <ThumbsUp size={13} className="rated good" aria-label="rated good" />;
  if (rating === -1) return <ThumbsDown size={13} className="rated bad" aria-label="rated bad" />;
  if (implicit === 1 || implicit === -1) {
    const good = implicit === 1;
    return (
      <span
        className={`rated implicit ${good ? "good" : "bad"}`}
        title={good ? "Not rated, but your next message approved it" : "Not rated, but your next message corrected it"}
      >
        {good ? <ThumbsUp size={12} aria-hidden /> : <ThumbsDown size={12} aria-hidden />}
        {good ? "approved" : "corrected"}
      </span>
    );
  }
  return <span className="muted small">not rated</span>;
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
