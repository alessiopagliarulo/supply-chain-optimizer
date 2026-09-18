import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Download, Upload } from 'lucide-react';
import {
  errorMessage,
  routingApi,
  type InstanceList,
  type PlanInstance,
  type SolveResponse,
  type SolverMethod,
} from '../services/api';
import { usePlanStore } from '../store/planStore';
import { CSV_COLUMNS, CSV_COLUMN_HELP, parseCustomerCsv, toCustomerCsv } from '../lib/customerCsv';
import RoutePlot, { RouteLegend } from '../components/RoutePlot';
import {
  Busy,
  Card,
  ErrorBox,
  Field,
  Page,
  Stat,
  buttonClass,
  inputClass,
  secondaryButtonClass,
} from '../components/ui';

const METHODS: { value: SolverMethod; label: string; help: string }[] = [
  { value: 'auto', label: 'Auto', help: 'The exact CP-SAT model on small instances, OR-Tools routing above that.' },
  { value: 'cpsat', label: 'CP-SAT (exact)', help: 'Proves optimality when it finishes inside the time limit.' },
  { value: 'ortools', label: 'OR-Tools routing', help: 'Guided local search; uses the whole time limit.' },
  { value: 'clarke_wright', label: 'Clarke-Wright savings', help: 'One fast greedy pass; ignores the time limit.' },
];

const STATUS_TEXT: Record<SolveResponse['status'], string> = {
  optimal: 'Proven optimal',
  feasible: 'Not proven optimal',
  infeasible: 'The validator rejected the routes',
  no_solution: 'The solver returned no routes',
};

interface LoadedInstance extends PlanInstance {
  label: string;
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export default function RoutePlanPage() {
  const { plan, setPlan } = usePlanStore();

  const [catalog, setCatalog] = useState<InstanceList | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const [mode, setMode] = useState<'instance' | 'csv'>('instance');
  const [instanceId, setInstanceId] = useState('');
  const [loaded, setLoaded] = useState<LoadedInstance | null>(
    plan ? { ...plan.instance, label: plan.label } : null,
  );
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadingInstance, setLoadingInstance] = useState(false);
  const [csvErrors, setCsvErrors] = useState<string[]>([]);

  const [method, setMethod] = useState<SolverMethod>(plan ? (plan.solution.method as SolverMethod) : 'auto');
  const [timeLimit, setTimeLimit] = useState('2');
  const [solving, setSolving] = useState(false);
  const [solveError, setSolveError] = useState<string | null>(null);
  const [solution, setSolution] = useState<SolveResponse | null>(plan?.solution ?? null);

  const limits = catalog?.limits;
  const customers = loaded ? loaded.nodes.length - 1 : 0;

  // Only the latest pick may land: a slow response for an earlier choice is dropped.
  const latestPick = useRef(0);
  // A solve lands only if the instance and fleet it was run on are still the ones loaded.
  const latestSolve = useRef(0);
  const loadInstance = useCallback((id: string) => {
    const pick = ++latestPick.current;
    latestSolve.current += 1;
    setInstanceId(id);
    setLoadingInstance(true);
    setLoadError(null);
    routingApi
      .getInstance(id)
      .then((inst) => {
        if (pick !== latestPick.current) return;
        setLoaded({
          label: inst.name,
          nodes: inst.nodes,
          num_vehicles: inst.num_vehicles,
          vehicle_capacity: inst.vehicle_capacity,
        });
        setSolution(null);
      })
      .catch((err: unknown) => pick === latestPick.current && setLoadError(errorMessage(err)))
      .finally(() => pick === latestPick.current && setLoadingInstance(false));
  }, []);

  // A plan carried over from earlier stays on screen; otherwise load the first instance.
  const [startedWithPlan] = useState(plan !== null);
  useEffect(() => {
    let live = true;
    routingApi
      .listInstances()
      .then((data) => {
        if (!live) return;
        setCatalog(data);
        if (!startedWithPlan && data.instances.length) loadInstance(data.instances[0].id);
      })
      .catch((err: unknown) => live && setCatalogError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, [startedWithPlan, loadInstance]);

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file || !limits) return;
    const parsed = parseCustomerCsv(await file.text(), limits.max_customers);
    if (!parsed.ok) {
      setCsvErrors(parsed.errors);
      return;
    }
    setCsvErrors([]);
    latestPick.current += 1;
    latestSolve.current += 1;
    setInstanceId('');
    const count = parsed.nodes.length - 1;
    const totalDemand = parsed.nodes.reduce((s, n) => s + n.demand, 0);
    setLoaded({
      label: file.name,
      nodes: parsed.nodes,
      num_vehicles: count,
      vehicle_capacity: Math.max(1, totalDemand),
    });
    setSolution(null);
  };

