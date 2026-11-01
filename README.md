# AI Dashboard (Switchboard)

A local dashboard that routes each prompt to the model best suited to answer it, and keeps track of tokens, cost and tasks along the way.

Easy prompts go to a local model running in Ollama. Hard ones go to Claude through Claude Code's headless mode (`claude -p`), which runs on a Claude Pro or Max login instead of API billing. Every request is logged with how it was classified, which rule picked the model, what it cost in tokens, and what it would have cost at API prices.

![Overview](docs/overview.png)

## How routing works

1. **Classify.** A local model reads the prompt and labels it with a task type (code, writing, math, analysis...) and a complexity from 1 to 5. If Ollama is down, a keyword heuristic takes over so routing never blocks.
2. **Pick a model.** Rules in `config.yaml` are checked top to bottom and the first match wins. By default, complexity 5 goes to Claude Opus, 4 goes to Claude Sonnet, and everything else stays local.
3. **Answer, with fallback.** If the chosen model fails (Ollama offline, not logged in to Claude Code, usage limit hit), the router tries the next model in `fallback_order` and records why.

Two details matter on a single GPU:

- The classifier uses whichever configured local model is already in VRAM, so classifying never triggers a model swap.
- If a rule picks a local model that isn't loaded but another one is, the loaded model answers. Turn this off with `prefer_loaded_local: false`.

You can also skip the router and pick a model by hand from the Console or the CLI.

![Request detail](docs/request-detail.png)

## What it tracks

- **Router requests.** Prompt, answer, classification, rule, model, tokens, latency and fallbacks, stored in SQLite.
- **Claude Code usage.** Your own Claude Code sessions are read from `~/.claude/projects/**/*.jsonl` every minute, so the dashboard shows usage by model and day even for work done outside it. Calls the router makes through `claude -p` are tagged separately. Chats on claude.ai don't write local logs, so they can't be counted.
- **Cost at API rates.** A Pro plan isn't billed per token, so costs are shown as what the same usage would cost on the Claude API. Local answers are priced against a reference model (Sonnet by default) to estimate what running locally kept off Claude. Local and Claude tokenizers differ, so treat that one as a ballpark.
- **Tasks.** A simple to do / in progress / done board. Attach prompts to a task to see how many tokens each piece of work takes.

## Requirements

- Windows 10/11 (macOS and Linux work too)
- Python 3.11 or newer
- Node.js 22 LTS (only to build the dashboard)
- [Ollama](https://ollama.com) with at least one model pulled
- [Claude Code](https://code.claude.com), installed and logged in with your Claude account

## Setup on Windows

```powershell
git clone https://github.com/Anders-T-Strand/AI-Dashboard.git
cd AI-Dashboard

# Pull the local models named in config.yaml (or edit config.yaml to match what you have)
ollama pull qwen3.6:35b-a3b

# Make sure Claude Code works headless on your login
claude -p "say hi"

# First run creates the Python environment, builds the dashboard, then starts the server
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Open http://127.0.0.1:8787.

After the first run, `.\start.ps1` just starts the server. Delete `frontend\dist` to force a rebuild after pulling changes.

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
python -m switchboard
```

## Using it from the terminal

With the server running:

```powershell
cd backend
python -m switchboard ask "Why does my PyTorch DataLoader hang on Windows?"
python -m switchboard ask --model claude-opus "Review this design: ..."
Get-Content notes.txt | python -m switchboard ask -
python -m switchboard ask --task 3 "Draft the intro paragraph"
```

The routing decision and token counts print to stderr and the answer to stdout, so the answer can be piped somewhere else.

## Configuration

Everything lives in `config.yaml`. Restart the server after editing it.

| Section | What it controls |
|---|---|
| `models` | Every model the router can use. `provider` is `ollama` or `claude_cli`. For Claude, `model` is the alias passed to `claude --model`. |
| `routing.rules` | Ordered rules. Each can set `task_types`, `min_complexity` and `max_complexity`. |
| `routing.classifier` | Which local model classifies, and whether it may use whatever is already loaded. |
| `routing.fallback_order` | What to try when the chosen model fails. |
| `claude` | CLI path, timeout, tools to block, and `use_subscription`, which strips `ANTHROPIC_API_KEY` from the `claude -p` environment so routed calls use your plan instead of API billing. |
| `pricing` | Per-million-token prices used for the API-rate estimates. |
| `server.host` | Set to your Tailscale IP or `0.0.0.0` to open the dashboard from other devices. There is no login, so only expose it on a private network. |

To send moderate coding work to Claude as well, uncomment the `code` rule in `config.yaml`. If Opus isn't available on your plan in Claude Code, complexity 5 prompts fall back to Sonnet on their own, or point the `expert` rule at `claude-sonnet`.

### A note on Claude usage limits

Routed Claude calls count against the same usage limits as your normal Claude and Claude Code use. `claude -p` is meant for scripting your own work, so this is a personal tool, not a backend to share with other people.

## Development

```powershell
# Backend tests (fake Ollama and a fake claude CLI, no network needed)
cd backend
pip install pytest
python -m pytest

# Dashboard with hot reload. API calls proxy to the Python server on :8787
cd frontend
npm run dev
```

```
backend/switchboard/
  router.py        classifier prompt, heuristic fallback, rule matching, fallback chain
  providers/       Ollama HTTP client and the claude -p runner
  claude_logs.py   incremental reader for Claude Code session logs
  stats.py         aggregations for the dashboard
  service.py       request logging and tasks
  main.py          FastAPI app, serves the built dashboard at /
frontend/src/      React + Vite dashboard
```

## Ideas for later

- Train a router on the logged requests (prompt features in, model that should have answered out) and compare it against the classifier
- Let a request be re-run on a different model from the request log to compare answers
- Thumbs up / down on answers to build labels for that router
