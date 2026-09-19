import { useEffect, useMemo } from 'react';
import L, { type LatLngTuple } from 'leaflet';
import { useMap } from './mapContext';
import type { PlacesSolveResponse } from '../../services/api';
import { mapRouteColor } from '../../lib/colors';
import { siteKey } from '../../lib/sites';

interface RouteLayerProps {
  plan: PlacesSolveResponse;
  /** Route to emphasise (the others fade); null draws all alike. */
  focus: number | null;
}

/** "3", "3-5" or "3, 6": a route's visit numbers at one point. */
function visitLabel(orders: number[]): string {
  if (orders.length === 1) return String(orders[0]);
  const contiguous = orders.every((o, i) => i === 0 || o === orders[i - 1] + 1);
  return contiguous ? `${orders[0]}-${orders[orders.length - 1]}` : orders.join(', ');
}

/** Tooltip content as a text node: distributor names never reach Leaflet as HTML. */
function textTip(text: string): HTMLElement {
  const el = document.createElement('span');
  el.textContent = text;
  return el;
}

interface Badge {
  ri: number;
  lat: number;
  lng: number;
  label: string;
  names: string[];
  shift: number;
}

/**
 * Each truck's route as a straight line through its stops (a great-circle chord, not
 * a road path), plus a numbered badge at every stop giving the visiting order.
 * Stops that share a point on the same route share one badge; routes that share a
 * point are nudged apart so every badge stays readable.
 */
export default function RouteLayer({ plan, focus }: RouteLayerProps) {
  const map = useMap();

  const lines = useMemo(
    () =>
      plan.routes.map((r) => {
        const pts: LatLngTuple[] = [[plan.depot.latitude, plan.depot.longitude]];
        for (const i of r.stops) pts.push([plan.stops[i].latitude, plan.stops[i].longitude]);
        pts.push([plan.depot.latitude, plan.depot.longitude]);
        return pts;
      }),
    [plan],
  );

  const badges = useMemo(() => {
    const at = new Map<string, Map<number, { orders: number[]; names: string[]; lat: number; lng: number }>>();
    plan.routes.forEach((r, ri) =>
      r.stops.forEach((si, k) => {
        const s = plan.stops[si];
        const key = siteKey(s);
        const perRoute = at.get(key) ?? new Map();
        at.set(key, perRoute);
        const entry = perRoute.get(ri) ?? { orders: [], names: [], lat: s.latitude, lng: s.longitude };
        perRoute.set(ri, entry);
        entry.orders.push(k + 1);
        entry.names.push(s.name);
      }),
    );
    const out: Badge[] = [];
    for (const perRoute of at.values()) {
      const entries = [...perRoute.entries()];
      entries.forEach(([ri, v], j) =>
        out.push({ ri, lat: v.lat, lng: v.lng, label: visitLabel(v.orders), names: v.names, shift: (j - (entries.length - 1) / 2) * 26 }),
      );
    }
    return out;
  }, [plan]);

  useEffect(() => {
    const group = L.layerGroup();
    lines.forEach((pts, i) => {
      const faded = focus !== null && focus !== i;
      L.polyline(pts, { color: '#ffffff', weight: 7, opacity: faded ? 0.25 : 0.9, interactive: false }).addTo(group);
      L.polyline(pts, { color: mapRouteColor(i), weight: focus === i ? 5 : 3.5, opacity: faded ? 0.25 : 1 })
        .bindTooltip(textTip(`Truck ${i + 1}`), { sticky: true })
        .addTo(group);
    });
    for (const b of badges) {
      L.marker([b.lat, b.lng], {
        keyboard: false,
        opacity: focus !== null && focus !== b.ri ? 0.35 : 1,
        zIndexOffset: 600,
        icon: L.divIcon({
          className: 'stop-badge-hit',
          iconSize: [30, 30],
          iconAnchor: [15 - b.shift, 15],
          // Only a colour from a fixed palette and digits reach this HTML.
          html: `<span class="stop-badge" style="background:${mapRouteColor(b.ri)}">${b.label}</span>`,
        }),
      })
        .bindTooltip(textTip(`Truck ${b.ri + 1}, stop ${b.label}: ${b.names.join(', ')}`), {
          direction: 'top',
          offset: [b.shift, -14],
        })
        .addTo(group);
    }
    group.addTo(map);
    return () => {
      group.remove();
    };
  }, [map, lines, badges, focus]);

  return null;
}
