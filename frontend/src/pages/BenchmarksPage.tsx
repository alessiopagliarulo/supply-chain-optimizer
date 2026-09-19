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
import {
  SUPPORTED_SCHEMA_VERSION,
  gapLabel,
  hasComparableGap,
  parseBenchmarks,
  percentLabel,
  ranksVehiclesFirst,
  sizesLabel,
  sortRows,
  vehicleGapLabel,
  type BenchmarkRow,
  type BenchmarkSummary,
  type BestKnownSource,
  type ParsedBenchmarks,
  type SortKey,
} from '../lib/benchmarks';
import { solverColor } from '../lib/colors';
import { Busy, Card, ErrorBox, Page } from '../components/ui';

const COLUMNS: { key: SortKey; label: string; numeric: boolean }[] = [
  { key: 'instance', label: 'Instance', numeric: false },
  { key: 'num_customers', label: 'Customers', numeric: true },
  { key: 'solver', label: 'Solver', numeric: false },
  { key: 'status', label: 'Status', numeric: false },
  { key: 'vehicles_used', label: 'Vehicles', numeric: true },
  { key: 'vehicle_gap', label: 'Vehicle gap', numeric: true },
  { key: 'distance', label: 'Distance', numeric: true },
  { key: 'gap_percent', label: 'Distance gap', numeric: true },
  { key: 'runtime_seconds', label: 'Runtime (s)', numeric: true },
];

const fmtStatus = (s: string) => s.replace(/_/g, ' ');
const notComparableTitle = (r: BenchmarkRow) =>
  `Used ${r.vehicles_used} vehicles; the best known uses ${r.reference?.vehicles ?? '?'}. The best known ranks fewest vehicles first, so distances are only compared at the same vehicle count.`;
const fmtReference = (r: BenchmarkRow) =>
  r.reference === null
    ? 'not published'
    : `${r.reference.distance}${r.reference.vehicles === null ? '' : ` · ${r.reference.vehicles} veh.`}`;

function GapTooltip({ active, payload }: { active?: boolean; payload?: { payload: BenchmarkRow }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-200 flex flex-col gap-0.5">
      <span className="font-semibold">{`${r.instance} · ${r.num_customers} customers · ${r.solver}`}</span>
      <span>{`Distance gap ${gapLabel(r)} · vehicles ${vehicleGapLabel(r)} · runtime ${r.runtime_seconds.toFixed(2)} s`}</span>
      <span>{`Distance ${r.distance === null ? 'no solution' : r.distance.toFixed(2)} · best known ${fmtReference(r)}`}</span>
    </div>
  );
}

function GapRuntimeChart({ rows }: { rows: BenchmarkRow[] }) {
  const plotted = rows.filter(hasComparableGap);
  // From the plotted rows, so a solver with nothing comparable gets no empty legend entry.
  const solvers = [...new Set(plotted.map((r) => r.solver))].sort();
  const otherFleet = rows.filter((r) => r.gap_comparable === false);
  const fleetNote =
    otherFleet.length === 0
      ? null
      : `${otherFleet.length} ${otherFleet.length === 1 ? 'row is' : 'rows are'} not plotted because the solver used a different number of vehicles than the best-known solution, so its distance cannot be compared. Their vehicle gap is in the table.`;
  if (plotted.length === 0) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm text-slate-400">No row in this selection has a comparable distance gap to plot.</p>
        {fleetNote && <p className="text-xs text-slate-400 leading-relaxed">{fleetNote}</p>}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
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
              name="Distance gap"
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
      {fleetNote && <p className="text-xs text-slate-400 leading-relaxed">{fleetNote}</p>}
    </div>
  );
}

