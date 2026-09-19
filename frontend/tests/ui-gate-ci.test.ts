/**
 * The one-command gate runner, and the docs that describe the gate.
 *
 * Two jobs, both aimed at the same defect class as issue #8 — a check nobody runs, and a
 * number nobody can falsify.
 *
 * 1. `scripts/ui-gate-ci.cjs` is what a CI step will invoke, so it must not be the one
 *    piece of the chain that nothing tests. The parts with teeth are the ones whose
 *    failure mode is a GREEN run about the wrong thing: a preview that slid to another
 *    port, a BASE pointing somewhere the preview is not, a leaked server holding 4173 for
 *    the next run.
 *
 * 2. The docs publish facts about the gate — how many routes it sweeps, which line of
 *    `ui-gate.cjs` to read. On 2026-09-19 every one of those facts was wrong:
 *    `docs/OUTSTANDING_WORK.md` said **188 passed across 10 routes** and cited
 *    `ui-gate.cjs:66` for the route list, while `ROUTES` was at `:80` and held **3**
 *    (`:66` is a comment line). The prose had been left behind by the 3-page rebuild, and
 *    nothing in the repo could see it. These tests read the gate's own source, so the next
 *    route added or line moved fails here instead of rotting in a doc.
 *
 * Lives in the frontend suite, beside `benchmarks.test.ts`, which already reaches up to
 * repo-root `docs/` the same way: `npm test` runs on every push (`ci.yml`, "Unit tests"),
 * so these guards are live today — unlike the browser gate itself, which is why they are
 * here rather than waiting on the workflow step.
 */
import { createServer, type Server } from 'node:http';
import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';

const require_ = createRequire(import.meta.url);
const runner = require_('../scripts/ui-gate-ci.cjs');
const {
  resolveConfig, previewArgs, playwrightInstallArgs, waitForServer, stopPreview,
  GATE_SCRIPT, DEFAULT_PORT, DEFAULT_API,
} = runner;

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const GATE_SOURCE = read('../scripts/ui-gate.cjs');
const PKG = JSON.parse(read('../package.json'));
const DOCS: Record<string, string> = {
  'README.md': read('../../README.md'),
  'docs/OUTSTANDING_WORK.md': read('../../docs/OUTSTANDING_WORK.md'),
};

/** The route list the gate actually sweeps, read out of the gate itself. */
const gateRoutes = (): string[] => {
  const m = GATE_SOURCE.match(/^const ROUTES\s*=\s*\[(.*?)\];/m);
  if (!m) throw new Error('ROUTES is no longer a single-line array in scripts/ui-gate.cjs');
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]);
};

describe('resolveConfig', () => {
  it('defaults to a local preview on 4173 with the live API proxied in', () => {
    const cfg = resolveConfig({});
    expect(cfg.port).toBe(DEFAULT_PORT);
    expect(cfg.host).toBe('127.0.0.1');
    expect(cfg.base).toBe(`http://127.0.0.1:${DEFAULT_PORT}`);
    expect(cfg.api).toBe(DEFAULT_API);
  });

  it('addresses 127.0.0.1 rather than localhost', () => {
    // ui-gate.cjs defaults BASE to http://localhost:4173. If the preview binds IPv4 and
    // Chromium resolves `localhost` to ::1, every navigation fails for a reason that has
    // nothing to do with the UI. Bind and address the same literal.
    expect(resolveConfig({}).base).not.toContain('localhost');
  });

  it('moves the port and the URL together', () => {
    // A port that moved without its URL means the gate audits whatever else is on 4173.
    const cfg = resolveConfig({ PREVIEW_PORT: '5199' });
    expect(cfg.port).toBe(5199);
    expect(cfg.base).toBe('http://127.0.0.1:5199');
    expect(previewArgs(cfg)).toContain('5199');
  });

  it('lets BASE and API be overridden for a run against the live site', () => {
    const cfg = resolveConfig({ BASE: 'https://example.test', API: 'http://localhost:8000' });
    expect(cfg.base).toBe('https://example.test');
    expect(cfg.api).toBe('http://localhost:8000');
  });

  it('refuses a port that is not a port, rather than becoming NaN', () => {
    expect(() => resolveConfig({ PREVIEW_PORT: 'four-one-seven-three' })).toThrow(/PREVIEW_PORT/);
    expect(() => resolveConfig({ PREVIEW_PORT: '0' })).toThrow(/PREVIEW_PORT/);
    expect(() => resolveConfig({ PREVIEW_PORT: '99999' })).toThrow(/PREVIEW_PORT/);
  });
});

