import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import type { PlanInstance, SolveResponse } from '../services/api';

/** A solved route plan, handed from Route Plan to Simulation. */
export interface CurrentPlan {
  /** What the reader picked or uploaded, e.g. "C101.25" or "customers.csv". */
  label: string;
  instance: PlanInstance;
  solution: SolveResponse;
}

interface PlanState {
  plan: CurrentPlan | null;
  setPlan: (plan: CurrentPlan | null) => void;
}

/**
 * Kept in sessionStorage so a refresh or a trip to another page does not lose the
 * plan. zustand's JSON storage already treats unavailable storage (private mode,
 * blocked site data) as empty, so the pages work the same without it.
 */
export const usePlanStore = create<PlanState>()(
  persist(
    (set) => ({
      plan: null,
      setPlan: (plan) => set({ plan }),
    }),
    { name: 'current-route-plan', storage: createJSONStorage(() => sessionStorage) },
  ),
);
