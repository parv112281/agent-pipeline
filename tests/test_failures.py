"""Things that go wrong: bad plans, errors, blocked runs, closed PRs."""

import json

import pytest

from pipeline import plan as plans
from pipeline.errors import PlanError

GOOD = {"build_route": "build", "tickets": [
    {"title": "Skeleton", "description": "d", "acceptance": ["a"], "doc_sections": [], "depends_on": []},
    {"title": "Second", "description": "d", "acceptance": ["a"], "doc_sections": ["design 1"], "depends_on": [1]},
]}


def variant(**changes):
    data = json.loads(json.dumps(GOOD))
    for key, value in changes.items():
        if key.startswith("t2_"):
            data["tickets"][1][key[3:]] = value
        else:
            data[key] = value
    return data


def test_extract_json_tolerates_fences_and_prose():
    assert plans.extract_json(json.dumps(GOOD)) == GOOD
    assert plans.extract_json("Sure!\n```json\n" + json.dumps(GOOD) + "\n```\nDone.") == GOOD
    assert plans.extract_json("The plan {x} is: " + json.dumps(GOOD) + " and that is it {") == GOOD
    assert plans.extract_json('{"note": 1}\n' + json.dumps(GOOD)) == GOOD
    with pytest.raises(PlanError):
        plans.extract_json("no json here")
    with pytest.raises(PlanError):
        plans.extract_json("")


@pytest.mark.parametrize("bad", [
    [1, 2],
    variant(build_route="rust"),
    variant(tickets=[]),
    variant(tickets="x"),
    variant(tickets=["x"]),
    variant(t2_title=""),
    variant(t2_title=5),
    variant(t2_description=None),
    variant(t2_acceptance=[]),
    variant(t2_acceptance="works"),
    variant(t2_acceptance=[1]),
    variant(t2_doc_sections=[3]),
    variant(t2_depends_on="1"),
    variant(t2_depends_on=[0]),
    variant(t2_depends_on=[3]),
    variant(t2_depends_on=[2]),
    variant(t2_depends_on=[True]),
    variant(t2_depends_on=[1.0]),
    {"build_route": "build", "tickets": [
        {"title": "a", "description": "d", "acceptance": ["a"], "depends_on": [2]},
        {"title": "b", "description": "d", "acceptance": ["a"], "depends_on": [1]}]},
])
def test_validate_plan_rejects(bad):
    with pytest.raises(PlanError):
        plans.validate_plan(bad)


def test_validate_plan_accepts():
    route, checks, tickets = plans.validate_plan(GOOD)
    assert route == "build" and checks is None and [t["depends_on"] for t in tickets] == [[], [1]]


@pytest.mark.parametrize("answer", [
    "I could not decide.",
    json.dumps(variant(build_route="make")),
    json.dumps(variant(t2_depends_on=[2])),
    "PIPELINE_BLOCKED: the design does not say which language",
])
def test_invalid_plan_blocks_the_plan_task(env, answer):
    env.project("alpha", route=None)
    env.ok("plan", "alpha")
    (task,) = env.board.find(stage="plan")
    env.board.finish(task["id"], answer, status="done")
    notes = env.fresh()
    assert len(notes) == 1 and notes[0].startswith("Blocked (plan, alpha)")
    assert "No tickets were created" in notes[0]
    task = env.board.tasks[task["id"]]
    assert task["status"] == "review" and "blocked" in task["tags"]
    assert not (env.state / "alpha" / "plan.json").exists() or env.plan("alpha")["tickets"] == []
    assert env.board.find(stage="build") == []
    for _ in range(2):
        assert env.fresh() == []
    # revise runs the plan again with the note; a good answer is then accepted
    env.ok("revise", task["id"][:8], "use python")
    task = env.board.tasks[task["id"]]
    assert task["status"] == "queued" and "blocked" not in task["tags"] and "use python" in task["prompt"]
    env.board.finish(task["id"], json.dumps(GOOD), status="done")
    assert "Plan created for alpha: 2 build tickets" in env.fresh()[0]
    assert len(env.board.find(stage="build")) == 1