describe('previewArgs', () => {
  it('pins the port with --strictPort', () => {
    // THE FALSE-CLEAN GUARD. Without --strictPort, vite preview silently increments to the
    // next free port when 4173 is held, the gate drives whatever already owns 4173, and the
    // run goes GREEN about the wrong build.
    expect(previewArgs(resolveConfig({}))).toEqual(
      ['preview', '--host', '127.0.0.1', '--port', '4173', '--strictPort'],
    );
  });
});

describe('playwrightInstallArgs', () => {
  it('only asks for system deps on a Linux CI runner', () => {
    // --with-deps shells out to `sudo apt-get`: unattended on GitHub's ubuntu images, a
    // password prompt on a developer's Linux box, meaningless on macOS and Windows.
    expect(playwrightInstallArgs('linux', { CI: 'true' })).toContain('--with-deps');
    expect(playwrightInstallArgs('linux', {})).not.toContain('--with-deps');
    expect(playwrightInstallArgs('darwin', { CI: 'true' })).not.toContain('--with-deps');
    expect(playwrightInstallArgs('win32', { CI: 'true' })).not.toContain('--with-deps');
    expect(playwrightInstallArgs('darwin', { CI: 'true' })).toContain('chromium');
  });
});

describe('waitForServer', () => {
  const servers: Server[] = [];
  afterAll(() => servers.forEach((s) => s.close()));

  it('waits through a server that is still booting', async () => {
    let hits = 0;
    const server = createServer((_req, res) => { res.writeHead(hits++ < 2 ? 503 : 200); res.end('ok'); });
    servers.push(server);
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
    const { port } = server.address() as { port: number };
    await expect(waitForServer(`http://127.0.0.1:${port}/`, { intervalMs: 20 })).resolves.toBe(200);
    expect(hits).toBeGreaterThanOrEqual(3);
  });

  it('gives up loudly on a port nobody is serving, naming the URL', async () => {
    // A silent hang here is the worst outcome: the job burns its whole timeout with no
    // clue why. The message has to say what it was waiting for.
    await expect(waitForServer('http://127.0.0.1:1/', { timeoutMs: 200, intervalMs: 20 }))
      .rejects.toThrow(/127\.0\.0\.1:1/);
  });
});

describe('stopPreview', () => {
  it('kills the preview so the port is free for the next run', async () => {
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, stdio: 'ignore' });
    const exited = new Promise<void>((r) => child.on('exit', () => r()));
    stopPreview(child);
    await expect(Promise.race([exited, new Promise((_, rej) => setTimeout(rej, 4000))])).resolves.toBeUndefined();
  });

  it('is safe to call twice, and on nothing', () => {
    // It runs from a `finally`, from `exit`, and from two signal handlers, so it is
    // genuinely called more than once on a normal run.
    expect(() => { stopPreview(undefined); stopPreview(null); }).not.toThrow();
  });
});

