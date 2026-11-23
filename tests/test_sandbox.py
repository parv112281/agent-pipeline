"""The build-sandbox route (spec section 20.1) and importing upstream code (spec section 20.4)."""

import io
import json
import tarfile

import pytest

from pipeline import plan as plans
from pipeline import prompts
from pipeline.engine import extract_upstream
from pipeline.errors import PipelineError, PlanError

CHECKS = ["./mvnw -q verify", "npx ng test --watch=false --browsers=ChromeHeadlessNoSandbox"]
SANDBOX_PLAN = {
    "build_route": "build-sandbox",
    "checks": CHECKS,
    "tickets": [
        {"title": "Make the imported code pass the checks", "description": "Add mise.toml.",
         "acceptance": ["all checks pass"], "doc_sections": ["design 1"], "depends_on": []},
        {"title": "Angular booking screens", "description": "Rewrite in Angular.",
         "acceptance": ["ng tests pass"], "doc_sections": [], "depends_on": [1]},
    ],
}


def test_sandbox_plan_needs_checks():
    route, checks, tickets = plans.validate_plan(SANDBOX_PLAN)
    assert route == "build-sandbox" and checks == CHECKS and len(tickets) == 2
    for bad in (None, [], ["ok", ""], ["two\nlines"], "./mvnw verify"):
        plan = dict(SANDBOX_PLAN, checks=bad)
        with pytest.raises(PlanError, match="checks"):
            plans.validate_plan(plan)
    # the fixed routes ignore checks
    fixed = dict(SANDBOX_PLAN, build_route="build-python")
    assert plans.validate_plan(fixed)[1] is None


def test_sandbox_plan_sets_checks_and_build_prompt(env):
    env.project("lab", route=None)
    env.ok("plan", "lab")
    (task,) = env.board.find(stage="plan")
    env.board.finish(task["id"], json.dumps(SANDBOX_PLAN), status="done")
    assert "Plan created for lab: 2 build tickets" in env.fresh()[0]
    project = env.read("projects.json")["projects"]["lab"]
    assert project["build_route"] == "build-sandbox" and project["checks"] == CHECKS
    (build,) = env.board.find(stage="build")
    assert build["agent"] == "build-sandbox"
    prompt = build["prompt"]
    assert "`./mvnw -q verify`" in prompt and "mise exec --" in prompt and "All of them must pass" in prompt
    assert "no Docker" in prompt and "WebFetch" in prompt
    assert "That is the only command you can run" not in prompt


def test_fixed_route_build_prompt_allows_inspection(env):
    ticket = {"n": 1, "title": "T", "description": "D", "acceptance": ["a"], "doc_sections": [], "kind": "build"}
    prompt = prompts.build("demo", ticket, "build-python", [])
    assert "`pytest`" in prompt and "Read-only inspection" in prompt
    assert "That is the only command you can run" not in prompt
    assert "mise" not in prompt


def test_specify_prompt_offers_routes_and_upstream():
    plain = prompts.specify("p", "idea")
    assert "holds only a README" in plain and "`build-sandbox`" in plain and "mise.toml" in plain
    assert "never run or applied" in plain and "permissively licensed" in plain
    imported = prompts.specify("p", "idea", has_upstream=True)
    assert "UPSTREAM.md" in imported and "holds only a README" not in imported


def test_plan_amend_passes_existing_checks():
    prompt = prompts.plan_amend("p", "feature", [], "build-sandbox", CHECKS)
    assert "uses the route `build-sandbox`" in prompt and "`./mvnw -q verify`" in prompt


def test_onboard_with_checks_and_guess(env):
    env.gh.add_repo("javaapp", {"pom.xml": "<project/>", "src/Main.java": ""})
    out = env.ok("onboard", "javaapp")
    assert "build-sandbox (guessed from the files)" in out and "--check" in out
    out = env.ok("onboard", "javaapp", "--check", "./mvnw -q verify", "--check", "helm lint chart")
    project = env.read("projects.json")["projects"]["javaapp"]
    assert project["build_route"] == "build-sandbox" and project["checks"] == ["./mvnw -q verify", "helm lint chart"]
    assert "Checks: `./mvnw -q verify`; `helm lint chart`." in out and "already on the board" in out
    # the onboarding task was queued by the first call, before the checks were known
    (task,) = env.board.find(stage="onboard")
    assert "does not know yet how this project is tested" in task["prompt"]
    assert "`./mvnw -q verify`" in prompts.onboard("javaapp", "build-sandbox", project["checks"])
    # no "add a test runner" ticket: the checks are the tests
    assert not (env.state / "javaapp" / "plan.json").exists() or env.plan("javaapp")["tickets"] == []
    code, _, err = env.run("onboard", "javaapp", "--route", "build", "--check", "x")
    assert code == 1 and "only applies to the build-sandbox route" in err


def test_angular_with_package_json_guesses_sandbox(env):
    env.gh.add_repo("ngapp", {"package.json": "{}", "angular.json": "{}"})
    assert "build-sandbox (guessed" in env.ok("onboard", "ngapp")


