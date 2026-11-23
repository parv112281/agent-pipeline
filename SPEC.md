# Agent pipeline: specification

Status: built 2026-10-07 (see section 19 for how the build differs from the text below and what has been tested live). Draft 2 replaced OpenProject with agent-task-board after a trial (section 15). Draft 3 added bug fixes and feature requests on existing code (section 18).

## 1. Purpose

Move personal projects from idea to merged code with AI agents doing most of the work and Parv acting as reviewer. Work can enter at any of three points: a vague idea, a fleshed-out idea, or finished requirements and design documents. Parv's review of pull requests is the only approval step; agents never merge.

The same pipeline handles work after the first implementation: bug fixes and feature requests, both on projects it created and on existing codebases it did not (section 18).

## 2. Components

| Component | Role | Where it runs |
|---|---|---|
| agent-task-board ("the board") | Task queue, agent runner, PR creation, merge detection, cost log, web UI | This machine, three systemd user services |
| n8n | Pipeline logic around the board: stages, ordering, rework from PR comments, WhatsApp | Docker (existing) |
| Claude Code, headless (`claude -p`) | The agents; started by the board's dispatcher | This machine, on the Pro plan login |
| host-tools | How n8n runs the few host commands it needs (create repo, clone) and reads/writes pipeline state | Existing systemd user service |
| GitHub (personal account `parv112281`) | Repositories and pull requests, through the `gh` CLI | github.com |
| WhatsApp agent | Intake and all notifications to Parv | Existing n8n workflow |

The board is our own copy of `davidcjw/agent-task-board`, pinned at commit `1eb7603` (2026-07-06, the last upstream commit) with the small changes listed in section 13. Upstream is effectively unmaintained, so this is code we own.

Not used from the board: its Telegram bot, its "scout", its macOS service installer, and its pre-PR review gate (it multiplies cost).

## 3. What the board does and what n8n does

**The board, as trialled:**

- Holds tasks in four columns: Queued, Running, Review, Done.
- Dispatcher claims the oldest queued task, creates an isolated git worktree on branch `atb/<task id>`, runs Claude Code headless there, then itself commits, pushes and opens the PR. The agent only edits files.
- Records cost, model and session ID per run.
- Rework: a task sent back with a note is run again on the same branch and the same PR gets a new commit.
- Merge watcher: when a task's PR is merged, moves the task to Done, pulls the local checkout and deletes the remote branch.
- Times out a run after 20 minutes, retries once, then leaves it in Review as an error.

**n8n, to be built:**

- Creates the task for each stage and decides when the next one may be queued.
- Keeps each project's plan and progress (section 5).
- Turns PR comments into rework (section 8).
- Tracks monthly spend and pauses the board (section 11).
- Sends every WhatsApp message (section 10).

## 4. Repositories

- **`ideas`** (one private repo): one folder per idea, `ideas/<slug>/`, holding
  - `raw.md`: the idea as first stated, verbatim.
  - `idea.md`: the fleshed-out idea.
- **One private repo per project**, created when its idea is approved, holding
  - `docs/requirements.md`
  - `docs/design.md`
  - the code.

Local checkouts live under `~/agent-pipeline/repos/<name>`; the board's worktrees under `~/agent-pipeline/worktrees/`. Each checkout sets a git author identity (this machine has no global one).

## 5. State

There is no separate tracker. State lives in three places:

| What | Where |
|---|---|
| Tasks that are queued, running, in review or done | The board |
| A project's plan: the ordered list of build tickets, their dependencies, and which are done | `~/agent-pipeline/state/<project>/plan.json`, written by n8n through host-tools |
| Which projects the pipeline knows about: repo, default branch, build route | `~/agent-pipeline/state/projects.json` |
| Documents and code | The repositories |

Every board task carries tags: `repo:<name>` (required by the board), `stage:<flesh-out|specify|plan|build|onboard>`, `project:<slug>`, and for build tasks `ticket:<n>`.

