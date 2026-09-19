"""The Render deploy must ship the commit the gate verified - not the branch tip.

Why this file exists
--------------------
``.github/workflows/deploy-render.yml`` resolves the commit under test into
``steps.commit.outputs.sha``, polls Model CI for a run against **that exact SHA**
and refuses on a missing, unfinished or failed verdict. Until issue #5, both
deploy steps then POSTed ``-d '{}'`` to Render's ``POST /v1/services/{id}/deploys``.
Render's API reference documents ``commitId`` as "The SHA of a specific Git
commit to deploy for a service. Defaults to the latest commit on the service's
connected branch." An empty body is therefore a request for the BRANCH TIP: the
workflow could verify commit A and ship commit B, whose gates never ran, under a
green tick. ``curl -sf`` also discarded Render's reply, so the log could not
even say which deploy it had started.

Both deploy steps now run ``.github/scripts/render-deploy.sh``. This file checks
two things, and neither can be satisfied by a comment:

1. THE WIRING, read from the workflow with every comment line removed: each
   deploy step goes through the script, the ``SHA`` it passes is bound to the
   same expression the Model CI gate polled on (not ``github.sha`` recomputed
   independently, which would name *a* commit, just not the gated one), no step
   calls Render's API directly, and both services are still deployed.
2. THE BEHAVIOUR, by running the real script under ``bash`` against a stubbed
   ``curl`` for every Render reply it handles: the request body is exactly
   ``{"commitId": <gated SHA>}``; a refusal fails and shows Render's text; a
   reply naming another commit fails; a reply naming NO commit fails (no result
   is not a good result); a 202 is confirmed through the deploy list or fails.
   Deleting any of those branches, or adding ``curl -f``, turns a test red.

WHY THE YAML IS READ AS TEXT. PyYAML is not pinned in ``backend/requirements.txt``;
hanging a load-bearing assertion on an undeclared dependency is the gap
``test_dependency_pins.py`` exists to close. The wiring checks only need the
step names and a few ``key: value`` lines, so plain text parsing is enough.

This file carries NO ``pytestmark = pytest.mark.model_ci``: it is a
workflow-hygiene gate, not a model gate, so ``MODEL_CI_GATE_CENSUS`` is untouched.
``bash`` and ``jq`` are required (both ship on the GitHub-hosted Ubuntu runners
CI uses); a missing one fails loudly rather than skipping.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "deploy-render.yml"
SCRIPT = REPO / ".github" / "scripts" / "render-deploy.sh"

#: The step whose whole job is to establish that the commit is gated. The deploy
#: steps must be bound to the same SHA expression this one is.
GATE_STEP = "Require Model CI green for this commit"

#: Every Render service this workflow is allowed to deploy. If a service is
#: genuinely added, removed or re-created, update this in the same commit.
EXPECTED_SERVICE_IDS = {
    "srv-d98ru31o3t8c73ed9dig": "backend (supply-chain-api)",
    "srv-d98ru9ss728c73c85bqg": "frontend (supply-chain-ui)",
}

GATED_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
API_KEY = "rnd_TESTKEYDONOTPRINT"


# ── Wiring: the workflow, comments removed ────────────────────────────────────


def _code_lines(text: str) -> str:
    """The text with every comment-only line dropped, so prose cannot satisfy
    (or trip) an assertion and a step's leading comment is never attributed to
    the step above it."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


_STEP_RE = re.compile(r"^\s*- name: (?P<name>.+)$", re.MULTILINE)


def _steps() -> dict[str, str]:
    """``{step name: the step's block}``, comments removed, each block running
    to the next step or EOF."""
    text = _code_lines(WORKFLOW.read_text(encoding="utf-8"))
    text = text[text.index("steps:"):]
    marks = list(_STEP_RE.finditer(text))
    assert marks, f"no steps found in {WORKFLOW} - has the workflow been restructured?"
    return {
        mark.group("name").strip(): text[
            mark.start():marks[i + 1].start() if i + 1 < len(marks) else len(text)
        ]
        for i, mark in enumerate(marks)
    }


