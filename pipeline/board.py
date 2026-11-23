"""HTTP adapter for the board API."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .errors import BoardError

DEFAULT_TOKEN_FILE = "~/.config/agent-pipeline/token"


class Board:
    def __init__(self, base_url, token_file=None, timeout=20):
        self.base_url = base_url.rstrip("/")
        self.token_file = Path(token_file or os.environ.get("PIPELINE_TOKEN_FILE") or DEFAULT_TOKEN_FILE).expanduser()
        self.timeout = timeout
        # No proxy: the board is on this machine.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _token(self):
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _request(self, method, path, body=None):
        headers = {"content-type": "application/json"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = json.loads(err.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            raise BoardError(f"Board {method} {path} failed: {err.code} {detail or err.reason}")
        except (urllib.error.URLError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise BoardError(f"Board unreachable at {self.base_url}: {reason}")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise BoardError(f"Board {method} {path} returned something that is not JSON")

    def get_board(self):
        board = self._request("GET", "/api/board")
        if not isinstance(board, dict) or not isinstance(board.get("tasks"), dict):
            raise BoardError("Board GET /api/board returned an unexpected shape")
        return board

    def create_task(self, fields):
        return self._request("POST", "/api/tasks", fields)["task"]

    def patch_task(self, task_id, patch):
        return self._request("PATCH", f"/api/tasks/{task_id}", patch)["task"]

    def set_paused(self, paused):
        self._request("POST", "/api/pause", {"paused": bool(paused)})
