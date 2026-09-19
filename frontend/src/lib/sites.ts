import type { LocatedDistributor } from '../services/api';

/**
 * One point on the map. Catalogue coordinates are per city, not per street address,
 * so several distributors can share exactly one point (Shenzhen holds most of the
 * Chinese distributors). A site is that point with everyone on it.
 */
export interface Site {
  key: string;
  latitude: number;
  longitude: number;
  city: string | null;
  state: string | null;
  country: string | null;
  /** Largest catalogue first, then by name. */
  distributors: LocatedDistributor[];
}

export const siteKey = (d: { latitude: number; longitude: number }) => `${d.latitude},${d.longitude}`;

/** Group distributors by exact coordinate. Sites come back largest first. */
export function groupSites(distributors: LocatedDistributor[]): Site[] {
  const byKey = new Map<string, Site>();
  for (const d of distributors) {
    const key = siteKey(d);
    let site = byKey.get(key);
    if (!site) {
      site = { key, latitude: d.latitude, longitude: d.longitude, city: d.city, state: d.state, country: d.country, distributors: [] };
      byKey.set(key, site);
    }
    site.distributors.push(d);
  }
  const sites = [...byKey.values()];
  for (const s of sites) {
    s.distributors.sort((a, b) => b.total_offers - a.total_offers || a.name.localeCompare(b.name));
  }
  return sites.sort((a, b) => b.distributors.length - a.distributors.length || (a.city ?? '').localeCompare(b.city ?? ''));
}

/** "Leeds, Yorkshire, UK" - whatever parts are known. */
export const placeLabel = (p: { city: string | null; state?: string | null; country: string | null }) =>
  [p.city, p.state, p.country].filter(Boolean).join(', ');

/** The road region a country belongs to, per the backend's map; an unmapped country is its own. */
export const regionOf = (regions: Record<string, string>, country: string | null) =>
  country ? regions[country] ?? country : null;
