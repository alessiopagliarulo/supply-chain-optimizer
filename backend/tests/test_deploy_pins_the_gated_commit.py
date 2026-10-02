"""The Render deploy must ship the commit the gate verified - not the branch tip.

Why this file exists
--------------------
``.github/workflows/deploy-render.yml`` resolves the commit under test into
``steps.commit.outputs.sha``, polls Model CI for a run against **that exact SHA**
and refuses on a missing, unfinished or failed verdict. Until issue #5, the
deploy then POSTed ``-d '{}'`` to Render's ``POST /v1/services/{id}/deploys``.
Render's API reference documents ``commitId`` as "The SHA of a specific Git
commit to deploy for a service. Defaults to the latest commit on the service's
connected branch." An empty body is therefore a request for the BRANCH TIP: the
workflow could verify commit A and ship commit B, whose gates never ran, under a
green tick.

The deploy is one step, "Deploy the gated commit to Render", whose ``run:``
script lives in the workflow file itself (GitHub protects that directory; see the
comment above the step). This file checks two things, and neither can be
satisfied by a comment:

1. THE WIRING, read from the workflow with every comment line removed: the step's
   ``SHA`` is bound to the same expression the Model CI gate polled on (not
   ``github.sha`` recomputed independently), no other step calls Render, the
   step cannot be skipped or allowed to fail, and both services are deployed.
2. THE BEHAVIOUR, by extracting the step's ``run:`` block and executing it under
   ``bash`` against a stubbed ``curl`` for each Render reply: every request body
   is exactly ``{"commitId": <gated SHA>}``; nothing is sent when the request
   cannot be built; a refusal, another commit or no commit fails; a 202 is
   confirmed through the deploy list; and a failure part-way never leaves the
   backend deploying while the frontend is left behind without cancelling it or
   saying PRODUCTION MAY BE SPLIT.

WHY THE YAML IS READ AS TEXT. PyYAML is not pinned in ``backend/requirements.txt``;
hanging a load-bearing assertion on an undeclared dependency is the gap
``test_dependency_pins.py`` exists to close. Plain text parsing is enough here.

This file carries NO ``pytestmark = pytest.mark.model_ci``: it is a
workflow-hygiene gate, not a model gate, so ``MODEL_CI_GATE_CENSUS`` is untouched.
``bash`` and ``jq`` are required (both ship on the GitHub-hosted Ubuntu runners
CI uses); a missing one fails loudly rather than skipping.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "deploy-render.yml"

GATE_STEP = "Require Model CI green for this commit"
DEPLOY_STEP = "Deploy the gated commit to Render (backend, then frontend)"

#: Every Render service this workflow deploys, in deploy order. If a service is
#: genuinely added, removed or re-created, update this in the same commit.
BACKEND = "srv-d98ru31o3t8c73ed9dig"
FRONTEND = "srv-d98ru9ss728c73c85bqg"
EXPECTED_SERVICE_IDS = [BACKEND, FRONTEND]

GATED_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
API_KEY = "rnd_TESTKEYDONOTPRINT"


# ── Wiring: the workflow, comments removed ────────────────────────────────────


def _code_lines(text: str) -> str:
    """The text with every comment-only line dropped, so prose can neither
    satisfy nor trip an assertion."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


_STEP_RE = re.compile(r"^(?P<indent>[ \t]*)- name: (?P<name>.+)$", re.MULTILINE)


def _steps() -> dict[str, tuple[int, str]]:
    """``{step name: (key indent, block)}``, comments removed."""
    text = _code_lines(WORKFLOW.read_text(encoding="utf-8"))
    text = text[text.index("steps:"):]
    marks = list(_STEP_RE.finditer(text))
    assert marks, f"no steps found in {WORKFLOW} - has the workflow been restructured?"
    return {
        m.group("name").strip(): (
            len(m.group("indent")) + 2,
            text[m.start():marks[i + 1].start() if i + 1 < len(marks) else len(text)],
        )
        for i, m in enumerate(marks)
    }


def _step(name: str) -> tuple[int, str]:
    steps = _steps()
    assert name in steps, (
        f"the step {name!r} is gone from {WORKFLOW}. If it was renamed, update this "
        "test; do not leave this guard dark - it ties the Model CI gate to the "
        "commit that ships (issue #5)."
    )
    return steps[name]


def _step_keys(name: str) -> set[str]:
    indent, block = _step(name)
    return set(re.findall(rf"^ {{{indent}}}([A-Za-z_-]+):", block, re.MULTILINE)) | {"name"}


