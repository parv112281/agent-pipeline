"""The happy path as a sequence of ticks, and idempotence of tick."""

import json

from conftest import OWNER

PLAN = {
    "build_route": "build-python",
    "tickets": [
        {"title": "Project skeleton", "description": "Layout and pytest.", "acceptance": ["pytest passes"],
         "doc_sections": ["design 2"], "depends_on": []},
        {"title": "Read EXIF dates", "description": "Parse dates.", "acceptance": ["dates parsed"],
         "doc_sections": ["requirements 1.1"], "depends_on": [1]},
        {"title": "Move files", "description": "Move into folders.", "acceptance": ["files moved"],
         "doc_sections": [], "depends_on": [1, 2]},
    ],
}


def quiet(env):
    """Several ticks with no change: nothing new is notified or queued."""
    tasks = len(env.board.tasks)
    for _ in range(3):
        assert env.fresh() == []
    assert len(env.board.tasks) == tasks


def test_idea_to_finished_project(env):
    raw = "a tool that sorts my photos into folders by date -- quickly"
    out = env.ok("idea", "a tool that sorts my photos", "into folders by date -- quickly")
    assert "queued" in out
    (flesh,) = env.board.find(stage="flesh-out")
    assert flesh["agent"] == "flesh-out"
    assert set(flesh["tags"]) == {"repo:ideas", "stage:flesh-out", "project:ideas"}
    assert raw in flesh["prompt"]
    assert "ideas/<slug>/raw.md" in flesh["prompt"] and "## Open questions" in flesh["prompt"]
    assert env.gh.called("clone")[0][1] == "ideas"
    assert ("set_identity", "ideas", "Parv", "6843133+parv112281@users.noreply.github.com") in env.gh.calls
    quiet(env)

    # the agent finishes; the board opens the PR
    url = env.open_pr(flesh, 1, cost=0.42)
    notes = env.tick()
    assert len(notes) == 1
    assert notes[0]["text"].startswith("PR ready (flesh-out)")
    assert url in notes[0]["text"] and "Cost of the run" not in notes[0]["text"]
    # unacknowledged notifications are printed again, but never duplicated
    assert env.tick() == notes
    assert env.ok("ack", notes[0]["id"]) == "ok"
    quiet(env)

    # Parv merges: the project repo is created and specify is queued
    idea = "# Photo sorter\n\n## Problem\nPhotos are a mess."
    env.merge(flesh, url, {"ideas/photo-sorter/raw.md": raw, "ideas/photo-sorter/idea.md": idea})
    notes = env.fresh()
    assert len(notes) == 1 and "Idea photo-sorter approved" in notes[0]
    assert env.gh.called("create_repo") == [("create_repo", "photo-sorter")]
    assert env.gh.called("protect_branch") == [("protect_branch", "photo-sorter", "main")]
    assert (env.root / "repos" / "photo-sorter" / ".git").is_dir()
    project = env.read("projects.json")["projects"]["photo-sorter"]
    assert project == {"repo": "photo-sorter", "default_branch": "main", "build_route": None,
                       "idea": "photo-sorter", "onboarded": False, "seen_issues": []}
    (spec,) = env.board.find(stage="specify")
    assert spec["agent"] == "specify"
    assert set(spec["tags"]) == {"repo:photo-sorter", "stage:specify", "project:photo-sorter"}
    assert idea in spec["prompt"]
    quiet(env)
    assert len(env.gh.called("create_repo")) == 1

    url = env.open_pr(spec, 1)
    assert "PR ready (specify, photo-sorter)" in env.fresh()[0]
    env.merge(spec, url, {"docs/requirements.md": "# Requirements", "docs/design.md": "# Design"})
    assert env.fresh() == []
    (plan_task,) = env.board.find(stage="plan")
    assert plan_task["agent"] == "plan" and plan_task["status"] == "queued"
    assert not plan_task["prompt"].startswith("-")
    assert "only one JSON object" in plan_task["prompt"]
    quiet(env)

    # the plan route opens no PR: the answer lands in Done
    answer = "Here is the plan.\n```json\n" + json.dumps(PLAN, indent=1) + "\n```\nGood luck."
    env.board.finish(plan_task["id"], answer, status="done")
    notes = env.fresh()
    assert len(notes) == 1
    assert "Plan created for photo-sorter: 3 build tickets" in notes[0]
    assert "1. Project skeleton" in notes[0] and "3. Move files" in notes[0]
    plan = env.plan("photo-sorter")
    assert [t["n"] for t in plan["tickets"]] == [1, 2, 3]
    assert plan["tickets"][2]["depends_on"] == [1, 2]
    assert plan["tickets"][0]["kind"] == "build"
    assert env.read("projects.json")["projects"]["photo-sorter"]["build_route"] == "build-python"
    (build1,) = env.board.find(stage="build")
    assert build1["agent"] == "build-python" and "ticket:1" in build1["tags"]
    assert build1["title"] == "Project skeleton"
    assert "pytest passes" in build1["prompt"] and "PIPELINE_BLOCKED" in build1["prompt"]
    assert "docs/requirements.md" in build1["prompt"]
    assert env.ticket("photo-sorter", 1)["status"] == "queued"
    quiet(env)

    # tickets go one at a time, each after the merge of the one before
    env.board.move(build1["id"], "running")
    quiet(env)
    url = env.open_pr(build1, 2)
    assert "PR ready (build, photo-sorter #1): Project skeleton" in env.fresh()[0]
    assert env.ticket("photo-sorter", 1)["status"] == "review"
    quiet(env)
    assert len(env.board.find(stage="build")) == 1
    env.merge(build1, url)
    assert env.fresh() == []
    assert env.ticket("photo-sorter", 1)["status"] == "done"
    assert env.ticket("photo-sorter", 1)["pr_url"] == url
    builds = env.board.find(stage="build")
    assert [t["title"] for t in builds] == ["Project skeleton", "Read EXIF dates"]
    quiet(env)

    url = env.open_pr(builds[1], 3)
    env.fresh()
    env.merge(builds[1], url)
    assert env.fresh() == []
    build3 = env.board.find(stage="build")[2]
    assert "ticket:3" in build3["tags"]
    url = env.open_pr(build3, 4)
    env.fresh()
    env.merge(build3, url)
    assert env.fresh() == ["Project photo-sorter finished: all 3 tickets are merged."]
    quiet(env)
    assert all(t["status"] == "done" for t in env.plan("photo-sorter")["tickets"])
    assert len(env.board.tasks) == 6


