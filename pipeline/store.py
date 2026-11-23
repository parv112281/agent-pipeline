"""State files under <root>/state. JSON, written atomically, one lock file."""

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from .errors import PipelineError

DEFAULT_ROOT = "~/agent-pipeline"

DEFAULT_CONFIG = {
    "board_url": "http://127.0.0.1:3738",
    "github_owner": "parv112281",
    "ideas_repo": "ideas",
    "max_reworks": 3,
    "git_name": "Parv",
    "git_email": "6843133+parv112281@users.noreply.github.com",
    "history_file": "~/agent-pipeline/board/.data/history.jsonl",
}


class Store:
    def __init__(self, root=None):
        root = root or os.environ.get("PIPELINE_HOME") or DEFAULT_ROOT
        self.root = Path(root).expanduser()
        self.state_dir = self.root / "state"
        self.repos_dir = self.root / "repos"
        self._seen = {}

    # locking

    @contextlib.contextmanager
    def lock(self, timeout=120.0):
        """Exclusive lock so a tick and an intake command cannot interleave."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.state_dir / ".lock", "w")
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise PipelineError("The pipeline is busy with another command. Try again in a minute.")
                    time.sleep(0.2)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    # raw JSON files

    def _read(self, path, default):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        try:
            data = json.loads(text)
        except ValueError:
            raise PipelineError(f"State file {path} is not valid JSON. Fix or remove it.")
        self._seen[path] = text
        return data

    def _write(self, path, data):
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if self._seen.get(path) == text and path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        self._seen[path] = text

    # typed accessors

    def load_config(self):
        path = self.state_dir / "config.json"
        stored = self._read(path, None)
        config = dict(DEFAULT_CONFIG)
        if isinstance(stored, dict):
            config.update(stored)
        if stored is None:
            self._write(path, config)
        return config

    def load_projects(self):
        data = self._read(self.state_dir / "projects.json", None)
        if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
            data = {"projects": {}}
        return data

    def save_projects(self, data):
        self._write(self.state_dir / "projects.json", data)

    def load_tasks(self):
        data = self._read(self.state_dir / "tasks.json", None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("tasks", {})
        data.setdefault("budget", {})
        return data

    def save_tasks(self, data):
        self._write(self.state_dir / "tasks.json", data)

    def load_outbox(self):
        data = self._read(self.state_dir / "outbox.json", None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("seq", 0)
        data.setdefault("items", [])
        data.setdefault("sent", [])
        return data

    def save_outbox(self, data):
        self._write(self.state_dir / "outbox.json", data)

    def plan_path(self, project):
        return self.state_dir / project / "plan.json"

    def load_plan(self, project):
        data = self._read(self.plan_path(project), None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("project", project)
        data.setdefault("next_n", 1)
        data.setdefault("tickets", [])
        return data

    def save_plan(self, project, data):
        self._write(self.plan_path(project), data)

    def read_history(self, history_file):
        """Run history lines of the board ({costUsd, at, ...}); bad lines are skipped."""
        path = Path(str(history_file)).expanduser()
        records = []
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records