def _binding(block: str, key: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(key)}:\s*\$\{{\{{\s*(?P<expr>.+?)\s*\}}\}}\s*$", block, re.MULTILINE
    )
    return match.group("expr") if match else None


def _deploy_script() -> str:
    """The deploy step's ``run: |`` block, dedented, comments included (it is
    executed exactly as Actions would run it)."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {DEPLOY_STEP}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    key_indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= key_indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def test_the_deploy_step_deploys_exactly_the_two_known_services_in_order():
    """A service silently dropped is frozen at an old release with nothing going
    red; the order matters because the backend is deployed first."""
    script = _code_lines(_deploy_script())
    match = re.search(r"^services=\((?P<ids>[^)]*)\)", script, re.MULTILINE)
    assert match, "the deploy script no longer declares its services=(...) list"
    assert match.group("ids").split() == EXPECTED_SERVICE_IDS, (
        f"the deploy script deploys {match.group('ids').split()}, expected "
        f"{EXPECTED_SERVICE_IDS} (backend first). If a Render service was deliberately "
        "added, removed or re-created, update EXPECTED_SERVICE_IDS in the same commit."
    )


def test_no_other_step_calls_render():
    """Every call to Render goes through the step the behaviour tests exercise."""
    for name, (_, block) in _steps().items():
        if name != DEPLOY_STEP:
            assert "api.render.com" not in block, (
                f"the step {name!r} calls Render's API outside the tested deploy step."
            )


def test_the_deployed_commit_is_the_one_the_model_ci_gate_checked():
    """THE LINK BETWEEN THE GATE AND THE DEPLOY.

    Sending *a* commitId is not enough; it must be THE commit Model CI was polled
    for. A deploy bound to ``github.sha`` would pass every behaviour test below
    and still ship a commit the gate never saw.
    """
    gated = _binding(_step(GATE_STEP)[1], "SHA")
    assert gated == "steps.commit.outputs.sha", (
        f"the Model CI gate now polls on {gated!r} rather than steps.commit.outputs.sha; "
        "this test can no longer tell which commit was verified."
    )
    binding = _binding(_step(DEPLOY_STEP)[1], "SHA")
    assert binding == gated, (
        f"the deploy takes its commit from {binding!r}, but the Model CI gate verified "
        f"{gated!r}. A commit resolved independently of the gate is exactly how "
        "issue #5 reopens."
    )


def test_the_deploy_step_cannot_be_skipped_or_allowed_to_fail():
    """A red deploy script under ``continue-on-error`` leaves a green job over an
    unverified release; an ``if:`` can skip it. Neither belongs on this step."""
    keys = _step_keys(DEPLOY_STEP)
    for key in ("if", "continue-on-error", "shell"):
        assert key not in keys, f"the deploy step has `{key}:`, which can hide or skip a failure"
    workflow = _code_lines(WORKFLOW.read_text(encoding="utf-8"))
    assert "continue-on-error" not in workflow, "the deploy workflow must not tolerate failures"


def test_the_script_never_uses_curl_fail():
    """``curl -f`` exits 22 on an HTTP error and throws away Render's reply,
    including the text explaining a refusal. Any spelling of the flag counts."""
    script = _code_lines(_deploy_script()).replace("\\\n", " ")
    for call in re.findall(r"\bcurl\b[^\n]*|\bargs(?:\+)?=\([^)]*\)", script):
        assert not re.search(r"(?<![^\s(])(-[A-Za-z]*f[A-Za-z]*|--fail\S*)(?!\S)", call), (
            f"the deploy script passes curl a fail flag: {call.strip()!r}. Capture the "
            "body and check the status code explicitly instead."
        )


# ── Behaviour: the real run block, against a stubbed curl ─────────────────────

# A stand-in for curl that honours the flags the script relies on (-o, -w, -X, -d
# and friends, -f and friends), records every request, and answers from a plan:
# {"POST <srv>" | "GET <srv>" | "CANCEL <srv>": [reply, ...]}, each reply
# {"code": int, "body": str, "exit": int}; replies are used in order and the
# last one repeats. "exit" simulates a transport failure (curl prints 000).
_STUB = r'''
import json, os, re, sys

args = sys.argv[1:]
out = fmt = body = None
method = "GET"
fail = False
url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-X", "-H"):
        val = args[i + 1]
        if a == "-o": out = val
        if a == "-w": fmt = val
        if a == "-X": method = val
        i += 2; continue
    if a in ("-d", "--data", "--data-raw", "--data-binary", "--json"):
        body = args[i + 1]
        if method == "GET": method = "POST"
        i += 2; continue
    if a in ("--fail", "--fail-with-body") or (a.startswith("-") and not a.startswith("--") and "f" in a):
        fail = True
    elif not a.startswith("-"):
        url = a
    i += 1

srv = re.search(r"srv-[A-Za-z0-9]+", url or "").group(0)
kind = "CANCEL" if url.endswith("/cancel") else method
key = f"{kind} {srv}"
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"key": key, "url": url, "body": body}) + "\n")

plan = json.load(open(os.environ["STUB_PLAN"]))
state_path = os.environ["STUB_PLAN"] + ".state"
state = json.load(open(state_path)) if os.path.exists(state_path) else {}
replies = plan.get(key) or [{"code": 500, "body": "{\"message\":\"no stub for " + key + "\"}"}]
n = state.get(key, 0)
state[key] = n + 1
json.dump(state, open(state_path, "w"))
reply = replies[min(n, len(replies) - 1)]

if reply.get("exit"):
    sys.stderr.write("curl: (7) simulated network failure\n")
    if fmt: sys.stdout.write(fmt.replace("%{http_code}", "000"))
    sys.exit(reply["exit"])
code = reply["code"]
if fail and code >= 400:
    sys.stderr.write(f"curl: (22) The requested URL returned error: {code}\n")
    sys.exit(22)
if out:
    with open(out, "w") as f: f.write(reply.get("body", ""))
else:
    sys.stdout.write(reply.get("body", ""))
if fmt:
    sys.stdout.write(fmt.replace("%{http_code}", str(code)))
'''


@dataclass
class Run:
    code: int
    output: str
    requests: list[dict]

    def keys(self) -> list[str]:
        return [r["key"] for r in self.requests]


def _sandbox_path(tmp_path: Path, *, with_jq: bool) -> str:
    """A PATH holding the stub curl and only the tools the script uses."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(f"#!{sys.executable}\n{_STUB}", encoding="utf-8")
    stub.chmod(0o755)
    tools = ["mktemp", "rm", "cat", "cp", "tr", "date", "sleep"] + (["jq"] if with_jq else [])
    for tool in tools:
        found = shutil.which(tool)
        assert found, f"{tool} is required to exercise the deploy script"
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(found)
    return str(bin_dir)


