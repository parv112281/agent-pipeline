"""Subprocess adapter for gh and git."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from .errors import GitHubError

DEFAULT_GH = "~/bin/gh"
DEFAULT_GIT = "/usr/bin/git"


def parse_time(value):
    """ISO 8601 time from GitHub to epoch seconds, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GitHub:
    def __init__(self, owner, gh=None, git=None):
        self.owner = owner
        self.gh = str(Path(gh or os.environ.get("PIPELINE_GH") or DEFAULT_GH).expanduser())
        self.git = str(Path(git or os.environ.get("PIPELINE_GIT") or DEFAULT_GIT).expanduser())

    def _env(self):
        """Environment for gh and git. This machine has no git credential helper
        for https, so every git call gets gh as its helper through GIT_CONFIG_*."""
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", NO_COLOR="1")
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            count = 0
        key = "credential.https://github.com.helper"
        for value in ("", f"!{self.gh} auth git-credential"):
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env

    def _run(self, args, stdin=None, timeout=90):
        env = self._env()
        try:
            done = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise GitHubError(f"{Path(args[0]).name} {args[1]} timed out")
        except OSError as err:
            raise GitHubError(f"could not run {args[0]}: {err}")
        if done.returncode != 0:
            detail = [l.strip() for l in (done.stderr or done.stdout or "").strip().splitlines() if l.strip()]
            # git push ends with a generic "failed to push some refs"; the reason is on an earlier line.
            specific = [l for l in detail if "remote rejected" in l or "refusing" in l or l.startswith("fatal:")]
            tail = specific[-1] if specific else detail[-1] if detail else f"exit {done.returncode}"
            raise GitHubError(f"{Path(args[0]).name} {' '.join(args[1:4])} failed: {tail}")
        return done.stdout

    def _api_list(self, path):
        sep = "&" if "?" in path else "?"
        out = self._run([self.gh, "api", "--paginate", "--slurp", f"{path}{sep}per_page=100"])
        try:
            pages = json.loads(out or "[]")
        except ValueError:
            raise GitHubError(f"gh api {path} returned something that is not JSON")
        items = []
        for page in pages:
            if isinstance(page, list):
                items.extend(x for x in page if isinstance(x, dict))
        return items

    # repositories and checkouts

    def repo_info(self, name):
        """{'default_branch': ...} or None when the repo does not exist."""
        try:
            out = self._run([self.gh, "api", f"repos/{self.owner}/{name}"])
        except GitHubError as err:
            if "404" in str(err) or "Not Found" in str(err):
                return None
            raise
        try:
            data = json.loads(out)
        except ValueError:
            raise GitHubError(f"gh api repos/{self.owner}/{name} returned something that is not JSON")
        return {"default_branch": data.get("default_branch") or "main"}

    def create_repo(self, name):
        self._run([self.gh, "repo", "create", f"{self.owner}/{name}", "--private", "--add-readme"])

    def clone(self, name, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self._run([self.gh, "repo", "clone", f"{self.owner}/{name}", str(dest)], timeout=300)

    def set_identity(self, path, name, email):
        self._run([self.git, "-C", str(path), "config", "user.name", name])
        self._run([self.git, "-C", str(path), "config", "user.email", email])

    def protect_branch(self, name, branch):
        """Require a pull request for the branch. Not available on every plan."""
        rule = {
            "required_status_checks": None,
            "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "restrictions": None,
        }
        self._run(
            [self.gh, "api", "-X", "PUT", f"repos/{self.owner}/{name}/branches/{branch}/protection", "--input", "-"],
            stdin=json.dumps(rule),
        )

    def checkout_state(self, path):
        """(current branch, True when no tracked file is modified)."""
        branch = self._run([self.git, "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        dirty = self._run([self.git, "-C", str(path), "status", "--porcelain", "--untracked-files=no"]).strip()
        return branch, not dirty

    def pull(self, path):
        self._run([self.git, "-C", str(path), "pull", "--ff-only"], timeout=120)

    # pull requests

    def pr_view(self, url):
        out = self._run([self.gh, "pr", "view", url, "--json", "state,mergedAt,body,files,commits"])
        try:
            data = json.loads(out)
        except ValueError:
            raise GitHubError(f"gh pr view {url} returned something that is not JSON")
        times = [parse_time(c.get("committedDate")) for c in data.get("commits") or [] if isinstance(c, dict)]
        times = [t for t in times if t is not None]
        return {
            "state": data.get("state") or "",
            "merged": data.get("state") == "MERGED" or bool(data.get("mergedAt")),
            "body": data.get("body") or "",
            "files": [f.get("path") for f in data.get("files") or [] if isinstance(f, dict) and f.get("path")],
            "last_commit_at": max(times) if times else None,
        }

    def pr_edit_body(self, url, body):
        self._run([self.gh, "pr", "edit", url, "--body-file", "-"], stdin=body)

    def issue_comments(self, repo, number):
        items = self._api_list(f"repos/{self.owner}/{repo}/issues/{number}/comments")
        return [
            {
                "id": c.get("id") or 0,
                "body": c.get("body") or "",
                "user": (c.get("user") or {}).get("login") or "",
                "created_at": parse_time(c.get("created_at")) or 0,
            }
            for c in items
        ]

    def review_comments(self, repo, number):
        items = self._api_list(f"repos/{self.owner}/{repo}/pulls/{number}/comments")
        return [
            {
                "id": c.get("id") or 0,
                "body": c.get("body") or "",
                "user": (c.get("user") or {}).get("login") or "",
                "created_at": parse_time(c.get("created_at")) or 0,
                "path": c.get("path") or "",
                "line": c.get("line") or c.get("original_line"),
            }
            for c in items
        ]

    def reviews(self, repo, number):
        items = self._api_list(f"repos/{self.owner}/{repo}/pulls/{number}/reviews")
        return [
            {
                "id": r.get("id") or 0,
                "body": r.get("body") or "",
                "user": (r.get("user") or {}).get("login") or "",
                "created_at": parse_time(r.get("submitted_at")) or 0,
            }
            for r in items
            if r.get("state") != "PENDING"
        ]

    # issues

    def list_issues(self, repo):
        out = self._run(
            [
                self.gh, "issue", "list", "-R", f"{self.owner}/{repo}", "--label", "agent", "--state", "open",
                "--json", "number,title,body,labels", "--limit", "50",
            ]
        )
        try:
            data = json.loads(out or "[]")
        except ValueError:
            raise GitHubError(f"gh issue list for {repo} returned something that is not JSON")
        issues = []
        for item in data:
            issues.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title") or "",
                    "body": item.get("body") or "",
                    "labels": [l.get("name", "") for l in item.get("labels") or [] if isinstance(l, dict)],
                }
            )
        return sorted(issues, key=lambda i: i["number"] or 0)

    # ------------------------------------------------------------------
    # upstream import (spec section 20)

    def upstream_info(self, full_name):
        """Licence and default branch of a public repo, or None when it does not exist."""
        try:
            out = self._run([self.gh, "api", f"repos/{full_name}"])
        except GitHubError as err:
            if "404" in str(err) or "Not Found" in str(err):
                return None
            raise
        data = json.loads(out)
        return {
            "license": ((data.get("license") or {}).get("spdx_id")) or None,
            "default_branch": data.get("default_branch") or "main",
        }

    def resolve_commit(self, full_name, ref):
        """Full commit SHA for a branch, tag or SHA of a public repo."""
        return self._run([self.gh, "api", f"repos/{full_name}/commits/{ref}", "--jq", ".sha"]).strip()

    def download_tarball(self, full_name, sha, dest_file, max_bytes):
        """Write the repo's tarball at a commit to dest_file; refuse one larger than max_bytes."""
        args = [self.gh, "api", f"repos/{full_name}/tarball/{sha}"]
        try:
            with open(dest_file, "wb") as handle:
                done = subprocess.run(args, stdout=handle, stderr=subprocess.PIPE, timeout=600, env=self._env())
        except subprocess.TimeoutExpired:
            raise GitHubError(f"downloading {full_name} timed out")
        if done.returncode != 0:
            tail = (done.stderr or b"").decode(errors="replace").strip().splitlines()
            raise GitHubError(f"downloading {full_name} failed: {tail[-1] if tail else done.returncode}")
        if Path(dest_file).stat().st_size > max_bytes:
            raise GitHubError(f"{full_name} is larger than {max_bytes // 1_000_000} MB; import it by hand")

    def commit_branch(self, path, branch, message):
        """Commit everything in the checkout on a new branch, push it, and return to the previous branch."""
        path = str(path)
        previous = self._run([self.git, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        # -B: a branch left by an import whose push failed is replaced, so the import can be run again.
        self._run([self.git, "-C", path, "checkout", "-q", "-B", branch])
        try:
            self._run([self.git, "-C", path, "add", "-A"])
            self._run([self.git, "-C", path, "commit", "-q", "-m", message])
            self._run([self.git, "-C", path, "push", "-q", "-u", "origin", branch], timeout=300)
        finally:
            self._run([self.git, "-C", path, "checkout", "-q", "-f", previous])
            self._run([self.git, "-C", path, "clean", "-q", "-fd"])

    def pr_for_branch(self, repo, branch):
        """The open PR whose head is this branch, or None."""
        out = self._run([self.gh, "pr", "list", "-R", f"{self.owner}/{repo}", "--head", branch, "--state", "open",
                         "--json", "url", "--jq", ".[0].url // empty"])
        return out.strip() or None

    def pr_create(self, repo, branch, title, body):
        out = self._run(
            [self.gh, "pr", "create", "-R", f"{self.owner}/{repo}", "--head", branch, "--title", title,
             "--body-file", "-"],
            stdin=body,
        )
        return out.strip().splitlines()[-1] if out.strip() else ""
