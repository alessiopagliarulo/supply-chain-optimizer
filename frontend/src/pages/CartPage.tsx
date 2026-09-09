import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, AlertTriangle, Package, X } from 'lucide-react';
import { useCartStore } from '../store/cartStore';
import { livePricesAPI } from '../services/api';
import type { LivePriceResponse } from '../services/api';

// Money helpers. Line/BOM totals always show exactly two decimals; component
// unit prices are genuinely sub-cent for passives (the catalog's cheapest real
// offer is $0.0031), so they show 2–4 decimals — enough precision to never
// round a real part to $0.00, without printing "$16.1600".
//
// Cart line items themselves have no currency field — they're always USD (the
// catalog's static offers are USD-only, confirmed against the live DB). The
// "Live:" comparison price below is a LiveOffer, though, and those genuinely
// come back in EUR/GBP from European distributors (Schukat, Farnell) — so it
// takes an explicit currency rather than defaulting silently to "$".
const fmtMoney = (n: number, currency: string | null | undefined, maximumFractionDigits: number) => {
  const code = (currency || 'USD').trim().toUpperCase();
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: code,
      minimumFractionDigits: 2,
      maximumFractionDigits,
    }).format(n);
  } catch {
    return `${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits })} ${code}`;
  }
};

const fmtUsd = (n: number, currency?: string | null) => fmtMoney(n, currency, 2);

const fmtUnitPrice = (n: number, currency?: string | null) => fmtMoney(n, currency, 4);