def _reply(code: int, body: str = "", exit: int = 0) -> dict:
    return {"code": code, "body": body, "exit": exit}


def _deploy_body(commit: str | None, deploy_id: str) -> str:
    body: dict = {"id": deploy_id, "status": "created"}
    if commit is not None:
        body["commit"] = {"id": commit, "message": "m"}
    return json.dumps(body)


def _ok(srv: str, commit: str = GATED_SHA) -> list[dict]:
    return [_reply(201, _deploy_body(commit, f"dep-{srv[-4:]}"))]


def _deploy(tmp_path: Path, plan: dict, *, sha: str = GATED_SHA, with_jq: bool = True) -> Run:
    bash = shutil.which("bash")
    assert bash, "bash is required to exercise the deploy script"
    script = tmp_path / "deploy.sh"
    script.write_text(_deploy_script(), encoding="utf-8")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    Path(str(plan_file) + ".state").unlink(missing_ok=True)
    log = tmp_path / "requests.jsonl"
    log.write_text("", encoding="utf-8")
    env = {
        "PATH": _sandbox_path(tmp_path, with_jq=with_jq),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "SHA": sha,
        "RENDER_API_KEY": API_KEY,
        "RENDER_LOOKUP_SLEEP": "0",
        "STUB_LOG": str(log),
        "STUB_PLAN": str(plan_file),
    }
    proc = subprocess.run([bash, str(script)], env=env, capture_output=True, text=True, timeout=60)
    requests = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    return Run(proc.returncode, proc.stdout + proc.stderr, requests)


def _both_ok() -> dict:
    return {f"POST {BACKEND}": _ok(BACKEND), f"POST {FRONTEND}": _ok(FRONTEND)}


def _iso(offset_s: int = 0, suffix: str = ".123456Z") -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + offset_s)) + suffix


def _listing(*deploys: dict) -> str:
    return json.dumps([{"deploy": d, "cursor": "c"} for d in deploys])


