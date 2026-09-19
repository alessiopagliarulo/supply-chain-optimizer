"""The loop's own gates, run for real instead of read.

The autonomous loop (``.github/workflows/claude-*.yml``) decides things in shell:
whether a Scout run that filed nothing is a success or a crash, who may start a
build, which ideas an existing PR "covers". Each of those was wrong at least once
on the PR that introduced it (the PR 38 audit), and none of them can be exercised
by dispatching a run without spending the owner's Claude usage. So this file
extracts the exact ``run:`` blocks from the workflow files and executes them
against a stub ``gh``, and calls the in-flight script's own exported functions.

  - Scout "Verify Scout actually filed something": a deliberate zero-filing
    ("none - <reason>", "None", "filed 0") passes; a crash, a turn-capped
    session, a missing transcript on the default branch or no decision fails.
  - Builder "Read loop config": a non-integer prCap falls back to 3 instead of
    silently disabling the cap.
  - Builder "Check the queue": only an admin/maintainer's `approved` label counts,
    on every trigger, not just the label event.
  - scripts/loop-inflight.mjs: only deliberate references ("closes #12") to THIS
    repo mark an idea covered; passing mentions and fork PRs never do.
  - scripts/validate-loop-config.sh: malformed files and wrong types fail.
  - Every Claude agent workflow triggered by pull_request/push runs only on
    `claude/` branches (the owner's own changes get plain CI).

Needs bash, jq and node on PATH, as every GitHub runner has.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
INFLIGHT = REPO / "scripts" / "loop-inflight.mjs"
VALIDATOR = REPO / "scripts" / "validate-loop-config.sh"
SLUG = "alessiopagliarulo/supply-chain-optimizer"

for tool in ("bash", "jq", "node"):
    if shutil.which(tool) is None:  # pragma: no cover - loud, not a skip
        raise RuntimeError(f"{tool} is required by test_loop_workflow_gates.py")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step_script(workflow: str, job: str, step_name: str) -> str:
    steps = _workflow(workflow)["jobs"][job]["steps"]
    matches = [s for s in steps if s.get("name") == step_name]
    assert len(matches) == 1, f"{workflow}: expected one step named {step_name!r}"
    return matches[0]["run"]


def _run_step(script: str, tmp_path: Path, env: dict, gh_script: str, cwd: Path = REPO):
    """Run a workflow step the way GitHub does (`bash -e`), with a stub `gh`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/bash\n" + gh_script)
    gh.chmod(0o755)
    step = tmp_path / "step.sh"
    step.write_text(script)
    output = tmp_path / "github_output"
    output.write_text("")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(tmp_path),
        **env,
    }
    proc = subprocess.run(
        ["bash", "-e", str(step)], cwd=cwd, env=full_env, capture_output=True, text=True, timeout=60
    )
    outputs: dict[str, str] = {}
    for line in output.read_text().splitlines():
        if "=" in line and "<<" not in line:
            k, v = line.split("=", 1)
            outputs[k] = v
    return proc, outputs


# ── Scout: a deliberate zero-filing is a success, a malfunction is not ──────────

VERIFY = ("claude-scout.yml", "scout", "Verify Scout actually filed something")

#: `gh issue list` answers with FAKE_ISSUES; `gh api repos/<r>` says main is the default.
GH_ISSUES = 'if [ "$1" = issue ]; then echo "${FAKE_ISSUES:-[]}"; elif [ "$1" = api ]; then echo main; fi\n'


def _transcript(tmp_path: Path, final_text: str, subtype: str = "success", is_error: bool = False) -> str:
    path = tmp_path / "execution.json"
    path.write_text(json.dumps([
        {"type": "system"},
        {"type": "assistant"},
        {"type": "result", "subtype": subtype, "is_error": is_error, "result": final_text},
    ]))
    return str(path)


