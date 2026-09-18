// SCREENSHOTS — the repeatable visual pass this project did not have.
//
// WHY THIS EXISTS
// ---------------
// `docs/screenshots/current/` once held a thorough walkthrough of the sourcing-era
// app (login, dashboard, cart, resilience, model card...), every shot dated
// **2026-08-17**. Since issue #16 the app is three pages - Route Plan, Simulation,
// Benchmarks - plus a landing page, and SHOTS below covers exactly those.
//
// A directory named `current/` that is three weeks stale is worse than no
// directory: its own `_problems.json` still records "app has no dedicated 404
// page" while `App.tsx` routes `*` to a real `NotFoundPage`.
//
// It went stale because there was no COMMAND. The readability gate has one
// (`npm run ui-gate`) and therefore runs on every push. The visual pass was
// produced ad hoc by the `ui-verifier` agent, which drives the **Playwright MCP
// server** — and that server was later removed from the owner's setup. The agent
// correctly refuses to fake results without it, so the visual pass simply stopped,
// silently, and nobody noticed for weeks. The tooling that checks the work had a
// single point of failure and no alarm on itself.
//
// So this script deliberately depends on NOTHING but the `playwright`
// devDependency that is already here and the Chromium already cached in
// `~/Library/Caches/ms-playwright`. No MCP server, no API key, no network service.
// If it can run, it runs.
//
//   cd frontend
//   npm run screenshots                                  # against the live site
//   BASE=http://localhost:4173 npm run screenshots       # against a local build
//   ONLY=benchmark,map npm run screenshots               # just those shots
//
// Writes to `docs/screenshots/current/`, and writes `_manifest.json` recording the
// commit, the base URL, the timestamp and every shot taken — so a future reader can
// tell what these images actually depict instead of guessing from a folder name.
//
// EXIT CODE IS MEANINGFUL. A shot whose page never finished loading, or that
// rendered the 404, or that came back essentially blank, FAILS the run. A
// screenshot tool that cheerfully saves a picture of a spinner is how a stale
// image ends up in a README under a caption claiming it is current.

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.BASE || 'https://supply-chain-ui-bhwz.onrender.com').replace(/\/$/, '');
const OUT = path.resolve(__dirname, '..', '..', 'docs', 'screenshots', 'current');
const ONLY = (process.env.ONLY || '').split(',').map(s => s.trim()).filter(Boolean);

// Desktop shots use 1440x900 — the same width the ui-gate treats as its primary
// desktop viewport, so the two passes describe the same rendering.
const DESKTOP = { width: 1440, height: 900 };
const MOBILE = { width: 390, height: 844 };

/** @type {{name:string, route:string, viewport?:object, full?:boolean, settle?:number, prepare?:Function}[]} */
const SHOTS = [
  { name: '01-landing', route: '/' },
  { name: '02-route-plan', route: '/route-plan', full: true,
    prepare: async page => {
      await page.getByRole('button', { name: /^Solve$/ }).click({ timeout: 60000 });
      await page.getByText('Route 1', { exact: true }).first().waitFor({ state: 'visible', timeout: 60000 });
    } },
  // Uses the plan the previous shot solved (kept in sessionStorage), or solves the
  // first built-in sample when run on its own (ONLY=simulation).
  { name: '03-simulation', route: '/simulation', full: true, settle: 60000,
    prepare: async page => {
      const sample = page.getByRole('button', { name: /Solve and use this sample/ });
      if (await sample.count()) await sample.click({ timeout: 60000 });
      await page.getByRole('button', { name: /^Run simulation$/ }).click({ timeout: 60000 });
      await page.getByText('On-time rate', { exact: true }).first().waitFor({ state: 'visible', timeout: 60000 });
      await page.getByRole('button', { name: /^Tune buffers$/ }).click({ timeout: 60000 });
      await page.getByText('Chosen buffers', { exact: true }).first().waitFor({ state: 'visible', timeout: 120000 });
    } },
  { name: '04-benchmarks', route: '/benchmarks', full: true },
  { name: '05-mobile-route-plan', route: '/route-plan', viewport: MOBILE },
];

