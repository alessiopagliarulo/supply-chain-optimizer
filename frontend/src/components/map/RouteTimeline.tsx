import { motion, AnimatePresence } from 'framer-motion';
import { MapPin, Package, TrendingUp, Clock, DollarSign, Leaf, Factory, X, Route, Weight } from 'lucide-react';
import type { RouteStop } from '../../store/optimizeStore';

interface RouteTimelineProps {
  route: RouteStop[];
  totalCost: number;
  totalCo2: number;
  etaP50: number;
  open: boolean;
  onClose: () => void;
  onFlyTo: (lat: number, lng: number) => void;
  /**
   * The backend's own sentence explaining why the summary bar above the legs
   * does not equal the sum of the legs below it. Under a cross-dock plan the
   * charged cost / CO2e / ETA describe the hub-routed route, while `route`
   * still lists the pre-consolidation pickup legs — they are what a map can
   * draw. CheckoutPage has rendered this since it existed; the map panel showed
   * the same two sets of numbers with nothing between them.
   */
  routeLegsNote?: string | null;
  transportCostBasis?: string;
}

const containerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: { staggerChildren: 0.06, delayChildren: 0.1 },
  },
};

const itemVariants = {
  hidden: { opacity: 0, x: 16 },
  visible: (i?: number) => ({
    opacity: 1,
    x: 0,
    transition: { delay: (i ?? 0) * 0.06, duration: 0.35 },
  }),
};

