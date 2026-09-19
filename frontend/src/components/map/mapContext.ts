import { createContext, useContext } from 'react';
import type L from 'leaflet';

/** The Leaflet map a <BaseMap> created, for the layers drawn inside it. */
export const MapContext = createContext<L.Map | null>(null);

/** The Leaflet map of the enclosing BaseMap. Only usable inside it. */
export function useMap(): L.Map {
  const map = useContext(MapContext);
  if (!map) throw new Error('useMap must be called inside <BaseMap>');
  return map;
}
