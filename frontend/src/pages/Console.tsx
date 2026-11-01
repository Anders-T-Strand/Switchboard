import { useState } from "react";
import { api, type RequestDetail, type RoutePlan } from "../api";
import { fromDetail, fromPlan, RouteSteps, UsageLine } from "../components/RouteSteps";
import { ErrorNote, Md, ModelTag, Panel } from "../components/bits";
import { when } from "../format";
import { useApi, useApp } from "../hooks";

export function Console() {
  const { modelKeys, label } = useApp();
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("auto");
  const [taskId, setTaskId] = useState<string>("");
  const [preview, setPreview] = useState<RoutePlan | null>(null);
  const [result, setResult] = useState<RequestDetail | null>(null);
  const [busy, setBusy] = useState<"send" | "preview" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const tasks = useApi(() => api.tasks(), []);
  const recent = useApi(() => api.requests({ limit: 8 }), []);

  const canSend = prompt.trim().length > 0 && busy === null;

  async function send() {
    if (!canSend) return;
    setBusy("send");
    setError(null);
    setResult(null);
    try {
      const r = await api.prompt({ prompt, model, task_id: taskId ? Number(taskId) : null });
      setResult(r);
      setPreview(null);
      recent.reload();
      tasks.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function doPreview() {
    if (!canSend) return;
    setBusy("preview");
    setError(null);
    try {
      setPreview(await api.preview({ prompt, model }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function openRecent(id: number) {
    setError(null);
    try {
      setResult(await api.request(id));
      setPreview(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const openTasks = (tasks.data ?? []).filter((t) => t.status !== "done");

  return (
    <div className="console">
      <div className="stack">
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <label htmlFor="prompt" className="sr-only">Prompt</label>
          <textarea
            id="prompt"
            value={prompt}
            onChange={(e) => {
              setPrompt(e.target.value);
              setPreview(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="Ask anything. The router picks the model."
            rows={7}
          />
          <div className="composer-bar">
            <label>
              <span>Model</span>
              <select value={model} onChange={(e) => { setModel(e.target.value); setPreview(null); }}>
                <option value="auto">Auto (router decides)</option>
                {modelKeys.map((k) => (
                  <option key={k} value={k}>{label(k)}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Task</span>
              <select value={taskId} onChange={(e) => setTaskId(e.target.value)}>
                <option value="">None</option>
                {openTasks.map((t) => (
                  <option key={t.id} value={t.id}>{t.title}</option>
                ))}
              </select>
            </label>
            <div className="composer-actions">
              <button type="button" className="ghost" onClick={doPreview} disabled={!canSend || model !== "auto"}>
                {busy === "preview" ? "Checking…" : "Preview route"}
              </button>
              <button type="submit" className="primary" disabled={!canSend}>
                {busy === "send" ? "Waiting for answer…" : "Send"}
              </button>
            </div>
          </div>
          <p className="hint">Ctrl+Enter sends. Preview runs only the classifier, so nothing is answered or logged.</p>
        </form>

        {error && <ErrorNote>{error}</ErrorNote>}

        {preview && !result && (
          <Panel title="Route preview">
            <RouteSteps view={fromPlan(preview)} pending />
          </Panel>
        )}

        {busy === "send" && (
          <Panel>
            <p className="muted working">Routing and waiting for the answer. Claude calls usually take a few seconds longer than local ones.</p>
          </Panel>
        )}

        {result && (
          <Panel
            title={result.status === "ok" ? `Answer from ${label(result.model_key)}` : "No answer"}
            sub={when(result.created_at)}
          >
            <RouteSteps view={fromDetail(result)} />
            {result.status === "ok" ? (
              <>
                <Md>{result.response ?? ""}</Md>
                <UsageLine d={result} />
              </>
            ) : (
              <ErrorNote>{result.error}</ErrorNote>
            )}
          </Panel>
        )}
      </div>

      <aside className="recent">
        <h2>Recent</h2>
        {recent.data?.items.length === 0 && <p className="muted">Your prompts will show up here.</p>}
        <ul>
          {(recent.data?.items ?? []).map((r) => (
            <li key={r.id}>
              <button className="recent-item" onClick={() => openRecent(r.id)}>
                <span className="recent-prompt">{r.prompt_preview}</span>
                <span className="recent-meta">
                  {r.status === "ok" ? <ModelTag modelKey={r.model_key} /> : <span className="error-text">Failed</span>}
                  <span className="muted">{when(r.created_at)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
