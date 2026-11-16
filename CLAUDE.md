# Switchboard

Guidance for working in this repo. The README is the user-facing description and covers each part in more depth. When this file and the code disagree, the code wins.

**The repo is public.** Nothing personal or secret goes into tracked files, tests, fixtures, screenshots or commit messages: no account names, inbox or school details, tokens or machine paths. Personal context for the current work is in `CLAUDE.local.md`, which is gitignored.

## What it is

A self-hosted dashboard that sends each prompt to the best-suited model, picks how hard that model should think, and tracks chats, tokens, cost and tasks. It is built around one Windows PC with a single GPU (an RTX 5090), so swapping local models in and out of VRAM is the thing to avoid.

- **Local models:** Qwen through Ollama. `qwen-moe` (`qwen3.6:35b-a3b`, MoE) is the default and `qwen-dense` (`qwen3.8:27b`) is the other.
- **Claude:** Haiku, Sonnet and Opus through `claude -p` on a Claude Pro login. No API key is involved. The runner strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the environment.
- **Starting it:** `switchboard.cmd` runs `start.ps1 app`. That makes the venv, reinstalls Python packages when `requirements.txt` or `requirements-desktop.txt` changes, rebuilds the frontend when its source changes and starts Qdrant in Docker if needed. Then it hands over to `pythonw -m switchboard app` and exits, so no terminal stays open. The app is the server, a window and a tray icon in one process, on http://127.0.0.1:8787. With arguments (`.\switchboard serve`, `.\switchboard ask "..."`) the launcher runs that command in the terminal with `--open`, as it always did. `start.sh` does the same setup on macOS and Linux and opens the browser.

## Commands

```powershell
# backend. pytest and ruff are not in requirements.txt, so install them once per venv
cd backend
.\.venv\Scripts\python -m pip install pytest ruff
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check switchboard tests

# the app window by hand. python, not pythonw, so the log also prints in the terminal
.\.venv\Scripts\python -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python -m switchboard app --devtools

# frontend
cd frontend
npm run dev      # hot reload, proxies /api to :8787
npm run build    # strict type check, then the build
```

## Rules for changes

- **Test every behavior change.** The fakes in `backend/tests/conftest.py` make it cheap: a fake Ollama (httpx `MockTransport`), a fake `claude` CLI script and Qdrant's in-process mode. The fake classifier reads markers in the prompt. `C<n> R<n>` sets complexity and reasoning, `HARD` and `EXPERT` pick a preset, `WRONG` and `THANKS` set the follow-up.
- **Schema changes go in two places in `db.py`:** `SCHEMA` and `ADDED_COLUMNS`, so existing databases migrate on startup.
- **Code has to run on Python 3.11.** No nested same-type quotes inside f-strings. Newer Pythons accept them, so only CI catches it.
- **CI runs on every push:** ruff and pytest on Ubuntu (3.11 and 3.13) and Windows (3.13), plus the frontend build.
- **Keep `README.md` in step with features.** It is written as a portfolio piece, with highlights, design decisions and how each part works.
- **Line endings:** `.gitattributes` keeps `.ps1` and `.cmd` as CRLF and `.sh` as LF. Don't reformat them.

## Conventions

- **Commits:** never add a `Claude-Session:` link to a commit message. The `Co-Authored-By: Claude` line is fine.
- **Prose** (README, UI copy, commit messages): plain and direct. No padding, no self-congratulation, no semicolons.
- **UI:** technical and dark, in walnut and brass. `--page #1d1915`, `--panel #26201b`, `--brass #d6aa55`, with `#3987e5` as the blue accent. Fonts are Barlow Semi Condensed, IBM Plex Sans and IBM Plex Mono. Stay consistent with what's there.
- **Console layout:** the header and composer stay fixed and only the thread scrolls. This was asked for specifically, so keep it.

## How it works

**Routing.** A local classifier rates each prompt on two separate axes. Task type and complexity (1-5) pick the model through ordered rules in `config.yaml`. Reasoning depth (1-5) picks the effort (low, medium, high, xhigh or max).

- The classifier uses whichever Qwen model is already loaded, so the GPU never swaps models to classify. If Ollama is down, a keyword heuristic takes over.
- Effort reaches Claude Code through `CLAUDE_CODE_EFFORT_LEVEL`. For local models, low turns thinking off. Rules can pin or clamp effort, and a model can cap it with `max_effort`.
- Picking a model or effort in the Console, or with `--model` and `--effort` on the CLI, overrides the router.
- A failed model falls back through `fallback_order`, but only for auto-routed prompts. A model picked by hand never falls back.

