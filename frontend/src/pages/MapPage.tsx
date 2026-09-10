import { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import Map, {
  Marker,
  NavigationControl,
  Source,
  Layer,
  type MapLayerMouseEvent,
  type MapRef,
} from 'react-map-gl/maplibre';
import type { LineLayerSpecification } from 'maplibre-gl';
import { distributorsAPI, getCrossDockHubs, type HubOut, graphAPI, benchmarkAPI } from '../services/api';
import { useAuthStore } from '../store/authStore';
import { useOptimizeStore } from '../store/optimizeStore';
import RouteMetricsBar from '../components/map/RouteMetricsBar';
import RouteLegPopup, { type RouteLegData } from '../components/map/RouteLegPopup';
import RouteTimeline from '../components/map/RouteTimeline';
import DistributorSearchBar from '../components/map/DistributorSearchBar';
import { RISK_COLORS } from '../lib/risk';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Route, ChevronDown, Globe, MapPin, X, Search, Package, Tag } from 'lucide-react';

const MAP_STYLE = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';

const INITIAL_VIEW = {
  longitude: -40,
  latitude: 30,
  zoom: 2.5,
  pitch: 0,
  bearing: 0,
};

interface DistributorPin {
  id: number;
  name: string;
  city: string | null;
  state: string | null;
  country: string;
  latitude: number;
  longitude: number;
  is_domestic: boolean;
  total_offers: number;
  total_stock: number;
}

interface RoadPath {
  coordinates: [number, number][];
  stopIndex: number;
}

// Network Risk view interfaces
interface GraphMetricsResponse {
  n_distributors: number;
  n_components: number;
  n_edges: number;
  // Algebraic connectivity (Fiedler value / λ₂), reported as two distinct numbers —
  // never conflate them in the UI:
  fiedler_whole_graph: number;       // λ₂ of the ENTIRE graph; exactly 0.0 because the
                                      // graph is disconnected (n_connected_components > 1)
  fiedler_giant_component: number;   // λ₂ of the LARGEST connected component only —
                                      // the informative number
  n_connected_components: number;
  giant_component_size: number;
  giant_component_fraction: number;
  single_source_count: number;
  betweenness: Record<string, number>;
  pagerank: Record<string, number>;
  // What the scores above mean, so betweenness is never read as a percentage or a
  // failure probability. Backend-authored (see backend/app/api/graph.py) — surfaced
  // verbatim rather than restated, so the two never drift apart.
  centrality_notes?: Record<string, string>;
  k_core_summary: Record<string, number>;
  hhi_by_category: Record<string, number>;
}

interface SingleSourceComponent {
  component_id: number;
  mpn: string;
  manufacturer: string;
  distributor_id: number;
  distributor_name: string;
}

interface HeatmapPoint {
  lat: number;
  lng: number;
  weight: number;
  distributor_id: number;
  distributor_name: string;
}

async function fetchRoadPath(
  from: [number, number],
  to: [number, number],
): Promise<[number, number][]> {
  try {
    const url =
      `https://router.project-osrm.org/route/v1/driving/` +
      `${from[0]},${from[1]};${to[0]},${to[1]}` +
      `?overview=full&geometries=geojson`;
    const res = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error('OSRM non-200');
    const data = await res.json();
    const coords: [number, number][] | undefined =
      data?.routes?.[0]?.geometry?.coordinates;
    if (coords && coords.length > 1) return coords;
  } catch {
    // fall through to straight line
  }
  return [from, to];
}

function buildRouteGeoJSON(paths: RoadPath[]): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: paths.map((p) => ({
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: p.coordinates },
      properties: { stopIndex: p.stopIndex, isReturn: p.stopIndex < 0 },
    })),
  };
}

const routeForwardPaint = {
  'line-color': '#3b82f6',
  'line-width': ['interpolate', ['linear'], ['zoom'], 2, 1.5, 6, 3, 10, 5],
  'line-opacity': 0.9,
} as any;
const routeReturnPaint = {
  'line-color': '#64748b',
  'line-width': ['interpolate', ['linear'], ['zoom'], 2, 1, 6, 2, 10, 3],
  'line-opacity': 0.5,
} as any;
const routeForwardLayout: LineLayerSpecification['layout'] = {
  'line-cap': 'round',
  'line-join': 'round',
};

const STRATEGY_COLORS: Record<string, string> = {
  cheapest: '#22c55e',
  fastest: '#3b82f6',
  greenest: '#10b981',
  balanced: '#a855f7',
};

