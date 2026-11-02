import { api, type ModelStats } from "../api";
import { PatchBay } from "../components/PatchBay";
import { StackedColumns } from "../components/StackedColumns";
import { ErrorNote, ModelTag, Panel, Readouts } from "../components/bits";
import { compact, duration, effortLabel, int, pct, usd } from "../format";
import { useApi, useApp } from "../hooks";

const FAMILY_LABEL: Record<string, string> = { opus: "Opus", sonnet: "Sonnet", haiku: "Haiku", fable: "Fable" };

/** "Low 4, High 2" for Claude rows, "Thinking on 3 of 8" for local rows. */
function effortMix(m: ModelStats, order: string[]): string {
  if (m.provider === "ollama") {
    return m.think_known ? `Thinking on ${int(m.thinking)} of ${int(m.think_known)}` : "–";
  }
  const parts = [...order, "default"]
    .filter((e) => m.efforts?.[e])
    .map((e) => `${e === "default" ? "Default" : effortLabel(e)} ${int(m.efforts[e])}`);
  return parts.length ? parts.join(", ") : "–";
}

export function Overview({ days }: { days: number }) {
  const { colors, label, modelKeys, status, config } = useApp();
  const summary = useApi(() => api.summary(days), [days], 30_000);
  const series = useApi(() => api.timeseries(days), [days], 30_000);

  if (summary.error) return <ErrorNote>Couldn't load stats: {summary.error}</ErrorNote>;
  const s = summary.data;
  const t = series.data;
  if (!s) return <p className="muted">Loading…</p>;

  const loadedKeys = (status?.models ?? []).filter((m) => m.loaded).map((m) => m.key);
  // Stack and legend order follow config order, so a model sits in the same place in every chart.
  const byConfig = (order: string[]) => (a: string, b: string) =>
    (order.indexOf(a) + 1 || 999) - (order.indexOf(b) + 1 || 999);
  const routerKeys = t ? [...t.router_keys].sort(byConfig(modelKeys)) : [];
  const ccKeys = t ? [...t.cc_keys].sort(byConfig(Object.keys(config?.pricing.models ?? {}))) : [];
  const cc = s.claude_code.interactive;
  const ccRouter = s.claude_code.router;
  const refModel = config?.pricing.reference_model;

  return (
    <div className="stack">
      <PatchBay byTaskType={s.by_task_type} modelKeys={modelKeys} loadedKeys={loadedKeys} />

      <Readouts
        items={[
          {
            label: "Prompts",
            value: int(s.requests.total),
            detail:
              s.requests.errors || s.requests.fallbacks
                ? `${int(s.requests.errors)} failed, ${int(s.requests.fallbacks)} fell back`
                : "none failed",
          },
          {
            label: "Answered locally",
            value: pct(s.requests.local_share),
            detail: `${int(s.requests.local)} of ${int(s.requests.ok)} answered`,
          },
          {
            label: "Local tokens",
            value: compact(s.tokens.local_in + s.tokens.local_out),
            detail: `${compact(s.tokens.local_in)} in, ${compact(s.tokens.local_out)} out`,
          },
          {
            label: "Claude tokens via router",
            value: compact(s.tokens.claude_in + s.tokens.claude_out),
            detail: `${compact(s.tokens.claude_in)} in, ${compact(s.tokens.claude_out)} out`,
          },
          {
            label: "Claude at API rates",
            value: usd(s.cost.claude_api_equivalent_usd),
            detail: "router calls, covered by your plan",
          },
          {
            label: "Kept off Claude",
            value: `≈ ${usd(s.cost.avoided_usd)}`,
            detail: refModel ? `local answers priced as ${FAMILY_LABEL[refModel] ?? refModel}` : "local answers",
          },
        ]}
      />

      <div className="grid-2">
        <Panel title="Router tokens per day" sub="Answered prompts, input plus output, by model">
          {t ? (
            <StackedColumns
              rows={t.router_tokens}
              keys={routerKeys}
              colorFor={(k) => colors[k] ?? "#7d7266"}
              labelFor={(k) => label(k)}
              format={(v) => compact(v)}
              emptyText="No answered prompts in this period."
            />
          ) : (
            <p className="muted">Loading…</p>
          )}
        </Panel>

        <Panel
          title="Claude Code usage at API rates"
          sub="Your own Claude Code sessions per day, read from local session logs"
        >
          {t ? (
            <StackedColumns
              rows={t.cc_cost}
              keys={ccKeys}
              colorFor={(k) => colors[`cc:${k}`] ?? "#7d7266"}
              labelFor={(k) => FAMILY_LABEL[k] ?? k}
              format={(v) => usd(v)}
              formatAxis={(v) => (v >= 10 ? `$${Math.round(v)}` : `$${v.toFixed(v < 1 ? 2 : 1)}`)}
              emptyText={
                status?.sync.ok === false
                  ? status.sync.reason ?? "Claude Code logs not found."
                  : "No Claude Code sessions in this period."
              }
            />
          ) : (
            <p className="muted">Loading…</p>
          )}
          <p className="footnote">
            {int(cc.sessions)} sessions, {compact(cc.output_tokens)} output tokens, {usd(cc.cost_usd)} at API rates.
            {ccRouter.messages > 0 && ` Router calls through claude -p add ${usd(ccRouter.cost_usd)} more.`} Chats on
            claude.ai don't write local logs, so they aren't counted here.
          </p>
        </Panel>
      </div>

        <Panel title="By model">
          {s.by_model.length === 0 ? (
            <p className="muted">Nothing yet in this period.</p>
          ) : (
            <div className="table-scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th className="num">Prompts</th>
                    <th>Effort</th>
                    <th>Feedback</th>
                    <th className="num">Tokens in</th>
                    <th className="num">Tokens out</th>
                    <th className="num">Median time</th>
                    <th className="num">At API rates</th>
                  </tr>
                </thead>
                <tbody>
                  {s.by_model.map((m) => (
                    <tr key={m.model_key}>
                      <td>{m.model_key === "(failed)" ? <span className="muted">No model answered</span> : <ModelTag modelKey={m.model_key} />}</td>
                      <td className="num">
                        {int(m.requests)}
                        {m.errors > 0 && <span className="muted block small">{int(m.errors)} failed</span>}
                      </td>
                      <td className="effort-mix">{effortMix(m, config?.efforts ?? [])}</td>
                      <td className="effort-mix">
                        {m.rated_good || m.rated_bad
                          ? `Rated ${int(m.rated_good)} good, ${int(m.rated_bad)} bad`
                          : !m.follow_up_good && !m.follow_up_bad && "–"}
                        {(m.follow_up_good > 0 || m.follow_up_bad > 0) && (
                          <span className="muted block small">
                            Follow-ups {int(m.follow_up_good)} approved, {int(m.follow_up_bad)} corrected
                          </span>
                        )}
                      </td>
                      <td className="num">{compact(m.input_tokens)}</td>
                      <td className="num">{compact(m.output_tokens)}</td>
                      <td className="num">{duration(m.median_latency_ms)}</td>
                      <td className="num">{m.provider === "claude_cli" ? usd(m.cost_usd) : "local"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

      <div className="grid-2">
        <Panel title="Complexity mix" sub="How capable a model each prompt needed, 1 trivial to 5 expert. Picks the model.">
          <StackedColumns
            rows={s.complexity.map((n, i) => ({ date: String(i + 1), count: n }))}
            keys={["count"]}
            colorFor={() => "var(--ink-2)"}
            labelFor={() => "Prompts"}
            format={(v) => int(v)}
            xLabel={(r) => String(r.date)}
            emptyText="No auto-routed prompts yet."
            height={180}
          />
        </Panel>

        <Panel title="Reasoning mix" sub="How much thinking each prompt needed, 1 none to 5 extensive. Picks the effort.">
          <StackedColumns
            rows={(s.reasoning ?? []).map((n, i) => ({ date: String(i + 1), count: n }))}
            keys={["count"]}
            colorFor={() => "var(--ink-2)"}
            labelFor={() => "Prompts"}
            format={(v) => int(v)}
            xLabel={(r) => String(r.date)}
            emptyText="No rated prompts yet."
            height={180}
          />
        </Panel>
      </div>
    </div>
  );
}
