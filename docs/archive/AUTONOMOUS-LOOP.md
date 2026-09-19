# The autonomous improvement loop

> **Historical, partly superseded.** This was the loop's original manual. The loop no
> longer builds without approval: `.github/loop-config.json` sets
> `autonomousBuildEnabled: false`, so the Builder builds ONLY issues a repository
> admin or maintainer labels `approved`, and there is no overnight cap lift. The
> Scout, Redraft agent and Builder also skip ideas already covered by open or draft
> PRs, recently pushed branches, recent merges and earlier ideas (label `covered`).
> The workflow headers in `.github/workflows/claude-*.yml` and
> [`DASHBOARD-CONTRACT.md`](DASHBOARD-CONTRACT.md) are the current source of truth.

How this repo improves itself while the computer is off, and what you do from your phone.

## What runs, and when

| Loop | When | What it does |
|---|---|---|
| **Scout** | Every hour | Researches the market and the codebase. Files issues labeled `proposal`. Stops at 8 open; never writes code. |
| **Builder** | When you label an issue `approved` (every 30 minutes as a backstop) | Builds one approved issue and opens one pull request - but only if your review queue has room. |
| **Auditor** | Every pull request | An independent agent attacks the PR from five angles and posts a verdict before you read it. |
| **Metrics** | Daily 7am | Recomputes `metrics/loop-metrics.json` from what actually merged, plus a local-only phone-readable summary. No agent, no tokens. |
| **Retro** | Sundays 6pm | Reads the week's real outcomes and proposes fixes to the loop itself. |
| **@claude** | Whenever you type it | Comment `@claude do X` on any issue or PR and an agent picks it up. |

## The queue rule — this is the important part

The loop only **drafts** on its own. **It never builds without your approval**: the Builder
builds an issue only after a repository admin or maintainer labels it `approved`. Your review
queue then throttles it:

- At most **3 agent pull requests** (`prCap`) may be open and waiting on you at once, at any
  hour. The Builder stands down when the queue is full. **Merge or close one, and the next
  approved issue gets built.**

## Your job (this is the whole manual)

1. **Review PRs whenever you have five minutes.** Read the plain-English description and the
   auditor's verdict. Merge, or comment what's wrong. Every one you clear frees a slot and
   pulls the next build forward. Your comments are what the retro learns from, so say why.
2. **To get something built: label an issue `approved`.** This is the only way anything gets
   built - the Builder never picks a proposal on its own.
3. **To ask for something specific:** comment `@claude <what you want>` on any issue or PR.
   An agent wakes up in the cloud and does it. This is your remote control, and it doesn't
   wait for any schedule.
4. **Sunday.** Skim the retro issue. It tells you whether this is working.

That's it. Everything else is automatic.

## The one number that matters

`metrics/loop-metrics.json` — the committed scorecard (a phone-readable rendering of it is
generated alongside but kept local). **Merge rate** is the health check. If you're
merging most of what the agents build, it's working. If you're throwing most of it away, the
loop is generating noise and the retro will tell you why.

Watch for the classic failure: **PR size climbing while merge rate falls.** That means the
agents are writing more and getting it right less. It's the single best early warning that the
loop has gone bad.

## Guardrails

- Agents never push to `main` and never merge their own work. **You merge. Always.** This is the
  real guardrail - the loop builds only what you approve, and nothing lands without you.
- Never more than 3 PRs waiting on you. The loop throttles itself to your
  review capacity instead of burying you.
- One PR per Builder run. No pile-ups from a single run.
- A blocked run comments on the issue and stops. It does not open a broken PR.
- The retro can only *propose* changes to the agents' own instructions, via a PR you merge.

## If something looks wrong

Comment `@claude` on any issue or PR and ask. It has full context on the repo and will answer
in plain English. To stop a loop entirely: Actions tab → the workflow → `···` → Disable.
