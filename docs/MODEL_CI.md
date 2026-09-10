# Model CI

**Every gate in this file exists because the defect it prevents actually shipped
from this repo.** None of them is a best practice copied from a blog post. They
are the postmortem, written as executable assertions.

On 2026-08-15 a read-only audit of the ML subsystem found six defects. All six
were live. All six were found by a human reading code. Not one was caught by the
420-test suite, because the suite tested that the code ran — not that the model
was fit to serve. That gap is what `model-ci` closes.

- **Workflow:** [`.github/workflows/model-ci.yml`](../.github/workflows/model-ci.yml)
- **Run it locally:** `cd backend && MODEL_CI_STRICT=1 pytest tests/ -m model_ci -v`
- **Marker:** `@pytest.mark.model_ci` (registered in `backend/pytest.ini`)

---

## Why a separate workflow rather than another job in `ci.yml`

`ci.yml` answers *"does the code work?"*. Model CI answers a different question —
*"is the model fit to serve?"* — and the two fail for different reasons, are
fixed by different actions, and want to be separately required in branch
protection. Three concrete reasons it is its own file:

1. **It runs with `MODEL_CI_STRICT=1`.** Every gate degrades to a clean skip on a
   checkout without artifacts, which is correct for `pytest tests/` and wrong for
   a gate: *a skipped gate is a green gate*. Strict mode promotes a skipped
   `model_ci` test to a **failure** (the hook lives in `backend/tests/conftest.py`).
   That flag must not leak into the ordinary suite, and scoping it to its own
   workflow is the honest way to do that.
2. **It must also run when only *data* changes.** `collect-lead-times.yml` commits
   a fresh DigiKey cross-section to the observed panel every Monday. That push
   contains no code, and the resulting staleness still has to be reported.
3. **Its output is a report, not a pass/fail bit.** The job writes provenance, the
   ship-gate verdict, the baseline comparison, the staleness answer and the
   leakage audit into the GitHub step summary on *every* run, including green
   ones. Folded into a generic "Backend tests" job, all of that is buried.

The contract gates **retrain on the committed observed panel** before asserting,
so this is a real retrain-and-verify, not an inspection of a stale blob.

---

## The gates, and the bug each one prevents

### 1. Train/serve feature-schema divergence

`backend/tests/test_lead_time_schema_contract.py`

**The bug.** Training (`build_observed_matrix`) emitted
`['is_active','log_stock','macro_stress','cat_<5>','src_digikey']`. Serving
(`build_feature_row` + `_align_row`) emitted a completely different set,
one-hot-encoded with the prefix `category_` instead of `cat_`. `_align_row`
reindexed onto the training columns and **zero-filled every name that did not
match** — which was all of them but one. The served vector was the constant
`[0,0,0.9967,0,0,0,0,0,0]`, so **every prediction in production was 62.1085
days**, for every part, forever. Meanwhile `/ml/model-comparison` published
**R² = 0.9291** — a number computed on a configuration that was never served.

**The gate.** One schema object (`ResolvedSchema`), one encoder (`_fill`), and
tests that assert the serving path reproduces training's columns *in the same
order*, that persisted columns round-trip through the parser, that the serving
vector width matches the fitted estimator, and that every kept training row
rebuilt through the *serving* entrypoint is bit-identical to the row training
produced.

### 2. A model that loses to its own baseline shipping anyway

`backend/tests/test_model_ci_gates.py::test_committed_lead_time_artifact_passed_its_ship_gate`
`…::test_committed_lead_time_artifact_beat_every_baseline`
`…::test_committed_regime_artifact_agrees_with_its_ship_gate`

**The bug.** `retrain_lead_time` computed `beats_baselines`, logged it, and then
persisted the model **regardless of the answer**. Comparing against a baseline
was a reporting habit, not a decision. The regime model shipped at val_accuracy
0.7333 against a persistence baseline of 0.8333 — it lost, and it served anyway.

**The gate.** `evaluate_lead_time_ship_gate` now refuses to persist, and deletes
any previously-persisted artifact, unless the champion (a) beats **every** naive
baseline on mean grouped-CV RMSE and (b) beats the **toughest** one by a margin
whose paired bootstrap CI excludes zero. Baselines are deliberately strong:
train-mean, always-210d, a category-mean lookup table, and a manufacturer-mean
lookup table. *A lookup table that beats the model is the model.*

The CI gate then checks the **artifact sitting in the repo** — because a gate you
can bypass by committing a joblib by hand is not a gate. It also asserts the
reverse for the regime model: if `regime.joblib` is on disk, its recorded ship
gate must have passed; a failing model must have been deleted, not left to answer.

