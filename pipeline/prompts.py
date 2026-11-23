"""Prompts for the agents. The board adds its own "edit files only, no git"
wrapper to every route that opens a pull request, so that is not repeated here."""

BLOCKED_RULE = (
    "If you cannot do this properly, do not guess. Stop and make the very last line of your final "
    "answer `PIPELINE_BLOCKED: <one sentence saying what is missing or what you need decided>`."
)

TEST_COMMANDS = {
    "build": "`npm test` (also allowed: `npm install`, `node --test`)",
    "build-python": "`pytest` (also allowed: `python -m pytest`)",
}

INSPECTION_TEXT = (
    "Read-only inspection inside the current worktree is allowed with `pwd`, `ls`, `cat`, `head`, "
    "`tail`, `wc`, and `rg` (without `--pre` or `--hostname-bin`). Use single simple commands; "
    "no pipelines, redirection, command substitution, or shell control operators. Use the file-edit "
    "tools to change files. Do not run git or other shell commands."
)

ROUTES_TEXT = (
    "`build` (Node; besides read-only file inspection, the agents can run only `npm test`, `npm install` and `node --test`), "
    "`build-python` (besides read-only file inspection, the agents can run only `pytest`), or `build-sandbox` (any toolchain pinned in `mise.toml`, "
    "for example Java with Maven or Gradle, Angular, Node, Python, Helm; the agents run the project's own check "
    "commands, and any other shell command, inside a sandbox)"
)

SANDBOX_TEXT = (
    "Shell commands run in a sandbox: you can write only inside the working directory and the build caches, and "
    "the network reaches only package registries (npm, PyPI, Maven Central, Gradle), GitHub and the toolchain "
    "download sites. There is no Docker, no Kubernetes cluster, no sudo, and no server or database except what "
    "the tests start themselves. Toolchains come from `mise.toml` in the repository root: run `mise install` "
    "first, then run commands through `mise exec -- <command>`. Headless Chrome (`/usr/bin/google-chrome`) only "
    "starts with `--no-sandbox` in here, so browser tests need a launcher that passes it."
)

WEB_TEXT = (
    "You can read documentation and open-source code on the web with WebFetch and WebSearch. Prefer an existing, "
    "permissively licensed library or tool over writing your own; never copy code from a project whose licence "
    "is not permissive, and say where copied code came from."
)


def test_command_text(route, checks=None):
    """How a prompt names the commands that test the project."""
    if route == "build-sandbox" and checks:
        return "each of these check commands, through `mise exec --`: " + "; ".join(f"`{c}`" for c in checks)
    return TEST_COMMANDS.get(route, "`the project test command`")


def _bullets(items):
    return "\n".join(f"- {item}" for item in items)


def flesh_out(raw_text, taken):
    taken_line = ""
    if taken:
        taken_line = " These names are taken, do not use them: " + ", ".join(sorted(taken)) + "."
    return f"""You are turning a raw idea for a personal software project into a short idea document. This repository holds one folder per idea.

The raw idea, exactly as its author wrote it, is between the markers:

<<<RAW_IDEA
{raw_text}
RAW_IDEA>>>

Do this:

1. Choose a slug for the idea: short kebab-case, lowercase letters, digits and hyphens only, at most 30 characters, specific enough to serve as the project and repository name (for example `meal-planner`). It must not be `ideas` and must not match an existing folder under `ideas/`.{taken_line}
2. Write `ideas/<slug>/raw.md` containing the raw idea verbatim: every word as given above, nothing added, corrected or reformatted.
3. Write `ideas/<slug>/idea.md` with a first-level title and exactly these second-level headings, in this order:
   - `## Problem`: what is annoying or missing today, in plain words.
   - `## Who it is for`: who would use it and in what situation.
   - `## What it does`: the behaviour a user would see, as a short list.
   - `## What it does not do`: things deliberately left out, so the scope stays small.
   - `## Open questions`: anything the raw idea leaves undecided. Ask, do not answer.
   - `## Rough size`: small, medium or large, with one sentence of reasoning.

Rules:

- Stay with what the raw idea says or clearly implies. Where you have to assume something, say so under Open questions instead of presenting it as decided.
- Keep it short: someone should be able to review the document in five minutes.
- Create exactly these two files and change nothing else.
- Finish with one sentence naming the slug you chose.

{BLOCKED_RULE}"""


