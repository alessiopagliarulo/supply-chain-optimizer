import type { PlanNode, RouteReport } from '../services/api';
import { routeColor } from '../lib/colors';
import { useElementWidth } from '../lib/useElementWidth';

/** Room for the axis labels: y labels on the left, x labels underneath. */
const PAD_LEFT = 44;
const PAD_RIGHT = 16;
const PAD_TOP = 16;
const PAD_BOTTOM = 32;
const MAX_HEIGHT = 520;

/** A few round tick values across [lo, hi]. */
function ticks(lo: number, hi: number, count: number): number[] {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => span / s <= count) ?? 10 * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

interface RoutePlotProps {
  nodes: PlanNode[];
  routes: number[][];
  /** Accessible summary of what the plot shows. */
  label: string;
}

/**
 * Depot, customers and every route on the instance's own x/y plane - a plain
 * scatter, not a map: the coordinates are planar and the distances Euclidean.
 * Both axes share one scale so a route's drawn length is its real length.
 */
export default function RoutePlot({ nodes, routes, label }: RoutePlotProps) {
  const [ref, width] = useElementWidth<HTMLDivElement>();
  const W = Math.max(width, 1);
  const H = Math.min(MAX_HEIGHT, Math.max(260, Math.round(W * 0.72)));

  const xs = nodes.map((n) => n.x);
  const ys = nodes.map((n) => n.y);
  const [xMin, xMax] = [Math.min(...xs), Math.max(...xs)];
  const [yMin, yMax] = [Math.min(...ys), Math.max(...ys)];
  const plotW = W - PAD_LEFT - PAD_RIGHT;
  const plotH = H - PAD_TOP - PAD_BOTTOM;
  const k = Math.min(plotW / (xMax - xMin || 1), plotH / (yMax - yMin || 1));
  const x0 = PAD_LEFT + (plotW - (xMax - xMin) * k) / 2;
  const y0 = PAD_TOP + (plotH - (yMax - yMin) * k) / 2;
  const sx = (x: number) => x0 + (x - xMin) * k;
  const sy = (y: number) => y0 + (yMax - y) * k;

  const routeOf = new Map<number, number>();
  routes.forEach((r, i) => r.forEach((c) => routeOf.set(c, i)));
  const xTicks = ticks(xMin, xMax, W < 480 ? 4 : 6);
  const yTicks = ticks(yMin, yMax, 5);
  const depot = nodes[0];
  const r = W < 480 ? 3.5 : 4.5;

  return (
    <div ref={ref} className="w-full">
      {width > 0 && (
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="block">
          <rect x={0} y={0} width={W} height={H} fill="#0b1220" rx={8} />
          {xTicks.map((t) => (
            <g key={`x${t}`}>
              <line x1={sx(t)} x2={sx(t)} y1={PAD_TOP} y2={H - PAD_BOTTOM} stroke="#1e293b" />
              <text x={sx(t)} y={H - 10} fill="#94a3b8" fontSize={12} textAnchor="middle">
                {t}
              </text>
            </g>
          ))}
          {yTicks.map((t) => (
            <g key={`y${t}`}>
              <line x1={PAD_LEFT} x2={W - PAD_RIGHT} y1={sy(t)} y2={sy(t)} stroke="#1e293b" />
              <text x={PAD_LEFT - 8} y={sy(t) + 4} fill="#94a3b8" fontSize={12} textAnchor="end">
                {t}
              </text>
            </g>
          ))}

          {routes.map((route, i) => (
            <polyline
              key={`r${i}`}
              points={[0, ...route, 0].map((n) => `${sx(nodes[n].x)},${sy(nodes[n].y)}`).join(' ')}
              fill="none"
              stroke={routeColor(i)}
              strokeWidth={2}
              strokeLinejoin="round"
              opacity={0.9}
            />
          ))}

          {nodes.slice(1).map((n, i) => {
            const id = i + 1;
            const routeIndex = routeOf.get(id);
            return (
              <circle
                key={`c${id}`}
                cx={sx(n.x)}
                cy={sy(n.y)}
                r={r}
                fill={routeIndex === undefined ? '#64748b' : routeColor(routeIndex)}
                stroke="#0b1220"
                strokeWidth={1.5}
              >
                <title>{`Customer ${id}: demand ${n.demand}, window ${n.ready}-${n.due}`}</title>
              </circle>
            );
          })}

          <rect
            x={sx(depot.x) - 7}
            y={sy(depot.y) - 7}
            width={14}
            height={14}
            fill="#f8fafc"
            stroke="#0b1220"
            strokeWidth={2}
          >
            <title>{`Depot: open ${depot.ready}-${depot.due}`}</title>
          </rect>
        </svg>
      )}
    </div>
  );
}

interface RouteLegendProps {
  reports: RouteReport[];
  capacity: number;
}

/** One row per route: its colour, stops, load against capacity, and distance. */
export function RouteLegend({ reports, capacity }: RouteLegendProps) {
  return (
    <ul className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-x-4 gap-y-1.5 text-sm" aria-label="Routes">
      <li className="flex items-center gap-2 min-w-0">
        <span className="w-3 h-3 shrink-0 bg-slate-50 border border-slate-900" aria-hidden="true" />
        <span className="text-slate-200 font-medium">Depot</span>
      </li>
      {reports.map((rep, i) => (
        <li key={i} className="flex items-center gap-2 min-w-0">
          <span className="w-3 h-3 rounded-full shrink-0" style={{ backgroundColor: routeColor(i) }} aria-hidden="true" />
          <span className="text-slate-200 font-medium whitespace-nowrap">{`Route ${i + 1}`}</span>
          <span className="text-slate-400 truncate">
            {`${rep.customers.length} stops · load ${rep.load}/${capacity} · distance ${rep.distance}`}
          </span>
        </li>
      ))}
    </ul>
  );
}
