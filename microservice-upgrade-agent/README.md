# Microservice Upgrade Agent

A standalone agentic service that upgrades a Java/Spring microservice (default
targets: **Java 21**, **Spring Framework 6**, **Spring Boot 3.5**) given a
path to its code. Unlike a text-completion pattern, this is a genuine
tool-use agent that reads, edits, and builds real files, and the acceptance
test is deterministic (a real build + startup check), not an LLM's opinion of
its own diff.

Extracted from the `talent-platform` project's microservice-upgrade agent so
it can be run, deployed, and iterated on independently of that app. Shared
plumbing (`BaseAgent`/`AgentContext`/`AgentResult`, the agent registry, the
`AgentRun` audit model, Anthropic credential resolution, DB session
plumbing) was copied rather than imported cross-repo, so this service has no
dependency on `talent-platform`.

```
 model (with tools) works: read_file, edit_file, write_file,
 list_directory, run_command (mvn/gradle/git/java, allowlisted)
        │
        ▼
 model calls report_status("ready_for_verification" | "blocked")
        │
   blocked? ──yes──► stop, report why (no verification attempted)
        │no
        ▼
 agent independently runs a REAL build, then a REAL startup attempt
 (verifier.py -- this is the ground truth, not the model's opinion)
        │
   both passed? ──yes──► done, approved
        │no
        ▼
 failure output fed back as a new turn ──► loop (up to max_verification_cycles)
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
verification).

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
- No Alembic migrations — schema changes mean dropping/recreating tables in
  dev, or hand-writing a migration once this matters.
- SQLite by default; swap `DATABASE_URL` for Postgres in production.