def specify(project, idea_text, has_upstream=False):
    if has_upstream:
        start = (
            "This repository already holds code imported from an open-source project; `UPSTREAM.md` says from "
            "where. Read that code before writing: the documents describe the project as it will be, building on "
            "the imported code, and the design says what is kept, changed and added."
        )
    else:
        start = "This repository is new and holds only a README."
    return f"""You are writing the requirements and the design for the project `{project}`. {start} The approved idea document is between the markers:

<<<IDEA
{idea_text}
IDEA>>>

Write two files, and no code:

1. `docs/requirements.md`: what the software must do.
   - Numbered sections and numbered requirements (for example `2.3`), so later tasks can point at them.
   - Each requirement is one testable statement of behaviour.
   - A section saying what is out of scope.
   - Take the open questions of the idea document into account: settle the ones that have an obvious smallest answer and say which answer you chose; list the rest in a section `Open questions`.
2. `docs/design.md`: how it will be built.
   - Numbered sections: language and libraries, file layout, main modules and what each is responsible for, data and storage, error handling, how it is tested.
   - Prefer the smallest design that meets the requirements. No speculative features.
   - The code will be written by agents on one of three routes: {ROUTES_TEXT}. Choose the stack the project needs and say which route it uses. Prefer `build` or `build-python` when they are enough.
   - For `build-sandbox`, the design must name the toolchain versions for `mise.toml` and the exact check commands that build and test everything, one per line (for example `./mvnw -q verify`, `npx ng test --watch=false --browsers=ChromeHeadlessNoSandbox`, `helm lint charts/app`).
   - Whatever the route, everything must be testable by those commands alone: no manual steps, no outside services, no Docker and no Kubernetes cluster. Deployment files (Dockerfiles, Kubernetes manifests, Helm charts, Jenkinsfiles) are checked statically, for example `helm template` piped to `kubeconform`, never run or applied; say in the design what is checked that way and what is left for the owner to try by hand.
   - {WEB_TEXT}

Rules:

- Both files go into the same change. Do not create or change any other file.
- Write plainly. A reviewer should be able to read both documents in fifteen minutes.

{BLOCKED_RULE}"""


def amend(project, feature_text, has_docs, has_overview):
    if has_docs:
        task = (
            "Update `docs/requirements.md` and `docs/design.md` so they describe this feature. Keep the existing "
            "numbering and wording where it is unaffected; add new numbered requirements and design sections, and "
            "correct any existing statement the feature changes."
        )
    else:
        task = (
            "This project has no requirements or design documents yet. Create `docs/requirements.md` and "
            "`docs/design.md` covering only this feature (not the existing behaviour), with numbered sections and "
            "numbered, testable requirements. The design must fit the code as it is."
        )
    overview = " Read `docs/overview.md` first; it describes the existing code." if has_overview else ""
    return f"""A feature has been requested for the project `{project}`. Before any code is written, the documents must describe it.{overview}

The request, as written by the project owner:

<<<FEATURE
{feature_text}
FEATURE>>>

{task}

Rules:

- Read the relevant existing code before writing, so the design matches what is there.
- Change only these two documents. No code, no other files.
- Keep the feature as small as the request allows. Anything you had to assume goes under an `Open questions` heading in the requirements.

{BLOCKED_RULE}"""