const problems = [];
const taken = [];

function note(msg) { console.log(msg); }

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: DESKTOP });
  const page = await ctx.newPage();

  const consoleErrors = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
  page.on('pageerror', e => consoleErrors.push(String(e).slice(0, 200)));

  note(`base: ${BASE}`);

  for (const shot of SHOTS) {
    if (ONLY.length && !ONLY.some(o => shot.name.includes(o) || shot.route.includes(o))) continue;

    const vp = shot.viewport || DESKTOP;
    await page.setViewportSize(vp);
    const before = consoleErrors.length;
    const problemsBefore = problems.length;

    process.stdout.write(`  ${shot.name.padEnd(24)} ${shot.route} … `);
    await page.goto(BASE + shot.route, { waitUntil: 'domcontentloaded', timeout: 120000 });
    await page.waitForTimeout(3000);
    if (shot.prepare) {
      try {
        await shot.prepare(page);
      } catch (e) {
        problems.push(`${shot.name}: prepare step failed: ${String(e).split('\n')[0]}`);
      }
    }

    // Wait for the page to stop saying it is working. Every long-running view in
    // this app renders an explicit progress string rather than an empty frame.
    const cap = shot.settle || 15000;
    try {
      await page.waitForFunction(
        () => !/Solving|Loading|Computing|Fetching|Please wait/i.test(document.body.innerText),
        null, { timeout: cap }
      );
    } catch { /* fall through — the blank/404 checks below decide if it is fatal */ }
    await page.waitForTimeout(2500);

    // These three checks were WRONG on their first run and reported 3 false
    // alarms out of 3 — /map "blank" (it is a canvas, so it has almost no text),
    // /benchmark and /frontier "still busy" (both had fully rendered; the words
    // merely appeared somewhere on a long page). A check that cries wolf every
    // run gets ignored, which is worse than not having it. Narrowed:
    //   * emptiness is judged on RENDERED PIXELS, not character count, so a map
    //     or a chart counts as content;
    //   * busy-ness only counts a progress string that is actually VISIBLE and
    //     in the top of the viewport, not any occurrence in prose further down
    //     (the frontier page legitimately contains the word "solved", and the
    //     benchmark page explains "loading" in body copy).
    const state = await page.evaluate(() => {
      const t = (document.body.innerText || '').trim();
      const vis = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
      };
      // Content = text OR a canvas/svg/img of real size. A map has one big canvas.
      const painted = [...document.querySelectorAll('canvas,svg,img')]
        .filter(vis)
        .reduce((a, el) => {
          const r = el.getBoundingClientRect();
          return a + r.width * r.height;
        }, 0);
      // Only a progress indicator still on screen counts as "busy".
      const busyEl = [...document.querySelectorAll('*')].find(el => {
        if (el.children.length) return false;            // leaf nodes only
        const s = (el.textContent || '').trim();
        if (!/^(solving|loading|computing|fetching)\b/i.test(s)) return false;
        if (!vis(el)) return false;
        return el.getBoundingClientRect().top < window.innerHeight;
      });
      return {
        chars: t.length,
        paintedArea: Math.round(painted),
        is404: /404|page not found|doesn't exist/i.test(t.slice(0, 400)),
        stillBusy: !!busyEl,
        busyText: busyEl ? (busyEl.textContent || '').trim().slice(0, 60) : null,
      };
    });

    const file = path.join(OUT, `${shot.name}.png`);
    // Every page scrolls inside its own `overflow-y-auto` container under a fixed
    // nav, so `fullPage` alone captures one viewport of it. For a full shot, grow
    // the viewport to the container's content height and scroll it back to the top.
    if (shot.full) {
      const extra = await page.evaluate(() => {
        const s = [...document.querySelectorAll('body *')]
          .filter(e => e.scrollHeight > e.clientHeight + 2 && /auto|scroll/.test(getComputedStyle(e).overflowY))
          .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
        if (!s) return 0;
        s.scrollTop = 0;
        return s.scrollHeight - s.clientHeight;
      });
      if (extra > 0) {
        await page.setViewportSize({ width: vp.width, height: vp.height + extra });
        await page.waitForTimeout(800);
      }
    }
    await page.screenshot({ path: file, fullPage: !!shot.full });
    taken.push(shot.name);

    const newErrors = consoleErrors.slice(before);
    if (state.is404) problems.push(`${shot.name}: rendered the 404 page at ${shot.route}`);
    // Blank = little text AND nothing painted. 200k px^2 is about a 450x450 canvas.
    if (state.chars < 400 && state.paintedArea < 200000) {
      problems.push(`${shot.name}: ${state.chars} chars and only ${state.paintedArea}px^2 painted — likely blank`);
    }
    if (state.stillBusy) {
      problems.push(`${shot.name}: a progress indicator was STILL VISIBLE after ${cap}ms ("${state.busyText}") — image may show a spinner`);
    }
    if (newErrors.length) problems.push(`${shot.name}: ${newErrors.length} console error(s): ${newErrors[0]}`);

    // Derive the per-shot label from the problems list itself. It used to repeat
    // the conditions inline, which meant that narrowing the checks above fixed the
    // summary and left this line still shouting PROBLEM at a healthy page — two
    // statements of the same rule, drifting apart. One source of truth.
    console.log(problems.length > problemsBefore ? 'PROBLEM' : 'ok');
  }

  // Record what these images actually are. A folder called `current/` proved it
  // cannot be trusted to say so on its own.
  let commit = 'unknown';
  try {
    commit = require('child_process')
      .execSync('git rev-parse HEAD', { cwd: __dirname }).toString().trim();
  } catch { /* not a git checkout — record 'unknown' rather than guessing */ }

  // A PARTIAL run must not overwrite a full manifest. `ONLY=map` used to replace
  // the whole file with one naming a single shot, so the manifest claimed the
  // folder held one image while fourteen sat beside it — a label misdescribing
  // what is actually on disk, which is the precise failure this manifest exists to
  // prevent. Partial runs now MERGE: each shot carries its own capture time and
  // commit, so a folder of mixed vintages says so instead of hiding it.
  const manifestPath = path.join(OUT, '_manifest.json');
  let prior = {};
  try { prior = JSON.parse(fs.readFileSync(manifestPath, 'utf8')); } catch { /* first run */ }

  const now = new Date().toISOString();
  const shots = Object.assign({}, prior.shots && !Array.isArray(prior.shots) ? prior.shots : {});
  for (const name of taken) shots[name] = { captured_at_utc: now, commit, base_url: BASE };

  // Drop entries for images that are no longer on disk, so the manifest cannot
  // outlive the files it describes.
  for (const name of Object.keys(shots)) {
    if (!fs.existsSync(path.join(OUT, `${name}.png`))) delete shots[name];
  }

  const vintages = [...new Set(Object.values(shots).map(v => v.commit))];
  fs.writeFileSync(manifestPath, JSON.stringify({
    last_run_utc: now,
    last_run_was_partial: ONLY.length > 0,
    last_run_only: ONLY.length ? ONLY : undefined,
    base_url: BASE,
    commit,
    viewport_desktop: DESKTOP,
    viewport_mobile: MOBILE,
    distinct_commits_in_folder: vintages,
    mixed_vintage: vintages.length > 1,
    shots,
    problems_this_run: problems,
  }, null, 2) + '\n');

  console.log(`\nwrote ${taken.length} shot(s) to docs/screenshots/current/`);
  if (problems.length) {
    console.error(`\n${problems.length} PROBLEM(S):`);
    for (const p of problems) console.error('  - ' + p);
    console.error('\nThese images are not trustworthy. Fix the page or the wait, do not commit them.');
  } else {
    console.log('no problems: every shot loaded, rendered real content, and finished working.');
  }

  await browser.close();
  process.exit(problems.length ? 1 : 0);
})();
