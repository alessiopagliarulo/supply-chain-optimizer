#!/usr/bin/env node
/**
 * Work in flight — what already exists, so the loop never drafts or builds it twice.
 *
 * THE PROBLEM THIS EXISTS TO SOLVE
 * --------------------------------
 * The owner does not only work through the loop's queue. They open Claude Code on
 * their own machine, build something by hand, push a branch, open a draft PR. Until
 * now the Scout, the Redraft agent and the Builder saw only the loop's own issues and
 * a short slice of `main`, so they kept proposing — and building — things that were
 * already sitting on a branch, in a draft PR, or merged last week.
 *
 * WHAT IT GATHERS (all read-only, through `gh`)
 * ---------------------------------------------
 *   - every open pull request, drafts flagged, whoever opened it;
 *   - every branch pushed in the last N days that has no open PR yet (the owner's
 *     local work, the moment it is pushed), with the files it changes;
 *   - pull requests merged in the last N days;
 *   - commits that landed on the default branch in the last N days;
 *   - every idea already filed: open proposals, approved, redrafting, and closed or
 *     declined ones.
 * N is `inFlight.lookbackDays` in .github/loop-config.json (default 14, 1–90).
 *
 * THREE COMMANDS
 * --------------
 *   node scripts/loop-inflight.mjs collect --out FILE
 *       Gathers the above into a JSON file. Each query is best-effort: one that
 *       fails is recorded as unavailable and the rest still arrive.
 *   node scripts/loop-inflight.mjs digest --in FILE
 *       Prints a compact, prompt-safe summary for the agents to read.
 *   node scripts/loop-inflight.mjs check --in FILE --issues 12,13 --mode new|build [--dry-run]
 *       The deterministic backstop. Compares each issue against the work above with
 *       the SAME duplicate detector the dashboard uses (MiniLM, the same text recipe,
 *       the same calibrated 0.828 threshold — see docs/ml-dedup.md in the dashboard
 *       repo), and marks a covered one with the `covered` label plus a comment that
 *       links to what covers it. It never closes, never re-labels anything else.
 *       Needs LOOP_EMBED_DIR, a folder for @huggingface/transformers and the model
 *       weights; installed there on demand, and the workflows cache that folder.
 *       Sets the step output `embed=loaded` once the detector has loaded.
 *   node scripts/loop-inflight.mjs embed-key
 *       Sets the step output `key`: the cache key for LOOP_EMBED_DIR, from the pinned
 *       library version and model, the runner's OS and CPU architecture.
 *
 * NOTHING HERE MAY FAIL A RUN. Every command exits 0 and says what it could not do
 * as a `::warning::`. A missing digest makes the agents slightly blinder; a red
 * scheduled workflow emails the owner every hour.
 *
 * Dependency-free Node (the embedding library is loaded from LOOP_EMBED_DIR only by
 * `check`). The dashboard repo's tests import the pure functions below directly.
 */
import { execFileSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

/* ------------------------------------------------------------------ */
/* Constants — the contract with the dashboard                         */
/* ------------------------------------------------------------------ */

export const DEFAULT_LOOKBACK_DAYS = 14;
export const MAX_LOOKBACK_DAYS = 90;

/** The flag this script (and the agents) put on an idea that existing work covers. */
export const COVERED_LABEL = "covered";
export const COVERED_LABEL_COLOR = "5319E7";
export const COVERED_LABEL_DESCRIPTION =
  "Existing work (a PR, branch, commit or idea) already seems to cover this — the owner decides";

/**
 * First line of every "covered" comment. The dashboard finds the comment by it and
 * reads the links under it; a cleared cover is never re-applied automatically
 * because this marker is still on the thread.
 */
export const COVERED_MARKER = "<!-- loop:covered -->";

/**
 * The duplicate detector, exactly as the dashboard runs it (lib/dedup/): the local
 * MiniLM encoder, mean-pooled and L2-normalised, over `docText` below. The threshold
 * is dense_local's precision-first operating point in metrics/dedup-eval.json
 * (precision 0.95, recall 0.76), and it only holds for texts at least as long as the
 * shortest positive pair it was fitted on — shorter texts are not scored at all,
 * because short-vs-long cosine is depressed regardless of meaning. The dashboard's
 * tests pin every one of these to its own copy.
 */
export const EMBED_MODEL = "Xenova/all-MiniLM-L6-v2";
export const EMBED_THRESHOLD = 0.828;
export const MIN_CALIBRATED_CHARS = 950;
export const MAX_EMBED_CHARS = 2000;
/** The embedding library, pinned to the version the dashboard's numbers came from. */
export const EMBED_LIB = "@huggingface/transformers";
export const EMBED_LIB_VERSION = "4.2.0";

/** At most this many links in one covered comment. */
const MAX_COVER_LINKS = 3;
/** Per-section cap in the digest — the agents can always ask `gh` for more. */
const DIGEST_SECTION_CAP = 30;
/** Branches whose changed files are looked up (one API call each). */
const MAX_BRANCHES_DETAILED = 15;
/** Bodies are kept long enough to judge length, and no longer. */
const MAX_BODY_CHARS = 6000;

/* ------------------------------------------------------------------ */
/* Small pure helpers                                                  */
/* ------------------------------------------------------------------ */

/** `inFlight.lookbackDays` from the loop config text, clamped; default on anything odd. */
export function readLookbackDays(configText) {
  let n;
  try {
    n = JSON.parse(configText ?? "")?.inFlight?.lookbackDays;
  } catch {
    return DEFAULT_LOOKBACK_DAYS;
  }
  if (typeof n !== "number" || !Number.isFinite(n)) return DEFAULT_LOOKBACK_DAYS;
  return Math.min(MAX_LOOKBACK_DAYS, Math.max(1, Math.round(n)));
}

/**
 * Same four-pattern author test the Scout's gate and stale check use on `git log`,
 * kept identical so the loop cannot disagree with itself about who wrote what.
 */
export function isLoopAuthor(text) {
  const t = String(text ?? "").toLowerCase();
  return t.includes("claude") || t.includes("github-actions") || t.includes("[bot]") || t.includes("anthropic");
}

/** Identical to stripMarkdown in the dashboard's lib/dedup/baseline.ts. */
export function stripMarkdown(md) {
  return String(md ?? "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[#>*_`|~-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Identical to docText in lib/dedup/baseline.ts: title weighted x2, stripped body. */
export function docText(doc) {
  const title = doc?.title ?? "";
  return `${title} ${title} ${stripMarkdown(doc?.body ?? "")}`.trim();
}

/**
 * Issue numbers a PR, branch or commit deliberately points at. Only the forms people
 * use on purpose count — "closes #12", "refs #12", "issue #12", an `/issues/12` link,
 * an `issue-12` branch segment — never a bare "#2", which is as often "step #2" as
 * it is an issue, and a false reference would flag an idea covered by unrelated work.
 */
export function referencedIssues(...texts) {
  const out = new Set();
  for (const text of texts) {
    const s = String(text ?? "");
    const keyword = /\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|references|see|issue|idea|part of|towards?|for)\s*:?\s*#(\d+)\b/gi;
    for (const m of s.matchAll(keyword)) out.add(Number(m[1]));
    for (const m of s.matchAll(/github\.com\/[\w.-]+\/[\w.-]+\/issues\/(\d+)\b/gi)) out.add(Number(m[1]));
    for (const m of s.matchAll(/(?:^|[/_-])issue-(\d+)(?=-|$|\/)/gi)) out.add(Number(m[1]));
  }
  return [...out];
}

/**
 * One line of third-party text made safe to put in a prompt or a step output:
 * no line breaks, no text impersonating the prompt's untrusted-data fences, and
 * nothing that could read as a heredoc delimiter on its own line.
 */
export function oneLine(text, max = 160) {
  let s = String(text ?? "")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/<<<[A-Z_-]*UNTRUSTED-DATA[A-Z_-]*>>>/g, "[redacted marker]")
    .replace(/\s+/g, " ")
    .trim();
  if (/^[A-Z_]*EOF$/.test(s)) s = `(${s})`;
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function isoDay(iso) {
  return typeof iso === "string" ? iso.slice(0, 10) : "?";
}

function cap(text, n) {
  const s = String(text ?? "");
  return s.length > n ? s.slice(0, n) : s;
}

/* ------------------------------------------------------------------ */
/* Collection                                                          */
/* ------------------------------------------------------------------ */

function defaultGh(args) {
  return execFileSync("gh", args, { encoding: "utf8", maxBuffer: 64e6, stdio: ["ignore", "pipe", "pipe"] });
}

/**
 * Gather everything in flight for `repo` ("owner/name"). `gh` is injectable so the
 * dashboard's tests can run this against canned responses. Never throws: a failed
 * query lands in `unavailable` and its section is simply empty.
 */
export function collect({ repo, lookbackDays = DEFAULT_LOOKBACK_DAYS, now = new Date(), gh = defaultGh }) {
  const [owner, name] = String(repo).split("/");
  const since = new Date(now.getTime() - lookbackDays * 86_400_000);
  const sinceIso = since.toISOString();
  const unavailable = [];
  const json = (label, args, fallback) => {
    try {
      const out = gh(args);
      return out && out.trim() ? JSON.parse(out) : fallback;
    } catch (err) {
      unavailable.push(`${label}: ${oneLine(err?.stderr || err?.message || err, 200)}`);
      return fallback;
    }
  };
  const base = `https://github.com/${owner}/${name}`;

  const defaultBranch =
    json("default branch", ["api", `repos/${owner}/${name}`, "--jq", "{b: .default_branch}"], {})?.b || "main";

  const openPrs = json("open pull requests", [
    "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
    "--json", "number,title,url,isDraft,headRefName,author,updatedAt,body",
  ], []).map((p) => ({
    kind: "pr",
    number: p.number,
    title: p.title ?? "",
    body: cap(p.body, MAX_BODY_CHARS),
    url: p.url,
    draft: !!p.isDraft,
    branch: p.headRefName ?? "",
    author: p.author?.login ?? "",
    loop: isLoopAuthor(p.author?.login),
    date: p.updatedAt ?? null,
    refs: referencedIssues(p.title, p.body, p.headRefName),
  }));

  const mergedPrs = json("merged pull requests", [
    "pr", "list", "--repo", repo, "--state", "merged", "--limit", "100",
    "--search", `merged:>=${isoDay(sinceIso)}`,
    "--json", "number,title,url,headRefName,author,mergedAt,body",
  ], [])
    .filter((p) => !p.mergedAt || Date.parse(p.mergedAt) >= since.getTime())
    .map((p) => ({
      kind: "merged-pr",
      number: p.number,
      title: p.title ?? "",
      body: cap(p.body, MAX_BODY_CHARS),
      url: p.url,
      branch: p.headRefName ?? "",
      author: p.author?.login ?? "",
      loop: isLoopAuthor(p.author?.login),
      date: p.mergedAt ?? null,
      refs: referencedIssues(p.title, p.body, p.headRefName),
    }));

  const commits = json("recent commits", [
    "api", `repos/${owner}/${name}/commits?sha=${encodeURIComponent(defaultBranch)}&since=${sinceIso}&per_page=100`,
  ], [])
    .filter((c) => (c.parents ?? []).length < 2)
    .map((c) => {
      const message = c.commit?.message ?? "";
      const who = `${c.commit?.author?.name ?? ""} ${c.commit?.author?.email ?? ""} ${c.author?.login ?? ""}`;
      return {
        kind: "commit",
        sha: String(c.sha ?? "").slice(0, 7),
        title: message.split("\n")[0] ?? "",
        url: c.html_url ?? `${base}/commit/${c.sha}`,
        author: c.commit?.author?.name ?? c.author?.login ?? "",
        loop: isLoopAuthor(who),
        date: c.commit?.author?.date ?? null,
        refs: referencedIssues(message),
      };
    });

  // Branches, newest push first. GraphQL gives every branch head's date in one call;
  // the REST branch list does not carry dates at all.
  const refsQuery = `query($o:String!,$r:String!){repository(owner:$o,name:$r){refs(refPrefix:"refs/heads/",first:100,orderBy:{field:TAG_COMMIT_DATE,direction:DESC}){nodes{name target{... on Commit{committedDate messageHeadline author{name email user{login}}}}}}}}`;
  const refNodes = json("branches", [
    "api", "graphql", "-f", `query=${refsQuery}`, "-f", `o=${owner}`, "-f", `r=${name}`,
    "--jq", ".data.repository.refs.nodes",
  ], []) ?? [];
  // A branch whose PR was merged or closed is finished, not in flight — even when a
  // squash merge leaves it "ahead" of the default branch forever.
  const finishedBranches = new Set(
    json("closed pull requests", [
      "pr", "list", "--repo", repo, "--state", "closed", "--limit", "200", "--json", "headRefName",
    ], []).map((p) => p.headRefName),
  );
  const prBranches = new Set([...openPrs.map((p) => p.branch), ...finishedBranches]);
  const recentBranches = refNodes
    .filter((n) => n?.name && n.name !== defaultBranch && !prBranches.has(n.name))
    .filter((n) => Date.parse(n.target?.committedDate ?? "") >= since.getTime());
  const branches = [];
  for (const [i, n] of recentBranches.entries()) {
    const who = `${n.target?.author?.name ?? ""} ${n.target?.author?.email ?? ""} ${n.target?.author?.user?.login ?? ""}`;
    const branch = {
      kind: "branch",
      title: n.name,
      branch: n.name,
      url: `${base}/compare/${encodeURIComponent(defaultBranch)}...${n.name.split("/").map(encodeURIComponent).join("/")}`,
      author: n.target?.author?.user?.login ?? n.target?.author?.name ?? "",
      loop: isLoopAuthor(who),
      date: n.target?.committedDate ?? null,
      headline: n.target?.messageHeadline ?? "",
      ahead: null,
      files: [],
      refs: referencedIssues(n.name, n.target?.messageHeadline),
    };
    if (i < MAX_BRANCHES_DETAILED) {
      const cmp = json(`compare ${n.name}`, [
        "api", `repos/${owner}/${name}/compare/${encodeURIComponent(defaultBranch)}...${encodeURIComponent(n.name)}`,
        "--jq", "{ahead: .ahead_by, files: [.files[]?.filename][:10], messages: [.commits[]?.commit.message][-5:]}",
      ], null);
      if (cmp) {
        branch.ahead = typeof cmp.ahead === "number" ? cmp.ahead : null;
        branch.files = Array.isArray(cmp.files) ? cmp.files : [];
        branch.refs = [...new Set([...branch.refs, ...referencedIssues(...(cmp.messages ?? []))])];
      }
    }
    // Zero commits ahead means it is already merged (or never diverged): not in flight.
    if (branch.ahead === 0) continue;
    branches.push(branch);
  }

  // Ideas. `gh issue list --label a --label b` is AND, so one query per label.
  const ideaMap = new Map();
  for (const state of ["open", "closed"]) {
    for (const label of ["proposal", "approved", "redraft", "declined"]) {
      const rows = json(`${state} ${label} ideas`, [
        "issue", "list", "--repo", repo, "--state", state, "--label", label,
        "--limit", state === "open" ? "200" : "100",
        "--json", "number,title,url,body,labels,state,stateReason,createdAt,closedAt",
      ], []);
      for (const r of rows) {
        if (ideaMap.has(r.number)) continue;
        ideaMap.set(r.number, {
          kind: "idea",
          number: r.number,
          title: r.title ?? "",
          body: cap(r.body, MAX_BODY_CHARS),
          url: r.url,
          state: String(r.state ?? state).toLowerCase() === "closed" ? "closed" : "open",
          stateReason: r.stateReason ?? null,
          labels: (r.labels ?? []).map((l) => (typeof l === "string" ? l : l?.name)).filter(Boolean),
          date: r.createdAt ?? null,
          closedAt: r.closedAt ?? null,
        });
      }
    }
  }
  const ideas = [...ideaMap.values()].sort((a, b) => b.number - a.number);

  return {
    repo,
    generatedAt: now.toISOString(),
    lookbackDays,
    since: sinceIso,
    defaultBranch,
    openPrs,
    branches,
    mergedPrs,
    commits,
    ideas,
    unavailable,
  };
}

/* ------------------------------------------------------------------ */
/* Digest — what the agents read                                       */
/* ------------------------------------------------------------------ */

function ideaStatus(i) {
  const has = (l) => i.labels.includes(l);
  if (has("declined")) return "DECLINED";
  if (i.state === "closed") return i.stateReason === "NOT_PLANNED" || i.stateReason === "not_planned" ? "closed, not planned" : "closed";
  const base = has("approved") && !has("proposal") ? "approved" : has("redraft") ? "being redrafted" : "open proposal";
  return has(COVERED_LABEL) ? `${base}, flagged covered` : base;
}

function section(title, items, render, note = "") {
  const lines = [`${title} (${items.length}${note ? `; ${note}` : ""})`];
  if (items.length === 0) lines.push("- (none)");
  for (const it of items.slice(0, DIGEST_SECTION_CAP)) lines.push(`- ${render(it)}`);
  if (items.length > DIGEST_SECTION_CAP) lines.push(`- …and ${items.length - DIGEST_SECTION_CAP} more (ask gh for the rest)`);
  return lines.join("\n");
}

/** The prompt-safe summary of a `collect` result. Every line is single-line and sanitised. */
export function renderDigest(data) {
  const who = (x) => `${oneLine(x.author, 40) || "unknown"} (${x.loop ? "loop" : "HUMAN"})`;
  const out = [];
  out.push(
    `Window: the last ${data.lookbackDays} day(s), since ${isoDay(data.since)}. Default branch: ${oneLine(data.defaultBranch, 60)}.`,
  );
  if (data.unavailable?.length) {
    out.push(`Could not read (treat these sections as incomplete, not empty): ${data.unavailable.map((u) => oneLine(u, 120)).join("; ")}`);
  }
  out.push("");
  out.push(section("OPEN PULL REQUESTS — drafts included; a draft is someone's work in progress", data.openPrs, (p) =>
    `${p.draft ? "[DRAFT] " : ""}#${p.number} ${oneLine(p.title)} — by ${who(p)}, branch ${oneLine(p.branch, 80)}, updated ${isoDay(p.date)} — ${p.url}`));
  out.push("");
  out.push(section("RECENTLY PUSHED BRANCHES WITH NO PR YET — someone is working here right now", data.branches, (b) => {
    const ahead = typeof b.ahead === "number" ? `${b.ahead} commit(s) ahead` : "ahead count unknown";
    const files = b.files?.length ? ` — touches ${b.files.map((f) => oneLine(f, 80)).join(", ")}` : "";
    return `${oneLine(b.branch, 100)} — last push ${isoDay(b.date)} by ${who(b)}, ${ahead}: "${oneLine(b.headline, 100)}"${files} — ${b.url}`;
  }));
  out.push("");
  out.push(section(`PULL REQUESTS MERGED IN THE WINDOW — this is shipped`, data.mergedPrs, (p) =>
    `#${p.number} ${oneLine(p.title)} — by ${who(p)}, merged ${isoDay(p.date)} — ${p.url}`));
  out.push("");
  // The loop's own commits (metrics refreshes, its merged builds) are this system
  // talking to itself and already appear above as PRs; they only crowd out the owner's.
  const humanCommits = data.commits.filter((c) => !c.loop);
  const loopCommits = data.commits.length - humanCommits.length;
  out.push(section(`COMMITS BY PEOPLE ON ${oneLine(data.defaultBranch, 60)} IN THE WINDOW — newest first; this is shipped`, humanCommits, (c) =>
    `${isoDay(c.date)} ${c.sha} ${oneLine(c.author, 40)}: ${oneLine(c.title)} — ${c.url}`, loopCommits ? `${loopCommits} loop commit(s) left out` : ""));
  out.push("");
  const open = data.ideas.filter((i) => i.state === "open" && !i.labels.includes("declined"));
  const closed = data.ideas.filter((i) => i.state === "closed" || i.labels.includes("declined"));
  out.push(section("IDEAS ALREADY FILED AND STILL OPEN", open, (i) =>
    `#${i.number} [${ideaStatus(i)}] ${oneLine(i.title)} — ${i.url}`));
  out.push("");
  out.push(section("IDEAS ALREADY CLOSED OR DECLINED — a DECLINED one is a permanent no", closed, (i) =>
    `#${i.number} [${ideaStatus(i)}] ${oneLine(i.title)} — ${i.url}`));
  return out.join("\n");
}

/* ------------------------------------------------------------------ */
/* Coverage — the deterministic backstop                               */
/* ------------------------------------------------------------------ */

/**
 * Work a PERSON did that names this idea on purpose — a PR, a pushed branch or a
 * commit saying "closes #12", "refs #12", `issue-12`… That is the strongest cover
 * there is and needs no similarity score: the owner built it (or is building it) by
 * hand. The loop's own PRs are left out: they are the Builder's claim on the idea,
 * which the Builder's gate already handles.
 */
export function explicitCovers(target, data) {
  const work = [...data.openPrs, ...data.branches, ...data.mergedPrs, ...data.commits];
  const seen = new Set();
  return work
    .filter((w) => !w.loop && (w.refs ?? []).includes(target.number))
    .filter((w) => (seen.has(w.url) ? false : (seen.add(w.url), true)))
    .map((w) => ({ item: w, score: null }));
}

/**
 * What an issue may be "covered by" on similarity, per mode:
 *   new   — a freshly filed or rewritten idea: any OLDER idea (open or closed, so a
 *           declined one counts), any open PR, any PR merged in the window;
 *   build — an approved idea about to be built: open and merged PRs only. The owner
 *           approved it knowing about the other ideas; only real work overrides that.
 * Anything that names the issue itself is left to `explicitCovers`.
 */
export function coverageCandidates(target, data, mode) {
  const prs = [...data.openPrs, ...data.mergedPrs].filter((p) => !(p.refs ?? []).includes(target.number));
  if (mode === "build") return prs;
  const olderIdeas = data.ideas.filter((i) => i.number < target.number);
  return [...olderIdeas, ...prs];
}

/** Whether the calibrated threshold means anything for this text. */
export function inCalibratedDomain(doc) {
  return docText(doc).length >= MIN_CALIBRATED_CHARS;
}

function dot(a, b) {
  let s = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i += 1) s += a[i] * b[i];
  return s;
}

/**
 * Matches at or above the threshold, best first. `vectorOf(doc)` returns the
 * L2-normalised embedding (or undefined when it could not be computed), so cosine is
 * a dot product — the same arithmetic the dashboard's queue scan does.
 */
export function findCovers(target, candidates, vectorOf, threshold = EMBED_THRESHOLD) {
  const tv = vectorOf(target);
  if (!tv) return [];
  const out = [];
  for (const c of candidates) {
    const cv = vectorOf(c);
    if (!cv) continue;
    const score = Math.round(dot(tv, cv) * 10000) / 10000;
    if (score >= threshold) out.push({ item: c, score });
  }
  out.sort((x, y) => y.score - x.score);
  return out.slice(0, MAX_COVER_LINKS);
}

/**
 * Everything that covers `target`: work that names it first (the strongest evidence),
 * then similarity matches, without repeats, at most three links.
 */
export function coversFor(target, data, mode, vectorOf) {
  const out = [];
  const seen = new Set();
  for (const m of [...explicitCovers(target, data), ...findCovers(target, coverageCandidates(target, data, mode), vectorOf)]) {
    if (seen.has(m.item.url)) continue;
    seen.add(m.item.url);
    out.push(m);
  }
  return out.slice(0, MAX_COVER_LINKS);
}

/** What one covering item is, in the owner's words. */
export function describeCover(item) {
  switch (item.kind) {
    case "pr":
      return `${item.draft ? "open draft PR" : "open PR"} #${item.number}: ${oneLine(item.title, 120)}`;
    case "merged-pr":
      return `PR #${item.number}, merged ${isoDay(item.date)}: ${oneLine(item.title, 120)}`;
    case "branch":
      return `branch ${oneLine(item.branch, 100)}, pushed ${isoDay(item.date)}`;
    case "commit":
      return `commit ${item.sha}: ${oneLine(item.title, 120)}`;
    case "idea":
      return `${item.labels?.includes("declined") ? "declined idea" : item.state === "closed" ? "closed idea" : "idea"} #${item.number}: ${oneLine(item.title, 120)}`;
    default:
      return oneLine(item.title, 120);
  }
}

/**
 * The comment that marks an idea covered. The first line is the marker the dashboard
 * looks for; every link sits in a `- [what](url)` bullet so it can be read back.
 */
export function coverComment(matches) {
  const lines = [
    COVERED_MARKER,
    "**This idea looks already covered by work that exists.**",
    "",
    ...matches.map(
      (m) =>
        `- [${describeCover(m.item).replace(/[[\]]/g, "")}](${m.item.url}) · ` +
        (m.score === null ? "points at this idea directly" : `${Math.round(m.score * 100)}% similar`),
    ),
    "",
    "Found automatically: work that names this idea directly, or whose text the dashboard's own duplicate detector " +
      `(${EMBED_MODEL}, threshold ${EMBED_THRESHOLD}) scores as the same request. ` +
      "It is a flag, not a verdict: nothing was closed or moved. Decline the idea if it really is covered, or clear the flag in the dashboard to keep it — once cleared, it is not flagged again automatically.",
  ];
  return lines.join("\n");
}

/* ------------------------------------------------------------------ */
/* Embedding (check only)                                              */
/* ------------------------------------------------------------------ */

/** The cache key for LOOP_EMBED_DIR: a new pin, OS or architecture is a new cache. */
export function embedCacheKey(os = process.env.RUNNER_OS || process.platform, arch = process.arch) {
  return `loop-embed-${os}-${arch}-transformers-${EMBED_LIB_VERSION}-${EMBED_MODEL.replace(/\//g, "_")}`;
}

/**
 * The encoder, installed on demand into LOOP_EMBED_DIR (under RUNNER_TEMP), with the
 * model weights downloaded into LOOP_EMBED_DIR/models. The Scout, Builder and Redraft
 * workflows restore that whole folder under embedCacheKey(), and save it only after a
 * miss on which `check` reported `embed=loaded`, so a broken install is never cached.
 */
async function loadEncoder() {
  const dir = process.env.LOOP_EMBED_DIR;
  if (!dir) throw new Error("LOOP_EMBED_DIR is not set, so there is nowhere to install the duplicate detector");
  const req = createRequire(join(dir, "package.json"));
  let resolved;
  try {
    resolved = req.resolve(EMBED_LIB);
  } catch {
    mkdirSync(dir, { recursive: true });
    if (!existsSync(join(dir, "package.json"))) writeFileSync(join(dir, "package.json"), '{"private":true}\n');
    console.log(`Installing ${EMBED_LIB}@${EMBED_LIB_VERSION} into ${dir}…`);
    execFileSync("npm", ["install", "--prefix", dir, "--no-audit", "--no-fund", "--loglevel=error", `${EMBED_LIB}@${EMBED_LIB_VERSION}`], {
      stdio: ["ignore", "inherit", "inherit"],
    });
    resolved = req.resolve(EMBED_LIB);
  }
  const mod = await import(pathToFileURL(resolved).href);
  const pipeline = mod.pipeline ?? mod.default?.pipeline;
  if (typeof pipeline !== "function") throw new Error("@huggingface/transformers has no pipeline export");
  // Weights land inside LOOP_EMBED_DIR, the folder the workflows cache, not in node_modules.
  const env = mod.env ?? mod.default?.env;
  if (env) env.cacheDir = join(dir, "models");
  // fp32, mean pooling, normalised: exactly what every number in dedup-eval.json used.
  const extractor = await pipeline("feature-extraction", EMBED_MODEL, { dtype: "fp32" });
  return async (texts) => {
    const out = [];
    for (let i = 0; i < texts.length; i += 16) {
      const batch = texts.slice(i, i + 16).map((t) => t.slice(0, MAX_EMBED_CHARS));
      const tensor = await extractor(batch, { pooling: "mean", normalize: true });
      const width = tensor.dims[tensor.dims.length - 1];
      const flat = tensor.data instanceof Float32Array ? tensor.data : Float32Array.from(tensor.data);
      for (let r = 0; r < batch.length; r += 1) out.push(flat.slice(r * width, (r + 1) * width));
    }
    return out;
  };
}

/* ------------------------------------------------------------------ */
/* CLI                                                                 */
/* ------------------------------------------------------------------ */

function arg(argv, name, fallback = undefined) {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : fallback;
}

function setOutput(key, value) {
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${value}\n`);
}

function readConfigText() {
  try {
    return readFileSync(".github/loop-config.json", "utf8");
  } catch {
    return "";
  }
}

async function cmdCheck(argv) {
  const data = JSON.parse(readFileSync(arg(argv, "in"), "utf8"));
  const mode = arg(argv, "mode", "new") === "build" ? "build" : "new";
  const dryRun = argv.includes("--dry-run");
  const repo = data.repo;
  const numbers = String(arg(argv, "issues", ""))
    .split(/[\s,]+/)
    .filter((s) => /^\d+$/.test(s))
    .map(Number);
  setOutput("covered", "");
  if (numbers.length === 0) {
    console.log("No issues to check.");
    return;
  }

  // Each target fresh from GitHub: the collected snapshot may predate this run's filing.
  const targets = [];
  for (const n of numbers) {
    try {
      const issue = JSON.parse(defaultGh(["issue", "view", String(n), "--repo", repo, "--json", "number,title,body,url,labels,comments"]));
      const labels = (issue.labels ?? []).map((l) => l.name);
      if (labels.includes(COVERED_LABEL)) {
        console.log(`#${n} — already flagged covered. Skipping.`);
        continue;
      }
      if ((issue.comments ?? []).some((c) => String(c.body ?? "").includes(COVERED_MARKER))) {
        console.log(`#${n} — was flagged covered before and the owner cleared the flag. Not flagging it again.`);
        continue;
      }
      targets.push({ kind: "idea", number: issue.number, title: issue.title ?? "", body: issue.body ?? "", url: issue.url });
    } catch (err) {
      console.log(`::warning::Couldn't read #${n}: ${oneLine(err?.message ?? err, 200)}`);
    }
  }
  if (targets.length === 0) return;

  // Similarity, only where the calibrated threshold means something. If the encoder
  // cannot load, the explicit covers below still stand on their own.
  const byUrl = new Map();
  const scorable = targets.filter((t) => {
    if (inCalibratedDomain(t)) return true;
    console.log(`#${t.number} — too short (${docText(t).length} chars; the detector is calibrated from ${MIN_CALIBRATED_CHARS}) to score by similarity. Only direct references are checked.`);
    return false;
  });
  if (scorable.length > 0) {
    const pool = new Map();
    for (const t of scorable) {
      pool.set(t.url, t);
      for (const c of coverageCandidates(t, data, mode)) if (inCalibratedDomain(c)) pool.set(c.url, c);
    }
    const docs = [...pool.values()];
    try {
      console.log(`Embedding ${docs.length} document(s) with ${EMBED_MODEL} (threshold ${EMBED_THRESHOLD})…`);
      const encode = await loadEncoder();
      setOutput("embed", "loaded");
      const vectors = await encode(docs.map(docText));
      docs.forEach((d, i) => byUrl.set(d.url, vectors[i]));
    } catch (err) {
      console.log(`::warning::The duplicate detector could not run (${oneLine(err?.message ?? err, 200)}). Checking direct references only.`);
    }
  }

  const covered = [];
  let labelReady = dryRun;
  for (const t of targets) {
    const matches = coversFor(t, data, mode, (d) => byUrl.get(d.url));
    if (matches.length === 0) {
      console.log(`#${t.number} — no existing work names it or scores at or above ${EMBED_THRESHOLD}. Not covered.`);
      continue;
    }
    console.log(`#${t.number} — COVERED by: ${matches.map((m) => `${m.item.url} (${m.score ?? "names it"})`).join(", ")}`);
    covered.push(t.number);
    if (dryRun) {
      console.log(coverComment(matches));
      continue;
    }
    try {
      if (!labelReady) {
        defaultGh(["label", "create", COVERED_LABEL, "--repo", repo, "--color", COVERED_LABEL_COLOR, "--description", COVERED_LABEL_DESCRIPTION, "--force"]);
        labelReady = true;
      }
      defaultGh(["issue", "comment", String(t.number), "--repo", repo, "--body", coverComment(matches)]);
      defaultGh(["issue", "edit", String(t.number), "--repo", repo, "--add-label", COVERED_LABEL]);
    } catch (err) {
      console.log(`::warning::Couldn't mark #${t.number} covered: ${oneLine(err?.stderr || err?.message || err, 200)}`);
    }
  }
  setOutput("covered", covered.join(","));
}

