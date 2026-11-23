# Agent pipeline: user guide

The pipeline takes a project from idea to merged code. Agents write the documents and the code; you review
and merge pull requests. Merging is the only approval step, and agents never merge.

## The flow

| Stage | What the agent produces | Where the PR opens | What happens when you merge |
|---|---|---|---|
| 1. Flesh out | `ideas/<slug>/idea.md` | `ideas` repo | A private repo `<slug>` is created and stage 2 starts |
| 2. Specify | `docs/requirements.md`, `docs/design.md` | Project repo | Stage 3 starts |
| 3. Plan | A list of build tickets (no PR) | Nothing to review | The first ticket starts on its own |
| 4. Build | Code and tests for one ticket | Project repo | The next ticket starts; after the last one you are told the project is finished |

One task runs at a time. Within a project, the next ticket never starts until the current PR is merged.
The pipeline checks for changes every 2 minutes, so allow a few minutes after a merge or a comment.

## Talking to it over WhatsApp

Send these to the WhatsApp agent. Wording and capitals do not need to be exact.

| You send | What it does |
|---|---|
| `new idea: <your idea in any words>` | Starts stage 1 |
| `specify <slug>` | Starts stage 2 for an `idea.md` you wrote yourself in `ideas/<slug>/` |
| `plan <project>` | Starts stage 3 for a repo where you wrote both documents yourself |
| `bug in <project>: <what is wrong>` | One fix ticket, ahead of all tickets that have not started |
| `small change to <project>: <what you want>` | One ticket at the end of the plan |
| `feature for <project>: <what you want>` | Documents are updated first (a PR), then planned, then built |
| `revise <project>#<ticket number>: <note>` | Sends a PR back for rework |
| `onboard <repo>` | Takes an existing GitHub repo of yours into the pipeline |
| `onboard <repo> --check '<command>'` | Same, for a Java, Angular or mixed repo: names the commands that build and test it (repeat `--check` for each) |
| `pipeline import <project> <owner>/<repo>[@<ref>]` | Starts a project from an open-source repo (permissive licences only): opens a PR that copies it in; merge it, then `specify <project>` |
| `what is left on <project>` | Tickets done and remaining |
| `pipeline status` | Everything queued, running, in review or blocked |
| `pipeline budget` | Reports that dollar-budget enforcement is disabled |
| `pause the pipeline` / `resume the pipeline` | Stops or restarts new agent runs |
| `pipeline detach <project>` / `pipeline attach <project>` | Takes one project out of the pipeline so you can work on it by hand, or hands it back |

You can run the same things in a terminal: `pipeline idea ...`, `pipeline request bug <project> ...`,
`pipeline status`, and so on. `pipeline` with no arguments lists the commands.

## Reviewing a PR

1. You get a WhatsApp message with the PR link and the cost of the run.
2. Read it on GitHub. Leave review comments on lines or files as you normally would.
3. Then do one of these:
   - **Merge**: the pipeline moves on.
   - **Ask for changes**: add an ordinary PR comment that starts with `/revise`. Text after it is optional.
     Review comments alone do nothing; `/revise` is the signal, and it sends along every review comment you
     made since the agent's last commit. The agent pushes a new commit to the same PR and you are notified.
   - **Close the PR**: the work is parked and you are told. Nothing else in that project that depends on it
     will start. To bring a parked ticket back: `revise <project>#<n>: <note>`.

A PR can be sent back three times. A fourth request marks it blocked instead; fix it by hand or close it.

## Requests from GitHub issues

Open an issue on a project repo and give it the label `agent`. Add the label `bug` or `feature` to choose
the path; with neither it is treated as a small change. The PR says `Fixes #<n>`, so merging closes the issue.

## Existing repos

`onboard <repo>` works for a repo that is on GitHub under `parv112281`. The pipeline makes its own clone
(it never touches your working copy) and opens a PR adding `docs/overview.md`. After that the repo accepts
bug, small change and feature requests. Add `--route build-python` (or say "with the build-python route") if
it cannot guess the language. Node projects are tested with `npm test`, Python projects with `pytest`; other
languages are not supported yet.

## When something is blocked

You get a message saying why. Common cases:

| Message says | What to do |
|---|---|
| The agent could not get tests passing | Open the PR, then `/revise` with a hint, or fix it yourself and merge |
| The agent asked a question (ambiguous ticket) | Answer it with `revise <project>#<n>: <answer>` |
| `needs planning` | The bug or small change was too big; resend it as `feature for <project>: ...` |
| Run failed or hit its cost ceiling | `revise <project>#<n>: try again` or split the work |
| Plan was invalid | `plan <project>` to run the plan stage again |
| Checkout problem | The pipeline's clone under `~/agent-pipeline/repos/` has local changes; clean it |

A blocked task holds its project until you deal with it. Other projects carry on.

## Budget

Agent runs spend from the $20 a month that your Claude plan includes for headless use. Each run is capped
($2 for idea, documents and planning; $1 for a build ticket). You get a warning at $15. At $20 the pipeline
pauses itself and resumes at the start of the next month; queued work is kept. A small project (idea through
five build tickets) has cost about $1.50 to $2.

## Seeing what is going on

- Board: http://127.0.0.1:3738 in a browser on this machine. Columns are Queued, Running, Review, Done.
- `pipeline status` and `pipeline budget` in a terminal.
- Logs: `journalctl --user -u agent-dispatcher -f` (agent runs), `-u agent-merge-watcher` (merges).
- n8n, workflow "Agent pipeline": one execution every 2 minutes; the "Pipeline tick" node shows any error.

## Things to know

- **WhatsApp messages can go missing.** WhatsApp delivers the pipeline's messages only within 24 hours of
  your last message to the number. If you have been quiet for a day, send anything (for example
  `pipeline status`) and check GitHub or the board for PRs you were not told about.
- **It runs only while you are logged in** on this machine. Work resumes at your next login.
- **Agents can only edit files and run the tests.** They cannot run other commands, use git or merge. A
  ticket that needs anything else comes back blocked.
- **Do not edit the pipeline's clones** under `~/agent-pipeline/repos/`. Work in your own checkout and push.
- **Changing the documents after a plan exists**: edit and merge the documents, then `plan <project>`.
  Tickets that have not started are replaced; finished ones are kept.

## If it stops working

```
systemctl --user status agent-board agent-dispatcher agent-merge-watcher host-tools
systemctl --user restart agent-dispatcher     # also puts a task stuck in Running back in the queue
pipeline tick                                  # runs one check by hand and prints pending messages
```

Where things live: the board in `~/agent-pipeline/board`, state in `~/agent-pipeline/state`, the engine and
this guide in `~/repos/personal/agent-pipeline` (`SPEC.md` describes the design, `README.md` the internals).

## Projects in other languages

Specify picks the route. `build` (Node, `npm test`) and `build-python` (`pytest`) are as before. Anything else,
such as Java with Maven, Angular, or a repo mixing Java, Angular and Helm, uses `build-sandbox`:

- The project pins its tools in `mise.toml` and names its check commands in the design; the plan carries them.
- Agents can run any command, but inside a sandbox: they write only in their worktree and the build caches, reach
  only package registries and GitHub, and have no Docker, cluster or sudo. Deployment files are checked statically;
  you deploy.
- To see what the sandbox allows on this machine: `board-config/smoke-test.sh installed` (one Haiku run, at most
  $0.15).