Current verdict, for the record:

| | verdict |
| --- | --- |
| lead-time | PASS — `gradient_boosting` beats all 4 baselines; vs `manufacturer_mean`, mean RMSE reduction **4.707 d**, 95% CI **[2.677, 7.002]**, won **19/20** folds |
| regime | PASS — Brier **0.393** beats persistence 0.539 and climatology 0.673; calibration slope 0.629 |

### 3. Serve-time coverage below a floor

`backend/tests/test_serve_coverage.py`

**The bug.** Feature admission asked *"does this column exist on the ORM
model?"*. `standard_pack` and `packaging` exist on `DistributorOffer` — and are
populated for DigiKey's own offers only, **571 of 8,176 rows (7.0%)**. Both were
admitted, and then every non-DigiKey offer raised `MissingFeatureError` at serve
time. The optimizer's `supply_risk.model_available` came back false on **6 of 6**
sampled runs. Having replaced a model that always answered with a constant, we
had produced a model that almost never answered at all.

**The gate.** `serve_availability` now *measures* coverage in the real database
and excludes anything under `MIN_SERVE_COVERAGE = 0.50`, with the measured
percentage in the exclusion reason. CI asserts, against the committed
`supply_chain.db`, that the served model answers for **≥ 80%** of real
`(offer, component)` pairs and that the optimizer's own call path —
`ml_factory_lead_time_days`, the exact function `solve.py` uses — clears the same
floor. This is a floor against *collapse*, not a performance target.

**The measured answer rate, and which denominator it uses.** "Coverage" names two
different quantities in this repo, so both are stated with their denominator:

| Quantity | Denominator | Measured |
|---|---|---:|
| **Answer rate over `(offer, component)` pairs** — the CI gate's own denominator (`tests/test_serve_coverage.py`) | 8,176 offer×component pairs | **97.85%** (8,000) |
| Answer rate over distinct components, via `GET /api/v1/ml/lead-time` | 791 components | **93.05%** (736) |
| Per-feature *column fill rate* (`measure_serve_coverage`, the 0.50 floor) | the table the feature lives on | per column; `standard_pack` 7.0% |

**97.85% is the figure this doc quotes**, because it is the one the gate measures;
the 93.05% component-level rate is given alongside because the endpoint answers per
component, not per offer. They are the same defect counted twice: 55 components have
`digikey_category IS NULL`, and those 55 own exactly 176 offers — 8,176 − 176 = 8,000
and 791 − 55 = 736.

> **Corrected 2026-08-16.** This paragraph previously read "measured: 94.4%". That
> number matches neither denominator. It was measured before the tracked
> `supply_chain.db` was rebuilt with the backfilled DigiKey columns and was never
> re-run against the current data. The comment above `MIN_ANSWER_RATE` in
> `tests/test_serve_coverage.py` still carries the stale 94.4%; the assertion itself
> is the 80% floor, so nothing was gated on it.

### 4. A near-constant predictor

`backend/tests/test_model_ci_gates.py::test_served_predictions_are_not_near_constant`

**The bug.** The other half of bug 1, and the reason it survived: every existing
test built its own toy schema, so nothing ever ran the *committed artifact* over
*real inputs* and looked at the spread of what came out.

**The gate.** Load the committed artifacts exactly as the API process does, score
every row of the committed database, and require a coefficient of variation
≥ 0.02 and ≥ 25 distinct predicted values. The constant-predictor bug produced a
CV of exactly zero and one distinct value. The contract file additionally
requires that *each of the four* estimators in the bake-off varies — a constant
runner-up is also a broken pipeline — and that the numeric block moves the output
at all, rather than being inert.

### 5. An endpoint whose declared inputs don't cover its model's requirements

`backend/tests/test_serve_coverage.py::test_lead_time_endpoint_declares_every_required_input`

**The bug.** The resolved schema grew a `parameter_count` requirement. The
`/ml/lead-time` FastAPI signature did not follow. FastAPI validated the request
before the model was ever consulted, so the endpoint returned **422 on every
call**. The model was fine; it was simply unreachable.

**The gate.** Introspect `inspect.signature(predict_lead_time_endpoint)` and
assert it accepts every key in `required_record_keys(...) | optional_record_keys(...)`
for the *currently served* schema. Adding a feature can no longer silently make
the endpoint un-callable.

### 6. An artifact with no provenance

`backend/tests/test_model_ci_gates.py::test_committed_artifact_records_full_provenance`
`…::test_provenance_row_count_agrees_with_the_published_sample_size`
`…::test_model_info_publishes_the_fit_time_provenance`

