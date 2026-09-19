// UI GATE RUNNER — the whole gate as ONE command, so CI can run it in one line.
//
//   cd frontend && npm run ui-gate:ci
//
// WHY THIS EXISTS. `scripts/ui-gate.cjs` is the only check in this repo that looks at
// the app the way a visitor does — a real Chromium over every route at four viewports,
// each assertion a postmortem of something that shipped once (see that file's header).
// `npm run build` is a type-check and a bundle: it cannot see a route that renders the
// 404 page, `NaN` in a stat card, an axe-core critical, or a chart legend printed on top
// of its own axis labels. Yet `grep -rni ui.gate .github/` returns nothing — the gate is
// in no workflow, so it runs only when a human remembers to type five commands in the
// right order (issue #8).
//
// Five commands is the reason. The gate needs a build, a Chromium binary, a preview
// server on a known port, the server to actually be answering before the first
// navigation, and the server killed afterwards even when the gate throws. That is a
// paragraph of YAML to get wrong in a workflow, and a paragraph to get wrong by hand.
// Here it is one command with the ordering, the readiness wait and the teardown written
// down once, so the CI step is `run: npm run ui-gate:ci` and the local invocation is the
// same command CI runs — a pasted local log is then evidence about CI, not an anecdote.
//
// THE WORKFLOW STEP IS NOT IN THIS PR. The bot that wrote this file cannot push under
// `.github/` — GitHub refuses ("without `workflows` permission"), and the owner declined
// to grant it on issue #5. The ready-to-paste step ships in the PR body instead. Note
// that `ci.yml` already runs `npm test`, so redefining THAT script here would start the
// gate running with no workflow edit; this file deliberately does not, because it would
// produce the effect the owner declined through a file the control does not cover, and
// would hide a live-network browser gate inside a step named "Unit tests".
//
// EXIT CODES ARE THE GATE'S OWN, passed through unchanged, so the step is red for
// exactly the reasons the gate is red:
//   0  every check passed
//   1  a check FAILED           (ui-gate.cjs:<end> — `process.exit(fail?1:0)`)
//   2  the gate threw           (ui-gate.cjs:<end> — `.catch(... process.exit(2))`)
//   2  the gate could not be RUN (build failed, no Chromium, preview never answered).
// `2` deliberately covers both "threw" and "could not run": in a project whose rule is
// that "not checked" is a failure and not a pass, an ungate-able commit and a crashing
// gate deserve the same verdict. The log distinguishes them in words.
//
// .cjs on purpose, beside ui-gate.cjs: `package.json` is `"type": "module"`, and Node
// resolves modules from the SCRIPT's directory upward, so both must sit next to the
// node_modules holding playwright and vite.
'use strict';

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const FRONTEND = path.resolve(__dirname, '..');
const GATE_SCRIPT = path.join(__dirname, 'ui-gate.cjs');
const DEFAULT_PORT = 4173;
const DEFAULT_API = 'https://supply-chain-api-qy8x.onrender.com';

// Resolved from node_modules/.bin, NOT `require.resolve('vite/bin/vite.js')`. Both vite 8
// and playwright 1.x declare an `exports` map that does not expose their bin scripts, so
// require.resolve throws ERR_PACKAGE_PATH_NOT_EXPORTED on them. Not `npx` either: npx will
// silently FETCH a package from the registry when the local one is missing, so a broken
// install would be papered over by a different version of the tool than the one pinned in
// package-lock.json.
const bin = (name) => path.join(FRONTEND, 'node_modules', '.bin', name);

/**
 * BASE is DERIVED from the port unless explicitly overridden, so a half-edit can never
 * point the preview server and the gate at different ports.
 *
 * 127.0.0.1, not `localhost`. ui-gate.cjs defaults BASE to `http://localhost:4173`; if the
 * preview binds IPv4 and Chromium resolves `localhost` to `::1`, every navigation fails for
 * a reason that has nothing to do with the UI. Bind and address the same literal.
 *
 * API is left at the live deployment, which is what ui-gate.cjs proxies `**\/api\/v1\/**`
 * to. Nothing here narrows the check set to make it pass: if the API is unreachable the
 * gate's own "API awake before the sweep" check goes red, which is the correct verdict for
 * a UI whose data cannot be fetched.
 */
function resolveConfig(env = process.env) {
  const raw = env.PREVIEW_PORT || String(DEFAULT_PORT);
  const port = Number(raw);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`PREVIEW_PORT must be a valid port number, got ${JSON.stringify(raw)}`);
  }
  const host = '127.0.0.1';
  return { port, host, base: env.BASE || `http://${host}:${port}`, api: env.API || DEFAULT_API };
}

/**
 * `--strictPort` is load-bearing, not tidiness. Without it vite preview quietly increments
 * to the next free port when 4173 is taken, the gate drives whatever already owns 4173, and
 * you get a GREEN run about the wrong build — a false clean, which this repo treats as worse
 * than a failure.
 */