describe('the runner is wired to the gate it claims to run', () => {
  it('points at a gate script that exists', () => {
    expect(existsSync(GATE_SCRIPT)).toBe(true);
  });

  it('is reachable as `npm run ui-gate:ci`, and leaves `npm run ui-gate` alone', () => {
    expect(PKG.scripts['ui-gate:ci']).toBe('node scripts/ui-gate-ci.cjs');
    expect(PKG.scripts['ui-gate']).toBe('node scripts/ui-gate.cjs');
  });

  it('hands the gate its target through the env vars the gate reads', () => {
    // If ui-gate.cjs ever hard-codes its target, this runner would serve a fresh build on
    // 4173 and then gate the LIVE SITE instead — a pass about the wrong artifact, which is
    // worse than a failure because nothing looks wrong.
    expect(GATE_SOURCE).toMatch(/process\.env\.BASE/);
    expect(GATE_SOURCE).toMatch(/process\.env\.API/);
  });

  it('relies only on devDependencies CI already installs', () => {
    // The whole premise of the one-line CI step is that `npm ci` has already got these.
    expect(PKG.devDependencies).toHaveProperty('playwright');
    expect(PKG.devDependencies).toHaveProperty('axe-core');
    expect(PKG.devDependencies).toHaveProperty('vite');
  });
});

describe('the docs describe the gate that exists', () => {
  it('cites line numbers in ui-gate.cjs that resolve to what they claim', () => {
    // `docs/OUTSTANDING_WORK.md:915` cited `ui-gate.cjs:66` for the routes array. Line 66
    // is a comment; ROUTES is at 80. A citation pointing at a blank or unrelated line is a
    // reader sent to the wrong place, and nothing could see it.
    const lines = GATE_SOURCE.split('\n');
    const bad: string[] = [];
    for (const [doc, text] of Object.entries(DOCS)) {
      for (const m of text.matchAll(/ui-gate\.cjs:(\d+)/g)) {
        const n = Number(m[1]);
        const line = lines[n - 1];
        if (!line?.trim()) bad.push(`${doc} cites ui-gate.cjs:${n}, which is blank or past EOF`);
        else if (!line.includes('ROUTES')) bad.push(`${doc} cites ui-gate.cjs:${n} for the route list, but that line is: ${line.trim()}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it('states the number of routes the gate actually sweeps', () => {
    // "10 routes" survived the shrink to 3 pages in two places. Any route count written
    // near a ui-gate mention has to match the gate's own array.
    const routes = gateRoutes();
    expect(routes).toEqual(['/route-plan', '/simulation', '/benchmarks']);
    const wrong: string[] = [];
    for (const [doc, text] of Object.entries(DOCS)) {
      text.split('\n').forEach((line, i) => {
        // Markdown table rows are the numbered postmortem log: a record of what was true at
        // the time, deliberately not rewritten as the code moves. Only running prose is
        // making a claim about the gate as it stands now.
        if (line.trimStart().startsWith('|')) return;
        for (const m of line.matchAll(/\*{0,2}(\d+) routes?\*{0,2}/g)) {
          const around = text.slice(Math.max(0, text.indexOf(line) - 400), text.indexOf(line) + line.length + 400);
          if (!around.includes('ui-gate')) continue;
          if (Number(m[1]) !== routes.length) {
            wrong.push(`${doc}:${i + 1} says "${m[0]}" but ROUTES holds ${routes.length}`);
          }
        }
      });
    }
    expect(wrong).toEqual([]);
  });

  it('publishes no absolute pass count for the gate that no run can back up', () => {
    // docs/OUTSTANDING_WORK.md's own standing rule, written about the backend suite: an
    // absolute pass count "was stated as 997 while the suite actually collected 1,121, and
    // it moves with every test added. The gate is 'nothing red but that one', not a number."
    // It said **188 passed** for this gate while the README said 104 and issue #8 said 256.
    const text = DOCS['docs/OUTSTANDING_WORK.md'];
    const claims = [...text.matchAll(/\*{0,2}(\d+) passed, (\d+) failed\*{0,2}/g)]
      .filter((m) => text.slice(Math.max(0, m.index! - 400), m.index! + 400).includes('ui-gate'))
      .map((m) => m[0]);
    expect(claims).toEqual([]);
  });
});
