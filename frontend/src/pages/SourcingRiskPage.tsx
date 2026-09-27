/**
 * Sourcing Risk: the two-stage stochastic sourcing model with a mean-CVaR objective,
 * restored from git tag archive/sourcing-v1.
 *
 * The visitor picks a bill of materials and solves it; the API sweeps the risk weight
 * lambda in `min (1 - lambda) E[cost] + lambda CVaR_95[cost]` with CP-SAT and returns one
 * plan per lambda. The slider then moves along that frontier, so every figure on the page
 * is read from a live response - POST /stochastic/frontier, GET /stochastic/calibration
 * and GET /catalogue/provenance - never typed in. The model's limits are stated here, on
 * the page, from the same responses.
 *
 * Nothing is fetched on mount: a solve can take tens of seconds, so it runs on a click.
 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
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
import {
  errorMessage,
  sourcingApi,
  type CalibrationResponse,
  type CatalogueProvenance,
  type FrontierPoint,
  type FrontierResponse,
} from '../services/api';
import { Busy, Card, ErrorBox, Field, Page, Stat, buttonClass, inputClass } from '../components/ui';

interface BomLineSpec {
  componentId: number;
  mpn: string;
  perBoard: number;
}

interface BomPreset {
  id: string;
  name: string;
  boards: number;
  lines: BomLineSpec[];
}

// Reference BOMs from the archived sourcing benchmark (archive/sourcing-v1,
// seeds/run_benchmark.py BOM_CATALOG), at a production volume where each one has a real
// cost-versus-tail trade-off and solves inside the server's budget. Component ids are the
// seeded catalogue's own.
const PRESETS: BomPreset[] = [
  {
    id: 'iot_sensor_node',
    name: 'IoT sensor node',
    boards: 10000,
    lines: [
      { componentId: 334, mpn: 'ESP32-WROOM-32E-N4', perBoard: 1 },
      { componentId: 442, mpn: 'OPA861ID', perBoard: 2 },
      { componentId: 377, mpn: 'GD25Q40CTIGR', perBoard: 1 },
      { componentId: 429, mpn: 'LM317DCY', perBoard: 1 },
    ],
  },
  {
    id: 'medical_monitoring_device',
    name: 'Medical monitoring device',
    boards: 5000,
    lines: [
      { componentId: 38, mpn: 'STM32F103CBT6', perBoard: 1 },
      { componentId: 437, mpn: 'INA2128U', perBoard: 4 },
      { componentId: 439, mpn: 'PGA206PA', perBoard: 2 },
      { componentId: 444, mpn: 'TPS780330220DDCT', perBoard: 1 },
    ],
  },
  {
    id: 'pcb_power_supply',
    name: 'PCB power supply',
    boards: 10000,
    lines: [
      { componentId: 429, mpn: 'LM317DCY', perBoard: 2 },
      { componentId: 431, mpn: 'TPS767D325PWP', perBoard: 1 },
      { componentId: 457, mpn: 'UA78M33CDCY', perBoard: 2 },
      { componentId: 442, mpn: 'OPA861ID', perBoard: 1 },
    ],
  },
];

const usd = (v: number) => `$${Math.round(v).toLocaleString('en-US')}`;
const count = (v: number) => v.toLocaleString('en-US');
const pct = (v: number, digits = 1) => `${(v * 100).toFixed(digits)}%`;
const lam = (v: number) => v.toFixed(2);

/** Axis ticks whose precision follows the span, so a tight frontier still reads. */
function moneyTick(values: number[]) {
  const span = Math.max(...values) - Math.min(...values);
  if (!Number.isFinite(span) || span < 2000) return (v: number) => usd(v);
  const decimals = span / 5000 >= 1 ? 0 : 1;
  return (v: number) => `$${(v / 1000).toFixed(decimals)}k`;
}

interface Solved {
  preset: BomPreset;
  frontier: FrontierResponse;
  points: FrontierPoint[];
  calibration: CalibrationResponse | null;
  provenance: CatalogueProvenance | null;
}

function PointTooltip({ active, payload }: { active?: boolean; payload?: { payload: FrontierPoint }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-200 flex flex-col gap-0.5">
      <span className="font-semibold">{`Risk weight ${lam(p.lambda)}`}</span>
      <span>{`Expected cost ${usd(p.expected_cost_usd)} · tail cost ${usd(p.cvar_95_usd)}`}</span>
      <span>{`${p.n_suppliers} distributor${p.n_suppliers === 1 ? '' : 's'} · ${p.solver_status.toLowerCase()}`}</span>
    </div>
  );
}

