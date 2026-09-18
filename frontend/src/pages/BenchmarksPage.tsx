import { useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, FileQuestion } from 'lucide-react';
import {
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { errorMessage, routingApi, type BenchmarksResponse } from '../services/api';
import { parseBenchmarks, sortRows, type BenchmarkRow, type ParsedBenchmarks, type SortKey } from '../lib/benchmarks';
import { solverColor } from '../lib/colors';
import { Busy, Card, ErrorBox, Page } from '../components/ui';

const COLUMNS: { key: SortKey; label: string; numeric: boolean }[] = [
  { key: 'instance', label: 'Instance', numeric: false },
  { key: 'num_customers', label: 'Customers', numeric: true },
  { key: 'solver', label: 'Solver', numeric: false },
  { key: 'vehicles_used', label: 'Vehicles', numeric: true },
  { key: 'total_distance', label: 'Distance', numeric: true },
  { key: 'gap_percent', label: 'Gap vs best known', numeric: true },
  { key: 'runtime_seconds', label: 'Runtime (s)', numeric: true },
  { key: 'feasible', label: 'Feasible', numeric: false },
];

const dash = '-';
const fmtGap = (g: number | null) => (g === null ? dash : `${g >= 0 ? '+' : ''}${g.toFixed(2)}%`);

function GapTooltip({ active, payload }: { active?: boolean; payload?: { payload: BenchmarkRow }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-200 flex flex-col gap-0.5">
      <span className="font-semibold">{`${r.instance} · ${r.solver}`}</span>
      <span>{`Gap ${fmtGap(r.gap_percent)} · runtime ${r.runtime_seconds.toFixed(2)} s`}</span>
      <span>{`Distance ${r.total_distance} · vehicles ${r.vehicles_used} · ${r.feasible ? 'feasible' : 'infeasible'}`}</span>
    </div>
  );
}

function GapRuntimeChart({ rows }: { rows: BenchmarkRow[] }) {
  const solvers = [...new Set(rows.map((r) => r.solver))].sort();
  const plotted = rows.filter((r) => r.gap_percent !== null);
  if (plotted.length === 0) {
    return <p className="text-sm text-slate-400">No row has a gap to plot: none carries a best-known value.</p>;
  }
  return (
    <div className="h-80 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 16, bottom: 28, left: 8 }}>
          <CartesianGrid stroke="#1e293b" />
          <XAxis
            type="number"
            dataKey="runtime_seconds"
            name="Runtime"
            unit=" s"
            domain={[0, 'auto']}
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            label={{ value: 'Runtime (seconds)', position: 'insideBottom', offset: -16, fill: '#94a3b8', fontSize: 12 }}
          />
          <YAxis
            type="number"
            dataKey="gap_percent"
            name="Gap"
            unit="%"
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            width={56}
          />
          <Tooltip content={<GapTooltip />} cursor={{ stroke: '#475569' }} />
          <Legend verticalAlign="top" height={32} wrapperStyle={{ fontSize: 12, color: '#cbd5e1' }} />
          {solvers.map((s, i) => (
            <Scatter key={s} name={s} data={plotted.filter((r) => r.solver === s)} fill={solverColor(s, i)} />
          ))}
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

function BenchmarkTable({ rows }: { rows: BenchmarkRow[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('instance');
  const [ascending, setAscending] = useState(true);
  const sorted = useMemo(() => sortRows(rows, sortKey, ascending), [rows, sortKey, ascending]);

  const onSort = (key: SortKey) => {
    if (key === sortKey) setAscending(!ascending);
    else {
      setSortKey(key);
      setAscending(true);
    }
  };

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm text-left tabular-nums">
        <caption className="sr-only">Benchmark results, one row per instance and solver</caption>
        <thead className="text-xs text-slate-400 border-b border-slate-800">
          <tr>
            {COLUMNS.map((c) => {
              const active = c.key === sortKey;
              return (
                <th
                  key={c.key}
                  scope="col"
                  aria-sort={active ? (ascending ? 'ascending' : 'descending') : 'none'}
                  className={`font-medium ${c.numeric ? 'text-right' : ''}`}
                >
                  <button
                    onClick={() => onSort(c.key)}
                    className={`inline-flex items-center gap-1 min-h-[44px] px-2 whitespace-nowrap rounded hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 ${
                      active ? 'text-white' : ''
                    }`}
                  >
                    {c.label}
                    {active &&
                      (ascending ? <ArrowUp className="w-3.5 h-3.5" aria-hidden="true" /> : <ArrowDown className="w-3.5 h-3.5" aria-hidden="true" />)}
                  </button>
                </th>
              );
            })}
            <th scope="col" className="font-medium text-right px-2 whitespace-nowrap">
              Best known
            </th>
            <th scope="col" className="font-medium px-2">
              Status
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr key={`${r.instance}-${r.solver}-${i}`} className="border-b border-slate-800/60 text-slate-300">
              <td className="py-1.5 px-2 whitespace-nowrap text-slate-100">{r.instance}</td>
              <td className="py-1.5 px-2 text-right">{r.num_customers}</td>
              <td className="py-1.5 px-2 whitespace-nowrap">
                <span className="inline-flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: solverColor(r.solver, 0) }} aria-hidden="true" />
                  {r.solver}
                </span>
              </td>
              <td className="py-1.5 px-2 text-right">{r.vehicles_used}</td>
              <td className="py-1.5 px-2 text-right">{r.total_distance}</td>
              <td className="py-1.5 px-2 text-right">{fmtGap(r.gap_percent)}</td>
              <td className="py-1.5 px-2 text-right">{r.runtime_seconds.toFixed(2)}</td>
              <td className={`py-1.5 px-2 ${r.feasible ? 'text-emerald-400' : 'text-red-400'}`}>{r.feasible ? 'yes' : 'no'}</td>
              <td className="py-1.5 px-2 text-right whitespace-nowrap">
                {r.best_known_distance === null
                  ? dash
                  : `${r.best_known_distance}${r.best_known_vehicles === null ? '' : ` · ${r.best_known_vehicles} veh.`}`}
              </td>
              <td className="py-1.5 px-2 whitespace-nowrap">{r.solver_status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Provenance({ data, artifact }: { data: ParsedBenchmarks; artifact: string }) {
  const p = data.provenance;
  const rowSources = [...new Set(data.rows.map((r) => r.best_known_source).filter((s): s is string => s !== null))];
  const bestKnown = p.best_known_source ?? (rowSources.length ? rowSources.join('; ') : null);
  const items: [string, string | null][] = [
    ['Data source', p.source],
    ['Best-known source', bestKnown],
    ['Time limit per solve', p.time_limit_seconds === null ? null : `${p.time_limit_seconds} s`],
    ['Generated', p.timestamp],
    ['Platform', p.platform],
    ['Python', p.python_version],
    ['Artifact', artifact],
  ];
  return (
    <dl className="grid grid-cols-1 sm:grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-sm">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-400">{k}</dt>
          <dd className="text-slate-200 break-words">{v ?? 'not recorded in the artifact'}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function BenchmarksPage() {
  const [response, setResponse] = useState<BenchmarksResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    routingApi
      .benchmarks()
      .then((data) => live && setResponse(data))
      .catch((err: unknown) => live && setError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, []);

  const parsed = useMemo(
    () => (response?.available ? parseBenchmarks(response.results, response.provenance) : null),
    [response],
  );

  return (
    <Page
      title="Benchmarks"
      intro="How each solver does on the Solomon CVRPTW benchmark instances against the published best-known solutions. Every figure comes from the committed benchmark results file, produced by the benchmark script; this page computes nothing."
    >
      {!response && !error && <Busy what="Loading benchmark results…" />}
      {error && <ErrorBox title="Could not load the benchmark results">{error}</ErrorBox>}

      {response && !response.available && (
        <Card>
          <div className="flex flex-col items-center text-center gap-3 py-8">
            <FileQuestion className="w-10 h-10 text-slate-500" aria-hidden="true" />
            <h2 className="text-lg font-semibold text-white">Benchmarks not generated yet</h2>
            <p className="text-sm text-slate-400 max-w-lg leading-relaxed">
              {`There is no ${response.artifact} in this deployment, so there is nothing to show. It appears here once the Solomon benchmark script has been run and its results committed.`}
            </p>
          </div>
        </Card>
      )}

      {parsed && (
        <>
          {parsed.unreadable > 0 && (
            <ErrorBox title="Some rows could not be read">
              {`${parsed.unreadable} of ${parsed.unreadable + parsed.rows.length} rows in the artifact are missing required fields and are not shown.`}
            </ErrorBox>
          )}
          {parsed.rows.length === 0 ? (
            <Card>
              <p className="text-sm text-slate-400">The benchmark results file has no readable rows.</p>
            </Card>
          ) : (
            <>
              <Card title="Gap vs runtime, per solver">
                <p className="text-xs text-slate-400 leading-relaxed">
                  One point per instance and solver. Gap is the percentage by which a solver's total distance exceeds
                  the best-known distance; rows without a best-known value are not plotted.
                </p>
                <GapRuntimeChart rows={parsed.rows} />
              </Card>
              <Card title="Results">
                <BenchmarkTable rows={parsed.rows} />
              </Card>
            </>
          )}
          <Card title="Provenance">
            <Provenance data={parsed} artifact={response?.artifact ?? ''} />
          </Card>
        </>
      )}
    </Page>
  );
}
