# Switchboard

[![CI](https://github.com/Anders-T-Strand/Switchboard/actions/workflows/ci.yml/badge.svg)](https://github.com/Anders-T-Strand/Switchboard/actions/workflows/ci.yml)

A self-hosted AI console that decides, for every prompt, which model should answer it and how hard that model should think.

Most prompts are answered by a Qwen model running locally through Ollama. The ones that need more go to Claude Sonnet or Opus through Claude Code's headless mode, on a Claude Pro subscription instead of API billing. Every chat is saved and can be picked up later, every turn records why it was routed where it was, and a thumbs up or down on an answer teaches the router which model handles which kind of prompt.

![Overview](docs/overview.png)

## Highlights

- **Two-axis routing.** A local classifier rates each prompt for complexity (which model) and reasoning (how much thinking) separately, so a prompt can run on Opus at low effort or on Sonnet at extra high.
- **A router that learns from your feedback.** New prompts are matched against past prompts in a Qdrant vector index. Thumbs up or down count as votes, and so does your next message when it says an answer was wrong or that it worked. If a model's answers to similar prompts went badly, the prompt moves up a model ladder. If a cheaper model's went well, it moves down.
- **One chat, several models.** A conversation can move between the local model and Claude, with history passed along in the form each one takes, and follow-ups rated in the context of what they refer to.
- **Hybrid search and memory.** Chat search fuses vector and keyword results with reciprocal rank fusion. Memory pulls related turns from other chats into a new one.
- **Usage you can see.** Tokens, latency and API-equivalent cost per model, plus your own Claude Code sessions read incrementally from its local logs.
- **Its own window on Windows.** `switchboard.cmd` opens the dashboard as a desktop app, with a tray icon and its own name and icon on the taskbar, and no terminal left open. It is the same local server in one Python process, with no Electron and no bundled exe.
- **Tested and checked.** 230 tests run against a fake Ollama, a fake `claude` CLI and Qdrant's in-process mode, on Windows and Linux in CI.

## Architecture

```mermaid
flowchart TB
    UI["React dashboard<br/>app window or browser tab"] <-->|REST| API["FastAPI"]
    CLI["switchboard ask"] -->|REST| API
    API --> CL["Classifier<br/>local model already in VRAM"]
    CL -->|"task type, complexity, reasoning"| RT["Rules, effort map, learning ladder"]
    QD[("Qdrant<br/>turn vectors")] -.->|"similar rated prompts"| RT
    RT -->|"model + effort"| OL["Ollama<br/>Qwen on a local GPU"]
    RT -->|"model + effort"| CC["claude -p<br/>Sonnet or Opus"]
    OL --> DB[("SQLite<br/>chats, turns, ratings")]
    CC --> DB
    LOGS["Claude Code session logs"] -->|"incremental sync"| DB
    DB -->|"background indexer"| EM["qwen3-embedding"]
    EM --> QD
```

## Design decisions

- **Claude through `claude -p`, not the API.** Routed calls run on an existing Pro login, so they cost nothing extra. The CLI's API key variables are stripped from its environment so a stray key can't switch it to API billing. Cost is still shown at API rates, as a measure of what was used.
- **Built around one GPU.** The classifier runs on whichever local model is already loaded, a loaded model answers instead of swapping in another, and every Ollama call uses the same context size, because Ollama reloads a model when that changes.
- **Complexity and reasoning are separate ratings.** How capable a model needs to be and how long it should think are different questions. Mapping them to model and effort separately keeps strong models from overthinking easy-but-delicate prompts.
- **SQLite is the record, Qdrant is derived.** Vectors can be rebuilt at any time. Each indexed turn records which embedding model made it, so changing models re-indexes, and a missing or resized collection is recreated from SQLite.
- **Learning comes from you, not from the models grading themselves.** A model judging its own answers would confirm its own mistakes, and having Claude grade every answer would cost a Claude call per prompt. So the votes are your ratings, plus corrections and approvals the classifier reads from your next message. Those count as half a rating, since they're an inference, and your own rating always replaces them. Moves need several similar votes, and every move is shown with the prompts behind it.
- **History is shaped per provider.** Ollama gets earlier turns as chat messages. Claude gets a transcript in its system prompt, which also covers turns another model answered. Both are trimmed oldest first to a per-provider budget.
- **A window around the server, not a second app.** The desktop app is the same FastAPI server and the same built dashboard. pywebview shows it in a WebView2 window and pystray adds the tray icon, all in the Python process that runs the server. Electron or Tauri would have meant a second toolchain and a packaging step for the same page. The server's port is also the lock that keeps it to one copy: a second launch finds the port taken and asks the first to bring its window forward.

## Tech stack

Python 3.11+, FastAPI, SQLite, Qdrant, Ollama, Claude Code CLI. React 19, TypeScript, Vite. pywebview and pystray for the app window. pytest, ruff and GitHub Actions.

## How routing works

1. **Classify.** A local model reads the prompt and rates it on two separate scales from 1 to 5. Complexity is how capable a model has to be. Reasoning is how much step-by-step thinking the answer needs. It also labels the task type (code, writing, math, analysis...). If Ollama is down, a keyword heuristic takes over so routing never blocks.
2. **Pick a model.** Rules in `config.yaml` are checked top to bottom and the first match wins. By default, complexity 5 goes to Claude Opus, 4 goes to Claude Sonnet, and so does a complexity 3 prompt that needs reasoning 5. Everything else stays local.
3. **Pick an effort.** Reasoning maps to an effort level: 1 and 2 are low, 3 is medium, 4 is high, 5 is extra high. Claude gets it through `CLAUDE_CODE_EFFORT_LEVEL`, which overrides your own `/effort` setting for that call only. Local models get thinking off at low and on above it.
4. **Answer, with fallback.** If the chosen model fails (Ollama offline, not logged in to Claude Code, usage limit hit), the router tries the next model in `fallback_order` at the same effort and records why.

Because the model and the effort are picked separately, they combine the way the prompt needs:

| Prompt | Complexity | Reasoning | Runs on |
|---|---|---|---|
| A wedding speech in a famous writer's style | 5 | 1 | Opus at low |
| Design an exactly-once event pipeline | 5 | 5 | Opus at extra high |
| A race condition across three workers | 4 | 5 | Sonnet at extra high |
| Refactor a function to remove shared state | 4 | 4 | Sonnet at high |
| A 3-week study plan | 3 | 3 | Qwen, thinking on |
| The capital of Norway | 1 | 1 | Qwen, thinking off |

Rules can pin the effort (`effort: high`) or clamp it (`min_effort`, `max_effort`), and a Claude model can have its own `max_effort` cap, for example to keep Opus from using too much of your plan. Picking a model or effort in the Console, or with `--model` and `--effort` on the CLI, overrides the router for that prompt.

If a rule picks a local model that isn't loaded but another one is, the loaded model answers. Turn this off with `prefer_loaded_local: false`.

## Chats

Every prompt belongs to a chat, including ones sent from the terminal, so any earlier question can be picked up again. The Console lists chats newest first, with search over titles and every message.

- **Each turn is routed on its own.** The classifier sees the previous exchange, so a short follow-up like "why?" or "make it shorter" is rated as hard as the work it refers to instead of as trivial.
- **History travels with the model.** Local models get earlier turns as chat messages. Claude gets them as a transcript in its system prompt, which works even when earlier replies came from Qwen. The oldest turns are left out first once a chat outgrows `chat.history_chars`, and the route note says when that happened.
- **Rate answers, or just reply.** Thumbs up or down on each answer. If you don't rate one but your next message says it was wrong ("that doesn't work", the same question again) or that it worked, the answer is marked corrected or approved. Both are counted per model on the Overview, and the router learns from them (see below).
- **Download the history.** The chat list links to two JSON Lines files: whole chats, and one line per turn with the routing decision, the outcome, the previous prompt, your rating and what your next message said about the answer.

Prompts sent before chats existed were turned into one-turn chats the first time the server started, so they can be continued too.

## Vector index (Qdrant)

Every answered turn is embedded through Ollama (`qwen3-embedding:0.6b` by default) and stored in [Qdrant](https://qdrant.tech). SQLite stays the record and Qdrant holds the vectors, so the index can always be rebuilt from SQLite. Each turn is one point with two named vectors: the prompt alone, and the prompt with its answer. A background thread indexes turns as they're saved, catches up on history the first time it runs, and re-indexes everything if you switch embedding model.

The index is used for three things:

- **Search by meaning.** Chat search ranks chats by meaning and by matching words together (reciprocal rank fusion), so "that vacuum thing in Postgres" finds the autovacuum chat.
- **A router that learns from your feedback.** Each new prompt is compared with past prompts. Every similar answer you rated is a vote, and an answer you didn't rate still counts at `follow_up_weight` (half by default) when your next message corrected or approved it. If the routed model's votes were mostly bad and add up to at least `min_votes`, the prompt moves up `routing.learn.ladder`, for example from Qwen to Sonnet. If a cheaper model's good votes reach `min_votes` with no bad ones, it moves down. "How it was routed" lists the closest past prompts with how their answers went, and the answer is tagged when feedback changed the route. The follow-up reading costs nothing extra, because it's one more field in the classifier call every message already makes.
- **Memory.** Turn on Memory for a chat in the Console, and each message brings up to four related turns from your other chats into the system prompt, for local models and Claude alike. The answer is tagged with how many it used, and "How it was routed" links to them.

### Running Qdrant

```powershell
ollama pull qwen3-embedding:0.6b
```

Then pick one:

- **Docker Desktop (easiest).** `.\switchboard` starts a `switchboard-qdrant` container when Qdrant isn't already running, storing data in a named Docker volume. Qdrant warns that mounting a Windows folder can lose data, which the named volume avoids. It only listens on 127.0.0.1, since Qdrant has no login by default. To run it yourself: `docker run -d --name switchboard-qdrant --restart unless-stopped -p 127.0.0.1:6333:6333 -v switchboard_qdrant:/qdrant/storage qdrant/qdrant`
- **Native Windows build.** Download `qdrant-x86_64-pc-windows-msvc.zip` from [Qdrant's releases](https://github.com/qdrant/qdrant/releases), unzip it and run `qdrant.exe`. It listens on port 6333 and keeps its data in a `storage` folder next to it.
- **No server.** In `config.yaml`, remove `index.qdrant.url` and set `index.qdrant.path: data/qdrant`. The Qdrant client then stores everything in that folder itself. Fine for one person, slower than the server.

To look inside, open http://localhost:6333/dashboard. Each point's id is the request id in SQLite, and its payload holds the model, effort, task type, complexity, reasoning and the start of the prompt. Full text and ratings stay in SQLite.

The sidebar shows how many turns are indexed. Without Qdrant or the embedding model, everything else keeps working and search falls back to matching words. The Models page shows the index status and has a button to re-index everything.

The embedding model is small (about 640 MB) and stays loaded next to your chat model.

## What it tracks

- **Chats and router requests.** Prompt, answer, classification, rule, model, effort, context sent along, tokens, latency, fallbacks, your rating and how your next message reacted, stored in SQLite.
- **Claude Code usage.** Your own Claude Code sessions are read from `~/.claude/projects/**/*.jsonl` every minute, so the dashboard shows usage by model and day even for work done outside it. Calls the router makes through `claude -p` are tagged separately. Chats on claude.ai don't write local logs, so they can't be counted.
- **Cost at API rates.** A Pro plan isn't billed per token, so costs are shown as what the same usage would cost on the Claude API. Local answers are priced against a reference model (Sonnet by default) to estimate what running locally kept off Claude. Local and Claude tokenizers differ, so treat that one as a ballpark.
- **Tasks.** A simple to do / in progress / done board. Attach prompts to a task to see how many tokens each piece of work takes.

## Today

The Today page is a morning brief: a list, built once a day, of what needs doing. Mail that needs a reply comes from an Outlook inbox, sorted by a local model into what only you can answer and what a model could draft. Coursework comes from a Canvas calendar feed. The page also shows your open tasks.

```powershell
python -m switchboard brief connect outlook  # sign in to Microsoft with a code
python -m switchboard brief connect canvas   # paste your calendar feed link at a hidden prompt
python -m switchboard brief                  # build today's brief now
python -m switchboard brief status           # the last build, as counts and nothing from the items
```

How mail gets there:

- **Read-only, through your own app registration.** Switchboard signs in to Microsoft Graph with the device code flow, as a public client with no secret, and asks only for `Mail.Read`, which can't send, delete or change anything. The sign-in is kept in the credential store and renewed on every build. `connect` walks you through the registration and prints only the mailbox's shape: how many messages and conversations, how many you've answered.
- **Handled mail leaves the list on its own.** A conversation where you replied after its last message is done, and so is one you flagged complete. A new message in it opens it again.
- **Sorted by a local model and nothing else.** Each new message goes to Ollama once, with its sender, subject and the 255-character preview Graph provides, fenced off as data. The answer has to fit a fixed JSON schema: reply, a task or nothing to do, whether a model could draft the reply, a category, a one-line summary and any deadline the mail names. Every field is checked, links are stripped from what the model writes, and the result is shown as plain text, so a message written to fool the model can at most mislabel itself. Mail the model says needs nothing stays off the list.
- **If the model is down,** mail Outlook files under Focused is listed unsorted, the rest waits, and the next build tries again.
- **Long sign-ins fit.** Windows Credential Manager refuses a value over 1,280 characters, and a Microsoft refresh token can be longer, so a long secret is stored in parts.

How coursework gets there:

- **The feed link is a secret.** Anyone who has it can read your calendar, so it goes in the operating system's credential store through `keyring`, never in a config file or the data folder. `connect` checks that the link returns a calendar before it saves it, and prints only the feed's shape: how many events, how their ids are built, what kind of dates they carry.
- **A small calendar reader of its own** takes four properties from each event and leaves the rest, including the description, which is text an instructor wrote. A link is kept only when it is `https` and goes back to the same Canvas the feed came from.
- **The feed can't say whether you handed something in.** So you tick coursework off yourself, and an assignment leaves the list two days after it was due.
- **The page shows what a source still lists.** An item that drops out stays in the database. It comes back if its due date moves, and it keeps a status you gave it.

The rules every source has to keep:

- **It never touches the router or the chat tables.** What a brief reads is private, while chats are indexed and can reach Claude through Memory or a fallback. So brief items live in tables of their own, and the code that builds them has no path to Claude.
- **A source can't break the others, or leak through an error.** One that fails is reported, the rest go on, and the last list stays in place. The page shows only a message the source wrote for it. What an unexpected error says is kept out of the log and the database, because it can repeat a private address.
- **Text from outside is treated as untrusted.** It is cut to length before it is stored, and only `https` links are kept.

## Requirements

- Windows 10/11, macOS or Linux
- Python 3.11 or newer
- Node.js 22 LTS (only to build the dashboard)
- [Ollama](https://ollama.com) with at least one model pulled
- [Claude Code](https://code.claude.com), installed and logged in with your Claude account

The app window on Windows is drawn by the WebView2 runtime. Windows 11 includes it and Windows 10 usually has it. Without it, use the browser instead (`.\switchboard serve`).

## Setup on Windows

```powershell
git clone https://github.com/Anders-T-Strand/Switchboard.git
cd Switchboard

# Pull the local models named in config.yaml (or edit config.yaml to match what you have)
ollama pull qwen3.6:35b-a3b

# Make sure Claude Code works headless on your login
claude -p "say hi"

# Start it. The first run sets up Python and builds the dashboard.
.\switchboard
```

Switchboard opens in its own window. The terminal shows the setup, which takes a while the first time, and closes once the window is up. You can also double-click `switchboard.cmd` in Explorer.

After the first run it just starts. It rebuilds the dashboard on its own when you pull changes.

### The app window

- **Closing the window doesn't stop Switchboard.** It hides to a tray icon and the server keeps running, so `ask` in a terminal still works. Click the tray icon to get the window back, or right-click it for Open, Open in browser and Quit. Quit is what stops the server. Windows 11 puts new tray icons behind the `^` arrow next to the clock, and you can drag this one out of there.
- **One copy runs at a time.** Starting it again brings the window that's already open to the front.
- **To keep it on the taskbar,** right-click its taskbar icon while it's running and choose Pin to taskbar. Pin the running app like this and not a shortcut to `switchboard.cmd`, or the taskbar shows the shortcut and the app as two icons. The pin still runs `switchboard.cmd`, so it keeps picking up what you pull.
- **The server log is a file,** since there is no terminal to print to: `data\logs\switchboard.log`. It starts a new file at 1 MB and keeps the two before it. If Switchboard can't start, a dialog says why.
- **Quit waits five seconds** for a prompt that's still being answered, then drops it.
- **It isn't a browser.** There is no right-click menu, and F5 and Ctrl+F do nothing. Text can be selected and copied, links in answers open in your browser, and the chat downloads ask where to save. `python -m switchboard app --devtools` turns the right-click menu and the browser keys on and opens the web inspector.

Prefer a browser tab? `.\switchboard serve` keeps the server in the terminal and opens the dashboard at http://127.0.0.1:8787, the way it worked before there was a window. It is the same server on the same port, so run one or the other.

To start it from any folder, add this to your PowerShell profile (`notepad $PROFILE`) with your own path:

```powershell
function switchboard { & "C:\path\to\Switchboard\switchboard.cmd" @args }
```

If the sidebar says Claude Code wasn't found, run `where.exe claude` in PowerShell. The server also checks `%USERPROFILE%\.local\bin` and `%APPDATA%\npm`, so a fresh install is usually picked up after a restart. Otherwise set `claude.command` in `config.yaml` to the full path of `claude.exe`.

## Setup on macOS and Linux

```bash
git clone https://github.com/Anders-T-Strand/Switchboard.git
cd Switchboard
ollama pull qwen3.6:35b-a3b        # or edit config.yaml to match your models
ollama pull qwen3-embedding:0.6b
./start.sh --open
```

`start.sh` does the same setup as the Windows launcher: it sets up Python, builds the dashboard when needed and starts Qdrant in Docker if it isn't running. Then it runs the server in the terminal and opens the dashboard in your browser.

The app window is built and tested on Windows. pywebview and pystray also run on macOS and Linux, so `python -m switchboard app` may work there once `backend/requirements-desktop.txt` is installed, but that is untested.

### Manual setup

```powershell
cd backend
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

cd ..\frontend
npm install
npm run build

cd ..\backend
python -m switchboard                      # the server. Add --open for a browser tab

pip install -r requirements-desktop.txt    # or the app window, which needs these as well
python -m switchboard app
```

## Using it from the terminal

With Switchboard running, as the app or as a server:

```powershell
cd backend
python -m switchboard ask "Why does my PyTorch DataLoader hang on Windows?"
python -m switchboard ask --model claude-opus "Review this design: ..."
python -m switchboard ask --effort low "Summarize this changelog: ..."
Get-Content notes.txt | python -m switchboard ask -
python -m switchboard ask --task 3 "Draft the intro paragraph"
python -m switchboard ask --chat last "And how would that change on Linux?"
```

The routing decision, token counts and chat id print to stderr and the answer to stdout, so the answer can be piped somewhere else. `--chat` takes a chat id or `last` to continue a conversation.

## Configuration

Everything lives in `config.yaml`. Restart Switchboard after editing it. With the app window that means Quit in the tray menu first, since closing the window leaves it running.

Settings of your own that shouldn't be published go in `config.local.yaml`, next to it. That file has the same shape, overrides whatever it names, and git ignores it.

| Section | What it controls |
|---|---|
| `models` | Every model the router can use. `provider` is `ollama` or `claude_cli`. For Claude, `model` is the alias passed to `claude --model` and `max_effort` caps its effort. For Ollama, `think` pins thinking on or off. |
| `routing.rules` | Ordered rules. Conditions: `task_types`, `min_complexity`, `max_complexity`, `min_reasoning`, `max_reasoning`. Effort: `effort` to pin it, `min_effort` and `max_effort` to clamp it. |
| `index` | Qdrant location, collection, embedding model and how much text per turn gets embedded. `enabled: false` turns the vector index off. |
| `routing.learn` | The model ladder for learning from feedback, how many similar prompts to look at, how close they must be, how many ratings it takes to move, and how much a correction or approval in a follow-up counts (`follow_up_weight`, 0 ignores them). |
| `chat.memory` | How many turns from other chats a message can bring in, and how related they must be. |
| `chat.history_chars` | How many characters of earlier conversation go along with each message, per provider. Keep the Ollama budget well under the `num_ctx` set on local models (16384 tokens by default). |
| `routing.effort.by_reasoning` | Which effort level each reasoning rating gets. Leave a level out to use your own `/effort` setting for it. |
| `routing.classifier` | Which local model classifies, and whether it may use whatever is already loaded. |
| `routing.fallback_order` | What to try when the chosen model fails. |
| `claude` | CLI path, timeout, tools to block, `use_subscription`, which strips `ANTHROPIC_API_KEY` from the `claude -p` environment so routed calls use your plan instead of API billing, and `skip_project_context`, which keeps this repo's `CLAUDE.md` files and Claude Code's auto memory out of routed calls. |
| `pricing` | Per-million-token prices used for the API-rate estimates. |
| `brief` | `enabled` turns the morning brief on. It is off by default, and `config.local.yaml` is the place to turn it on. `coursework` sets how far ahead an assignment is listed (`lookahead_days`) and how long after its due date (`overdue_days`). `mail` takes your app registration's `client_id`, how far back to look (`lookback_days`) and how many messages to read (`max_messages`). |
| `server.host` | Set to your Tailscale IP or `0.0.0.0` to open the dashboard from other devices. There is no login, so only expose it on a private network. |

To send moderate coding work to Claude as well, uncomment the `code` rule in `config.yaml`. If Opus isn't available on your plan in Claude Code, complexity 5 prompts fall back to Sonnet on their own, or point the `expert` rule at `claude-sonnet`.

### A note on Claude usage limits

Routed Claude calls count against the same usage limits as your normal Claude and Claude Code use. `claude -p` is meant for scripting your own work, so this is a personal tool, not a backend to share with other people.

## Development

```powershell
# Backend lint and tests (fake Ollama, fake claude CLI and in-process Qdrant, no network needed)
cd backend
pip install -r requirements.txt pytest ruff
ruff check switchboard tests
python -m pytest

# Dashboard with hot reload. API calls proxy to the Python server on :8787
cd frontend
npm run dev
```

CI runs the same lint and tests on Ubuntu (Python 3.11 and 3.13) and Windows, plus the frontend type check and build. The tests for the app window put fakes where the window and the tray icon go, so they need no display, and CI never installs `requirements-desktop.txt`.

```
backend/switchboard/
  router.py        classifier prompt, heuristic fallback, rules, effort, learning ladder, history shaping
  index.py         Qdrant indexer, similar-prompt lookup, search and memory
  service.py       chats, turns, ratings, export, tasks
  providers/       Ollama HTTP client and the claude -p runner
  claude_logs.py   incremental reader for Claude Code session logs
  stats.py         aggregations for the dashboard
  db.py            SQLite schema and migrations
  config.py        config.yaml models and validation
  main.py          FastAPI app, background threads, serves the built dashboard at /
  desktop.py       the app window: server thread, tray icon, one copy per port, log file
  brief/           the morning brief: its sources (Outlook through Microsoft Graph, a Canvas calendar feed), mail triage by a local model, its own tables, the build and the brief command
  credentials.py   secrets in the operating system's credential store, through keyring, in parts when they are long
backend/tests/     pytest suite
frontend/src/      React + TypeScript dashboard
```

## Ideas for later

- Train a router on the rated turns (the Qdrant vectors make good features) and compare it against the classifier and the rating-based ladder
- Stream answers as they're generated
- Re-run a turn on a different model to compare answers
- Start with Windows, in the tray, and send notifications from there. `app --hidden` and the tray icon are in place for both
