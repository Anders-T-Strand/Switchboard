import { useState } from "react";
import { api, type Task, type TaskStatus } from "../api";
import { ErrorNote } from "../components/bits";
import { compact, int, usd, when } from "../format";
import { useApi } from "../hooks";

const COLUMNS: { status: TaskStatus; title: string; empty: string }[] = [
  { status: "todo", title: "To do", empty: "Nothing queued." },
  { status: "doing", title: "In progress", empty: "Nothing in progress." },
  { status: "done", title: "Done", empty: "Nothing finished yet." },
];
const ORDER: TaskStatus[] = ["todo", "doing", "done"];

export function Tasks() {
  const tasks = useApi(() => api.tasks(), []);
  const [title, setTitle] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      tasks.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="stack">
      <form
        className="task-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!title.trim()) return;
          act(async () => {
            await api.createTask({ title, notes });
            setTitle("");
            setNotes("");
          });
        }}
      >
        <label className="grow">
          <span>New task</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Write the project README" maxLength={300} />
        </label>
        <label className="grow">
          <span>Notes</span>
          <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional" />
        </label>
        <button type="submit" className="primary" disabled={!title.trim()}>Add task</button>
      </form>
      <p className="hint">Attach prompts to a task from the Console to see how many tokens each piece of work takes.</p>

      {(error || tasks.error) && <ErrorNote>{error ?? tasks.error}</ErrorNote>}

      <div className="board">
        {COLUMNS.map((col) => {
          const items = (tasks.data ?? []).filter((t) => t.status === col.status);
          return (
            <section className="board-col" key={col.status} aria-label={col.title}>
              <h2>
                {col.title} <span className="muted">{items.length}</span>
              </h2>
              {items.length === 0 && <p className="muted">{col.empty}</p>}
              {items.map((t) => (
                <TaskCard key={t.id} task={t} onAct={act} />
              ))}
            </section>
          );
        })}
      </div>
    </div>
  );
}

function TaskCard({ task, onAct }: { task: Task; onAct: (fn: () => Promise<unknown>) => void }) {
  const idx = ORDER.indexOf(task.status);
  const move = (dir: -1 | 1) => onAct(() => api.updateTask(task.id, { status: ORDER[idx + dir] }));
  return (
    <article className="task-card">
      <h3>{task.title}</h3>
      {task.notes && <p className="task-notes">{task.notes}</p>}
      <p className="task-meta">
        {task.requests > 0 ? (
          <a href={`#/requests?task=${task.id}`}>
            {int(task.requests)} {task.requests === 1 ? "prompt" : "prompts"}, {compact(task.tokens)} tokens
            {task.cost_usd > 0 ? `, ${usd(task.cost_usd)} at API rates` : ""}
          </a>
        ) : (
          <span className="muted">No prompts yet</span>
        )}
      </p>
      <p className="task-meta muted">Updated {when(task.updated_at)}</p>
      <div className="task-actions">
        {idx > 0 && (
          <button className="ghost small" onClick={() => move(-1)}>
            Move to {COLUMNS[idx - 1].title.toLowerCase()}
          </button>
        )}
        {idx < ORDER.length - 1 && (
          <button className="ghost small" onClick={() => move(1)}>
            Move to {COLUMNS[idx + 1].title.toLowerCase()}
          </button>
        )}
        <button
          className="ghost small danger"
          onClick={() => {
            if (window.confirm(`Delete "${task.title}"? Its prompts stay in the request log.`)) {
              onAct(() => api.deleteTask(task.id));
            }
          }}
        >
          Delete
        </button>
      </div>
    </article>
  );
}