**Learning.** Each new prompt is embedded with `qwen3-embedding:0.6b` and compared with past prompts in Qdrant. Every similar past answer is a vote. A thumbs up or down counts fully. An unrated answer counts at `follow_up_weight` (0.5) when the classifier reads the next message as a correction or an approval, and a rating always replaces that. Clearly bad votes move a prompt up the ladder (`qwen-moe`, `claude-sonnet`, `claude-opus`). All-good votes for a cheaper model move it down. The models never grade their own answers, on purpose.

**Chats.** Every prompt belongs to a conversation that can be continued. Ollama gets the history as messages and Claude gets it as an XML transcript in the system prompt, each trimmed to `chat.history_chars`. Memory is a per-chat toggle that adds related excerpts from other chats. Search fuses word matching and search by meaning with reciprocal rank fusion. Export is JSONL, per conversation or per turn.

**Vector index.** Qdrant runs in Docker as `switchboard-qdrant`, bound to 127.0.0.1 because it has no login, with data in the named volume `switchboard_qdrant`. The collection `switchboard_turns` holds one point per answered turn. The point id is the request id, and each point has two named vectors, `prompt` and `turn`. A background thread backfills old turns and re-indexes everything when the embedding model changes. SQLite is the record and Qdrant can always be rebuilt from it.

**Tracking.** Tokens, latency and cost per request. Cost is what the call would have cost at API rates, plus what answering locally saved. Claude Code usage is read from the session logs under `~/.claude`, and calls made from `data/claude-workdir` are tagged as the router's. Tasks are a To do, In progress and Done board. Prompts are tagged with a task from the Console or with `ask --task ID`, and each card totals its prompts, tokens and cost. Nothing moves cards automatically.

## Code map

```
config.yaml                  models, routing rules, effort map, learning, chat, index, pricing
start.ps1 / switchboard.cmd  Windows launcher, the app window by default (start.sh for macOS and Linux)
backend/requirements-desktop.txt   pywebview, pystray and pillow, for the app window only
backend/switchboard/
  router.py        classifier prompt and schema, heuristic fallback, rules, effort, learning ladder, history shaping
  index.py         Qdrant indexer, similar-prompt lookup, search and memory
  service.py       chats, turns, ratings, follow-up flags, export, tasks
  providers/       Ollama HTTP client (chat, embed) and the claude -p runner
  claude_logs.py   incremental reader for Claude Code session logs
  stats.py         aggregations for the Overview page
  pricing.py       cost at API rates, and the cost avoided by answering locally
  db.py            SQLite schema (WAL) and column migrations (ADDED_COLUMNS)
  config.py        pydantic models for config.yaml, with cross-reference validation
  main.py          FastAPI app, background threads, serves the built dashboard at /
  desktop.py       the app window: uvicorn in a thread, pywebview window, pystray tray icon, one copy per port, log file
  __main__.py      CLI: serve (--open), app (--hidden, --devtools), ask (--model, --effort, --task, --chat ID|last, --json), sync
backend/tests/     pytest with the fakes described above
frontend/src/      React 19 + TypeScript (strict) + Vite
  pages/           Overview, Console, Requests, Tasks, Models
  components/      RouteSteps ("How it was routed"), PatchBay, StackedColumns, bits
  styles.css       all styling, with the palette variables at the top
```

## Gotchas

