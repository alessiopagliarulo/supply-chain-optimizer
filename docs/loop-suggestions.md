# Loop suggestions

Proposals from the weekly retro for changes to the loop's workflows and scripts.
The owner applies template changes from the dashboard — nothing here is applied
automatically, and the retro cannot edit `.github/workflows/` itself.

## 2026-09-13 — loop-metrics.yml (`scripts/loop-metrics.mjs`)

**Problem:** the scorecard has reported the loop at **2 PRs opened, 2 merged, 100%
merge rate, 421-line median** on every one of its 37 recorded days, while the loop
has authored **zero** PRs in this repo. Both counted PRs are the owner's own July
bootstrap PRs — #1 `claude/install-autonomous-loop` and #2 `claude/loop-fixes`, both
`author.login = alessiopagliarulo`. They land in the agent slice because `isAgentPr`
classifies by branch prefix alone, and the owner used `claude/` branches to install
the loop. The same snapshot reports `prs_opened_human: 0`, so the human slice that
was added specifically to stop the loop from claiming the owner's work is empty while
the owner's work sits in the loop's column.

The cost is not cosmetic. This file is what the retro and the Scout read to decide
whether the loop is working. For 61 days it said the loop was batting 1.000 during a
period when Scout and Builder stood down on every single run.

**Suggested prompt change:**

```diff
-const isAgentPr = (pr) => pr.headRefName?.startsWith("claude/");
+// Branch prefix alone is not enough: the owner installed this loop from `claude/`
+// branches (#1, #2), and those PRs were counted as loop output for 37 days. A PR is
+// the loop's only if an agent actually opened it.
+const AGENT_LOGINS = new Set(["claude", "app/claude", "github-actions[bot]"]);
+const isAgentPr = (pr) =>
+  pr.headRefName?.startsWith("claude/") &&
+  AGENT_LOGINS.has(pr.author?.login ?? "");
```

This needs `author` added to the existing `--json` field list on the `gh pr list`
call two lines below (`"number,title,state,headRefName,..."` → `...,author`).

**Why it should work:** authorship is the fact the metric is actually trying to
capture, and it is already available from the same API call. With this applied,
today's snapshot reads `prs_opened: 0, merge_rate_pct: null` — which is the true and
much more useful statement that the loop has not yet shipped anything here.

**Second, smaller ask in the same file:** every PR figure is all-time, so the history
array cannot show a trend — 37 identical rows. A rolling 7-day slice alongside the
all-time one (`prs_opened_7d`, `prs_merged_7d`, `merge_rate_pct_7d`) would let the
retro answer "is the merge rate falling while PR count rises?" from the data instead
of by hand.