def test_dependency_order_and_one_task_per_project(env):
    env.project("alpha", tickets=[("A", [], "todo"), ("B", [1], "todo"), ("C", [], "todo")])
    env.project("beta", tickets=[("X", [], "todo")])
    env.fresh()
    # one task per project, both projects served
    assert [t["title"] for t in env.board.find(stage="build")] == ["A", "X"]
    assert ("pull", "alpha") in env.gh.calls
    a = env.board.find(project="alpha")[0]
    # A is closed unmerged: B waits for it for good, C does not depend on it
    url = env.open_pr(a, 1)
    env.fresh()
    env.gh.prs[url]["state"] = "CLOSED"
    env.board.tasks[a["id"]]["tags"] += ["blocked", "pr-closed"]
    notes = env.fresh()
    assert len(notes) == 1 and notes[0].startswith("PR closed (build, alpha #1): A. The work is parked.")
    assert env.ticket("alpha", 1)["status"] == "blocked"
    assert [t["title"] for t in env.board.find(project="alpha")] == ["A", "C"]
    for _ in range(2):
        assert env.fresh() == []
    c = env.board.find(project="alpha")[1]
    url = env.open_pr(c, 2)
    env.fresh()
    env.merge(c, url)
    assert env.fresh() == []
    assert env.ticket("alpha", 2)["status"] == "todo"
    assert len(env.board.find(project="alpha")) == 2


def test_closed_pr_reopened_and_merged_is_picked_up(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (a,) = env.board.find(stage="build")
    url = env.open_pr(a, 1)
    env.fresh()
    env.gh.prs[url]["state"] = "CLOSED"
    env.board.tasks[a["id"]]["tags"] += ["blocked", "pr-closed"]
    assert "PR closed" in env.fresh()[0]
    env.gh.prs[url].update(state="MERGED", merged=True)
    assert env.fresh() == ["Project alpha finished: all 1 tickets are merged."]
    task = env.board.tasks[a["id"]]
    assert task["status"] == "done" and "pr-closed" not in task["tags"]
    assert env.ticket("alpha", 1)["status"] == "done"


def test_checkout_on_wrong_branch_or_dirty_is_not_queued(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.gh.branch["alpha"] = "atb/old"
    notes = env.fresh()
    assert len(notes) == 1 and "on branch atb/old, not main" in notes[0]
    assert env.board.tasks == {}
    assert env.fresh() == []
    del env.gh.branch["alpha"]
    env.gh.dirty.add("alpha")
    assert "uncommitted changes" in env.fresh()[0]
    env.gh.dirty.clear()
    assert env.fresh() == []
    assert len(env.board.find(stage="build")) == 1


def test_crash_after_creating_task_does_not_duplicate(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    # plan.json was not saved: the ticket still says todo
    plan = env.plan("alpha")
    plan["tickets"][0].update(status="todo", task_id=None)
    (env.state / "alpha" / "plan.json").write_text(json.dumps(plan))
    env.fresh()
    (task,) = env.board.find(stage="build")
    assert env.ticket("alpha", 1)["task_id"] == task["id"]
    assert env.ticket("alpha", 1)["status"] == "queued"


def test_tasks_without_stage_tag_are_ignored(env):
    env.board.create_task({"title": "by hand", "prompt": "x", "agent": "build", "tags": ["repo:alpha"]})
    task = next(iter(env.board.tasks.values()))
    env.board.finish(task["id"], "boom", error=True)
    assert env.fresh() == []
    assert env.board.patches == []
    assert OWNER
