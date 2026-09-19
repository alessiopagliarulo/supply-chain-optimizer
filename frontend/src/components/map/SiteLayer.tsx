import { useEffect, useRef } from 'react';
import { useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet.markercluster';
import 'leaflet.markercluster/dist/MarkerCluster.css';
import { placeLabel, type Site } from '../../lib/sites';

/**
 * How a site is drawn. `selected` is the site the reader opened; on the route
 * planner `depot` and `stop` mark the plan and `dim` a site the depot cannot reach.
 */
export type SiteTone = 'default' | 'selected' | 'depot' | 'stop' | 'dim';

interface SiteLayerProps {
  sites: Site[];
  toneOf: (site: Site) => SiteTone;
  onSelect: (site: Site) => void;
  /** Merge nearby sites into one numbered bubble when zoomed out (the Map page). */
  cluster?: boolean;
  /** Extra line in each marker's tooltip, e.g. "Click to add as a destination". */
  hintOf?: (site: Site) => string;
}

const count = (n: number) => `${n} distributor${n === 1 ? '' : 's'}`;

function siteIcon(site: Site, tone: SiteTone): L.DivIcon {
  const n = site.distributors.length;
  const size = n > 1 ? 'group' : 'single';
  return L.divIcon({
    className: 'site-marker-hit',
    // A 44px hit area around a smaller visible dot, so the touch target is honest.
    iconSize: [44, 44],
    iconAnchor: [22, 22],
    html: `<span class="site-marker site-marker--${tone} site-marker--${size}">${n > 1 ? n : ''}</span>`,
  });
}

/** Draws one marker per site, optionally clustered, with Leaflet directly. */
export default function SiteLayer({ sites, toneOf, onSelect, cluster = false, hintOf }: SiteLayerProps) {
  const map = useMap();
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  useEffect(() => {
    const group: L.LayerGroup = cluster
      ? L.markerClusterGroup({
          maxClusterRadius: 44,
          showCoverageOnHover: false,
          spiderfyOnMaxZoom: false,
          iconCreateFunction: (c) => {
            const markers = c.getAllChildMarkers();
            const n = markers.reduce((s, m) => s + ((m.options as { distributors?: number }).distributors ?? 1), 0);
            return L.divIcon({
              className: 'site-marker-hit',
              iconSize: [44, 44],
              iconAnchor: [22, 22],
              html: `<span class="site-marker site-marker--cluster" title="${count(n)} at ${markers.length} locations. Click to zoom in.">${n}<span class="sr-only"> distributors at ${markers.length} locations, zoom in</span></span>`,
            });
          },
        })
      : L.layerGroup();
    for (const site of sites) {
      const tone = toneOf(site);
      const hint = hintOf?.(site);
      const title = `${placeLabel(site)}: ${count(site.distributors.length)}${hint ? `. ${hint}` : ''}`;
      const marker = L.marker([site.latitude, site.longitude], {
        icon: siteIcon(site, tone),
        title,
        alt: title,
        keyboard: true,
        riseOnHover: true,
        zIndexOffset: tone === 'depot' ? 1000 : tone === 'selected' ? 900 : tone === 'stop' ? 500 : 0,
        distributors: site.distributors.length,
      } as L.MarkerOptions & { distributors: number });
      marker.on('click', () => onSelectRef.current(site));
      group.addLayer(marker);
    }
    group.addTo(map);
    return () => {
      group.remove();
    };
  }, [map, sites, toneOf, cluster, hintOf]);

  return null;
}