A build ticket that is not yet eligible exists only in `plan.json`; it appears on the board when n8n queues it. The WhatsApp agent can read `plan.json` to answer "what is left on project X".

**Blocked** has no column. A blocked task sits in Review with a `blocked` tag and the reason in its result, and Parv is notified.

## 6. Stages

Each stage: n8n queues a task → the dispatcher runs an agent → a PR opens → Parv is notified on WhatsApp → Parv merges → the merge watcher moves the task to Done → n8n queues the next stage.

Each stage has its own route in the board's `routes.json`, which fixes the primary model and permissions, and configures a Codex fallback.

| Stage | Route | Primary model | Opens a PR |
|---|---|---|---|
| 1. Flesh out | `flesh-out` | Opus | Yes, in `ideas` |
| 2. Specify | `specify` | Opus | Yes, in the project repo |
| 3. Plan | `plan` | Opus | No |
| 4. Build | `build` | Sonnet | Yes, in the project repo |

### Stage 1: Flesh out

- **Entry**: WhatsApp message, e.g. "new idea: …".
- **n8n**: queues a `flesh-out` task on `ideas` with the raw text in the prompt. The board task is the ticket for the fleshing-out work.
- **Agent**: writes `ideas/<slug>/raw.md` (verbatim) and `idea.md` (problem, who it is for, what it does, what it does not do, open questions, rough size).
- **Gate**: Parv merges the PR. Closing the PR parks the idea.

### Stage 2: Specify

- **Entry**: a merged `idea.md`. Either from stage 1, or Parv commits a fleshed-out `idea.md` to `ideas` and messages "specify <slug>".
- **n8n**: creates the private project repo with a README as its first commit, clones it locally, and queues a `specify` task with the idea document's text in the prompt.
- **Agent**: writes `docs/requirements.md` and `docs/design.md`. One PR contains both.
- **Gate**: Parv merges the PR.

### Stage 3: Plan

- **Entry**: merged requirements and design documents. Either from stage 2, or Parv commits both to a project repo and messages "plan <project>".
- **Agent**: reads the two documents and returns the build tickets as JSON: title, description, acceptance criteria, relevant document sections, and which tickets each depends on. The first ticket is always the project skeleton with a test runner. It changes no files, so there is no PR.
- **n8n**: validates the JSON, writes `plan.json`, sends a WhatsApp summary (ticket count and titles) for information, and queues the first eligible build ticket.
- **Gate**: none.

### Stage 4: Build

- **Entry**: n8n queues a build ticket when every ticket it depends on is merged and no other task for that project is queued, running or in review.
- **Agent**: implements the ticket and makes the tests pass.
- **Tests**: the agent must run the tests and report the result. If it cannot get them passing it ends its result with a line `PIPELINE_BLOCKED: <reason>`. The board still opens a PR when files changed; n8n then tags the task `blocked` and the WhatsApp message says the PR has failing tests.
- **Gate**: Parv merges the PR. n8n marks the ticket done in `plan.json` and queues the next eligible one.

Because n8n feeds build tickets one at a time, a ticket never starts on code that has not merged.

## 7. Triggers

n8n polls every 2 minutes; no new public endpoints.

- **Board** (`GET /api/board`): tasks newly in Review → notify; tasks newly in Done → advance the pipeline; tasks in Review with an error → mark blocked and notify.
- **GitHub** (through `gh`): PRs of tasks in Review → look for rework requests (section 8).

Concurrency: the dispatcher runs one task at a time across all projects.

## 8. Rework

GitHub does not let you request changes on a PR opened by your own account, so the signal is a comment:

1. Parv leaves review comments on the PR as usual.
2. Parv adds a PR comment starting with `/revise` (optionally followed by a summary).
3. n8n collects the `/revise` comment and all review comments made since the agent's last commit, and sends the task back on the board with that text as the correction note.
4. The dispatcher runs the agent on the same branch; the same PR gets a new commit; Parv is notified.