def test_both_requests_name_exactly_the_gated_commit(tmp_path):
    """THE ORIGINAL DEFECT, checked on the wire: whatever the script's spelling
    (``-d '{}'``, ``--data "{}"``, ``-d ''``), every body Render receives must be
    ``{"commitId": <gated SHA>}`` and nothing else, backend first."""
    run = _deploy(tmp_path, _both_ok())
    assert run.keys() == [f"POST {BACKEND}", f"POST {FRONTEND}"], run.requests
    for request in run.requests:
        assert request["url"].startswith("https://api.render.com/v1/services/"), request["url"]
        assert json.loads(request["body"] or "null") == {"commitId": GATED_SHA}, (
            f"Render was sent {request['body']!r}. Without commitId set to the gated SHA, "
            "Render deploys the latest commit on the branch, gated or not (issue #5)."
        )
    assert run.code == 0, run.output


def test_a_confirmed_deploy_prints_each_deploy_id_and_commit(tmp_path):
    run = _deploy(tmp_path, _both_ok())
    assert run.code == 0, run.output
    for srv in (BACKEND, FRONTEND):
        line = [ln for ln in run.output.splitlines() if f"dep-{srv[-4:]}" in ln]
        assert line and GATED_SHA in line[0], (
            "a successful deploy must print Render's deploy id next to the commit Render "
            f"reports, so the run log says which build it started. Output:\n{run.output}"
        )


def test_nothing_is_sent_when_jq_is_missing(tmp_path):
    """Without jq the request body cannot be built. It must never collapse to an
    empty body on the wire, which Render reads as "deploy the branch tip"."""
    run = _deploy(tmp_path, _both_ok(), with_jq=False)
    assert run.code != 0, run.output
    assert not run.requests, f"Render was called without a buildable request: {run.requests}"


def test_nothing_is_sent_for_a_malformed_sha(tmp_path):
    run = _deploy(tmp_path, _both_ok(), sha="")
    assert run.code != 0, run.output
    assert not run.requests, f"Render was called with no gated commit: {run.requests}"


def test_a_refused_backend_leaves_production_unchanged(tmp_path):
    reason = "commit not found on connected branch"
    plan = {**_both_ok(), f"POST {BACKEND}": [_reply(404, json.dumps({"message": reason}))]}
    run = _deploy(tmp_path, plan)
    assert run.code != 0, run.output
    assert run.keys() == [f"POST {BACKEND}"], (
        f"after the backend was refused, the frontend must not be asked: {run.requests}"
    )
    assert "::error::Render refused" in run.output and "HTTP 404" in run.output, run.output
    assert reason in run.output, (
        f"Render's own explanation was swallowed (curl -f does this). Output:\n{run.output}"
    )


@pytest.mark.parametrize(
    "frontend",
    [[_reply(503, '{"message":"unavailable"}')], [_reply(0, exit=7)]],
    ids=["refused", "network-failure"],
)
def test_a_refused_frontend_cancels_the_accepted_backend(tmp_path, frontend):
    """The backend must not ship alone: it is cancelled so both stay together."""
    plan = {
        **_both_ok(),
        f"POST {FRONTEND}": frontend,
        f"CANCEL {BACKEND}": [_reply(200, _deploy_body(GATED_SHA, "dep-" + BACKEND[-4:]))],
    }
    run = _deploy(tmp_path, plan)
    assert run.code != 0, run.output
    assert f"CANCEL {BACKEND}" in run.keys(), (
        f"the backend deploy was left building while the frontend was refused. Output:\n{run.output}"
    )
    assert "SPLIT" not in run.output, run.output


def test_a_failed_cancel_says_production_may_be_split(tmp_path):
    plan = {
        **_both_ok(),
        f"POST {FRONTEND}": [_reply(503, "{}")],
        f"CANCEL {BACKEND}": [_reply(409, '{"message":"already live"}')],
    }
    run = _deploy(tmp_path, plan)
    assert run.code != 0, run.output
    assert "PRODUCTION MAY BE SPLIT" in run.output, (
        f"a backend that could not be cancelled must be reported loudly. Output:\n{run.output}"
    )


