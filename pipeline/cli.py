"""Command line of the engine. See README.md for the commands."""

import json
import os
import sys
import time

from .board import Board
from .engine import Engine
from .errors import PipelineError
from .github import GitHub
from .store import Store

USAGE = (
    "usage: pipeline tick | ack <id>... | notify <text> | idea <text> | specify <slug> | plan <project> | "
    "request bug|small|feature <project> <text> | revise <ref> <note> | "
    "onboard <repo> [--route build|build-python|build-sandbox] [--check <command>]... | "
    "import <project> <owner>/<repo>[@<ref>] | detach <project> | attach <project> | status [<project>] | budget | "
    "pause | resume"
)

NO_LOCK = ("status", "budget")


def _need(args, count, what):
    if len(args) < count:
        raise PipelineError(f"Missing {what}. {USAGE}")


def dispatch(engine, command, args):
    if command == "ack":
        _need(args, 1, "notification id")
        return engine.cmd_ack(args)
    if command == "notify":
        return engine.cmd_notify(" ".join(args))
    if command == "idea":
        return engine.cmd_idea(" ".join(args))
    if command == "specify":
        _need(args, 1, "slug")
        return engine.cmd_specify(args[0])
    if command == "plan":
        _need(args, 1, "project")
        return engine.cmd_plan(args[0])
    if command == "request":
        _need(args, 3, "kind, project or text")
        return engine.cmd_request(args[0].lower(), args[1], " ".join(args[2:]))
    if command == "revise":
        _need(args, 2, "reference or note")
        return engine.cmd_revise(args[0], " ".join(args[1:]))
    if command == "onboard":
        rest = list(args)
        route = None
        if "--route" in rest:
            at = rest.index("--route")
            if at + 1 >= len(rest):
                raise PipelineError("--route needs a value: build, build-python or build-sandbox.")
            route = rest[at + 1]
            del rest[at:at + 2]
        checks = []
        while "--check" in rest:
            at = rest.index("--check")
            if at + 1 >= len(rest):
                raise PipelineError("--check needs a command, for example: --check './mvnw -q verify'")
            checks.append(rest[at + 1])
            del rest[at:at + 2]
        _need(rest, 1, "repo")
        return engine.cmd_onboard(rest[0], route, checks or None)
    if command == "import":
        _need(args, 2, "project or upstream (owner/repo[@ref])")
        return engine.cmd_import(args[0], args[1])
    if command == "detach":
        _need(args, 1, "project")
        return engine.cmd_detach(args[0])
    if command == "attach":
        _need(args, 1, "project")
        return engine.cmd_attach(args[0])
    if command == "status":
        return engine.cmd_status(args[0] if args else None)
    if command == "budget":
        return engine.cmd_budget()
    if command == "pause":
        return engine.cmd_pause()
    if command == "resume":
        return engine.cmd_resume()
    raise PipelineError(f"Unknown command {command}. {USAGE}")


def main(argv=None, store=None, board=None, github=None, clock=None, stdout=None, stderr=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    def log(message):
        print(" ".join(str(message).split())[:500], file=stderr)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, file=stdout)
        return 0
    command, args = argv[0], argv[1:]
    empty = json.dumps({"notifications": []})
    try:
        store = store or Store()
        if command in NO_LOCK:
            return _run(command, args, store, board, github, clock, stdout, log)
        with store.lock():
            return _run(command, args, store, board, github, clock, stdout, log)
    except PipelineError as err:
        if command == "tick":
            print(empty, file=stdout)
        log(err)
        return 1
    except Exception as err:
        if command == "tick":
            print(empty, file=stdout)
        log(f"Unexpected {type(err).__name__}: {err}")
        return 1


def _run(command, args, store, board, github, clock, stdout, log):
    config = store.load_config()
    board = board or Board(os.environ.get("PIPELINE_BOARD_URL") or config["board_url"])
    github = github or GitHub(config["github_owner"])
    engine = Engine(store, board, github, clock=clock or time.time, log=log)
    if command == "tick":
        print(json.dumps(engine.tick(), ensure_ascii=False), file=stdout)
        return 0
    text = dispatch(engine, command, args)
    print(text if len(text) <= 1490 else text[:1470].rstrip() + " (cut short)", file=stdout)
    return 0