The WhatsApp message "revise <ticket>: <note>" does the same thing without PR comments.

n8n counts rework passes per task. After 3 passes the task is tagged `blocked` instead of being sent back again.

## 9. Agent permissions

Agent runs are unattended, so they do not use the WhatsApp command-approval step.

- No route uses `--dangerously-skip-permissions` (the board's example config does). Each route runs Claude Code with `--permission-mode acceptEdits` and an explicit tool allow-list: Read, Edit, Write, Glob, Grep, and the project's test command. This was the configuration used in the trial.
- The allow-list is per route, not per repo. A project whose test command differs (for example Python) needs its own build route, e.g. `build-python`.
- The agent never runs git. The dispatcher commits and pushes.
- GitHub access is the `gh` login for `parv112281` (scope `repo`). Branch protection on `main` in each project repo requires a PR, so nothing is pushed to `main` directly.
- The board's API requires a bearer token and listens only on the Docker bridge address, like host-tools.
- Agents never merge, never delete repos, never force-push.

## 10. Notifications (WhatsApp)

- PR ready for review: stage, title, PR link.
- Revised PR ready after rework.
- Plan created: number of build tickets and their titles.
- Task blocked: task, reason, what the agent needs.
- Project finished: all build tickets merged.

## 11. Subscription usage

Dollar estimates from headless Claude and Codex are not used as an API-spend budget. There are no per-run `--max-budget-usd` caps, monthly dollar warnings, or dollar-triggered automatic pauses. Old budget configuration values are ignored. A pause previously created by the retired budget engine is cleared on the next tick; manual pauses remain intact. `pipeline budget` remains as a compatibility command reporting that enforcement is disabled.

Provider subscription limits still apply. Each Claude attempt has one Codex fallback for any execution failure. Explicit cancellation stops the task; fallback is never launched after cancellation. Both attempts remain bounded by the configured runner timeout.

## 12. Failure handling

- A Claude startup failure, reported error, nonzero exit, malformed result, or timeout triggers one Codex attempt in the same worktree. Inspect and preserve partial edits; do not resume Claude session IDs in Codex. Each attempt has its own timeout.
- If Codex also fails, the existing Review/error flow reports the failure. Explicit user cancellation stops both providers. Existing timeout auto-requeue remains bounded to one retry.
- Agent finds the task ambiguous: it ends with `PIPELINE_BLOCKED: <question>` and does not guess.
- PR closed without merging: the task is tagged `blocked` and Parv is notified (requires change 1 in section 13; unmodified, the board deletes the task).
- Merge conflict during rework: the board merges `main` into the branch and asks the agent to resolve; if markers remain, the run fails and the task is blocked.
- Machine or service restarts mid-run: the board returns orphaned Running tasks to Queued.
- Plan JSON invalid: n8n marks the plan task blocked; no build tickets are created.

## 13. Changes to our copy of the board

1. Merge watcher: on a closed, unmerged PR, tag the task `blocked` instead of deleting it.
2. Delete the local `atb/<id>` branch after a merge (the trial left it behind).
3. Start with an empty board. A fresh board seeds sample tasks, some queued, which a dispatcher in execute mode would run.
4. systemd user units for the board, dispatcher and merge watcher (upstream only ships a macOS installer).

Known limitation: the board's own test suite needs a newer Node than this machine's 20.18. The board and dispatcher run fine on 20.18.

## 14. Out of scope for the first version

- Agents reviewing each other's PRs.
- Automatic deployment after merge.
- More than one task running at a time.
- Changing requirements or design after a plan exists (handled by hand: edit `plan.json` or re-run the plan stage).
- A tracker with per-project views and ticket history. If that is wanted later, OpenProject can be added as a read-only mirror of `plan.json`.

## 15. Trial record (2026-10-06)

agent-task-board was run against a private throwaway repo with one task and one rework pass.

| Piece | Result |
|---|---|
| Claim a queued task, run Claude Code headless with restricted permissions | Worked |
| Worktree, commit, push, open PR | Worked |
| Cost recorded per run | Worked ($0.09, $0.09, $0.11) |
| Rework pushed to the same PR | Worked; whether the original session was resumed is unconfirmed |
| Merge detected: task to Done, local pull, remote branch deleted | Worked |

Not trialled: Opus routes, the non-PR plan route, timeouts, cost ceilings being hit, merge conflicts, the pause endpoint.

## 16. Decisions on earlier open questions

Settled 2026-10-07; all were the proposed answers.

1. **Budget overflow**: the pipeline pauses at $20. No paid overage.
2. **Stage 1 model**: Opus.
3. **Build ticket size**: at most a change reviewable in about 15 minutes.
4. **Rework signal**: a `/revise` comment on the PR, with the WhatsApp "revise" message as an alternative.
5. **Languages**: the first build routes allow `npm test` and `pytest`.
6. **Change intake**: WhatsApp and GitHub issues labelled `agent`.
7. **Who sizes a request**: Parv, by saying "bug", "small change" or "feature". No triage agent.
8. **Onboarding existing repos**: the onboarding task writes `docs/overview.md` first.

There are no open questions at this point.

## 17. Proposed build order

1. Install our copy of the board with the section 13 changes, empty, as systemd services; write the four routes.
2. Create the `ideas` repo.
3. Stage 4 end to end by hand: queue a build task, review, `/revise`, merge. n8n does notifications and rework only.
4. `plan.json`, ordering, and stage 3.
5. Stage 2, including project repo creation.
6. Stage 1 and WhatsApp intake.
7. Budget tracking and pause.
8. Change requests on pipeline-built projects (section 18): bug and small-change intake first, then the feature path.
9. Onboarding of existing codebases.

## 18. Changes after the first implementation

Bug fixes and feature requests, on projects the pipeline built and on existing codebases.

### Intake

- **WhatsApp**: "bug in <project>: …", "small change to <project>: …", or "feature for <project>: …".
- **GitHub issue** on the project repo with the label `agent`. n8n polls for these. The issue title and body become the request; the PR says `Fixes #<n>` so merging closes the issue.

Requests for a project that is not in `projects.json` are refused with a message saying it needs onboarding first.

### Two paths, chosen by the kind of request

| Kind | Path | Stages used |
|---|---|---|
| Bug, or small change | Direct | One build task |
| Feature | Through the documents | Amend → Plan → Build |

**Direct (bug or small change)**

- n8n adds one ticket to the project's `plan.json` and queues it as a `build` task.
- For a bug, the agent must first add a test that fails because of the bug, then fix it.
- If the documents describe the behaviour being changed, the agent updates them in the same PR.
- If the work turns out to be more than one reviewable change, the agent stops with `PIPELINE_BLOCKED: needs planning` and Parv can resend it as a feature.

**Feature**

- **Amend** (route `specify`, Opus): the agent updates `docs/requirements.md` and `docs/design.md` for the feature, in one PR. Parv merges.
- **Plan** (route `plan`): the agent returns build tickets for the difference only, and n8n appends them to `plan.json`.
- **Build**: as stage 4.

### Ordering against work already in the plan

- Still one task per project at a time.
- A bug ticket goes ahead of all tickets that have not started.
- Small changes and feature tickets go to the end of the plan.
- A task that is running or in review is never interrupted.

### Existing codebases the pipeline did not create

A codebase must be onboarded once before it can receive requests. Parv messages "onboard <repo>".

Requirements:

- The repo is on GitHub under `parv112281`. A local-only repo has to be pushed first, by Parv.
- It has a test command that one of the build routes allows. A repo with no tests gets "add a test runner" as its first ticket.

Onboarding steps, done by n8n:

1. Make a separate clone under `~/agent-pipeline/repos/<name>`. The pipeline never works in Parv's own checkout: the board branches from, and pulls into, whatever checkout it is given.
2. Add the project to `projects.json` with its build route and default branch.
3. Queue an `onboard` task (Opus): the agent reads the code and writes `docs/overview.md` (what it does, how it is laid out, how to run and test it), as a PR. Later tasks get this file in their prompt instead of exploring from scratch.

Onboarded codebases are not required to have `docs/requirements.md` or `docs/design.md`. The first feature request creates them, covering only that feature.

### What this adds to n8n

- Parsing the three request kinds and the GitHub issue poll.
- Inserting tickets into an existing `plan.json` with the ordering above.
- The onboarding flow and `projects.json`.

## 19. As built (2026-10-07)

Differences from the sections above:

- **The pipeline logic is a host program, not n8n nodes.** Everything sections 3 to 18 assign to "n8n" is done by the engine, `~/bin/pipeline` (source in `pipeline/`, tests in `tests/`), so it can be unit tested. n8n does two things: the "Agent pipeline" workflow runs `pipeline tick` every 2 minutes through host-tools and sends each returned notification over WhatsApp, acknowledging it only after WhatsApp accepts it; and the WhatsApp agent maps the intake phrases to `pipeline <command>`.
- **The board listens on 127.0.0.1:3738**, not the Docker bridge address, because only host programs talk to it. The bearer token is in `~/.config/agent-pipeline/token`.
- **Routes are chosen by the task's `agent` field.** There are six: `flesh-out`, `specify`, `onboard`, `plan` (Opus, $2), `build` and `build-python` (Sonnet, $1). `build` also allows `npm install` and `node --test`. There is no default route, and the dispatcher refuses to start if the routes file is missing or contains `--dangerously-skip-permissions`.
- **Git credentials**: the machine has no git credential helper, so the dispatcher, the merge watcher and the engine pass the `gh` helper to git through their environment. `~/.gitconfig` is unchanged.
- **Closed PR**: the watcher tags the task `blocked` and `pr-closed`. The engine re-checks such PRs, so reopening and merging one still advances the pipeline.
- **Issue kinds**: an issue labelled `agent` is a small change unless it also has the label `bug` or `feature`.
- **Feature requests** wait in `plan.json` until the project is idle. Re-running `plan` replaces only tickets that have not started.
- **Rework comments** count only when written by `parv112281`.
- **Branch protection** is attempted when a project repo is created; if GitHub refuses it (private repos on a free plan), the repo is still created and the message says so.

Tested: 91 engine tests and 15 board tests pass. Live on 2026-10-07: stage 1 with Opus ($0.16), PR opened, WhatsApp notification sent and acknowledged through n8n, one rework pass pushed to the same PR ($0.20).

Known limits:

- WhatsApp only delivers free-form messages within 24 hours of Parv's last message to the number. Outside that window a notification may be accepted and then dropped.
- The services run only while Parv is logged in.
- A tick that fails is silent; look at the "Pipeline tick" node output in n8n or run `pipeline tick` by hand.

## 20. Any-stack builds and upstream imports (2026-10-19)

The first build routes allowed only `npm test` and `pytest`, so the pipeline could not build Java, Angular or
infrastructure projects, and agents could not reach open-source code. Section 9's fixed allow-lists stay for the
existing routes; this section adds a third build route and an import command.

### 20.1 The `build-sandbox` route

- **Isolation comes from the operating system, not a command list.** The route runs Claude Code with `Bash` allowed
  and its built-in sandbox (bubblewrap and socat on Linux) set by `--settings` to
  `~/agent-pipeline/board/agent/sandbox-settings.json`:
  - `failIfUnavailable: true` and `allowUnsandboxedCommands: false`: a command never runs outside the sandbox, and
    the run fails if the sandbox cannot start. Because these come from `--settings`, a repository's own
    `.claude/settings.json` cannot loosen them.
  - Writes: the task's worktree (the working directory), the temp directory, and the build caches `~/.m2`,
    `~/.gradle`, `~/.npm`, `~/.cache` and mise's folders. The worktree's git data lives outside it, so an agent
    cannot commit even if it tries; the dispatcher still does all git work.
  - Reads denied: SSH and GPG keys, cloud and Docker credentials, the `gh` login, Claude's credentials, the board's
    `.env` and the pipeline state.
  - Network: only package registries (npm, PyPI, Maven Central, Gradle), GitHub and toolchain download sites
    (including `mise-java.jdx.dev` for JDK metadata and `tuf-repo-cdn.sigstore.dev`, which mise needs to check
    tool signatures such as uv's).
  - No Docker, no Kubernetes cluster, no sudo. Infrastructure files are checked statically (for example `helm
    template` piped to `kubeconform`); deploying stays with Parv.
- **Toolchains come from the project.** Each project pins its tools in `mise.toml` (mise, MIT, installed at
  `~/bin/mise`), so a project can use Node 24 while the services run on Node 20, or JDK 17 next to JDK 21. The agent
  runs `mise install`, then every command through `mise exec --`.
- **Checks come from the project.** A `build-sandbox` project records `checks` in `projects.json`: the commands that
  build and test everything (for example `./mvnw -q verify`). The design names them, the plan answer carries them
  (`"checks": [...]`, required for this route), and every build prompt lists them. Onboarding takes them with
  `onboard <repo> --check '<command>'` (repeatable); repos with `mise.toml`, `pom.xml`, `build.gradle` or
  `angular.json` are guessed as `build-sandbox`.
- **Model and ceiling** are the same as `build`: Sonnet, $1 per run.
- `board-config/` in this repository holds the routes file, the sandbox settings, `install.sh` (backs up and
  installs both, installs mise with a checksum check, and runs the board's routes check) and `smoke-test.sh` (one
  Haiku run, at most $0.15, that shows what the sandbox allows and blocks).

### 20.2 Web access for every route

Every route also allows `WebFetch` and `WebSearch`, so agents can read documentation and open-source code. These
tools only read; the sandbox's network list does not apply to them. Prompts tell agents to prefer existing,
permissively licensed libraries and tools over writing their own, and never to copy code under a licence that is not
permissive.

### 20.3 Specify can choose any stack

The specify prompt offers the three routes and asks the design to name the route; for `build-sandbox` it must also
name the `mise.toml` versions and the check commands. Everything must still be testable by those commands alone,
with no manual steps or outside services.

### 20.4 Importing upstream code

`import <project> <owner>/<repo>[@<ref>]` starts a project from an open-source repository:

- Only permissive licences are accepted (Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, ISC, 0BSD, Unlicense, as
  GitHub reports them). Anything else, or no licence, is refused.
- The engine creates the private project repo if needed (as specify does), downloads the tarball at the resolved
  commit (at most 100 MB and 20,000 files, unpacked with Python's safe `data` filter, `.git` dropped), adds
  `UPSTREAM.md` (source, commit, licence, date), and opens a PR on the branch `import/<owner>-<repo>-<sha7>`. The
  upstream licence and notice files are kept as they are. GitHub Actions workflows (`.github/workflows/`) are left
  out and listed in `UPSTREAM.md`: the `gh` login has no `workflow` scope, so GitHub would refuse the push, and
  widening the scope would also let pushed agent changes add CI. A failed import can be run again; the leftover
  local branch is replaced.
- The engine does the git work, so agents still never run git. Parv merges the PR, then sends `specify <project>`;
  the specify prompt then tells the agent to read the imported code first.

### 20.5 A failed PR step is recovered

When the board pushes the agent's branch but its `gh pr create` fails (a GitHub timeout, for example), the result
ends with `⚠ PR step failed: gh pr create failed`. The engine then opens the PR from the pushed branch `atb/<task id>`
itself (or adopts an open PR already on that branch), records it, and sends the usual "PR ready" note instead of
"Blocked". It tries on up to five ticks. Because the task's result does not name the PR, the engine watches it for
merges itself, as it does after a failed rework. A failed push is not recovered: there is no branch to open.
