import { useEffect, useState } from "react";
import { api, type RequestDetail } from "../api";
import { fromDetail, RouteSteps, UsageLine } from "../components/RouteSteps";
import { ErrorNote, Md, ModelTag } from "../components/bits";
import { capitalize, compact, duration, usd, when } from "../format";
import { hashParam, useApi, useApp } from "../hooks";

const PAGE = 50;

export function Requests() {
  const { modelKeys, label } = useApp();
  const [model, setModel] = useState("");
  const [status, setStatus] = useState("");
  const [taskId, setTaskId] = useState<number | null>(() => {
    const t = hashParam("task");
    return t ? Number(t) : null;
  });
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  const list = useApi(
    () => api.requests({ limit: PAGE, offset: page * PAGE, model, status, task_id: taskId }),
    [model, status, taskId, page],
    20_000,
  );

  useEffect(() => setPage(0), [model, status, taskId]);

  const total = list.data?.total ?? 0;
  const items = list.data?.items ?? [];
  const taskTitle = items.find((i) => i.task_id === taskId)?.task_title;

  return (
    <div className="stack">
      <div className="filters">
        <label>
          <span>Model</span>
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">All models</option>
            {modelKeys.map((k) => (
              <option key={k} value={k}>{label(k)}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Result</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All</option>
            <option value="ok">Answered</option>
            <option value="error">Failed</option>
          </select>
        </label>
        {taskId !== null && (
          <button className="chip" onClick={() => setTaskId(null)} aria-label="Clear task filter">
            Task: {taskTitle ?? `#${taskId}`} <span aria-hidden>×</span>
          </button>
        )}
        <span className="muted count">{total === 1 ? "1 request" : `${compact(total)} requests`}</span>
      </div>

      {list.error && <ErrorNote>Couldn't load requests: {list.error}</ErrorNote>}

      {list.data && items.length === 0 ? (
        <p className="muted">No requests match. Send one from the Console.</p>
      ) : (
        <div className="table-scroll">
          <table className="data rows-clickable">
            <thead>
              <tr>
                <th>When</th>
                <th>Prompt</th>
                <th>Task type</th>
                <th className="num">Cx</th>
                <th>Answered by</th>
                <th className="num">Tokens</th>
                <th className="num">Time</th>
                <th className="num">API rates</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id} onClick={() => setOpenId(r.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpenId(r.id)}>
                  <td className="nowrap">{when(r.created_at)}</td>
                  <td className="prompt-cell">{r.prompt_preview}</td>
                  <td>{r.task_type ? capitalize(r.task_type) : <span className="muted">forced</span>}</td>
                  <td className="num">{r.complexity ?? "–"}</td>
                  <td className="nowrap">
                    {r.status === "ok" ? <ModelTag modelKey={r.model_key} /> : <span className="error-text">Failed</span>}
                    {r.fell_back ? <span className="flag">fallback</span> : null}
                  </td>
                  <td className="num">{compact(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens + r.output_tokens)}</td>
                  <td className="num">{duration(r.latency_ms)}</td>
                  <td className="num">{r.provider === "claude_cli" ? usd(r.cost_usd) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {total > PAGE && (
        <div className="pager">
          <button className="ghost small" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Newer</button>
          <span className="muted">
            {page * PAGE + 1}–{Math.min(total, (page + 1) * PAGE)} of {total}
          </span>
          <button className="ghost small" disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>Older</button>
        </div>
      )}

      {openId !== null && <RequestDrawer id={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}

function RequestDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { label } = useApp();
  const [d, setD] = useState<RequestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setD(null);
    api.request(id).then(setD).catch((e: Error) => setError(e.message));
  }, [id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`Request ${id}`} onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h2>{d ? (d.status === "ok" ? `Answer from ${label(d.model_key)}` : "Failed request") : "Loading…"}</h2>
          <button className="ghost small" onClick={onClose} autoFocus>Close</button>
        </header>
        {error && <ErrorNote>{error}</ErrorNote>}
        {d && (
          <div className="stack">
            <p className="muted">
              {new Date(d.created_at).toLocaleString()}
              {d.task_title ? ` for task "${d.task_title}"` : ""}
              {d.provider_model ? `, model id ${d.provider_model}` : ""}
            </p>
            <RouteSteps view={fromDetail(d)} />
            <section>
              <h3>Prompt</h3>
              <pre className="prompt-text">{d.prompt}</pre>
            </section>
            <section>
              <h3>Response</h3>
              {d.status === "ok" ? <Md>{d.response ?? ""}</Md> : <ErrorNote>{d.error}</ErrorNote>}
            </section>
            {d.status === "ok" && <UsageLine d={d} />}
          </div>
        )}
      </aside>
    </div>
  );
}