def _deploy_steps() -> dict[str, str]:
    """The steps that run the deploy script."""
    steps = {name: block for name, block in _steps().items() if "render-deploy.sh" in block}
    assert steps, (
        f"no step in {WORKFLOW} runs .github/scripts/render-deploy.sh. If the deploy "
        "mechanism changed, point this test at the new one; do not leave this guard "
        "dark - it is what ties the Model CI gate to the commit that ships (issue #5)."
    )
    return steps


def _binding(block: str, key: str) -> str | None:
    """The ``${{ ... }}`` expression bound to ``key`` in a step block."""
    match = re.search(
        rf"^\s*{re.escape(key)}:\s*\$\{{\{{\s*(?P<expr>.+?)\s*\}}\}}\s*$", block, re.MULTILINE
    )
    return match.group("expr") if match else None


def _gated_expression() -> str:
    gate = _steps().get(GATE_STEP)
    assert gate, (
        f"the step {GATE_STEP!r} is gone from {WORKFLOW}. That step is the Model CI "
        "verification itself; without it there is no gated commit for the deploy to "
        "be pinned to. Restore it, or update GATE_STEP if it was renamed."
    )
    gated = _binding(gate, "SHA")
    assert gated == "steps.commit.outputs.sha", (
        f"the Model CI gate now polls on {gated!r} rather than steps.commit.outputs.sha; "
        "this test can no longer tell which commit was verified."
    )
    return gated


def test_the_workflow_deploys_exactly_the_two_known_services():
    """A service silently dropped from the deploy is frozen at an old release,
    with nothing going red to say so."""
    deployed = {
        service_id
        for block in _deploy_steps().values()
        for service_id in re.findall(r"render-deploy\.sh\s+(srv-[A-Za-z0-9]+)", block)
    }
    assert deployed == set(EXPECTED_SERVICE_IDS), (
        f"the workflow deploys {sorted(deployed)}, but {sorted(EXPECTED_SERVICE_IDS)} "
        "are expected. If a Render service was deliberately added, removed or "
        "re-created, update EXPECTED_SERVICE_IDS in the same commit."
    )


def test_no_step_calls_render_directly():
    """Every call to Render goes through the script the behaviour tests below
    exercise. A raw ``curl`` added to the workflow would bypass all of them."""
    for name, block in _steps().items():
        assert "api.render.com" not in block, (
            f"the step {name!r} calls Render's API directly. Deploy through "
            ".github/scripts/render-deploy.sh so the commit pinning and the reply "
            "checks apply to it."
        )


def test_the_deployed_commit_is_the_one_the_model_ci_gate_checked():
    """THE LINK BETWEEN THE GATE AND THE DEPLOY.

    Sending *a* commitId is not enough; it must be THE commit Model CI was polled
    for. A deploy bound to ``github.sha`` would pass every behaviour test below
    and still ship a commit the gate never saw, because the two expressions are
    resolved at different moments against a branch whose tip moves. The script
    itself is also checked out at that commit, not at main's tip.
    """
    gated = _gated_expression()
    for name, block in _deploy_steps().items():
        binding = _binding(block, "SHA")
        assert binding == gated, (
            f"the step {name!r} takes its commit from {binding!r}, but the Model CI "
            f"gate verified {gated!r}. A commit resolved independently of the gate is "
            "exactly how issue #5 reopens: the one that ships can be the unverified one."
        )
    checkout = [block for block in _steps().values() if "actions/checkout" in block]
    assert checkout, "the deploy job no longer checks out the repo, so the script cannot run"
    for block in checkout:
        assert _binding(block, "ref") == gated, (
            "the deploy script must be checked out at the gated commit "
            f"({gated!r}), not at whatever main points to when the job starts."
        )


def test_the_script_never_uses_curl_fail():
    """``curl -f`` exits 22 on an HTTP error and throws away Render's reply,
    including the text explaining a refusal. Any spelling of the flag counts."""
    script = _code_lines(SCRIPT.read_text(encoding="utf-8")).replace("\\\n", " ")
    for call in re.findall(r"\bcurl\b[^\n]*", script):
        assert not re.search(r"(?<!\S)(-[A-Za-z]*f[A-Za-z]*|--fail\S*)(?!\S)", call), (
            f"render-deploy.sh calls curl with a fail flag: {call.strip()!r}. Capture the "
            "body and check the status code explicitly instead."
        )


# ── Behaviour: the real script, against a stubbed curl ────────────────────────