PLAN_ANSWER = """Your final answer must be only one JSON object, with no text before or after it and no code fence, in exactly this shape:

{"build_route": "build" | "build-python" | "build-sandbox", "checks": ["..."], "tickets": [{"title": "...", "description": "...", "acceptance": ["..."], "doc_sections": ["..."], "depends_on": [1]}]}

- `build_route`: `build` for a Node project tested with `npm test`, `build-python` for a Python project tested with `pytest`, `build-sandbox` for anything else, as the design says.
- `checks`: only for `build-sandbox`, and then required: the check commands from the design, one command per entry, exactly as they are run (without `mise exec --`). Every ticket must leave all of them passing. Leave it out for the other routes.
- `title`: short, imperative; it becomes the pull request title.
- `description`: what to build, with enough detail that an agent who sees only this ticket and the documents can do it. Name files and functions where the design names them.
- `acceptance`: checks that can be verified by automated tests. At least one per ticket.
- `doc_sections`: the sections of the documents the agent must read for this ticket, for example `requirements 2.1` or `design 3`.
- `depends_on`: 1-based positions, in this same list, of earlier tickets that must be merged first. Empty when there are none. Do not refer to tickets outside this answer."""


def plan(project):
    return f"""You are planning the build of the project `{project}`. Read `docs/requirements.md` and `docs/design.md` in full (and `docs/overview.md` if it exists), then split the work into build tickets. Do not change any file.

Rules for the tickets:

- The first ticket is always the project skeleton with a working test runner: the layout from the design, the dependency manifest, a `.gitignore` for the language's generated files (for example `__pycache__/`, `.pytest_cache/`, `node_modules/`, `target/`, build output), and one trivial passing test, so that `npm test` (route `build`), `pytest` (route `build-python`) or every check (route `build-sandbox`, which also needs `mise.toml` with the design's toolchain versions) runs and passes. If the repository already holds imported code (`UPSTREAM.md`), the first ticket instead makes the existing code pass the checks unchanged in behaviour.
- Each ticket is at most a change a person can review in about 15 minutes. Split anything larger.
- Each ticket leaves the tests passing and includes its own tests.
- Tickets are built one at a time, in list order, each on top of the merged result of the ones before. Order them so that works.
- Besides read-only file inspection, the build agents can run only `npm test`, `npm install` and `node --test` (route `build`) or `pytest` (route `build-python`). On route `build-sandbox` they can run any command in a sandbox with no Docker, no cluster and no network except package registries and GitHub. No ticket may need a manual step or an outside service.
- Together the tickets must cover every requirement. Do not add work the documents do not ask for.

{PLAN_ANSWER}

If the documents are too incomplete or contradictory to plan from, answer instead with a single line `PIPELINE_BLOCKED: <what is missing>`."""


def plan_amend(project, feature_text, existing_titles, route, checks=None):
    existing = _bullets(existing_titles) if existing_titles else "- (none)"
    route_line = (
        f"This project uses the route `{route}`; give that as `build_route`."
        if route
        else "Choose `build_route` from what the code uses."
    )
    if route == "build-sandbox" and checks:
        route_line += (
            " Its checks are " + "; ".join(f"`{c}`" for c in checks) + ". Give them as `checks`, with any the "
            "feature adds."
        )
    return f"""A feature has been added to the documents of the project `{project}`. Read `docs/requirements.md` and `docs/design.md` in full (and `docs/overview.md` if it exists), look at the existing code, then return build tickets for this feature only. Do not change any file.

The feature request:

<<<FEATURE
{feature_text}
FEATURE>>>

Tickets that already exist for this project (done or waiting); do not repeat their work:

{existing}

Rules for the tickets:

- Cover only the difference this feature makes: what the documents now ask for and the code does not yet do.
- If the project has no working test runner yet, the first ticket adds one with a trivial passing test and a `.gitignore` for generated files if the project has none.
- Each ticket is at most a change a person can review in about 15 minutes. Split anything larger.
- Each ticket leaves the tests passing and includes its own tests.
- Tickets are built one at a time, in list order, after the existing ones.
- Besides read-only file inspection, the build agents can run only `npm test`, `npm install` and `node --test` (route `build`) or `pytest` (route `build-python`); on route `build-sandbox` they can run any command in a sandbox with no Docker, no cluster and no network except package registries and GitHub. No ticket may need a manual step or an outside service. {route_line}

{PLAN_ANSWER}

If the documents are too incomplete or contradictory to plan from, answer instead with a single line `PIPELINE_BLOCKED: <what is missing>`."""


