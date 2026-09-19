"""The Render deploy must ship the commit the gate verified - not the branch tip.

Why this file exists
--------------------
``.github/workflows/deploy-render.yml`` goes to real trouble to establish WHICH
commit is safe to ship. It resolves the SHA under test into
``steps.commit.outputs.sha``, polls the Model CI API for a run against **that
exact SHA**, and refuses to deploy if the run is missing, unfinished or failed.
Its own header says so at length.

Then, until the commit that added this file, it threw the answer away. Both
deploy steps POSTed ``-d '{}'`` to Render's ``POST /v1/services/{id}/deploys``.
Render's API reference documents ``commitId`` as "The SHA of a specific Git
commit to deploy for a service. Defaults to the latest commit on the service's
connected branch." An empty body is therefore a request for the BRANCH TIP, and
the SHA the workflow had just spent up to fifteen minutes certifying was never
sent. The poll deadline is 900 seconds; the tip can move inside that window. So
the workflow could verify commit A and ship commit B, whose gates had not run at
all - under a green tick, on a project whose entire pitch is that every
published number is gated by a run you can inspect. (GitHub issue #5.)

Nothing could see it from a passing run. Both steps went green, Render showed a
fresh deploy, and ``curl -sf`` discarded the response body, so the log could not
even name the deploy it had created, let alone the commit inside it. The only
way to observe the defect was to read Render's API docs for what an empty body
means.

WHY THIS READS THE YAML AS TEXT. ``import yaml`` happens to work here, but
PyYAML is not pinned in ``backend/requirements.txt`` - it is present only
because something else vendors it transitively today. Hanging a load-bearing
assertion on an undeclared dependency is precisely the gap
``test_dependency_pins.py`` exists to close (green because of something the
suite never declared). This check does not need YAML semantics, only the shape
of specific ``run:`` blocks, so it parses text with regex - the same convention
``test_dependency_pins.py`` uses on ``requirements.txt``.

WHAT IS ASSERTED, AND WHY EACH ONE HAS TEETH. Not "a deploy step exists", which
is a tautology, and not "the file contains the string commitId somewhere", which
a comment would satisfy while the bug stayed live. For every step that calls
Render's deploy endpoint:

  1. it does not send the literal empty body ``{}`` - the original defect;
  2. it sets ``commitId``, built from the ``$SHA`` env var;
  3. THE ONE THAT MATTERS - that ``SHA`` env var is bound to the *same*
     ``${{ }}`` expression the "Require Model CI green for this commit" step is
     bound to. A deploy wired instead to ``github.sha``, recomputed independently
     of the gate, passes 1 and 2 and still reopens issue #5: it would name a
     commit, just not necessarily the gated one. This assertion is the link
     between the gate and the deploy, and it is the only one that survives that
     mutation;
  4. Render's response is captured and the deploy id printed, so the run log can
     answer "which build did this start?" after the fact;
  5. both known services are still deployed - one silently dropped is a service
     frozen at an old release with nothing going red.

This file carries NO ``pytestmark = pytest.mark.model_ci``. That is deliberate:
it is a workflow-hygiene gate, not a model gate, and adding the mark would
require updating ``MODEL_CI_GATE_CENSUS`` in ``test_model_ci_gates.py`` on every
future edit here for no benefit. ``shard_tests.py`` partitions from the glob, so
this file is picked up by the sharded CI run with no registration.
"""
from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parent.parent.parent
    / ".github" / "workflows" / "deploy-render.yml"
)

#: The step whose whole job is to establish that the commit is gated. The deploy
#: steps must be bound to the same SHA expression this one is.
GATE_STEP = "Require Model CI green for this commit"

#: Every Render service this workflow is allowed to deploy. If a service is
#: genuinely added, removed or re-created, update this in the same commit.
EXPECTED_SERVICE_IDS = {
    "srv-d98ru31o3t8c73ed9dig": "backend (supply-chain-api)",
    "srv-d98ru9ss728c73c85bqg": "frontend (supply-chain-ui)",
}

_STEP_RE = re.compile(r"^      - name: (?P<name>.+)$", re.MULTILINE)


def _steps() -> dict[str, str]:
    """``{step name: the step's block}``, each running to the next step or EOF."""
    text = WORKFLOW.read_text(encoding="utf-8")
    marks = list(_STEP_RE.finditer(text))
    assert marks, f"no steps found in {WORKFLOW} - has the workflow been restructured?"
    blocks: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        blocks[mark.group("name").strip()] = text[mark.start():end]
    return blocks


def _deploy_steps() -> dict[str, str]:
    """Just the steps that actually call Render's deploy endpoint."""
    steps = {
        name: block
        for name, block in _steps().items()
        if "api.render.com" in block and "/deploys" in block
    }
    assert steps, (
        f"{WORKFLOW} no longer contains a step calling Render's "
        "/v1/services/*/deploys endpoint. Either the deploy mechanism changed - in "
        "which case point this test at the new one - or the deploy was deleted. Do "
        "not leave this guard dark: it is the only thing standing between the Model "
        "CI gate and an ungated release (issue #5)."
    )
    return steps