- **Ollama `num_ctx`:** use the same `num_ctx` for every call to a model, or Ollama reloads it. It is set per model in `config.yaml`, at 16384, and the classifier passes the same options.
- **Finding `claude`:** it may not be on PATH. The runner also looks in `~/.local/bin/claude.exe`, `%APPDATA%\npm\claude.cmd` and `~/.claude/local`. If routed calls fail with "Not logged in", run `claude` and `/login` once.
- **What the Console's models know:** nothing about the user or this project. Local models get no system prompt unless Memory is on or the API caller passes one. Claude gets the generic `claude.system_prompt` from config and runs in the empty `data/claude-workdir`. Bash and the file-editing tools are disallowed there. Read, Grep, Glob and the web tools are not.
- **The app's own Claude calls and this file:** `data/claude-workdir` is inside the repo, so `claude -p` would read this file, `CLAUDE.local.md` and Claude Code's auto memory for the repo on every routed call. `claude.skip_project_context` (on by default) keeps all three out by passing `--setting-sources user` and setting `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. `~/.claude/CLAUDE.md` still loads.
- **Tests must never reach a real `claude`:** a dev machine has one installed, and the runner looks in the usual install folders when the command isn't on PATH. A test that makes the CLI unfindable has to patch `install_locations` as well, or it makes a real call.
- **Console CSS:** the Console fills the window on desktop (`.main.main-fill`). The grid needs `grid-template-rows: minmax(0, 1fr)`, or the thread grows instead of scrolling.
- **A second instance needs its own config:** never start one on the default `config.yaml` while testing. It would share `data/` and the Qdrant collection, where point ids are request ids, so a fresh database overwrites real vectors. Point `SWITCHBOARD_CONFIG` at a copy with another `server.port` and either `index.enabled: false` or another `index.qdrant.collection`. To keep test prompts off Claude and the GPU, leave only a local model in that copy and aim `ollama.base_url` at a stub.
- **The app has no console:** under `pythonw`, `sys.stdout` and `sys.stderr` are None, so `print` does nothing and a traceback goes nowhere. `desktop.setup_logging` sends logging and stray output to `data/logs/switchboard.log`, and anything that stops the app has to end in `desktop.alert`, which is a dialog there.
- **GUI imports stay inside functions in `desktop.py`:** CI installs `requirements.txt` only. A module-level import of `webview`, `pystray` or `PIL` breaks the suite everywhere, and a test checks for it. The window and the tray icon are faked in tests, so what only a display can show (`run_gui` and the Windows helpers under it) has to be checked by hand.
- **Closing is not quitting:** the X button hides the window and the server runs on until Quit in the tray menu. On Windows only a close the user asked for is cancelled (the X, Alt+F4, Close window on the taskbar). A plain `WM_CLOSE`, which is what `taskkill` and Task Manager send, and Windows shutting down close it for real, and cancelling those would block shutdown.
- **The taskbar name is not the window title:** Windows calls a `pythonw` window "Python" even with an AppUserModelID and a window icon. The name, the icon and what Pin to taskbar pins come from the relaunch properties `desktop._taskbar_identity` sets on the window. Keep `APP_ID` stable, because a pinned icon is matched to the running app by it.
- **One copy per port:** the app binds the port before anything else. When that fails it posts to `/api/desktop/show` on whatever holds the port and exits. That route exists only in the app, not under `serve`.

## Next steps

### 1. Morning brief (current)

A page built each morning that lists what needs doing, starting with mail that needs a reply and upcoming Canvas assignments, with a first pass at what a model can draft and what needs a person. Nothing is built yet. The open questions are in `CLAUDE.local.md`.

Constraints that follow from the code and from the repo being public:

- **Raw mail and course text stay local.** Force a local model for anything that reads them. Auto-routing can reach Claude through the rules or through `fallback_order`, and only a forced model never falls back.
- **Keep those calls out of the chat tables, or make the indexer skip them.** Every answered turn is indexed, Memory can carry an indexed turn into a later Claude prompt, and indexed turns vote in the learning router.
- **Mail and course text are untrusted input.** They can contain instructions aimed at the model. If Claude plans on summaries, run it with no tools (`--tools ""`) and no MCP servers (`--strict-mcp-config`). Models draft. Nothing gets sent or changed without a click.
- **Personal settings stay out of the tracked `config.yaml`:** Canvas URL, mail queries, schedule. `SWITCHBOARD_CONFIG` already points the app at another file. Tokens go in Windows Credential Manager (the `keyring` package) or an env var, not in `data/`, which is the parent of the folder Claude runs in.
- **Mail API:** Gmail API or Microsoft Graph, depending on the inbox. A Google OAuth client left in "Testing" status gets refresh tokens that expire after seven days, which would break an unattended job quietly.
- **Canvas:** a personal access token gives the REST API (upcoming assignments, due dates, submission status). It can't be scoped, so it carries full account access. If the school blocks student tokens, the calendar feed gives due dates without one.
- **Schedule:** Switchboard only runs while it's open. Use both a Windows Task Scheduler job that runs a new `python -m switchboard brief` and a build on the first open of the day, keyed by date so it only builds once. `brief` has to work without the server running, like `sync` and unlike `ask`.
- **Where it shows:** probably a new Today page. Brief items could become Tasks with one click.
- **What the app window already has for it:** `python -m switchboard app --hidden` starts in the tray with no window, which is what a login item should run. `Shell.notify` in `desktop.py` shows a notification from the tray icon. Neither is wired to anything yet. `notify` has only run against a fake icon, so look at what Windows puts in a real notification's header. It may say Python, as the taskbar did before the window got its relaunch properties, and then `APP_ID` needs registering with Windows.

### 2. Give the Console's models some context

An "about you and this setup" system prompt that goes to every model, Ollama included. The text is personal, so it belongs in a personal config, not the tracked one. Optionally, let a chat link to a project folder so Claude runs there with read-only tools (Read, Grep, Glob) and can answer questions about the code.

### 3. Tasks

- Tag a whole chat with a task, not only single prompts.
- Attribute Claude Code usage to tasks by project folder.

### 4. Later

- Real screenshots in the README, such as a chat with "How it was routed" open and Qdrant's Visualize view.
- From the README's ideas list: train a router on the rated turns and compare it with the classifier, stream answers as they're generated, re-run a turn on another model to compare answers.
