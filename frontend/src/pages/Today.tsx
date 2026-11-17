import { useEffect, useState, type ReactNode } from "react";
import { api, type Brief, type BriefItem, type BriefItemStatus, type Task } from "../api";
import { ErrorNote } from "../components/bits";
import { dueLabel, when } from "../format";
import { useApi } from "../hooks";

const IDLE_MS = 60_000;
const BUILDING_MS = 2_000;

type Act = (fn: () => Promise<unknown>) => void;

export function Today() {
  const [pollMs, setPollMs] = useState(IDLE_MS);
  const brief = useApi(() => api.brief(), [], pollMs);
  const tasks = useApi(() => api.tasks(), []);
  const [error, setError] = useState<string | null>(null);

  const data = brief.data;
  const building = data?.building ?? false;
  // Ask often while a build is going, so the page fills in as soon as it is done.
  useEffect(() => setPollMs(building ? BUILDING_MS : IDLE_MS), [building]);

  const act: Act = async (fn) => {
    setError(null);
    try {
      await fn();
      brief.reload();
      tasks.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!data) return brief.error ? <ErrorNote>{brief.error}</ErrorNote> : <p className="muted">Loading…</p>;

  const open = data.items.filter((i) => i.status === "open");
  const closed = data.items.filter((i) => i.status !== "open");
  const coursework = open.filter((i) => i.source === "coursework");
  const rest = open.filter((i) => i.source !== "coursework");
  const yours = rest.filter((i) => i.owner === "you");
  const draftable = rest.filter((i) => i.owner === "model");
  const board = (tasks.data ?? []).filter((t) => t.status !== "done");
  const builtToday = data.run !== null && data.run.day === data.day && data.run.status !== "running";
  const failed = (data.run?.sources ?? []).filter((s) => s.ok === false);

  return (
    <div className="stack">
      <div className="today-bar">
        <p className="today-status">{statusLine(data, open.length)}</p>
        <button className="primary" disabled={building || !data.enabled} onClick={() => act(() => api.buildBrief())}>
          {building ? "Building…" : builtToday ? "Refresh" : "Build now"}
        </button>
      </div>

      {!data.enabled && (
        <p className="hint">
          The morning brief is off. Set <code>brief.enabled</code> to true in <code>config.local.yaml</code> and restart
          Switchboard.
        </p>
      )}
      {data.enabled &&
        data.sources
          .filter((s) => !s.connected && s.setup)
          .map((s) => (
            <p className="hint" key={s.name}>
              {s.label} isn't connected. Run <code>{s.setup}</code> in the <code>backend</code> folder.
            </p>
          ))}
      {(error || brief.error || tasks.error) && <ErrorNote>{error ?? brief.error ?? tasks.error}</ErrorNote>}
      {failed.map((s) => (
        <ErrorNote key={s.name}>
          {s.label}: {s.error}
        </ErrorNote>
      ))}

      <Section title="Needs you" count={yours.length}>
        {yours.map((i) => (
          <ItemCard key={i.id} item={i} onAct={act} />
        ))}
      </Section>
      <Section title="A model can draft" count={draftable.length}>
        {draftable.map((i) => (
          <ItemCard key={i.id} item={i} onAct={act} />
        ))}
      </Section>
      <Section title="Coursework" count={coursework.length}>
        {coursework.map((i) => (
          <ItemCard key={i.id} item={i} onAct={act} />
        ))}
      </Section>
      <Section title="On your board" count={board.length} aside={<a href="#/tasks">Open the board</a>}>
        {board.map((t) => (
          <BoardCard key={t.id} task={t} />
        ))}
      </Section>

      {data.enabled && builtToday && open.length === 0 && board.length === 0 && (
        <p className="muted">Nothing needs you right now.</p>
      )}

      {closed.length > 0 && (
        <section className="today-section" aria-label="Closed today">
          <h2>
            Closed today <span className="muted">{closed.length}</span>
          </h2>
          <ul className="today-closed">
            {closed.map((i) => (
              <li key={i.id}>
                <span className="today-closed-title">{i.title}</span>
                <span className="muted">{i.status === "done" ? "Done" : "Dismissed"}</span>
                <button className="ghost small" onClick={() => act(() => api.setBriefItem(i.id, "open"))}>
                  Reopen
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function statusLine(data: Brief, open: number): string {
  const run = data.run;
  if (data.building) return "Building the brief…";
  if (!run) return "No brief has been built yet.";
  if (run.day !== data.day) return `Last built ${when(run.started_at)}. Today's hasn't been built.`;
  const found = `${open} open`;
  if (run.status === "error") return `Today's build failed, ${when(run.finished_at ?? run.started_at)}.`;
  return `Built ${when(run.finished_at ?? run.started_at)}, ${found}.`;
}

/** A titled group of cards. Renders nothing when it is empty, so the page only shows what there is. */
function Section({ title, count, aside, children }: { title: string; count: number; aside?: ReactNode; children: ReactNode }) {
  if (count === 0) return null;
  return (
    <section className="today-section" aria-label={title}>
      <h2>
        {title} <span className="muted">{count}</span>
        {aside && <span className="today-aside">{aside}</span>}
      </h2>
      <div className="today-grid">{children}</div>
    </section>
  );
}

function ItemCard({ item, onAct }: { item: BriefItem; onAct: Act }) {
  const due = item.due_at ? dueLabel(item.due_at) : null;
  const set = (status: BriefItemStatus) => onAct(() => api.setBriefItem(item.id, status));
  // The server only stores https links. Checked again here, because a link is the one thing on
  // this card that gets followed.
  const link = item.link?.startsWith("https://") ? item.link : null;
  return (
    <article className="task-card brief-card">
      <h3>{item.title}</h3>
      {(item.context || item.received_at) && (
        <p className="brief-from">
          <span>{item.context}</span>
          {item.received_at && <span className="muted nowrap">{when(item.received_at)}</span>}
        </p>
      )}
      {item.summary && <p className="task-notes">{item.summary}</p>}
      {item.reason && <p className="hint brief-reason">{item.reason}</p>}
      {due && <p className={`task-meta due ${due.tone}`}>{due.text}</p>}
      <div className="task-actions">
        <button className="ghost small" onClick={() => set("done")}>
          Done
        </button>
        <button className="ghost small" onClick={() => set("dismissed")}>
          Dismiss
        </button>
        {item.task_id === null ? (
          <button className="ghost small" onClick={() => onAct(() => api.briefItemTask(item.id))}>
            Make a task
          </button>
        ) : (
          <a className="brief-link" href="#/tasks" title={item.task_title ?? undefined}>
            On the board
          </a>
        )}
        {link && (
          <a className="brief-link" href={link} target="_blank" rel="noreferrer">
            Open
          </a>
        )}
      </div>
    </article>
  );
}

function BoardCard({ task }: { task: Task }) {
  return (
    <article className="task-card">
      <h3>{task.title}</h3>
      {task.notes && <p className="task-notes">{task.notes}</p>}
      <p className="task-meta muted">{task.status === "doing" ? "In progress" : "To do"}</p>
    </article>
  );
}