@pytest.mark.parametrize(
    "final_text",
    [
        "Dropped two candidates (covered by PR #30).\nSCOUT-DECISION: none - every candidate was already covered",
        "SCOUT-DECISION: None - everything already covered",
        "SCOUT-DECISION: NONE: nothing cleared the evidence floor",
        "**SCOUT-DECISION: none — nothing new**",
        "> `SCOUT-DECISION: none - blockquoted`",
        "SCOUT-DECISION: filed 0",
        "SCOUT-DECISION: filed 0 - all covered",
    ],
)
def test_scout_zero_filing_with_a_stated_decision_passes(tmp_path, final_text):
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "main",
         "EXECUTION_FILE": _transcript(tmp_path, final_text)},
        GH_ISSUES,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "deliberately filed nothing" in proc.stdout


@pytest.mark.parametrize(
    ("final_text", "subtype", "is_error", "why"),
    [
        ("I will wait for the researchers to report back.", "success", False, "no decision"),
        ("SCOUT-DECISION: none -   ", "success", False, "empty reason"),
        ("SCOUT-DECISION: nonetheless I stopped", "success", False, "not a decision"),
        ("SCOUT-DECISION: filed 2", "success", False, "claims issues that do not exist"),
        ("SCOUT-DECISION: none - out of turns", "error_max_turns", True, "turn-capped session"),
        ("SCOUT-DECISION: none - crashed", "error_during_execution", True, "errored session"),
    ],
)
def test_scout_zero_filing_without_a_clean_decision_fails(tmp_path, final_text, subtype, is_error, why):
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "main",
         "EXECUTION_FILE": _transcript(tmp_path, final_text, subtype, is_error)},
        GH_ISSUES,
    )
    assert proc.returncode == 1, f"{why}: should fail\n{proc.stdout}{proc.stderr}"
    assert "Scout filed ZERO issues" in proc.stdout


def test_scout_with_no_transcript_on_the_default_branch_fails(tmp_path):
    """The agent never ran on main: that is a malfunction, not a stand-down."""
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "main", "EXECUTION_FILE": ""},
        GH_ISSUES,
    )
    assert proc.returncode == 1


def test_scout_transcript_without_a_result_fails(tmp_path):
    path = tmp_path / "execution.json"
    path.write_text('[{"type": "system"}]')
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "main", "EXECUTION_FILE": str(path)},
        GH_ISSUES,
    )
    assert proc.returncode == 1


def test_scout_branch_test_run_passes_with_a_warning(tmp_path):
    """claude-code-action refuses to run a workflow that differs from main's."""
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "fm/some-branch", "EXECUTION_FILE": ""},
        GH_ISSUES,
    )
    assert proc.returncode == 0
    assert "::warning::" in proc.stdout


def test_scout_that_filed_something_passes_whatever_it_said(tmp_path):
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "main", "EXECUTION_FILE": "",
         "FAKE_ISSUES": '[{"number": 38}, {"number": 5}]'},
        GH_ISSUES,
    )
    assert proc.returncode == 0
    assert "Scout filed 1 new proposal(s)." in proc.stdout


def test_scout_counts_only_issues_above_the_high_water_mark(tmp_path):
    proc, _ = _run_step(
        _step_script(*VERIFY), tmp_path,
        {"HIGH_WATER": "37", "REPO": SLUG, "REF_NAME": "main",
         "EXECUTION_FILE": _transcript(tmp_path, "I will report back."),
         "FAKE_ISSUES": '[{"number": 5}, {"number": 37}]'},
        GH_ISSUES,
    )
    assert proc.returncode == 1


def test_scout_prompt_asks_for_the_decision_line_the_verifier_reads():
    text = (WORKFLOWS / "claude-scout.yml").read_text()
    assert "SCOUT-DECISION: filed <N>" in text
    assert "SCOUT-DECISION: none - <one plain-English sentence" in text


# ── Builder: the queue cap cannot be silently disabled ──────────────────────────

@pytest.mark.parametrize(
    ("config", "cap"),
    [
        ({"prCap": 3}, "3"),
        ({"prCap": 0}, "0"),
        ({"prCap": "unlimited"}, "999999"),
        ({}, "3"),
        ({"prCap": 3.7}, "3"),
        ({"prCap": -5}, "3"),
        ({"prCap": "3"}, "3"),
    ],
)
def test_builder_prcap_is_always_a_whole_number(tmp_path, config, cap):
    work = tmp_path / "repo"
    (work / ".github").mkdir(parents=True)
    (work / ".github" / "loop-config.json").write_text(json.dumps(config))
    proc, out = _run_step(
        _step_script("claude-builder.yml", "build", "Read loop config"), tmp_path, {}, "exit 1\n", cwd=work
    )
    assert proc.returncode == 0, proc.stderr
    assert out["cap"] == cap
    assert out["autonomous"] == "false"