  const setFleet = (field: 'num_vehicles' | 'vehicle_capacity', raw: string) => {
    if (!loaded) return;
    latestSolve.current += 1;
    const value = Math.max(0, Math.floor(Number(raw) || 0));
    setLoaded({ ...loaded, [field]: value });
    setSolution(null);
  };

  const timeLimitValue = Number(timeLimit);
  const timeLimitOk = limits
    ? Number.isFinite(timeLimitValue) && timeLimitValue > 0 && timeLimitValue <= limits.max_time_limit_seconds
    : false;
  const exactTooBig = limits ? method === 'cpsat' && customers > limits.max_exact_customers : false;
  const canSolve = !!loaded && !!limits && timeLimitOk && !exactTooBig && loaded.num_vehicles >= 1 && !solving;

  const solve = async () => {
    if (!loaded) return;
    const run = ++latestSolve.current;
    setSolving(true);
    setSolveError(null);
    const instance: PlanInstance = {
      nodes: loaded.nodes,
      num_vehicles: loaded.num_vehicles,
      vehicle_capacity: loaded.vehicle_capacity,
    };
    try {
      const result = await routingApi.solve({ ...instance, method, time_limit_seconds: timeLimitValue });
      if (run !== latestSolve.current) return;
      setSolution(result);
      setPlan({ label: loaded.label, instance, solution: result });
    } catch (err) {
      if (run === latestSolve.current) setSolveError(errorMessage(err));
    } finally {
      setSolving(false);
    }
  };

  const methodHelp = METHODS.find((m) => m.value === method)?.help;

