# Microservice Upgrade Agent

A standalone agentic service that upgrades a Java/Spring microservice (default
targets: **Java 21**, **Spring Framework 6**, **Spring Boot 3.5**) given a
path to its code. Unlike a text-completion pattern, this is a genuine
tool-use agent that reads, edits, and builds real files, and the acceptance
test is deterministic (a real build + startup check), not an LLM's opinion of
its own diff.

```
 POST /upgrade {repo_path, targets, use_memory}
        │
        ▼
 LONG-TERM MEMORY (upgrade_memory.py, before the run starts)
 load up to UPGRADE_MEMORY_MAX_PRIOR_RUNS earlier finished runs on the same
 repo from the AgentRun table ──► PriorRunSummary each: targets, approved?,
 cycles used, summary, tail of last failed verification
        │
        ▼
 CONTEXT built for the model
   system prompt   role + target versions + working method   (every call)
   kickoff msg     repo root + "list it, read the build file"
                   + "Memory from previous upgrade attempts" (hints, not facts)
   tools           read_file, write_file, edit_file, list_directory,
                   run_command (mvn/gradle/git/java, allowlisted), report_status
        │
        ▼
 ┌──► model (with tools) works; every assistant turn and tool result is
 │    appended to ONE messages list (working memory, never reset)
 │    reads truncated: files 20k chars, other results 8k, dirs 500 entries
 │    no tool call ──► nudge turn; ≤ max_tool_turns_per_cycle turns
 │         │
 │         ▼
 │    model calls report_status("ready_for_verification" | "blocked")
 │    (turn cap hit ──► treated as ready_for_verification)
 │         │
 │    blocked? ──yes──► stop, report why (no verification attempted)
 │         │no
 │         ▼
 │    agent independently runs a REAL build, then a REAL startup attempt
 │    (verifier.py -- this is the ground truth, not the model's opinion)
 │         │
 │    both passed? ──yes──► done, approved
 │         │no
 │         ▼
 └─── last 4k chars of failure output appended to the SAME messages list
      as a new user turn (up to max_verification_cycles)

 on any exit (approved, blocked, or cycles exhausted):
 AgentRun saved: input, output (+ prior_run_ids shown), full trace
 ──► becomes long-term memory for the next run on this repo
```

The key design choice: **the acceptance test is deterministic, not an LLM
judgment.** After the model believes it's done, the agent runs the build and
startup check itself and only accepts the upgrade when those actually
succeed — a real compile error or a real failed Spring context still counts
as "not done" no matter what the model claims.

## Project layout

```
app/
  agents/
    base.py       BaseAgent / AgentContext / AgentResult contract
    auth.py       Anthropic credential introspection (GET /health/llm)
    registry.py   in-process agent registry (bootstrap_agents())
    upgrade/
      agent.py      MicroserviceUpgradeAgent -- the verification loop
      llm.py        tool-calling LLM client (+ scripted fake for tests)
      prompts.py    system/kickoff/feedback prompt templates
      schemas.py    internal dataclasses (UpgradeInput, trace records, ...)
      tools.py      read_file/write_file/edit_file/list_directory/run_command
                    -- path confinement + command allowlist
      verifier.py   build-tool detection, real build + startup check
  api/
    routes_upgrade.py   POST /upgrade
    routes_agents.py    GET /agents, GET /agent-runs
    deps.py             DbSession dependency
  models/agent_run.py   AgentRun (audit trail for every invocation)
  schemas/              API-facing Pydantic models
  services/upgrade_service.py   orchestrates a run, persists an AgentRun
  services/upgrade_memory.py    long-term memory: summarizes earlier runs
                                against the same repo for the next run
  config.py   settings (env / .env)
  db.py       SQLAlchemy engine/session
  main.py     FastAPI app
tests/
  test_upgrade_agent.py   path confinement, command allowlist, build-tool
                          detection, and the full agent loop (scripted LLM +
                          faked build/startup results)
```

