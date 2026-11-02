import { useMemo, useState } from "react";
import { api } from "./api";
import { Light } from "./components/bits";
import { buildColorMap } from "./colors";
import { AppContext, useApi, useHashRoute, type AppContextValue } from "./hooks";
import { Console } from "./pages/Console";
import { Models } from "./pages/Models";
import { Overview } from "./pages/Overview";
import { Requests } from "./pages/Requests";
import { Tasks } from "./pages/Tasks";

const PAGES = [
  { id: "overview", title: "Overview" },
  { id: "console", title: "Console" },
  { id: "requests", title: "Requests" },
  { id: "tasks", title: "Tasks" },
  { id: "models", title: "Models and routing" },
];
const RANGES = [7, 14, 30];

function readDays(): number {
  try {
    const v = Number(window.localStorage.getItem("switchboard.days"));
    return RANGES.includes(v) ? v : 7;
  } catch {
    return 7;
  }
}

export default function App() {
  const [route] = useHashRoute("overview");
  const [days, setDaysState] = useState(readDays);
  const config = useApi(() => api.config(), []);
  const status = useApi(() => api.status(), [], 15_000);

  const setDays = (d: number) => {
    setDaysState(d);
    try {
      window.localStorage.setItem("switchboard.days", String(d));
    } catch {
      // storage unavailable, keep it in memory
    }
  };

  const ctx = useMemo<AppContextValue>(() => {
    const cfg = config.data;
    const modelKeys = cfg ? Object.keys(cfg.models) : [];
    const colors = buildColorMap(modelKeys, cfg ? Object.keys(cfg.pricing.models) : []);
    const labels: Record<string, string> = {};
    if (cfg) Object.entries(cfg.models).forEach(([k, m]) => (labels[k] = m.label || k));
    return {
      config: cfg,
      status: status.data,
      colors,
      modelKeys,
      label: (k) => (k ? labels[k] ?? k : "–"),
    };
  }, [config.data, status.data]);

  const page = PAGES.find((p) => p.id === route) ?? PAGES[0];
  const st = status.data;
  const loaded = st?.models.filter((m) => m.loaded).map((m) => m.label) ?? [];

  return (
    <AppContext.Provider value={ctx}>
      <div className="app">
        <nav className="sidebar" aria-label="Main">
          <a className="brand" href="#/overview">
            <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden>
              <circle cx="9" cy="11" r="4.5" fill="none" stroke="var(--brass)" strokeWidth="2" />
              <circle cx="23" cy="21" r="4.5" fill="none" stroke="var(--brass)" strokeWidth="2" />
              <path d="M12.5 13.5 C16 24 18 24 19.5 19" fill="none" stroke="#3987e5" strokeWidth="2.5" strokeLinecap="round" />
            </svg>
            Switchboard
          </a>
          <ul className="nav">
            {PAGES.map((p) => (
              <li key={p.id}>
                <a href={`#/${p.id}`} aria-current={p.id === page.id ? "page" : undefined}>
                  {p.id === "models" ? "Models" : p.title}
                </a>
              </li>
            ))}
          </ul>
          <div className="lights">
            <Light on={!!st?.ollama.reachable}>
              Ollama{st?.ollama.reachable ? (loaded.length ? `, ${loaded.join(", ")} loaded` : ", nothing loaded") : " offline"}
            </Light>
            <Light on={!!st?.claude.found}>{st?.claude.found ? "Claude Code ready" : "Claude Code not found"}</Light>
            {st?.index?.enabled && (
              <Light on={st.index.ok === true || st.index.ok === false} warn={st.index.ok === false}>
                {st.index.ok === false
                  ? "Qdrant unavailable"
                  : st.index.ok
                    ? `Qdrant, ${(st.index.indexed ?? 0).toLocaleString()} turns indexed${st.index.pending ? `, ${st.index.pending} to go` : ""}`
                    : "Qdrant, connecting"}
              </Light>
            )}
          </div>
        </nav>

        <main className="main">
          <header className="topbar">
            <h1>{page.title}</h1>
            {page.id === "overview" && (
              <div className="segmented" role="group" aria-label="Time range">
                {RANGES.map((d) => (
                  <button key={d} aria-pressed={days === d} onClick={() => setDays(d)}>
                    {d} days
                  </button>
                ))}
              </div>
            )}
          </header>

          {config.error ? (
            <div className="error-note" role="alert">
              Can't reach the Switchboard server ({config.error}). Start it with <code>python -m switchboard</code> in the backend folder.
            </div>
          ) : !config.data ? (
            <p className="muted">Connecting…</p>
          ) : page.id === "overview" ? (
            <Overview days={days} />
          ) : page.id === "console" ? (
            <Console />
          ) : page.id === "requests" ? (
            <Requests />
          ) : page.id === "tasks" ? (
            <Tasks />
          ) : (
            <Models onSynced={status.reload} />
          )}
        </main>
      </div>
    </AppContext.Provider>
  );
}
