import { useState } from "react";
import { api } from "../api";
import { ErrorNote, Light, ModelTag, Panel } from "../components/bits";
import { capitalize, int, price, when } from "../format";
import { useApp } from "../hooks";

export function Models({ onSynced }: { onSynced: () => void }) {
  const { config, status, label } = useApp();
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!config || !status) return <p className="muted">Loading…</p>;
  const r = config.routing;

  function ruleText(rule: (typeof r.rules)[number]): string {
    const parts: string[] = [];
    if (rule.task_types?.length) parts.push(rule.task_types.map(capitalize).join(" or "));
    if (rule.min_complexity && rule.max_complexity) parts.push(`complexity ${rule.min_complexity} to ${rule.max_complexity}`);
    else if (rule.min_complexity) parts.push(`complexity ${rule.min_complexity} or more`);
    else if (rule.max_complexity) parts.push(`complexity ${rule.max_complexity} or less`);
    return parts.length ? parts.join(", ") : "Anything else";
  }

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
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {status.models.map((m) => (
                <tr key={m.key}>
                  <td><ModelTag modelKey={m.key} /> <span className="muted key">{m.key}</span></td>
                  <td>{m.provider === "ollama" ? "Ollama (local)" : "Claude Code (your plan)"}</td>
                  <td><code>{m.model}</code></td>
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
        <Panel title="Routing rules" sub="Checked top to bottom. The first rule that matches picks the model.">
          <ol className="rules">
            {r.rules.map((rule) => (
              <li key={rule.name}>
                <code>{rule.name}</code>
                <span>{ruleText(rule)}</span>
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
                {r.classifier.think === false ? ", reasoning off" : ""}
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
          </Panel>
        </div>
      </div>
    </div>
  );
}
