import { useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Package, DollarSign, Leaf, Ruler, ChevronRight, Weight } from 'lucide-react';

export interface RouteLegData {
  distributorName: string;
  city: string | null;
  state: string | null;
  country: string | null;
  legCostUsd: number;
  legCo2eKg: number;
  // What the truck is carrying ON this leg. A pickup tour leaves the depot
  // empty, so the outbound leg is 0 kg — which is why its CO₂ is 0 too, ton-mile
  // factors billing emissions to freight carried. Undefined for responses from
  // before the field existed.
  legCarriedKg?: number;
  distanceKm: number;
  components: string[];
  legIndex: number;   // 1-based
  totalLegs: number;
}

interface RouteLegPopupProps {
  data: RouteLegData | null;
  position: { x: number; y: number };
  onClose: () => void;
  containerRef: React.RefObject<HTMLDivElement | null>;
}

export default function RouteLegPopup({ data, position, onClose, containerRef }: RouteLegPopupProps) {
  const popupRef = useRef<HTMLDivElement>(null);
  const [adjusted, setAdjusted] = useState(position);

  useEffect(() => {
    if (!data || !popupRef.current || !containerRef.current) return;
    const { offsetWidth: pw, offsetHeight: ph } = popupRef.current;
    const { offsetWidth: cw, offsetHeight: ch } = containerRef.current;
    let x = position.x + 12;
    let y = position.y - ph / 2;
    if (x + pw > cw - 10) x = position.x - pw - 12;
    if (y < 10) y = 10;
    if (y + ph > ch - 10) y = ch - ph - 10;
    setAdjusted({ x, y });
  }, [data, position, containerRef]);

  return (
    <AnimatePresence>
      {data && (
        <motion.div
          ref={popupRef}
          key="leg-popup"
          initial={{ opacity: 0, scale: 0.92, y: 6 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.92, y: 6 }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
          className="absolute z-20 w-72 pointer-events-auto"
          style={{ left: adjusted.x, top: adjusted.y }}
        >
          <div className="bg-gray-900/95 backdrop-blur-md border border-gray-700/80 rounded-xl shadow-2xl overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700/60">
              <div className="flex items-center gap-2">
                <Package className="w-3.5 h-3.5 text-blue-400" />
                <span className="text-xs font-semibold text-white">Route Leg</span>
              </div>
              <div className="flex items-center gap-2">
                {/* Step progress pills */}
                <div className="flex gap-0.5">
                  {Array.from({ length: data.totalLegs }).map((_, i) => (
                    <div
                      key={i}
                      className={`h-1.5 w-4 rounded-full transition-colors ${
                        i < data.legIndex ? 'bg-blue-500' : 'bg-gray-700'
                      }`}
                    />
                  ))}
                </div>
                <span className="text-[11px] text-gray-400 tabular-nums">
                  {data.legIndex}/{data.totalLegs}
                </span>
                <button
                  onClick={onClose}
                  className="p-1 rounded-md text-gray-500 hover:text-white hover:bg-gray-700 transition-colors"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            </div>

            {/* Distributor */}
            <div className="px-4 py-3 border-b border-gray-700/40">
              <div className="text-[11px] text-gray-500 mb-0.5">Distributor</div>
              <div className="text-sm font-semibold text-white leading-tight">{data.distributorName}</div>
              {(data.city || data.state) && (
                <div className="flex items-center gap-1 mt-0.5 text-[11px] text-gray-400">
                  <ChevronRight className="w-3 h-3" />
                  {[data.city, data.state].filter(Boolean).join(', ')}
                  {data.country && data.country !== 'USA' && (
                    <span className="text-gray-500"> ({data.country})</span>
                  )}
                </div>
              )}
            </div>

            {/* Metrics grid */}
            <div
              /* 2x2 rather than a fourth column: this popup is only 288 px wide,
                 and four money/mass/distance cells side by side overflow it as
                 soon as a leg costs five figures. */
              className={`grid ${
                data.legCarriedKg === undefined ? 'grid-cols-3' : 'grid-cols-2'
              } gap-px bg-gray-700/30 m-3 rounded-lg overflow-hidden`}
            >
              <div className="bg-gray-800/80 px-3 py-2.5">
                <div className="flex items-center gap-1 mb-1">
                  <DollarSign className="w-3 h-3 text-green-400" />
                  <span className="text-[11px] text-gray-400">Leg Cost</span>
                </div>
                <div className="text-sm font-bold text-green-400">
                  ${data.legCostUsd.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                </div>
              </div>
              <div className="bg-gray-800/80 px-3 py-2.5">
                <div className="flex items-center gap-1 mb-1">
                  <Leaf className="w-3 h-3 text-emerald-400" />
                  <span className="text-[11px] text-gray-400">CO₂</span>
                </div>
                <div className="text-sm font-bold text-emerald-400">
                  {data.legCo2eKg.toFixed(1)} kg
                </div>
              </div>
              {data.legCarriedKg !== undefined && (
                <div className="bg-gray-800/80 px-3 py-2.5">
                  <div className="flex items-center gap-1 mb-1">
                    <Weight className="w-3 h-3 text-amber-300" />
                    <span className="text-[11px] text-gray-400">Load</span>
                  </div>
                  <div className="text-sm font-bold text-amber-300">
                    {data.legCarriedKg.toFixed(1)} kg
                  </div>
                </div>
              )}
              <div className="bg-gray-800/80 px-3 py-2.5">
                <div className="flex items-center gap-1 mb-1">
                  <Ruler className="w-3 h-3 text-blue-400" />
                  <span className="text-[11px] text-gray-400">Distance</span>
                </div>
                <div className="text-sm font-bold text-blue-400">
                  {data.distanceKm.toFixed(0)} km
                </div>
              </div>
            </div>

            {/* Components */}
            {data.components.length > 0 && (
              <div className="px-4 pb-3">
                <div className="text-[11px] text-gray-500 mb-1.5">Components collected</div>
                <div className="flex flex-wrap gap-1">
                  {data.components.slice(0, 4).map((c, i) => (
                    <span
                      key={i}
                      className="text-[11px] px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-300 border border-blue-500/25"
                    >
                      {c}
                    </span>
                  ))}
                  {data.components.length > 4 && (
                    <span className="text-[11px] text-gray-500">+{data.components.length - 4} more</span>
                  )}
                </div>
              </div>
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