def build(project, ticket, route, docs_present, checks=None):
    kind = ticket.get("kind") or "build"
    lines = [f"You are working on the project `{project}`. Implement this one ticket and nothing beyond it.", ""]
    lines.append(f"Ticket #{ticket['n']}: {ticket['title']}")
    lines.append("")
    lines.append(ticket["description"])
    if ticket.get("acceptance"):
        lines += ["", "Acceptance criteria:", _bullets(ticket["acceptance"])]
    lines += ["", "Before you change anything:"]
    if docs_present:
        lines.append("- Read these documents: " + ", ".join(f"`{d}`" for d in docs_present) + ".")
    else:
        lines.append("- This project has no documents under `docs/`; read the code that the ticket touches.")
    if ticket.get("doc_sections"):
        lines.append("- These sections matter most for this ticket: " + "; ".join(ticket["doc_sections"]) + ".")
    lines.append("- Look at the existing code and tests and follow their layout and style.")
    lines += ["", "How to work:"]
    if kind == "bug":
        lines.append(
            "- This is a bug. First add a test that fails because of the bug, run it and see it fail, then fix "
            "the code so it passes."
        )
    else:
        lines.append("- Add or update tests for what you build.")
    if route == "build-sandbox":
        lines.append(f"- Run {test_command_text(route, checks)}. All of them must pass before you finish.")
        lines.append(f"- {SANDBOX_TEXT}")
        lines.append(f"- {WEB_TEXT}")
    else:
        lines.append(
            f"- Run the tests with {test_command_text(route)}. These are the permitted test/install commands."
        )
        lines.append(f"- {INSPECTION_TEXT}")
    lines.append(
        "- If `docs/requirements.md`, `docs/design.md` or `docs/overview.md` describe the behaviour you are "
        "changing, update them in the same change so they stay true."
    )
    lines.append("- Keep the change small enough to review in about 15 minutes. Do not refactor unrelated code.")
    lines.append(
        "- Every file you leave in the working directory is committed, including what the tests generate. Make "
        "sure `.gitignore` covers generated files (`__pycache__/`, `.pytest_cache/`, `node_modules/`, coverage "
        "and build output); create or extend it if it does not, and delete generated files that are not ignored."
    )
    lines += [
        "",
        "Your final answer: a short summary of what you changed, then each test command you ran and its result "
        "(how many tests passed and failed).",
        "",
        "Stop instead of guessing. Make the very last line of your final answer `PIPELINE_BLOCKED: <reason>` when:",
        "- you cannot get the tests passing (say which test fails and why); leave your work in place;",
        "- the ticket is ambiguous or contradicts the documents (ask the question);",
    ]
    if kind in ("bug", "small"):
        lines.append(
            "- the work turns out to be more than one reviewable change: write exactly "
            "`PIPELINE_BLOCKED: needs planning` and change nothing."
        )
    else:
        lines.append("- the ticket needs a command or tool you cannot run.")
    return "\n".join(lines)


def onboard(repo, route, checks=None):
    if route == "build-sandbox" and checks:
        test_line = f"The pipeline will run {test_command_text(route, checks)}."
    elif route in TEST_COMMANDS:
        test_line = f"The pipeline will run its tests with {TEST_COMMANDS[route]}."
    else:
        test_line = "The pipeline does not know yet how this project is tested; find out and say so clearly."
    return f"""You are documenting the existing codebase `{repo}` so that later agents can work on it without exploring it from scratch. Read the code, then write one file, `docs/overview.md`, with a first-level title and these second-level headings:

- `## What it does`: the purpose of the software and its main features, from the code, not from guesses.
- `## Layout`: the directories and important files, one line each, and how the parts fit together.
- `## How to run it`: commands and configuration it needs.
- `## How to test it`: the test command, where the tests live, and what is not covered. {test_line} If there are no tests, say so in the first sentence of this section.
- `## Conventions`: naming, style and patterns that a change should follow.
- `## Things to know`: traps, unfinished parts, anything surprising.

Rules:

- Describe what is there. Do not propose changes and do not change any code.
- Create only `docs/overview.md`.
- Keep it under about 200 lines; point to files instead of copying code.

{BLOCKED_RULE}"""
