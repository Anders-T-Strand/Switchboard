# Switchboard

[![CI](https://github.com/Anders-T-Strand/Switchboard/actions/workflows/ci.yml/badge.svg)](https://github.com/Anders-T-Strand/Switchboard/actions/workflows/ci.yml)

A self-hosted AI console that decides, for every prompt, which model should answer it and how hard that model should think.

Most prompts are answered by a Qwen model running locally through Ollama. The ones that need more go to Claude Sonnet or Opus through Claude Code's headless mode, on a Claude Pro subscription instead of API billing. Every chat is saved and can be picked up later, every turn records why it was routed where it was, and a thumbs up or down on an answer teaches the router which model handles which kind of prompt.

It also builds a morning brief, a daily list of mail that needs a reply and coursework that is due, sorted by the local model so none of it goes to Claude. Any answer can be read aloud with ElevenLabs.

![Overview](docs/overview.png)

## Highlights

- **Two-axis routing.** A local classifier rates each prompt for complexity (which model) and reasoning (how much thinking) separately, so a prompt can run on Opus at low effort or on Sonnet at extra high.
- **A router that learns from your feedback.** New prompts are matched against past prompts in a Qdrant vector index. Thumbs up or down count as votes, and so does your next message when it says an answer was wrong or that it worked. If a model's answers to similar prompts went badly, the prompt moves up a model ladder. If a cheaper model's went well, it moves down.
- **One chat, several models.** A conversation can move between the local model and Claude, with history passed along in the form each one takes, and follow-ups rated in the context of what they refer to.
- **Hybrid search and memory.** Chat search fuses vector and keyword results with reciprocal rank fusion. Memory pulls related turns from other chats into a new one.
- **Usage you can see.** Tokens, latency and API-equivalent cost per model, plus your own Claude Code sessions read incrementally from its local logs.
- **Its own window on Windows.** `switchboard.cmd` opens the dashboard as a desktop app, with a tray icon and its own name and icon on the taskbar, and no terminal left open. It is the same local server in one Python process, with no Electron and no bundled exe.
- **A morning brief that stays local.** Every morning Switchboard lists the Outlook mail that needs a reply and the Canvas coursework that is due. A local model sorts the mail into what needs you and what it could draft, and writes a draft when asked. Nothing from the brief is sent to Claude, and nothing is ever sent as mail.
- **Answers read aloud, with the latency measured.** A speaker button on each answer has ElevenLabs read it. The audio starts playing while it is still being made, a replay comes from a saved clip at no cost, and every play records its time to first audio.
- **Tested and checked.** 325 tests run against a fake Ollama, a fake `claude` CLI, a fake Microsoft Graph, a fake ElevenLabs and Qdrant's in-process mode, on Windows and Linux in CI.

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
    API <-.->|"read aloud: text out, MP3 back"| EL["ElevenLabs<br/>streaming text to speech"]
    MAIL["Outlook mail<br/>Microsoft Graph, read-only"] --> BR["Morning brief"]
    FEED["Canvas calendar feed"] --> BR
    BR -->|"sort and draft, not through the router"| OL
    BR --> BT[("Brief tables<br/>apart from chats")]
```

## Design decisions

- **Claude through `claude -p`, not the API.** Routed calls run on an existing Pro login, so they cost nothing extra. The CLI's API key variables are stripped from its environment so a stray key can't switch it to API billing. Cost is still shown at API rates, as a measure of what was used.
- **Built around one GPU.** The classifier runs on whichever local model is already loaded, a loaded model answers instead of swapping in another, and every Ollama call uses the same context size, because Ollama reloads a model when that changes.
- **Complexity and reasoning are separate ratings.** How capable a model needs to be and how long it should think are different questions. Mapping them to model and effort separately keeps strong models from overthinking easy-but-delicate prompts.
- **SQLite is the record, Qdrant is derived.** Vectors can be rebuilt at any time. Each indexed turn records which embedding model made it, so changing models re-indexes, and a missing or resized collection is recreated from SQLite.
- **Learning comes from you, not from the models grading themselves.** A model judging its own answers would confirm its own mistakes, and having Claude grade every answer would cost a Claude call per prompt. So the votes are your ratings, plus corrections and approvals the classifier reads from your next message. Those count as half a rating, since they're an inference, and your own rating always replaces them. Moves need several similar votes, and every move is shown with the prompts behind it.
- **History is shaped per provider.** Ollama gets earlier turns as chat messages. Claude gets a transcript in its system prompt, which also covers turns another model answered. Both are trimmed oldest first to a per-provider budget.
- **A window around the server, not a second app.** The desktop app is the same FastAPI server and the same built dashboard. pywebview shows it in a WebView2 window and pystray adds the tray icon, all in the Python process that runs the server. Electron or Tauri would have meant a second toolchain and a packaging step for the same page. The server's port is also the lock that keeps it to one copy: a second launch finds the port taken and asks the first to bring its window forward.
- **Read aloud goes through the server.** The page asks Switchboard for an answer's audio and Switchboard asks ElevenLabs, so the API key stays in the credential store and never reaches the browser. The server waits for the first chunk of audio before it answers the page. That is why an error can still come back as a message, and why the time to first audio is known in time to send it along in a header.
- **The brief never reaches Claude.** Mail and coursework are private, while chats are indexed and can reach Claude through Memory or a fallback. So the brief calls Ollama directly instead of going through the router, and keeps its items in tables of their own. Every mail is treated as untrusted input: it goes to the model fenced off as data, the answer has to fit a fixed JSON schema, and the page shows it as plain text.
- **A setting Switchboard doesn't know stops it at startup.** Pydantic drops unknown keys by default, so a section one level out of place was ignored, and the app then failed somewhere else with a message that pointed the wrong way. Now every part of the config refuses keys it doesn't define, and the error names the file, the setting and, for one in the wrong place, where it belongs.

## Tech stack

Python 3.11+, FastAPI, SQLite, Qdrant, Ollama, Claude Code CLI, Microsoft Graph, ElevenLabs text to speech. React 19, TypeScript, Vite. pywebview and pystray for the app window, keyring for secrets. pytest, ruff and GitHub Actions.

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

## Read aloud

Each answer in the Console has a speaker button that reads it aloud with [ElevenLabs](https://elevenlabs.io). It is off until you save an API key and turn it on:

```powershell
python -m switchboard speech connect   # paste your ElevenLabs API key at a hidden prompt
python -m switchboard speech status    # plays, characters sent and timings, as counts
python -m switchboard speech forget    # remove the key again
```

Then set `enabled: true` under `speech:` in `config.local.yaml` and restart Switchboard.

- **The key stays on the server.** It is kept in the operating system's credential store. The page asks Switchboard for an answer's audio, and Switchboard calls ElevenLabs' streaming endpoint with the key in a header.
- **It plays before the clip is finished.** Switchboard passes the audio on as ElevenLabs makes it, and the page plays it with Media Source Extensions. Where a browser can't stream MP3 that way, the page fetches the whole clip first.
- **Markdown is read as sentences.** Code blocks are skipped, a link is read by its text, a bare web address is left out and a table is read row by row. ElevenLabs bills by the character, so a long answer is cut at a sentence near `max_chars`, 2,500 by default.
- **A replay costs nothing.** A finished clip is kept under `data/speech`, named by a hash of the text, the voice, the model and the format, and playing it again sends nothing. A clip that was stopped or broke off is never kept, and deleting a chat deletes the clips no other chat uses.
- **One answer plays at a time.** Starting another stops the one playing, and pressing the button again stops it.
- **Errors come back before any audio,** as a message under the answer: read aloud is off, no key is saved, ElevenLabs refused the key, or what ElevenLabs said went wrong.

How the latency is measured:

- **Time to first audio is measured on the server,** from when the request reaches Switchboard to when the first chunk of audio from ElevenLabs is in hand. It covers looking up and cleaning the answer, reading the key, the connection to ElevenLabs and ElevenLabs' own time. The Console shows it next to the button after every play. Its tooltip adds how long after the click the sound started in the browser, which also counts decoding and buffering.
- **Every play is recorded:** characters sent, whether it came from a saved clip, how the stream ended (sent in full, stopped by the page before it was, or broken off by ElevenLabs), the time to first audio and the time to the last byte. Nothing from the answer goes in that table.
- **The Models page shows the medians,** from ElevenLabs and from saved clips separately, along with the characters sent in plays ElevenLabs answered and how much room the saved clips take.
- **The defaults are picked for speed.** The model is `eleven_flash_v2_5`, ElevenLabs' lowest-latency model, and the voice is George, one of its default voices. With read aloud on, the HTTP client is made when Switchboard starts, since loading its certificates takes over a tenth of a second. It stays open between plays and keeps its connection for a minute, so plays close together skip the TLS handshake.

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
- **Read aloud.** Every play, with the characters sent to ElevenLabs and how long the first audio took.
- **Morning brief builds.** When each build ran and what started it, what each source found or why it failed, and the local model's tokens and time.

## Today

The Today page is a morning brief: a list, built once a day, of what needs doing. Mail that needs a reply comes from an Outlook inbox, sorted by a local model into what only you can answer and what a model could draft. Coursework comes from a Canvas calendar feed. The page also shows your open tasks, and once the brief is on it is the page Switchboard opens on.

It builds itself every morning:

- **At a set time, or as soon as Switchboard starts after it.** After `brief.build_after`, 06:30 by default, the server builds the day's brief once. Started later, because the PC was off, it builds right away. A build that failed is tried again half an hour later.
- **It gives Ollama a moment.** Started with Windows, Switchboard can be up before Ollama is, and mail would go unsorted. So in its first 15 minutes a due build waits for Ollama to answer.
- **A notification says when it's ready,** from the tray icon, with counts and nothing else: "Today's brief is ready: 2 need you, 1 a model can draft and 3 coursework." The tray menu has Build the brief now as well.
- **Start with Windows,** a check box in the tray menu, puts a shortcut in your Startup folder that starts Switchboard hidden in the tray. The brief is then waiting in the morning without you opening anything.

```powershell
python -m switchboard brief connect outlook  # sign in to Microsoft with a code
python -m switchboard brief connect canvas   # paste your calendar feed link at a hidden prompt
python -m switchboard brief                  # build today's brief now
python -m switchboard brief status           # the last build, as counts and nothing from the items
```

How mail gets there:

- **Read-only, through your own app registration.** Switchboard signs in to Microsoft Graph with the device code flow, as a public client with no secret, and asks only for `Mail.Read`, which can't send, delete or change anything. The sign-in is kept in the credential store and renewed on every build. `connect` walks you through the registration and prints only the mailbox's shape: how many messages and conversations, how many you've answered. Run again, it reuses the saved sign-in to check the mailbox without a new code.
- **Handled mail leaves the list on its own.** A conversation where you replied after its last message is done, and so is one you flagged complete. A new message in it opens it again.
- **Sorted by a local model and nothing else.** Each new message goes to Ollama once, with its sender, subject and the 255-character preview Graph provides, fenced off as data. The answer has to fit a fixed JSON schema: reply, a task or nothing to do, whether a model could draft the reply, a category, a one-line summary and any deadline the mail names. Every field is checked, links are stripped from what the model writes, and the result is shown as plain text, so a message written to fool the model can at most mislabel itself. Mail the model says needs nothing stays off the list.
- **Each message is sorted once,** so a build reads up to `max_messages` (200 by default), newest first, and says so when there were more in those days. Only the first build takes a while.
- **Drafts on request, never sent.** Draft a reply on any mail card and the local model writes one from the conversation's own text and a few lines you wrote about how you write. Graph gives each message without the quoted ones under it, so the model reads the thread once, oldest first, and the message being answered always fits. Where the reply needs something only you know, such as times you're free, it leaves a placeholder in brackets. The draft is shown as plain text with a Copy button, links it wasn't given in your notes become [link], and a new message in the conversation clears it. Switchboard can't send mail: its permission is read-only.
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
- An [ElevenLabs](https://elevenlabs.io) API key, only for read aloud
- For the morning brief, which is optional: a personal Microsoft account's Outlook mailbox with your own app registration in Microsoft Entra, and the calendar feed link from Canvas

The app window on Windows is drawn by the WebView2 runtime. Windows 11 includes it and Windows 10 usually has it. Without it, use the browser instead (`.\switchboard serve`).

## Setup on Windows

```powershell
git clone https://github.com/Anders-T-Strand/Switchboard.git
cd Switchboard

# Pull the local models named in config.yaml (or edit config.yaml to match what you have)
ollama pull qwen3.6:35b-a3b
ollama pull qwen3-embedding:0.6b

# Make sure Claude Code works headless on your login
claude -p "say hi"

# Start it. The first run sets up Python and builds the dashboard.
.\switchboard
```

Switchboard opens in its own window. The terminal shows the setup, which takes a while the first time, and closes once the window is up. You can also double-click `switchboard.cmd` in Explorer.

After the first run it just starts. It rebuilds the dashboard on its own when you pull changes.

### The app window

- **Closing the window doesn't stop Switchboard.** It hides to a tray icon and the server keeps running, so `ask` in a terminal still works. Click the tray icon to get the window back, or right-click it for Open, Open in browser, Build the brief now (while the brief is on), Start with Windows and Quit. Quit is what stops the server. Windows 11 puts new tray icons behind the `^` arrow next to the clock, and you can drag this one out of there.
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

The other commands are `brief` and `speech`, described in their own sections, and `sync`, which reads new Claude Code session logs once and prints a summary. From the repo folder, `.\switchboard` runs any of them as well, such as `.\switchboard brief status`.

## Configuration

Everything lives in `config.yaml`. Restart Switchboard after editing it. With the app window that means Quit in the tray menu first, since closing the window leaves it running.

Settings of your own that shouldn't be published go in `config.local.yaml`, next to it. That file has the same shape, overrides whatever it names, and git ignores it.

A setting Switchboard doesn't know, in either file, stops it at startup instead of being ignored. The message names the file and the setting, and says where a setting belongs when it is only in the wrong place, such as a `mail:` section at the top level instead of under `brief:`.

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
| `ollama` | Where Ollama answers (`base_url`), how long a call may take (`timeout_s`) and how long a model stays in VRAM after a request (`keep_alive`, 30 minutes). |
| `claude` | CLI path, timeout, tools to block, `use_subscription`, which strips `ANTHROPIC_API_KEY` from the `claude -p` environment so routed calls use your plan instead of API billing, and `skip_project_context`, which keeps this repo's `CLAUDE.md` files and Claude Code's auto memory out of routed calls. |
| `pricing` | Per-million-token prices used for the API-rate estimates. |
| `brief` | `enabled` turns the morning brief on. It is off by default, and `config.local.yaml` is the place to turn it on. `build_after` is the local time it is built from, in quotes, such as `"06:30"`. `coursework` sets how far ahead an assignment is listed (`lookahead_days`) and how long after its due date (`overdue_days`). `mail` takes your app registration's `client_id`, the sign-in `tenant` (`consumers`, for a personal Microsoft account), how far back to look (`lookback_days`), how many messages to read (`max_messages`) and a few lines on how you write, for drafts (`writing_notes`). |
| `speech` | Read aloud with ElevenLabs. `enabled` turns it on. It is off by default, and `config.local.yaml` is the place to turn it on. `voice_id` and `model_id` pick the voice and the model, George and `eleven_flash_v2_5` by default. `max_chars` is where a long answer is cut, and `cache: false` stops keeping finished clips. |
| `server` | `host` is 127.0.0.1. Set it to your Tailscale IP or `0.0.0.0` to open the dashboard from other devices. There is no login, so only expose it on a private network. `port` is 8787, and `data_dir` holds the database, the log and the saved clips. |

To send moderate coding work to Claude as well, uncomment the `code` rule in `config.yaml`. If Opus isn't available on your plan in Claude Code, complexity 5 prompts fall back to Sonnet on their own, or point the `expert` rule at `claude-sonnet`.

### A note on Claude usage limits

Routed Claude calls count against the same usage limits as your normal Claude and Claude Code use. `claude -p` is meant for scripting your own work, so this is a personal tool, not a backend to share with other people.

## Development

```powershell
# Backend lint and tests (fake Ollama, fake claude CLI, fake ElevenLabs and in-process Qdrant, no network needed)
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
  pricing.py       cost at API rates, and what answering locally kept off Claude
  db.py            SQLite schema and migrations
  config.py        config.yaml and config.local.yaml: models, validation, and errors that name the file and the setting
  main.py          FastAPI app, background threads, serves the built dashboard at /
  __main__.py      the command line: serve, app, ask, brief, speech and sync
  desktop.py       the app window: server thread, tray icon and its menu, notifications, one copy per port, log file
  startup.py       Start with Windows: a shortcut in the Startup folder that opens the app in the tray
  brief/           the morning brief: its sources (Outlook through Microsoft Graph, a Canvas calendar feed), mail triage and drafts by a local model, its own tables, the build and the brief command
  credentials.py   secrets in the operating system's credential store, through keyring, in parts when they are long
  speech/          read aloud: Markdown into sentences, the ElevenLabs stream, saved clips, the play log and the speech command
backend/tests/     pytest suite
frontend/src/      React + TypeScript dashboard: the Today, Overview, Console, Requests, Tasks and Models pages
```

## Ideas for later

- Train a router on the rated turns (the Qdrant vectors make good features) and compare it against the classifier and the rating-based ladder
- Stream answers as they're generated
- Re-run a turn on a different model to compare answers
- Give every model, local ones included, a short system prompt about you and your setup, kept in `config.local.yaml`
- Tag a whole chat with a task, and count Claude Code usage toward tasks by project folder
