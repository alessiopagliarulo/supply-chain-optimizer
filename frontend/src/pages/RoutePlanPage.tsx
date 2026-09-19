import { useSearchParams } from 'react-router-dom';
import { Grid3x3, MapPinned, type LucideIcon } from 'lucide-react';
import { Page } from '../components/ui';
import RealPlacePlanner from './RealPlacePlanner';
import SolomonPlanner from './SolomonPlanner';

type Source = 'places' | 'solomon';

const SOURCES: { value: Source; label: string; icon: LucideIcon }[] = [
  { value: 'places', label: 'Real places', icon: MapPinned },
  { value: 'solomon', label: 'Solomon test cases', icon: Grid3x3 },
];

/**
 * One routing engine, two kinds of input: real distributor locations on a map, or the
 * planar x/y Solomon benchmark instances (and customer CSVs in the same format).
 * The tab lives in the URL (`?source=solomon`) so either view can be linked to.
 */
export default function RoutePlanPage() {
  const [params, setParams] = useSearchParams();
  const source: Source = params.get('source') === 'solomon' ? 'solomon' : 'places';

  const choose = (next: Source) => {
    const p = new URLSearchParams(params);
    if (next === 'places') p.delete('source');
    else p.set('source', next);
    setParams(p, { replace: true });
  };

  return (
    <Page
      title="Route Plan"
      intro={
        source === 'places' ? (
          <>
            Pick a depot and destinations from the real distributor catalogue and plan truck routes with the
            capacitated vehicle routing solver. Every plan is checked by the shared validator, not taken on the
            solver&apos;s word.
          </>
        ) : (
          <>
            Solve a Solomon benchmark instance or your own customer CSV on a flat x/y plane: capacitated vehicle
            routing with a time window at every customer. Every plan is checked by the shared validator, not taken on
            the solver&apos;s word.
          </>
        )
      }
    >
      <div role="tablist" aria-label="What to route" className="grid grid-cols-2 gap-2 max-w-lg">
        {SOURCES.map(({ value, label, icon: Icon }) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={source === value}
            onClick={() => choose(value)}
            className={`min-h-[44px] flex items-center justify-center gap-2 rounded-lg text-sm font-medium border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 ${
              source === value ? 'bg-blue-600 border-blue-500 text-white' : 'border-slate-700 text-slate-300 hover:bg-slate-800'
            }`}
          >
            <Icon className="w-4 h-4" aria-hidden="true" />
            {label}
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-label={SOURCES.find((s) => s.value === source)?.label}>
        {source === 'places' ? <RealPlacePlanner /> : <SolomonPlanner />}
      </div>
    </Page>
  );
}
