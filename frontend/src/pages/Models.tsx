import { useState } from "react";
import { api } from "../api";
import { ErrorNote, Light, ModelTag, Panel } from "../components/bits";
import { capitalize, effortLabel, int, price, when } from "../format";
import { useApp } from "../hooks";

export function Models({ onSynced }: { onSynced: () => void }) {
  const { config, status, label } = useApp();
  const [syncing, setSyncing] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!config || !status) return <p className="muted">Loading…</p>;
  const r = config.routing;

  function range(name: string, min: number | null, max: number | null): string | null {
    if (min && max) return `${name} ${min} to ${max}`;
    if (min) return `${name} ${min} or more`;
    if (max) return `${name} ${max} or less`;
    return null;
  }

  function ruleText(rule: (typeof r.rules)[number]): string {
    const parts: string[] = [];
    if (rule.task_types?.length) parts.push(rule.task_types.map(capitalize).join(" or "));
    const cx = range("complexity", rule.min_complexity, rule.max_complexity);
    const rs = range("reasoning", rule.min_reasoning, rule.max_reasoning);
    if (cx) parts.push(cx);
    if (rs) parts.push(rs);
    return parts.length ? parts.join(", ") : "Anything else";
  }

  function ruleEffort(rule: (typeof r.rules)[number]): string | null {
    if (rule.effort) return `Effort pinned at ${effortLabel(rule.effort).toLowerCase()}`;
    if (rule.min_effort && rule.max_effort) {
      return `Effort kept between ${effortLabel(rule.min_effort).toLowerCase()} and ${effortLabel(rule.max_effort).toLowerCase()}`;
    }
    if (rule.max_effort) return `Effort capped at ${effortLabel(rule.max_effort).toLowerCase()}`;
    if (rule.min_effort) return `Effort at least ${effortLabel(rule.min_effort).toLowerCase()}`;
    return null;
  }

  function modelEffort(key: string): string {
    const m = config!.models[key];
    if (m.provider === "ollama") {
      if (m.think === true) return "Thinking always on";
      if (m.think === false) return "Thinking always off";
      return "Thinking off at low effort, on above";
    }
    return m.max_effort ? `Up to ${effortLabel(m.max_effort).toLowerCase()}` : "Any level";
  }

  const byReasoning = [1, 2, 3, 4, 5].map((n) => ({ n, effort: r.effort.by_reasoning[String(n)] ?? null }));

  return (
    <div className="stack">
      <Panel title="Models" sub="Defined in config.yaml. Restart the server after editing it.">
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th>Model</th>
                <th>Runs on</th>
                <th>Model id</th>
                <th>Effort</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {status.models.map((m) => (
                <tr key={m.key}>
                  <td><ModelTag modelKey={m.key} /> <span className="muted key">{m.key}</span></td>
                  <td>{m.provider === "ollama" ? "Ollama (local)" : "Claude Code (your plan)"}</td>
                  <td><code>{m.model}</code></td>
                  <td className="muted">{modelEffort(m.key)}</td>
                  <td>
                    {m.provider === "ollama" ? (
                      m.loaded ? (
                        <Light on>In VRAM</Light>
                      ) : m.available ? (
                        <Light on={false}>Installed, not loaded</Light>
                      ) : (
                        <Light on warn>{status.ollama.reachable ? `Not pulled. Run ollama pull ${m.model}` : "Ollama offline"}</Light>
                      )
                    ) : m.available ? (
                      <Light on>Ready</Light>
                    ) : (
                      <Light on warn>CLI not found</Light>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="grid-2">
        <div className="stack">
        <Panel title="Routing rules" sub="Checked top to bottom. The first rule that matches picks the model.">
          <ol className="rules">
            {r.rules.map((rule) => (
              <li key={rule.name}>
                <code>{rule.name}</code>
                <span>
                  {ruleText(rule)}
                  {ruleEffort(rule) && <span className="muted block small">{ruleEffort(rule)}</span>}
                </span>
                <ModelTag modelKey={rule.model} />
              </li>
            ))}
          </ol>
          <dl className="facts">
            <div>
              <dt>Classifier</dt>
              <dd>
                {label(r.classifier.model)}
                {r.classifier.use_loaded_model ? ", or whichever local model is already loaded" : ""}
                {r.classifier.think === false ? ", thinking off" : ""}
              </dd>
            </div>
            <div>
              <dt>Avoid model swaps</dt>
              <dd>{r.prefer_loaded_local ? "On. A loaded local model answers instead of loading another one." : "Off"}</dd>
            </div>
            <div>
              <dt>If a model fails</dt>
              <dd>{r.fallback_order.length ? `Try ${r.fallback_order.map(label).join(", then ")}` : "Report the error"}</dd>
            </div>
          </dl>
        </Panel>

        <Panel
          title="Effort by reasoning"
          sub="The classifier rates reasoning separately from complexity. Complexity picks the model, reasoning picks how hard it thinks."
        >
          <ol className="effort-ladder">
            {byReasoning.map(({ n, effort }) => (
              <li key={n}>
                <span className="ladder-n">{n}</span>
                <span className="ladder-bar" aria-hidden>
                  <span style={{ width: `${effort ? ((config.efforts.indexOf(effort) + 1) / config.efforts.length) * 100 : 0}%` }} />
                </span>
                <span>{effort ? effortLabel(effort) : <span className="muted">Your /effort setting</span>}</span>
                <span className="muted small">{effort === "low" ? "local: thinking off" : effort ? "local: thinking on" : ""}</span>
              </li>
            ))}
          </ol>
          <p className="footnote">
            Rules can pin or cap the effort, and a model can have its own cap, all in config.yaml. Picking an effort in the
            Console overrides all of it for that prompt.
          </p>
        </Panel>
        </div>

        <div className="stack">
          <Panel title="Connections">
            <dl className="facts">
              <div>
                <dt>Ollama</dt>
                <dd>
                  <Light on={status.ollama.reachable}>
                    {status.ollama.reachable ? `Version ${status.ollama.version} at ${status.ollama.base_url}` : `Not reachable at ${status.ollama.base_url}`}
                  </Light>
                </dd>
              </div>
              <div>
                <dt>Claude Code</dt>
                <dd>
                  <Light on={status.claude.found}>
                    {status.claude.found ? `${status.claude.version ?? "found"}` : "CLI not found"}
                  </Light>
                  {status.claude.found ? (
                    <span className="muted block">
                      <code>{status.claude.path}</code>
                    </span>
                  ) : (
                    <span className="muted block">
                      Install the Claude Code CLI, or set <code>claude.command</code> in config.yaml to the full path of
                      claude.exe, then restart.
                    </span>
                  )}
                  <span className="muted block">
                    {status.claude.use_subscription
                      ? "API key removed from its environment, so calls use your Pro login"
                      : "Uses whatever credentials claude finds, including an API key"}
                  </span>
                </dd>
              </div>
            </dl>
          </Panel>

          {status.index?.enabled && (
            <Panel
              title="Vector index"
              sub="Every answered turn, embedded and stored in Qdrant"
              actions={
                <button
                  className="ghost small"
                  disabled={rebuilding}
                  onClick={async () => {
                    if (!window.confirm("Drop the Qdrant collection and index every turn again?")) return;
                    setRebuilding(true);
                    setError(null);
                    try {
                      await api.rebuildIndex();
                      onSynced();
                    } catch (e) {
                      setError((e as Error).message);
                    } finally {
                      setRebuilding(false);
                    }
                  }}
                >
                  {rebuilding ? "Starting…" : "Re-index everything"}
                </button>
              }
            >
              <dl className="facts">
                <div>
                  <dt>Qdrant</dt>
                  <dd>
                    <Light on={status.index.ok === true || status.index.ok === false} warn={status.index.ok === false}>
                      {status.index.ok === false ? status.index.reason : status.index.where}
                    </Light>
                    <span className="muted block">Collection {status.index.collection}</span>
                  </dd>
                </div>
                <div>
                  <dt>Embeddings</dt>
                  <dd>
                    <code>{status.index.embed_model}</code>
                    {status.index.dim ? <span className="muted">, {status.index.dim} dimensions</span> : null}
                  </dd>
                </div>
                <div>
                  <dt>Indexed</dt>
                  <dd>
                    {int(status.index.indexed ?? 0)} of {int(status.index.turns ?? 0)} answered turns
                    {status.index.pending ? <span className="muted">, {int(status.index.pending)} waiting</span> : null}
                  </dd>
                </div>
                <div>
                  <dt>Learning</dt>
                  <dd>
                    {r.learn.enabled && r.learn.ladder.length ? (
                      <>
                        Moves along {r.learn.ladder.map(label).join(", then ")} once {r.learn.min_votes} similar
                        prompts (similarity {r.learn.min_similarity} or more) were rated
                      </>
                    ) : (
                      "Off"
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Memory</dt>
                  <dd>
                    Up to {config.memory.max_items} related turns from other chats, similarity {config.memory.min_score}{" "}
                    or more, when a chat has Memory turned on
                  </dd>
                </div>
              </dl>
            </Panel>
          )}

          <Panel
            title="Claude Code logs"
            actions={
              <button
                className="ghost small"
                disabled={syncing}
                onClick={async () => {
                  setSyncing(true);
                  setError(null);
                  try {
                    await api.sync();
                    onSynced();
                  } catch (e) {
                    setError((e as Error).message);
                  } finally {
                    setSyncing(false);
                  }
                }}
              >
                {syncing ? "Reading…" : "Read logs now"}
              </button>
            }
          >
            {error && <ErrorNote>{error}</ErrorNote>}
            <dl className="facts">
              <div>
                <dt>Folder</dt>
                <dd><code>{status.sync.root ?? `${status.claude.logs_root}/projects`}</code></dd>
              </div>
              <div>
                <dt>Last read</dt>
                <dd>
                  {status.sync.ok === false ? (
                    <Light on warn>{status.sync.reason}</Light>
                  ) : (
                    `${when(status.sync.at)}, ${int(status.sync.files)} session files`
                  )}
                </dd>
              </div>
            </dl>
          </Panel>

          <Panel title="Prices used for estimates" sub="USD per million tokens, Claude API list prices">
            <div className="table-scroll">
            <table className="data compact">
              <thead>
                <tr>
                  <th>Family</th>
                  <th className="num">Input</th>
                  <th className="num">Output</th>
                  <th className="num">Cache read</th>
                  <th className="num">Cache write</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(config.pricing.models).map(([name, p]) => (
                  <tr key={name}>
                    <td>
                      {capitalize(name)}
                      {config.pricing.reference_model === name && <span className="flag">reference</span>}
                    </td>
                    <td className="num">{price(p.input)}</td>
                    <td className="num">{price(p.output)}</td>
                    <td className="num">{price(p.cache_read)}</td>
                    <td className="num">{price(p.cache_write)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
