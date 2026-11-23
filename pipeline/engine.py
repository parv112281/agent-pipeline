"""The engine: shared helpers, intake commands and the tick.

All outside effects go through three adapters: board (HTTP), gh (GitHub and
git) and store (state files). Tests replace them with fakes.
"""

import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import plan as plans
from . import prompts
from .errors import BoardError, CheckoutError, GitHubError, PipelineError, PlanError

STAGES = ("flesh-out", "specify", "plan", "build", "onboard")
PR_STAGES = ("flesh-out", "specify", "build", "onboard")
ACTIVE = ("queued", "running", "review")
PR_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
BOARD_PR_RE = re.compile(r"^BOARD_PR:\s*(\S+)\s*$", re.M)
BLOCKED_RE = re.compile(r"^\s*PIPELINE_BLOCKED:[ \t]*(.*)$", re.M)
REVISE_RE = re.compile(r"^\s*/revise\b[ \t]*:?", re.I)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IDEA_FILE_RE = re.compile(r"^ideas/([^/]+)/idea\.md$")
DOCS = ("docs/overview.md", "docs/requirements.md", "docs/design.md")
UPSTREAM_RE = re.compile(r"^([\w.-]+/[\w.-]+)(?:@([\w./-]+))?$")
UPSTREAM_FILE = "UPSTREAM.md"
# Permissive licences only (SPDX ids as GitHub reports them); copyleft code is not imported automatically.
IMPORT_LICENSES = ("Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense")
IMPORT_MAX_BYTES = 100_000_000
IMPORT_MAX_FILES = 20_000
# Files that mark a project the fixed npm/pytest routes cannot build.
SANDBOX_MARKERS = ("mise.toml", ".mise.toml", ".tool-versions", "pom.xml", "build.gradle", "build.gradle.kts", "angular.json")
NOTE_LIMIT = 8000
# The board pushed the agent's branch but its `gh pr create` failed (a GitHub hiccup): the engine opens the PR.
PR_CREATE_FAILED = "⚠ PR step failed: gh pr create failed"
PR_RECOVER_TRIES = 5
TEXT_LIMIT = 900
SENT_KEEP = 20  # acknowledged notifications kept in the outbox, so the chat agent can see what the user was sent


def short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def tag_value(task, key):
    prefix = key + ":"
    for tag in task.get("tags") or []:
        if isinstance(tag, str) and tag.startswith(prefix):
            return tag[len(prefix):].strip()
    return ""


def has_tag(task, name):
    return any(str(t).lower() == name for t in task.get("tags") or [])


def result_pr_url(text):
    """The PR of a task: the board's BOARD_PR line, else the first PR link."""
    text = str(text or "")
    for match in BOARD_PR_RE.finditer(text):
        if PR_RE.fullmatch(match.group(1)):
            return match.group(1)
    match = PR_RE.search(text)
    return match.group(0) if match else None


def blocked_reason(text):
    matches = BLOCKED_RE.findall(str(text or ""))
    if not matches:
        return None
    return matches[-1].strip() or "no reason given"


def fingerprint(task):
    raw = f"{task.get('result') or ''}|{bool(task.get('error'))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def result_tail(text, limit=300):
    lines = [l for l in str(text or "").splitlines() if l.strip() and not l.startswith("BOARD_PR:")]
    tail = " ".join(" ".join(lines).split())
    return tail if len(tail) <= limit else "..." + tail[-(limit - 3):]


def money(value):
    return f"${float(value):.2f}"


def build_rework_note(revise_texts, review_comments, reviews):
    """The correction note sent back with a task."""
    parts = []
    main = "\n\n".join(t.strip() for t in revise_texts if t and t.strip())
    if main:
        parts.append(main)
    lines = []
    for comment in review_comments:
        where = comment.get("path") or "(no file)"
        if comment.get("line"):
            where += f" line {comment['line']}"
        lines.append(f"- {where}: {comment.get('body', '').strip()}")
    for review in reviews:
        lines.append(f"- Review: {review.get('body', '').strip()}")
    if lines:
        parts.append("Review comments on the pull request:\n" + "\n".join(lines))
    note = "\n\n".join(parts) or "Address the review feedback on the pull request."
    return note[:NOTE_LIMIT]


def extract_upstream(tarball, dest):
    """Unpack a GitHub tarball into dest, dropping its top folder, any .git folder and GitHub Actions
    workflows (the pipeline's gh login has no `workflow` scope, so GitHub would refuse the push).
    Returns (files imported, workflow files left out). Uses tarfile's "data" filter, so no absolute
    paths, links out of dest or device files get through."""
    dest = Path(dest)
    count = 0
    skipped = 0
    with tarfile.open(tarball, "r:gz") as archive, tempfile.TemporaryDirectory(prefix="pipeline-unpack-") as tmp:
        members = []
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if len(parts) < 2 or ".git" in parts[1:]:
                continue
            if parts[1:3] == (".github", "workflows"):
                skipped += member.isfile()
                continue
            if member.isfile():
                count += 1
            members.append(member)
        if count > IMPORT_MAX_FILES:
            raise PipelineError(f"The upstream has {count} files, more than {IMPORT_MAX_FILES}; import it by hand.")
        archive.extractall(tmp, members=members, filter="data")
        top = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(top) != 1:
            raise PipelineError("The upstream tarball does not have the expected single top folder.")
        shutil.copytree(top[0], dest, dirs_exist_ok=True, symlinks=True)
    return count, skipped


