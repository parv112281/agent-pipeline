"""Fakes for the three adapters and a fixture that wires an engine to them."""

import copy
import io
import json
import sys
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import cli  # noqa: E402
from pipeline.engine import Engine  # noqa: E402
from pipeline.errors import BoardError, GitHubError  # noqa: E402
from pipeline.store import Store  # noqa: E402

OWNER = "parv112281"


class FakeBoard:
    """Behaves like the board API as read from its source."""

    def __init__(self):
        self.tasks = {}
        self.paused = False
        self.down = False
        self.count = 0
        self.time = 1000
        self.patches = []

    def _check(self):
        if self.down:
            raise BoardError("Board unreachable at http://fake: connection refused")

    def get_board(self):
        self._check()
        columns = {s: [] for s in ("queued", "running", "review", "done")}
        for task in self.tasks.values():
            columns[task["status"]].append(task["id"])
        return {"tasks": copy.deepcopy(self.tasks), "columns": columns, "paused": self.paused}

    @staticmethod
    def _tags(tags):
        seen, out = set(), []
        for raw in tags:
            tag = str(raw).strip().lstrip("#")
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                out.append(tag)
        return out

    def create_task(self, fields):
        self._check()
        self.count += 1
        self.time += 1
        task = {
            "id": f"{self.count:08x}-aaaa-4bbb-8ccc-000000000000",
            "title": fields["title"],
            "prompt": fields["prompt"],
            "agent": fields["agent"],
            "tags": self._tags(fields.get("tags", [])),
            "notes": fields.get("notes", ""),
            "status": fields.get("status", "queued"),
            "createdAt": self.time,
            "updatedAt": self.time,
        }
        self.tasks[task["id"]] = task
        return copy.deepcopy(task)

    def patch_task(self, task_id, patch):
        self._check()
        if task_id not in self.tasks:
            raise BoardError(f"Board PATCH /api/tasks/{task_id} failed: 404 Task not found")
        self.patches.append((task_id, copy.deepcopy(patch)))
        task = self.tasks[task_id]
        self.time += 1
        for key in ("title", "prompt", "agent", "notes", "reviseNote"):
            if key in patch:
                task[key] = patch[key]
        if "tags" in patch:
            task["tags"] = self._tags(patch["tags"])
        if patch.get("status") in ("queued", "running", "review", "done"):
            task["status"] = patch["status"]
        task["updatedAt"] = self.time
        return copy.deepcopy(task)

    def set_paused(self, paused):
        self._check()
        self.paused = bool(paused)

    # what the dispatcher and the merge watcher would do

    def finish(self, task_id, result, status="review", error=False, cost=0.5):
        task = self.tasks[task_id]
        self.time += 1
        task.update(result=result, status=status, error=error, costUsd=cost, updatedAt=self.time)

    def open_pr(self, task_id, url, text="Implemented it. Ran the tests: 4 passed.", cost=0.5, error=False):
        self.finish(task_id, f"{text}\n\nBOARD_PR: {url}", cost=cost, error=error)

    def move(self, task_id, status):
        self.tasks[task_id]["status"] = status

    def find(self, stage=None, project=None, status=None):
        found = []
        for task in self.tasks.values():
            if stage and f"stage:{stage}" not in task["tags"]:
                continue
            if project and f"project:{project}" not in task["tags"]:
                continue
            if status and task["status"] != status:
                continue
            found.append(task)
        return sorted(found, key=lambda t: t["createdAt"])


