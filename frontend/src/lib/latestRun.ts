/**
 * Runs async requests where only the newest may land, and "busy" always ends.
 *
 * Two separate questions, deliberately kept apart:
 *   - May this response be SHOWN? Only if nothing newer started and nothing
 *     invalidated it since (the inputs it was computed for may have changed).
 *   - Is anything still RUNNING? That depends on in-flight requests alone. A stale
 *     response still finished, so it must still end the busy state.
 *
 * Mixing the two (clearing "busy" only when the response was the latest) once left
 * the Route Plan page on a permanent "Planning routes..." spinner: ticking a
 * destination during the opening auto-solve invalidated it, the response arrived,
 * nothing cleared busy, and the Plan routes button stayed disabled until a reload.
 */
export interface LatestRunner<T> {
  /** Start `request`; its result reaches `onResult`/`onError` only if still current. */
  run: (request: () => Promise<T>) => Promise<void>;
  /** Make every in-flight request stale without cancelling it. */
  invalidate: () => void;
}

export function createLatestRunner<T>(handlers: {
  onBusy: (busy: boolean) => void;
  onResult: (value: T) => void;
  onError: (err: unknown) => void;
}): LatestRunner<T> {
  let latest = 0;
  let inFlight = 0;
  return {
    invalidate: () => {
      latest += 1;
    },
    run: async (request) => {
      const mine = ++latest;
      inFlight += 1;
      handlers.onBusy(true);
      try {
        const value = await request();
        if (mine === latest) handlers.onResult(value);
      } catch (err) {
        if (mine === latest) handlers.onError(err);
      } finally {
        inFlight -= 1;
        if (inFlight === 0) handlers.onBusy(false);
      }
    },
  };
}