# A stand-in for curl that honours the flags the script relies on (-o, -w, -d and
# friends, -f and friends), records every request, and answers from env vars.
_STUB = r'''
import json, os, sys

args = sys.argv[1:]
out = fmt = body = None
method = "GET"
fail = False
url = None
i = 0
while i < len(args):
    a = args[i]
    if a == "-o":
        out = args[i + 1]; i += 2; continue
    if a == "-w":
        fmt = args[i + 1]; i += 2; continue
    if a == "-X":
        method = args[i + 1]; i += 2; continue
    if a == "-H":
        i += 2; continue
    if a in ("-d", "--data", "--data-raw", "--data-binary", "--json"):
        body = args[i + 1]; method = "POST" if method == "GET" else method; i += 2; continue
    if a in ("--fail", "--fail-with-body") or (a.startswith("-") and not a.startswith("--") and "f" in a):
        fail = True
    elif not a.startswith("-"):
        url = a
    i += 1

with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"method": method, "url": url, "body": body, "argv": args}) + "\n")

kind = "POST" if method == "POST" else "LIST"
code = int(os.environ.get(f"STUB_{kind}_CODE", "500"))
payload = os.environ.get(f"STUB_{kind}_BODY", "")
if fail and code >= 400:
    sys.stderr.write(f"curl: (22) The requested URL returned error: {code}\n")
    sys.exit(22)
if out:
    with open(out, "w") as f:
        f.write(payload)
else:
    sys.stdout.write(payload)
if fmt:
    sys.stdout.write(fmt.replace("%{http_code}", str(code)).replace("\\n", "\n"))
'''


@dataclass
class Run:
    code: int
    output: str
    requests: list[dict]

    @property
    def posts(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]


def _deploy(tmp_path: Path, *, sha: str = GATED_SHA, post: tuple[int, str],
            listing: tuple[int, str] = (500, "")) -> Run:
    for tool in ("bash", "jq"):
        assert shutil.which(tool), f"{tool} is required to exercise render-deploy.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(f"#!{sys.executable}\n{_STUB}", encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "requests.jsonl"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SHA": sha,
        "RENDER_API_KEY": API_KEY,
        "RENDER_LOOKUP_SLEEP": "0",
        "STUB_LOG": str(log),
        "STUB_POST_CODE": str(post[0]),
        "STUB_POST_BODY": post[1],
        "STUB_LIST_CODE": str(listing[0]),
        "STUB_LIST_BODY": listing[1],
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), "srv-test", "supply-chain-test"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return Run(proc.returncode, proc.stdout + proc.stderr, requests)


def _deploy_body(commit: str | None, deploy_id: str = "dep-abc") -> str:
    body: dict = {"id": deploy_id, "status": "created"}
    if commit is not None:
        body["commit"] = {"id": commit, "message": "m"}
    return json.dumps(body)