class FakeGitHub:
    def __init__(self):
        self.repos = {}
        self.prs = {}
        self.issue_comment_list = {}
        self.review_comment_list = {}
        self.review_list = {}
        self.issues = {}
        self.calls = []
        self.fail = None  # callable(method, args) -> bool
        self.protect_fails = False
        self.branch = {}
        self.dirty = set()
        self.upstreams = {}
        self.pushed = {}

    def _call(self, method, *args):
        self.calls.append((method,) + args)
        if self.fail and self.fail(method, args):
            raise GitHubError(f"gh {method} failed: simulated")

    def called(self, method):
        return [c for c in self.calls if c[0] == method]

    def add_repo(self, name, files=None, default_branch="main"):
        self.repos[name] = {"default_branch": default_branch, "files": dict(files or {"README.md": "# " + name})}

    def add_pr(self, url, **fields):
        pr = {"state": "OPEN", "merged": False, "body": "## Summary", "files": [], "last_commit_at": 0}
        pr.update(fields)
        self.prs[url] = pr
        return pr

    def _sync(self, name, dest):
        dest = Path(dest)
        for rel, text in self.repos[name]["files"].items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def repo_info(self, name):
        self._call("repo_info", name)
        if name not in self.repos:
            return None
        return {"default_branch": self.repos[name]["default_branch"]}

    def create_repo(self, name):
        self._call("create_repo", name)
        self.add_repo(name)

    def clone(self, name, dest):
        self._call("clone", name, str(dest))
        (Path(dest) / ".git").mkdir(parents=True)
        self._sync(name, dest)

    def set_identity(self, path, name, email):
        self._call("set_identity", Path(path).name, name, email)

    def protect_branch(self, name, branch):
        self._call("protect_branch", name, branch)
        if self.protect_fails:
            raise GitHubError("gh api -X PUT failed: Upgrade to GitHub Pro or make this repository public (HTTP 403)")

    def checkout_state(self, path):
        name = Path(path).name
        self._call("checkout_state", name)
        return self.branch.get(name, self.repos[name]["default_branch"]), name not in self.dirty

    def pull(self, path):
        name = Path(path).name
        self._call("pull", name)
        self._sync(name, path)

    def pr_view(self, url):
        self._call("pr_view", url)
        if url not in self.prs:
            raise GitHubError(f"gh pr view failed: no such PR {url}")
        return copy.deepcopy(self.prs[url])

    def pr_edit_body(self, url, body):
        self._call("pr_edit_body", url, body)
        self.prs[url]["body"] = body

    def issue_comments(self, repo, number):
        self._call("issue_comments", repo, number)
        return copy.deepcopy(self.issue_comment_list.get((repo, number), []))

    def review_comments(self, repo, number):
        self._call("review_comments", repo, number)
        return copy.deepcopy(self.review_comment_list.get((repo, number), []))

    def reviews(self, repo, number):
        self._call("reviews", repo, number)
        return copy.deepcopy(self.review_list.get((repo, number), []))

    def list_issues(self, repo):
        self._call("list_issues", repo)
        return copy.deepcopy(self.issues.get(repo, []))

    # upstream import
    def add_upstream(self, full, files, license="Apache-2.0", sha="0123456789abcdef0123456789abcdef01234567"):
        self.upstreams[full] = {"files": dict(files), "license": license, "sha": sha}

    def upstream_info(self, full):
        self._call("upstream_info", full)
        up = self.upstreams.get(full)
        return None if up is None else {"license": up["license"], "default_branch": "main"}

    def resolve_commit(self, full, ref):
        self._call("resolve_commit", full, ref)
        return self.upstreams[full]["sha"]

    def download_tarball(self, full, sha, dest_file, max_bytes):
        """A real tarball shaped like GitHub's: one top folder named after the repo and commit."""
        self._call("download_tarball", full, sha)
        top = full.replace("/", "-") + "-" + sha[:7]
        with tarfile.open(dest_file, "w:gz") as archive:
            for rel, text in self.upstreams[full]["files"].items():
                data = text.encode()
                info = tarfile.TarInfo(f"{top}/{rel}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def commit_branch(self, path, branch, message):
        path = Path(path)
        files = {
            str(f.relative_to(path)): f.read_text(encoding="utf-8")
            for f in path.rglob("*") if f.is_file() and ".git" not in f.relative_to(path).parts
        }
        self._call("commit_branch", path.name, branch, message)
        self.pushed[(path.name, branch)] = files

    def pr_for_branch(self, repo, branch):
        self._call("pr_for_branch", repo, branch)
        return next((u for u, pr in self.prs.items() if pr.get("head") == branch and pr["state"] == "OPEN"), None)

    def pr_create(self, repo, branch, title, body):
        self._call("pr_create", repo, branch, title, body)
        number = len(self.prs) + 1
        url = f"https://github.com/{OWNER}/{repo}/pull/{number}"
        self.add_pr(url, body=body, head=branch)
        return url


class Env:
    def __init__(self, tmp_path):
        self.root = tmp_path / "home"
        self.store = Store(self.root)
        self.board = FakeBoard()
        self.gh = FakeGitHub()
        self.now = datetime(2026, 10, 7, 12, 0).timestamp()
        self.history = tmp_path / "history.jsonl"
        self.state = self.root / "state"
        self.state.mkdir(parents=True)
        (self.state / "config.json").write_text(json.dumps({"history_file": str(self.history)}))
        self.gh.add_repo("ideas")
        self.log = []

    def engine(self):
        return Engine(Store(self.root), self.board, self.gh, clock=lambda: self.now, log=self.log.append)

    def run(self, *argv):
        """Run a command through the CLI. Returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv), store=Store(self.root), board=self.board, github=self.gh,
            clock=lambda: self.now, stdout=out, stderr=err,
        )
        return code, out.getvalue().strip(), err.getvalue().strip()

    def ok(self, *argv):
        code, out, err = self.run(*argv)
        assert code == 0, err
        return out

    def tick(self):
        code, out, err = self.run("tick")
        assert code == 0, err
        self.last_err = err
        return json.loads(out)["notifications"]

    def texts(self):
        return [n["text"] for n in self.tick()]

    def fresh(self):
        """Tick, return the new notification texts, and acknowledge them."""
        notes = self.tick()
        if notes:
            self.ok("ack", *[n["id"] for n in notes])
        return [n["text"] for n in notes]

    def read(self, name):
        return json.loads((self.state / name).read_text())

    def plan(self, project):
        return self.read(f"{project}/plan.json")

    def ticket(self, project, number):
        return next(t for t in self.plan(project)["tickets"] if t["n"] == number)

    def spend(self, cost, when=None):
        when = self.now if when is None else when
        with open(self.history, "a") as handle:
            handle.write(json.dumps({"id": "x", "costUsd": cost, "at": int(when * 1000)}) + "\n")

    def pr_url(self, repo, number):
        return f"https://github.com/{OWNER}/{repo}/pull/{number}"

    def open_pr(self, task, number, **kwargs):
        repo = next(t for t in task["tags"] if t.startswith("repo:"))[5:]
        url = self.pr_url(repo, number)
        self.gh.add_pr(url)
        self.board.open_pr(task["id"], url, **kwargs)
        return url

    def merge(self, task, url, files=None):
        """Parv merges the PR; the watcher moves the task to Done."""
        repo = next(t for t in task["tags"] if t.startswith("repo:"))[5:]
        files = files or {}
        self.gh.repos[repo]["files"].update(files)
        self.gh.prs[url].update(state="MERGED", merged=True, files=list(files))
        self.board.move(task["id"], "done")

    def project(self, name, route="build-python", files=None, tickets=None):
        """A known project with a checkout, and optionally a plan."""
        base = {"README.md": "# " + name, "docs/requirements.md": "# R", "docs/design.md": "# D"}
        base.update(files or {})
        self.gh.add_repo(name, base)
        self.gh.clone(name, self.root / "repos" / name)
        projects = self.read("projects.json") if (self.state / "projects.json").exists() else {"projects": {}}
        projects["projects"][name] = {
            "repo": name, "default_branch": "main", "build_route": route, "idea": None,
            "onboarded": True, "seen_issues": [],
        }
        (self.state / "projects.json").write_text(json.dumps(projects))
        if tickets is not None:
            plan = {"project": name, "next_n": len(tickets) + 1, "tickets": []}
            for i, (title, deps, status) in enumerate(tickets, start=1):
                plan["tickets"].append({
                    "n": i, "title": title, "description": f"Do {title}", "acceptance": ["works"],
                    "doc_sections": [], "depends_on": deps, "kind": "build", "status": status,
                    "task_id": None, "pr_url": None, "issue": None,
                })
            (self.state / name).mkdir(exist_ok=True)
            (self.state / name / "plan.json").write_text(json.dumps(plan))
        self.gh.calls.clear()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
