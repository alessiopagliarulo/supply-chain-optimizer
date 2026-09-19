import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  errorMessage,
  routingApi,
  type BufferCandidate,
  type Distribution,
  type InstanceSummary,
  type RoutingLimits,
  type SimulateResponse,
  type SolverMethod,
  type TuneBuffersResponse,
  type TuningObjective,
} from '../services/api';
import { usePlanStore } from '../store/planStore';
import { Busy, Card, ErrorBox, Field, Page, Stat, buttonClass, inputClass, secondaryButtonClass } from '../components/ui';

const pct = (v: number, digits = 1) => `${(v * 100).toFixed(digits)}%`;
const num = (v: number, digits = 1) => v.toFixed(digits);

/** A number input's value, or null when it is not a number inside [lo, hi]. */
function bounded(raw: string, lo: number, hi: number, integer = false): number | null {
  const v = Number(raw);
  if (raw.trim() === '' || !Number.isFinite(v) || v < lo || v > hi) return null;
  if (integer && !Number.isInteger(v)) return null;
  return v;
}

function SamplePlanLoader() {
  const setPlan = usePlanStore((s) => s.setPlan);
  const [instances, setInstances] = useState<InstanceSummary[] | null>(null);
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    routingApi
      .listInstances()
      .then((data) => {
        if (!live) return;
        setInstances(data.instances);
        if (data.instances.length) setChoice(data.instances[0].id);
      })
      .catch((err: unknown) => live && setError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, []);

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      const inst = await routingApi.getInstance(choice);
      const instance = { nodes: inst.nodes, num_vehicles: inst.num_vehicles, vehicle_capacity: inst.vehicle_capacity };
      const solution = await routingApi.solve({ ...instance, method: 'auto', time_limit_seconds: 2 });
      setPlan({ label: inst.name, instance, solution });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="No plan yet">
      <p className="text-sm text-slate-400 leading-relaxed">
        Solve a Solomon test case on the <Link to="/route-plan?source=solomon" className="text-blue-400 hover:text-blue-300 underline">Route Plan</Link>{' '}
        page, or solve a built-in sample here with the automatic solver.
      </p>
      {instances && (
        <div className="flex flex-col sm:flex-row gap-3 sm:items-end">
          <div className="flex-1 min-w-0">
            <Field label="Sample" htmlFor="sample">
              <select id="sample" className={inputClass} value={choice} onChange={(e) => setChoice(e.target.value)}>
                {instances.map((i) => (
                  <option key={i.id} value={i.id}>
                    {`${i.name} · ${i.num_customers} customers`}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <button className={buttonClass} disabled={busy || !choice} onClick={load}>
            Solve and use this sample
          </button>
        </div>
      )}
      {!instances && !error && <Busy what="Loading samples…" />}
      {busy && <Busy what="Solving the sample…" />}
      {error && <ErrorBox title="Could not load a sample plan">{error}</ErrorBox>}
    </Card>
  );
}

interface FrontierPoint extends BufferCandidate {
  on_time_pct: number;
}

function FrontierTooltip({ active, payload }: { active?: boolean; payload?: { payload: FrontierPoint }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-200 flex flex-col gap-0.5">
      <span className="font-semibold">{`Schedule buffer ${p.schedule_buffer_pct}% · capacity buffer ${p.capacity_buffer_pct}%`}</span>
      <span>{`On time ${pct(p.on_time_rate)} · distance ${p.total_distance} · vehicles ${p.vehicles_used}`}</span>
      <span>{`Mean lateness ${num(p.mean_lateness)} · p95 lateness ${num(p.p95_lateness)}`}</span>
    </div>
  );
}

function FrontierChart({ result }: { result: TuneBuffersResponse }) {
  const same = (a: BufferCandidate, b: BufferCandidate) =>
    a.schedule_buffer_pct === b.schedule_buffer_pct && a.capacity_buffer_pct === b.capacity_buffer_pct;
  const toPoint = (c: BufferCandidate): FrontierPoint => ({ ...c, on_time_pct: c.on_time_rate * 100 });
  const others = result.frontier.filter((c) => !same(c, result.chosen_buffer)).map(toPoint);
  const chosen = [toPoint(result.chosen_buffer)];
  // Zoom the y axis to the evaluated range, on round ticks, but always up to 100%.
  const lowest = Math.min(...result.frontier.map((c) => c.on_time_rate * 100), result.target_on_time_rate * 100);
  const step = 100 - lowest > 50 ? 25 : 100 - lowest > 20 ? 10 : 5;
  const yMin = Math.max(0, Math.floor((lowest - step / 2) / step) * step);
  const yTicks = Array.from({ length: Math.round((100 - yMin) / step) + 1 }, (_, i) => yMin + i * step);

  return (
    <div className="h-80 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 16, bottom: 28, left: 8 }}>
          <CartesianGrid stroke="#1e293b" />
          <XAxis
            type="number"
            dataKey="total_distance"
            name="Total distance"
            domain={['auto', 'auto']}
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            label={{ value: 'Total distance of the buffered plan', position: 'insideBottom', offset: -16, fill: '#94a3b8', fontSize: 12 }}
          />
          <YAxis
            type="number"
            dataKey="on_time_pct"
            name="On-time rate"
            domain={[yMin, 100]}
            ticks={yTicks}
            unit="%"
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            width={52}
          />
          <Tooltip content={<FrontierTooltip />} cursor={{ stroke: '#475569' }} />
          <Legend verticalAlign="top" height={32} wrapperStyle={{ fontSize: 12, color: '#cbd5e1' }} />
          <ReferenceLine
            y={result.target_on_time_rate * 100}
            stroke="#f59e0b"
            strokeDasharray="4 4"
            label={{ value: 'target', position: 'insideTopRight', fill: '#fbbf24', fontSize: 12 }}
          />
          <Scatter name="Evaluated buffers" data={others} fill="#60a5fa" />
          <Scatter name="Chosen buffer" data={chosen} fill="#34d399" shape="diamond" legendType="diamond" />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * The Digital Twin's "coming next" state. The twin itself (a live replica of the real
 * distributor network driven by forecasts) is a later task; until it lands this page
 * says so plainly and runs the part that already works - the discrete-event
 * simulation and buffer tuning of a solved Route Plan.
 */
function ComingNext() {
  return (
    <section
      aria-labelledby="twin-next"
      className="rounded-xl border border-blue-500/40 bg-gradient-to-br from-blue-950/60 to-slate-900 p-4 sm:p-5 flex flex-col gap-3"
    >
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-blue-200 bg-blue-500/20 border border-blue-400/40 rounded-full px-2.5 py-0.5">
          Coming next
        </span>
      </div>
      <h2 id="twin-next" className="text-lg font-semibold text-white">
        A digital twin of the distributor network
      </h2>
      <p className="text-sm text-slate-300 leading-relaxed max-w-3xl">
        The next step replays the real distributor network from the Map page: forecast demand for the real
        components, turn it into route plans on the real locations, and simulate those plans day by day to see where
        they break. It is not built yet. What works today is below: stress-testing one solved Solomon route plan
        with random travel and service times, and tuning the buffers that keep it on time.
      </p>
      <div className="flex flex-wrap gap-2">
        <Link to="/map" className={secondaryButtonClass}>
          Explore the distributor map
        </Link>
        <Link to="/route-plan" className={secondaryButtonClass}>
          Plan routes on real places
        </Link>
      </div>
    </section>
  );
}

export default function DigitalTwinPage() {
  const plan = usePlanStore((s) => s.plan);
  const [limits, setLimits] = useState<RoutingLimits | null>(null);

  const [variability, setVariability] = useState('0.2');
  const [distribution, setDistribution] = useState<Distribution>('lognormal');
  const [replications, setReplications] = useState('200');
  const [seed, setSeed] = useState('42');
  const [simBusy, setSimBusy] = useState(false);
  const [simError, setSimError] = useState<string | null>(null);
  const [sim, setSim] = useState<SimulateResponse | null>(null);

  const [target, setTarget] = useState('95');
  const [objective, setObjective] = useState<TuningObjective>('minimize_cost_for_target');
  const [schedMax, setSchedMax] = useState('20');
  const [schedStep, setSchedStep] = useState('5');
  const [capMax, setCapMax] = useState('10');
  const [capStep, setCapStep] = useState('5');
  const [tuneReps, setTuneReps] = useState('50');
  const [tuneMethod, setTuneMethod] = useState<SolverMethod>('clarke_wright');
  const [tuneBusy, setTuneBusy] = useState(false);
  const [tuneError, setTuneError] = useState<string | null>(null);
  const [tune, setTune] = useState<TuneBuffersResponse | null>(null);

  useEffect(() => {
    let live = true;
    routingApi
      .listInstances()
      .then((data) => live && setLimits(data.limits))
      .catch(() => undefined); // the calls below report their own errors
    return () => {
      live = false;
    };
  }, []);

  // Results belong to the plan they were run on; a different plan clears them.
  // Adjusting state during render is React's recommended way to reset on a change.
  const [resultsFor, setResultsFor] = useState(plan);
  if (resultsFor !== plan) {
    setResultsFor(plan);
    setSim(null);
    setTune(null);
    setSimError(null);
    setTuneError(null);
  }

  const maxReps = limits?.max_replications ?? 1;
  const v = bounded(variability, 0, 2);
  const reps = bounded(replications, 1, maxReps, true);
  const seedValue = bounded(seed, -(2 ** 31), 2 ** 31 - 1, true);
  const customers = plan ? plan.instance.nodes.length - 1 : 0;

  const runSimulation = async () => {
    if (!plan || v === null || reps === null || seedValue === null) return;
    setSimBusy(true);
    setSimError(null);
    try {
      setSim(
        await routingApi.simulate({
          ...plan.instance,
          routes: plan.solution.routes,
          num_replications: reps,
          variability: v,
          distribution,
          seed: seedValue,
        }),
      );
    } catch (err) {
      setSimError(errorMessage(err));
    } finally {
      setSimBusy(false);
    }
  };

  const targetValue = bounded(target, 0, 100);
  const sMax = bounded(schedMax, 0, 100);
  const sStep = bounded(schedStep, 0.1, 100);
  const cMax = bounded(capMax, 0, 50);
  const cStep = bounded(capStep, 0.1, 50);
  const tReps = bounded(tuneReps, 1, maxReps, true);
  const tooBigToTune = limits ? customers > limits.max_tuning_customers : false;
  const tuneReady =
    !!plan && !!limits && !tooBigToTune && v !== null && seedValue !== null && targetValue !== null &&
    sMax !== null && sStep !== null && cMax !== null && cStep !== null && tReps !== null;

  const runTuning = async () => {
    if (!plan || !tuneReady || !limits) return;
    setTuneBusy(true);
    setTuneError(null);
    try {
      setTune(
        await routingApi.tuneBuffers({
          ...plan.instance,
          schedule_buffer_max: sMax,
          schedule_buffer_step: sStep,
          capacity_buffer_max: cMax,
          capacity_buffer_step: cStep,
          num_replications: tReps,
          variability: v,
          distribution,
          target_on_time_rate: targetValue / 100,
          objective,
          base_seed: seedValue,
          method: tuneMethod,
          time_limit_seconds: Math.min(1, limits.max_time_limit_seconds),
        }),
      );
    } catch (err) {
      setTuneError(errorMessage(err));
    } finally {
      setTuneBusy(false);
    }
  };

  const late = sim ? sim.max_route_completion_time > sim.depot_close : false;
  const lateOnAverage = sim ? sim.mean_route_completion_time > sim.depot_close : false;

  return (
    <Page
      title="Digital Twin"
      intro="A simulated replica of the logistics network, for testing plans before they meet the real world. The full twin is coming next; today this page stress-tests a solved route plan in a discrete-event simulation."
    >
      <ComingNext />
      <h2 className="text-base font-semibold text-slate-100 -mb-2">Available now: simulate a route plan</h2>
      {!plan && <SamplePlanLoader />}

      {plan && (
        <>
          <Card title="Plan under test">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <p className="text-sm text-slate-300">
                <span className="font-semibold text-white">{plan.label}</span>
                {` · ${customers} customers · ${plan.solution.vehicles_used} routes · distance ${plan.solution.total_cost} · solved by ${plan.solution.method} · ${plan.solution.feasible ? 'feasible' : 'not feasible'}`}
              </p>
              <Link to="/route-plan?source=solomon" className={secondaryButtonClass}>
                Change plan
              </Link>
            </div>
          </Card>

          <Card title="Run the simulation">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              <Field label="Variability" htmlFor="variability" hint="Coefficient of variation (lognormal) or relative range (triangular); none reproduces the plan.">
                <input id="variability" type="number" min={0} max={2} step={0.05} className={inputClass} value={variability} onChange={(e) => setVariability(e.target.value)} />
              </Field>
              <Field label="Distribution" htmlFor="distribution">
                <select id="distribution" className={inputClass} value={distribution} onChange={(e) => setDistribution(e.target.value as Distribution)}>
                  <option value="lognormal">Lognormal</option>
                  <option value="triangular">Triangular</option>
                </select>
              </Field>
              <Field label="Replications" htmlFor="replications" hint={limits ? `Up to ${limits.max_replications}.` : undefined}>
                <input id="replications" type="number" min={1} max={maxReps} step={1} className={inputClass} value={replications} onChange={(e) => setReplications(e.target.value)} />
              </Field>
              <Field label="Seed" htmlFor="seed" hint="Same seed, same result.">
                <input id="seed" type="number" step={1} className={inputClass} value={seed} onChange={(e) => setSeed(e.target.value)} />
              </Field>
            </div>
            <button className={`${buttonClass} self-start`} disabled={simBusy || !limits || v === null || reps === null || seedValue === null} onClick={runSimulation}>
              Run simulation
            </button>
            {simBusy && <Busy what="Simulating…" />}
            {simError && <ErrorBox title="The simulation failed">{simError}</ErrorBox>}
            {sim && !simBusy && (
              <>
                {!sim.plan_feasible && (
                  <ErrorBox title="This plan is not feasible even without variability">
                    {sim.plan_violations.slice(0, 5).join('; ')}
                  </ErrorBox>
                )}
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                  <Stat label="On-time rate" value={pct(sim.on_time_rate)} detail="Stops whose service starts by their due time, over all replications." />
                  <Stat label="Mean lateness" value={num(sim.mean_lateness)} detail="Per stop, in the instance's time units." />
                  <Stat label="Lateness p95" value={num(sim.p95_lateness)} detail="Per stop, the percentile across replications." />
                  <Stat
                    label="Completion vs depot close"
                    value={`${num(sim.mean_route_completion_time)} / ${sim.depot_close}`}
                    tone={lateOnAverage ? 'bad' : 'good'}
                    detail={`Mean return to the depot; latest ${num(sim.max_route_completion_time)}${late ? ', after closing in some replications' : ''}.`}
                  />
                  <Stat label="Utilization (mean)" value={pct(sim.vehicle_utilization_mean)} detail="Service time over time away from the depot." />
                  <Stat label="Utilization (lowest route)" value={pct(sim.vehicle_utilization_min)} />
                  <Stat label="Replications" value={sim.num_replications} detail={`${sim.distribution}, variability ${sim.variability}, seed ${sim.seed}`} />
                </div>
              </>
            )}
          </Card>

          <Card title="Tune buffers">
            <p className="text-sm text-slate-400 leading-relaxed">
              Each candidate re-solves the instance with travel and service times inflated by a schedule buffer and
              vehicle capacity reduced by a capacity buffer, then simulates the new plan with the variability,
              distribution and seed above. All candidates share the same random numbers, so they are compared fairly.
            </p>
            {tooBigToTune && limits && (
              <ErrorBox title="Too many customers to tune here">
                {`Buffer tuning is capped at ${limits.max_tuning_customers} customers; this plan has ${customers}.`}
              </ErrorBox>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              <Field label="Objective" htmlFor="objective">
                <select id="objective" className={inputClass} value={objective} onChange={(e) => setObjective(e.target.value as TuningObjective)}>
                  <option value="minimize_cost_for_target">Shortest plan meeting the target</option>
                  <option value="maximize_on_time">Highest on-time rate</option>
                </select>
              </Field>
              <Field label="Target on-time rate (%)" htmlFor="target">
                <input id="target" type="number" min={0} max={100} step={1} className={inputClass} value={target} onChange={(e) => setTarget(e.target.value)} />
              </Field>
              <Field label="Solver per candidate" htmlFor="tune-method" hint="Clarke-Wright is fastest; the others spend their time limit on every candidate.">
                <select id="tune-method" className={inputClass} value={tuneMethod} onChange={(e) => setTuneMethod(e.target.value as SolverMethod)}>
                  <option value="clarke_wright">Clarke-Wright savings</option>
                  <option value="auto">Auto</option>
                  <option value="ortools">OR-Tools routing</option>
                  <option value="cpsat">CP-SAT (exact)</option>
                </select>
              </Field>
              <Field label="Replications per candidate" htmlFor="tune-reps">
                <input id="tune-reps" type="number" min={1} max={maxReps} step={1} className={inputClass} value={tuneReps} onChange={(e) => setTuneReps(e.target.value)} />
              </Field>
              <Field label="Schedule buffer up to (%)" htmlFor="sched-max">
                <input id="sched-max" type="number" min={0} max={100} className={inputClass} value={schedMax} onChange={(e) => setSchedMax(e.target.value)} />
              </Field>
              <Field label="Schedule buffer step (%)" htmlFor="sched-step">
                <input id="sched-step" type="number" min={0.1} max={100} className={inputClass} value={schedStep} onChange={(e) => setSchedStep(e.target.value)} />
              </Field>
              <Field label="Capacity buffer up to (%)" htmlFor="cap-max">
                <input id="cap-max" type="number" min={0} max={50} className={inputClass} value={capMax} onChange={(e) => setCapMax(e.target.value)} />
              </Field>
              <Field label="Capacity buffer step (%)" htmlFor="cap-step">
                <input id="cap-step" type="number" min={0.1} max={50} className={inputClass} value={capStep} onChange={(e) => setCapStep(e.target.value)} />
              </Field>
            </div>
            <button className={`${buttonClass} self-start`} disabled={tuneBusy || !tuneReady} onClick={runTuning}>
              Tune buffers
            </button>
            {tuneBusy && <Busy what="Solving and simulating every candidate…" />}
            {tuneError && <ErrorBox title="Buffer tuning failed">{tuneError}</ErrorBox>}
            {tune && !tuneBusy && (
              <>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                  <Stat
                    label="Chosen buffers"
                    value={`${tune.chosen_buffer.schedule_buffer_pct}% / ${tune.chosen_buffer.capacity_buffer_pct}%`}
                    detail="Schedule / capacity."
                  />
                  <Stat
                    label="On-time rate"
                    value={pct(tune.chosen_buffer.on_time_rate)}
                    tone={tune.chosen_buffer.on_time_rate >= tune.target_on_time_rate ? 'good' : 'bad'}
                    detail={`Target ${pct(tune.target_on_time_rate, 0)}.`}
                  />
                  <Stat label="Total distance" value={tune.chosen_buffer.total_distance} detail={`${tune.chosen_buffer.vehicles_used} vehicles.`} />
                  <Stat label="Lateness p95" value={num(tune.chosen_buffer.p95_lateness)} detail={`Mean ${num(tune.chosen_buffer.mean_lateness)}.`} />
                </div>
                <FrontierChart result={tune} />
                <div className="overflow-x-auto">
                  <table className="w-full text-sm text-left tabular-nums">
                    <caption className="sr-only">Every feasible buffer candidate</caption>
                    <thead className="text-xs text-slate-400 border-b border-slate-800">
                      <tr>
                        <th className="py-2 pr-4 font-medium">Schedule buffer</th>
                        <th className="py-2 pr-4 font-medium">Capacity buffer</th>
                        <th className="py-2 pr-4 font-medium">On time</th>
                        <th className="py-2 pr-4 font-medium">Mean lateness</th>
                        <th className="py-2 pr-4 font-medium">Lateness p95</th>
                        <th className="py-2 pr-4 font-medium">Distance</th>
                        <th className="py-2 font-medium">Vehicles</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tune.frontier.map((c) => {
                        const isChosen =
                          c.schedule_buffer_pct === tune.chosen_buffer.schedule_buffer_pct &&
                          c.capacity_buffer_pct === tune.chosen_buffer.capacity_buffer_pct;
                        return (
                          <tr key={`${c.schedule_buffer_pct}-${c.capacity_buffer_pct}`} className={`border-b border-slate-800/60 ${isChosen ? 'text-emerald-300 font-semibold' : 'text-slate-300'}`}>
                            <td className="py-1.5 pr-4">{`${c.schedule_buffer_pct}%`}</td>
                            <td className="py-1.5 pr-4">{`${c.capacity_buffer_pct}%`}</td>
                            <td className="py-1.5 pr-4">{pct(c.on_time_rate)}</td>
                            <td className="py-1.5 pr-4">{num(c.mean_lateness)}</td>
                            <td className="py-1.5 pr-4">{num(c.p95_lateness)}</td>
                            <td className="py-1.5 pr-4">{c.total_distance}</td>
                            <td className="py-1.5">{c.vehicles_used}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="text-xs text-slate-400">
                  Candidates whose buffered plan the solver could not make feasible are left out.
                </p>
              </>
            )}
          </Card>
        </>
      )}
    </Page>
  );
}
