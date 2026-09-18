/**
 * Twelve hues picked to stay distinct from each other and readable on the dark
 * background. More routes than colours reuse the palette; the route number in the
 * legend is what tells them apart.
 */
export const ROUTE_COLORS = [
  '#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#f87171',
  '#22d3ee', '#a3e635', '#fb923c', '#e879f9', '#2dd4bf', '#facc15',
];

export const routeColor = (i: number): string => ROUTE_COLORS[i % ROUTE_COLORS.length];

/** One colour per solver, shared by the Benchmarks table and chart. */
export const SOLVER_COLORS: Record<string, string> = {
  cpsat: '#60a5fa',
  clarke_wright: '#fbbf24',
  ortools: '#34d399',
};

export const solverColor = (solver: string, i: number): string => SOLVER_COLORS[solver] ?? routeColor(i + 3);
