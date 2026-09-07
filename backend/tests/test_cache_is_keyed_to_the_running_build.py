"""The scenario cache must not outlive the code that wrote it.

THE DEFECT THESE COVER (2026-09-01, hit in production, not theoretical).

``CacheManager.generate_key`` hashed only ``scenario_type`` + params. The cache
table lives in the TRACKED ``backend/supply_chain.db`` with a 1-hour TTL, so
after a deploy that changed a served string or a computed value the API kept
serving the OLD body for up to an hour, out of a cache that had no way to tell
the code had changed.

It was hit for real: after ``app/api/resilience.py``'s ``_hedging_summary`` /
``_fulfilment_clause`` stopped claiming "Zero fulfillment impact" when the
endpoint's own fulfilment fields disagreed, the first boot after the fix served
the retired sentence from cache, with ``hedging.baseline_fulfillment_p50``
null. The site published a claim the code no longer makes — the exact thing the
standing bar forbids.

CI cannot catch this. It builds a fresh database with an empty cache, so the
cross-build read never happens there. Only the deployed artifact carries rows
written by an earlier build. That is the same shape as the 2026-08-29 alembic
incident recorded in this repo's maintainer notes.
"""
import hashlib

import pytest

from app.cache import CacheManager
from app.core import version as version_module
from app.core.version import build_commit, code_version, fingerprint_of
from app.models.scenario import ScenarioCache

# Two plausible deployed SHAs. Nothing about the values matters except that they
# differ, the way HEAD differs across a deploy.
SHA_BEFORE = "1c00994a1c00994a1c00994a1c00994a1c00994a"
SHA_AFTER = "646bb66b646bb66b646bb66b646bb66b646bb66b"

# The sentence the fix retired, as it was actually cached.
RETIRED_BODY = {
    "hedging": {
        "note": "Zero fulfillment impact under this scenario.",
        "baseline_fulfillment_p50": None,
    }
}

SCENARIO = "distributor-failure"
PARAMS = {"distributor_id": 7, "bom": [[1, 10], [2, 4]], "quantity_source": "stated"}


def test_a_deploy_invalidates_the_cache_instead_of_serving_the_previous_build(
    db_session, monkeypatch
):
    """Cache under one build, redeploy, and the old body must NOT come back.

    This is the regression test for the incident. Without the ``code_version()``
    component in the key, the two keys are identical and the second read returns
    the retired sentence.
    """
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_BEFORE)
    key_before = CacheManager.generate_key(SCENARIO, PARAMS)
    CacheManager.set(db_session, key_before, SCENARIO, RETIRED_BODY)

    # Sanity: within one build the cache is still a cache.
    assert CacheManager.get(db_session, key_before) == RETIRED_BODY, (
        "the cache must still hit for the build that wrote the entry"
    )

    # The deploy.
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)
    key_after = CacheManager.generate_key(SCENARIO, PARAMS)

    assert key_after != key_before, (
        "the same request hashed to the same key across two different builds — "
        "the cache cannot tell the code changed, which is exactly how the "
        "retired 'Zero fulfillment impact' sentence was served after the fix"
    )
    assert CacheManager.get(db_session, key_after) is None, (
        "a request served by new code read an entry written by old code"
    )


def test_the_cached_body_of_a_previous_build_is_unreachable_by_content(
    db_session, monkeypatch
):
    """Belt and braces: no key the new build can compute reaches the old row.

    The row is still on disk until it is purged or expires; what must be true is
    that the running build has no key that returns it.
    """
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_BEFORE)
    CacheManager.set(
        db_session, CacheManager.generate_key(SCENARIO, PARAMS), SCENARIO, RETIRED_BODY
    )
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)

    for params in (PARAMS, dict(PARAMS), {**PARAMS}):
        assert CacheManager.get(db_session, CacheManager.generate_key(SCENARIO, params)) is None