- **Tools** (`app/agents/upgrade/tools.py`): `read_file`, `write_file`,
  `edit_file` (exact-match replace, mirrors Anthropic's text-editor tool
  contract), `list_directory`, `run_command`, and `report_status` (the
  model's only way to end a cycle). All file/command access is **confined to
  the given repo root** — path traversal is rejected the same way as
  Anthropic's own text-editor-tool guidance. `run_command` runs with
  `shell=False` against an **executable allowlist** (`mvn`/`mvnw`,
  `gradle`/`gradlew`, `java`, `javac`, `git`, and basic read-only Unix
  utilities) — no shell interpreters, no `rm`/`curl`/`chmod`/`sudo`, and
  shell operators (`&&`, `|`, `;`, backticks) are never interpreted since the
  command is split into argv and executed directly.
- **Verification** (`app/agents/upgrade/verifier.py`): detects Maven
  (`pom.xml`) vs. Gradle (`build.gradle[.kts]`), prefers the wrapper script
  (`./mvnw`/`./gradlew`) if present, runs the build, then starts the app as a
  real subprocess and tails its output for a Spring Boot startup-success
  marker (`Started <App> in N seconds`) or a failure marker
  (`APPLICATION FAILED TO START`, an uncaught exception, or the process
  exiting on its own before starting), killing the whole process group
  afterward either way.
- **Model/effort**: defaults to `claude-opus-5` at `effort: xhigh` —
  Anthropic's own recommendation for coding/agentic work — configurable via
  `UPGRADE_AGENT_MODEL` / `UPGRADE_AGENT_EFFORT`.
- Full audit trail — every tool call, model note, and verification attempt,
  chronologically ordered — in `AgentRun.trace`.
- **Long-term memory**: summaries of earlier runs against the same repo
  (outcome, and the last build/startup failure if it didn't pass) are shown
  to the model at kickoff, so a retry doesn't repeat what already failed —
  see [Memory across runs](#memory-across-runs).

## Context and memory: how the agent makes decisions

The agent has no vector database. Everything the model knows while it works
comes from four places: the **system prompt**, a single **conversation
history** that grows for the whole run, the **repository on disk**, which
the model reads through its tools, and **summaries of earlier runs** against
the same repo (long-term memory, built from the `AgentRun` audit table). Control-flow
decisions (keep going, verify, stop) are made by the agent's code, never
inferred from the model's free text.

### What goes into the model's context

| Layer | Source | Contents | Lifetime |
|---|---|---|---|
| System prompt | `build_system_prompt()` in [prompts.py](app/agents/upgrade/prompts.py) | Role, the target Java/Spring Framework/Spring Boot versions from the request, and a 6-step working method (inspect first, bump build versions, fix breaking APIs such as `javax.*` → `jakarta.*`, build incrementally, report when ready, report `blocked` if stuck), plus rules: edit surgically, don't restyle, don't skip tests | Resent unchanged on every API call |
| Kickoff message | `build_kickoff_message()` | The repo root path, an instruction to list it and read the build file before anything else, and — if there are any — summaries of earlier runs against this repo (see [Memory across runs](#memory-across-runs)) | First user turn |
| Tool definitions | `TOOL_SCHEMAS` in [tools.py](app/agents/upgrade/tools.py) | `read_file`, `write_file`, `edit_file`, `list_directory`, `run_command`, `report_status` | Every API call |
| Conversation history | `messages` list in [agent.py](app/agents/upgrade/agent.py) | Every assistant turn (text + tool calls), every tool result, verification feedback, and nudges | The whole run, across **all** verification cycles |

The model starts with no knowledge of the codebase. It builds its picture of
the repo by calling `list_directory` and `read_file`, so what it "knows"
about the service is whatever it has read into the conversation so far.

### Working memory within a run

A single `messages` list is created once per run and passed by reference
into every cycle of `_run_inner_loop()`. It is **never reset between
verification cycles**. As a result, when a verification fails in cycle 3,
the model still sees:

- every file it read and every edit it made in cycles 1–2,
- its own earlier reasoning (text blocks),
- the exact build/startup failure output from each earlier failed cycle.

That history lets it avoid repeating a failed fix, connect a new failure to
a change it made earlier, and pick up where it left off rather than
re-inspecting the whole repo. The repository itself acts as durable state
too: edits land on disk immediately, so the model can re-read a file to
check what it actually contains now.

### Feedback the agent injects into the context

The agent adds user turns to steer the model at three points:

1. **Verification failure** (`build_verification_feedback_message()`):
   `"Verification cycle N failed. Details: …"`, followed by the build or
   startup output. This is how ground truth reaches the model: it learns
   what actually broke from the real Maven/Gradle/JVM output, not from its
   own guess.
2. **No tool calls**: if the model replies with text only, the agent adds a
   nudge telling it to call `report_status` if it's finished, or to keep
   using tools. This stops a turn from stalling.
3. **Tool results**: each tool call's output (file contents, command output,
   or an error such as a path-confinement or allowlist rejection) returns
   as a `tool_result` block, with `is_error` set on failures so the model
   can adjust.

### Keeping context bounded

Large outputs are truncated before they enter the conversation, so a single
huge file or build log can't crowd out everything else:

| What | Limit | Where |
|---|---|---|
| `read_file` contents | first 20,000 chars | `MAX_FILE_READ_CHARS`, tools.py |
| Other tool results (`run_command` output, etc.) | first 8,000 chars | `MAX_TOOL_RESULT_CHARS`, tools.py |
| `list_directory` entries | 500 entries | `MAX_LIST_ENTRIES`, tools.py |
| Verification failure output fed back | **last** 4,000 chars | `_LOG_TAIL_CHARS`, agent.py |

Verification logs keep the *tail* rather than the head because Maven,
Gradle and Spring Boot print the decisive error (the compile error summary,
`APPLICATION FAILED TO START`, the root-cause exception) at the end.
Truncated text is labelled (for example
`...[showing last 4000 of 51234 characters]`), so the model knows it is
seeing only part of the output.

Turn and cycle caps bound the total size too: at most
`UPGRADE_MAX_TOOL_TURNS_PER_CYCLE` (default 25) model turns per cycle and
`UPGRADE_MAX_VERIFICATION_CYCLES` (default 5) cycles per run.

### Who decides what

| Decision | Made by | Signal |
|---|---|---|
| Which file to read or edit, which command to run | Model | Its own reasoning over the conversation history |
| "I'm done, verify me" / "I'm stuck" | Model | Structured `report_status` tool call with `status` = `ready_for_verification` or `blocked`. Free text is **not** used for control flow. |
| Whether the upgrade actually works | Agent code ([verifier.py](app/agents/upgrade/verifier.py)) | Real build exit code, then real startup-log markers |
| Retry or stop | Agent code | Verification result + remaining cycles |
| What happens when the turn cap is hit | Agent code | Runs a real verification anyway rather than giving up silently |

The model's text is recorded as `ModelNote`s in the trace for auditing, but
the loop never branches on it. That keeps the model's opinion of its own
work out of the accept/reject decision.

### Memory across runs

The agent has **long-term memory built from its own audit trail**. Every run
is already stored as an `AgentRun` row (input, output, full trace). Before a
new run starts, [upgrade_memory.py](app/services/upgrade_memory.py) looks up
the most recent earlier runs against the **same repository** and condenses
each into a `PriorRunSummary`:

- run id and date,
- the Java / Spring Framework / Spring Boot versions it targeted,
- whether it was approved and how many verification cycles it used,
- its final summary (for example the model's `blocked` reason),
- if it wasn't approved, the tail of its **last failed verification**
  (the real build/startup error it ended on).

These summaries are appended to the kickoff message under *"Memory from
previous upgrade attempts on this repository"*, newest first. The model is
told to treat them as **hints, not facts**: the files on disk are still the
source of truth, since an earlier run may have left partial edits in place.
Their purpose is to stop the agent repeating an approach that already
failed and to send it straight at the problem the last attempt got stuck on.

How runs are selected:

| Rule | Why |
|---|---|
| Same agent, same repo. Paths are normalized (`~`, relative paths, trailing `/` all resolve to one absolute path) | "./svc" and "/abs/svc" are the same repo |
| Only runs with status `succeeded` (the agent finished, approved or not) | Crashed runs (bad credentials, exceptions) say nothing about the repo |
| Newest first, at most `UPGRADE_MEMORY_MAX_PRIOR_RUNS` (default 3) | Keeps the kickoff message small |
| Summary capped at 1,000 chars, failure tail at 1,500 chars per run | Memory can't crowd out the working context |
| The current run is never included | Memory is loaded before the new `AgentRun` row is created |

The memory lives in the service layer, not the agent: the agent only
receives plain dataclasses on `UpgradeInput.prior_runs`, so it has no
database dependency and stays easy to test with scripted inputs.

Each run records which earlier runs it was shown in
`output_payload.prior_run_ids` (visible via `GET /agent-runs/{id}`), so you
can always tell what memory a run started from.

**Turning it off:**

- Per request: `{"repo_path": "...", "use_memory": false}`.
- Globally: `UPGRADE_MEMORY_MAX_PRIOR_RUNS=0`.

Turn it off when an earlier run's lessons no longer apply, for example after
you reset the repo to a different branch, or when you want a clean baseline
run for comparison.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
ant auth login          # recommended -- no API key needed, see below

uvicorn app.main:app --reload
```

Then open `http://localhost:8000/docs` for interactive API docs.

### Anthropic authentication

**No static API key is required.** The agent talks to Claude through a
zero-argument `AsyncAnthropic()` client, which resolves credentials itself,
in order:

1. `ANTHROPIC_API_KEY` env var
2. `ANTHROPIC_AUTH_TOKEN` env var
3. The active [`ant auth login`](https://platform.claude.com/docs/en/api/sdks/cli)
   profile
4. Workload Identity Federation env vars (for service-to-service deployments)

Run `ant auth login` once and the app needs nothing in `.env` at all -- this
is the recommended path for local development, since there's no static
secret to create, store, or rotate. `ANTHROPIC_API_KEY`
(or `ANTHROPIC_AUTH_TOKEN`) in `.env` remains available as a fallback for
environments where an interactive login isn't possible, e.g. CI or a
headless deployment target.

Check which source is actually active two ways:

```bash
ant auth status              # from the shell
curl localhost:8000/health/llm   # from the running app
```

`GET /health/llm` reports *which mechanism* is configured (`api_key`,
`auth_token`, `ant_cli`, `workload_identity_federation`, `mock`, or `none`)
and a human-readable detail string -- never the secret value itself. Note it
has no auth of its own (see [Known simplifications](#known-simplifications)) --
fine for local dev, but restrict or remove it before exposing this API
publicly, since it does disclose whether credentials are configured at all.

## Usage

```bash
curl -X POST localhost:8000/upgrade \
  -H "content-type: application/json" \
  -d '{"repo_path": "/path/to/your/microservice"}'
```

Optional fields: `target_java_version`, `target_spring_framework_version`,
`target_spring_boot_version`, `max_verification_cycles`,
`max_tool_turns_per_cycle`, `build_timeout_seconds`,
`startup_timeout_seconds`, and `use_memory` (default `true`; set `false` to
start without summaries of earlier runs on this repo).

```json
{
  "approved": true,
  "cycles_used": 2,
  "build_tool": "maven",
  "summary": "Build succeeded and the application started successfully.",
  "agent_run_id": 7
}
```

**Before pointing this at a real repo:**
- It executes real shell commands and edits real files — use a **git-tracked
  repo** so every change is reviewable and revertible (`git diff` /
  `git checkout`); this agent doesn't commit or push on your behalf.
- Requires `mvn`/`gradle` (or wrapper scripts) actually installed and on
  `PATH` (or vendored as `./mvnw`/`./gradlew` in the repo).
- `POST /upgrade` blocks for the entire run, which can be many minutes (real
  builds, real JVM startup, possibly several cycles) — see
  [Known simplifications](#known-simplifications) on the lack of a
  background job queue. For a real upgrade run, expect to wait; don't put
  this behind a short HTTP timeout.
- The test suite exercises the tool-execution and verification-*parsing*
  logic (path confinement, command allowlist, build-tool/log-marker
  detection) plus the full agentic loop against a scripted fake model and
  fake build/startup results — it does **not** run a real Maven/Gradle build
  or start a real JVM (no such toolchain in CI here). Do a manual smoke test
  against a real service before trusting this broadly.

## Running tests

```bash
pytest
```

Tests force `LLM_MOCK_MODE=true` (set in `tests/conftest.py`) and use a
scripted/injected fake LLM plus fake `run_build`/`run_startup_check`
(`ScriptedToolCallLLM` in `app/agents/upgrade/llm.py`), so the whole suite
runs offline with no API key, network access, or real subprocess/JVM
execution -- see `tests/test_upgrade_agent.py` (path confinement, command
allowlist, build-tool detection, and the full agent loop: first-try success,
fail-then-retry, model reports "blocked", turn-cap still triggers real
verification), plus long-term memory (kickoff formatting, the agent passing
earlier-run summaries to the model, and the repo-matching / filtering / limit
rules for loading them from the database).

## API overview

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/upgrade` | Run the microservice-upgrade agent against a local repo path |
| `GET` | `/agents` | List registered agents and what they do |
| `GET` | `/agent-runs`, `/agent-runs/{id}` | Audit trail for every agent invocation |
| `GET` | `/health`, `/health/llm` | Liveness / Anthropic credential status |

## Known simplifications

- No authentication/authorization on the API — anyone who can reach it can
  call it. This matters more than usual here: `repo_path` is caller-supplied
  and unrestricted, so an unauthenticated caller can point it at any
  directory the server process can read/write and run allowlisted commands
  there (confinement is relative to whatever `repo_path` they choose, not a
  fixed sandbox). Add auth (e.g. login + JWT bearer tokens) before exposing
  this beyond local dev.
- No background job queue — the agent runs synchronously in the request,
  which can take many minutes (real builds, real JVM startup) and will hold
  the HTTP connection open the whole time — move to a task queue before
  this matters.
- No context compaction — the conversation history only grows within a
  run. Per-result truncation and the turn/cycle caps bound it, but a long
  run on a large codebase (up to 25 turns × 5 cycles) can still approach the
  model's context window. Neither old tool results nor earlier cycles are
  summarized or pruned.
- Long-term memory is per-repo summaries only — it carries forward each
  earlier run's outcome and last failure, not a distilled list of lessons
  (for example, "this repo needs dependency X pinned"), and it never shares
  what one repo taught the agent with a different repo. Repos are matched
  by filesystem path, so a repo moved or cloned elsewhere starts with no
  memory. Matching scans the most recent 200 runs of the agent in Python
  (`repo_path` lives inside JSON, which isn't portably queryable across
  SQLite and Postgres). See [Memory across runs](#memory-across-runs).
- No Alembic migrations — schema changes mean dropping/recreating tables in
  dev, or hand-writing a migration once this matters.
- SQLite by default; swap `DATABASE_URL` for Postgres in production.