function FrontierChart({ points, selected }: { points: FrontierPoint[]; selected: FrontierPoint }) {
  const xTick = moneyTick(points.map((p) => p.expected_cost_usd));
  const yTick = moneyTick(points.map((p) => p.cvar_95_usd));
  return (
    <div className="h-80 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 16, bottom: 28, left: 8 }}>
          <CartesianGrid stroke="#1e293b" />
          <XAxis
            type="number"
            dataKey="expected_cost_usd"
            name="Expected cost"
            domain={['auto', 'auto']}
            tickFormatter={xTick}
            minTickGap={24}
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            label={{ value: 'Expected cost', position: 'insideBottom', offset: -16, fill: '#94a3b8', fontSize: 12 }}
          />
          <YAxis
            type="number"
            dataKey="cvar_95_usd"
            name="Tail cost"
            domain={['auto', 'auto']}
            tickFormatter={yTick}
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            width={64}
          />
          <Tooltip content={<PointTooltip />} cursor={{ stroke: '#475569' }} />
          <Legend verticalAlign="top" height={32} wrapperStyle={{ fontSize: 12, color: '#cbd5e1' }} />
          <Scatter name="Plan per risk weight" data={points} fill="#60a5fa" line={{ stroke: '#334155' }} />
          <Scatter name="Selected" data={[selected]} fill="#34d399" shape="diamond" legendType="diamond" />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

function Limits({ solved, point }: { solved: Solved | null; point: FrontierPoint | null }) {
  const f = solved?.frontier;
  const cal = solved?.calibration;
  const year = solved?.provenance?.snapshot_year;
  return (
    <Card title="What this result rests on">
      <ul className="flex flex-col gap-3 text-sm text-slate-300 leading-relaxed list-disc pl-5">
        <li>
          <span className="font-semibold text-white">The disruption chances are assumed, not measured. </span>
          {cal && f
            ? `They start from a published base rate - ${pct(cal.parameters.base_annual_prob)} a year, from ${cal.base_rate_source.citation} - which describes whole firms, not single suppliers. Over a ${f.calibration.horizon_days}-day order window that gives each distributor in this pool a failure chance between ${pct(f.calibration.p_disruption_min)} and ${pct(f.calibration.p_disruption_max)}, set by how central it is in the supplier network. Nothing in the catalogue was used to estimate them, and failures are drawn independently, so a shared shock that takes out several distributors at once is not modelled.`
            : 'They start from a published base rate for whole firms and are spread across distributors by how central each one is in the supplier network. Nothing in the catalogue was used to estimate them, and failures are drawn independently. Solve a BOM to see the exact figures.'}
        </li>
        <li>
          <span className="font-semibold text-white">At low volume the tail rests on a few scenarios. </span>
          {f && point
            ? `The tail cost is the average of the worst 5% of outcomes. At the selected risk weight that average covers ${point.n_atoms_in_tail} distinct combination${point.n_atoms_in_tail === 1 ? '' : 's'} of failed distributors, out of ${count(f.scenarios.evaluation_set.n_atoms)} ${f.scenarios.evaluation_set.kind === 'exact' ? 'possible combinations, each weighted by its exact probability' : 'combinations in a random sample'}. When only a handful of combinations decide it, one assumed probability can move the whole number.`
            : 'The tail cost is the average of the worst 5% of outcomes. At low volume only a handful of failure combinations decide it, so one assumed probability can move the whole number.'}
        </li>
        <li>
          <span className="font-semibold text-white">The prices and stock are a frozen snapshot. </span>
          {year
            ? `Every offer is a real ${year} observation from the catalogue, not a current quote.`
            : 'Every offer is a real observation from a past year, not a current quote.'}
        </li>
        <li>
          <span className="font-semibold text-white">The solver runs on a budget. </span>
          {f
            ? `Each risk weight gets at most ${f.solver.max_time_in_seconds_per_point} seconds of CP-SAT search and the whole sweep at most ${f.solver.sweep_time_budget_s} seconds; the second stage is capped at ${count(f.scenarios.solve_set.variable_budget)} variables. This request solved ${f.solver.points_solved} of ${f.solver.points_requested} risk weights in ${f.solver.sweep_wall_seconds.toFixed(1)} seconds${f.solver.any_point_hit_time_limit ? ', and at least one hit its time limit, so that plan is the best found rather than proven optimal' : ', every one to proven optimality'}${f.scenarios.solve_set.thinned ? '. The scenario set was thinned to fit the variable cap; the costs shown are still scored on the full set.' : '.'}`
            : 'Each risk weight gets a fixed CP-SAT time limit and the whole sweep a fixed total budget; the page shows both once a BOM is solved. Only the preset BOMs are offered, because they solve inside that budget.'}
        </li>
        <li>
          <span className="font-semibold text-white">A risk-weight sweep can miss some efficient plans. </span>
          Sweeping one weight finds only the plans on the outer edge of the cost-versus-tail trade-off, so the
          true set of efficient plans can be larger than the points shown.
        </li>
      </ul>
      {f && f.caveats.length > 0 && (
        <details className="group text-sm text-slate-400">
          <summary className="cursor-pointer text-slate-300 hover:text-white min-h-[44px] flex items-center gap-1.5 list-none [&::-webkit-details-marker]:hidden">
            <ChevronRight className="w-4 h-4 transition-transform group-open:rotate-90" aria-hidden="true" />
            The API's own caveats, in full
          </summary>
          <ul className="flex flex-col gap-2 list-disc pl-5 mt-2 leading-relaxed">
            {f.caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}

export default function SourcingRiskPage() {
  const [presetId, setPresetId] = useState(PRESETS[0].id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [solved, setSolved] = useState<Solved | null>(null);
  const [index, setIndex] = useState(0);

  const preset = PRESETS.find((p) => p.id === presetId) ?? PRESETS[0];

  const solve = async () => {
    setBusy(true);
    setError(null);
    try {
      const items = preset.lines.map((l) => ({ component_id: l.componentId, quantity: l.perBoard * preset.boards }));
      // The frontier is the answer; the other two only annotate it, so their failure
      // must never hide a solved frontier.
      const [frontier, calibration, provenance] = await Promise.all([
        sourcingApi.frontier(items),
        sourcingApi.calibration().catch(() => null),
        sourcingApi.catalogueProvenance().catch(() => null),
      ]);
      const points = [...frontier.frontier].sort((a, b) => a.lambda - b.lambda);
      setSolved({ preset, frontier, points, calibration, provenance });
      setIndex(0);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const names = useMemo(() => {
    const out = new Map<number, { name: string; p: number }>();
    for (const d of solved?.calibration?.distributors ?? []) {
      out.set(d.distributor_id, { name: d.distributor_name, p: d.p_disruption_over_horizon });
    }
    return out;
  }, [solved]);

  const points = solved?.points ?? [];
  const point = points.length ? points[Math.min(index, points.length - 1)] : null;
  const baseline = points[0] ?? null;
  const baselineIds = new Set(baseline?.supplier_ids ?? []);

  return (
    <Page
      title="Sourcing Risk"
      intro="Pick a bill of materials and a risk weight. A two-stage stochastic CP-SAT model decides which distributors to buy from, trading the expected cost of the order against its tail cost - the average cost of the worst 5% of disruption scenarios, after emergency re-buying from the distributors that survive."
    >
      <Card title="Bill of materials">
        <div className="flex flex-col sm:flex-row gap-3 sm:items-end">
          <div className="flex-1 min-w-0">
            <Field label="Reference BOM" htmlFor="bom">
              <select id="bom" className={inputClass} value={presetId} onChange={(e) => setPresetId(e.target.value)}>
                {PRESETS.map((p) => (
                  <option key={p.id} value={p.id}>
                    {`${p.name} · ${count(p.boards)} boards`}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <button className={buttonClass} disabled={busy} onClick={solve}>
            Solve the frontier
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left tabular-nums">
            <caption className="sr-only">Lines of the selected bill of materials</caption>
            <thead className="text-xs text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-2 pr-4 font-medium">Part</th>
                <th className="py-2 pr-4 font-medium">Per board</th>
                <th className="py-2 font-medium">Units ordered</th>
              </tr>
            </thead>
            <tbody>
              {preset.lines.map((l) => (
                <tr key={l.mpn} className="border-b border-slate-800/60 text-slate-300">
                  <td className="py-1.5 pr-4 font-mono">{l.mpn}</td>
                  <td className="py-1.5 pr-4">{l.perBoard}</td>
                  <td className="py-1.5">{count(l.perBoard * preset.boards)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {busy && <Busy what="Solving one CP-SAT model per risk weight…" />}
        {error && <ErrorBox title="The frontier could not be solved">{error}</ErrorBox>}
      </Card>

      {solved && point && baseline && !busy && (
        <Card title={`${solved.preset.name} · ${count(solved.preset.boards)} boards`}>
          {solved.frontier.partial && (
            <ErrorBox title="Part of the frontier is missing">
              {`${solved.frontier.solver.points_unsolved} of ${solved.frontier.solver.points_requested} risk weights ran out of search time and are not shown. That is a limit of this server's budget, not a finding that no plan exists.`}
            </ErrorBox>
          )}
          <Field
            label={`Risk weight: ${lam(point.lambda)}`}
            htmlFor="risk-weight"
            hint="Left: minimise the expected cost. Right: minimise the tail cost. Each stop is a separate CP-SAT solve."
          >
            <input
              id="risk-weight"
              type="range"
              min={0}
              max={points.length - 1}
              step={1}
              value={Math.min(index, points.length - 1)}
              onChange={(e) => setIndex(Number(e.target.value))}
              aria-valuetext={`Risk weight ${lam(point.lambda)}`}
              className="range-dark w-full h-11 cursor-pointer"
            />
          </Field>
          <div className="flex justify-between text-xs text-slate-400 -mt-2">
            <span>Cheapest on average</span>
            <span>Safest tail</span>
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <Stat
              label="Expected cost"
              value={usd(point.expected_cost_usd)}
              detail={point === baseline ? 'The lowest on this frontier.' : `${usd(point.expected_cost_usd - baseline.expected_cost_usd)} more than the cheapest plan.`}
            />
            <Stat
              label="Tail cost (CVaR)"
              value={usd(point.cvar_95_usd)}
              tone={point.cvar_95_usd < baseline.cvar_95_usd ? 'good' : 'neutral'}
              detail={point === baseline ? 'Average of the worst 5% of outcomes.' : `${usd(baseline.cvar_95_usd - point.cvar_95_usd)} less than the cheapest plan.`}
            />
            <Stat label="Distributors used" value={point.n_suppliers} detail={`${baseline.n_suppliers} in the cheapest plan.`} />
            <Stat label="Tail premium" value={usd(point.tail_premium_usd)} detail="Tail cost minus expected cost." />
          </div>

          <FrontierChart points={points} selected={point} />

          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-semibold text-slate-200">Distributors in the plan at this risk weight</h3>
            <ul className="flex flex-col gap-1.5 text-sm">
              {point.supplier_ids.map((id) => {
                const d = names.get(id);
                const isNew = !baselineIds.has(id);
                return (
                  <li key={id} className="flex flex-wrap items-baseline gap-x-2 text-slate-300">
                    <span className={isNew ? 'text-emerald-300 font-semibold' : 'text-white'}>
                      {d ? d.name : `Distributor ${id}`}
                    </span>
                    {d && <span className="text-xs text-slate-400">{`assumed failure chance ${pct(d.p)} per order window`}</span>}
                    {isNew && <span className="text-xs text-emerald-400">not in the cheapest plan</span>}
                  </li>
                );
              })}
            </ul>
          </div>

          {solved.frontier.recommendation.statement && (
            <p className="text-sm text-slate-300 leading-relaxed">{solved.frontier.recommendation.statement}</p>
          )}

          <div className="overflow-x-auto">
            <table className="w-full text-sm text-left tabular-nums">
              <caption className="sr-only">Every solved risk weight</caption>
              <thead className="text-xs text-slate-400 border-b border-slate-800">
                <tr>
                  <th className="py-2 pr-4 font-medium">Risk weight</th>
                  <th className="py-2 pr-4 font-medium">Expected cost</th>
                  <th className="py-2 pr-4 font-medium">Tail cost</th>
                  <th className="py-2 pr-4 font-medium">Distributors</th>
                  <th className="py-2 font-medium">Solver</th>
                </tr>
              </thead>
              <tbody>
                {points.map((p) => (
                  <tr
                    key={p.lambda}
                    className={`border-b border-slate-800/60 ${p === point ? 'text-emerald-300 font-semibold' : 'text-slate-300'}`}
                  >
                    <td className="py-1.5 pr-4">{lam(p.lambda)}</td>
                    <td className="py-1.5 pr-4">{usd(p.expected_cost_usd)}</td>
                    <td className="py-1.5 pr-4">{usd(p.cvar_95_usd)}</td>
                    <td className="py-1.5 pr-4">{p.n_suppliers}</td>
                    <td className="py-1.5">{p.solver_status.toLowerCase()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-sm text-slate-400 leading-relaxed">
            Routing deliveries is a second, separate model: the{' '}
            <Link to="/route-plan" className="text-blue-400 hover:text-blue-300 underline">
              Route Plan
            </Link>{' '}
            page plans vehicle routes with time windows and does not use the sourcing plan chosen here.
          </p>
        </Card>
      )}

      <Limits solved={solved} point={point} />
    </Page>
  );
}