def test_purge_foreign_versions_removes_other_builds_entries_and_keeps_ours(
    db_session, monkeypatch
):
    """Rows dead on arrival must not sit in the tracked DB until they expire."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_BEFORE)
    CacheManager.set(
        db_session, CacheManager.generate_key(SCENARIO, PARAMS), SCENARIO, RETIRED_BODY
    )
    CacheManager.set(
        db_session,
        CacheManager.generate_key("geopolitical-risk", {"country": "CN"}),
        "geopolitical-risk",
        {"ok": True},
    )

    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)
    mine = CacheManager.generate_key(SCENARIO, PARAMS)
    CacheManager.set(db_session, mine, SCENARIO, {"ok": "current build"})

    assert db_session.query(ScenarioCache).count() == 3

    purged = CacheManager.purge_foreign_versions(db_session)

    assert purged == 2, "both entries from the previous build should have gone"
    remaining = db_session.query(ScenarioCache).all()
    assert [r.cache_key for r in remaining] == [mine]
    assert CacheManager.get(db_session, mine) == {"ok": "current build"}, (
        "the purge must not touch the running build's own entries"
    )


def test_purge_is_a_no_op_when_every_entry_belongs_to_the_running_build(db_session):
    CacheManager.set(
        db_session, CacheManager.generate_key(SCENARIO, PARAMS), SCENARIO, {"ok": True}
    )
    assert CacheManager.purge_foreign_versions(db_session) == 0
    assert db_session.query(ScenarioCache).count() == 1


def test_the_key_still_fits_the_column(monkeypatch):
    """cache_key is String(512); the version prefix must not need a migration."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)
    key = CacheManager.generate_key(SCENARIO, PARAMS)
    prefix, _, digest = key.partition(":")
    assert len(key) == 77 <= 512
    assert prefix == code_version()
    assert len(digest) == 64
    assert int(digest, 16) >= 0  # hex


def test_params_still_decide_the_key_within_one_build(monkeypatch):
    """The version component must not swallow the params component."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)
    a = CacheManager.generate_key(SCENARIO, PARAMS)
    b = CacheManager.generate_key(SCENARIO, {**PARAMS, "distributor_id": 8})
    c = CacheManager.generate_key("geopolitical-risk", PARAMS)
    assert len({a, b, c}) == 3
    assert a == CacheManager.generate_key(SCENARIO, dict(reversed(list(PARAMS.items()))))


def test_version_endpoint_and_cache_key_read_the_same_build_identity(monkeypatch):
    """One mechanism, not two. /version must not disagree with the cache."""
    from app.main import version_info

    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)
    assert version_info()["commit"] == build_commit() == SHA_AFTER


def test_the_guard_is_not_silently_disabled_when_no_commit_is_available(monkeypatch):
    """No env, no git — the fallback must still distinguish two builds.

    "unknown" as the whole signal would collapse every build to one token and
    turn the guard off without saying so. ``code_version()`` therefore always
    mixes in a content fingerprint of ``backend/app/**/*.py``.
    """
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setattr(version_module, "_git_head", lambda: "")
    assert build_commit() == "unknown"

    monkeypatch.setattr(version_module, "source_fingerprint", lambda: "a" * 64)
    with_old_source = code_version()
    monkeypatch.setattr(version_module, "source_fingerprint", lambda: "b" * 64)
    with_new_source = code_version()

    assert with_old_source != with_new_source, (
        "with no commit available the cache key stopped distinguishing builds"
    )
    assert len(with_old_source) == 12


def test_the_source_fingerprint_tracks_content_not_timestamps(tmp_path):
    """The fallback signal must move when the code moves — and only then."""
    pkg = tmp_path / "app"
    (pkg / "api").mkdir(parents=True)
    module = pkg / "api" / "resilience.py"
    module.write_text("NOTE = 'Zero fulfillment impact'\n")
    (pkg / "__init__.py").write_text("")

    before = fingerprint_of(pkg)

    # Touching without editing must not invalidate the cache.
    module.touch()
    assert fingerprint_of(pkg) == before

    # Editing must.
    module.write_text("NOTE = 'fulfilment fields disagree'\n")
    after_edit = fingerprint_of(pkg)
    assert after_edit != before

    # So must adding a module, and deleting one.
    (pkg / "cache.py").write_text("TTL = 3600\n")
    after_add = fingerprint_of(pkg)
    assert after_add not in (before, after_edit)
    (pkg / "cache.py").unlink()
    assert fingerprint_of(pkg) == after_edit


def test_the_real_app_source_fingerprint_is_a_real_hash():
    """Guard against the fingerprint quietly degrading to a constant."""
    fp = version_module.source_fingerprint()
    assert len(fp) == 64
    assert fp != hashlib.sha256(b"").hexdigest()
    assert version_module.APP_ROOT.name == "app"
    assert (version_module.APP_ROOT / "cache.py").exists()


@pytest.mark.parametrize("scenario_type", [
    "distributor-failure",
    "geopolitical-risk",
    "delivery-target",
    "criticality-sweep",
    "dual-sourcing-plan",
    "sensitivity",
    "cvar-frontier",
])
def test_every_cached_endpoint_is_covered_by_the_version_prefix(scenario_type, monkeypatch):
    """All eight cache users go through generate_key, so all of them are pinned."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_BEFORE)
    old = CacheManager.generate_key(scenario_type, {"x": 1})
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA_AFTER)
    assert CacheManager.generate_key(scenario_type, {"x": 1}) != old