export default function CartPage() {
  const navigate = useNavigate();
  const { items, loading, error, fetchCart, removeItem, clearCart } = useCartStore();

  useEffect(() => {
    fetchCart();
  }, [fetchCart]);

  const totalCost = items.reduce((sum, i) => sum + (i.unit_price ?? 0) * i.quantity, 0);

  // ── Live BOM pricing — compares the cart's locked-in 2024 snapshot price
  // against real distributor prices right now, fetched in one batched call.
  const [bomState, setBomState] = useState<'idle' | 'loading' | 'loaded' | 'error'>('idle');
  const [bomResults, setBomResults] = useState<Record<string, LivePriceResponse>>({});
  const [bomError, setBomError] = useState('');

  const checkLivePricing = async () => {
    const mpns = Array.from(new Set(items.map((i) => i.mpn).filter((m): m is string => !!m)));
    if (mpns.length === 0) return;
    setBomState('loading');
    setBomError('');
    try {
      const res = await livePricesAPI.bom(mpns.map((mpn) => ({ mpn, quantity: 1 })));
      setBomResults(res.data.results);
      setBomState('loaded');
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      setBomError(axiosErr?.response?.data?.detail || 'Live BOM pricing lookup failed.');
      setBomState('error');
    }
  };

  return (
    <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full p-6">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-bold text-white">Bill of Materials (BOM)</h1>
          {items.length > 0 && (
            <div className="flex items-center gap-4">
              <button
                onClick={checkLivePricing}
                disabled={bomState === 'loading'}
                className="text-xs inline-flex items-center gap-1.5 bg-amber-500/10 hover:bg-amber-500/20 disabled:opacity-50 border border-amber-500/30 text-amber-400 px-3 py-1.5 rounded-lg transition-colors"
                title="Compare against real-time prices from Nexar, DigiKey, OEMsecrets & TrustedParts"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${bomState === 'loading' ? 'animate-spin' : ''}`} />
                {bomState === 'loading' ? 'Checking live prices…' : 'Check live pricing'}
              </button>
              <button
                onClick={() => clearCart()}
                className="text-xs text-red-400 hover:text-red-300 min-h-[44px] px-2 transition-colors"
              >
                Clear all
              </button>
            </div>
          )}
        </div>

        {bomState === 'error' && (
          <div className="text-xs text-red-400 bg-red-900/20 border border-red-700/40 rounded-lg p-3 mb-4">{bomError}</div>
        )}

        {loading && (
          <div className="text-center text-slate-400 py-10">Loading cart...</div>
        )}

        {!loading && error && (
          <div className="text-center py-16 text-slate-400">
            <AlertTriangle className="w-12 h-12 text-slate-400 mx-auto mb-4" aria-hidden="true" />
            <div className="text-lg font-medium text-red-400">Failed to load cart</div>
            <div className="text-sm mt-1 mb-6 text-slate-400">{error}</div>
            <button
              onClick={() => fetchCart()}
              className="bg-blue-600 hover:bg-blue-500 text-white px-5 py-2 rounded text-sm font-medium transition-colors"
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !error && items.length === 0 && (
          <div className="text-center py-16 text-slate-400">
            <Package className="w-12 h-12 text-slate-400 mx-auto mb-4" aria-hidden="true" />
            <div className="text-lg font-medium text-slate-400">Your BOM is empty</div>
            <div className="text-sm mt-1 mb-6">Go to the Components tab to add parts to your BOM</div>
            <button
              onClick={() => navigate('/components')}
              className="bg-blue-600 hover:bg-blue-500 text-white px-5 py-2 rounded text-sm font-medium transition-colors"
            >
              Browse Components
            </button>
          </div>
        )}

        {!loading && items.length > 0 && (
          <>
            <div className="space-y-2 mb-6">
              {items.map((item) => (
                <div
                  key={item.id}
                  className="flex items-center gap-4 bg-slate-800 border border-slate-700 rounded-lg p-4"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-white font-medium text-sm truncate">
                      {item.mpn ?? `Component #${item.component_id}`}
                    </div>
                    <div className="text-slate-400 text-xs mt-0.5">
                      {item.manufacturer && <span>{item.manufacturer} &middot; </span>}
                      {item.distributor_name ?? `Distributor #${item.distributor_id}`}
                      {item.distributor_country && item.distributor_country !== 'USA' && (
                        <span className="text-slate-400"> ({item.distributor_country})</span>
                      )}
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-white text-sm font-medium">
                      {item.quantity.toLocaleString()} {item.quantity === 1 ? 'unit' : 'units'}
                    </div>
                    {item.unit_price != null && (
                      <div className="text-slate-400 text-xs">
                        @ {fmtUnitPrice(item.unit_price)}/unit
                      </div>
                    )}
                    <div className="text-blue-400 text-sm font-semibold">
                      {fmtUsd((item.unit_price ?? 0) * item.quantity)}
                    </div>
                    {bomState === 'loaded' && item.mpn && (() => {
                      const live = bomResults[item.mpn];
                      if (!live || live.offers.length === 0) {
                        return <div className="text-[11px] text-slate-400 mt-1">No live offers found</div>;
                      }
                      const bestLive = live.offers[0];
                      const liveCurrency = bestLive.currency || 'USD';
                      if (item.unit_price == null) {
                        return (
                          <div className="text-[11px] text-amber-400 mt-1">
                            Live: {fmtUnitPrice(bestLive.price, liveCurrency)} at {bestLive.distributor}
                          </div>
                        );
                      }
                      // The cart's unit_price is always USD; comparing it to a non-USD
                      // live price without conversion would be the same silent unit
                      // error as elsewhere in this repo, so no % delta is computed —
                      // the live figure is still shown, honestly labeled.
                      if (liveCurrency.toUpperCase() !== 'USD') {
                        return (
                          <div className="text-[11px] text-amber-400 mt-1">
                            Live: {fmtUnitPrice(bestLive.price, liveCurrency)} at {bestLive.distributor} (not USD — not compared)
                          </div>
                        );
                      }
                      const delta = ((bestLive.price - item.unit_price) / item.unit_price) * 100;
                      return (
                        <div className={`text-[11px] mt-1 ${delta > 0 ? 'text-red-400' : delta < 0 ? 'text-green-400' : 'text-slate-400'}`}>
                          Live: {fmtUnitPrice(bestLive.price, liveCurrency)} at {bestLive.distributor} ({delta > 0 ? '+' : ''}{delta.toFixed(1)}%)
                        </div>
                      );
                    })()}
                  </div>
                  <button
                    onClick={() => removeItem(item.id)}
                    aria-label={`Remove ${item.mpn ?? `component #${item.component_id}`} from BOM`}
                    className="shrink-0 inline-flex items-center justify-center w-11 h-11 rounded-lg text-slate-400 hover:text-red-400 hover:bg-slate-700/50 transition-colors"
                  >
                    <X className="w-5 h-5" aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>

            <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
              <div className="flex justify-between text-sm mb-2">
                <span className="text-slate-400">Components subtotal</span>
                <span className="text-white">{fmtUsd(totalCost)}</span>
              </div>
              <div className="flex justify-between text-sm mb-3">
                <span className="text-slate-400">Line items</span>
                <span className="text-white">{items.length}</span>
              </div>
              <div className="border-t border-slate-700 pt-3 flex items-center justify-between">
                <div className="text-slate-300 text-sm">
                  Choose distributors and build the pickup route for these lines.
                </div>
                <button
                  onClick={() => navigate('/checkout')}
                  className="bg-green-700 hover:bg-green-600 text-white px-6 py-3 rounded font-medium text-sm transition-colors"
                >
                  Optimize & Checkout
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