export default function MapPage() {
  const { user } = useAuthStore();
  const { multiResult, selectedId, setSelectedId, getSelected } = useOptimizeStore();
  const selectedRoute = getSelected();
  const mapRef = useRef<MapRef>(null);
  const mapContainerRef = useRef<HTMLDivElement>(null);

  const [distributors, setDistributors] = useState<DistributorPin[]>([]);
  const [hubs, setHubs] = useState<HubOut[]>([]);
  const [selectedDist, setSelectedDist] = useState<DistributorPin | null>(null);
  const [loading, setLoading] = useState(true);
  const [distributorsError, setDistributorsError] = useState<string | null>(null);
  const [distributorsRetryKey, setDistributorsRetryKey] = useState(0);
  const [timelineOpen, setTimelineOpen] = useState(false);
  const [viewState, setViewState] = useState(INITIAL_VIEW);
  const [showRoutes, setShowRoutes] = useState(true);
  const [showDomesticOnly, setShowDomesticOnly] = useState(false);

  const [roadPaths, setRoadPaths] = useState<RoadPath[]>([]);
  const [routeLoading, setRouteLoading] = useState(false);

  const [legPopup, setLegPopup] = useState<{
    data: RouteLegData;
    position: { x: number; y: number };
  } | null>(null);

  const [hoveredDistId, setHoveredDistId] = useState<number | null>(null);
  const [routeDropdownOpen, setRouteDropdownOpen] = useState(false);

  const [distDetail, setDistDetail] = useState<{
    top_components: {
      component_id: number;
      mpn: string;
      manufacturer: string;
      category: string;
      price: number;
      stock: number;
      sku: string;
    }[];
  } | null>(null);
  const [distDetailLoading, setDistDetailLoading] = useState(false);
  const [componentSearch, setComponentSearch] = useState('');
  const [categoryFilter, setCategoryFilter] = useState<string>('');

  // Network Risk view state
  const [mapView, setMapView] = useState<'routes' | 'network-risk'>('routes');
  const [graphMetrics, setGraphMetrics] = useState<GraphMetricsResponse | null>(null);
  const [singleSourceComponents, setSingleSourceComponents] = useState<SingleSourceComponent[]>([]);
  const [singleSourceDistributorIds, setSingleSourceDistributorIds] = useState<Set<number>>(new Set());
  const [showNetworkRiskPanel, setShowNetworkRiskPanel] = useState(false);
  const [graphMetricsLoading, setGraphMetricsLoading] = useState(false);
  const [graphMetricsError, setGraphMetricsError] = useState(false);
  const [selectedDistributorId, setSelectedDistributorId] = useState<number | null>(null);
  const componentRowRefs = useRef(new globalThis.Map<number, HTMLButtonElement>());
  const [cascadeActive, setCascadeActive] = useState(false);
  const [cascadeHeatmapData, setCascadeHeatmapData] = useState<HeatmapPoint[]>([]);
  const [cascadeLoading, setCascadeLoading] = useState(false);
  const [cascadeFetched, setCascadeFetched] = useState(false);
  // Backend-authored provenance for the heatmap weights (GET /benchmark/cascade-heatmap
  // `note` + `run_id`) — surfaced next to the legend so "0-100%" is never read as a
  // calibrated probability. See the legend render below.
  const [cascadeNote, setCascadeNote] = useState<string>('');
  const [cascadeRunId, setCascadeRunId] = useState<number | null>(null);

  useEffect(() => {
    // Note: `loading` starts true and `distributorsError` starts null, so no
    // synchronous setState is needed here on the initial run — the retry
    // button (which also bumps distributorsRetryKey) resets both before
    // triggering this effect again.
    let cancelled = false;
    distributorsAPI
      .list()
      .then((res) => {
        if (cancelled) return;
        setDistributors(res.data);
      })
      .catch(() => {
        if (cancelled) return;
        setDistributorsError('Failed to load the distributor network. Check your connection and try again.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [distributorsRetryKey]);

  useEffect(() => {
    getCrossDockHubs().then(setHubs).catch(() => setHubs([]));
  }, []);

  // Fetch OSRM road geometry for selected route
  useEffect(() => {
    if (!selectedRoute?.route || !user) {
      setRoadPaths([]);
      return;
    }

    let cancelled = false;
    setRouteLoading(true);

    const legs: { from: [number, number]; to: [number, number]; stopIndex: number }[] = [];
    let prev: [number, number] = [user.longitude, user.latitude];
    for (let i = 0; i < selectedRoute.route.length; i++) {
      const stop = selectedRoute.route[i];
      legs.push({ from: prev, to: [stop.lng, stop.lat], stopIndex: i });
      prev = [stop.lng, stop.lat];
    }
    legs.push({ from: prev, to: [user.longitude, user.latitude], stopIndex: -1 });

    Promise.all(
      legs.map((leg) =>
        fetchRoadPath(leg.from, leg.to).then((coords) => ({
          coordinates: coords,
          stopIndex: leg.stopIndex,
        }))
      )
    )
      .then((results) => {
        if (!cancelled) setRoadPaths(results);
      })
      .catch(() => {
        // fetchRoadPath already falls back to a straight line internally, but
        // guard here too so a route change never leaves routeLoading stuck.
        if (!cancelled) setRoadPaths([]);
      })
      .finally(() => {
        if (!cancelled) setRouteLoading(false);
      });

    return () => { cancelled = true; };
  }, [selectedRoute, user]);

  // Fetch distributor detail (with component offerings) when sidebar opens
  useEffect(() => {
    if (!selectedDist) {
      setDistDetail(null);
      setComponentSearch('');
      setCategoryFilter('');
      return;
    }
    setDistDetailLoading(true);
    distributorsAPI.get(selectedDist.id).then((res) => {
      setDistDetail(res.data);
      setDistDetailLoading(false);
    }).catch(() => setDistDetailLoading(false));
  }, [selectedDist]);

  // Fetch graph metrics AND single-source components when switching to Network Risk view
  useEffect(() => {
    if (mapView !== 'network-risk') return;

    let cancelled = false;

    // Fetch betweenness/pagerank metrics for marker sizing
    if (!graphMetrics) {
      setGraphMetricsLoading(true);
      setGraphMetricsError(false);
      graphAPI.metrics()
        .then((res) => {
          if (cancelled) return;
          setGraphMetrics(res.data);
          setGraphMetricsLoading(false);
        })
        .catch(() => {
          if (cancelled) return;
          setGraphMetricsLoading(false);
          setGraphMetricsError(true);
        });
    }

    // Fetch real single-source component list (unchanged behaviour)
    if (singleSourceComponents.length === 0) {
      benchmarkAPI.singleSourceComponents()
        .then((res) => {
          if (cancelled) return;
          const components: SingleSourceComponent[] = res.data.components ?? [];
          setSingleSourceComponents(components);
          setSingleSourceDistributorIds(new Set(components.map((c) => c.distributor_id)));
        })
        .catch(() => {
          // Silent fail — side panel shows empty state; no halos shown
        });
    }

    return () => { cancelled = true; };
  }, [mapView]); // eslint-disable-line react-hooks/exhaustive-deps

  // Cascade heatmap fetch. `cascadeFetched` distinguishes "haven't asked yet"
  // from "asked and the backend genuinely has no cascade data" (e.g. no
  // completed optimization runs) so the toggle/legend can show an honest
  // no-data state instead of implying a populated layer. `cascadeLoading` is
  // set from the toggle's onClick handler (an event, not this effect) right
  // before activation, so this effect body never calls setState outside of
  // the fetch's own callbacks.
  useEffect(() => {
    if (!cascadeActive) return;
    if (cascadeFetched) return; // already attempted this session
    let cancelled = false;
    benchmarkAPI.cascadeHeatmap()
      .then((res) => {
        if (cancelled) return;
        setCascadeHeatmapData(res.data.points);
        setCascadeNote(res.data.note ?? '');
        setCascadeRunId(res.data.run_id ?? null);
      })
      .catch(() => {
        // Fetch failed — treated the same as "no data available" below.
      })
      .finally(() => {
        if (cancelled) return;
        setCascadeLoading(false);
        setCascadeFetched(true);
      });
    return () => { cancelled = true; };
  }, [cascadeActive]); // eslint-disable-line react-hooks/exhaustive-deps

  // Scroll + highlight component row when a distributor marker is clicked.
  // componentRowRefs is keyed by each component's own component_id (unique per
  // row) — a distributor can be the sole source of several components, so we
  // scroll to the first matching row for the clicked distributor.
  useEffect(() => {
    if (selectedDistributorId === null) return;
    const target = singleSourceComponents.find((c) => c.distributor_id === selectedDistributorId);
    if (!target) return;
    const el = componentRowRefs.current.get(target.component_id);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [selectedDistributorId, singleSourceComponents]);

  const visibleDistributors = useMemo(
    () => showDomesticOnly ? distributors.filter((d) => d.is_domestic) : distributors,
    [distributors, showDomesticOnly]
  );

  // Network Risk color/size channels must discriminate across THIS graph's own
  // betweenness distribution, not a fixed 0–1 scale. The old 0.4/0.7 thresholds
  // (via lib/risk.ts's generic riskLabel) were calibrated for a min-max-normalised
  // score that was later removed from the graph builder — the live max betweenness
  // across all 92 distributors is ~0.291, so those cutoffs could never fire and
  // every marker rendered green/"low". See docs/OUTSTANDING_WORK.md item 3.
  // (That figure was ~0.246 until 2026-09-03, when a dead 20% link holdout was
  // removed from the graph builder and the graph gained 1,574 previously excluded
  // offers. The conclusion is unchanged: 0.291 is still nowhere near 0.4.)
  //
  // Fix: band by percentile rank within the observed distributor set (top decile /
  // next 30% / bottom 60%) and size markers relative to the observed max, not an
  // assumed ceiling. This is a RELATIVE ranking for this run's graph, not a
  // calibrated risk level — deliberately not reusing riskLabel's 0.4/0.7 semantics,
  // and deliberately not read as a probability (see the tooltip below and
  // optimization/stochastic.py's p_disruption, which is the calibrated quantity
  // for that purpose).
  const betweennessStats = useMemo(() => {
    if (!graphMetrics) return null;
    const values = Object.values(graphMetrics.betweenness);
    if (values.length === 0) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const quantile = (p: number) => {
      const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(p * (sorted.length - 1))));
      return sorted[idx];
    };
    return { max: sorted[sorted.length - 1], p90: quantile(0.9), p60: quantile(0.6), n: sorted.length };
  }, [graphMetrics]);

  type StructuralTier = 'low' | 'medium' | 'high';

  const structuralTier = useCallback(
    (btw: number): StructuralTier => {
      if (!betweennessStats) return 'low';
      if (btw >= betweennessStats.p90) return 'high';
      if (btw >= betweennessStats.p60) return 'medium';
      return 'low';
    },
    [betweennessStats]
  );

  const STRUCTURAL_TIER_LABELS: Record<StructuralTier, string> = {
    high: 'top 10% (decile) by betweenness',
    medium: 'next 30% by betweenness',
    low: 'bottom 60% by betweenness',
  };

  const routeGeoJSON = useMemo(() => buildRouteGeoJSON(roadPaths), [roadPaths]);

  // Frame the solved route rather than leaving the camera parked where it started.
  // INITIAL_VIEW is a fixed centre (-40, 30) at a fixed zoom: at 1440px that frames
  // North America and Europe, and at 390px the horizontal span collapses to open
  // ocean. The route was being drawn correctly the whole time and was simply
  // off-screen, so on a phone the map read as broken and took every upstream result
  // -- the CP-SAT solve, the Monte Carlo, the cross-dock consolidation -- down with it.
  useEffect(() => {
    if (!showRoutes || roadPaths.length === 0) return;
    const map = mapRef.current;
    if (!map) return;

    let west = Infinity;
    let south = Infinity;
    let east = -Infinity;
    let north = -Infinity;
    for (const path of roadPaths) {
      for (const [lng, lat] of path.coordinates) {
        if (!Number.isFinite(lng) || !Number.isFinite(lat)) continue;
        if (lng < west) west = lng;
        if (lng > east) east = lng;
        if (lat < south) south = lat;
        if (lat > north) north = lat;
      }
    }
    // A route whose legs all failed to resolve leaves the extent untouched; fitting
    // an infinite box would throw and blank the map, so do nothing and keep the
    // starting view.
    if (!Number.isFinite(west) || !Number.isFinite(east)) return;

    // The HUD runs along the bottom and the legend sits over the lower-left, so the
    // usable window is not the viewport. Pad asymmetrically instead of centring the
    // route underneath the overlays. maxZoom keeps a single-city route from zooming
    // to street level.
    const narrow = typeof window !== 'undefined' && window.innerWidth < 1024;
    map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      {
        padding: narrow
          ? { top: 96, bottom: 200, left: 24, right: 24 }
          : { top: 80, bottom: 140, left: 300, right: 80 },
        maxZoom: 7,
        duration: 800,
      },
    );
  }, [roadPaths, showRoutes]);

  const forwardGeoJSON = useMemo<GeoJSON.FeatureCollection>(() => ({
    type: 'FeatureCollection',
    features: routeGeoJSON.features.filter(
      (f) => (f.properties as { isReturn: boolean }).isReturn === false
    ),
  }), [routeGeoJSON]);

  const returnGeoJSON = useMemo<GeoJSON.FeatureCollection>(() => ({
    type: 'FeatureCollection',
    features: routeGeoJSON.features.filter(
      (f) => (f.properties as { isReturn: boolean }).isReturn === true
    ),
  }), [routeGeoJSON]);

  const handleMapClick = useCallback(
    (e: MapLayerMouseEvent) => {
      const features = e.features;
      if (!features || features.length === 0) {
        setLegPopup(null);
        return;
      }
      const feature = features[0];
      const stopIndex: number = feature.properties?.stopIndex ?? -1;
      if (stopIndex < 0 || !selectedRoute) return;
      // The clicked feature can belong to a route that has since been replaced
      // (switch routes and the old line survives a frame), so stopIndex may point
      // past the end of the current tour. Indexing blind threw an uncaught
      // TypeError that no boundary caught, and the popup silently never opened.
      const stop = selectedRoute.route[stopIndex];
      if (!stop) {
        setLegPopup(null);
        return;
      }
      setLegPopup({
        data: {
          distributorName: stop.distributor_name,
          city:            stop.city,
          state:           stop.state,
          country:         stop.country,
          legCostUsd:      stop.leg_cost_usd,
          legCo2eKg:       stop.leg_co2e_kg,
          legCarriedKg:    stop.leg_carried_kg,
          distanceKm:      stop.distance_km,
          components:      stop.components,
          legIndex:        stopIndex + 1,
          totalLegs:       selectedRoute.route.length,
        },
        position: { x: e.point.x, y: e.point.y },
      });
    },
    [selectedRoute]
  );

  const handleFlyTo = useCallback((lat: number, lng: number) => {
    mapRef.current?.flyTo({ center: [lng, lat], zoom: 9, duration: 1000 });
  }, []);

  const handleSearchSelect = useCallback((dist: DistributorPin) => {
    mapRef.current?.flyTo({ center: [dist.longitude, dist.latitude], zoom: 9, duration: 1000 });
    setSelectedDist(dist);
  }, []);

  const alternatives = multiResult?.alternatives ?? [];

  const domesticCount = distributors.filter((d) => d.is_domestic).length;
  const internationalCount = distributors.length - domesticCount;

  return (
    <div ref={mapContainerRef} className="flex h-full bg-slate-900 text-slate-100 relative overflow-hidden">
      <div className="flex-1 relative">
        {loading && !distributorsError && (
          <div className="absolute inset-0 z-30 flex items-center justify-center bg-slate-900/80">
            <div className="flex flex-col items-center gap-3">
              <div className="w-8 h-8 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
              <span className="text-blue-400 text-sm">Loading distributor network...</span>
            </div>
          </div>
        )}

        {distributorsError && (
          <div className="absolute inset-0 z-30 flex items-center justify-center bg-slate-900/80">
            <div className="flex flex-col items-center gap-3 max-w-xs text-center px-4">
              <span className="text-red-400 text-sm">{distributorsError}</span>
              <button
                onClick={() => {
                  setLoading(true);
                  setDistributorsError(null);
                  setDistributorsRetryKey((k) => k + 1);
                }}
                className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium transition-colors"
              >
                Retry
              </button>
            </div>
          </div>
        )}

        {/* Distributor search bar — top center. Stacked below the Routes /
            Network Risk toggle + zoom control on narrow and mid-width
            screens (where a centered max-w-sm bar would otherwise paint
            over both and make them unclickable); back to top-center once
            there's enough width (lg+) for everything to sit on one row
            without overlapping. */}
        {!loading && !distributorsError && (
          <div className="absolute top-28 lg:top-3 left-1/2 -translate-x-1/2 z-20 w-full max-w-sm px-4 pointer-events-auto">
            <DistributorSearchBar distributors={visibleDistributors} onSelect={handleSearchSelect} />
          </div>
        )}

        {routeLoading && (
          <div className="absolute top-16 left-1/2 -translate-x-1/2 z-20 flex items-center gap-2 bg-slate-900/90 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-300 pointer-events-none">
            <div className="w-3 h-3 rounded-full border border-blue-500 border-t-transparent animate-spin" />
            Fetching road routes...
          </div>
        )}

        <Map
          ref={mapRef}
          {...viewState}
          onMove={(e) => setViewState(e.viewState)}
          mapStyle={MAP_STYLE}
          attributionControl={false}
          style={{ width: '100%', height: '100%' }}
          // 'route-forward-hit' is the transparent 20px-wide companion to the visible
          // line. It existed and was never registered here, so the map only ever
          // hit-tested the 1.5-5px painted line: clicking dead-centre opened the leg
          // popup and +/-3px opened nothing. Both are listed because they share a
          // source and therefore carry the same stopIndex property.
          interactiveLayerIds={
            showRoutes && roadPaths.length ? ['route-forward-hit', 'route-forward'] : []
          }
          onClick={handleMapClick}
          cursor="grab"
        >
          <NavigationControl position="top-right" />

          {showRoutes && roadPaths.length > 0 && (
            <>
              <Source id="route-return" type="geojson" data={returnGeoJSON}>
                <Layer
                  id="route-return"
                  type="line"
                  paint={routeReturnPaint}
                  layout={routeForwardLayout}
                />
              </Source>

              <Source id="route-forward" type="geojson" data={forwardGeoJSON}>
                <Layer
                  id="route-forward-hit"
                  type="line"
                  paint={{ 'line-color': 'transparent', 'line-width': 20 }}
                  layout={routeForwardLayout}
                />
                <Layer
                  id="route-forward"
                  type="line"
                  paint={{
                    ...routeForwardPaint,
                    'line-color': STRATEGY_COLORS[selectedId ?? 'balanced'] ?? '#3b82f6',
                  }}
                  layout={routeForwardLayout}
                />
              </Source>
            </>
          )}

          {/* Cascade heatmap layer — only in Network Risk view when cascade toggle is active */}
          {cascadeActive && cascadeHeatmapData.length > 0 && (
            <Source
              id="cascade-heatmap-source"
              type="geojson"
              data={{
                type: 'FeatureCollection',
                features: cascadeHeatmapData.map((pt) => ({
                  type: 'Feature',
                  geometry: { type: 'Point', coordinates: [pt.lng, pt.lat] },
                  properties: { weight: pt.weight },
                })),
              }}
            >
              <Layer
                id="cascade-heatmap"
                type="heatmap"
                paint={{
                  'heatmap-weight': ['interpolate', ['linear'], ['get', 'weight'], 0, 0, 1, 1],
                  'heatmap-intensity': 1.5,
                  'heatmap-radius': 40,
                  'heatmap-opacity': 0.7,
                  'heatmap-color': [
                    'interpolate', ['linear'], ['heatmap-density'],
                    0,    '#440154',
                    0.25, '#3b528b',
                    0.5,  '#21918c',
                    0.75, '#5ec962',
                    1,    '#fde725',
                  ],
                } as any}
              />
            </Source>
          )}

          {visibleDistributors.map((dist) => {
            if (mapView === 'network-risk') {
              // Network Risk view — betweenness sizing + percentile-tier color.
              // Both channels are scaled against betweennessStats (the observed
              // distribution across all 92 distributors), not a fixed 0–1 ceiling —
              // see the comment above betweennessStats for why.
              const btw = graphMetrics ? (graphMetrics.betweenness[String(dist.id)] ?? 0) : 0;
              const maxBtw = betweennessStats?.max ?? 0;
              const pxSize = maxBtw > 0 ? Math.max(6, Math.min(22, 6 + (btw / maxBtw) * 16)) : 6;
              const riskTier = structuralTier(btw);
              const markerColor = RISK_COLORS[riskTier];
              // Sole-source halo: derived from real API data — distributor appears in
              // /benchmark/single-source-components response. NOT a betweenness threshold.
              const isSingleSource = singleSourceDistributorIds.has(dist.id);
              const singleSourceCount = singleSourceComponents.filter(
                (c) => c.distributor_id === dist.id
              ).length;
              return (
                <Marker key={`nr-${dist.id}`} longitude={dist.longitude} latitude={dist.latitude} anchor="center">
                  <div className="relative" style={{ width: pxSize, height: pxSize }}>
                    <div
                      onClick={() => {
                        setShowNetworkRiskPanel(true);
                        setSelectedDistributorId(dist.id);
                      }}
                      title={`${dist.name} — betweenness centrality: ${btw.toFixed(4)} (raw structural score, 0–1; not a percentage or failure probability) · ${STRUCTURAL_TIER_LABELS[riskTier]} of this network's ${betweennessStats?.n ?? 92} distributors${isSingleSource ? ` · sole source of ${singleSourceCount} component${singleSourceCount !== 1 ? 's' : ''}` : ''}`}
                      aria-label={`${dist.name}${isSingleSource ? `, sole source of ${singleSourceCount} single-source component${singleSourceCount !== 1 ? 's' : ''}, highest risk` : ''}`}
                      role="button"
                      tabIndex={0}
                      style={{
                        width: pxSize,
                        height: pxSize,
                        backgroundColor: markerColor,
                        borderRadius: '50%',
                        border: '2px solid rgba(255,255,255,0.2)',
                        cursor: 'pointer',
                      }}
                      className="hover:scale-110 transition-transform"
                    />
                    {isSingleSource && (
                      <div
                        className="absolute inset-0 rounded-full bg-red-500/60 animate-ping"
                        aria-hidden="true"
                      />
                    )}
                  </div>
                </Marker>
              );
            }
            // Routes view — existing marker JSX unchanged
            return (
              <Marker
                key={dist.id}
                longitude={dist.longitude}
                latitude={dist.latitude}
                anchor="center"
                onClick={(e) => {
                  e.originalEvent.stopPropagation();
                  setSelectedDist(dist);
                }}
              >
                <div
                  className="relative group cursor-pointer"
                  onMouseEnter={() => setHoveredDistId(dist.id)}
                  onMouseLeave={() => setHoveredDistId(null)}
                >
                  {/* Shape as well as colour. Both classes used to be circles that
                      differed only in hue — blue for domestic, amber for
                      international — which is exactly the distinction a
                      red-green-deficient viewer cannot make and a greyscale print
                      destroys. Domestic is now round, international square, and
                      the legend panel below names both shapes in words. */}
                  <div
                    className={`border-2 border-white/20 transition-all duration-150 shadow-md pointer-events-none ${
                      dist.is_domestic ? 'rounded-full' : 'rounded-[1px]'
                    } ${hoveredDistId === dist.id ? 'scale-[2.2] border-white/70' : ''}`}
                    style={{
                      width: Math.max(6, Math.min(14, 6 + dist.total_offers / 100)),
                      height: Math.max(6, Math.min(14, 6 + dist.total_offers / 100)),
                      backgroundColor: dist.is_domestic ? '#3b82f6' : '#f59e0b',
                    }}
                  />
                  {hoveredDistId === dist.id && (
                    <div className="absolute -top-8 left-1/2 -translate-x-1/2 bg-slate-800/95 text-white text-xs px-2 py-0.5 rounded whitespace-nowrap pointer-events-none z-50 border border-slate-600 shadow-lg select-none">
                      {dist.name}
                    </div>
                  )}
                </div>
              </Marker>
            );
          })}

          {hubs.map((hub) => {
            const isActiveHub =
              !!selectedRoute?.cross_dock?.enabled &&
              selectedRoute?.cross_dock?.hub_id === hub.id;
            return (
              <Marker
                key={`hub-${hub.id}`}
                longitude={hub.longitude}
                latitude={hub.latitude}
                anchor="center"
              >
                <div
                  data-testid="hub-marker"
                  title={`${hub.name} (${hub.city}, ${hub.state})${isActiveHub ? ' — consolidation hub for selected strategy' : ''}`}
                  className={`relative cursor-pointer hover:scale-150 transition-transform ${
                    isActiveHub ? 'hub-marker-pulse' : ''
                  }`}
                  style={{
                    width: isActiveHub ? '16px' : '12px',
                    height: isActiveHub ? '16px' : '12px',
                    backgroundColor: '#fbbf24',
                    border: '1.5px solid rgba(254, 243, 199, 0.9)',
                    transform: 'rotate(45deg)',
                    boxShadow: isActiveHub
                      ? undefined // controlled by hub-marker-pulse animation
                      : '0 0 8px 2px rgba(251, 191, 36, 0.45), 0 0 2px 0 rgba(0,0,0,0.6)',
                  }}
                />
              </Marker>
            );
          })}

          {user && (
            <Marker longitude={user.longitude} latitude={user.latitude} anchor="bottom">
              <div className="relative cursor-pointer group" title={user.factory_name}>
                <div className="w-5 h-5 rounded-full bg-white border-4 border-green-500 shadow-lg shadow-green-500/50 group-hover:scale-125 transition-transform" />
                <div className="absolute -top-7 left-1/2 -translate-x-1/2 bg-green-600 text-white text-xs px-2 py-0.5 rounded whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
                  {user.factory_name}
                </div>
              </div>
            </Marker>
          )}
        </Map>

        {selectedRoute && (
          <RouteMetricsBar
            totalCostUsd={selectedRoute.total_cost_usd}
            totalCo2eKg={selectedRoute.total_co2e_kg}
            etaP50={selectedRoute.eta_p50}
            stopCount={selectedRoute.route.length}
          />
        )}

        {/* Route strategy switcher */}
        {alternatives.length > 0 && (
          <div className="absolute top-4 left-4 z-10 pointer-events-auto">
            <div className="relative">
              <button
                onClick={() => setRouteDropdownOpen((v) => !v)}
                className="flex items-center gap-2 bg-black/60 backdrop-blur-lg border border-white/10 rounded-lg px-3 py-2 text-xs font-medium text-white hover:bg-black/70 transition-colors"
              >
                <div
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: STRATEGY_COLORS[selectedId ?? 'balanced'] }}
                />
                {selectedRoute?.label ?? 'Select Route'}
                <ChevronDown className={`w-3 h-3 transition-transform ${routeDropdownOpen ? 'rotate-180' : ''}`} />
              </button>

              {routeDropdownOpen && (
                <div className="absolute top-full left-0 mt-1 w-72 bg-slate-900/95 backdrop-blur-lg border border-slate-700 rounded-lg shadow-2xl overflow-hidden">
                  {alternatives.map((alt) => {
                    const isActive = alt.id === selectedId;
                    return (
                      <button
                        key={alt.id}
                        onClick={() => { setSelectedId(alt.id); setRouteDropdownOpen(false); }}
                        className={`w-full flex items-start gap-3 px-3 py-2.5 text-left transition-colors ${
                          isActive ? 'bg-blue-500/10' : 'hover:bg-slate-800'
                        }`}
                      >
                        <div
                          className="w-2.5 h-2.5 rounded-full mt-1 shrink-0"
                          style={{ backgroundColor: STRATEGY_COLORS[alt.id] }}
                        />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between">
                            <span className={`text-xs font-semibold ${isActive ? 'text-white' : 'text-slate-300'}`}>
                              {alt.label}
                            </span>
                            {multiResult?.recommended_id === alt.id && (
                              <span className="text-[9px] text-purple-400 bg-purple-400/10 px-1.5 py-0.5 rounded font-medium">REC</span>
                            )}
                          </div>
                          <div className="flex items-center gap-3 mt-0.5 text-[11px] text-slate-400">
                            <span>${alt.total_cost_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
                            <span>{alt.eta_p50}d ETA</span>
                            <span>{alt.total_co2e_kg.toFixed(1)} kg CO2</span>
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Network Risk view toggle — UI-SPEC section 8 */}
        <div
          className="absolute top-4 right-14 z-10 flex bg-slate-900/90 border border-slate-700 rounded-lg overflow-hidden text-xs font-semibold"
          role="tablist"
          aria-label="Map view"
        >
          <button
            role="tab"
            aria-selected={mapView === 'routes'}
            onClick={() => { setMapView('routes'); setShowNetworkRiskPanel(false); }}
            className={`px-3 py-2 min-h-[44px] transition-colors ${mapView === 'routes' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'}`}
          >
            Routes
          </button>
          <button
            role="tab"
            aria-selected={mapView === 'network-risk'}
            onClick={() => { setMapView('network-risk'); setShowNetworkRiskPanel(true); }}
            className={`px-3 py-2 min-h-[44px] min-w-[96px] transition-colors ${mapView === 'network-risk' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'}`}
          >
            {graphMetricsLoading ? (
              <span className="flex items-center gap-2">
                <div className="w-3 h-3 rounded-full border border-current border-t-transparent animate-spin" />
                Loading…
              </span>
            ) : (
              'Network Risk'
            )}
          </button>
        </div>

        {/* Cascade Risk sub-toggle — only visible in Network Risk view */}
        {mapView === 'network-risk' && (
          <button
            onClick={() => {
              const activating = !cascadeActive;
              if (activating && !cascadeFetched) setCascadeLoading(true);
              setCascadeActive(activating);
            }}
            title={cascadeActive && cascadeFetched && !cascadeLoading && cascadeHeatmapData.length === 0 ? 'No cascade risk data available yet — needs completed optimization runs to compute BOM-collapse probabilities' : undefined}
            className={`absolute top-14 right-14 z-10 px-3 py-2 min-h-[44px] text-xs font-semibold rounded-lg border transition-colors ${
              cascadeActive && cascadeHeatmapData.length > 0
                ? 'bg-slate-700 text-white border-slate-500'
                : 'bg-slate-900/90 text-slate-300 border-slate-700 hover:bg-slate-800'
            }`}
          >
            Cascade Risk
            {cascadeActive && cascadeLoading && (
              <span className="ml-1.5 inline-block w-2.5 h-2.5 rounded-full border border-current border-t-transparent animate-spin align-[-1px]" />
            )}
            {cascadeActive && cascadeFetched && !cascadeLoading && cascadeHeatmapData.length === 0 && (
              <span className="ml-1 text-slate-400 font-normal">(no data)</span>
            )}
            {cascadeActive && cascadeHeatmapData.length > 0 ? '\u2713' : ''}
          </button>
        )}

        {selectedRoute && mapView === 'routes' && (
          <button
            onClick={() => {
              setTimelineOpen((v) => !v);
              setSelectedDist(null);
            }}
            className={`absolute top-16 right-14 z-10 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all pointer-events-auto border ${
              timelineOpen
                ? 'bg-blue-600 text-white border-blue-500'
                : 'bg-slate-800/90 text-slate-300 border-slate-700 hover:bg-slate-700'
            }`}
          >
            <Route className="w-3.5 h-3.5" />
            Route Stops
          </button>
        )}

        {/* Map legend / filter panel */}
        <div className="absolute bottom-4 left-4 z-10 pointer-events-auto">
          <div className="bg-slate-900/95 backdrop-blur-md border border-slate-700/60 rounded-xl shadow-2xl overflow-hidden w-64 p-4 space-y-3">
            <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
              Distributor Network
            </div>

            {/* Circle vs square, matching the markers on the map itself. These two
                rows used to be identical circles separated only by hue. */}
            {/* flex-wrap is load-bearing: the panel is a fixed w-64 with
                overflow-hidden, and the two rows with their shape words are wider
                than its content box — without wrapping the second one is cut off
                by the panel rather than overflowing anything the gate can see. */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
              <span className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-blue-500" aria-hidden="true" />
                <span className="text-slate-300">Domestic ({domesticCount}) &middot; round</span>
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-[1px] bg-amber-500" aria-hidden="true" />
                <span className="text-slate-300">Int'l ({internationalCount}) &middot; square</span>
              </span>
            </div>

            <button
              onClick={() => setShowDomesticOnly((v) => !v)}
              className={`flex items-center gap-2 min-h-[44px] text-xs font-medium transition-colors ${
                showDomesticOnly ? 'text-blue-400' : 'text-slate-400'
              }`}
            >
              <Globe className="w-3.5 h-3.5" />
              {showDomesticOnly ? 'Show all distributors' : 'Domestic only'}
            </button>

            {selectedRoute && (
              <button
                onClick={() => setShowRoutes((v) => !v)}
                className={`flex items-center gap-2 text-xs font-medium transition-colors ${
                  showRoutes ? 'text-blue-400' : 'text-slate-400'
                }`}
              >
                <div className={`w-6 h-0.5 rounded-full transition-colors ${showRoutes ? 'bg-blue-500' : 'bg-slate-600'}`} />
                {showRoutes ? 'Hide' : 'Show'} route paths
              </button>
            )}

            {hubs.length > 0 && (
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <div
                  className="w-2.5 h-2.5 bg-amber-400 border border-amber-200/80 flex-shrink-0"
                  style={{ transform: 'rotate(45deg)' }}
                />
                Cross-Dock Hubs ({hubs.length})
              </div>
            )}

            {user && (
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <div className="w-3 h-3 rounded-full bg-white border-2 border-green-500 flex-shrink-0" />
                Your Factory
              </div>
            )}

            {/* Network Risk marker legend — bands are percentile rank within THIS
                graph's own betweenness distribution, not fixed risk thresholds.
                See betweennessStats / structuralTier above for why fixed 0.4/0.7
                cutoffs can never fire on this data. */}
            {mapView === 'network-risk' && betweennessStats && (
              <div>
                <p className="text-xs font-semibold text-slate-400 mb-1.5">
                  Marker color &amp; size (betweenness)
                </p>
                <div className="space-y-1">
                  <span className="flex items-center gap-1.5 text-xs">
                    <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: RISK_COLORS.high }} />
                    <span className="text-slate-300">Top 10% (decile) by betweenness</span>
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: RISK_COLORS.medium }} />
                    <span className="text-slate-300">Next 30% by betweenness</span>
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: RISK_COLORS.low }} />
                    <span className="text-slate-300">Bottom 60% by betweenness</span>
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1.5 leading-snug">
                  Ranked within this network's {betweennessStats.n} distributors
                  (observed max {betweennessStats.max.toFixed(4)}) — a relative
                  ranking, not a calibrated risk level or failure probability.
                </p>
              </div>
            )}

            {/* Cascade heatmap legend — only draws the gradient once the layer
                actually has points; otherwise shows an explicit no-data note
                rather than a legend describing a layer with nothing on it. */}
            {mapView === 'network-risk' && cascadeActive && (
              cascadeHeatmapData.length > 0 ? (
                <div>
                  <p className="text-xs font-semibold text-slate-400 mb-2">Cascade risk</p>
                  <div className="flex items-center gap-1">
                    <div
                      className="flex-1 h-3 rounded"
                      style={{ background: 'linear-gradient(to right, #440154, #3b528b, #21918c, #5ec962, #fde725)' }}
                    />
                  </div>
                  <div className="flex justify-between text-xs text-slate-400 mt-1">
                    <span>Least exposed</span>
                    <span>Most exposed</span>
                  </div>
                  <p className="text-xs text-slate-400 mt-1.5 leading-snug">
                    Relative index, not a calibrated probability — each distributor's
                    weight is its mean modelled BOM-collapse exposure, max-normalized
                    against the single most-exposed distributor
                    {cascadeRunId != null ? ` in run_id=${cascadeRunId}` : ''}.{' '}
                    {cascadeNote}
                  </p>
                  <p className="text-xs text-amber-400/80 mt-1.5 leading-snug">
                    <span className="font-medium">Known limitation:</span> this run
                    predates the disruption-probability calibration fix described on
                    the CVaR Frontier page — the legacy model it used saturates at its
                    ceiling for the network's most-central distributors rather than
                    reflecting a calibrated rate. Read this layer as a ranking of
                    relative exposure, not an absolute 0–100% BOM-collapse percentage.
                  </p>
                </div>
              ) : cascadeFetched && !cascadeLoading ? (
                <div>
                  <p className="text-xs font-semibold text-slate-400 mb-1">Cascade risk</p>
                  <p className="text-xs text-slate-400 leading-snug">
                    No cascade risk data yet — this layer needs completed optimization runs to compute BOM-collapse probabilities.
                  </p>
                </div>
              ) : null
            )}
          </div>
        </div>

        <RouteLegPopup
          data={legPopup?.data ?? null}
          position={legPopup?.position ?? { x: 0, y: 0 }}
          onClose={() => setLegPopup(null)}
          containerRef={mapContainerRef}
        />
      </div>

      {/* Network Risk side panel — right-docked, auto-opens in Network Risk view */}
      {mapView === 'network-risk' && showNetworkRiskPanel && (
        <div className="absolute top-0 right-0 h-full w-96 bg-slate-900/98 backdrop-blur-md border-l border-slate-700/60 z-20 flex flex-col">
          {/* Header */}
          <div className="p-4 border-b border-slate-700 flex items-start justify-between">
            <div>
              <h2 className="text-3xl font-semibold text-white">Single-source components</h2>
              <p className="text-sm text-slate-400 mt-1">
                Components available from exactly one distributor (single-source exposure). These fail the BOM if that distributor goes offline.
              </p>
              {graphMetricsError && (
                <p className="text-xs text-slate-400">Risk data unavailable — reload to retry</p>
              )}
            </div>
            <button
              aria-label="Close network risk panel"
              onClick={() => {
                setShowNetworkRiskPanel(false);
                setSelectedDistributorId(null);
              }}
              className="text-slate-400 hover:text-white ml-2 p-1 rounded hover:bg-slate-700 transition-colors"
            >
              <X size={16} />
            </button>
          </div>

          {/* Network connectivity — algebraic connectivity (Fiedler value / λ₂).
              Reported as two clearly-labeled numbers: the whole-graph value is
              exactly 0.0 because this supplier network is genuinely disconnected;
              the giant-component value is the informative one. Never show a single
              unlabeled "Fiedler" number — see docs/GAP_AUDIT_2026-07-01.md 0.7. */}
          {graphMetrics && (
            <div className="px-4 py-3 border-b border-slate-700/50 flex-shrink-0">
              <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
                Network connectivity
              </p>
              <div className="grid grid-cols-2 gap-2">
                <div className="bg-slate-800/60 rounded-lg p-2.5">
                  <div className="text-lg font-bold text-white">
                    {graphMetrics.fiedler_giant_component.toFixed(3)}
                  </div>
                  <div className="text-[11px] text-slate-400 mt-0.5 leading-tight">
                    λ₂ (giant component)
                  </div>
                </div>
                <div className="bg-slate-800/60 rounded-lg p-2.5">
                  <div className="text-lg font-bold text-white">
                    {graphMetrics.n_connected_components}
                  </div>
                  <div className="text-[11px] text-slate-400 mt-0.5 leading-tight">
                    connected components
                  </div>
                </div>
              </div>
              <p className="text-xs text-slate-400 mt-2 leading-snug">
                Giant component: {graphMetrics.giant_component_size} of{' '}
                {graphMetrics.n_distributors + graphMetrics.n_components} nodes (
                {(graphMetrics.giant_component_fraction * 100).toFixed(0)}%). Whole-graph λ₂ ={' '}
                {graphMetrics.fiedler_whole_graph.toFixed(1)} — exactly zero because the network is
                split into {graphMetrics.n_connected_components} disconnected pieces; the
                giant-component λ₂ above measures how tightly-knit the main network is.
              </p>
              {graphMetrics.centrality_notes?.betweenness && (
                <p className="text-xs text-slate-400 mt-2 leading-snug">
                  <span className="text-slate-400">Marker size & tint (betweenness).</span>{' '}
                  {graphMetrics.centrality_notes.betweenness}
                </p>
              )}
              {betweennessStats && (
                <p className="text-xs text-slate-400 mt-2 leading-snug">
                  Color and size bands are this network's own percentile rank (top
                  10% / next 30% / bottom 60%), not a fixed 0–1 scale — the observed
                  max betweenness across all {betweennessStats.n} distributors is
                  only {betweennessStats.max.toFixed(4)}, so a fixed threshold like
                  0.4 could never be reached. See the map legend for the color key.
                </p>
              )}
            </div>
          )}

          {/* Body */}
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {singleSourceComponents.length === 0 ? (
              <p className="text-slate-400 text-xs text-center mt-8">
                No single-source components detected in the current graph.
              </p>
            ) : (
              singleSourceComponents.map((comp) => {
                // Find the distributor's coordinates from the existing distributors list
                // for flyTo on click
                const dist = distributors.find((d) => d.id === comp.distributor_id);
                return (
                  <button
                    key={comp.component_id}
                    ref={(el) => {
                      // Keyed by the component's own id — a distributor can be the
                      // sole source of several components, so distributor_id is not
                      // unique here and would cause rows to overwrite each other's ref.
                      if (el) componentRowRefs.current.set(comp.component_id, el);
                      else componentRowRefs.current.delete(comp.component_id);
                    }}
                    onClick={() => {
                      if (dist && mapRef.current) {
                        mapRef.current.flyTo({ center: [dist.longitude, dist.latitude], zoom: 5, duration: 800 });
                      }
                    }}
                    className={`w-full text-left bg-slate-800/40 hover:bg-slate-800/70 rounded-lg px-3 py-2 border-l-2 border-red-500 transition-colors ${
                      selectedDistributorId === comp.distributor_id ? 'ring-1 ring-red-400' : ''
                    }`}
                  >
                    {/* UI-SPEC § 8: {MPN} · {manufacturer} · only source: {distributor_name} */}
                    <p className="text-xs font-mono text-white">{comp.mpn}</p>
                    <p className="text-xs text-slate-400">{comp.manufacturer}</p>
                    <p className="text-sm text-slate-300">only source: {comp.distributor_name}</p>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* Distributor detail sidebar */}
      {selectedDist && (() => {
        const allComponents = distDetail?.top_components ?? [];
        const categories = [...new Set(allComponents.map((c) => c.category))].sort();
        const filtered = allComponents.filter((c) => {
          const q = componentSearch.toLowerCase();
          const matchesSearch = !q || c.mpn.toLowerCase().includes(q) || c.manufacturer.toLowerCase().includes(q) || c.category.toLowerCase().includes(q);
          const matchesCategory = !categoryFilter || c.category === categoryFilter;
          return matchesSearch && matchesCategory;
        });

        return (
          <div className="absolute top-0 right-0 h-full w-96 bg-slate-900/98 backdrop-blur-md border-l border-slate-700/60 shadow-2xl z-20 flex flex-col pointer-events-auto">
            {/* Header */}
            <div className="flex items-start justify-between p-5 border-b border-slate-700/50 flex-shrink-0">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span
                    aria-hidden="true"
                    className={`w-2.5 h-2.5 flex-shrink-0 ${
                      selectedDist.is_domestic ? 'rounded-full' : 'rounded-[1px]'
                    }`}
                    style={{ backgroundColor: selectedDist.is_domestic ? '#3b82f6' : '#f59e0b' }}
                  />
                  <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
                    {selectedDist.is_domestic ? 'Domestic' : 'International'} Distributor
                  </span>
                </div>
                <h2 className="text-sm font-semibold text-white leading-tight truncate">{selectedDist.name}</h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  {[selectedDist.city, selectedDist.state, selectedDist.country].filter(Boolean).join(', ')}
                </p>
              </div>
              <button
                onClick={() => setSelectedDist(null)}
                className="ml-3 p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-700 transition-colors flex-shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Stats */}
            <div className="grid grid-cols-3 gap-2 p-4 border-b border-slate-700/50 flex-shrink-0">
              <div className="bg-slate-800/60 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-white">{selectedDist.total_offers}</div>
                <div className="text-[11px] text-slate-400 mt-0.5">Offers</div>
              </div>
              <div className="bg-slate-800/60 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-white">{selectedDist.total_stock.toLocaleString()}</div>
                <div className="text-[11px] text-slate-400 mt-0.5">Total Stock</div>
              </div>
              <div className="bg-slate-800/60 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-white flex items-center justify-center gap-1">
                  <MapPin className="w-3.5 h-3.5 text-slate-400" />
                  <span className="text-xs">{selectedDist.latitude.toFixed(1)}°</span>
                </div>
                <div className="text-[11px] text-slate-400 mt-0.5">{selectedDist.longitude.toFixed(1)}° Lng</div>
              </div>
            </div>

            {/* Component offerings section */}
            <div className="flex flex-col flex-1 min-h-0">
              {/* Section header */}
              <div className="px-4 pt-3 pb-2 flex-shrink-0">
                <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                  <Package className="w-3 h-3" />
                  Component Offerings
                  {distDetailLoading && (
                    <div className="w-3 h-3 rounded-full border border-blue-500 border-t-transparent animate-spin ml-1" />
                  )}
                </div>

                {/* Search */}
                <div className="relative mb-2">
                  <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
                  <input
                    type="text"
                    placeholder="Search MPN, manufacturer..."
                    value={componentSearch}
                    onChange={(e) => setComponentSearch(e.target.value)}
                    className="w-full pl-8 pr-3 py-1.5 text-xs rounded-lg bg-slate-800/80 border border-slate-700/60 text-white placeholder-slate-500 focus:outline-none focus:border-blue-500/60 transition-colors"
                  />
                  {componentSearch && (
                    <button
                      onClick={() => setComponentSearch('')}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-white"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  )}
                </div>

                {/* Category filter */}
                {categories.length > 0 && (
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <Tag className="w-3 h-3 text-slate-400 flex-shrink-0" />
                    <button
                      onClick={() => setCategoryFilter('')}
                      className={`text-[11px] px-2 py-0.5 rounded-full transition-colors ${!categoryFilter ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30' : 'text-slate-400 hover:text-slate-300'}`}
                    >
                      All
                    </button>
                    {categories.slice(0, 5).map((cat) => (
                      <button
                        key={cat}
                        onClick={() => setCategoryFilter(cat === categoryFilter ? '' : cat)}
                        className={`text-[11px] px-2 py-0.5 rounded-full transition-colors truncate max-w-[90px] ${cat === categoryFilter ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30' : 'text-slate-400 hover:text-slate-300'}`}
                        title={cat}
                      >
                        {cat}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Component list */}
              <div className="flex-1 overflow-y-auto px-2 pb-4 space-y-1 scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
                {distDetailLoading ? (
                  <div className="flex flex-col items-center justify-center h-32 gap-2">
                    <div className="w-6 h-6 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
                    <span className="text-xs text-slate-400">Loading offerings...</span>
                  </div>
                ) : filtered.length === 0 ? (
                  <div className="flex flex-col items-center justify-center h-32 text-slate-400 text-xs">
                    {allComponents.length === 0 ? 'No offerings found' : 'No matches for your search'}
                  </div>
                ) : (
                  filtered.map((comp) => (
                    <div
                      key={comp.component_id}
                      className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-slate-800/40 hover:bg-slate-800/70 transition-colors border border-transparent hover:border-slate-700/50 group"
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs font-mono font-semibold text-white truncate">{comp.mpn}</span>
                        </div>
                        <div className="flex items-center gap-1.5 mt-0.5">
                          <span className="text-[11px] text-slate-400 truncate">{comp.manufacturer}</span>
                          <span className="text-[11px] text-slate-400">·</span>
                          <span className="text-[11px] text-blue-500/70 truncate">{comp.category}</span>
                        </div>
                        {comp.sku && (
                          <div className="text-[9px] text-slate-400 font-mono mt-0.5 truncate">SKU: {comp.sku}</div>
                        )}
                      </div>
                      <div className="flex-shrink-0 text-right ml-3">
                        <div className="text-xs font-semibold text-green-400">
                          ${comp.price.toFixed(2)}
                        </div>
                        <div className="text-[11px] text-slate-400 mt-0.5">
                          {comp.stock.toLocaleString()} in stock
                        </div>
                      </div>
                    </div>
                  ))
                )}
              </div>

              {/* Footer count */}
              {!distDetailLoading && allComponents.length > 0 && (
                <div className="px-4 py-2 border-t border-slate-700/40 flex-shrink-0">
                  <span className="text-[11px] text-slate-400">
                    Showing {filtered.length} of {allComponents.length} offerings
                    {allComponents.length === 20 ? ' (top 20 by stock)' : ''}
                  </span>
                </div>
              )}
            </div>
          </div>
        );
      })()}

      {selectedRoute && (
        <RouteTimeline
          route={selectedRoute.route}
          totalCost={selectedRoute.total_cost_usd}
          totalCo2={selectedRoute.total_co2e_kg}
          etaP50={selectedRoute.eta_p50}
          open={timelineOpen}
          onClose={() => setTimelineOpen(false)}
          onFlyTo={handleFlyTo}
          routeLegsNote={selectedRoute.route_legs_note}
          transportCostBasis={selectedRoute.transport_cost_basis}
        />
      )}
    </div>
  );
}