def test_replanning_keeps_started_tickets_and_never_reuses_numbers(env):
    env.project("alpha", tickets=[("Old done", [], "done"), ("Old todo", [1], "todo")])
    env.board.paused = True
    engine = env.engine()
    plan = engine.plan("alpha")
    added = plans.add_planned(plan, plans.validate_plan(GOOD)[2], "build", replace=True)
    assert [t["n"] for t in plan["tickets"]] == [1, 3, 4]
    assert added[1]["depends_on"] == [3]


def test_error_in_review_is_blocked_and_notified_once(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], "Something long\nError: Reached max budget ($1)", error=True)
    notes = env.fresh()
    assert len(notes) == 1
    assert notes[0].startswith("Blocked (build, alpha #1): A. The run failed:")
    assert "Reached max budget ($1)" in notes[0] and "revise alpha#1" in notes[0]
    assert "blocked" in env.board.tasks[task["id"]]["tags"]
    assert env.ticket("alpha", 1)["status"] == "blocked"
    for _ in range(3):
        assert env.fresh() == []
    assert len(env.board.tasks) == 1


def test_pipeline_blocked_with_pr(env):
    env.project("alpha", tickets=[("A", [], "todo"), ("B", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    url = env.open_pr(task, 7, text="Tried.\nPIPELINE_BLOCKED: test_login fails, the API key format is unclear")
    notes = env.fresh()
    assert len(notes) == 1 and notes[0].startswith("Blocked (build, alpha #1): A.")
    assert "failing tests" in notes[0] and "the API key format is unclear" in notes[0] and url in notes[0]
    assert "blocked" in env.board.tasks[task["id"]]["tags"]
    assert env.fresh() == []
    # the project waits; nothing else is queued behind a blocked task
    assert len(env.board.find(stage="build")) == 1
    # Parv merges anyway: the pipeline moves on
    env.merge(task, url)
    env.fresh()
    assert env.ticket("alpha", 1)["status"] == "done"
    assert len(env.board.find(stage="build")) == 2


def test_needs_planning_hint_for_small_change(env):
    env.project("alpha", tickets=[])
    env.ok("request", "small", "alpha", "rewrite everything")
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], "Too big.\nPIPELINE_BLOCKED: needs planning", status="done")
    env.board.move(task["id"], "review")
    notes = env.fresh()
    assert 'send it again as "feature for alpha: ..."' in notes[0]


def test_done_without_pr_goes_back_to_review_blocked(env):
    env.project("alpha", tickets=[("A", [], "todo"), ("B", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], "Nothing to do.\n\n(no file changes, no PR opened)", status="done")
    notes = env.fresh()
    assert len(notes) == 1 and "ended without a PR" in notes[0]
    task = env.board.tasks[task["id"]]
    assert task["status"] == "review" and "blocked" in task["tags"]
    assert env.ticket("alpha", 1)["status"] == "blocked"
    for _ in range(2):
        assert env.fresh() == []
    assert len(env.board.find(stage="build")) == 1
    # revise without a PR runs it again from scratch with the note
    env.ok("revise", "alpha#1", "the", "file is src/a.py")
    task = env.board.tasks[task["id"]]
    assert task["status"] == "queued" and "blocked" not in task["tags"] and "revise" not in task["tags"]
    assert task["prompt"].endswith("the file is src/a.py")


def test_closed_pr_found_by_the_engine_in_done(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    url = env.open_pr(task, 1)
    env.fresh()
    env.gh.prs[url]["state"] = "CLOSED"
    env.board.move(task["id"], "done")
    env.fresh()
    task = env.board.tasks[task["id"]]
    assert task["status"] == "review" and {"blocked", "pr-closed"} <= set(task["tags"])
    assert "PR closed" in env.fresh()[0]
    assert env.fresh() == []
    # un-park the ticket by hand
    out = env.ok("revise", "alpha#1", "try again, smaller")
    assert "back in the plan" in out and "Queued now" in out
    tasks = env.board.find(stage="build")
    assert len(tasks) == 2 and "try again, smaller" in tasks[1]["prompt"]
    assert env.ticket("alpha", 1)["task_id"] == tasks[1]["id"]
    env.fresh()
    assert env.ticket("alpha", 1)["status"] == "queued"


def test_open_pr_dragged_to_done_is_moved_back_silently(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.open_pr(task, 1)
    env.fresh()
    env.board.move(task["id"], "done")
    assert env.fresh() == []
    assert env.board.tasks[task["id"]]["status"] == "review"
    assert env.ticket("alpha", 1)["status"] == "review"


def test_removed_card_parks_the_ticket(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    del env.board.tasks[task["id"]]
    notes = env.fresh()
    assert len(notes) == 1 and "is parked" in notes[0]
    assert env.ticket("alpha", 1)["status"] == "blocked"
    assert env.fresh() == [] and env.board.tasks == {}


def test_board_unreachable_still_prints_outbox(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.open_pr(task, 1)
    first = env.tick()
    assert len(first) == 1
    env.board.down = True
    code, out, err = env.run("tick")
    assert code == 0 and json.loads(out) == {"notifications": first}
    assert "unreachable" in err


def test_github_failure_on_one_task_does_not_stop_the_rest(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.project("beta", tickets=[("X", [], "todo")])
    env.fresh()
    a, x = env.board.find(stage="build")
    url_a = env.open_pr(a, 1)
    url_x = env.open_pr(x, 1)
    env.fresh()
    env.merge(a, url_a)
    env.merge(x, url_x)
    env.gh.fail = lambda method, args: "alpha" in " ".join(str(a) for a in args)
    notes = env.fresh()
    assert notes == ["Project beta finished: all 1 tickets are merged."]
    assert "alpha" in env.last_err
    assert env.ticket("alpha", 1)["status"] == "review"
    # GitHub is back: the stuck one is handled on a later tick
    env.gh.fail = None
    assert env.fresh() == ["Project alpha finished: all 1 tickets are merged."]


def test_invalid_state_file_keeps_tick_output_json(env):
    (env.state / "tasks.json").write_text("{broken")
    code, out, err = env.run("tick")
    assert code == 1 and json.loads(out) == {"notifications": []}
    assert "not valid JSON" in err


PR_FAILED = "Added it. Ran the tests: 4 passed.\n\n⚠ PR step failed: gh pr create failed: Post \"https://api.github.com/graphql\": net/http: TLS handshake timeout"


def test_failed_pr_step_is_recovered_by_the_engine(env):
    env.project("alpha", tickets=[("A", [], "todo"), ("B", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], PR_FAILED, error=True)
    notes = env.fresh()
    (call,) = env.gh.called("pr_create")
    assert call[1:3] == ("alpha", f"atb/{task['id']}")
    assert "Ran the tests: 4 passed." in call[4] and "TLS handshake timeout" in call[4]
    url = env.pr_url("alpha", 1)
    assert len(notes) == 1 and notes[0].startswith("PR ready (build, alpha #1): A.") and url in notes[0]
    assert "blocked" not in env.board.tasks[task["id"]]["tags"]
    assert env.ticket("alpha", 1)["status"] == "review"
    assert env.fresh() == []
    assert len(env.gh.called("pr_create")) == 1
    # the engine watches the PR itself, since the result does not name it
    env.merge(task, url)
    env.board.move(task["id"], "review")
    env.fresh()
    assert env.ticket("alpha", 1)["status"] == "done"


def test_failed_pr_step_is_retried_on_later_ticks(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], PR_FAILED, error=True)
    env.gh.fail = lambda method, args: method == "pr_for_branch"
    notes = env.fresh()
    assert len(notes) == 1 and notes[0].startswith("Blocked (build, alpha #1): A. The run failed:")
    assert env.ticket("alpha", 1)["status"] == "blocked"
    env.gh.fail = None
    notes = env.fresh()
    assert len(notes) == 1 and notes[0].startswith("PR ready (build, alpha #1): A.")
    assert env.ticket("alpha", 1)["status"] == "review"


def test_failed_pr_step_gives_up_after_some_tries(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], PR_FAILED, error=True)
    env.gh.fail = lambda method, args: method == "pr_for_branch"
    for _ in range(8):
        env.fresh()
    assert len(env.gh.called("pr_for_branch")) == 5


def test_failed_push_is_not_recovered(env):
    env.project("alpha", tickets=[("A", [], "todo")])
    env.fresh()
    (task,) = env.board.find(stage="build")
    env.board.finish(task["id"], "Added it.\n\n⚠ PR step failed: git push failed: rejected", error=True)
    notes = env.fresh()
    assert env.gh.called("pr_for_branch") == [] and env.gh.called("pr_create") == []
    assert notes[0].startswith("Blocked (build, alpha #1): A. The run failed:")