class Engine:
    def __init__(self, store, board, github, clock=time.time, log=None):
        self.store = store
        self.board = board
        self.gh = github
        self.clock = clock
        self.log = log or (lambda message: print(message, file=sys.stderr))
        self.cfg = store.load_config()
        self._projects_file = store.load_projects()
        self.projects = self._projects_file["projects"]
        self.state = store.load_tasks()
        self.outbox = store.load_outbox()
        self._plans = {}
        self.tasks = None
        self.paused = False

    # ------------------------------------------------------------------
    # state helpers

    def flush(self):
        self.store.save_projects(self._projects_file)
        self.store.save_tasks(self.state)
        self.store.save_outbox(self.outbox)
        for slug, data in self._plans.items():
            self.store.save_plan(slug, data)

    def plan(self, slug):
        if slug not in self._plans:
            self._plans[slug] = self.store.load_plan(slug)
        return self._plans[slug]

    def has_plan(self, slug):
        return slug in self._plans or self.store.plan_path(slug).exists()

    def notify(self, text):
        self.outbox["seq"] += 1
        item = {"id": f"n-{self.outbox['seq']}", "text": short(text, TEXT_LIMIT), "at": int(self.clock())}
        self.outbox["items"].append(item)
        return item

    def outbox_payload(self):
        """What tick prints: at most 5 notifications, under 6000 characters."""
        items = [{"id": i["id"], "text": i["text"]} for i in self.outbox["items"][:5]]
        while items and len(json.dumps({"notifications": items}, ensure_ascii=False)) >= 6000:
            items.pop()
        return {"notifications": items}

    def rec(self, task):
        record = self.state["tasks"].setdefault(task["id"], {})
        record.setdefault("stage", tag_value(task, "stage"))
        record.setdefault("project", tag_value(task, "project"))
        return record

    # ------------------------------------------------------------------
    # board helpers

    def load_board(self):
        board = self.board.get_board()
        self.tasks = board["tasks"]
        self.paused = bool(board.get("paused"))
        return board

    def pipeline_tasks(self):
        found = [
            t for t in (self.tasks or {}).values()
            if isinstance(t, dict) and tag_value(t, "stage") in STAGES and t.get("archivedAt") is None
        ]
        return sorted(found, key=lambda t: (t.get("createdAt") or 0, t.get("id") or ""))

    def project_tasks(self, slug):
        return [t for t in self.pipeline_tasks() if tag_value(t, "project") == slug]

    def detached(self, slug):
        """A detached project is worked on by hand: the engine leaves it and its tasks alone."""
        return bool(self.projects.get(slug, {}).get("detached"))

    def require_attached(self, slug):
        if self.detached(slug):
            raise PipelineError(f'{slug} is detached from the pipeline. To hand it back: "attach {slug}".')

    def project_active(self, slug):
        """A task of the project is queued, running or in review. A task whose
        PR was closed is parked for good and does not count."""
        return any(t.get("status") in ACTIVE and not has_tag(t, "pr-closed") for t in self.project_tasks(slug))

    def find_active(self, stage, slug, ticket=None):
        for task in self.project_tasks(slug):
            if task.get("status") not in ACTIVE or has_tag(task, "pr-closed"):
                continue
            if tag_value(task, "stage") != stage:
                continue
            if ticket is not None and tag_value(task, "ticket") != str(ticket):
                continue
            return task
        return None

    def create_task(self, title, prompt, agent, tags, notes=""):
        if not any(t.startswith("repo:") and len(t) > 5 for t in tags):
            raise PipelineError("Refusing to create a board task without a repo: tag.")
        if prompt.lstrip().startswith("-"):
            # The plan route passes the prompt as the value of -p.
            prompt = "Task: " + prompt.lstrip()
        fields = {"title": short(title, 100), "prompt": prompt, "agent": agent, "tags": list(tags), "notes": notes}
        task = self.board.create_task(fields)
        if self.tasks is not None:
            self.tasks[task["id"]] = task
        self.rec(task)
        self.flush()
        return task

    def patch_task(self, task_id, patch):
        task = self.board.patch_task(task_id, patch)
        if "status" in patch and task.get("status") != patch["status"]:
            raise BoardError(f"Board did not move task {task_id[:8]} to {patch['status']}")
        if self.tasks is not None:
            self.tasks[task_id] = task
        return task

    def retag(self, task_id, add=(), remove=(), **extra):
        """Change tags from a fresh read (a PATCH replaces the whole list)."""
        fresh = self.board.get_board()["tasks"].get(task_id)
        if not fresh:
            raise BoardError(f"Task {task_id[:8]} is no longer on the board")
        drop = {r.lower() for r in remove}
        tags = [t for t in fresh.get("tags") or [] if str(t).lower() not in drop]
        for tag in add:
            if not any(str(t).lower() == tag.lower() for t in tags):
                tags.append(tag)
        if tags == (fresh.get("tags") or []) and not extra:
            self.tasks[task_id] = fresh
            return fresh
        return self.patch_task(task_id, dict(extra, tags=tags))

    def label(self, task):
        stage = tag_value(task, "stage")
        project = tag_value(task, "project")
        ticket = tag_value(task, "ticket")
        if stage == "flesh-out":
            return "flesh-out"
        name = "amend" if has_tag(task, "amend") and stage == "specify" else stage
        return f"{name}, {project} #{ticket}" if ticket else f"{name}, {project}"

    def ref(self, task):
        ticket = tag_value(task, "ticket")
        if ticket:
            return f"{tag_value(task, 'project')}#{ticket}"
        return task["id"][:8]

    # ------------------------------------------------------------------
    # checkouts

    def repo_path(self, name):
        return self.store.repos_dir / name

    def default_branch(self, name):
        for project in self.projects.values():
            if project.get("repo") == name:
                return project.get("default_branch") or "main"
        return self.state.setdefault("branches", {}).get(name, "main")

    def ensure_checkout(self, name, missing=None):
        """Clone under repos/ when absent, and set the git identity."""
        path = self.repo_path(name)
        if not (path / ".git").exists():
            info = self.gh.repo_info(name)
            if info is None:
                owner = self.cfg["github_owner"]
                raise PipelineError(missing or f"There is no GitHub repo {owner}/{name}.")
            self.gh.clone(name, path)
            self.state.setdefault("branches", {})[name] = info.get("default_branch") or "main"
        self.gh.set_identity(path, self.cfg["git_name"], self.cfg["git_email"])
        return path

    def prepare_checkout(self, name):
        """Before queueing on a repo: default branch, clean, pulled."""
        path = self.repo_path(name)
        if not (path / ".git").exists():
            raise CheckoutError(f"There is no checkout of {name} under {self.store.repos_dir}.")
        want = self.default_branch(name)
        branch, clean = self.gh.checkout_state(path)
        if branch != want:
            raise CheckoutError(f"The checkout of {name} is on branch {branch}, not {want}. Nothing was queued.")
        if not clean:
            raise CheckoutError(f"The checkout of {name} has uncommitted changes. Nothing was queued.")
        self.gh.pull(path)
        return path

    def docs_in(self, name):
        path = self.repo_path(name)
        return [d for d in DOCS if (path / d).is_file()]

    # ------------------------------------------------------------------
    # intake commands

    def cmd_idea(self, text):
        text = text.strip()
        if not text:
            raise PipelineError("Give the idea after the command, for example: idea a tool that sorts my photos")
        self.load_board()
        ideas = self.cfg["ideas_repo"]
        owner = self.cfg["github_owner"]
        path = self.ensure_checkout(
            ideas,
            missing=f"The GitHub repo {owner}/{ideas} does not exist. Create it first: "
            f"gh repo create {ideas} --private --add-readme",
        )
        self.prepare_checkout(ideas)
        taken = set(self.projects)
        folder = path / "ideas"
        if folder.is_dir():
            taken.update(p.name for p in folder.iterdir() if p.is_dir())
        task = self.create_task(
            f"Flesh out idea: {short(text, 60)}",
            prompts.flesh_out(text, taken),
            "flesh-out",
            [f"repo:{ideas}", "stage:flesh-out", "project:ideas"],
        )
        return f"Idea queued for fleshing out (task {task['id'][:8]}). You will get a PR in {ideas} to review."

    def cmd_specify(self, slug):
        self.load_board()
        return self.do_specify(slug.strip().rstrip(":"))

    def do_specify(self, slug):
        """Stage 2 entry. Also run by tick when a flesh-out PR is merged."""
        ideas = self.cfg["ideas_repo"]
        owner = self.cfg["github_owner"]
        if not SLUG_RE.match(slug) or slug == ideas:
            raise PipelineError(f'"{slug}" is not a usable project name (lowercase letters, digits, hyphens).')
        existing = self.find_active("specify", slug)
        if existing:
            return f"A specify task for {slug} is already on the board (task {existing['id'][:8]})."
        ideas_path = self.ensure_checkout(
            ideas,
            missing=f"The GitHub repo {owner}/{ideas} does not exist. Create it first: "
            f"gh repo create {ideas} --private --add-readme",
        )
        self.prepare_checkout(ideas)
        idea_file = ideas_path / "ideas" / slug / "idea.md"
        if not idea_file.is_file():
            raise PipelineError(f"ideas/{slug}/idea.md is not on the main branch of {ideas}.")
        idea_text = idea_file.read_text(encoding="utf-8")

        created, warnings = self.ensure_project_repo(slug)
        docs = self.docs_in(slug)
        if "docs/requirements.md" in docs and "docs/design.md" in docs:
            raise PipelineError(
                f"{slug} already has requirements and design documents. "
                f'Send "plan {slug}" or a feature request instead.'
            )
        self.prepare_checkout(slug)
        upstream = self.repo_path(slug) / UPSTREAM_FILE
        task = self.create_task(
            f"Requirements and design for {slug}",
            prompts.specify(slug, idea_text, upstream.is_file()),
            "specify",
            [f"repo:{slug}", "stage:specify", f"project:{slug}"],
        )
        made = f"Created the private repo {owner}/{slug}. " if created else ""
        message = f"{made}Specify task queued for {slug} (task {task['id'][:8]})."
        return " ".join([message] + warnings)

    def ensure_project_repo(self, slug):
        """Create the private project repo when missing, clone it and record the project.
        Returns (created, warnings)."""
        warnings = []
        info = self.gh.repo_info(slug)
        if info is None:
            self.gh.create_repo(slug)
            info = self.gh.repo_info(slug) or {"default_branch": "main"}
            try:
                self.gh.protect_branch(slug, info["default_branch"])
            except GitHubError as err:
                warnings.append(f"Warning: could not protect {info['default_branch']} ({short(err, 140)}).")
            created = True
        else:
            created = False
        self.ensure_checkout(slug)
        if slug not in self.projects:
            self.projects[slug] = {
                "repo": slug,
                "default_branch": info.get("default_branch") or "main",
                "build_route": None,
                "idea": slug,
                "onboarded": False,
                "seen_issues": [],
            }
        self.flush()
        return created, warnings

    def cmd_import(self, slug, spec):
        """Copy an open-source repo into a project as a PR (spec section 20). The engine does the git work,
        so agents still never run git, and only permissively licensed code is accepted."""
        slug = slug.strip().rstrip(":")
        match = UPSTREAM_RE.match(spec.strip())
        if not match:
            raise PipelineError("Give the upstream as owner/repo or owner/repo@ref, for example AxonIQ/hotel-demo@master.")
        full, ref = match.group(1), match.group(2)
        if not SLUG_RE.match(slug) or slug == self.cfg["ideas_repo"]:
            raise PipelineError(f'"{slug}" is not a usable project name (lowercase letters, digits, hyphens).')
        info = self.gh.upstream_info(full)
        if info is None:
            raise PipelineError(f"There is no public GitHub repo {full}.")
        licence = info["license"]
        if licence not in IMPORT_LICENSES:
            found = licence or "no licence"
            raise PipelineError(
                f"{full} has {found}. Only these licences can be imported: {', '.join(IMPORT_LICENSES)}."
            )
        self.load_board()
        if self.project_active(slug):
            raise PipelineError(f"{slug} has a task in progress. Import when it is merged or closed.")
        sha = self.gh.resolve_commit(full, ref or info["default_branch"])
        created, warnings = self.ensure_project_repo(slug)
        path = self.prepare_checkout(slug)
        if (path / UPSTREAM_FILE).exists():
            raise PipelineError(f"{slug} already holds imported code ({UPSTREAM_FILE}). Nothing was imported.")
        with tempfile.TemporaryDirectory(prefix="pipeline-import-") as tmp:
            tarball = Path(tmp) / "upstream.tar.gz"
            self.gh.download_tarball(full, sha, tarball, IMPORT_MAX_BYTES)
            count, skipped = extract_upstream(tarball, path)
        left_out = (
            f"GitHub Actions workflows (`.github/workflows/`, {skipped} files) were left out: the pipeline's GitHub "
            "login cannot push them, and this project is built by the pipeline's agents.\n\n"
            if skipped
            else ""
        )
        when = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        (path / UPSTREAM_FILE).write_text(
            f"# Upstream\n\nThis repository started from an import of open-source code.\n\n"
            f"- Source: https://github.com/{full}\n- Commit: `{sha}`\n- Licence: {licence}\n- Imported: {when}\n\n"
            "The upstream licence and notice files are kept as they were. Changes since the import are this "
            f"project's own.\n\n{left_out}",
            encoding="utf-8",
        )
        owner_repo = full.replace("/", "-").lower()
        branch = f"import/{owner_repo}-{sha[:7]}"
        self.gh.commit_branch(path, branch, f"Import {full}@{sha[:7]} ({licence})")
        body = (
            f"Imports [{full}](https://github.com/{full}) at `{sha}` ({licence}, {count} files) as the starting "
            f"point for `{slug}`. Nothing was changed except adding `{UPSTREAM_FILE}`.\n\n{left_out}"
            f'After merging, run "specify {slug}" so the documents describe the imported code.'
        )
        url = self.gh.pr_create(slug, branch, f"Import {full}@{sha[:7]}", body)
        made = f"Created the private repo {self.cfg['github_owner']}/{slug}. " if created else ""
        return " ".join([f"{made}Import PR for {slug} ({count} files from {full}@{sha[:7]}, {licence}): {url}"] + warnings)

    def cmd_plan(self, slug):
        slug = slug.strip().rstrip(":")
        self.load_board()
        if slug not in self.projects:
            if not SLUG_RE.match(slug) or self.gh.repo_info(slug) is None:
                raise PipelineError(self.unknown_project(slug))
            self.ensure_checkout(slug)
            self.projects[slug] = {
                "repo": slug,
                "default_branch": self.default_branch(slug),
                "build_route": None,
                "idea": None,
                "onboarded": False,
                "seen_issues": [],
            }
            self.flush()
        self.require_attached(slug)
        existing = self.find_active("plan", slug)
        if existing:
            return f"A plan task for {slug} is already on the board (task {existing['id'][:8]})."
        if self.project_active(slug):
            raise PipelineError(f"{slug} has a task in progress. Plan again when it is merged or closed.")
        task = self.queue_plan(slug)
        return f"Plan task queued for {slug} (task {task['id'][:8]}). You will get the ticket list when it is done."

    def queue_plan(self, slug, amend=False, request=None):
        project = self.projects[slug]
        existing = self.find_active("plan", slug)
        if existing:
            return existing
        repo = project["repo"]
        self.prepare_checkout(repo)
        docs = self.docs_in(repo)
        if "docs/requirements.md" not in docs or "docs/design.md" not in docs:
            raise PipelineError(f"{slug} needs docs/requirements.md and docs/design.md on its main branch first.")
        tags = [f"repo:{repo}", "stage:plan", f"project:{slug}"]
        if amend:
            tags.append("amend")
            request = request or {}
            titles = [f"#{t['n']} {t['title']} ({t['status']})" for t in self.plan(slug)["tickets"]]
            prompt = prompts.plan_amend(
                slug, request.get("text", ""), titles, project.get("build_route"), project.get("checks")
            )
            title = f"Plan feature for {slug}: {short(request.get('text', ''), 50)}"
        else:
            prompt = prompts.plan(slug)
            title = f"Plan build tickets for {slug}"
        return self.create_task(title, prompt, "plan", tags, notes=json.dumps(request) if request else "")

    def unknown_project(self, slug):
        return (
            f'The pipeline does not know a project "{slug}". It needs onboarding first: '
            f'send "onboard {slug}".'
        )

    def cmd_request(self, kind, slug, text):
        slug = slug.strip().rstrip(":")
        text = text.strip()
        if kind not in ("bug", "small", "feature"):
            raise PipelineError("The kind of request must be bug, small or feature.")
        if slug not in self.projects:
            raise PipelineError(self.unknown_project(slug))
        if not text:
            raise PipelineError("Describe the request after the project name.")
        self.require_attached(slug)
        message = self.add_request(kind, slug, text)
        self.flush()
        try:
            self.load_board()
            task = self.queue_project(slug)
            if task:
                message += f" Queued now (task {task['id'][:8]})."
            else:
                message += " It starts when the project has no other task in progress."
        except CheckoutError as err:
            message += f" Not queued yet: {err}"
        except (BoardError, GitHubError) as err:
            self.log(f"request: {err}")
            message += " It will be queued on the next poll."
        return message

    def add_request(self, kind, slug, text, title=None, issue=None):
        """Record a request in plan.json (spec section 18). Does not touch the board."""
        project = self.projects[slug]
        plan = self.plan(slug)
        if kind == "feature":
            plan["feature_seq"] = plan.get("feature_seq", 0) + 1
            request = {"id": f"{slug}-f{plan['feature_seq']}", "text": text, "issue": issue}
            plan.setdefault("pending_features", []).append(request)
            plan["finished_notified"] = False
            return f"Feature recorded for {slug}. The documents are amended first (a PR for you), then it is planned."
        if not project.get("build_route"):
            raise PipelineError(
                f"{slug} has no build route yet. Set one with: onboard {project['repo']} --route build "
                "(or build-python, or build-sandbox --check <command>)."
            )
        if not title:
            title = short(text.splitlines()[0], 70)
            if kind == "bug" and not title.lower().startswith(("fix", "bug")):
                title = short("Fix: " + title, 70)
        if kind == "bug":
            acceptance = ["A new test reproduces the bug and passes after the fix.", "All tests pass."]
        else:
            acceptance = ["The change is covered by a test.", "All tests pass."]
        ticket = plans.make_ticket(plan, title, text, acceptance, kind=kind, issue=issue)
        plans.add_direct(plan, ticket)
        place = "ahead of the tickets not yet started" if kind == "bug" else "at the end of the plan"
        return f"Added as ticket #{ticket['n']} of {slug}, {place}."

    def cmd_onboard(self, repo, route=None, checks=None):
        repo = repo.strip().rstrip(":")
        if route not in (None,) + plans.BUILD_ROUTES:
            raise PipelineError("The route must be build, build-python or build-sandbox.")
        if checks:
            checks = plans.validate_checks(checks)
            if route not in (None, plans.SANDBOX_ROUTE):
                raise PipelineError("--check only applies to the build-sandbox route.")
            route = plans.SANDBOX_ROUTE
        owner = self.cfg["github_owner"]
        if not re.match(r"^[\w.-]+$", repo) or repo == self.cfg["ideas_repo"]:
            raise PipelineError(f'"{repo}" cannot be onboarded.')
        self.load_board()
        info = self.gh.repo_info(repo)
        if info is None:
            raise PipelineError(
                f"There is no GitHub repo {owner}/{repo}. A local-only repo has to be pushed to GitHub first."
            )
        path = self.ensure_checkout(repo)
        known = repo in self.projects
        project = self.projects.setdefault(
            repo,
            {
                "repo": repo,
                "default_branch": info.get("default_branch") or "main",
                "build_route": None,
                "idea": None,
                "onboarded": False,
                "seen_issues": [],
            },
        )
        guessed = False
        if route:
            project["build_route"] = route
        elif not project.get("build_route"):
            project["build_route"] = self.guess_route(path)
            guessed = True
        if checks:
            project["checks"] = checks
        self.flush()
        route_now = project["build_route"]
        if route_now is None:
            route_text = (
                "No build route could be guessed from the files; send the command again with "
                "--route build, --route build-python or --check <command> before requesting changes."
            )
        else:
            route_text = f"Build route: {route_now}{' (guessed from the files)' if guessed else ''}."
            if route_now == plans.SANDBOX_ROUTE:
                if project.get("checks"):
                    route_text += " Checks: " + "; ".join(f"`{c}`" for c in project["checks"]) + "."
                else:
                    route_text += (
                        " It has no checks yet: send the command again with --check <command> (repeat for each "
                        "command) before requesting changes."
                    )
        self.prepare_checkout(repo)
        if known and (path / "docs/overview.md").is_file():
            return f"{repo} is already onboarded. {route_text}"
        existing = self.find_active("onboard", repo)
        if existing:
            return f"An onboarding task for {repo} is already on the board (task {existing['id'][:8]}). {route_text}"
        extra = ""
        if route_now and route_now != plans.SANDBOX_ROUTE and not self.has_tests(path, route_now):
            if not self.plan(repo)["tickets"]:
                command = "npm test" if route_now == "build" else "pytest"
                plan = self.plan(repo)
                ticket = plans.make_ticket(
                    plan,
                    "Add a test runner",
                    f"This project has no tests. Set up the test runner so that `{command}` runs and passes, with "
                    "one small test of existing behaviour. Do not change behaviour.",
                    [f"`{command}` runs and passes.", "At least one test checks existing behaviour."],
                    kind="small",
                )
                plans.add_direct(plan, ticket)
                extra = " No tests found, so the first ticket is: add a test runner."
        task = self.create_task(
            f"Onboard {repo}: write docs/overview.md",
            prompts.onboard(repo, route_now, project.get("checks")),
            "onboard",
            [f"repo:{repo}", "stage:onboard", f"project:{repo}"],
        )
        return f"Onboarding {repo}: cloned and queued a task to write docs/overview.md (task {task['id'][:8]}). {route_text}{extra}"

    @staticmethod
    def guess_route(path):
        # Toolchain files first: a Java or Angular project also has a package.json or pom.xml that the
        # fixed routes cannot build.
        if any((path / name).is_file() for name in SANDBOX_MARKERS):
            return plans.SANDBOX_ROUTE
        if (path / "package.json").is_file():
            return "build"
        if any((path / name).is_file() for name in ("pyproject.toml", "setup.py", "requirements.txt", "pytest.ini")):
            return "build-python"
        return None

    @staticmethod
    def has_tests(path, route):
        if route == "build":
            try:
                script = json.loads((path / "package.json").read_text(encoding="utf-8")).get("scripts", {}).get("test", "")
            except (OSError, ValueError, AttributeError):
                return False
            return bool(script) and "no test specified" not in script
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__"}
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in skip:
                        stack.append(entry)
                elif entry.suffix == ".py" and (entry.name.startswith("test_") or entry.name.endswith("_test.py")):
                    return True
        return False

    # ------------------------------------------------------------------
    # rework

    def cmd_revise(self, ref, note):
        ref = ref.strip().rstrip(":")
        note = note.strip()
        if not ref or not note:
            raise PipelineError('Say what to change, for example: revise myproject#3 use a table instead of a list')
        self.load_board()
        task = None
        ticket_ref = re.match(r"^([\w.-]+)#(\d+)$", ref)
        if PR_RE.fullmatch(ref):
            for candidate in self.pipeline_tasks():
                known = self.state["tasks"].get(candidate["id"], {}).get("pr_url")
                if ref.lower() in ((known or "").lower(), (result_pr_url(candidate.get("result")) or "").lower()):
                    task = candidate
            if task is None:
                raise PipelineError("No pipeline task on the board has that PR.")
        elif ticket_ref:
            slug, number = ticket_ref.group(1), int(ticket_ref.group(2))
            if slug not in self.projects:
                raise PipelineError(self.unknown_project(slug))
            self.require_attached(slug)
            ticket = plans.find_ticket(self.plan(slug), number)
            if ticket is None:
                raise PipelineError(f"{slug} has no ticket #{number}.")
            task = self.tasks.get(ticket.get("task_id") or "") or self.find_active("build", slug, number)
            if task is None or has_tag(task, "pr-closed"):
                if ticket["status"] != "blocked":
                    raise PipelineError(f"Ticket #{number} of {slug} is {ticket['status']}; there is nothing to revise.")
                return self.unpark(slug, ticket, note)
        else:
            if len(ref) < 8:
                raise PipelineError("Give at least 8 characters of the task id, a PR link, or <project>#<ticket>.")
            matches = [t for t in self.pipeline_tasks() if t["id"].lower().startswith(ref.lower())]
            if not matches:
                raise PipelineError(f"No pipeline task on the board starts with {ref}.")
            if len(matches) > 1:
                raise PipelineError(f"More than one task starts with {ref}; give more characters.")
            task = matches[0]

        if has_tag(task, "pr-closed"):
            number = tag_value(task, "ticket")
            slug = tag_value(task, "project")
            ticket = plans.find_ticket(self.plan(slug), int(number)) if number.isdigit() and slug in self.projects else None
            if ticket is not None and ticket["status"] == "blocked":
                return self.unpark(slug, ticket, note)
            raise PipelineError("That PR was closed, so the work is parked. Start it again from its intake command.")
        if task.get("status") != "review":
            raise PipelineError(f'"{task["title"]}" is {task.get("status")}; only a task in Review can be revised.')
        rec = self.rec(task)
        url = result_pr_url(task.get("result")) if rec.get("stage") in PR_STAGES else None
        if url:
            rec["pr_url"] = url
        if rec.get("pr_url"):
            sent = self.send_rework(task, rec, note, notify_limit=False)
            self.flush()
            if not sent:
                raise PipelineError(self.limit_text(task, rec))
            return f'Sent back for rework (pass {rec["rework_count"]} of {self.cfg["max_reworks"]}): {task["title"]}.'
        # No PR to update: run the task again from scratch with the note added.
        count = rec.get("rework_count", 0) + 1
        if count > self.cfg["max_reworks"]:
            self.retag(task["id"], add=["blocked"])
            rec["limit_reached"] = True
            self.flush()
            raise PipelineError(self.limit_text(task, rec))
        prompt = f"{task.get('prompt', '')}\n\nNote from the reviewer after an earlier attempt: {note[:NOTE_LIMIT]}"
        self.retag(task["id"], remove=["blocked", "revise"], status="queued", prompt=prompt)
        rec.update(rework_count=count, last_rework_at=self.clock(), handled_review=False, awaiting_rework=False)
        self.flush()
        return f'Queued again with your note (attempt {count + 1}): {task["title"]}.'

    def unpark(self, slug, ticket, note):
        """A parked ticket (closed PR, removed card) goes back to todo with a note."""
        ticket.update(status="todo", task_id=None, pr_url=None)
        ticket["description"] = f"{ticket['description']}\n\nNote from the reviewer after an earlier attempt: {note}"
        self.plan(slug)["finished_notified"] = False
        self.flush()
        message = f"Ticket #{ticket['n']} of {slug} is back in the plan with your note."
        try:
            task = self.queue_project(slug)
            message += f" Queued now (task {task['id'][:8]})." if task else " It starts when the project is free."
        except CheckoutError as err:
            message += f" Not queued yet: {err}"
        except GitHubError as err:
            self.log(f"revise: {err}")
            message += " It will be queued on the next poll."
        return message

    def limit_text(self, task, rec):
        link = f" Merge or close the PR: {rec['pr_url']}" if rec.get("pr_url") else ""
        return (
            f"Rework limit reached ({self.label(task)}): {task['title']} has had "
            f"{self.cfg['max_reworks']} rework passes and is now blocked.{link}"
        )

    def send_rework(self, task, rec, note, comment_id=None, notify_limit=True):
        """Send a task with a PR back to the board. False when the limit is hit."""
        if comment_id:
            rec["last_revise_id"] = max(comment_id, rec.get("last_revise_id", 0))
        count = rec.get("rework_count", 0) + 1
        if count > self.cfg["max_reworks"]:
            self.retag(task["id"], add=["blocked"])
            rec["limit_reached"] = True
            if notify_limit:
                self.notify(self.limit_text(task, rec))
            self.flush()
            return False
        self.retag(task["id"], add=["revise"], remove=["blocked"], status="queued", reviseNote=note[:NOTE_LIMIT])
        rec.update(rework_count=count, last_rework_at=self.clock(), handled_review=False, awaiting_rework=True)
        self.flush()
        return True

    def check_rework(self, task, rec, url):
        """Look for a /revise comment on the PR (spec section 8)."""
        if rec.get("limit_reached"):
            return
        match = PR_RE.fullmatch(url)
        if not match:
            return
        owner, repo, number = match.group(1), match.group(2), int(match.group(3))
        me = self.cfg["github_owner"].lower()
        last_id = rec.get("last_revise_id", 0)
        requests = [
            c for c in self.gh.issue_comments(repo, number)
            if REVISE_RE.match(c.get("body") or "") and c.get("id", 0) > last_id and c.get("user", "").lower() == me
        ]
        if not requests:
            return
        info = self.gh.pr_view(url)
        pushed = info.get("last_commit_at") or 0
        if not last_id:
            # Nothing consumed on record: a request older than the last push was handled before.
            requests = [c for c in requests if c.get("created_at", 0) > pushed]
            if not requests:
                return
        cutoff = max(pushed, rec.get("last_rework_at") or 0)
        comments = [
            c for c in self.gh.review_comments(repo, number)
            if c.get("created_at", 0) > cutoff and c.get("user", "").lower() == me and (c.get("body") or "").strip()
        ]
        reviews = [
            r for r in self.gh.reviews(repo, number)
            if r.get("created_at", 0) > cutoff and r.get("user", "").lower() == me and (r.get("body") or "").strip()
        ]
        requests.sort(key=lambda c: c.get("id", 0))
        texts = [REVISE_RE.sub("", c["body"], count=1) for c in requests]
        note = build_rework_note(texts, sorted(comments, key=lambda c: c.get("id", 0)), reviews)
        self.send_rework(task, rec, note, comment_id=requests[-1]["id"])

    # ------------------------------------------------------------------
    # queueing

    def queue_project(self, slug):
        """Queue the next thing for a project when nothing of it is in progress."""
        project = self.projects[slug]
        if self.project_active(slug):
            return None
        if not self.has_plan(slug):
            return None
        plan = self.plan(slug)
        repo = project["repo"]
        ticket = plans.next_eligible(plan)
        if ticket is not None:
            route = project.get("build_route")
            if not route:
                return None
            self.prepare_checkout(repo)
            task = self.create_task(
                ticket["title"],
                prompts.build(slug, ticket, route, self.docs_in(repo), project.get("checks")),
                route,
                [f"repo:{repo}", "stage:build", f"project:{slug}", f"ticket:{ticket['n']}"],
            )
            ticket.update(status="queued", task_id=task["id"])
            self.flush()
            return task
        pending = plan.get("pending_features") or []
        # A request whose task was created just before a crash is already on the board.
        on_board = " ".join(str(t.get("notes") or "") for t in self.project_tasks(slug))
        while pending and f'"id": "{pending[0]["id"]}"' in on_board:
            pending.pop(0)
        if not pending:
            return None
        request = pending[0]
        self.prepare_checkout(repo)
        docs = self.docs_in(repo)
        has_docs = "docs/requirements.md" in docs and "docs/design.md" in docs
        task = self.create_task(
            f"Amend documents for feature: {short(request['text'], 50)}",
            prompts.amend(slug, request["text"], has_docs, "docs/overview.md" in docs),
            "specify",
            [f"repo:{repo}", "stage:specify", f"project:{slug}", "amend"],
            notes=json.dumps(request),
        )
        pending.pop(0)
        self.flush()
        return task

    # ------------------------------------------------------------------
    # tick

    def tick(self):
        """One poll. Returns what to print."""
        try:
            self.load_board()
        except BoardError as err:
            self.log(f"tick: {err}")
            return self.outbox_payload()
        known = self.state["tasks"]
        for task_id in [i for i in known if i not in self.tasks]:
            del known[task_id]
        for task in self.pipeline_tasks():
            if self.detached(tag_value(task, "project")):
                continue
            self.guard(f"task {task['id'][:8]}", self.process_task, task)
        self.guard("plans", self.sync_plans)
        attached = [slug for slug in self.projects if not self.detached(slug)]
        for slug in attached:
            self.guard(f"issues of {slug}", self.poll_issues, slug)
        for slug in attached:
            self.guard(f"queue of {slug}", self.queue_step, slug)
        self.guard("plans", self.sync_plans)
        self.guard("finished", self.check_finished)
        self.guard("legacy budget pause", self.clear_legacy_budget_pause)
        self.flush()
        return self.outbox_payload()

    def guard(self, what, fn, *args):
        """Run one step; a failure is logged and the rest of the tick goes on."""
        try:
            fn(*args)
        except PipelineError as err:
            self.log(f"tick: {what}: {err}")
        except Exception as err:  # a bug in one step must not stop the others
            self.log(f"tick: {what}: unexpected {type(err).__name__}: {err}")
        finally:
            self.flush()

    def process_task(self, task):
        rec = self.rec(task)
        status = task.get("status")
        if rec["stage"] in PR_STAGES:
            url = result_pr_url(task.get("result"))
            if url:
                rec["pr_url"] = url
        if status == "review":
            self.handle_review(task, rec)
        elif status == "done":
            fp = fingerprint(task)
            if not rec.get("advanced") or rec.get("advanced_fp") != fp:
                self.handle_done(task, rec, fp)
        else:
            rec["handled_review"] = False
            rec["advanced"] = False

    def handle_review(self, task, rec):
        url = rec.get("pr_url")
        if not url and rec["stage"] in PR_STAGES and PR_CREATE_FAILED in (task.get("result") or ""):
            url = self.recover_pr(task, rec)
        in_result = bool(result_pr_url(task.get("result"))) and rec["stage"] in PR_STAGES
        label = self.label(task)
        title = task.get("title", "")

        if has_tag(task, "pr-closed"):
            if not rec.get("closed_notified"):
                link = f" {url}" if url else ""
                self.notify(f"PR closed ({label}): {title}. The work is parked.{link}")
                rec.update(closed_notified=True, handled_review=True, fp=fingerprint(task))
                self.flush()
            if url:
                # The watcher ignores this task now. If the PR was reopened or merged after all, pick it up.
                info = self.gh.pr_view(url)
                if info["merged"]:
                    task = self.retag(task["id"], remove=["pr-closed", "blocked"], status="done")
                    self.handle_merged(task, rec, info, fingerprint(task))
                elif info["state"] == "OPEN":
                    self.retag(task["id"], remove=["pr-closed", "blocked"])
                    rec["closed_notified"] = False
            return

        fp = fingerprint(task)
        if not rec.get("handled_review") or rec.get("fp") != fp:
            link = f" PR: {url}" if url else ""
            hint = f'To send it back with a note: "revise {self.ref(task)}: <note>".'
            reason = blocked_reason(task.get("result"))
            if task.get("error") and rec.get("pr_recovered_fp") != fp:
                self.retag(task["id"], add=["blocked"])
                self.notify(
                    f"Blocked ({label}): {title}. The run failed: {result_tail(task.get('result'))}{link} {hint}"
                )
            elif reason is not None:
                self.retag(task["id"], add=["blocked"])
                if url and rec["stage"] == "build":
                    what = "The PR has failing tests or an open question"
                else:
                    what = "The agent stopped"
                extra = ""
                if "needs planning" in reason.lower():
                    extra = f' Too big for one change; send it again as "feature for {rec["project"]}: ...".'
                self.notify(f"Blocked ({label}): {title}. {what}: {short(reason, 300)}{link}{extra} {hint}")
            elif url:
                self.retag(task["id"], remove=["blocked"])
                head = "Revised PR ready" if rec.get("awaiting_rework") else "PR ready"
                self.notify(f"{head} ({label}): {title}. {url}")
            else:
                self.retag(task["id"], add=["blocked"])
                self.notify(f"Blocked ({label}): {title}. It is in Review without a PR. {hint}")
            rec.update(handled_review=True, fp=fp, awaiting_rework=False)
            self.flush()

        if not url:
            return
        if not in_result:
            # The result no longer names the PR (a failed rework), so the watcher is blind. Check it here.
            info = self.gh.pr_view(url)
            if info["merged"]:
                task = self.patch_task(task["id"], {"status": "done"})
                self.handle_merged(task, rec, info, fingerprint(task))
                return
            if info["state"] == "CLOSED":
                self.retag(task["id"], add=["blocked", "pr-closed"])
                return
        self.ensure_fixes(task, rec, url)
        self.check_rework(task, rec, url)

    def ensure_fixes(self, task, rec, url):
        """A PR for a ticket that came from a GitHub issue says Fixes #n."""
        if rec.get("fixes_done") or rec["stage"] != "build":
            return
        ticket = self.ticket_of(task)
        if ticket is None or not ticket.get("issue"):
            return
        number = ticket["issue"]
        body = self.gh.pr_view(url)["body"]
        if not re.search(rf"\bFixes #{number}\b", body, re.I):
            self.gh.pr_edit_body(url, f"{body.rstrip()}\n\nFixes #{number}\n")
        rec["fixes_done"] = True

    def ticket_of(self, task):
        slug = tag_value(task, "project")
        number = tag_value(task, "ticket")
        if slug not in self.projects or not number.isdigit():
            return None
        ticket = plans.find_ticket(self.plan(slug), int(number))
        if ticket is not None and ticket.get("task_id") not in (None, task["id"]):
            return None
        return ticket

    def recover_pr(self, task, rec):
        """Open the PR the board failed to open; tried again on later ticks. Returns its URL or None."""
        tries = rec.get("pr_recover_tries", 0)
        if tries >= PR_RECOVER_TRIES:
            return None
        rec["pr_recover_tries"] = tries + 1
        repo, branch = tag_value(task, "repo"), f"atb/{task['id']}"
        result = task.get("result") or ""
        summary = result.split(PR_CREATE_FAILED)[0].strip()
        failure = result.split(PR_CREATE_FAILED)[1].strip()
        body = (
            f"{summary[:6000]}\n\n---\nThe board pushed this branch, but its PR step failed "
            f"({short(failure, 200)}), so the pipeline opened the PR.\n\nBoard task: {task['id']}"
        )
        try:
            url = self.gh.pr_for_branch(repo, branch) or self.gh.pr_create(repo, branch, task.get("title") or branch, body)
        except GitHubError as err:
            self.log(f"recover PR {task['id'][:8]} (try {tries + 1} of {PR_RECOVER_TRIES}): {err}")
            return None
        if not url:
            return None
        rec.update(pr_url=url, pr_recovered_fp=fingerprint(task), handled_review=False)
        self.flush()
        return url

    def handle_done(self, task, rec, fp):
        label = self.label(task)
        title = task.get("title", "")
        if rec["stage"] == "plan":
            self.handle_plan_done(task, rec, fp)
            return
        url = rec.get("pr_url")
        if not url:
            self.retag(task["id"], add=["blocked"], status="review")
            rec.update(handled_review=True, fp=fp)
            self.notify(
                f"Blocked ({label}): {title}. The run ended without a PR (no changes, or it was cancelled): "
                f'{result_tail(task.get("result"), 200)} To run it again with a note: "revise {self.ref(task)}: <note>".'
            )
            return
        # Done does not prove a merge: a rework that changes nothing also lands here.
        info = self.gh.pr_view(url)
        if info["merged"]:
            self.handle_merged(task, rec, info, fp)
        elif info["state"] == "CLOSED":
            self.retag(task["id"], add=["blocked", "pr-closed"], status="review")
        else:
            self.patch_task(task["id"], {"status": "review"})
            if not result_pr_url(task.get("result")):
                self.notify(
                    f"Rework changed nothing ({label}): {title}. The PR is unchanged and still open: {url}"
                )
            rec.update(handled_review=True, fp=fp, awaiting_rework=False)

    def handle_merged(self, task, rec, info, fp):
        """A merged PR: advance the pipeline by stage."""
        stage = rec["stage"]
        slug = rec["project"]
        try:
            if stage == "flesh-out":
                slugs = [m.group(1) for m in (IDEA_FILE_RE.match(f) for f in info.get("files") or []) if m]
                if not slugs:
                    self.notify(
                        f"Idea PR merged, but it holds no ideas/<slug>/idea.md: {task.get('title', '')}. "
                        'Send "specify <slug>" yourself when the file is there.'
                    )
                for idea in slugs:
                    try:
                        self.notify(f"Idea {idea} approved. {self.do_specify(idea)}")
                    except (GitHubError, BoardError):
                        raise
                    except PipelineError as err:
                        self.notify(f'Idea {idea} was merged, but specifying could not start: {err}')
            elif stage == "specify":
                amend = has_tag(task, "amend")
                request = None
                if amend:
                    try:
                        request = json.loads(task.get("notes") or "{}")
                    except ValueError:
                        request = {}
                if slug in self.projects:
                    self.queue_plan(slug, amend=amend, request=request)
            elif stage == "build":
                ticket = self.ticket_of(task)
                if ticket is not None:
                    ticket.update(status="done", task_id=task["id"], pr_url=rec.get("pr_url"))
            elif stage == "onboard":
                if slug in self.projects:
                    project = self.projects[slug]
                    project["onboarded"] = True
                    route = project.get("build_route")
                    tail = (
                        f"It can take bug, small change and feature requests now (route {route})."
                        if route
                        else f"Set a build route before requesting changes: onboard {slug} --route build (or build-python)."
                    )
                    self.notify(f"Onboarding of {slug} is merged. {tail}")
        except (GitHubError, BoardError):
            raise
        except PipelineError as err:
            self.notify(f"Merged ({self.label(task)}), but the next step could not start: {err}")
        rec.update(advanced=True, advanced_fp=fp)

    def handle_plan_done(self, task, rec, fp):
        slug = rec["project"]
        title = task.get("title", "")
        amend = has_tag(task, "amend")
        hint = f'To run it again with a note: "revise {task["id"][:8]}: <note>".'
        if slug not in self.projects:
            rec.update(advanced=True, advanced_fp=fp)
            return
        reason = blocked_reason(task.get("result"))
        try:
            if reason is not None:
                raise PlanError(f"the agent stopped: {short(reason, 300)}")
            route, checks, tickets = plans.validate_plan(plans.extract_json(task.get("result")))
        except PlanError as err:
            self.retag(task["id"], add=["blocked"], status="review")
            rec.update(handled_review=True, fp=fp)
            self.notify(f"Blocked (plan, {slug}): {title}. The plan is not usable: {err}. No tickets were created. {hint}")
            return
        project = self.projects[slug]
        plan = self.plan(slug)
        added = plans.add_planned(plan, tickets, "feature" if amend else "build", replace=not amend)
        if amend:
            try:
                issue = json.loads(task.get("notes") or "{}").get("issue")
            except (ValueError, AttributeError):
                issue = None
            if issue:
                added[-1]["issue"] = issue
            if not project.get("build_route"):
                project["build_route"] = route
        else:
            project["build_route"] = route
        if checks and project.get("build_route") == plans.SANDBOX_ROUTE:
            project["checks"] = checks
        rec.update(advanced=True, advanced_fp=fp)
        titles = " ".join(f"{i}. {short(t['title'], 60)}" for i, t in enumerate(added, start=1))
        head = f"Feature planned for {slug}" if amend else f"Plan created for {slug}"
        self.notify(f"{head}: {len(added)} build tickets. {titles}")

    def sync_plans(self):
        """Bring ticket status in plan.json in line with the board."""
        for slug in self.projects:
            if self.detached(slug) or not self.has_plan(slug):
                continue
            for ticket in self.plan(slug)["tickets"]:
                if ticket["status"] == "done":
                    continue
                task_id = ticket.get("task_id")
                if task_id:
                    task = self.tasks.get(task_id)
                    if task is None:
                        if ticket["status"] in ("queued", "review"):
                            ticket["status"] = "blocked"
                            self.notify(
                                f"Ticket #{ticket['n']} of {slug} ({short(ticket['title'], 60)}) is parked: its task "
                                f'is no longer on the board. To put it back: "revise {slug}#{ticket["n"]}: <note>".'
                            )
                        continue
                else:
                    task = self.find_active("build", slug, ticket["n"])
                    if task is None:
                        continue
                    ticket["task_id"] = task["id"]
                rec = self.state["tasks"].get(task["id"], {})
                if rec.get("pr_url"):
                    ticket["pr_url"] = rec["pr_url"]
                status = task.get("status")
                if status in ("queued", "running"):
                    ticket["status"] = "queued"
                elif status == "review":
                    ticket["status"] = "blocked" if has_tag(task, "blocked") else "review"
                elif status == "done" and rec.get("advanced"):
                    ticket["status"] = "done"

    def poll_issues(self, slug):
        """Open GitHub issues labelled agent become requests (spec section 18)."""
        project = self.projects[slug]
        seen = project.setdefault("seen_issues", [])
        for issue in self.gh.list_issues(project["repo"]):
            number = issue.get("number")
            if number is None or number in seen:
                continue
            labels = {l.lower() for l in issue.get("labels") or []}
            kind = "bug" if "bug" in labels else "feature" if "feature" in labels else "small"
            title = short(issue.get("title"), 70) or f"Issue #{number}"
            body = (issue.get("body") or "").strip()
            text = f"{issue.get('title', '').strip()}\n\n{body}".strip()
            text += f"\n\n(From GitHub issue #{number}.)"
            try:
                self.add_request(kind, slug, text, title=title, issue=number)
                self.notify(f"Issue #{number} on {slug} taken as a {kind} request: {title}.")
            except PipelineError as err:
                self.notify(f"Issue #{number} on {slug} was not taken: {err}")
            seen.append(number)
            self.flush()

    def queue_step(self, slug):
        warned = self.state.setdefault("checkout_warned", {})
        try:
            self.queue_project(slug)
        except CheckoutError as err:
            if warned.get(slug) != str(err):
                warned[slug] = str(err)
                self.notify(f"{slug}: {err} Fix the checkout and the next poll will queue the work.")
            return
        warned.pop(slug, None)

    def check_finished(self):
        for slug in self.projects:
            if self.detached(slug) or not self.has_plan(slug):
                continue
            plan = self.plan(slug)
            tickets = plan["tickets"]
            if not tickets or plan.get("finished_notified") or plan.get("pending_features"):
                continue
            if any(t["status"] != "done" for t in tickets) or self.project_active(slug):
                continue
            plan["finished_notified"] = True
            self.notify(f"Project {slug} finished: all {len(tickets)} tickets are merged.")

    # ------------------------------------------------------------------
    # Dollar-based enforcement is retired for subscription-authenticated agents.
    # Keep the old command and stored marker compatible with existing n8n/chat
    # integrations, but never read history or warn/pause on estimated API cost.

    def clear_legacy_budget_pause(self):
        budget = self.state["budget"]
        if budget.get("paused_by_engine"):
            if self.paused:
                self.board.set_paused(False)
                self.paused = False
            budget["paused_by_engine"] = False

    def cmd_budget(self):
        try:
            self.load_board()
            state = "paused" if self.paused else "running"
        except BoardError:
            state = "not reachable"
        return f"Dollar budget enforcement is disabled for headless subscription agents. The board is {state}."

    def cmd_pause(self):
        self.board.set_paused(True)
        self.state["budget"]["paused_by_engine"] = False
        self.flush()
        return "The board is paused. Running tasks finish; queued tasks wait."

    def cmd_resume(self):
        self.board.set_paused(False)
        self.state["budget"]["paused_by_engine"] = False
        self.flush()
        return "The board is running again."

    def cmd_detach(self, slug):
        slug = slug.strip().rstrip(":")
        if slug not in self.projects:
            raise PipelineError(self.unknown_project(slug))
        if self.detached(slug):
            return f"{slug} is already detached."
        self.projects[slug]["detached"] = True
        self.flush()
        return (
            f"{slug} is detached: no more tickets are queued, and its tasks on the board are left as they are "
            f'(archive them on the board if you like). To hand it back: "attach {slug}".'
        )

    def cmd_attach(self, slug):
        slug = slug.strip().rstrip(":")
        if slug not in self.projects:
            raise PipelineError(self.unknown_project(slug))
        if not self.detached(slug):
            return f"{slug} is not detached."
        self.projects[slug].pop("detached", None)
        self.flush()
        return f"{slug} is back in the pipeline. Its next ticket is queued on the next poll."

    def cmd_ack(self, ids):
        wanted = set(ids)
        sent = [i for i in self.outbox["items"] if i["id"] in wanted]
        self.outbox["items"] = [i for i in self.outbox["items"] if i["id"] not in wanted]
        self.outbox["sent"] = (self.outbox["sent"] + sent)[-SENT_KEEP:]
        self.flush()
        return "ok"

    def cmd_notify(self, text):
        if not text.strip():
            raise PipelineError("Missing notification text.")
        item = self.notify(text)
        self.flush()
        return item["id"]

    # ------------------------------------------------------------------
    # status

    def cmd_status(self, slug=None):
        lines = []
        try:
            self.load_board()
            if self.paused:
                lines.append("The board is paused.")
        except BoardError:
            self.tasks = {}
            lines.append("The board is not reachable; showing the plan files only.")
        if slug:
            slug = slug.strip().rstrip(":")
            if slug not in self.projects:
                raise PipelineError(self.unknown_project(slug))
            lines += self.status_lines(slug, detail=True)
        else:
            if not self.projects:
                lines.append("No projects yet.")
            for name in sorted(self.projects):
                lines += self.status_lines(name, detail=False)
            ideas = [t for t in self.project_tasks("ideas") if t.get("status") in ACTIVE and not has_tag(t, "pr-closed")]
            for task in ideas:
                lines.append(f"Idea {self.describe(task)}")
        text = "\n".join(lines)
        return text if len(text) <= 1400 else text[:1380].rstrip() + "\n(cut short)"

    def describe(self, task):
        status = task.get("status")
        if status == "review" and has_tag(task, "blocked"):
            status = "blocked"
        elif status == "review":
            status = "in review"
        number = tag_value(task, "ticket")
        name = f"#{number} " if number else f"{self.label(task).split(',')[0]}: "
        url = self.state["tasks"].get(task["id"], {}).get("pr_url")
        link = f" {url}" if url and status in ("in review", "blocked") else ""
        return f"{status}: {name}{short(task.get('title'), 50)}{link}"

    def status_lines(self, slug, detail):
        project = self.projects[slug]
        tickets = self.plan(slug)["tickets"] if self.has_plan(slug) else []
        done = sum(1 for t in tickets if t["status"] == "done")
        head = f"{slug}: {done} of {len(tickets)} tickets done" if tickets else f"{slug}: no tickets yet"
        if not project.get("build_route"):
            head += ", no build route"
        if self.detached(slug):
            head += ", detached (worked on by hand)"
        lines = [head + "."]
        for task in self.project_tasks(slug):
            if task.get("status") in ACTIVE and not has_tag(task, "pr-closed"):
                lines.append(f"  {self.describe(task)}")
        pending = self.plan(slug).get("pending_features") if self.has_plan(slug) else None
        if pending:
            lines.append(f"  {len(pending)} feature request(s) waiting for the documents to be amended.")
        if detail:
            left = [t for t in tickets if t["status"] != "done"]
            if left:
                lines.append("Left:")
            for ticket in left:
                deps = [d for d in ticket["depends_on"] if (plans.find_ticket(self.plan(slug), d) or {}).get("status") != "done"]
                after = f" (after #{', #'.join(str(d) for d in deps)})" if deps else ""
                lines.append(f"  #{ticket['n']} [{ticket['status']}] {short(ticket['title'], 60)}{after}")
        return lines
