"""Changes after the first implementation (spec section 18)."""

import json

FEATURE_PLAN = {"build_route": "build-python", "tickets": [
    {"title": "Export model", "description": "d", "acceptance": ["a"], "doc_sections": ["design 7"], "depends_on": []},
    {"title": "Export command", "description": "d", "acceptance": ["a"], "doc_sections": [], "depends_on": [1]},
]}


def test_unknown_project_is_refused(env):
    for kind in ("bug", "small", "feature"):
        code, out, err = env.run("request", kind, "ghost", "something")
        assert code == 1 and out == ""
        assert "needs onboarding first" in err and "onboard ghost" in err
    assert env.board.tasks == {}
    assert env.run("request", "huge", "ghost", "x")[0] == 1
    assert env.run("request", "bug", "ghost")[0] == 1


def test_bug_goes_ahead_of_unstarted_tickets_and_small_goes_last(env):
    env.project("alpha", tickets=[("A", [], "done"), ("B", [], "todo"), ("C", [2], "todo"), ("D", [], "todo")])
    env.fresh()
    (running,) = env.board.find(stage="build")
    assert running["title"] == "B"

    out = env.ok("request", "bug", "alpha:", "crash", "on empty input")
    assert "ticket #5" in out and "ahead of the tickets not yet started" in out
    assert "starts when the project has no other task" in out
    out = env.ok("request", "small", "alpha", "rename the --out flag to --output")
    assert "ticket #6" in out and "at the end of the plan" in out
    plan = env.plan("alpha")
    assert [t["n"] for t in plan["tickets"]] == [1, 2, 5, 3, 4, 6]
    bug = plan["tickets"][2]
    assert bug["kind"] == "bug" and bug["title"] == "Fix: crash on empty input" and bug["status"] == "todo"
    assert plan["tickets"][-1]["kind"] == "small"
    # the task in progress is not interrupted
    assert len(env.board.find(stage="build")) == 1

    url = env.open_pr(running, 1)
    env.fresh()
    env.merge(running, url)
    env.fresh()
    bug_task = env.board.find(stage="build")[1]
    assert "ticket:5" in bug_task["tags"] and bug_task["agent"] == "build-python"
    assert "First add a test that fails because of the bug" in bug_task["prompt"]
    assert "PIPELINE_BLOCKED: needs planning" in bug_task["prompt"]
    assert "update them in the same change" in bug_task["prompt"]


def test_request_on_idle_project_is_queued_at_once(env):
    env.project("alpha", tickets=[("A", [], "done")])
    out = env.ok("request", "small", "alpha", "add a --version flag")
    assert "Queued now" in out
    (task,) = env.board.find(stage="build")
    assert task["title"] == "add a --version flag" and "ticket:2" in task["tags"]
    url = env.open_pr(task, 3)
    env.fresh()
    env.merge(task, url)
    assert env.fresh() == ["Project alpha finished: all 2 tickets are merged."]


def test_direct_request_needs_a_build_route(env):
    env.project("alpha", route=None, tickets=[])
    code, _, err = env.run("request", "bug", "alpha", "x")
    assert code == 1 and "no build route" in err


def test_feature_goes_through_amend_then_plan_and_appends_tickets(env):
    env.project("alpha", tickets=[("A", [], "done"), ("B", [], "todo")])
    env.fresh()
    (b,) = env.board.find(stage="build")
    out = env.ok("request", "feature", "alpha", "export the report as CSV")
    assert "Feature recorded" in out
    # one task per project: the amend task waits for the open ticket
    assert env.board.find(stage="specify") == []
    assert "1 feature request(s) waiting" in env.ok("status", "alpha")
    url = env.open_pr(b, 1)
    env.fresh()
    env.merge(b, url)
    assert env.fresh() == []  # not "finished": a feature is waiting

    (amend,) = env.board.find(stage="specify")
    assert amend["agent"] == "specify"
    assert set(amend["tags"]) == {"repo:alpha", "stage:specify", "project:alpha", "amend"}
    assert "export the report as CSV" in amend["prompt"]
    assert "Update `docs/requirements.md` and `docs/design.md`" in amend["prompt"]
    assert env.plan("alpha")["pending_features"] == []
    url = env.open_pr(amend, 2)
    assert "PR ready (amend, alpha)" in env.fresh()[0]
    env.merge(amend, url)
    env.fresh()

    (plan_task,) = env.board.find(stage="plan")
    assert "amend" in plan_task["tags"]
    assert "export the report as CSV" in plan_task["prompt"]
    assert "#1 A (done)" in plan_task["prompt"] and "for this feature only" in plan_task["prompt"]
    env.board.finish(plan_task["id"], json.dumps(FEATURE_PLAN), status="done")
    notes = env.fresh()
    assert len(notes) == 1 and notes[0].startswith("Feature planned for alpha: 2 build tickets.")
    plan = env.plan("alpha")
    assert [(t["n"], t["kind"], t["status"]) for t in plan["tickets"]] == [
        (1, "build", "done"), (2, "build", "done"), (3, "feature", "queued"), (4, "feature", "todo")]
    assert plan["tickets"][3]["depends_on"] == [3]
    assert [t["title"] for t in env.board.find(stage="build")] == ["B", "Export model"]


