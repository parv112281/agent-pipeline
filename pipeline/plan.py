"""Plan files: parsing the plan agent's answer and ordering tickets."""

import json
import re

from .errors import PlanError

BUILD_ROUTES = ("build", "build-python", "build-sandbox")
# The route whose test commands come from the project (its "checks") instead of a fixed allow-list.
SANDBOX_ROUTE = "build-sandbox"
FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.S)


def extract_json(text):
    """Find the plan object in an answer that may carry code fences or prose."""
    text = (text or "").strip()
    if not text:
        raise PlanError("the answer is empty")
    candidates = [text] + [m.strip() for m in FENCE_RE.findall(text)]
    found = []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    if not found:
        decoder = json.JSONDecoder()
        start = text.find("{")
        while start != -1:
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError:
                start = text.find("{", start + 1)
                continue
            if isinstance(value, dict):
                found.append(value)
            start = text.find("{", end)
    for value in found:
        if "tickets" in value:
            return value
    if found:
        return found[0]
    raise PlanError("no JSON object found in the answer")


def _strings(value, what, allow_empty):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise PlanError(f"{what} must be a list of non-empty strings")
    if not value and not allow_empty:
        raise PlanError(f"{what} must not be empty")
    return [x.strip() for x in value]


def validate_checks(value):
    """The check commands of a build-sandbox project: a non-empty list of one-line commands."""
    checks = _strings(value, "checks", allow_empty=False)
    if any("\n" in c for c in checks):
        raise PlanError("checks must be one-line commands")
    return checks


def validate_plan(obj):
    """Strict check of the plan object. Returns (build_route, checks, tickets).

    checks is None for the fixed routes, and the project's check commands for build-sandbox.
    """
    if not isinstance(obj, dict):
        raise PlanError("the plan is not a JSON object")
    route = obj.get("build_route")
    if route not in BUILD_ROUTES:
        raise PlanError('build_route must be "build", "build-python" or "build-sandbox"')
    checks = validate_checks(obj.get("checks")) if route == SANDBOX_ROUTE else None
    raw = obj.get("tickets")
    if not isinstance(raw, list) or not raw:
        raise PlanError("tickets must be a non-empty list")
    tickets = []
    for pos, item in enumerate(raw, start=1):
        where = f"ticket {pos}"
        if not isinstance(item, dict):
            raise PlanError(f"{where} is not an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PlanError(f"{where}: title must be a non-empty string")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise PlanError(f"{where}: description must be a non-empty string")
        acceptance = _strings(item.get("acceptance"), f"{where}: acceptance", allow_empty=False)
        sections = _strings(item.get("doc_sections", []), f"{where}: doc_sections", allow_empty=True)
        deps = item.get("depends_on", [])
        if not isinstance(deps, list):
            raise PlanError(f"{where}: depends_on must be a list")
        clean = []
        for dep in deps:
            if isinstance(dep, bool) or not isinstance(dep, int):
                raise PlanError(f"{where}: depends_on must hold ticket positions (whole numbers)")
            if dep < 1 or dep > len(raw):
                raise PlanError(f"{where}: depends_on refers to ticket {dep}, which does not exist")
            if dep == pos:
                raise PlanError(f"{where}: a ticket cannot depend on itself")
            if dep not in clean:
                clean.append(dep)
        tickets.append(
            {
                "title": title.strip(),
                "description": description.strip(),
                "acceptance": acceptance,
                "doc_sections": sections,
                "depends_on": clean,
            }
        )
    _check_acyclic(tickets)
    return route, checks, tickets


def _check_acyclic(tickets):
    state = {}

    def visit(pos, trail):
        if state.get(pos) == "done":
            return
        if state.get(pos) == "open":
            raise PlanError(f"depends_on forms a cycle through ticket {pos}")
        state[pos] = "open"
        for dep in tickets[pos - 1]["depends_on"]:
            visit(dep, trail + [pos])
        state[pos] = "done"

    for pos in range(1, len(tickets) + 1):
        visit(pos, [])


def make_ticket(plan, title, description, acceptance=None, doc_sections=None, depends_on=None, kind="build", issue=None):
    number = plan["next_n"]
    plan["next_n"] = number + 1
    return {
        "n": number,
        "title": title,
        "description": description,
        "acceptance": list(acceptance or []),
        "doc_sections": list(doc_sections or []),
        "depends_on": list(depends_on or []),
        "kind": kind,
        "status": "todo",
        "task_id": None,
        "pr_url": None,
        "issue": issue,
    }


def add_planned(plan, tickets, kind, replace):
    """Add validated plan tickets. Positions in depends_on become ticket numbers.

    With replace, tickets that have not started are dropped first; numbers are
    never reused, so old board tasks cannot be mistaken for new tickets.
    """
    if replace:
        plan["tickets"] = [t for t in plan["tickets"] if t["status"] != "todo"]
    first = plan["next_n"]
    added = []
    for item in tickets:
        deps = [first + pos - 1 for pos in item["depends_on"]]
        added.append(
            make_ticket(plan, item["title"], item["description"], item["acceptance"], item["doc_sections"], deps, kind)
        )
    plan["tickets"].extend(added)
    plan["finished_notified"] = False
    return added


def add_direct(plan, ticket):
    """Bug: ahead of every ticket not yet started. Anything else: at the end."""
    tickets = plan["tickets"]
    if ticket["kind"] == "bug":
        index = next((i for i, t in enumerate(tickets) if t["status"] == "todo"), len(tickets))
        tickets.insert(index, ticket)
    else:
        tickets.append(ticket)
    plan["finished_notified"] = False


def find_ticket(plan, number):
    for ticket in plan["tickets"]:
        if ticket["n"] == number:
            return ticket
    return None


def next_eligible(plan):
    """First todo ticket in list order whose dependencies are all done."""
    done = {t["n"] for t in plan["tickets"] if t["status"] == "done"}
    for ticket in plan["tickets"]:
        if ticket["status"] == "todo" and all(dep in done for dep in ticket["depends_on"]):
            return ticket
    return None