def test_render_building_another_commit_fails_and_cancels_both(tmp_path):
    plan = {
        **_both_ok(),
        f"POST {FRONTEND}": _ok(FRONTEND, OTHER_SHA),
        f"CANCEL {BACKEND}": [_reply(200, "{}")],
        f"CANCEL {FRONTEND}": [_reply(200, "{}")],
    }
    run = _deploy(tmp_path, plan)
    assert run.code != 0, (
        "Render reported building a different commit and the step stayed green: an "
        f"unverified release under a green tick. Output:\n{run.output}"
    )
    assert "::error::" in run.output and OTHER_SHA in run.output, run.output
    assert {f"CANCEL {BACKEND}", f"CANCEL {FRONTEND}"} <= set(run.keys()), (
        f"every deploy this run started must be cancelled on a mismatch: {run.keys()}"
    )


def test_an_uppercase_commit_from_render_is_the_same_commit(tmp_path):
    plan = {**_both_ok(), f"POST {FRONTEND}": _ok(FRONTEND, GATED_SHA.upper())}
    run = _deploy(tmp_path, plan)
    assert run.code == 0, run.output


@pytest.mark.parametrize(
    "body",
    [_deploy_body(None, "dep-x"), "{}", "", "[]", '"ok"', "<html>502 from a proxy</html>"],
    ids=["no-commit", "empty-json", "empty", "json-array", "json-string", "not-json"],
)
def test_render_naming_no_commit_fails_and_shows_what_came_back(tmp_path, body):
    """No result is not a good result: a 2xx that never says which commit is
    being built is no proof the gated one is."""
    run = _deploy(tmp_path, {**_both_ok(), f"POST {FRONTEND}": [_reply(201, body)]})
    assert run.code != 0, f"a reply naming no commit was accepted as proof. Output:\n{run.output}"
    assert "did not confirm" in run.output, run.output
    if body:
        assert body in run.output, f"the unexpected body must be shown. Output:\n{run.output}"


def test_a_queued_deploy_is_confirmed_through_the_deploy_list(tmp_path):
    """Render answers 202 with an empty body when it queues behind a running
    deploy; the script must then find the queued deploy of the gated commit."""
    listing = _listing(
        {"id": "dep-queued1", "status": "queued", "createdAt": _iso(), "commit": {"id": GATED_SHA}},
        {"id": "dep-live0", "status": "live", "createdAt": _iso(-3600), "commit": {"id": OTHER_SHA}},
    )
    plan = {**_both_ok(), f"POST {FRONTEND}": [_reply(202)], f"GET {FRONTEND}": [_reply(200, listing)]}
    run = _deploy(tmp_path, plan)
    assert run.code == 0, run.output
    assert "dep-queued1" in run.output, run.output


def test_the_deploy_list_lookup_survives_a_network_blip_and_offset_timestamps(tmp_path):
    listing = _listing(
        {"id": "dep-queued2", "status": "queued", "createdAt": _iso(suffix="+00:00"),
         "commit": {"id": GATED_SHA}},
    )
    plan = {
        **_both_ok(),
        f"POST {FRONTEND}": [_reply(202)],
        f"GET {FRONTEND}": [_reply(0, exit=7), _reply(200, listing)],
    }
    run = _deploy(tmp_path, plan)
    assert run.code == 0, run.output
    assert "dep-queued2" in run.output, run.output


@pytest.mark.parametrize(
    "deploys",
    [
        [],
        [{"id": "dep-other", "status": "queued", "createdAt": _iso(), "commit": {"id": OTHER_SHA}}],
        [{"id": "dep-old", "status": "live", "createdAt": _iso(-7200), "commit": {"id": GATED_SHA}}],
    ],
    ids=["nothing-listed", "only-another-commit", "only-an-old-deploy-of-this-commit"],
)
def test_a_queued_deploy_that_cannot_be_found_fails(tmp_path, deploys):
    plan = {
        **_both_ok(),
        f"POST {FRONTEND}": [_reply(202)],
        f"GET {FRONTEND}": [_reply(200, _listing(*deploys))],
    }
    run = _deploy(tmp_path, plan)
    assert run.code != 0, (
        f"a 202 was accepted without finding a fresh deploy of the gated commit. Output:\n{run.output}"
    )


def test_the_api_key_is_never_printed(tmp_path):
    for plan in [
        _both_ok(),
        {**_both_ok(), f"POST {FRONTEND}": _ok(FRONTEND, OTHER_SHA)},
        {**_both_ok(), f"POST {BACKEND}": [_reply(401, '{"message":"unauthorized"}')]},
    ]:
        run = _deploy(tmp_path, plan)
        assert API_KEY not in run.output, f"the Render API key leaked into the log for {plan}"
