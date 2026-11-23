"""Intake commands."""

import json


def test_idea_fails_when_ideas_repo_missing(env):
    del env.gh.repos["ideas"]
    code, out, err = env.run("idea", "something")
    assert code == 1 and out == ""
    assert "gh repo create ideas --private --add-readme" in err
    assert env.board.tasks == {}


def test_idea_needs_text_and_a_reachable_board(env):
    assert env.run("idea")[0] == 1
    env.board.down = True
    code, _, err = env.run("idea", "x")
    assert code == 1 and "unreachable" in err
    assert env.gh.called("clone") == []


def test_idea_reuses_checkout_and_pulls(env):
    env.ok("idea", "one")
    env.ok("idea", "two")
    assert len(env.gh.called("clone")) == 1
    assert len(env.gh.called("pull")) == 2
    assert len(env.board.find(stage="flesh-out")) == 2


def test_specify_creates_repo_and_queues(env):
    env.gh.repos["ideas"]["files"]["ideas/tea-timer/idea.md"] = "# Tea timer\nBrew better."
    out = env.ok("specify", "tea-timer")
    assert "Created the private repo parv112281/tea-timer" in out and "Warning" not in out
    assert env.gh.called("create_repo") == [("create_repo", "tea-timer")]
    assert ("set_identity", "tea-timer", "Parv", "6843133+parv112281@users.noreply.github.com") in env.gh.calls
    (task,) = env.board.find(stage="specify")
    assert "Brew better." in task["prompt"]
    assert "docs/requirements.md" in task["prompt"] and "docs/design.md" in task["prompt"]
    assert env.read("projects.json")["projects"]["tea-timer"]["idea"] == "tea-timer"
    # asking again does not queue a second task
    assert "already on the board" in env.ok("specify", "tea-timer")
    assert len(env.board.find(stage="specify")) == 1


def test_specify_protection_failure_is_a_warning(env):
    env.gh.repos["ideas"]["files"]["ideas/tea-timer/idea.md"] = "# Tea timer"
    env.gh.protect_fails = True
    out = env.ok("specify", "tea-timer")
    assert "Warning: could not protect main" in out
    assert len(env.board.find(stage="specify")) == 1


def test_specify_existing_repo_is_not_created_again(env):
    env.gh.repos["ideas"]["files"]["ideas/tea-timer/idea.md"] = "# Tea timer"
    env.gh.add_repo("tea-timer")
    env.ok("specify", "tea-timer")
    assert env.gh.called("create_repo") == [] and env.gh.called("protect_branch") == []
    assert len(env.gh.called("clone")) == 2


def test_specify_missing_idea_or_bad_slug(env):
    code, _, err = env.run("specify", "nope")
    assert code == 1 and "ideas/nope/idea.md is not on the main branch" in err
    assert env.run("specify", "Bad Slug")[0] == 1
    assert env.run("specify", "ideas")[0] == 1
    assert env.gh.called("create_repo") == []


def test_plan_requires_both_documents(env):
    env.project("alpha", route=None, files={"docs/design.md": "# D"})
    del env.gh.repos["alpha"]["files"]["docs/requirements.md"]
    (env.root / "repos/alpha/docs/requirements.md").unlink()
    code, _, err = env.run("plan", "alpha")
    assert code == 1 and "needs docs/requirements.md and docs/design.md" in err
    assert env.board.tasks == {}


def test_plan_queues_task_with_strict_prompt(env):
    env.project("alpha", route=None)
    out = env.ok("plan", "alpha")
    assert "Plan task queued" in out
    (task,) = env.board.find(stage="plan")
    assert task["agent"] == "plan"
    assert set(task["tags"]) == {"repo:alpha", "stage:plan", "project:alpha"}
    prompt = task["prompt"]
    for needle in ('"build_route": "build" | "build-python" | "build-sandbox"', "project skeleton with a working test runner",
                   "about 15 minutes", "`npm test`, `npm install` and `node --test`", "`pytest`",
                   "No ticket may need a manual step", "1-based positions", "`checks`: only for `build-sandbox`"):
        assert needle in prompt
    assert ("pull", "alpha") in env.gh.calls
    assert "already on the board" in env.ok("plan", "alpha")
    assert len(env.board.tasks) == 1


def test_plan_registers_a_repo_parv_made_himself(env):
    env.gh.add_repo("handmade", {"docs/requirements.md": "r", "docs/design.md": "d"})
    env.ok("plan", "handmade")
    assert "handmade" in env.read("projects.json")["projects"]
    assert len(env.board.find(stage="plan")) == 1
    code, _, err = env.run("plan", "ghost")
    assert code == 1 and "needs onboarding first" in err


def test_onboard_guesses_route_and_queues(env):
    env.gh.add_repo("website", {"package.json": json.dumps({"scripts": {"test": "node --test"}}), "index.js": ""})
    out = env.ok("onboard", "website")
    assert "Build route: build (guessed from the files)" in out
    project = env.read("projects.json")["projects"]["website"]
    assert project["build_route"] == "build" and project["onboarded"] is False
    assert project["default_branch"] == "main"
    (task,) = env.board.find(stage="onboard")
    assert task["agent"] == "onboard"
    assert set(task["tags"]) == {"repo:website", "stage:onboard", "project:website"}
    assert "docs/overview.md" in task["prompt"]
    assert env.gh.called("clone")[0][2] == str(env.root / "repos" / "website")
    assert not (env.state / "website" / "plan.json").exists()

    url = env.open_pr(task, 1)
    assert "PR ready (onboard, website)" in env.fresh()[0]
    env.merge(task, url, {"docs/overview.md": "# Overview"})
    notes = env.fresh()
    assert len(notes) == 1 and "Onboarding of website is merged" in notes[0]
    assert env.read("projects.json")["projects"]["website"]["onboarded"] is True
    assert "already onboarded" in env.ok("onboard", "website")


