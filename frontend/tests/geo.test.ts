/**
 * The client-side great-circle helper must agree with the backend's (app/vrp/geo.py,
 * pinned by backend/tests/test_real_place_routing.py) on the same known city pairs.
 */
import { describe, expect, it } from 'vitest';
import { greatCircleKm, suggestStops } from '../src/lib/geo';
import type { LocatedDistributor } from '../src/services/api';

const at = (latitude: number, longitude: number) => ({ latitude, longitude });

describe('greatCircleKm', () => {
  it.each([
    ['London-Paris', at(51.5074, -0.1278), at(48.8566, 2.3522), 343.5],
    ['New York-Los Angeles', at(40.7128, -74.006), at(34.0522, -118.2437), 3935.7],
    ['Sydney-Melbourne', at(-33.8688, 151.2093), at(-37.8136, 144.9631), 713.4],
  ])('%s', (_, a, b, km) => {
    expect(greatCircleKm(a, b)).toBeCloseTo(km, -1);
    expect(Math.abs(greatCircleKm(a, b) - km) / km).toBeLessThan(0.002);
  });

  it('is zero from a point to itself', () => {
    expect(greatCircleKm(at(22.5431, 114.0579), at(22.5431, 114.0579))).toBe(0);
  });
});

describe('suggestStops', () => {
  const d = (id: number, country: string, latitude: number, longitude: number): LocatedDistributor => ({
    id,
    name: `D${id}`,
    latitude,
    longitude,
    city: null,
    state: null,
    country,
    is_domestic: null,
    total_offers: 0,
    total_stock: 0,
    shares_location_with: 0,
  });
  const regions = { UK: 'Europe', Germany: 'Europe', China: 'Mainland Asia' };
  const scenario = { stop_load: 3, vehicle_capacity: 12, num_vehicles: 1, speed_kmh: 60, service_minutes: 30, max_route_hours: 14 };

  it('keeps to the depot region, the route limit and the fleet, one per location first', () => {
    const depot = d(1, 'UK', 53.796, -1.5478); // Leeds
    const all = [
      depot,
      d(2, 'UK', 51.5074, -0.1278), // London
      d(3, 'UK', 51.5074, -0.1278), // London again: same point
      d(4, 'UK', 52.4862, -1.8904), // Birmingham
      d(5, 'China', 22.5431, 114.0579), // another region
      d(6, 'Germany', 52.52, 13.405), // Berlin: a round trip is far over the limit
    ];
    // One truck of 12 pallets at 3 per stop: room for 4, only 3 reachable in time.
    expect(suggestStops(depot, all, regions, scenario, 1.3, 60)).toEqual([4, 2, 3]);
    expect(suggestStops(depot, all, regions, { ...scenario, vehicle_capacity: 6 }, 1.3, 60)).toEqual([4, 2]);
  });
});