export default function RouteTimeline({
  route,
  totalCost,
  totalCo2,
  etaP50,
  open,
  onClose,
  onFlyTo,
  routeLegsNote,
  transportCostBasis,
}: RouteTimelineProps) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          key="timeline"
          initial={{ x: '100%', opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          exit={{ x: '100%', opacity: 0 }}
          transition={{ type: 'spring', stiffness: 300, damping: 30 }}
          className="absolute top-0 right-0 h-full w-80 bg-slate-900/98 backdrop-blur-md border-l border-slate-700/60 shadow-2xl z-20 flex flex-col pointer-events-auto"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-700/50">
            <div className="flex items-center gap-2">
              <Route className="w-4 h-4 text-blue-400" />
              <h2 className="text-sm font-semibold text-white">Route Timeline</h2>
              <span className="text-[11px] font-medium px-1.5 py-0.5 rounded-full bg-blue-500/20 text-blue-400 border border-blue-500/30">
                {route.length} stops
              </span>
            </div>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-700 transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Summary bar */}
          <div className="grid grid-cols-3 border-b border-slate-700/50">
            <div className="flex flex-col items-center py-3 border-r border-slate-700/50">
              <DollarSign className="w-3.5 h-3.5 text-green-400 mb-1" />
              <div className="text-sm font-bold text-green-400">
                ${totalCost.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </div>
              <div className="text-[11px] text-slate-400">Total Cost</div>
            </div>
            <div className="flex flex-col items-center py-3 border-r border-slate-700/50">
              <Leaf className="w-3.5 h-3.5 text-emerald-400 mb-1" />
              <div className="text-sm font-bold text-emerald-400">
                {totalCo2.toFixed(1)} kg
              </div>
              <div className="text-[11px] text-slate-400">CO₂</div>
            </div>
            <div className="flex flex-col items-center py-3">
              <Clock className="w-3.5 h-3.5 text-blue-400 mb-1" />
              <div className="text-sm font-bold text-blue-400">{etaP50}d</div>
              <div className="text-[11px] text-slate-400">P50 ETA</div>
            </div>
          </div>

          {/*
            Why the summary bar above and the legs below can disagree. Rendered
            only for a cross-dock plan, because that is the only case where they
            genuinely describe different routes — on a direct pickup tour the
            headline IS the sum of the legs and a caveat would be noise.
            Same treatment as CheckoutPage's route panel.
          */}
          {routeLegsNote && transportCostBasis === 'cross_dock_consolidated' && (
            <p className="text-xs text-slate-400 leading-relaxed px-5 py-3 border-b border-slate-700/50 border-l-2 border-l-amber-500/50">
              {routeLegsNote}
            </p>
          )}

          {/* Timeline scroll area */}
          <div className="flex-1 overflow-y-auto">
            <motion.div
              variants={containerVariants}
              initial="hidden"
              animate="visible"
              className="p-4 space-y-0"
            >
              {route.map((stop, idx) => (
                <motion.div key={stop.distributor_id} variants={itemVariants} className="relative">
                  {/* Timeline connector */}
                  {idx < route.length - 1 && (
                    <div className="absolute left-[19px] top-10 bottom-0 w-px bg-gradient-to-b from-blue-500/60 to-slate-700/60" />
                  )}

                  <button
                    onClick={() => onFlyTo(stop.lat, stop.lng)}
                    className="group relative w-full flex items-start gap-3 rounded-lg p-3 hover:bg-slate-800/60 transition-colors text-left mb-2"
                  >
                    {/* Step circle */}
                    <div className="flex-shrink-0 w-9 h-9 rounded-full border-2 border-blue-500 bg-slate-900 flex items-center justify-center text-xs font-bold text-blue-400 group-hover:border-blue-400 group-hover:bg-blue-500 group-hover:text-slate-900 transition-all z-10">
                      {stop.order}
                    </div>

                    <div className="flex-1 min-w-0 pt-0.5">
                      <div className="text-xs font-semibold text-white leading-tight truncate group-hover:text-blue-300 transition-colors">
                        {stop.distributor_name}
                      </div>
                      {(stop.city || stop.state) && (
                        <div className="flex items-center gap-1 mt-0.5 text-[11px] text-slate-400">
                          <MapPin className="w-2.5 h-2.5" />
                          {[stop.city, stop.state].filter(Boolean).join(', ')}
                        </div>
                      )}

                      {/*
                        `Load` is what the truck is actually carrying ON this
                        leg, and it is why the outbound leg reads 0.0 kg CO₂: a
                        pickup tour leaves the depot empty, and ton-mile factors
                        bill emissions to freight carried. Without it the zero
                        looks like a missing number rather than an empty trailer.
                      */}
                      <div
                        className={`grid ${
                          stop.leg_carried_kg === undefined ? 'grid-cols-3' : 'grid-cols-4'
                        } gap-1.5 mt-2 bg-slate-800/50 rounded-md p-2`}
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-0.5 mb-0.5">
                            <TrendingUp className="w-2.5 h-2.5 text-slate-500" />
                            <span className="text-[9px] text-slate-500">Cost</span>
                          </div>
                          <span className="text-[11px] font-semibold text-green-400">
                            ${stop.leg_cost_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                          </span>
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-0.5 mb-0.5">
                            <Leaf className="w-2.5 h-2.5 text-slate-500" />
                            <span className="text-[9px] text-slate-500">CO₂</span>
                          </div>
                          <span className="text-[11px] font-semibold text-orange-400">
                            {stop.leg_co2e_kg.toFixed(1)}kg
                          </span>
                        </div>
                        {stop.leg_carried_kg !== undefined && (
                          <div className="min-w-0">
                            <div className="flex items-center gap-0.5 mb-0.5">
                              <Weight className="w-2.5 h-2.5 text-slate-500" />
                              <span className="text-[9px] text-slate-500">Load</span>
                            </div>
                            <span className="text-[11px] font-semibold text-amber-300">
                              {stop.leg_carried_kg.toFixed(1)}kg
                            </span>
                          </div>
                        )}
                        <div className="min-w-0">
                          <div className="flex items-center gap-0.5 mb-0.5">
                            <Package className="w-2.5 h-2.5 text-slate-500" />
                            <span className="text-[9px] text-slate-500">Dist</span>
                          </div>
                          <span className="text-[11px] font-semibold text-blue-400">
                            {stop.distance_km.toFixed(0)}km
                          </span>
                        </div>
                      </div>

                      {/* Materials */}
                      {stop.components.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          {stop.components.slice(0, 3).map((m, i) => (
                            <span
                              key={i}
                              className="text-[9px] px-1.5 py-0.5 rounded-full bg-slate-700/70 text-slate-400"
                            >
                              {m}
                            </span>
                          ))}
                          {stop.components.length > 3 && (
                            <span className="text-[9px] text-slate-600">
                              +{stop.components.length - 3}
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  </button>
                </motion.div>
              ))}

              {/* Return to factory */}
              <motion.div variants={itemVariants}>
                <div className="flex items-center gap-3 p-3 rounded-lg bg-slate-800/30 border border-slate-700/40">
                  <div className="w-9 h-9 rounded-full border-2 border-emerald-500 bg-slate-900 flex items-center justify-center flex-shrink-0">
                    <Factory className="w-4 h-4 text-emerald-400" />
                  </div>
                  <div>
                    <div className="text-xs font-semibold text-white">Return to Factory</div>
                    <div className="text-[11px] text-slate-400 mt-0.5">Route complete</div>
                  </div>
                </div>
              </motion.div>
            </motion.div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
