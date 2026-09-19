import { Fragment, useMemo } from 'react';
import { Marker, Polyline, Tooltip } from 'react-leaflet';
import L, { type LatLngTuple } from 'leaflet';
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
  const contiguous = orders.every((o, i) => i === 0 || o === orders[i - 1] + 1);
  if (orders.length === 1) return String(orders[0]);
  return contiguous ? `${orders[0]}-${orders[orders.length - 1]}` : orders.join(', ');
}

/**
 * Each truck's route as a straight line through its stops (a great-circle chord, not
 * a road path), plus a numbered badge at every stop giving the visiting order.
 * Stops that share a point on the same route share one badge; routes that share a
 * point are nudged apart so every badge stays readable.
 */
export default function RouteLayer({ plan, focus }: RouteLayerProps) {
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
    // point -> route -> visit numbers and names
    const at = new Map<string, Map<number, { orders: number[]; names: string[]; lat: number; lng: number }>>();
    plan.routes.forEach((r, ri) =>
      r.stops.forEach((si, k) => {
        const s = plan.stops[si];
        const key = siteKey(s);
        if (!at.has(key)) at.set(key, new Map());
        const perRoute = at.get(key)!;
        if (!perRoute.has(ri)) perRoute.set(ri, { orders: [], names: [], lat: s.latitude, lng: s.longitude });
        perRoute.get(ri)!.orders.push(k + 1);
        perRoute.get(ri)!.names.push(s.name);
      }),
    );
    const out: { key: string; ri: number; lat: number; lng: number; label: string; names: string[]; shift: number }[] = [];
    for (const [key, perRoute] of at) {
      const entries = [...perRoute.entries()];
      entries.forEach(([ri, v], j) =>
        out.push({ key: `${key}:${ri}`, ri, lat: v.lat, lng: v.lng, label: visitLabel(v.orders), names: v.names, shift: (j - (entries.length - 1) / 2) * 26 }),
      );
    }
    return out;
  }, [plan]);

  return (
    <>
      {lines.map((pts, i) => {
        const faded = focus !== null && focus !== i;
        return (
          <Fragment key={i}>
            <Polyline positions={pts} pathOptions={{ color: '#ffffff', weight: 7, opacity: faded ? 0.25 : 0.9 }} interactive={false} />
            <Polyline positions={pts} pathOptions={{ color: mapRouteColor(i), weight: focus === i ? 5 : 3.5, opacity: faded ? 0.25 : 1 }}>
              <Tooltip sticky>{`Truck ${i + 1}`}</Tooltip>
            </Polyline>
          </Fragment>
        );
      })}
      {badges.map((b) => (
        <Marker
          key={b.key}
          position={[b.lat, b.lng]}
          keyboard={false}
          opacity={focus !== null && focus !== b.ri ? 0.35 : 1}
          zIndexOffset={600}
          icon={L.divIcon({
            className: 'stop-badge-hit',
            iconSize: [30, 30],
            iconAnchor: [15 - b.shift, 15],
            html: `<span class="stop-badge" style="background:${mapRouteColor(b.ri)}">${b.label}</span>`,
          })}
        >
          <Tooltip direction="top" offset={[b.shift, -14]}>{`Truck ${b.ri + 1}, stop ${b.label}: ${b.names.join(', ')}`}</Tooltip>
        </Marker>
      ))}
    </>
  );
}