**The bug.** `metrics.joblib` carried no `trained_at`, no training-data hash, no
row count and no git SHA. There was no way to say *which data produced which
model* — which is precisely why nobody noticed that a published R² described a
configuration nobody was serving, or that the "training sample size" being
reported (8,731) was the offer count rather than the panel's row count (810 rows
when that bug was found; 3,351 today).

**The fields**, stamped at fit time by `model_store.build_provenance()` and
persisted into `metrics.joblib`. The values below are **the committed artifact's
own**, not illustrative ones — `tests/test_docs_match_artifacts.py` reads them
back out of `metrics.joblib` and fails if this table drifts:

| field | this artifact | why |
| --- | --- | --- |
| `trained_at` | `2026-09-10T04:26:01+00:00` | when |
| `git_sha` | `65bbab70…-dirty` (the `-dirty` suffix is stamped when the tree was modified; it was, and that is recorded rather than hidden) | at which commit, and whether it was reproducible |
| `sklearn_version` | `1.8.0` | unpickling an estimator across versions is not guaranteed |
| `training_data_path` | `…/observed_lead_times.csv` | which file |
| `training_data_sha256` | `d94df904…` | which *bytes* — the basis of the staleness check |
| `n_training_rows` | `3351` | rows the model actually fitted on |
| `n_panel_rows` | `3406` | rows in the panel before drops (never imputed) |
| `n_distinct_families` | `472` | the fold-group count — distinct `_group_key` values, **not** the 361 distinct `base_product` values; the real unit of generalisation, not the row count |
| `n_snapshot_dates` | `6` | how much time the panel spans |
| `lead_time_status` | `trained` | that this was a real fit, not a skipped run |

**The gates.** `missing_provenance_fields()` returns every required field that is
absent *or null* (a null field counts as missing, not as present), and CI fails if
the list is non-empty. A second gate asserts `n_training_rows` equals the sample
size published by the API, so one number cannot mean two things. A third asserts
the whole block is actually reachable at `GET /api/v1/ml/model-info` — provenance
that is recorded but not published is not provenance.

### 7. A gate that silently stops testing

`backend/tests/test_lead_time_schema_contract.py` — the `META` block at the bottom

**The bug — the one that matters most here.** The variance tests were written to
prove that changing the category changes the prediction. They then **kept passing
while testing nothing.** DigiKey's `dk_category` became the model's canonical
(refusing) categorical, but the tests still hardcoded the record key `category`.
So they varied a *secondary* feature — one the estimator could legitimately
ignore — over a constant predictor, and reported green.

**The gate.** Six meta-tests that watch the watcher:

- every name in `PRIMARY_CATEGORY_FEATURES` is a declared `CategoricalSpec`, so a
  rename in the spec table breaks **here**, loudly, instead of degrading
  `primary_category_feature()` to a silent fallback;
- at least one candidate carries `unseen_policy="refuse"` — the refusal policy is
  what *makes* a feature primary;
- the resolved primary feature is actually in the served schema and was not
  excluded at fit time;
- if a refusing candidate survived admission, it **must** be the one chosen —
  declaration order may not quietly promote the fallback taxonomy;
- the variance helper mutates **exactly** the primary feature's record key and
  nothing else, and at the vector level that mutation moves **only** that
  feature's own one-hot block (a key nothing reads would leave the encoded row
  byte-identical, and every downstream assertion would be comparing a number
  against itself);
- `known_categories()` — the vocabulary every variance test loops over — reports
  that same feature's levels, and there are at least two of them.

**Strict mode is part of this gate.** A test that vanishes is as dangerous as one
that no-ops, so `MODEL_CI_STRICT=1` turns any skipped `model_ci` test into a
failure. In CI the artifacts, the panel and the database are all committed —
there is no legitimate reason for a gate not to run.

---

## Staleness: a warning, deliberately not a failure

`model_store.check_training_data_staleness()` compares the `training_data_sha256`
recorded in the artifact against the SHA-256 of the panel in the checkout. It is
surfaced three ways: a `WARNING` in the API startup log, the
`training_data_stale` / `staleness_detail` fields on `GET /api/v1/ml/model-info`,
and a `::warning::` annotation plus a section in the model-CI step summary.

**It never fails the build, and that is a design decision, not laziness.** A
GitHub Action appends a fresh DigiKey cross-section to the panel every Monday and
commits it; the models are retrained by hand. Failing on a hash mismatch would
turn every collector commit red, and a check that is red for a legitimate reason
every week is a check people learn to ignore. Warning instead makes the
collector's growth *visible* — "the panel moved and the served model has not seen
the new rows" — which is exactly the fact that used to be silent.

`test_staleness_detects_a_changed_panel` proves the warning can actually fire, in
both directions. A warning nobody can trigger is decoration.

