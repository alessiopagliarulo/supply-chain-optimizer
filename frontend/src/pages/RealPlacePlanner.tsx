/**
 * Route Plan, "Real places" tab: a depot and destinations picked from the real
 * distributor catalogue, planned by the CVRPTW engine on great-circle distance times
 * the road factor, and drawn on an OpenStreetMap map.
 *
 * Everything that is not a location - loads, trucks, speed, hours - is an EXAMPLE
 * scenario, labelled as such wherever it appears (docs/REAL_PLACE_ROUTING.md).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ChevronDown, FlaskConical, Info, Route as RouteIcon } from 'lucide-react';
import type { LatLngTuple } from 'leaflet';
import BaseMap, { FitBounds, FlyTo } from '../components/map/BaseMap';
import SiteLayer, { type SiteTone } from '../components/map/SiteLayer';
import RouteLayer from '../components/map/RouteLayer';
import SnapshotNotice from '../components/SnapshotNotice';
import { Busy, Card, ErrorBox, Field, Stat, buttonClass, inputClass, secondaryButtonClass } from '../components/ui';
import { useCatalogue } from '../services/catalogue';
import {
  errorMessage,
  placesApi,
  type LocatedDistributor,
  type PlacesModel,
  type PlacesScenario,
  type PlacesSolveResponse,
  type SolverMethod,
} from '../services/api';
import { groupSites, placeLabel, regionOf, siteKey, type Site } from '../lib/sites';
import { reachable, suggestStops } from '../lib/geo';
import { mapRouteColor } from '../lib/colors';

const METHODS: { value: SolverMethod; label: string }[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'cpsat', label: 'CP-SAT (exact)' },
  { value: 'ortools', label: 'OR-Tools routing' },
  { value: 'clarke_wright', label: 'Clarke-Wright savings' },
];

const STATUS_TEXT: Record<PlacesSolveResponse['status'], string> = {
  optimal: 'Proven optimal',
  feasible: 'Not proven optimal',
  infeasible: 'The validator rejected the routes',
  no_solution: 'The solver found no routes',
};

type ScenarioForm = Record<keyof PlacesScenario, string>;

const SCENARIO_FIELDS: { key: keyof PlacesScenario; label: string; unit: string; step: string }[] = [
  { key: 'num_vehicles', label: 'Trucks', unit: 'trucks', step: '1' },
  { key: 'vehicle_capacity', label: 'Truck capacity', unit: 'pallets', step: '1' },
  { key: 'stop_load', label: 'Load per stop', unit: 'pallets', step: '1' },
  { key: 'speed_kmh', label: 'Average speed', unit: 'km/h', step: '5' },
  { key: 'service_minutes', label: 'Time at each stop', unit: 'minutes', step: '5' },
  { key: 'max_route_hours', label: 'Longest route', unit: 'hours', step: '1' },
];

const toForm = (s: PlacesScenario): ScenarioForm =>
  Object.fromEntries(Object.entries(s).map(([k, v]) => [k, String(v)])) as ScenarioForm;

function parseScenario(f: ScenarioForm): PlacesScenario | null {
  const out = {} as PlacesScenario;
  for (const { key } of SCENARIO_FIELDS) {
    const v = Number(f[key]);
    if (f[key].trim() === '' || !Number.isFinite(v) || v < 0) return null;
    out[key] = v;
  }
  return out;
}

const km = (v: number) => `${Math.round(v).toLocaleString()} km`;
const hours = (v: number) => `${v.toFixed(1)} h`;

/** The legend and per-truck timeline under the map (the archived RouteTimeline, reshaped per truck). */
function RouteList({
  plan,
  focus,
  onFocus,
  onFlyTo,
}: {
  plan: PlacesSolveResponse;
  focus: number | null;
  onFocus: (i: number | null) => void;
  onFlyTo: (p: { latitude: number; longitude: number; key: string }) => void;
}) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <ul className="flex flex-col gap-2" aria-label="Routes">
      {plan.routes.map((r, i) => {
        const expanded = open === i;
        return (
          <li
            key={i}
            className={`rounded-lg border bg-slate-950 transition-colors ${focus === i ? 'border-slate-500' : 'border-slate-800'}`}
            onMouseEnter={() => onFocus(i)}
            onMouseLeave={() => onFocus(null)}
          >
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setOpen(expanded ? null : i)}
              onFocus={() => onFocus(i)}
              onBlur={() => onFocus(null)}
              className="w-full min-h-[44px] flex items-center gap-3 px-3 py-2 text-left rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
            >
              <span className="w-5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: mapRouteColor(i) }} aria-hidden="true" />
              <span className="text-sm font-semibold text-slate-100 whitespace-nowrap">{`Truck ${i + 1}`}</span>
              <span className="text-xs sm:text-sm text-slate-400 min-w-0 flex-1">
                {`${r.stops.length} stop${r.stops.length === 1 ? '' : 's'} · ${r.load}/${plan.scenario.vehicle_capacity} pallets · ${km(r.distance_km)} · ${hours(r.duration_hours)}`}
              </span>
              <ChevronDown
                className={`w-4 h-4 text-slate-400 shrink-0 transition-transform ${expanded ? 'rotate-180' : ''}`}
                aria-hidden="true"
              />
            </button>
            {expanded && (
              <ol className="px-3 pb-3 flex flex-col">
                <li className="flex items-center gap-3 py-1.5 text-sm text-slate-300">
                  <span className="w-6 h-6 rounded-md bg-red-600 border-2 border-white shrink-0" aria-hidden="true" />
                  {`Leave ${plan.depot.name}, ${plan.depot.city ?? ''}`}
                </li>
                {r.stops.map((si, k) => {
                  const s = plan.stops[si];
                  return (
                    <li key={s.id}>
                      <button
                        type="button"
                        onClick={() => onFlyTo({ latitude: s.latitude, longitude: s.longitude, key: `${s.id}:${Date.now()}` })}
                        className="w-full min-h-[44px] flex items-center gap-3 py-1.5 text-left rounded-md hover:bg-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                      >
                        <span
                          className="w-6 h-6 rounded-full shrink-0 flex items-center justify-center text-xs font-bold text-white border-2 border-white"
                          style={{ backgroundColor: mapRouteColor(i) }}
                          aria-hidden="true"
                        >
                          {k + 1}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm text-slate-100 truncate">{s.name}</span>
                          <span className="block text-xs text-slate-400 truncate">{placeLabel(s)}</span>
                        </span>
                        <span className="text-xs text-slate-400 tabular-nums whitespace-nowrap">{`+${hours(r.service_start_hours[k])}`}</span>
                      </button>
                    </li>
                  );
                })}
                <li className="flex items-center gap-3 py-1.5 text-sm text-slate-300">
                  <span className="w-6 h-6 rounded-md bg-red-600 border-2 border-white shrink-0" aria-hidden="true" />
                  <span className="flex-1">{`Back at ${plan.depot.name}`}</span>
                  <span className="text-xs text-slate-400 tabular-nums whitespace-nowrap">{`+${hours(r.duration_hours)}`}</span>
                </li>
              </ol>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** What the map's symbols mean: an overlay on wide screens, a strip under the map on phones. */
function MapLegend({ plan, className }: { plan: PlacesSolveResponse | null; className: string }) {
  return (
    <div className={`rounded-lg px-3 py-2 text-xs text-slate-800 ${className}`} aria-label="Map legend">
      <span className="flex items-center gap-2">
        <span className="w-3.5 h-3.5 rounded-[3px] bg-red-600 border-2 border-white shadow" aria-hidden="true" />
        Depot
      </span>
      <span className="flex items-center gap-2">
        <span className="w-3.5 h-3.5 rounded-full bg-slate-900 border-2 border-white shadow" aria-hidden="true" />
        Destination
      </span>
      <span className="flex items-center gap-2">
        <span className="w-3.5 h-3.5 rounded-full bg-blue-600 border-2 border-white shadow" aria-hidden="true" />
        Reachable, not chosen
      </span>
      {plan?.routes.map((_, i) => (
        <span key={i} className="flex items-center gap-2">
          <span className="w-3.5 h-1 rounded-full" style={{ backgroundColor: mapRouteColor(i) }} aria-hidden="true" />
          {`Truck ${i + 1}`}
        </span>
      ))}
    </div>
  );
}

function DistanceModelNote({ model }: { model: PlacesModel }) {
  return (
    <div className="flex gap-2.5 rounded-lg border border-slate-800 bg-slate-950 px-3 py-2.5 text-sm text-slate-300">
      <Info className="w-4 h-4 mt-0.5 shrink-0 text-blue-400" aria-hidden="true" />
      <p className="leading-relaxed min-w-0">
        <span className="font-semibold text-slate-100">{`Road distance = straight-line distance × ${model.road_factor}.`}</span>{' '}
        {model.road_factor_rationale} Lines on the map are straight connections between stops, not the roads a truck
        would take.
      </p>
    </div>
  );
}

export default function RealPlacePlanner() {
  const { catalogue, error: catalogueError, retry } = useCatalogue();
  const [model, setModel] = useState<PlacesModel | null>(null);
  const [modelError, setModelError] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();

  const [depotId, setDepotId] = useState<number | null>(null);
  const [stopIds, setStopIds] = useState<number[]>([]);
  const [form, setForm] = useState<ScenarioForm | null>(null);
  const [method, setMethod] = useState<SolverMethod>('auto');
  const [timeLimit, setTimeLimit] = useState('2');

  const [plan, setPlan] = useState<PlacesSolveResponse | null>(null);
  const [solving, setSolving] = useState(false);
  const [solveError, setSolveError] = useState<string | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [flyTarget, setFlyTarget] = useState<{ latitude: number; longitude: number; key: string } | null>(null);
  const latestSolve = useRef(0);

  useEffect(() => {
    let live = true;
    placesApi
      .model()
      .then((m) => live && setModel(m))
      .catch((err: unknown) => live && setModelError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, []);

  const byId = useMemo(() => new Map((catalogue?.distributors ?? []).map((d) => [d.id, d])), [catalogue]);
  const depot = depotId !== null ? byId.get(depotId) ?? null : null;
  const scenario = form ? parseScenario(form) : null;

  const invalidate = () => {
    latestSolve.current += 1;
    setPlan(null);
    setSolveError(null);
    setFocus(null);
  };

  const solve = useCallback(
    async (depot: number, stops: number[], sc: PlacesScenario, m: SolverMethod, limit: number) => {
      const run = ++latestSolve.current;
      setSolving(true);
      setSolveError(null);
      try {
        const result = await placesApi.solve({ depot_id: depot, stop_ids: stops, scenario: sc, method: m, time_limit_seconds: limit });
        if (run === latestSolve.current) setPlan(result);
      } catch (err) {
        if (run === latestSolve.current) setSolveError(errorMessage(err));
      } finally {
        if (run === latestSolve.current) setSolving(false);
      }
    },
    [],
  );

  // First load: the depot from ?depot= (the Map page's "Plan routes from this
  // distributor") with the nearest destinations that fit, else the backend's example.
  // Either way the plan is solved straight away so the page opens on drawn routes.
  const initialised = useRef(false);
  useEffect(() => {
    if (initialised.current || !model || !catalogue) return;
    initialised.current = true;
    const sc = model.default_scenario;
    setForm(toForm(sc));
    const asked = Number(params.get('depot'));
    const fromUrl = Number.isInteger(asked) ? byId.get(asked) : undefined;
    let d: number | null = null;
    let s: number[] = [];
    if (fromUrl) {
      d = fromUrl.id;
      s = suggestStops(fromUrl, catalogue.distributors, model.road_regions, sc, model.road_factor, model.limits.max_stops);
    } else if (model.example && byId.has(model.example.depot_id)) {
      d = model.example.depot_id;
      s = model.example.stop_ids.filter((id) => byId.has(id));
    } else if (catalogue.distributors.length) {
      d = catalogue.distributors[0].id;
      s = suggestStops(catalogue.distributors[0], catalogue.distributors, model.road_regions, sc, model.road_factor, model.limits.max_stops);
    }
    setDepotId(d);
    setStopIds(s);
    if (d !== null && s.length) void solve(d, s, sc, 'auto', 2);
  }, [model, catalogue, byId, params, solve]);

  const candidates = useMemo(
    () => (depot && model && catalogue ? reachable(depot, catalogue.distributors, model.road_regions) : []),
    [depot, model, catalogue],
  );
  const candidateSites = useMemo(() => {
    const out: { key: string; label: string; km: number; members: LocatedDistributor[] }[] = [];
    const idx = new Map<string, number>();
    for (const { d, km: dist } of candidates) {
      const key = siteKey(d);
      if (!idx.has(key)) {
        idx.set(key, out.length);
        out.push({ key, label: placeLabel(d), km: dist, members: [] });
      }
      out[idx.get(key)!].members.push(d);
    }
    return out;
  }, [candidates]);

  const sites = useMemo(() => (catalogue ? groupSites(catalogue.distributors) : []), [catalogue]);
  const selected = useMemo(() => new Set(stopIds), [stopIds]);
  const region = depot && model ? regionOf(model.road_regions, depot.country) : null;
  const inRegion = useCallback(
    (s: Site) => !!model && regionOf(model.road_regions, s.country) === region,
    [model, region],
  );

  const toneOf = useCallback(
    (s: Site): SiteTone => {
      if (depot && s.key === siteKey(depot)) return 'depot';
      if (s.distributors.some((d) => selected.has(d.id))) return 'stop';
      return inRegion(s) ? 'default' : 'dim';
    },
    [depot, selected, inRegion],
  );
  const hintOf = useCallback(
    (s: Site) => {
      if (depot && s.key === siteKey(depot)) return `Depot: ${depot.name}`;
      if (!inRegion(s)) return `Not reachable by road from the depot (${region ?? 'no region'})`;
      return s.distributors.every((d) => selected.has(d.id)) ? 'Click to remove as destinations' : 'Click to add as destinations';
    },
    [depot, inRegion, region, selected],
  );

  const setStops = (next: number[]) => {
    setStopIds(next);
    invalidate();
  };
  const toggleMany = (ids: number[]) => {
    const all = ids.every((id) => selected.has(id));
    setStops(all ? stopIds.filter((id) => !ids.includes(id)) : [...stopIds, ...ids.filter((id) => !selected.has(id))]);
  };
  const onMapSite = (s: Site) => {
    if (!depot || s.key === siteKey(depot) || !inRegion(s)) return;
    toggleMany(s.distributors.filter((d) => d.id !== depot.id).map((d) => d.id));
  };
  const chooseDepot = (id: number) => {
    const d = byId.get(id);
    if (!d || !model || !catalogue) return;
    setDepotId(id);
    const sc = scenario ?? model.default_scenario;
    setStops(suggestStops(d, catalogue.distributors, model.road_regions, sc, model.road_factor, model.limits.max_stops));
    const next = new URLSearchParams(params);
    next.set('depot', String(id));
    setParams(next, { replace: true });
  };

  const depotGroups = useMemo(() => {
    const groups = new Map<string, LocatedDistributor[]>();
    for (const d of [...(catalogue?.distributors ?? [])].sort((a, b) => a.name.localeCompare(b.name))) {
      const c = d.country ?? 'Unknown';
      if (!groups.has(c)) groups.set(c, []);
      groups.get(c)!.push(d);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [catalogue]);

  const fitPoints = useMemo<LatLngTuple[]>(() => {
    if (!depot) return sites.map((s) => [s.latitude, s.longitude]);
    const pts: LatLngTuple[] = [[depot.latitude, depot.longitude]];
    for (const id of stopIds) {
      const d = byId.get(id);
      if (d) pts.push([d.latitude, d.longitude]);
    }
    return pts;
  }, [depot, stopIds, byId, sites]);
  // Refit when the depot changes or a plan lands, not on every checkbox.
  const fitKey = `${depotId}:${plan ? plan.stops.map((s) => s.id).join(',') : 'none'}`;

  if (catalogueError || modelError) {
    return (
      <ErrorBox title="Could not load the real-place planner">
        {catalogueError ?? modelError}{' '}
        {catalogueError && (
          <button type="button" onClick={retry} className="underline text-red-100 hover:text-white">
            Try again
          </button>
        )}
      </ErrorBox>
    );
  }
  if (!catalogue || !model || !form) return <Busy what="Loading the distributor catalogue…" />;

  const limits = model.limits;
  const timeLimitValue = Number(timeLimit);
  const timeLimitOk = Number.isFinite(timeLimitValue) && timeLimitValue > 0 && timeLimitValue <= limits.max_time_limit_seconds;
  const exactTooBig = method === 'cpsat' && stopIds.length > limits.max_exact_customers;
  const tooMany = stopIds.length > limits.max_stops;
  const canSolve = !!depot && stopIds.length > 0 && !!scenario && timeLimitOk && !exactTooBig && !tooMany && !solving;
  const countryIds = depot ? candidates.filter(({ d }) => d.country === depot.country).map(({ d }) => d.id) : [];

  return (
    <div className="flex flex-col gap-6">
      <SnapshotNotice provenance={catalogue.provenance} compact />
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,23rem)_minmax(0,1fr)] gap-6 items-start">
        <Card title="Depot, destinations and fleet">
          <Field
            label="Depot"
            htmlFor="depot"
            hint="Where every truck starts and ends: a real distributor location from the catalogue."
          >
            <select id="depot" className={inputClass} value={depotId ?? ''} onChange={(e) => chooseDepot(Number(e.target.value))}>
              {depotGroups.map(([country, list]) => (
                <optgroup key={country} label={country}>
                  {list.map((d) => (
                    <option key={d.id} value={d.id}>{`${d.name} (${d.city ?? 'city unknown'})`}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </Field>

          <fieldset className="flex flex-col gap-2 min-w-0">
            <legend className="text-sm font-medium text-slate-300 mb-1.5">
              {`Destinations: ${stopIds.length} selected`}
            </legend>
            <p className="text-xs text-slate-400 leading-relaxed">
              {`Real distributors a truck can reach by road from the depot (${region ?? 'no region'}), nearest first. Click a marker on the map to add or remove a whole city.`}
            </p>
            <div className="grid grid-cols-3 gap-2">
              <button
                type="button"
                className={`${secondaryButtonClass} px-2`}
                onClick={() =>
                  depot && setStops(suggestStops(depot, catalogue.distributors, model.road_regions, scenario ?? model.default_scenario, model.road_factor, limits.max_stops))
                }
              >
                Nearest
              </button>
              <button
                type="button"
                className={`${secondaryButtonClass} px-2`}
                disabled={!countryIds.length}
                onClick={() => setStops(countryIds.slice(0, limits.max_stops))}
                title={depot ? `Every reachable distributor in ${depot.country}` : undefined}
              >
                {depot?.country ? `All ${depot.country}` : 'Country'}
              </button>
              <button type="button" className={`${secondaryButtonClass} px-2`} onClick={() => setStops([])}>
                Clear
              </button>
            </div>
            <div className="max-h-72 overflow-y-auto rounded-lg border border-slate-800 bg-slate-950 divide-y divide-slate-800">
              {candidateSites.length === 0 && (
                <p className="px-3 py-3 text-sm text-slate-400">No other distributor is reachable by road from this depot.</p>
              )}
              {candidateSites.map((cs) => {
                const ids = cs.members.map((d) => d.id);
                const all = ids.every((id) => selected.has(id));
                const some = !all && ids.some((id) => selected.has(id));
                return (
                  <div key={cs.key} className="py-1">
                    <label className="flex items-center gap-3 px-3 min-h-[44px] cursor-pointer">
                      <input
                        type="checkbox"
                        className="w-4 h-4 accent-blue-500"
                        checked={all}
                        ref={(el) => {
                          if (el) el.indeterminate = some;
                        }}
                        onChange={() => toggleMany(ids)}
                      />
                      <span className="flex-1 min-w-0 text-sm font-medium text-slate-100 truncate">{cs.label}</span>
                      <span className="text-xs text-slate-400 tabular-nums whitespace-nowrap">
                        {`≈ ${km(cs.km * model.road_factor)}`}
                      </span>
                    </label>
                    {cs.members.length > 1 &&
                      cs.members.map((d) => (
                        <label key={d.id} className="flex items-center gap-3 pl-10 pr-3 min-h-[36px] cursor-pointer">
                          <input
                            type="checkbox"
                            className="w-4 h-4 accent-blue-500"
                            checked={selected.has(d.id)}
                            onChange={() => toggleMany([d.id])}
                          />
                          <span className="text-sm text-slate-300 truncate">{d.name}</span>
                        </label>
                      ))}
                    {cs.members.length === 1 && <p className="pl-10 pr-3 pb-1.5 -mt-1.5 text-xs text-slate-400 truncate">{cs.members[0].name}</p>}
                  </div>
                );
              })}
            </div>
            {tooMany && <p className="text-xs text-amber-300">{`At most ${limits.max_stops} destinations per plan.`}</p>}
          </fieldset>

          <fieldset className="flex flex-col gap-3 rounded-lg border border-violet-500/40 bg-violet-500/5 p-3 min-w-0">
            <legend className="px-1.5 text-sm font-semibold text-violet-200 flex items-center gap-1.5">
              <FlaskConical className="w-4 h-4" aria-hidden="true" />
              Example scenario
            </legend>
            <p className="text-xs text-violet-100/90 leading-relaxed">
              The places are real; everything else here is made up. The catalogue has no orders, trucks or opening
              hours, so this is an illustrative pickup run: each truck leaves the depot, collects the same load at
              each destination and returns. No destination is a real customer and no load is a real order.
            </p>
            <div className="grid grid-cols-2 gap-3">
              {SCENARIO_FIELDS.map(({ key, label, unit, step }) => (
                <Field key={key} label={label} htmlFor={`sc-${key}`} hint={unit}>
                  <input
                    id={`sc-${key}`}
                    type="number"
                    min={0}
                    step={step}
                    className={inputClass}
                    value={form[key]}
                    onChange={(e) => {
                      setForm({ ...form, [key]: e.target.value });
                      invalidate();
                    }}
                  />
                </Field>
              ))}
            </div>
            <button
              type="button"
              className={`${secondaryButtonClass} self-start`}
              onClick={() => {
                setForm(toForm(model.default_scenario));
                invalidate();
              }}
            >
              Reset the example
            </button>
          </fieldset>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Solver" htmlFor="pl-method">
              <select id="pl-method" className={inputClass} value={method} onChange={(e) => setMethod(e.target.value as SolverMethod)}>
                {METHODS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Time limit" htmlFor="pl-time" hint={`seconds, up to ${limits.max_time_limit_seconds}`}>
              <input
                id="pl-time"
                type="number"
                min={0.1}
                step={0.5}
                max={limits.max_time_limit_seconds}
                className={inputClass}
                value={timeLimit}
                onChange={(e) => setTimeLimit(e.target.value)}
              />
            </Field>
          </div>
          {method === 'auto' && (
            <p className="text-xs text-slate-400 -mt-2">
              {`Auto: the exact CP-SAT model up to ${limits.auto_exact_max_customers} destinations, OR-Tools routing above.`}
            </p>
          )}
          {exactTooBig && (
            <p className="text-xs text-amber-300">{`The exact model is capped at ${limits.max_exact_customers} destinations; ${stopIds.length} are selected.`}</p>
          )}
          {!scenario && <p className="text-xs text-amber-300">Every example-scenario field needs a number.</p>}

          <button
            type="button"
            className={buttonClass}
            disabled={!canSolve}
            onClick={() => depot && scenario && solve(depot.id, stopIds, scenario, method, timeLimitValue)}
          >
            <RouteIcon className="w-4 h-4" aria-hidden="true" />
            Plan routes
          </button>
        </Card>

        <Card title={depot ? `Routes from ${depot.name}, ${depot.city ?? ''}` : 'Routes'}>
          <div className="relative">
            <BaseMap
              label="Map of the depot, the destinations and the planned routes"
              className="h-[420px] sm:h-[520px] rounded-lg border border-slate-800"
            >
              <SiteLayer sites={sites} toneOf={toneOf} hintOf={hintOf} onSelect={onMapSite} />
              {plan && <RouteLayer plan={plan} focus={focus} />}
              <FitBounds points={fitPoints} fitKey={fitKey} maxZoom={9} />
              <FlyTo target={flyTarget} minZoom={9} />
            </BaseMap>
            <MapLegend plan={plan} className="hidden sm:flex absolute top-2 right-2 z-[1000] shadow-md flex-col gap-1 max-h-[70%] overflow-y-auto bg-white/95" />
          </div>
          <MapLegend plan={plan} className="sm:hidden flex flex-wrap gap-x-4 gap-y-1.5 -mt-2 bg-white" />

          <DistanceModelNote model={model} />

          {solving && <Busy what="Planning routes…" />}
          {solveError && <ErrorBox title="These routes cannot be planned">{solveError}</ErrorBox>}
          {!solving && !plan && !solveError && (
            <p className="text-sm text-slate-400">
              {stopIds.length ? `${stopIds.length} destinations selected. Press Plan routes to draw the trucks' routes.` : 'Choose at least one destination.'}
            </p>
          )}
          {plan && !solving && (
            <>
              <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
                <Stat
                  label="Road distance"
                  value={km(plan.total_km)}
                  detail={`${km(plan.total_straight_line_km)} straight line × ${plan.road_factor}`}
                />
                <Stat label="Trucks used" value={plan.vehicles_used} detail={`of ${plan.scenario.num_vehicles} in the example fleet`} />
                <Stat
                  label="Solver runtime"
                  value={`${plan.wall_seconds.toFixed(2)} s`}
                  detail={`method: ${plan.method}`}
                />
                <Stat
                  label="Feasibility"
                  value={plan.feasible ? 'Feasible' : 'Not feasible'}
                  tone={plan.feasible ? 'good' : 'bad'}
                  detail={STATUS_TEXT[plan.status]}
                />
              </div>
              {plan.violations.length > 0 && (
                <ErrorBox title="The validator rejected this plan">
                  <ul className="list-disc pl-4 flex flex-col gap-0.5">
                    {plan.violations.slice(0, 10).map((v) => (
                      <li key={v}>{v}</li>
                    ))}
                  </ul>
                </ErrorBox>
              )}
              <RouteList plan={plan} focus={focus} onFocus={setFocus} onFlyTo={setFlyTarget} />
              <p className="text-xs text-slate-400">
                {`Times are hours after leaving the depot at ${plan.scenario.speed_kmh} km/h with ${plan.scenario.service_minutes} minutes at each stop, both from the example scenario.`}
              </p>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}