UPSTREAM = {"README.md": "# Hotel demo", "LICENSE": "Apache License", "booking/App.java": "class App {}",
            ".git/config": "should be dropped", ".github/workflows/maven.yml": "on: push"}


def test_import_creates_repo_and_opens_pr(env):
    env.gh.add_upstream("AxonIQ/hotel-demo", UPSTREAM)
    out = env.ok("import", "hotel-lab", "AxonIQ/hotel-demo@master")
    assert "Created the private repo" in out and "3 files from AxonIQ/hotel-demo@0123456" in out
    assert ("resolve_commit", "AxonIQ/hotel-demo", "master") in env.gh.calls
    assert env.gh.called("create_repo") == [("create_repo", "hotel-lab")]
    (commit,) = env.gh.called("commit_branch")
    assert commit[1:3] == ("hotel-lab", "import/axoniq-hotel-demo-0123456")
    files = env.gh.pushed[("hotel-lab", "import/axoniq-hotel-demo-0123456")]
    assert files["booking/App.java"] == "class App {}" and files["LICENSE"] == "Apache License"
    assert not any(name.startswith(".git") for name in files)
    assert "1 files) were left out" in files["UPSTREAM.md"]
    assert "0123456789abcdef0123456789abcdef01234567" in files["UPSTREAM.md"] and "Apache-2.0" in files["UPSTREAM.md"]
    (pr,) = env.gh.called("pr_create")
    assert pr[1] == "hotel-lab" and "specify hotel-lab" in pr[4] and ".github/workflows/" in pr[4]
    assert env.read("projects.json")["projects"]["hotel-lab"]["idea"] == "hotel-lab"
    assert env.board.tasks == {}


def test_import_refuses_bad_licence_missing_repo_and_bad_spec(env):
    env.gh.add_upstream("someone/nolicence", UPSTREAM, license=None)
    env.gh.add_upstream("someone/gpl", UPSTREAM, license="GPL-3.0")
    for spec, message in (("someone/nolicence", "has no licence"), ("someone/gpl", "has GPL-3.0"),
                          ("someone/ghost", "no public GitHub repo"), ("not-a-repo", "owner/repo")):
        code, _, err = env.run("import", "lab", spec)
        assert code == 1 and message in err, err
    assert env.gh.called("create_repo") == []


def test_import_refuses_second_import(env):
    env.gh.add_upstream("AxonIQ/hotel-demo", UPSTREAM)
    env.project("hotel-lab", route=None, files={"UPSTREAM.md": "# Upstream"})
    code, _, err = env.run("import", "hotel-lab", "AxonIQ/hotel-demo")
    assert code == 1 and "already holds imported code" in err
    assert env.gh.called("commit_branch") == []


def test_specify_after_import_mentions_upstream(env):
    env.gh.repos["ideas"]["files"]["ideas/hotel-lab/idea.md"] = "# Hotel lab"
    env.project("hotel-lab", route=None, files={"UPSTREAM.md": "# Upstream"})
    for name in ("docs/requirements.md", "docs/design.md"):
        del env.gh.repos["hotel-lab"]["files"][name]
        (env.root / "repos" / "hotel-lab" / name).unlink()
    env.ok("specify", "hotel-lab")
    (task,) = env.board.find(stage="specify")
    assert "UPSTREAM.md" in task["prompt"]


def _tarball(tmp_path, members):
    path = tmp_path / "t.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, text in members:
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_extract_upstream_drops_top_folder_and_git(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "README.md").write_text("old")
    tar = _tarball(tmp_path, [("top/README.md", "new"), ("top/a/b.txt", "b"), ("top/.git/HEAD", "x"),
                              ("top/.github/workflows/ci.yml", "on: push"), ("top/.github/dependabot.yml", "v: 2")])
    assert extract_upstream(tar, dest) == (3, 1)
    assert not (dest / ".github/workflows").exists() and (dest / ".github/dependabot.yml").is_file()
    assert (dest / "README.md").read_text() == "new" and (dest / "a/b.txt").read_text() == "b"
    assert not (dest / ".git").exists()


def test_extract_upstream_blocks_escaping_paths(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    tar = _tarball(tmp_path, [("top/../../evil.txt", "x")])
    with pytest.raises(Exception):
        extract_upstream(tar, dest)
    assert not (tmp_path / "evil.txt").exists()


def test_git_error_names_the_rejected_ref(tmp_path):
    from pipeline.errors import GitHubError
    from pipeline.github import GitHub

    script = tmp_path / "fakegit"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'To https://github.com/o/r.git' >&2\n"
        "echo ' ! [remote rejected] b -> b (refusing to allow an OAuth App to create or update workflow "
        "`.github/workflows/maven.yml` without `workflow` scope)' >&2\n"
        "echo \"error: failed to push some refs to 'https://github.com/o/r.git'\" >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    with pytest.raises(GitHubError, match="without `workflow` scope"):
        GitHub("o", gh=str(script), git=str(script))._run([str(script), "push", "origin", "b"])
