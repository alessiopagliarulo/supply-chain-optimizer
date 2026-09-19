import type { LocatedDistributor, PlacesScenario } from '../services/api';
import { regionOf, siteKey } from './sites';

/** IUGG mean Earth radius, the same constant as backend/app/vrp/geo.py. */
const EARTH_RADIUS_KM = 6371.0088;

type Point = { latitude: number; longitude: number };

/**
 * Haversine distance in km. The same formula as the backend, used here only to rank
 * and pre-select destinations; every distance a plan reports comes from the server.
 */
export function greatCircleKm(a: Point, b: Point): number {
  const r = Math.PI / 180;
  const h =
    Math.sin(((b.latitude - a.latitude) * r) / 2) ** 2 +
    Math.cos(a.latitude * r) * Math.cos(b.latitude * r) * Math.sin(((b.longitude - a.longitude) * r) / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** The distributors a truck from `depot` can reach by road, nearest first. */
export function reachable(depot: LocatedDistributor, all: LocatedDistributor[], regions: Record<string, string>) {
  const region = regionOf(regions, depot.country);
  return all
    .filter((d) => d.id !== depot.id && regionOf(regions, d.country) === region)
    .map((d) => ({ d, km: greatCircleKm(depot, d) }))
    .sort((a, b) => a.km - b.km || a.d.name.localeCompare(b.d.name));
}

/**
 * A starting set of destinations the example fleet can actually serve: nearest first,
 * one distributor per location before a second at the same point, keeping only stops
 * whose round trip fits the route limit and stopping when the fleet is full.
 */
export function suggestStops(
  depot: LocatedDistributor,
  all: LocatedDistributor[],
  regions: Record<string, string>,
  scenario: PlacesScenario,
  roadFactor: number,
  maxStops: number,
): number[] {
  const perTruck = Math.floor(scenario.vehicle_capacity / scenario.stop_load);
  const room = Math.min(maxStops, perTruck * scenario.num_vehicles);
  const fits = reachable(depot, all, regions).filter(({ km }) => {
    const hours = (2 * km * roadFactor) / scenario.speed_kmh + scenario.service_minutes / 60;
    return hours <= scenario.max_route_hours;
  });
  const firsts: number[] = [];
  const rest: number[] = [];
  const seen = new Set<string>();
  for (const { d } of fits) {
    const key = siteKey(d);
    (seen.has(key) ? rest : firsts).push(d.id);
    seen.add(key);
  }
  return [...firsts, ...rest].slice(0, room);
}
