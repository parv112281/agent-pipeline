# Agent pipeline

Takes personal projects from idea to merged code with AI agents doing the work and Parv reviewing pull
requests. `SPEC.md` says what the pipeline does. This repository holds the engine: a Python command-line
program (`~/bin/pipeline`) that drives the task board and GitHub. Where the specification says "n8n does X",
the engine does X; n8n only runs `pipeline tick` every 2 minutes, sends the notifications it returns over
WhatsApp, and passes Parv's commands on.

## Commands

| Command | Does |
|---|---|
| `pipeline tick` | One poll. Prints `{"notifications":[{"id","text"}]}`: every unacknowledged notification, oldest first, at most 5. Safe to run again at any time. |
| `pipeline ack <id>...` | Removes notifications from the outbox and keeps them in its `sent` list (the last 20), so the chat agent can see what was sent. Prints `ok`. |
| `pipeline notify <text>` | Queues a notification for the user, as if the pipeline had sent it. Prints its id. |
| `pipeline idea <text>` | Queues a flesh-out task on the `ideas` repo. |
| `pipeline specify <slug>` | Creates the project repo if needed and queues the requirements and design task for `ideas/<slug>/idea.md`. |
| `pipeline plan <project>` | Queues the plan task. Needs `docs/requirements.md` and `docs/design.md` on the main branch. |
| `pipeline request bug\|small\|feature <project> <text>` | A change to a known project. Bug: one ticket ahead of the tickets not yet started. Small: one ticket at the end. Feature: documents are amended first, then planned. |
| `pipeline revise <ref> <note>` | Sends a task in Review back with a note. `<ref>` is `<project>#<ticket>`, 8 or more characters of the board task id, or a PR link. On a parked ticket it puts the ticket back in the plan. |
| `pipeline onboard <repo> [--route build\|build-python]` | Takes an existing GitHub repo into the pipeline and queues a task that writes `docs/overview.md`. |
| `pipeline status [<project>]` | What is queued, running, in review or blocked, and the tickets left. |
| `pipeline budget` | Compatibility command: reports that dollar-budget enforcement is disabled. |
| `pipeline pause`, `pipeline resume` | Pause or resume the board by hand. |
| `pipeline detach <project>`, `pipeline attach <project>` | Take a project out of the pipeline to work on it by hand: no tickets are queued and its board tasks are left alone; `request`, `revise` and `plan` refuse it. `attach` hands it back. |

Exit code 0 on success; otherwise non-zero with one line on stderr. A rework can also be requested on GitHub:
comment `/revise <summary>` on the PR; review comments made since the agent's last push are sent along.

## State files

Under `~/agent-pipeline/state` (another root can be given with `PIPELINE_HOME`):

- `config.json`: settings, created with defaults (board URL, GitHub owner, rework limit, git identity,
  path of the board's run history).
- `projects.json`: the projects the pipeline knows: repo, default branch, build route, issues already taken.
- `<project>/plan.json`: the ordered tickets of a project with dependencies and status (`todo`, `queued`,
  `review`, `done`, `blocked`), plus feature requests waiting to be amended. List order is priority order. A
  parked ticket can be put back by hand by setting its status to `todo` and its `task_id` to `null`.
- `tasks.json`: what the engine has already handled per board task (so nothing is notified or advanced twice),
  the PR link of each task, rework counts, and legacy budget-pause markers.
- `outbox.json`: notifications not yet acknowledged.
- `.lock`: lock file; a tick and a command never run at the same time.

Checkouts live under `~/agent-pipeline/repos/<name>`. The board token is read from
`~/.config/agent-pipeline/token` and never printed.

## Layout

- `pipeline/engine.py`: intake commands, the tick, rework, and legacy budget-pause migration.
- `pipeline/prompts.py`: the prompts given to the agents.
- `pipeline/plan.py`: reading and checking the plan agent's answer, ticket ordering.
- `pipeline/board.py`, `pipeline/github.py`, `pipeline/store.py`: the three adapters for everything outside
  (board HTTP API, `gh` and `git`, state files). Tests replace the first two with fakes.
- `pipeline/cli.py`: the command line. `~/bin/pipeline` starts it.
- `n8n/`: exports of the two n8n workflows. Values in angle brackets (`<WHATSAPP_CREDENTIAL_ID>`,
  `<PARV_WHATSAPP_NUMBER>` and so on) are placeholders: after importing, pick your own credentials on each
  node and fill in the phone numbers.

Standard library only, Python 3.12. `gh` is called as `~/bin/gh` and git as `/usr/bin/git` (`PIPELINE_GH`,
`PIPELINE_GIT`, `PIPELINE_BOARD_URL` and `PIPELINE_TOKEN_FILE` override the defaults, for tests).

## Tests

```
cd ~/repos/personal/agent-pipeline
/home/parv/anaconda3/bin/python3 -m pytest tests -q
```

The tests use a fake board, a fake GitHub and a temporary state directory. They do not need the board
service and touch nothing on GitHub. One test runs `~/bin/pipeline` as a subprocess against a small fake
board HTTP server and stub `gh` and `git` programs.

## Headless runtime and file inspection

The three user services start through `systemd/pipeline-node`, which explicitly loads nvm and uses the
`agent-pipeline` alias. Install and select that dedicated runtime with `nvm install 24` and
`nvm alias agent-pipeline 24`. This does not change your default nvm alias. Reload systemd and restart
the board, dispatcher, and merge watcher after changing the runtime. Node 24 is needed by the marketplace
project for `node:sqlite` and its test-runner glob. Runtime startup fails with status 78 if the alias is
missing or selects another major version.

Build prompts permit read-only worktree inspection (`pwd`, `ls`, `cat`, `head`, `tail`, `wc`, and `rg`)
as well as their route-specific test/install commands. This matches the Codex command guard; prompts
must not prohibit reading the documents and code they require agents to inspect. The existing filesystem
and network permissions remain in force. Already-created tasks retain their original prompts and need
the same correction before being sent back through `pipeline revise`.

Execution permission profiles also allow exact loopback hosts (`localhost`, `127.0.0.1`, `::1`) for
HTTP acceptance tests. The private-network guard, external-domain allowlist, protected-data denies,
and read-only planning profile remain intact.
