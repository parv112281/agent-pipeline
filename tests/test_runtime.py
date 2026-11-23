"""Regression coverage for headless file inspection and nvm service startup."""
import os
import subprocess
from pathlib import Path

import pytest
from pipeline import prompts

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("route,command", [("build", "npm test"), ("build-python", "pytest")])
def test_build_instructions_allow_reading_before_tests(route, command):
    ticket = {"n": 1, "title": "Read then edit", "description": "D"}
    text = prompts.build("demo", ticket, route, ["docs/design.md"])
    assert "Read these documents" in text
    assert "Read-only inspection inside the current worktree is allowed" in text
    assert all(f"`{cmd}`" in text for cmd in ["cat", "ls", "rg"])
    assert command in text
    assert "That is the only command you can run" not in text
    assert "Do not run git or other shell commands" in text
    assert "no pipelines, redirection, command substitution" in text

@pytest.mark.parametrize("make_prompt", [lambda: prompts.plan("p"),
    lambda: prompts.plan_amend("p", "feature", [], "build"),
    lambda: prompts.specify("p", "idea")])
def test_planning_does_not_describe_build_routes_as_unable_to_read(make_prompt):
    assert "read-only file inspection" in make_prompt()


def invoke_wrapper(tmp_path, major="24", alias_exists=True):
    nvm_dir = tmp_path / "nvm"
    node_bin = tmp_path / "node-bin"
    nvm_dir.mkdir()
    node_bin.mkdir()
    node = node_bin / "node"
    node.write_text("#!/bin/sh\nif [ \"$1\" = -p ]; then echo " + major + "; else printf '%s\\n' \"$@\"; fi\n")
    node.chmod(0o755)
    if alias_exists:
        nvm = 'nvm() { export PATH="' + str(node_bin) + ':$PATH"; }\n'
    else:
        nvm = 'nvm() { return 3; }\n'
    (nvm_dir / "nvm.sh").write_text(nvm)
    return subprocess.run(["bash", str(ROOT / "systemd/pipeline-node"), "--env-file=.env", "agent/dispatcher.mjs"],
        env={**os.environ, "NVM_DIR": str(nvm_dir)}, text=True, capture_output=True)


def test_service_wrapper_selects_nvm_node_and_preserves_arguments(tmp_path):
    result = invoke_wrapper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["--env-file=.env", "agent/dispatcher.mjs"]

@pytest.mark.parametrize("major,alias_exists", [("20", True), ("24", False)])
def test_service_wrapper_fails_closed_for_missing_or_wrong_runtime(tmp_path, major, alias_exists):
    result = invoke_wrapper(tmp_path, major, alias_exists)
    assert result.returncode == 78
    assert "Pipeline runtime:" in result.stderr


def test_all_services_use_the_same_launcher():
    for name in ("agent-board", "agent-dispatcher", "agent-merge-watcher"):
        text = (ROOT / "systemd" / f"{name}.service").read_text()
        assert "v20.18.0" not in text
        assert "systemd/pipeline-node" in text