function SolverName({ solver }: { solver: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: solverColor(solver, 0) }} aria-hidden="true" />
      {solver}
    </span>
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
      <table data-testid="benchmark-results" className="w-full text-sm text-left tabular-nums">
        <caption className="sr-only">Benchmark results, one row per instance, size and solver</caption>
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
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr key={`${r.instance}-${r.num_customers}-${r.solver}-${i}`} className="border-b border-slate-800/60 text-slate-300">
              <td className="py-1.5 px-2 whitespace-nowrap text-slate-100">{r.instance}</td>
              <td className="py-1.5 px-2 text-right">{r.num_customers}</td>
              <td className="py-1.5 px-2 whitespace-nowrap">
                <SolverName solver={r.solver} />
              </td>
              <td className={`py-1.5 px-2 whitespace-nowrap ${r.feasible ? 'text-emerald-400' : 'text-red-400'}`}>{fmtStatus(r.status)}</td>
              <td className="py-1.5 px-2 text-right">{r.vehicles_used}</td>
              <td className={`py-1.5 px-2 text-right whitespace-nowrap ${r.gap_comparable === false ? 'text-amber-300' : ''}`}>
                {vehicleGapLabel(r)}
              </td>
              <td className="py-1.5 px-2 text-right whitespace-nowrap">{r.distance === null ? 'no solution' : r.distance.toFixed(2)}</td>
              <td
                className={`py-1.5 px-2 text-right whitespace-nowrap ${r.gap_comparable === false ? 'text-slate-500' : ''}`}
                title={r.gap_comparable === false ? notComparableTitle(r) : undefined}
              >
                {gapLabel(r)}
              </td>
              <td className="py-1.5 px-2 text-right">{r.runtime_seconds.toFixed(2)}</td>
              <td className={`py-1.5 px-2 text-right whitespace-nowrap ${r.reference === null ? 'text-slate-500' : ''}`}>
                {fmtReference(r)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const sourceSizes = (sources: BestKnownSource[]) => sizesLabel(sources.flatMap((s) => s.applies_to));

/** The plain-English note on how rows are compared, with the sizes each reference table covers. */
function ComparisonNote({ sources }: { sources: BestKnownSource[] }) {
  const vehiclesFirst = sourceSizes(sources.filter(ranksVehiclesFirst));
  const distanceOnly = sourceSizes(sources.filter((s) => !ranksVehiclesFirst(s)));
  // Without a vehicles-first reference every gap is a plain distance comparison: nothing to explain.
  if (!vehiclesFirst) return null;
  return (
    <Card title="How results are compared">
      <p className="text-sm text-slate-300 leading-relaxed">
        {`The ${vehiclesFirst}-customer best-known solutions are ranked by number of trucks first, then by total distance. `}
        Sending out more trucks can shorten the total distance, but that does not beat a solution that needed fewer
        trucks. So a distance gap is only shown when the solver used the same number of trucks as the best known;
        otherwise the table shows the difference in trucks and marks the distance gap &quot;not comparable&quot;.
        {distanceOnly && ` The ${distanceOnly}-customer references rank on distance alone, so those gaps are always compared directly.`}
      </p>
    </Card>
  );
}

const fmtMean = (s: BenchmarkSummary) => (s.mean_gap_percent === null ? 'none comparable' : percentLabel(s.mean_gap_percent));

/** The script's averages, each shown with how many runs it covers. */
function AveragesTable({ summary, sources }: { summary: BenchmarkSummary[]; sources: BestKnownSource[] }) {
  const vehiclesFirst = sourceSizes(sources.filter(ranksVehiclesFirst));
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-slate-400 leading-relaxed">
        Each average covers only the runs with a comparable distance gap (&quot;runs averaged&quot;); runs with no
        feasible solution or no published best-known value are left out.
        {vehiclesFirst &&
          ` At ${vehiclesFirst} customers the comparable runs are the ones that matched the best-known truck count, usually the instances the solver handled well, so those averages are not the solver's performance over all its runs.`}
        {' '}The truck columns count every feasible run with a best-known value.
      </p>
      {/* Focusable and named so a keyboard user can scroll it when it overflows on a phone. */}
      <div
        className="overflow-x-auto rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
        tabIndex={0}
        role="region"
        aria-label="Averages per solver table"
      >
        <table data-testid="benchmark-averages" className="w-full text-sm text-left tabular-nums">
          <caption className="sr-only">Average distance gap per instance size and solver, with the runs each covers</caption>
          <thead className="text-xs text-slate-400 border-b border-slate-800">
            <tr>
              <th scope="col" className="font-medium px-2 py-2">Customers</th>
              <th scope="col" className="font-medium px-2 py-2">Solver</th>
              <th scope="col" className="font-medium px-2 py-2 text-right whitespace-nowrap">Mean distance gap</th>
              <th scope="col" className="font-medium px-2 py-2 text-right whitespace-nowrap">Runs averaged</th>
              <th scope="col" className="font-medium px-2 py-2 text-right whitespace-nowrap">More trucks</th>
              <th scope="col" className="font-medium px-2 py-2 text-right whitespace-nowrap">Fewer trucks</th>
            </tr>
          </thead>
          <tbody>
            {summary.map((s) => (
              <tr key={`${s.num_customers}-${s.solver}`} className="border-b border-slate-800/60 text-slate-300">
                <td className="py-1.5 px-2">{s.num_customers}</td>
                <td className="py-1.5 px-2 whitespace-nowrap">
                  <SolverName solver={s.solver} />
                </td>
                <td className={`py-1.5 px-2 text-right whitespace-nowrap ${s.mean_gap_percent === null ? 'text-slate-500' : ''}`}>
                  {fmtMean(s)}
                </td>
                <td className="py-1.5 px-2 text-right whitespace-nowrap">{`${s.gap_comparable} of ${s.runs}`}</td>
                <td className="py-1.5 px-2 text-right">{s.more_vehicles_than_reference}</td>
                <td className="py-1.5 px-2 text-right">{s.fewer_vehicles_than_reference}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SizeFilter({ sizes, value, onChange }: { sizes: number[]; value: number | null; onChange: (v: number | null) => void }) {
  const options: [number | null, string][] = [[null, 'All sizes'], ...sizes.map((s): [number, string] => [s, `${s} customers`])];
  return (
    <div role="group" aria-label="Instance size" className="flex flex-wrap gap-2">
      {options.map(([v, label]) => (
        <button
          key={label}
          onClick={() => onChange(v)}
          aria-pressed={value === v}
          className={`min-h-[44px] px-3 rounded-lg text-sm border focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 ${
            value === v ? 'bg-blue-600 border-blue-500 text-white' : 'border-slate-700 text-slate-300 hover:text-white hover:border-slate-500'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function Provenance({ data, artifact }: { data: ParsedBenchmarks; artifact: string }) {
  const p = data.provenance;
  const items: [string, string | null][] = [
    ['Data source', p.data_source],
    ['Time limit per solve', p.time_limit_seconds === null ? null : `${p.time_limit_seconds} s`],
    ['Generated', p.generated_at],
    ['Code commit', p.git_commit],
    ['OR-Tools', p.ortools_version],
    ['Platform', p.platform],
    ['Python', p.python_version],
    ['Artifact', artifact],
  ];
  return (
    <div className="flex flex-col gap-4">
      <dl className="grid grid-cols-1 sm:grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-sm">
        {items.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-slate-400">{k}</dt>
            <dd className="text-slate-200 break-words">{v ?? 'not recorded in the artifact'}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium text-slate-200">Best-known values</h3>
        {p.best_known_sources.length === 0 ? (
          <p className="text-sm text-slate-400">Not recorded in the artifact.</p>
        ) : (
          <ul className="flex flex-col gap-2 text-sm text-slate-300 leading-relaxed">
            {p.best_known_sources.map((b) => (
              <li key={b.title}>
                <span className="text-slate-200">{b.title}</span>
                {b.applies_to.length > 0 && <span className="text-slate-400">{` - used for ${b.applies_to.join(' and ')} customers.`}</span>}
                {b.distance_convention && <span className="block text-xs text-slate-400">{`Distance convention: ${b.distance_convention.replace(/\.*$/, '')}.`}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Results({ data }: { data: ParsedBenchmarks }) {
  const sizes = useMemo(() => [...new Set(data.rows.map((r) => r.num_customers))].sort((a, b) => a - b), [data]);
  const [size, setSize] = useState<number | null>(null);
  const rows = useMemo(() => (size === null ? data.rows : data.rows.filter((r) => r.num_customers === size)), [data, size]);
  const summary = useMemo(
    () => (size === null ? data.summary : data.summary.filter((s) => s.num_customers === size)),
    [data, size],
  );
  return (
    <>
      <SizeFilter sizes={sizes} value={size} onChange={setSize} />
      <ComparisonNote sources={data.provenance.best_known_sources} />
      <Card title="Distance gap vs runtime, per solver">
        <p className="text-xs text-slate-400 leading-relaxed">
          One point per instance, size and solver. The distance gap is the percentage by which a solver's total
          distance exceeds the best-known distance. Rows with no feasible solution, no published best-known value, or a
          different number of vehicles than the best known are not plotted.
        </p>
        <GapRuntimeChart rows={rows} />
      </Card>
      {summary.length > 0 && (
        <Card title="Averages per solver">
          <AveragesTable summary={summary} sources={data.provenance.best_known_sources} />
        </Card>
      )}
      <Card title="Results">
        <p className="text-xs text-slate-400 leading-relaxed">
          Each gap is measured in its reference's own distance convention (listed under Provenance), so a solver's
          full-precision distance can differ slightly from the distance its gap was computed on. Status "optimal" means
          CP-SAT proved optimality on its own integer model, where travel times are rounded up; that model is stricter
          than the reference's, so an optimal row can still show a small positive gap.
        </p>
        <BenchmarkTable rows={rows} />
      </Card>
    </>
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
    () => (response?.available ? parseBenchmarks(response.results, response.provenance, response.schema_version, response.summary) : null),
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
          {parsed.summary_unreadable > 0 && (
            <ErrorBox title="Some averages could not be read">
              {`${parsed.summary_unreadable} of ${parsed.summary_unreadable + parsed.summary.length} summary entries in the artifact are missing required fields and are not shown.`}
            </ErrorBox>
          )}
          {!parsed.schema_supported && (
            <ErrorBox title="Benchmark file format not recognised">
              {`The artifact declares schema version ${parsed.schema_version ?? 'none'}, and this page reads version ${SUPPORTED_SCHEMA_VERSION}. Rows may not be read correctly until the page is updated.`}
            </ErrorBox>
          )}
          {parsed.rows.length === 0 ? (
            <Card>
              <p className="text-sm text-slate-400">The benchmark results file has no readable rows.</p>
            </Card>
          ) : (
            <Results data={parsed} />
          )}
          <Card title="Provenance">
            <Provenance data={parsed} artifact={response?.artifact ?? ''} />
          </Card>
        </>
      )}
    </Page>
  );
}
