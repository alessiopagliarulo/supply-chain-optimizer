/**
 * The Route Plan page's solve bookkeeping (src/lib/latestRun.ts). The first test is the
 * audited deadlock: a request invalidated while in flight must still end "busy".
 */
import { describe, expect, it } from 'vitest';
import { createLatestRunner } from '../src/lib/latestRun';

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function harness() {
  const log = { busy: [] as boolean[], results: [] as string[], errors: [] as unknown[] };
  const runner = createLatestRunner<string>({
    onBusy: (b) => log.busy.push(b),
    onResult: (v) => log.results.push(v),
    onError: (e) => log.errors.push(e),
  });
  return { log, runner };
}

describe('createLatestRunner', () => {
  it('ends busy when the only request was invalidated before it answered', async () => {
    const { log, runner } = harness();
    const d = deferred<string>();
    const done = runner.run(() => d.promise);
    runner.invalidate(); // the reader ticks a destination during the opening auto-solve
    d.resolve('stale plan');
    await done;
    expect(log.busy.at(-1)).toBe(false);
    expect(log.results).toEqual([]); // and the stale plan is not shown
  });

  it('ends busy when an invalidated request fails', async () => {
    const { log, runner } = harness();
    const d = deferred<string>();
    const done = runner.run(() => d.promise);
    runner.invalidate();
    d.reject(new Error('timeout'));
    await done;
    expect(log.busy.at(-1)).toBe(false);
    expect(log.errors).toEqual([]);
  });

  it('shows only the newest of overlapping requests and stays busy until both finish', async () => {
    const { log, runner } = harness();
    const first = deferred<string>();
    const second = deferred<string>();
    const a = runner.run(() => first.promise);
    const b = runner.run(() => second.promise);
    second.resolve('new');
    await b;
    expect(log.results).toEqual(['new']);
    expect(log.busy.at(-1)).toBe(true); // the first is still running
    first.resolve('old');
    await a;
    expect(log.results).toEqual(['new']);
    expect(log.busy.at(-1)).toBe(false);
  });

  it('delivers a current result and error', async () => {
    const { log, runner } = harness();
    await runner.run(() => Promise.resolve('plan'));
    await runner.run(() => Promise.reject(new Error('422')));
    expect(log.results).toEqual(['plan']);
    expect(log.errors).toHaveLength(1);
    expect(log.busy).toEqual([true, false, true, false]);
  });
});