def test_onboard_route_option_python_guess_and_unknown(env):
    env.gh.add_repo("tool", {"pyproject.toml": "", "tests/test_x.py": ""})
    assert "build-python (guessed" in env.ok("onboard", "tool")
    env.gh.add_repo("odd", {"main.go": ""})
    out = env.ok("onboard", "odd")
    assert "No build route could be guessed" in out
    assert env.read("projects.json")["projects"]["odd"]["build_route"] is None
    env.gh.add_repo("forced", {"main.go": "", "x_test.py": ""})
    assert "Build route: build-python." in env.ok("onboard", "forced", "--route", "build-python")
    assert env.run("onboard", "forced", "--route", "rust")[0] == 1


def test_onboard_repo_without_tests_gets_test_runner_ticket(env):
    env.gh.add_repo("bare", {"requirements.txt": "", "app.py": ""})
    out = env.ok("onboard", "bare")
    assert "first ticket is: add a test runner" in out
    assert env.ticket("bare", 1)["title"] == "Add a test runner"
    # it waits for the onboarding task
    env.fresh()
    assert env.board.find(stage="build") == []
    (task,) = env.board.find(stage="onboard")
    url = env.open_pr(task, 1)
    env.fresh()
    env.merge(task, url, {"docs/overview.md": "# O"})
    env.fresh()
    (build,) = env.board.find(stage="build")
    assert build["title"] == "Add a test runner" and "docs/overview.md" in build["prompt"]


def test_onboard_requires_repo_on_github(env):
    code, _, err = env.run("onboard", "local-only")
    assert code == 1 and "has to be pushed to GitHub first" in err
    assert env.run("onboard", "ideas")[0] == 1


def test_status_and_usage(env):
    env.project("alpha", tickets=[("A", [], "done"), ("B", [1], "todo"), ("C", [2], "todo")])
    env.fresh()
    out = env.ok("status")
    assert "alpha: 1 of 3 tickets done." in out and "queued: #2 B" in out
    out = env.ok("status", "alpha")
    assert "#2 [queued] B" in out and "#3 [todo] C (after #2)" in out
    env.board.down = True
    assert "not reachable" in env.ok("status")
    assert env.run("status", "ghost")[0] == 1
    assert env.run("frobnicate")[0] == 1
    assert env.ok("help").startswith("usage:")


def test_pause_and_resume_by_hand(env):
    assert "paused" in env.ok("pause")
    assert env.board.paused is True
    assert "paused" in env.ok("budget")
    assert "running again" in env.ok("resume")
    assert env.board.paused is False


def test_task_without_repo_tag_is_refused(env):
    import pytest
    from pipeline.errors import PipelineError
    engine = env.engine()
    engine.load_board()
    with pytest.raises(PipelineError):
        engine.create_task("t", "p", "build", ["stage:build"])
    task = engine.create_task("t", "--help me", "plan", ["repo:x", "stage:plan"])
    assert not task["prompt"].startswith("-")


def test_prompts_ask_for_gitignore():
    from pipeline import prompts

    ticket = {"n": 1, "title": "Skeleton", "description": "Set up.", "kind": "build"}
    assert ".gitignore" in prompts.plan("demo")
    assert ".gitignore" in prompts.build("demo", ticket, "build-python", [])


def test_detach_stops_the_pipeline_for_a_project_and_attach_resumes_it(env):
    env.project("alpha", tickets=[("A", [], "todo"), ("B", [], "todo")])
    env.project("beta", tickets=[("X", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build", project="alpha")
    env.board.finish(task["id"], "Run failed", error=True)
    env.fresh()
    assert env.ticket("alpha", 1)["status"] == "blocked"

    assert "alpha is detached" in env.ok("detach", "alpha")
    assert "already detached" in env.ok("detach", "alpha")
    assert "detached (worked on by hand)" in env.ok("status")
    # Its blocked task is left alone, even when it leaves the board: no notes, no new tickets.
    del env.board.tasks[task["id"]]
    assert env.fresh() == []
    assert env.ticket("alpha", 1)["status"] == "blocked"
    assert env.ticket("alpha", 2)["status"] == "todo"
    assert env.board.find(stage="build", project="alpha") == []
    # Other projects carry on.
    assert len(env.board.find(stage="build", project="beta")) == 1
    for argv in (("request", "small", "alpha", "Do more"), ("revise", "alpha#1", "again"), ("plan", "alpha")):
        code, _, err = env.run(*argv)
        assert code == 1 and 'attach alpha' in err

    assert "back in the pipeline" in env.ok("attach", "alpha")
    assert "not detached" in env.ok("attach", "alpha")
    env.fresh()
    (queued,) = env.board.find(stage="build", project="alpha")
    assert queued["title"] == "B"
    assert env.run("detach", "ghost")[0] == 1