def _sha_binding(block: str) -> str | None:
    """The ``${{ … }}`` expression bound to the step's ``SHA`` env var."""
    match = re.search(r"^\s*SHA:\s*\$\{\{\s*(?P<expr>.+?)\s*\}\}\s*$", block, re.MULTILINE)
    return match.group("expr") if match else None


def test_the_workflow_still_deploys_exactly_the_two_known_services():
    """A service silently dropped from the deploy is a service frozen at an old
    release, with nothing anywhere going red to say so."""
    deployed = {
        service_id
        for block in _deploy_steps().values()
        for service_id in re.findall(r"srv-[A-Za-z0-9]+", block)
    }
    assert deployed == set(EXPECTED_SERVICE_IDS), (
        f"the workflow deploys {sorted(deployed)}, but {sorted(EXPECTED_SERVICE_IDS)} "
        "are expected. If a Render service was deliberately added, removed or "
        "re-created, update EXPECTED_SERVICE_IDS in the same commit."
    )


def test_no_render_deploy_sends_an_empty_body():
    """THE ORIGINAL DEFECT, stated as narrowly as it actually was.

    ``-d '{}'`` asks Render for the latest commit on the connected branch, which
    is not the commit anything in this workflow verified.
    """
    for name, block in _deploy_steps().items():
        assert "-d '{}'" not in block, (
            f"the step {name!r} POSTs an empty JSON body to Render. Per Render's "
            "create-deploy API that means 'deploy the latest commit on the connected "
            "branch' - so the SHA this workflow spent up to 15 minutes gating is "
            "discarded and whatever is on main's tip ships instead. Send "
            '{"commitId": "<the gated SHA>"}.'
        )


def test_every_render_deploy_sends_the_commit_id():
    for name, block in _deploy_steps().items():
        assert "commitId" in block, (
            f"the step {name!r} does not set commitId, so Render has no way to know "
            "which commit to build and falls back to the branch tip."
        )
        assert '--arg sha "$SHA"' in block, (
            f"the step {name!r} does not build its request body from the $SHA env "
            "var. The commit sent to Render must be the gated one, and it must be "
            "passed as data rather than spliced into the JSON by hand."
        )


def test_the_deployed_commit_is_the_one_the_model_ci_gate_checked():
    """THE ASSERTION WITH TEETH - the link between the gate and the deploy.

    Sending *a* commitId is not enough; it must be THE commit Model CI was polled
    for. A deploy bound to ``github.sha`` instead would satisfy every other test
    in this file and still ship a commit the gate never saw, because the two
    expressions are resolved at different moments against a branch whose tip
    moves. So this compares the deploy steps' SHA binding against the gate step's
    own binding and requires them to be the same expression.
    """
    gate = _steps().get(GATE_STEP)
    assert gate, (
        f"the step {GATE_STEP!r} is gone from {WORKFLOW}. That step is the Model CI "
        "verification itself - without it there is no gated commit for the deploy to "
        "be pinned to, and this test can no longer check the pinning. Restore it, or "
        "update GATE_STEP if it was renamed."
    )
    gated = _sha_binding(gate)
    assert gated == "steps.commit.outputs.sha", (
        f"the Model CI gate now polls on {gated!r} rather than the resolved "
        "steps.commit.outputs.sha; this test can no longer tell which commit was "
        "verified."
    )

    for name, block in _deploy_steps().items():
        binding = _sha_binding(block)
        assert binding is not None, (
            f"the step {name!r} has no SHA env var, so the commit it sends to Render "
            "cannot be traced back to the gate."
        )
        assert binding == gated, (
            f"the step {name!r} takes its commit from {binding!r}, but the Model CI "
            f"gate verified {gated!r}. A commitId that is present but resolved "
            "independently of the gate is exactly how issue #5 reopens: the two can "
            "name different commits, and the one that ships is the unverified one."
        )


def test_render_deploys_report_which_build_they_started():
    """``curl -sf`` failed the step on an HTTP error and discarded the body with it.

    The log could not say which deploy it had created, so the workflow could not
    answer a question about itself, and Render's own explanation of a refusal was
    swallowed too.
    """
    for name, block in _deploy_steps().items():
        assert "-sf " not in block, (
            f"the step {name!r} still uses `curl -sf`, which discards the response "
            "body - including the deploy id on success and Render's error text on "
            "failure. Capture the body and check the status code explicitly."
        )
        assert re.search(r"jq[^\n]*\.id\b", block), (
            f"the step {name!r} never extracts the deploy id from Render's response, "
            "so the run log cannot say which build it started."
        )
        assert re.search(r"^\s*echo .*\$\(jq|^\s*echo .*deploy", block, re.MULTILINE), (
            f"the step {name!r} does not print what it learned from Render's "
            "response. A captured-but-unprinted id is invisible in the Actions log."
        )