def test_first_feature_on_onboarded_project_creates_the_documents(env):
    env.gh.add_repo("legacy", {"package.json": json.dumps({"scripts": {"test": "node --test"}}),
                               "docs/overview.md": "# Overview"})
    env.gh.clone("legacy", env.root / "repos" / "legacy")
    (env.state / "projects.json").write_text(json.dumps({"projects": {"legacy": {
        "repo": "legacy", "default_branch": "main", "build_route": "build", "idea": None,
        "onboarded": True, "seen_issues": []}}}))
    out = env.ok("request", "feature", "legacy", "dark mode")
    assert "Queued now" in out
    (amend,) = env.board.find(stage="specify")
    assert "covering only this feature" in amend["prompt"]
    assert "Read `docs/overview.md` first" in amend["prompt"]
    # the plan cannot start if the amend PR did not bring the documents
    url = env.open_pr(amend, 1)
    env.fresh()
    env.merge(amend, url)
    notes = env.fresh()
    assert len(notes) == 1 and "the next step could not start" in notes[0]
    assert env.fresh() == []


def test_issue_intake_and_fixes_line(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (a,) = env.board.find(stage="build")
    env.gh.issues["alpha"] = [
        {"number": 12, "title": "Crash on empty file", "body": "Steps: run it on an empty file.", "labels": ["agent", "bug"]},
        {"number": 13, "title": "Add --quiet", "body": "", "labels": ["agent"]},
        {"number": 14, "title": "Export to PDF", "body": "Like the CSV one.", "labels": ["agent", "feature"]},
    ]
    notes = env.fresh()
    assert notes == [
        "Issue #12 on alpha taken as a bug request: Crash on empty file.",
        "Issue #13 on alpha taken as a small request: Add --quiet.",
        "Issue #14 on alpha taken as a feature request: Export to PDF.",
    ]
    assert env.read("projects.json")["projects"]["alpha"]["seen_issues"] == [12, 13, 14]
    plan = env.plan("alpha")
    assert [(t["n"], t["kind"], t["issue"]) for t in plan["tickets"]] == [(1, "build", None), (2, "bug", 12), (3, "small", 13)]
    assert "Steps: run it on an empty file." in plan["tickets"][1]["description"]
    assert plan["pending_features"][0]["issue"] == 14
    # seen issues are not taken twice
    assert env.fresh() == []
    assert len(env.plan("alpha")["tickets"]) == 3

    url = env.open_pr(a, 1)
    env.fresh()
    assert env.gh.called("pr_edit_body") == []  # ticket 1 did not come from an issue
    env.merge(a, url)
    env.fresh()
    bug = env.board.find(stage="build")[1]
    assert bug["title"] == "Crash on empty file" and "ticket:2" in bug["tags"]
    url = env.open_pr(bug, 2)
    env.fresh()
    assert env.gh.prs[url]["body"] == "## Summary\n\nFixes #12\n"
    env.fresh()
    env.fresh()
    assert len(env.gh.called("pr_edit_body")) == 1


def test_fixes_line_is_not_added_twice_and_survives_a_github_failure(env):
    env.project("alpha", tickets=[])
    env.gh.issues["alpha"] = [{"number": 3, "title": "Typo", "body": "", "labels": ["agent"]}]
    env.fresh()
    (task,) = env.board.find(stage="build")
    url = env.open_pr(task, 1)
    env.gh.fail = lambda method, args: method == "pr_edit_body"
    notes = env.fresh()
    assert len(notes) == 1 and "PR ready" in notes[0]
    env.gh.fail = None
    assert env.fresh() == []
    assert env.gh.prs[url]["body"].endswith("Fixes #3\n")
    # a body that already says it is left alone
    env.gh.prs[url]["body"] = "x\n\nfixes #3"
    tasks = env.read("tasks.json")
    tasks["tasks"][task["id"]]["fixes_done"] = False
    (env.state / "tasks.json").write_text(json.dumps(tasks))
    env.fresh()
    assert env.gh.prs[url]["body"] == "x\n\nfixes #3"


def test_feature_from_issue_puts_fixes_on_the_last_ticket(env):
    env.project("alpha", tickets=[])
    env.gh.issues["alpha"] = [{"number": 14, "title": "Export", "body": "", "labels": ["agent", "feature"]}]
    env.fresh()
    (amend,) = env.board.find(stage="specify")
    url = env.open_pr(amend, 1)
    env.fresh()
    env.merge(amend, url)
    env.fresh()
    (plan_task,) = env.board.find(stage="plan")
    env.board.finish(plan_task["id"], json.dumps(FEATURE_PLAN), status="done")
    env.fresh()
    assert [t["issue"] for t in env.plan("alpha")["tickets"]] == [None, 14]


def test_issue_poll_failure_for_one_project_does_not_stop_the_other(env):
    env.project("alpha", tickets=[])
    env.project("beta", tickets=[])
    env.gh.issues["beta"] = [{"number": 1, "title": "Typo", "body": "", "labels": ["agent"]}]
    env.gh.fail = lambda method, args: method == "list_issues" and args == ("alpha",)
    assert env.fresh() == ["Issue #1 on beta taken as a small request: Typo."]
    assert len(env.board.find(project="beta")) == 1
