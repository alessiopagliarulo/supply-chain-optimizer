import { useEffect, type ReactNode } from 'react';
import { MapContainer, TileLayer, useMap } from 'react-leaflet';
import { latLngBounds, type LatLngTuple } from 'leaflet';
import 'leaflet/dist/leaflet.css';

/**
 * OpenStreetMap's standard tiles: free, no API key. The tile usage policy asks for a
 * visible attribution, which is drawn below as a plain paragraph (Leaflet's own control
 * is off) so its link reads as an inline text link.
 */
const OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';

interface BaseMapProps {
  /** Accessible name for the map region. */
  label: string;
  className?: string;
  children?: ReactNode;
}

export default function BaseMap({ label, className = '', children }: BaseMapProps) {
  return (
    <div role="region" aria-label={label} className={`relative isolate overflow-hidden ${className}`}>
      <MapContainer
        center={[30, 10]}
        zoom={2}
        minZoom={1}
        maxZoom={18}
        worldCopyJump
        maxBounds={[
          [-85, -540],
          [85, 540],
        ]}
        maxBoundsViscosity={1}
        attributionControl={false}
        className="h-full w-full"
      >
        <TileLayer url={OSM_TILES} maxZoom={19} />
        <FillHeight />
        {children}
      </MapContainer>
      <p className="absolute bottom-0 right-0 z-[1000] rounded-tl bg-white/90 px-2 py-0.5 text-xs text-slate-700">
        {'© '}
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer" className="underline text-slate-800">
          OpenStreetMap
        </a>
        {' contributors'}
      </p>
    </div>
  );
}

/**
 * Never zoom out so far that the world is shorter than the map, which would leave grey
 * bands above and below it on a tall screen. Re-measured whenever the map resizes.
 */
function FillHeight() {
  const map = useMap();
  useEffect(() => {
    const apply = () => {
      const h = map.getSize().y;
      const z = Math.max(1, Math.ceil(Math.log2(h / 256)));
      map.setMinZoom(z);
      if (map.getZoom() < z) map.setZoom(z);
    };
    apply();
    map.on('resize', apply);
    return () => {
      map.off('resize', apply);
    };
  }, [map]);
  return null;
}

/**
 * Fit the view to `points` whenever `fitKey` changes (not on every render, so the
 * reader's own panning and zooming is left alone).
 */
export function FitBounds({ points, fitKey, maxZoom = 9 }: { points: LatLngTuple[]; fitKey: string; maxZoom?: number }) {
  const map = useMap();
  useEffect(() => {
    if (!points.length) return;
    map.fitBounds(latLngBounds(points), { padding: [40, 40], maxZoom });
    // `points` is read through fitKey on purpose; see the doc comment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, fitKey, maxZoom]);
  return null;
}

/** Pan to a point without changing the zoom, unless it is zoomed out further than `minZoom`. */
export function FlyTo({ target, minZoom = 6 }: { target: { latitude: number; longitude: number; key: string } | null; minZoom?: number }) {
  const map = useMap();
  useEffect(() => {
    if (!target) return;
    map.flyTo([target.latitude, target.longitude], Math.max(map.getZoom(), minZoom), { duration: 0.6 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, target?.key]);
  return null;
}