def _now_iso(offset_s: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.123456Z", time.gmtime(time.time() + offset_s))


def test_the_request_names_exactly_the_gated_commit(tmp_path):
    """THE ORIGINAL DEFECT, checked on the wire: whatever the script's spelling
    (``-d '{}'``, ``--data "{}"``, ``-d ''``), the body Render receives must be
    ``{"commitId": <gated SHA>}`` and nothing else."""
    run = _deploy(tmp_path, post=(201, _deploy_body(GATED_SHA)))
    assert len(run.posts) == 1, f"expected one deploy request, saw {run.requests}"
    post = run.posts[0]
    assert post["url"].endswith("/services/srv-test/deploys"), post["url"]
    assert json.loads(post["body"] or "null") == {"commitId": GATED_SHA}, (
        f"Render was sent {post['body']!r}. Without commitId set to the gated SHA, "
        "Render deploys the latest commit on the branch, gated or not (issue #5)."
    )
    assert run.code == 0, run.output


def test_a_confirmed_deploy_prints_its_id_and_commit(tmp_path):
    run = _deploy(tmp_path, post=(201, _deploy_body(GATED_SHA, "dep-xyz789")))
    assert run.code == 0, run.output
    confirmation = [line for line in run.output.splitlines() if "dep-xyz789" in line]
    assert confirmation and GATED_SHA in confirmation[0], (
        "a successful deploy must print Render's deploy id next to the commit Render "
        f"reports, so the run log says which build it started. Output:\n{run.output}"
    )


def test_render_building_another_commit_fails(tmp_path):
    run = _deploy(tmp_path, post=(201, _deploy_body(OTHER_SHA)))
    assert run.code != 0, (
        "Render reported building a different commit and the step stayed green: an "
        f"unverified release under a green tick. Output:\n{run.output}"
    )
    assert "::error::" in run.output and OTHER_SHA in run.output, run.output


@pytest.mark.parametrize("body", [_deploy_body(None), "{}", ""], ids=["no-commit", "empty-json", "empty"])
def test_render_naming_no_commit_fails(tmp_path, body):
    """No result is not a good result: a 2xx that never says which commit is
    being built is no proof the gated one is."""
    run = _deploy(tmp_path, post=(201, body))
    assert run.code != 0, f"a reply naming no commit was accepted as proof. Output:\n{run.output}"
    assert "never named the commit" in run.output, run.output


def test_a_refused_deploy_fails_and_shows_renders_reason(tmp_path):
    reason = "commit not found on connected branch"
    run = _deploy(tmp_path, post=(404, json.dumps({"id": "not_found", "message": reason})))
    assert run.code != 0, run.output
    assert "::error::Render refused" in run.output and "HTTP 404" in run.output, (
        f"a non-2xx reply must be reported as Render refusing the deploy. Output:\n{run.output}"
    )
    assert reason in run.output, (
        f"Render's own explanation was swallowed (curl -f does this). Output:\n{run.output}"
    )


def test_a_non_json_success_fails_and_shows_the_body(tmp_path):
    page = "<html>502 from some proxy</html>"
    run = _deploy(tmp_path, post=(200, page))
    assert run.code != 0, run.output
    assert page in run.output, f"the unexpected body must be shown. Output:\n{run.output}"


def test_a_queued_deploy_is_confirmed_through_the_deploy_list(tmp_path):
    """Render answers 202 with an empty body when it queues behind a running
    deploy. The script must then find the queued deploy of the gated commit."""
    listing = json.dumps([
        {"deploy": {"id": "dep-queued1", "status": "queued", "createdAt": _now_iso(),
                    "commit": {"id": GATED_SHA}}, "cursor": "c1"},
        {"deploy": {"id": "dep-live0", "status": "live", "createdAt": _now_iso(-3600),
                    "commit": {"id": OTHER_SHA}}, "cursor": "c0"},
    ])
    run = _deploy(tmp_path, post=(202, ""), listing=(200, listing))
    assert run.code == 0, run.output
    assert "dep-queued1" in run.output, run.output


@pytest.mark.parametrize(
    "deploys",
    [
        [],
        [{"id": "dep-other", "status": "queued", "createdAt": _now_iso(), "commit": {"id": OTHER_SHA}}],
        [{"id": "dep-old", "status": "live", "createdAt": _now_iso(-7200), "commit": {"id": GATED_SHA}}],
    ],
    ids=["nothing-listed", "only-another-commit", "only-an-old-deploy-of-this-commit"],
)
def test_a_queued_deploy_that_cannot_be_found_fails(tmp_path, deploys):
    listing = json.dumps([{"deploy": d, "cursor": "c"} for d in deploys])
    run = _deploy(tmp_path, post=(202, ""), listing=(200, listing))
    assert run.code != 0, (
        f"a 202 was accepted without finding a fresh deploy of the gated commit. Output:\n{run.output}"
    )


def test_a_queued_deploy_naming_another_commit_fails(tmp_path):
    run = _deploy(tmp_path, post=(202, _deploy_body(OTHER_SHA)))
    assert run.code != 0, run.output


def test_a_malformed_sha_is_refused_before_calling_render(tmp_path):
    run = _deploy(tmp_path, sha="", post=(201, _deploy_body("")))
    assert run.code != 0, run.output
    assert not run.posts, f"the script called Render with no gated commit: {run.requests}"


def test_the_api_key_is_never_printed(tmp_path):
    for post in [(201, _deploy_body(GATED_SHA)), (201, _deploy_body(OTHER_SHA)),
                 (401, '{"message":"unauthorized"}')]:
        run = _deploy(tmp_path, post=post)
        assert API_KEY not in run.output, f"the Render API key leaked into the log for {post}"
