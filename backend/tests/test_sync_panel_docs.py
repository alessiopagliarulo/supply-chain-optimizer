"""The collector's doc sync keeps the panel guard green, and only by telling the truth.

``scripts/sync_panel_docs.py`` runs inside .github/workflows/collect-lead-times.yml
between collecting a snapshot and pushing it to main. These tests simulate that
run on a copy of the repo: append a fake snapshot to the panel CSV, show the
UNCHANGED guard (tests/test_lead_time_panel_docs_pinned_to_csv.py) go red, run the
sync, and show the same guard go green. They also pin the properties that make the
sync safe to run unattended: it is idempotent, it never rewrites a served-model
figure, it refuses to write when a sentence was reworded, and a hand edit to a
wrong number is still caught by the guard.
"""
from __future__ import annotations

import csv
import importlib.util
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Tuple

import joblib
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


sync_panel_docs = _load("sync_panel_docs", BACKEND_ROOT / "scripts" / "sync_panel_docs.py")

#: The fake snapshot drops this many rows of the latest real one, so a stale
#: "742 on ..." cannot match the new count by accident.
DROPPED_ROWS = 11


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The files the sync reads and writes, copied into the repo's own layout."""
    for rel in (*sync_panel_docs.DOCS, sync_panel_docs.PANEL_CSV, sync_panel_docs.METRICS):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dest)
    return tmp_path


def _guard(root: Path):
    """A private instance of the real guard module, pointed at ``root``."""
    guard = _load(
        f"panel_guard_{id(root)}",
        BACKEND_ROOT / "tests" / "test_lead_time_panel_docs_pinned_to_csv.py",
    )
    guard.REPO_ROOT = root
    guard.PANEL_CSV = root / sync_panel_docs.PANEL_CSV
    guard.DOC_PATHS = {doc: root / doc for doc in sync_panel_docs.DOCS}
    return guard


def _run_guard(root: Path) -> None:
    guard = _guard(root)
    guard.test_panel_csv_parses_to_a_plausible_row_count()
    guard.test_every_scanned_doc_is_readable()
    guard.test_every_doc_states_the_panel_total_and_it_matches_the_csv()
    guard.test_per_snapshot_breakdowns_match_the_csv()


def _append_snapshot(root: Path) -> Tuple[str, str, int]:
    """What a collector run does to the CSV: append one dated snapshot, a week after
    the latest one on disk. Returns (previous latest date, new date, new row count)."""
    panel = root / sync_panel_docs.PANEL_CSV
    with panel.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fields = reader.fieldnames
    latest = max(r["snapshot_date"] for r in rows)
    new_date = (date.fromisoformat(latest) + timedelta(days=7)).isoformat()
    latest_rows = [r for r in rows if r["snapshot_date"] == latest]
    new = [dict(r, snapshot_date=new_date) for r in latest_rows[: max(1, len(latest_rows) - DROPPED_ROWS)]]
    with panel.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=fields, lineterminator="\n").writerows(new)
    return latest, new_date, len(new)


def _texts(root: Path) -> dict:
    return {doc: (root / doc).read_text(encoding="utf-8") for doc in sync_panel_docs.DOCS}


def test_the_committed_docs_already_carry_the_csv_figures():
    """Every rule matches its expected sentences in the committed docs, and they are
    current. A pull request that rewords one of them fails here, not on a Monday."""
    assert sync_panel_docs.sync(REPO_ROOT, check=True) == []


def test_a_simulated_collector_run_turns_the_guard_red_and_the_sync_turns_it_green(repo):
    _run_guard(repo)
    gap_before = len(sync_panel_docs.read_facts(repo).dates_since_cut)
    latest, new_date, n_new = _append_snapshot(repo)
    with pytest.raises(AssertionError):
        _run_guard(repo)

    assert sorted(sync_panel_docs.sync(repo)) == sorted(sync_panel_docs.DOCS)
    _run_guard(repo)

    facts = sync_panel_docs.read_facts(repo)
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert f"{n_new:,} on {new_date})" in readme
    assert f"`{facts.sha256[:8]}…` since the {new_date} collector run" in readme
    assert f"{sync_panel_docs._word(gap_before + 1)} snapshots behind the panel" in readme
    assert f"{latest} and {new_date} snapshots are not" in readme


def test_the_sync_is_idempotent(repo):
    _append_snapshot(repo)
    sync_panel_docs.sync(repo)
    once = _texts(repo)
    assert sync_panel_docs.sync(repo) == []
    assert _texts(repo) == once


def test_served_model_figures_are_never_rewritten(repo):
    """The training cut describes metrics.joblib, not the CSV. A collector run must
    leave every one of its figures, and the owed-retrain wording, exactly as it was."""
    served = ["3,351", "3,406", "d94df904", "2026-09-10", "six-snapshot", "324", "472",
              "+0.080", "−0.706", "first six", "retrain is still owed", "stale: true"]
    before = _texts(repo)
    _append_snapshot(repo)
    sync_panel_docs.sync(repo)
    after = _texts(repo)
    for doc in sync_panel_docs.DOCS:
        for token in served:
            assert after[doc].count(token) == before[doc].count(token), (doc, token)


def test_a_hand_edit_to_a_wrong_number_is_still_caught_by_the_guard(repo):
    readme = repo / "README.md"
    text = readme.read_text(encoding="utf-8")
    total = f"{sync_panel_docs.read_facts(repo).total:,}"
    readme.write_text(
        text.replace(f"{total} real observations", "5,000 real observations", 1), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="5,000"):
        _run_guard(repo)


def test_a_reworded_sentence_stops_the_sync_before_it_writes_anything(repo):
    overview = repo / "docs" / "PROJECT_OVERVIEW.md"
    overview.write_text(
        overview.read_text(encoding="utf-8").replace("snapshots behind the panel", "snapshots stale"),
        encoding="utf-8",
    )
    _append_snapshot(repo)
    before = _texts(repo)
    with pytest.raises(SystemExit, match="snapshots_behind"):
        sync_panel_docs.sync(repo)
    assert _texts(repo) == before


def test_it_refuses_to_describe_a_gap_once_the_model_has_caught_up(repo):
    metrics_path = repo / sync_panel_docs.METRICS
    metrics = joblib.load(metrics_path)
    facts = sync_panel_docs.read_facts(repo)
    metrics["provenance"]["n_panel_rows"] = facts.total
    metrics["provenance"]["n_snapshot_dates"] = len(facts.per_date)
    joblib.dump(metrics, metrics_path)
    with pytest.raises(SystemExit, match="retrain"):
        sync_panel_docs.sync(repo)