---

## What model CI does *not* claim

- It does not prove the model is *good*. It proves the model beats its stated
  baselines on family-grouped folds, answers for real inputs, and is honestly
  described. For what the model can actually do, read
  **[`docs/LEAKAGE_PROGRESSION.md`](LEAKAGE_PROGRESSION.md)** and its artifact
  [`docs/leakage_progression.json`](leakage_progression.json), regenerated by
  `python -m seeds.run_leakage_progression`. Measured over 50 folds
  (5-fold × 10 shuffles, seed 42), same estimator, same rows, same pipeline —
  only the grouping changes:

  | Split regime | R² mean | R² median | fold sd |
  |---|---:|---:|---:|
  | random rows (the wrong protocol) | **+0.839** | +0.842 | 0.018 |
  | `GroupKFold` by part-family key (`_group_key`) | **+0.080** | +0.139 | 0.242 |
  | `GroupKFold` by manufacturer | **−0.706** | −0.107 | 2.509 |

  **3,351 rows, 472 family grouping keys, 28 manufacturers.** The effective sample
  size for generalisation is the **28 manufacturers**, not the 3,351 rows. That
  collapse is the finding, and publishing only the first number would be the most
  misleading thing this repo could do.

  **472 is not a count of part families.** The fold groups are
  `lead_time_model._group_key` outputs — `family:{base_product}` where DigiKey
  returned a base product, `mpn:{mpn}` where it did not — so the key count is a
  strict refinement of the **361 distinct `base_product` values** on the panel
  (361 real families + 111 MPNs split out of the `Unknown` bucket = 472). Earlier
  revisions of this line said "467 part families", which attached the grouping-key
  number to the base-product name *and* quoted a retired panel vintage;
  `lead_time_model._group_key`'s own docstring states the same arithmetic.

  The negative number is worth stating precisely: R² is measured against the
  *held-out fold's own* mean, so R² < 0 means the squared error exceeds that
  vendor's entire label variance — on a vendor it has never quoted the model has
  no explanatory power at all. It is *not* beaten by the trivial predictors on
  those folds (`train_mean` scores −2.114 there), so the honest reading is: the
  model is the best member of a set in which **nothing generalises to an unseen
  vendor.**

  `lead_time_leakage_audit` in `metrics.joblib` reports the same collapse from
  the training run, on the same 3,351 rows and the same `gradient_boosting`
  champion, under 20 repeated `GroupShuffleSplit` 80/20 holdouts rather than
  `GroupKFold`: **+0.8367 → +0.1276 → −0.5174** (medians +0.8384 / +0.1797 /
  −0.1771). Read this as two protocols measuring one phenomenon, not as two
  attempts at one number. The random and family rungs land in the same place; the
  manufacturer *means* do not (−0.517 vs −0.706), because that mean is dominated
  by folds whose held-out vendor barely varies in `y`, and a 5-fold `GroupKFold`
  test side (5–6 vendors) is a different draw from a 20% `GroupShuffleSplit` one.
  The **medians** — −0.177 and −0.107 — are the comparable pair, and they agree.
  The published numbers are the `GroupKFold` ones above, because those are the
  ones a reader can reproduce with a single command that does not retrain
  anything; `lead_time_leakage_audit` is the one `GET /api/v1/ml/model-comparison`
  serves and the one its `caveat` string interpolates.

  *This is the paragraph that rotted.* Until 2026-08-26 it quoted
  "+0.612 → +0.189 → −0.476" — a `random_forest` champion on an 810-row,
  27-manufacturer panel that had already been retrained away, while the API's
  own `caveat` quoted a third, equally retired trio. Both are now derived rather
  than transcribed, and `tests/test_docs_match_artifacts.py` reads
  `metrics.joblib` directly so the next retrain that moves these numbers fails CI
  instead of publishing quietly.
- It does not do drift detection on live traffic, shadow deployment, or automated
  retraining. The weekly collector grows the panel; retraining is a human action.
- It does not gate demand forecasting. The synthetic per-part path
  (`train_forecasts.py`, a Prophet fit over inventory-derived, not measured,
  demand) was retired outright rather than gated — migration
  `0008_drop_synthetic_demand_tables.py` drops the tables it read from. What
  replaced it, the intermittent-demand method benchmark
  (`GET /api/v1/demand/benchmark`, `docs/intermittent_demand.json`), is a
  research artifact regenerated by a script and read at request time, not a
  fitted model this repo trains and serves — so there is nothing here for model
  CI to gate yet. Connecting that benchmark to a served decision is open work
  tracked in `docs/archive/ML_API_PUSH_PLAN.md`.
