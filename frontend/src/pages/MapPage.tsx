import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, ArrowRight, MapPin, X } from 'lucide-react';
import type { LatLngTuple } from 'leaflet';
import BaseMap, { FitBounds, FlyTo } from '../components/map/BaseMap';
import SiteLayer, { type SiteTone } from '../components/map/SiteLayer';
import DistributorSearchBar from '../components/map/DistributorSearchBar';
import SnapshotNotice from '../components/SnapshotNotice';
import { Busy, ErrorBox, buttonClass, secondaryButtonClass } from '../components/ui';
import { useCatalogue } from '../services/catalogue';
import { catalogueApi, errorMessage, type CarriedComponent, type LocatedDistributor } from '../services/api';
import { groupSites, placeLabel, type Site } from '../lib/sites';

const OVERVIEW_SITES = 8;
const TOP_COMPONENTS = 8;

const fmt = (n: number) => n.toLocaleString();

function money(price: number, currency: string | null) {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency ?? 'USD', maximumSignificantDigits: 4 }).format(price);
  } catch {
    return `${price} ${currency ?? ''}`.trim();
  }
}

/** What one distributor carries in the snapshot: its categories and biggest stock lines. */
function DistributorDetail({ distributor, year }: { distributor: LocatedDistributor; year: number }) {
  const [components, setComponents] = useState<CarriedComponent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // The parent keys this component by distributor, so each one starts from empty state.
    let live = true;
    catalogueApi
      .components(distributor.id)
      .then((c) => live && setComponents(c))
      .catch((err: unknown) => live && setError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, [distributor.id]);

  const categories = useMemo(() => {
    if (!components) return [];
    const counts = new Map<string, number>();
    for (const c of components) counts.set(c.category, (counts.get(c.category) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [components]);
  const most = categories[0]?.[1] ?? 1;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h3 className="text-lg font-semibold text-white leading-snug">{distributor.name}</h3>
        <p className="flex items-center gap-1.5 text-sm text-slate-300">
          <MapPin className="w-3.5 h-3.5 text-slate-400 shrink-0" aria-hidden="true" />
          {placeLabel(distributor)}
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-2">
        <div className="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2">
          <dt className="text-xs text-slate-400">{`Price offers (${year})`}</dt>
          <dd className="text-lg font-semibold text-white tabular-nums">{fmt(distributor.total_offers)}</dd>
        </div>
        <div className="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2">
          <dt className="text-xs text-slate-400">{`Units in stock (${year})`}</dt>
          <dd className="text-lg font-semibold text-white tabular-nums">{fmt(distributor.total_stock)}</dd>
        </div>
      </dl>

      <section className="flex flex-col gap-2" aria-label="What it carries">
        <h4 className="text-sm font-semibold text-slate-200">What it carries</h4>
        {!components && !error && <Busy what="Loading its catalogue…" />}
        {error && <ErrorBox title="Could not load its components">{error}</ErrorBox>}
        {components && components.length === 0 && (
          <p className="text-sm text-slate-400">No priced offers in the snapshot for this distributor.</p>
        )}
        {components && components.length > 0 && (
          <>
            <ul className="flex flex-col gap-1.5">
              {categories.map(([category, n]) => (
                <li key={category} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 text-sm">
                  <span className="text-slate-200 truncate">{category}</span>
                  <span className="text-slate-400 tabular-nums text-xs">{`${n} part${n === 1 ? '' : 's'}`}</span>
                  <span className="col-span-2 h-1.5 rounded-full bg-slate-800 overflow-hidden" aria-hidden="true">
                    <span className="block h-full rounded-full bg-blue-500" style={{ width: `${(n / most) * 100}%` }} />
                  </span>
                </li>
              ))}
            </ul>
            <h4 className="text-sm font-semibold text-slate-200 mt-2">Largest stock lines</h4>
            <div className="overflow-x-auto -mx-1">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-400">
                    <th className="font-medium px-1 py-1.5">Part</th>
                    <th className="font-medium px-1 py-1.5 text-right">Stock</th>
                    <th className="font-medium px-1 py-1.5 text-right">Unit price</th>
                  </tr>
                </thead>
                <tbody>
                  {components.slice(0, TOP_COMPONENTS).map((c) => (
                    <tr key={c.component_id} className="border-t border-slate-800 align-top">
                      <td className="px-1 py-1.5 min-w-0">
                        <span className="block text-slate-100 font-mono text-xs break-all">{c.mpn}</span>
                        <span className="block text-xs text-slate-400">{`${c.manufacturer} · ${c.category}`}</span>
                      </td>
                      <td className="px-1 py-1.5 text-right tabular-nums text-slate-200">{fmt(c.stock)}</td>
                      <td className="px-1 py-1.5 text-right tabular-nums text-slate-200 whitespace-nowrap">{money(c.price, c.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-slate-400">
              {`${fmt(components.length)} parts in total. Prices and stock are ${year} observations, not current quotes.`}
            </p>
          </>
        )}
      </section>

      <Link to={`/route-plan?depot=${distributor.id}`} className={buttonClass}>
        Plan routes from this distributor
        <ArrowRight className="w-4 h-4" aria-hidden="true" />
      </Link>
    </div>
  );
}

export default function MapPage() {
  const { catalogue, error, retry } = useCatalogue();
  const [siteKey, setSiteKey] = useState<string | null>(null);
  const [distributorId, setDistributorId] = useState<number | null>(null);
  const [flyTarget, setFlyTarget] = useState<{ latitude: number; longitude: number; key: string } | null>(null);
  const [showAllSites, setShowAllSites] = useState(false);

  const sites = useMemo(() => (catalogue ? groupSites(catalogue.distributors) : []), [catalogue]);
  const site = sites.find((s) => s.key === siteKey) ?? null;
  const distributor =
    site?.distributors.find((d) => d.id === distributorId) ?? (site?.distributors.length === 1 ? site.distributors[0] : null);
  const points = useMemo<LatLngTuple[]>(() => sites.map((s) => [s.latitude, s.longitude]), [sites]);

  const openSite = useCallback((s: Site, fly = false) => {
    setSiteKey(s.key);
    setDistributorId(s.distributors.length === 1 ? s.distributors[0].id : null);
    if (fly) setFlyTarget({ latitude: s.latitude, longitude: s.longitude, key: `${s.key}:${Date.now()}` });
  }, []);
  const toneOf = useCallback((s: Site): SiteTone => (s.key === siteKey ? 'selected' : 'default'), [siteKey]);
  const hintOf = useCallback(() => 'Click for details', []);

  const onSearch = (d: LocatedDistributor) => {
    const s = sites.find((x) => x.distributors.some((y) => y.id === d.id));
    if (!s) return;
    openSite(s, true);
    setDistributorId(d.id);
  };

  const countries = useMemo(() => new Set(sites.map((s) => s.country)).size, [sites]);

  return (
    <div className="h-full overflow-y-auto lg:overflow-hidden bg-slate-950 flex flex-col lg:flex-row">
      <BaseMap
        label="Map of distributor locations"
        className="h-[55vh] min-h-[320px] shrink-0 lg:h-full lg:flex-1 lg:min-w-0"
      >
        {sites.length > 0 && (
          <>
            <SiteLayer sites={sites} toneOf={toneOf} hintOf={hintOf} onSelect={openSite} cluster />
            <FitBounds points={points} fitKey={`all:${sites.length}`} maxZoom={4} />
            <FlyTo target={flyTarget} minZoom={10} />
          </>
        )}
      </BaseMap>

      <aside
        aria-label="Distributor details"
        className="lg:w-[26rem] lg:shrink-0 lg:h-full lg:overflow-y-auto border-t lg:border-t-0 lg:border-l border-slate-800 bg-slate-900 px-4 sm:px-5 py-5 flex flex-col gap-4"
      >
        <header className="flex flex-col gap-1.5">
          <h1 className="text-xl font-semibold text-white">Distributor map</h1>
          <p className="text-sm text-slate-400 leading-relaxed">
            Every electronic-component distributor in the catalogue with a verified location, drawn where it really
            is. Where many share a city, they share one numbered marker; zoomed out, nearby markers merge into a
            cluster that splits as you zoom in.
          </p>
        </header>

        {error && (
          <ErrorBox title="Could not load the distributors">
            {error}{' '}
            <button type="button" onClick={retry} className="underline text-red-100 hover:text-white">
              Try again
            </button>
          </ErrorBox>
        )}
        {!catalogue && !error && <Busy what="Loading distributors…" />}

        {catalogue && (
          <>
            <SnapshotNotice provenance={catalogue.provenance} />
            <DistributorSearchBar distributors={catalogue.distributors} onSelect={onSearch} />

            {!site && (
              <section className="flex flex-col gap-3" aria-label="Overview">
                <p className="text-sm text-slate-300">
                  {`${catalogue.distributors.length} distributors at ${sites.length} locations in ${countries} countries. Click a marker, or pick a location:`}
                </p>
                <ul className="flex flex-col gap-1.5">
                  {(showAllSites ? sites : sites.slice(0, OVERVIEW_SITES)).map((s) => (
                    <li key={s.key}>
                      <button
                        type="button"
                        onClick={() => openSite(s, true)}
                        className="w-full min-h-[44px] flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950 hover:border-blue-500 px-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                      >
                        <span className="text-sm text-slate-200 truncate">{placeLabel(s)}</span>
                        <span className="shrink-0 text-xs font-semibold text-white bg-blue-600 rounded-full px-2 py-0.5 tabular-nums">
                          {s.distributors.length}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                {sites.length > OVERVIEW_SITES && (
                  <button type="button" className={secondaryButtonClass} onClick={() => setShowAllSites((v) => !v)}>
                    {showAllSites ? 'Show fewer locations' : `Show all ${sites.length} locations`}
                  </button>
                )}
              </section>
            )}

            {site && (
              <section className="flex flex-col gap-4" aria-label={`Distributors in ${placeLabel(site)}`}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="text-base font-semibold text-white">{placeLabel(site)}</h2>
                    <p className="text-xs text-slate-400">
                      {site.distributors.length === 1
                        ? 'One distributor at this location.'
                        : `${site.distributors.length} distributors share this city-level point.`}
                    </p>
                  </div>
                  <button
                    type="button"
                    aria-label="Close details"
                    onClick={() => {
                      setSiteKey(null);
                      setDistributorId(null);
                    }}
                    className="shrink-0 w-11 h-11 flex items-center justify-center rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                  >
                    <X className="w-4 h-4" aria-hidden="true" />
                  </button>
                </div>

                {site.distributors.length > 1 && !distributor && (
                  <ul className="flex flex-col gap-1.5" aria-label="Distributors here">
                    {site.distributors.map((d) => (
                      <li key={d.id}>
                        <button
                          type="button"
                          onClick={() => setDistributorId(d.id)}
                          className="w-full min-h-[44px] flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950 hover:border-blue-500 px-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                        >
                          <span className="text-sm text-slate-100 truncate">{d.name}</span>
                          <span className="shrink-0 text-xs text-slate-400 tabular-nums">{`${fmt(d.total_offers)} offers`}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}

                {distributor && site.distributors.length > 1 && (
                  <button type="button" className={`${secondaryButtonClass} self-start`} onClick={() => setDistributorId(null)}>
                    <ArrowLeft className="w-4 h-4" aria-hidden="true" />
                    {`All ${site.distributors.length} here`}
                  </button>
                )}
                {distributor && <DistributorDetail key={distributor.id} distributor={distributor} year={catalogue.provenance.snapshot_year} />}
              </section>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