async function main(argv) {
  const cmd = argv[0];
  if (cmd === "collect") {
    const repo = arg(argv, "repo", process.env.GITHUB_REPOSITORY);
    if (!repo) throw new Error("no repository: pass --repo owner/name or set GITHUB_REPOSITORY");
    const lookbackDays = Number(arg(argv, "lookback-days")) || readLookbackDays(readConfigText());
    const data = collect({ repo, lookbackDays });
    writeFileSync(arg(argv, "out", "inflight.json"), JSON.stringify(data, null, 2));
    console.log(
      `Collected for ${repo}, last ${lookbackDays} day(s): ${data.openPrs.length} open PR(s) (${data.openPrs.filter((p) => p.draft).length} draft), ` +
        `${data.branches.length} recently pushed branch(es) without a PR, ${data.mergedPrs.length} merged PR(s), ` +
        `${data.commits.length} commit(s), ${data.ideas.length} idea(s).`,
    );
    for (const u of data.unavailable) console.log(`::warning::In-flight data incomplete — ${u}`);
    return;
  }
  if (cmd === "digest") {
    const data = JSON.parse(readFileSync(arg(argv, "in", "inflight.json"), "utf8"));
    process.stdout.write(`${renderDigest(data)}\n`);
    return;
  }
  if (cmd === "check") {
    await cmdCheck(argv);
    return;
  }
  if (cmd === "embed-key") {
    const key = embedCacheKey();
    setOutput("key", key);
    console.log(key);
    return;
  }
  throw new Error(`unknown command '${cmd ?? ""}' — use collect, digest, check or embed-key`);
}

const invokedDirectly = (() => {
  try {
    return import.meta.url === pathToFileURL(process.argv[1] ?? "").href;
  } catch {
    return false;
  }
})();

if (invokedDirectly) {
  main(process.argv.slice(2)).catch((err) => {
    // Never a red run: the loop works without this, just less well.
    console.log(`::warning::loop-inflight: ${oneLine(err?.message ?? err, 300)}`);
    process.exit(0);
  });
}
