import { Check, ChevronDown, Copy, Pencil, Plus, RotateCcw, Search, ThumbsDown, ThumbsUp, Trash2 } from "lucide-react";
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Conversation, type RequestDetail, type RoutePlan } from "../api";
import { effortText, fromDetail, fromPlan, RouteSteps, UsageLine } from "../components/RouteSteps";
import { ErrorNote, Md, ModelTag } from "../components/bits";
import { effortLabel, when } from "../format";
import { hashParam, useApi, useApp } from "../hooks";

function chatIdFromHash(): number | null {
  const c = hashParam("c");
  return c ? Number(c) || null : null;
}

export function Console() {
  const { modelKeys, label, config, status } = useApp();
  const [chatId, setChatId] = useState<number | null>(chatIdFromHash);
  const chatIdRef = useRef(chatId);
  chatIdRef.current = chatId;
  const [conv, setConv] = useState<Conversation | null>(null);
  const [loadingConv, setLoadingConv] = useState(false);
  const [pending, setPending] = useState<{ prompt: string; since: number } | null>(null);
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("auto");
  const [effort, setEffort] = useState("auto");
  const [newChatMemory, setNewChatMemory] = useState(false);
  const [taskId, setTaskId] = useState("");
  const [preview, setPreview] = useState<RoutePlan | null>(null);
  const [busy, setBusy] = useState<"send" | "preview" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const tasks = useApi(() => api.tasks(), []);
  const list = useApi(() => api.conversations(search), [search]);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const onHash = () => setChatId(chatIdFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => setSearch(query.trim()), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  // Load the selected chat. A chat this page just created is already in state.
  useEffect(() => {
    setPreview(null);
    setError(null);
    if (chatId === null) {
      setConv(null);
      return;
    }
    if (conv?.id === chatId) return;
    let cancelled = false;
    setLoadingConv(true);
    api
      .conversation(chatId)
      .then((c) => {
        if (cancelled) return;
        setConv(c);
        const lastTask = [...c.messages].reverse().find((m) => m.task_id)?.task_id;
        setTaskId(lastTask ? String(lastTask) : "");
      })
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoadingConv(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId]);

  // Scroll the whole page to the bottom (not just the thread end) so the sticky composer
  // sits below the newest message instead of covering it.
  useEffect(() => {
    if (conv || pending) window.scrollTo({ top: document.documentElement.scrollHeight });
  }, [conv?.id, conv?.messages.length, pending]);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
  }, [prompt]);

  async function send(text: string) {
    const t = text.trim();
    if (!t || busy) return;
    const target = conv?.id ?? null;
    setBusy("send");
    setError(null);
    setPreview(null);
    setPending({ prompt: t, since: Date.now() });
    setPrompt("");
    try {
      const r = await api.prompt({
        prompt: t,
        model,
        effort,
        task_id: taskId ? Number(taskId) : null,
        conversation_id: target,
        memory: target === null ? newChatMemory : undefined,
      });
      if (target !== null) {
        setConv((cur) => (cur && cur.id === r.conversation_id ? { ...cur, messages: [...cur.messages, r] } : cur));
      } else if (chatIdRef.current === null) {
        // Still on the new chat: open the chat that was just created.
        setConv(await api.conversation(r.conversation_id!));
        window.location.hash = `/console?c=${r.conversation_id}`;
      }
      list.reload();
      tasks.reload();
    } catch (e) {
      setError((e as Error).message);
      setPrompt((cur) => cur || t);
    } finally {
      setBusy(null);
      setPending(null);
    }
  }

  async function doPreview() {
    if (!prompt.trim() || busy) return;
    setBusy("preview");
    setError(null);
    try {
      setPreview(await api.preview({ prompt, model, effort, conversation_id: conv?.id ?? null }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function toggleMemory(on: boolean) {
    if (!conv) {
      setNewChatMemory(on);
      return;
    }
    try {
      await api.setMemory(conv.id, on);
      setConv((cur) => (cur && cur.id === conv.id ? { ...cur, memory: on ? 1 : 0 } : cur));
      list.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function rate(m: RequestDetail, value: -1 | 1) {
    const next = m.rating === value ? 0 : value;
    try {
      const r = await api.rate(m.id, next);
      setConv((cur) =>
        cur ? { ...cur, messages: cur.messages.map((x) => (x.id === r.id ? { ...x, rating: r.rating } : x)) } : cur,
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function newChat() {
    setConv(null);
    setPrompt("");
    window.location.hash = "/console";
    inputRef.current?.focus();
  }

  const openTasks = (tasks.data ?? []).filter((t) => t.status !== "done");
  const indexOn = status?.index?.enabled ?? false;
  const memoryOn = conv ? Boolean(conv.memory) : newChatMemory;
  const items = list.data?.items ?? [];
  const showEmpty = !conv && !pending && !loadingConv;

  return (
    <div className="chat">
      <section className="thread-col" aria-label="Conversation">
        {conv && (
          <ChatHeader
            conv={conv}
            onRenamed={(c) => {
              setConv(c);
              list.reload();
            }}
            onDeleted={() => {
              list.reload();
              newChat();
            }}
            onError={setError}
          />
        )}

        <div className="thread">
          {showEmpty && (
            <div className="chat-empty">
              <p>Ask anything. Each message is routed on its own, and the chat is saved so you can pick it up later.</p>
            </div>
          )}
          {loadingConv && <p className="muted">Loading chat…</p>}
          {conv?.messages.map((m) => (
            <Turn key={m.id} m={m} busy={busy !== null} onRate={rate} onRetry={(p) => send(p)} />
          ))}
          {pending && (
            <article className="turn">
              <div className="msg-user">{pending.prompt}</div>
              <div className="msg-assistant">
                <Working since={pending.since} />
              </div>
            </article>
          )}
          {preview && !pending && (
            <div className="msg-route preview">
              <p className="muted small">Route preview for your draft. Nothing was answered or saved.</p>
              <RouteSteps view={fromPlan(preview)} pending />
            </div>
          )}
          {error && <ErrorNote>{error}</ErrorNote>}
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            send(prompt);
          }}
        >
          <label htmlFor="prompt" className="sr-only">Message</label>
          <textarea
            id="prompt"
            ref={inputRef}
            value={prompt}
            rows={2}
            onChange={(e) => {
              setPrompt(e.target.value);
              setPreview(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send(prompt);
              }
            }}
            placeholder={conv ? "Reply…" : "Ask anything"}
          />
          <div className="composer-bar">
            <label>
              <span>Model</span>
              <select value={model} onChange={(e) => { setModel(e.target.value); setPreview(null); }}>
                <option value="auto">Auto (router)</option>
                {modelKeys.map((k) => (
                  <option key={k} value={k}>{label(k)}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Effort</span>
              <select value={effort} onChange={(e) => { setEffort(e.target.value); setPreview(null); }}>
                <option value="auto">Auto (reasoning)</option>
                {(config?.efforts ?? []).map((e) => (
                  <option key={e} value={e}>{effortLabel(e)}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Task</span>
              <select value={taskId} onChange={(e) => setTaskId(e.target.value)}>
                <option value="">No task</option>
                {openTasks.map((t) => (
                  <option key={t.id} value={t.id}>{t.title}</option>
                ))}
              </select>
            </label>
            {indexOn && (
              <label className="toggle" title="Adds related turns from your other chats to each message in this chat">
                <input type="checkbox" checked={memoryOn} onChange={(e) => toggleMemory(e.target.checked)} />
                Memory
              </label>
            )}
            <div className="composer-actions">
              <button
                type="button"
                className="ghost"
                onClick={doPreview}
                disabled={!prompt.trim() || busy !== null || (model !== "auto" && effort !== "auto")}
              >
                {busy === "preview" ? "Checking…" : "Preview route"}
              </button>
              <button type="submit" className="primary" disabled={!prompt.trim() || busy !== null}>
                Send
              </button>
            </div>
          </div>
          <p className="hint">Enter sends, Shift+Enter adds a line.</p>
        </form>
      </section>

      <aside className="chat-list" aria-label="Chats">
        <div className="chat-list-head">
          <h2>Chats</h2>
          <button className="ghost small with-icon" onClick={newChat}>
            <Plus size={14} aria-hidden /> New chat
          </button>
        </div>
        <label className="search">
          <Search size={14} aria-hidden />
          <span className="sr-only">Search chats</span>
          <input type="search" placeholder="Search chats" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        {search && list.data?.search && (
          <p className="search-note">
            {list.data.search === "words"
              ? "Matching words only. Search by meaning needs the vector index."
              : "Ranked by meaning and by matching words together"}
          </p>
        )}
        <ul>
          {items.map((c) => (
            <li key={c.id}>
              <a
                href={`#/console?c=${c.id}`}
                className="chat-item"
                aria-current={c.id === chatId ? "page" : undefined}
              >
                <span className="chat-title">{c.title}</span>
                <span className="chat-meta">
                  {c.last_status === "error" ? <span className="error-text">Failed</span> : <ModelTag modelKey={c.last_model} />}
                  <span className="muted">
                    {c.turns > 1 ? `${c.turns} turns, ` : ""}
                    {when(c.updated_at)}
                  </span>
                </span>
              </a>
            </li>
          ))}
        </ul>
        {list.data && items.length === 0 && (
          <p className="muted small">{search ? "No chats match." : "Your chats will show up here."}</p>
        )}
        {list.data && list.data.total > items.length && (
          <p className="muted small">Showing the latest {items.length} of {list.data.total}. Search to find older ones.</p>
        )}
        <div className="chat-export">
          <a href="/api/export.jsonl?format=conversations" download>Download all chats</a>
          <a href="/api/export.jsonl?format=turns" download>Download turns for training</a>
        </div>
      </aside>
    </div>
  );
}

function ChatHeader({ conv, onRenamed, onDeleted, onError }: {
  conv: Conversation;
  onRenamed: (c: Conversation) => void;
  onDeleted: () => void;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(conv.title);
  useEffect(() => setTitle(conv.title), [conv.id, conv.title]);

  async function save() {
    try {
      onRenamed(await api.renameConversation(conv.id, title));
      setEditing(false);
    } catch (e) {
      onError((e as Error).message);
    }
  }

  return (
    <header className="thread-head">
      {editing ? (
        <form
          className="rename"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <label htmlFor="chat-title" className="sr-only">Chat title</label>
          <input id="chat-title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus maxLength={200}
            onKeyDown={(e) => e.key === "Escape" && setEditing(false)} />
          <button type="submit" className="primary small" disabled={!title.trim()}>Save</button>
          <button type="button" className="ghost small" onClick={() => setEditing(false)}>Cancel</button>
        </form>
      ) : (
        <>
          <h2>{conv.title}</h2>
          <div className="thread-actions">
            <button className="ghost small with-icon" onClick={() => setEditing(true)}>
              <Pencil size={13} aria-hidden /> Rename
            </button>
            <button
              className="ghost small danger with-icon"
              onClick={async () => {
                if (!window.confirm(`Delete "${conv.title}"? Its turns are removed from the request log and stats too.`)) return;
                try {
                  await api.deleteConversation(conv.id);
                  onDeleted();
                } catch (e) {
                  onError((e as Error).message);
                }
              }}
            >
              <Trash2 size={13} aria-hidden /> Delete
            </button>
          </div>
        </>
      )}
    </header>
  );
}

function Turn({ m, busy, onRate, onRetry }: {
  m: RequestDetail;
  busy: boolean;
  onRate: (m: RequestDetail, value: -1 | 1) => void;
  onRetry: (prompt: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const routeRef = useRef<HTMLDivElement>(null);
  const { config } = useApp();
  useEffect(() => {
    if (open) routeRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [open]);
  const provider = m.provider ?? (m.model_key ? config?.models[m.model_key]?.provider : undefined);
  const think = m.think === null || m.think === undefined ? null : Boolean(m.think);
  const effortNote = provider === "ollama" ? effortText(provider, m.effort, think) : m.effort ? `${effortLabel(m.effort)} effort` : null;

  return (
    <article className="turn">
      <div className="msg-user">{m.prompt}</div>
      <div className="msg-assistant">
        <div className="msg-meta">
          {m.status === "ok" ? <ModelTag modelKey={m.model_key} /> : <span className="error-text">No answer</span>}
          {m.status === "ok" && effortNote && <span className="muted">{effortNote}</span>}
          {m.fell_back ? <span className="flag">fallback</span> : null}
          {m.memory?.length ? (
            <span className="flag" title={m.memory.map((x) => x.title).join(", ")}>
              memory from {m.memory.length === 1 ? "1 chat turn" : `${m.memory.length} chat turns`}
            </span>
          ) : null}
          {m.routing?.learned || /moved (up|down) to/.test(m.route_note ?? "") ? (
            <span className="flag">routed by your ratings</span>
          ) : null}
        </div>
        {m.status === "ok" ? <Md>{m.response ?? ""}</Md> : <ErrorNote>{m.error}</ErrorNote>}
        <div className="msg-actions">
          {m.status === "ok" ? (
            <>
              <IconButton label="Good answer" pressed={m.rating === 1} onClick={() => onRate(m, 1)}>
                <ThumbsUp size={15} aria-hidden />
              </IconButton>
              <IconButton label="Bad answer" pressed={m.rating === -1} onClick={() => onRate(m, -1)}>
                <ThumbsDown size={15} aria-hidden />
              </IconButton>
              <CopyButton text={m.response ?? ""} />
            </>
          ) : (
            <button className="ghost small with-icon" onClick={() => onRetry(m.prompt)} disabled={busy}>
              <RotateCcw size={13} aria-hidden /> Try again
            </button>
          )}
          <button className="link-button" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            How it was routed <ChevronDown size={14} aria-hidden className={open ? "flip" : undefined} />
          </button>
          <span className="muted small msg-time">{when(m.created_at)}</span>
        </div>
        {open && (
          <div className="msg-route" ref={routeRef}>
            <RouteSteps view={fromDetail(m)} />
            {m.status === "ok" && <UsageLine d={m} />}
          </div>
        )}
      </div>
    </article>
  );
}

function IconButton({ label, pressed, onClick, children }: {
  label: string;
  pressed?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className="icon-btn" aria-label={label} title={label} aria-pressed={pressed} onClick={onClick}>
      {children}
    </button>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard API needs a secure context (fails over plain http on another device), so fall back.
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }
  return (
    <IconButton label={copied ? "Copied" : "Copy answer"} onClick={copy}>
      {copied ? <Check size={15} aria-hidden /> : <Copy size={15} aria-hidden />}
    </IconButton>
  );
}

function Working({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const s = Math.floor((now - since) / 1000);
  return (
    <p className="muted working">
      Routing and waiting for an answer{s >= 2 ? `, ${s} s` : ""}
    </p>
  );
}
