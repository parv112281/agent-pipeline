"""The real adapters: Board over HTTP, GitHub against stub gh and git
programs, and the installed launcher as a subprocess."""

import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.board import Board
from pipeline.errors import BoardError, GitHubError
from pipeline.github import GitHub

PYTHON = "/home/parv/anaconda3/bin/python3"
LAUNCHER = Path.home() / "bin" / "pipeline"
TOKEN = "test-token-not-the-real-one"

STUB = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
env = {{k: v for k, v in os.environ.items() if k.startswith("GIT_")}}
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({{"prog": os.path.basename(sys.argv[0]), "args": sys.argv[1:], "stdin": stdin, "env": env}}) + "\\n")
line = " ".join(sys.argv[1:])
for rule in json.load(open(os.path.join(here, "rules.json"))):
    if rule["match"] in line:
        sys.stdout.write(rule.get("out", ""))
        sys.stderr.write(rule.get("err", ""))
        sys.exit(rule.get("code", 0))
sys.exit(0)
"""


class Stub:
    def __init__(self, folder):
        self.folder = folder
        folder.mkdir()
        for name in ("gh", "git"):
            path = folder / name
            path.write_text(STUB.format(python=PYTHON))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.rules([])

    def rules(self, rules):
        (self.folder / "rules.json").write_text(json.dumps(rules))

    def calls(self):
        path = self.folder / "calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def github(self):
        return GitHub("parv112281", gh=str(self.folder / "gh"), git=str(self.folder / "git"))


@pytest.fixture
def stub(tmp_path):
    return Stub(tmp_path / "bin")


def test_github_repo_info_and_missing_repo(stub):
    stub.rules([
        {"match": "repos/parv112281/ghost", "err": "gh: Not Found (HTTP 404)\n", "code": 1},
        {"match": "repos/parv112281/broken", "err": "gh: Server Error (HTTP 502)\n", "code": 1},
        {"match": "repos/parv112281/site", "out": json.dumps({"default_branch": "trunk", "private": True})},
    ])
    gh = stub.github()
    assert gh.repo_info("site") == {"default_branch": "trunk"}
    assert gh.repo_info("ghost") is None
    with pytest.raises(GitHubError):
        gh.repo_info("broken")


def test_github_commands_and_git_credentials(stub, tmp_path):
    gh = stub.github()
    stub.rules([{"match": "rev-parse", "out": "main\n"}, {"match": "status --porcelain", "out": " M a.py\n"}])
    gh.create_repo("new")
    gh.clone("new", tmp_path / "repos" / "new")
    gh.set_identity(tmp_path, "Parv", "6843133+parv112281@users.noreply.github.com")
    gh.protect_branch("new", "main")
    gh.pull(tmp_path)
    assert gh.checkout_state(tmp_path) == ("main", False)
    gh.pr_edit_body("https://github.com/parv112281/new/pull/1", "body\n\nFixes #2\n")
    calls = stub.calls()
    lines = [c["prog"] + " " + " ".join(c["args"]) for c in calls]
    assert lines[0] == "gh repo create parv112281/new --private --add-readme"
    assert lines[1] == f"gh repo clone parv112281/new {tmp_path}/repos/new"
    assert lines[2] == f"git -C {tmp_path} config user.name Parv"
    assert lines[3] == f"git -C {tmp_path} config user.email 6843133+parv112281@users.noreply.github.com"
    assert lines[4] == "gh api -X PUT repos/parv112281/new/branches/main/protection --input -"
    rule = json.loads(calls[4]["stdin"])
    assert rule["enforce_admins"] is True and rule["required_pull_request_reviews"] == {"required_approving_review_count": 0}
    assert lines[5] == f"git -C {tmp_path} pull --ff-only"
    assert lines[8] == "gh pr edit https://github.com/parv112281/new/pull/1 --body-file -"
    assert calls[8]["stdin"] == "body\n\nFixes #2\n"
    # every git call gets gh as credential helper, without touching ~/.gitconfig
    env = calls[5]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_VALUE_1"] == f"!{stub.folder / 'gh'} auth git-credential"


def test_github_pr_view_comments_and_issues(stub):
    view = {"state": "MERGED", "mergedAt": "2026-10-07T10:00:00Z", "body": "text",
            "files": [{"path": "ideas/x/idea.md", "additions": 3}],
            "commits": [{"committedDate": "2026-10-07T08:00:00Z"}, {"committedDate": "2026-10-07T09:00:00Z"}]}
    comments = [[{"id": 5, "body": "/revise x", "user": {"login": "parv112281"}, "created_at": "2026-10-07T09:30:00Z"}],
                [{"id": 6, "body": None, "user": None, "created_at": None}]]
    review_comments = [[{"id": 7, "body": "rename", "user": {"login": "parv112281"}, "path": "a.py", "line": None,
                         "original_line": 4, "created_at": "2026-10-07T09:31:00Z"}]]
    reviews = [[{"id": 8, "body": "ok", "state": "COMMENTED", "user": {"login": "parv112281"},
                 "submitted_at": "2026-10-07T09:32:00Z"},
                {"id": 9, "body": "draft", "state": "PENDING", "user": {"login": "parv112281"}}]]
    issues = [{"number": 4, "title": "B", "body": None, "labels": [{"name": "agent"}, {"name": "bug"}]},
              {"number": 2, "title": "A", "body": "text", "labels": [{"name": "agent"}]}]
    stub.rules([
        {"match": "pr view", "out": json.dumps(view)},
        {"match": "issues/3/comments", "out": json.dumps(comments)},
        {"match": "pulls/3/comments", "out": json.dumps(review_comments)},
        {"match": "pulls/3/reviews", "out": json.dumps(reviews)},
        {"match": "issue list", "out": json.dumps(issues)},
    ])
    gh = stub.github()
    info = gh.pr_view("https://github.com/parv112281/r/pull/3")
    assert info["merged"] is True and info["files"] == ["ideas/x/idea.md"] and info["body"] == "text"
    assert info["last_commit_at"] == 1791363600.0
    got = gh.issue_comments("r", 3)
    assert got[0] == {"id": 5, "body": "/revise x", "user": "parv112281", "created_at": 1791365400.0}
    assert got[1] == {"id": 6, "body": "", "user": "", "created_at": 0}
    assert gh.review_comments("r", 3)[0]["line"] == 4 and gh.review_comments("r", 3)[0]["path"] == "a.py"
    assert [r["id"] for r in gh.reviews("r", 3)] == [8]
    assert gh.list_issues("r") == [
        {"number": 2, "title": "A", "body": "text", "labels": ["agent"]},
        {"number": 4, "title": "B", "body": "", "labels": ["agent", "bug"]},
    ]
    lines = [" ".join(c["args"]) for c in stub.calls()]
    assert lines[0] == "pr view https://github.com/parv112281/r/pull/3 --json state,mergedAt,body,files,commits"
    assert lines[1] == "api --paginate --slurp repos/parv112281/r/issues/3/comments?per_page=100"
    assert lines[-1] == "issue list -R parv112281/r --label agent --state open --json number,title,body,labels --limit 50"


class FakeBoardServer:
    """A tiny board: GET /api/board, POST /api/tasks, PATCH /api/tasks/:id, POST /api/pause."""

    def __init__(self, tasks=None):
        self.tasks = tasks or {}
        self.paused = False
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, body))
                if self.headers.get("authorization") != f"Bearer {TOKEN}":
                    return self.reply(401, {"error": "Unauthorized"})
                if self.command == "GET" and self.path == "/api/board":
                    return self.reply(200, {"tasks": outer.tasks, "columns": {}, "paused": outer.paused})
                if self.command == "POST" and self.path == "/api/tasks":
                    task = dict(body, id="11111111-2222-4333-8444-555555555555", status="queued")
                    outer.tasks[task["id"]] = task
                    return self.reply(201, {"task": task, "board": {}})
                if self.command == "POST" and self.path == "/api/pause":
                    outer.paused = body["paused"]
                    return self.reply(200, {})
                if self.command == "PATCH" and self.path.startswith("/api/tasks/"):
                    task = outer.tasks.get(self.path.rsplit("/", 1)[1])
                    if not task:
                        return self.reply(404, {"error": "Task not found"})
                    task.update(body)
                    return self.reply(200, {"task": task, "board": {}})
                return self.reply(404, {"error": "no such route"})

            do_GET = do_POST = do_PATCH = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    srv = FakeBoardServer()
    yield srv
    srv.close()


def test_board_adapter_over_http(server, tmp_path):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    board = Board(server.url, token_file=token)
    assert board.get_board()["tasks"] == {}
    task = board.create_task({"title": "t", "prompt": "p", "agent": "build", "tags": ["repo:x"], "notes": ""})
    assert task["status"] == "queued"
    assert board.patch_task(task["id"], {"status": "review", "tags": ["repo:x", "blocked"]})["tags"] == ["repo:x", "blocked"]
    board.set_paused(True)
    assert board.get_board()["paused"] is True
    with pytest.raises(BoardError) as err:
        board.patch_task("nope", {"status": "done"})
    assert "404" in str(err.value) and "Task not found" in str(err.value)
    assert server.requests[1] == ("POST", "/api/tasks",
                                  {"title": "t", "prompt": "p", "agent": "build", "tags": ["repo:x"], "notes": ""})
    # a wrong token is an error that does not show the token
    token.write_text("wrong")
    with pytest.raises(BoardError) as err:
        board.get_board()
    assert "401" in str(err.value) and "wrong" not in str(err.value)


def test_board_adapter_unreachable(tmp_path):
    board = Board("http://127.0.0.1:9", token_file=tmp_path / "missing", timeout=2)
    with pytest.raises(BoardError) as err:
        board.get_board()
    assert "unreachable" in str(err.value)


def run_launcher(args, home, server_url, stub, token):
    (home / "state").mkdir(parents=True, exist_ok=True)
    config = {"board_url": server_url, "history_file": str(home / "history.jsonl")}
    (home / "state" / "config.json").write_text(json.dumps(config))
    env = {
        "PATH": f"{Path.home()}/bin:/usr/local/bin:/usr/bin:/bin",  # what host-tools gives it
        "HOME": str(Path.home()),
        "PIPELINE_HOME": str(home),
        "PIPELINE_TOKEN_FILE": str(token),
        "PIPELINE_GH": str(stub.folder / "gh"),
        "PIPELINE_GIT": str(stub.folder / "git"),
    }
    return subprocess.run([str(LAUNCHER)] + args, cwd="/", env=env, capture_output=True, text=True, timeout=60)


def test_launcher_tick_against_fake_board_server(server, stub, tmp_path):
    assert LAUNCHER.is_file() and os.access(LAUNCHER, os.X_OK)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    url = "https://github.com/parv112281/alpha/pull/4"
    server.tasks["aaaaaaaa-0000-4000-8000-000000000001"] = {
        "id": "aaaaaaaa-0000-4000-8000-000000000001", "title": "Add login", "prompt": "p", "agent": "build",
        "tags": ["repo:alpha", "stage:build", "project:alpha", "ticket:2"], "notes": "", "status": "review",
        "createdAt": 1, "updatedAt": 2, "result": f"Done. 3 passed.\n\nBOARD_PR: {url}", "error": False, "costUsd": 0.31,
    }
    server.tasks["aaaaaaaa-0000-4000-8000-000000000002"] = {
        "id": "aaaaaaaa-0000-4000-8000-000000000002", "title": "Broken", "prompt": "p", "agent": "specify",
        "tags": ["repo:beta", "stage:specify", "project:beta"], "notes": "", "status": "review",
        "createdAt": 3, "updatedAt": 4, "result": "claude exited 1", "error": True,
    }
    stub.rules([{"match": "api --paginate", "out": "[[]]"}])
    home = tmp_path / "home"
    done = run_launcher(["tick"], home, server.url, stub, token)
    assert done.returncode == 0, done.stderr
    notes = json.loads(done.stdout)["notifications"]
    assert [n["text"] for n in notes] == [
        f"PR ready (build, alpha #2): Add login. {url}",
        'Blocked (specify, beta): Broken. The run failed: claude exited 1 To send it back with a note: '
        '"revise aaaaaaaa: <note>".',
    ]
    assert TOKEN not in done.stdout + done.stderr
    assert "blocked" in server.tasks["aaaaaaaa-0000-4000-8000-000000000002"]["tags"]
    patches = [r for r in server.requests if r[0] == "PATCH"]
    assert patches == [("PATCH", "/api/tasks/aaaaaaaa-0000-4000-8000-000000000002",
                        {"tags": ["repo:beta", "stage:specify", "project:beta", "blocked"]})]
    assert any("issues/4/comments" in " ".join(c["args"]) for c in stub.calls())
    for name in ("config.json", "tasks.json", "outbox.json", "projects.json"):
        assert (home / "state" / name).is_file()

    # a second tick prints the same two and changes nothing; ack removes them
    again = run_launcher(["tick"], home, server.url, stub, token)
    assert json.loads(again.stdout)["notifications"] == notes
    assert len([r for r in server.requests if r[0] == "PATCH"]) == 1
    acked = run_launcher(["ack"] + [n["id"] for n in notes], home, server.url, stub, token)
    assert acked.stdout.strip() == "ok"
    assert json.loads(run_launcher(["tick"], home, server.url, stub, token).stdout) == {"notifications": []}

    # board down: still valid JSON, exit 0, the problem on stderr
    down = run_launcher(["tick"], home, "http://127.0.0.1:9", stub, token)
    assert down.returncode == 0 and json.loads(down.stdout) == {"notifications": []}
    assert "unreachable" in down.stderr
    bad = run_launcher(["request", "bug", "ghost", "x"], home, server.url, stub, token)
    assert bad.returncode == 1 and bad.stdout == "" and "needs onboarding first" in bad.stderr
    assert len(bad.stderr.strip().splitlines()) == 1