  return (
    <Page
      title="Route Plan"
      intro="Pick a built-in instance or upload your own customers, choose a solver, and get a capacitated vehicle routing plan that respects every time window. Every plan is checked by the shared validator, not taken on the solver's word."
    >
      {catalogError && (
        <ErrorBox title="Could not load the instance list">{catalogError}</ErrorBox>
      )}
      {!catalog && !catalogError && <Busy what="Loading instances…" />}

      {catalog && limits && (
        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] gap-6 items-start">
          <Card title="Instance and solver">
            <div role="radiogroup" aria-label="Instance source" className="grid grid-cols-2 gap-2">
              {(['instance', 'csv'] as const).map((m) => (
                <button
                  key={m}
                  role="radio"
                  aria-checked={mode === m}
                  onClick={() => setMode(m)}
                  className={`min-h-[44px] rounded-lg text-sm font-medium border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 ${
                    mode === m
                      ? 'bg-blue-600 border-blue-500 text-white'
                      : 'border-slate-700 text-slate-300 hover:bg-slate-800'
                  }`}
                >
                  {m === 'instance' ? 'Built-in instance' : 'Upload CSV'}
                </button>
              ))}
            </div>

            {mode === 'instance' ? (
              <Field
                label="Instance"
                htmlFor="instance"
                hint="Built-in samples are real Solomon benchmark instances. Solomon benchmark files appear here too once they are in the repository."
              >
                <select id="instance" className={inputClass} value={instanceId} onChange={(e) => loadInstance(e.target.value)}>
                  {instanceId === '' && (
                    <option value="" disabled>
                      {loaded ? `Current plan: ${loaded.label}` : 'Choose an instance'}
                    </option>
                  )}
                  {catalog.instances.map((inst) => (
                    <option key={inst.id} value={inst.id}>
                      {`${inst.name} · ${inst.num_customers} customers · ${inst.source}`}
                    </option>
                  ))}
                </select>
              </Field>
            ) : (
              <div className="flex flex-col gap-3">
                <label className={`${secondaryButtonClass} cursor-pointer`}>
                  <Upload className="w-4 h-4" aria-hidden="true" />
                  Choose a CSV file
                  <input type="file" accept=".csv,text/csv" className="sr-only" onChange={onFile} />
                </label>
                <div className="text-xs text-slate-400 leading-relaxed flex flex-col gap-1.5">
                  <p>
                    A header row, then one row per stop. The first row is the depot; its window is the
                    planning horizon. Time windows apply to when service starts. Columns:
                  </p>
                  <ul className="flex flex-col gap-1">
                    {CSV_COLUMNS.map((c) => (
                      <li key={c}>
                        <code className="text-slate-200">{c}</code>
                        {` - ${CSV_COLUMN_HELP[c]}`}
                      </li>
                    ))}
                  </ul>
                  <p>{`At most ${limits.max_customers} customers. Other columns are ignored.`}</p>
                </div>
                {csvErrors.length > 0 && (
                  <ErrorBox title="That file can't be used">
                    <ul className="list-disc pl-4 flex flex-col gap-0.5">
                      {csvErrors.map((e) => (
                        <li key={e}>{e}</li>
                      ))}
                    </ul>
                  </ErrorBox>
                )}
              </div>
            )}

            {loadingInstance && <Busy what="Loading instance…" />}
            {loadError && <ErrorBox title="Could not load that instance">{loadError}</ErrorBox>}

            {loaded && (
              <div className="grid grid-cols-2 gap-3">
                <Field label="Vehicles" htmlFor="vehicles">
                  <input
                    id="vehicles"
                    type="number"
                    min={1}
                    className={inputClass}
                    value={loaded.num_vehicles}
                    onChange={(e) => setFleet('num_vehicles', e.target.value)}
                  />
                </Field>
                <Field label="Capacity each" htmlFor="capacity">
                  <input
                    id="capacity"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={loaded.vehicle_capacity}
                    onChange={(e) => setFleet('vehicle_capacity', e.target.value)}
                  />
                </Field>
              </div>
            )}

            <Field label="Solver" htmlFor="method" hint={methodHelp}>
              <select id="method" className={inputClass} value={method} onChange={(e) => setMethod(e.target.value as SolverMethod)}>
                {METHODS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </Field>
            {exactTooBig && (
              <p className="text-xs text-amber-300">
                {`The exact model is capped at ${limits.max_exact_customers} customers; this instance has ${customers}.`}
              </p>
            )}

            <Field label="Time limit (seconds)" htmlFor="time-limit" hint={`Up to ${limits.max_time_limit_seconds} seconds.`}>
              <input
                id="time-limit"
                type="number"
                min={0.1}
                step={0.5}
                max={limits.max_time_limit_seconds}
                className={inputClass}
                value={timeLimit}
                onChange={(e) => setTimeLimit(e.target.value)}
              />
            </Field>

            <button className={buttonClass} disabled={!canSolve} onClick={solve}>
              Solve
            </button>
            {loaded && (
              <button
                className={secondaryButtonClass}
                onClick={() => download(`${loaded.label.replace(/\.csv$/i, '')}.csv`, toCustomerCsv(loaded.nodes))}
              >
                <Download className="w-4 h-4" aria-hidden="true" />
                Download this instance as CSV
              </button>
            )}
          </Card>

          <Card title={loaded ? `Plan for ${loaded.label}` : 'Plan'}>
            {solving && <Busy what="Solving…" />}
            {solveError && <ErrorBox title="The solve failed">{solveError}</ErrorBox>}
            {!solving && !solution && loaded && (
              <p className="text-sm text-slate-400">
                {`${customers} customers loaded. Choose a solver and press Solve to draw the routes.`}
              </p>
            )}
            {!solving && solution && loaded && (
              <>
                <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
                  <Stat label="Total distance" value={solution.total_cost} />
                  <Stat
                    label="Vehicles used"
                    value={solution.vehicles_used}
                    detail={`of ${loaded.num_vehicles} available`}
                  />
                  <Stat
                    label="Feasibility"
                    value={solution.feasible ? 'Feasible' : 'Not feasible'}
                    tone={solution.feasible ? 'good' : 'bad'}
                    detail={STATUS_TEXT[solution.status]}
                  />
                  <Stat
                    label="Solver runtime"
                    value={`${solution.wall_seconds.toFixed(2)} s`}
                    detail={`method: ${solution.method}`}
                  />
                </div>
                {solution.validation.violations.length > 0 && (
                  <ErrorBox title="The validator rejected this plan">
                    <ul className="list-disc pl-4 flex flex-col gap-0.5">
                      {solution.validation.violations.slice(0, 10).map((v) => (
                        <li key={v}>{v}</li>
                      ))}
                    </ul>
                  </ErrorBox>
                )}
                <RoutePlot
                  nodes={loaded.nodes}
                  routes={solution.routes}
                  label={`Route plan for ${loaded.label}: ${solution.vehicles_used} routes from the depot over ${customers} customers`}
                />
                <RouteLegend reports={solution.validation.routes} capacity={loaded.vehicle_capacity} />
                {solution.routes.length > 0 && (
                  <Link to="/simulation" className={`${buttonClass} self-start`}>
                    Simulate this plan
                    <ArrowRight className="w-4 h-4" aria-hidden="true" />
                  </Link>
                )}
              </>
            )}
            {!loaded && !loadingInstance && (
              <p className="text-sm text-slate-400">Pick an instance or upload a CSV to start.</p>
            )}
          </Card>
        </div>
      )}
    </Page>
  );
}