# ── Builder: only the owner's approval counts, on every trigger ─────────────────

#: #12 approved by the admin owner, #13 by a triage-only collaborator, #14 by an App.
GH_QUEUE = r"""
args="$*"
case "$args" in
  "pr list"*isDraft*) echo 0 ;;
  "pr list"*) echo "" ;;
  "issue list --state open --label approved"*'join(" ")'*) echo "12 13 14" ;;
  "issue list --state open --label approved"*) echo 3 ;;
  "issue list --state open --label proposal"*) echo 2 ;;
  "issue list --state open --label covered"*) echo "" ;;
  "api repos/o/r/issues/12/events"*) echo owner ;;
  "api repos/o/r/issues/13/events"*) echo someone-else; echo triager ;;
  "api repos/o/r/issues/14/events"*) echo 'dashboard[bot]' ;;
  "api repos/o/r/collaborators/owner/permission"*) echo admin ;;
  "api repos/o/r/collaborators/triager/permission"*) echo triage ;;
  "api repos/o/r --jq .owner.type"*) echo User ;;
  *) echo "unexpected gh call: $args" >&2; exit 1 ;;
esac
"""


def test_builder_counts_only_approvals_by_an_admin_or_maintainer(tmp_path):
    proc, out = _run_step(
        _step_script("claude-builder.yml", "build", "Check the queue"), tmp_path,
        {"CAP": "3", "AUTONOMOUS": "false", "REPO": "o/r", "REPO_OWNER": "o"},
        GH_QUEUE,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["authorized"] == "12"
    assert out["unauthorized_ideas"] == "#13, #14"
    assert out["go"] == "true"


def test_builder_stands_down_when_no_approval_is_the_owners(tmp_path):
    gh = GH_QUEUE.replace('"api repos/o/r/issues/12/events"*) echo owner ;;',
                          '"api repos/o/r/issues/12/events"*) echo triager ;;')
    proc, out = _run_step(
        _step_script("claude-builder.yml", "build", "Check the queue"), tmp_path,
        {"CAP": "3", "AUTONOMOUS": "false", "REPO": "o/r", "REPO_OWNER": "o"},
        gh,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["authorized"] == ""
    assert out["go"] == "false"


def test_builder_prompt_lists_unauthorized_approvals_as_off_limits():
    text = (WORKFLOWS / "claude-builder.yml").read_text()
    assert "${{ steps.gate.outputs.unauthorized_ideas }}" in text


# ── loop-inflight.mjs: what "covers" an idea ────────────────────────────────────

def _node(js: str):
    proc = subprocess.run(
        ["node", "--input-type=module", "-e",
         f"import * as m from {json.dumps(INFLIGHT.as_uri())};\n{js}"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        # Passing mentions from this repo's own merged PR bodies (#19, #21, #22).
        ("Issue #14 (next) will tune schedule/capacity buffers and #16 will add a Simulation page", []),
        ("Issue #16 will add a Benchmarks page that displays these results", []),
        ("This deliberately does NOT do #36; see #36 for why we punted.", []),
        ("part of #12, towards #13, for #14, idea #15", []),
        ("step #2 of the plan", []),
        ("Upstream bug: https://github.com/nodejs/node/issues/36 - waiting on them.", []),
        ("Fixes https://github.com/other/repo/issues/24", []),
        ("closes other/repo#22", []),
        # Deliberate references.
        ("Closes #12", [12]),
        ("fixes: #7 and Resolved #8", [7, 8]),
        ("refs #9", [9]),
        (f"Closes {SLUG}#21", [21]),
        (f"Fixes https://github.com/{SLUG}/issues/23", [23]),
        ("claude/issue-25-add-map", [25]),
    ],
)
def test_only_deliberate_references_to_this_repo_count(text, refs):
    got = _node(f"console.log(JSON.stringify(m.referencedIssues({json.dumps(SLUG)}, {json.dumps(text)})))")
    assert sorted(got) == refs


def test_a_passing_mention_or_a_fork_pr_never_covers_an_idea():
    """End to end through coversFor, the function the `check` command acts on."""
    js = f"""
    const repo = {json.dumps(SLUG)};
    const pr = (number, body, extra = {{}}) => ({{
      kind: "pr", number, title: "t", body, url: `https://github.com/${{repo}}/pull/${{number}}`,
      loop: false, fork: false, refs: m.referencedIssues(repo, "t", body), ...extra,
    }});
    const data = {{
      repo,
      openPrs: [pr(50, "See #42 for why we punted."), pr(51, "Closes #43", {{ fork: true }})],
      mergedPrs: [pr(52, "Closes #44")],
      branches: [], commits: [], ideas: [],
    }};
    const covered = [42, 43, 44].filter(
      (n) => m.coversFor({{ kind: "idea", number: n, title: "x", body: "y", url: "u" + n }}, data, "build", () => undefined).length > 0,
    );
    console.log(JSON.stringify(covered));
    """
    assert _node(js) == [44]


# ── validate-loop-config.sh ─────────────────────────────────────────────────────

def _validate(tmp_path: Path, content: str) -> subprocess.CompletedProcess:
    cfg = tmp_path / "loop-config.json"
    cfg.write_text(content)
    return subprocess.run(["bash", str(VALIDATOR), str(cfg)], capture_output=True, text=True, timeout=30)


def test_the_committed_loop_config_is_valid():
    proc = subprocess.run(["bash", str(VALIDATOR), str(REPO / ".github" / "loop-config.json")],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout
    assert json.loads((REPO / ".github" / "loop-config.json").read_text())["autonomousBuildEnabled"] is False


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{\"prCap\": 3,}",
        "[]",
        '{"prCap": -5}',
        '{"prCap": 3.7}',
        '{"prCap": "3"}',
        '{"autonomousBuildEnabled": null}',
        '{"autonomousBuildEnabled": "false"}',
        '{"inFlight": {"lookbackDays": 200}}',
        '{"scout": {"currentGoals": "one string"}}',
        '{"scout": {"staleCheck": {"enabled": "yes"}}}',
        '{"aiProvider": "openai"}',
    ],
)
def test_a_malformed_or_mistyped_loop_config_fails(tmp_path, content):
    assert _validate(tmp_path, content).returncode == 1


@pytest.mark.parametrize("content", ["{}", '{"prCap": "unlimited", "ideaQueueCap": 6}', '{"prCap": 0}'])
def test_a_well_formed_loop_config_passes(tmp_path, content):
    assert _validate(tmp_path, content).returncode == 0


def test_an_unknown_key_warns_but_passes(tmp_path):
    proc = _validate(tmp_path, '{"autonomusBuildEnabled": true}')
    assert proc.returncode == 0
    assert "::warning" in proc.stdout and "autonomusBuildEnabled" in proc.stdout


# ── Only the loop's own PRs spend Claude usage ──────────────────────────────────

def _triggers(wf: dict) -> dict:
    on = wf.get(True, wf.get("on"))  # PyYAML reads the bare key `on` as True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {k: None for k in on}
    return on or {}


def test_agent_workflows_on_pull_request_or_push_run_only_on_claude_branches():
    checked = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        if "anthropics/claude-code-action" not in text:
            continue
        wf = yaml.safe_load(text)
        if not {"pull_request", "push", "pull_request_target"} & set(_triggers(wf)):
            continue
        for job_name, job in wf["jobs"].items():
            cond = str(job.get("if", ""))
            assert (
                "startsWith(github.head_ref, 'claude/')" in cond
                or "startsWith(github.event.pull_request.head.ref, 'claude/')" in cond
            ), f"{path.name} job {job_name!r} runs a Claude agent on every PR; gate it on claude/ branches"
            checked.append(f"{path.name}:{job_name}")
    # Anti-vacuity: the Auditor and Demo are the two known PR-triggered agents.
    assert "claude-audit.yml:audit" in checked and "claude-demo.yml:demo" in checked