const previewArgs = (cfg) => ['preview', '--host', cfg.host, '--port', String(cfg.port), '--strictPort'];

/** `--with-deps` shells out to `sudo apt-get`. Fine unattended on GitHub's ubuntu runners;
 *  on a developer's machine it would prompt, and on macOS/Windows it means nothing. */
const playwrightInstallArgs = (platform = process.platform, env = process.env) =>
  platform === 'linux' && env.CI ? ['install', '--with-deps', 'chromium'] : ['install', 'chromium'];

/**
 * Poll until the preview actually SERVES the app. A bare TCP connect succeeds against a
 * socket that never returns a byte, so this requires a real HTTP response.
 */
function waitForServer(url, { timeoutMs = 60000, intervalMs = 250, get = http.get } = {}) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = get(url, (res) => {
        res.resume();
        if (res.statusCode && res.statusCode < 500) return resolve(res.statusCode);
        retry(`HTTP ${res.statusCode}`);
      });
      req.on('error', (err) => retry(err.message));
      req.setTimeout(2000, () => req.destroy(new Error('request timed out')));
    };
    const retry = (why) => {
      if (Date.now() >= deadline) {
        return reject(new Error(`${url} did not answer within ${timeoutMs}ms (last: ${why})`));
      }
      setTimeout(attempt, intervalMs);
    };
    attempt();
  });
}

/** `detached` so the whole process GROUP can be killed: vite spawns children, and killing
 *  only the parent leaves the port held and poisons the next run. Idempotent. */
function stopPreview(child) {
  if (!child || child.killed || child.exitCode !== null) return;
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    try { child.kill('SIGTERM'); } catch { /* already gone */ }
  }
  setTimeout(() => {
    try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
  }, 5000).unref();
}

const run = (label, cmd, args, opts = {}) => {
  console.log(`\n── ${label}\n   $ ${cmd} ${args.join(' ')}`);
  const { status, error } = spawnSync(cmd, args, { cwd: FRONTEND, stdio: 'inherit', ...opts });
  if (error) throw error;
  return status === null ? 2 : status;
};

async function main() {
  const cfg = resolveConfig();
  console.log(
    `\n══════════ UI GATE ══════════\n` +
    `  a real Chromium over every route at 4 viewports\n` +
    `  BASE ${cfg.base}   (local build, served by vite preview)\n` +
    `  API  ${cfg.api}   (proxied into the page by ui-gate.cjs)\n` +
    `═════════════════════════════`
  );

  // ALWAYS build. Never "skip if dist/ exists": a stale dist would have the gate certify
  // code that is not the code under test, which is the false-clean this whole file is
  // guarding against. In CI the checkout is fresh anyway; locally `tsc -b` is incremental.
  if (run('Building the app under test', 'npm', ['run', 'build']) !== 0) {
    console.error('::error::the build failed, so there is nothing to gate.');
    return 2;
  }

  // Skipped entirely when the browser is already there, so a developer pays the download
  // once and CI pays it per fresh runner.
  const { chromium } = require('playwright');
  if (fs.existsSync(chromium.executablePath())) {
    console.log(`\n── Chromium already present at ${chromium.executablePath()}`);
  } else if (run('Installing Chromium for Playwright', bin('playwright'), playwrightInstallArgs()) !== 0) {
    console.error('::error::could not install the Chromium the gate drives, so the gate cannot run.');
    return 2;
  }

  let preview;
  const shutdown = () => stopPreview(preview);
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  process.on('exit', shutdown);

  try {
    console.log(`\n── Serving the build on ${cfg.base}\n   $ vite ${previewArgs(cfg).join(' ')}`);
    preview = spawn(bin('vite'), previewArgs(cfg), { cwd: FRONTEND, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    // Prefixed rather than swallowed: an EADDRINUSE here is the difference between "the gate
    // failed" and "the gate never ran", and the log must be able to say which.
    const echo = (stream) => stream.on('data', (d) => process.stdout.write(`   preview| ${d}`));
    echo(preview.stdout);
    echo(preview.stderr);
    preview.on('error', (err) => console.error(`   preview| failed to start: ${err.message}`));

    try {
      await waitForServer(cfg.base);
      console.log('   preview is answering.');
    } catch (err) {
      console.error(`::error::${err.message}`);
      return 2;
    }

    return run('Running the gate', process.execPath, [GATE_SCRIPT], {
      env: { ...process.env, BASE: cfg.base, API: cfg.api },
    });
  } finally {
    shutdown();
  }
}

module.exports = {
  resolveConfig, previewArgs, playwrightInstallArgs, waitForServer, stopPreview,
  GATE_SCRIPT, DEFAULT_PORT, DEFAULT_API,
};

if (require.main === module) {
  main().then(
    (code) => process.exit(code),
    (err) => { console.error('FATAL', err); process.exit(2); }
  );
}
