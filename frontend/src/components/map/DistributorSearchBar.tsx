import { useId, useRef, useState, type KeyboardEvent } from 'react';
import { Search, X } from 'lucide-react';
import type { LocatedDistributor } from '../../services/api';
import { placeLabel } from '../../lib/sites';

interface Props {
  distributors: LocatedDistributor[];
  onSelect: (d: LocatedDistributor) => void;
  placeholder?: string;
}

const MAX_RESULTS = 8;

/**
 * Type-ahead over distributor names and cities, with arrow-key navigation. Restored
 * from the archived map page (tag archive/sourcing-v1) as an accessible combobox,
 * without its animation library.
 */
export default function DistributorSearchBar({ distributors, onSelect, placeholder = 'Search distributors or cities' }: Props) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  const q = query.trim().toLowerCase();
  const results = q
    ? distributors
        .filter((d) => d.name.toLowerCase().includes(q) || placeLabel(d).toLowerCase().includes(q))
        .slice(0, MAX_RESULTS)
    : [];
  const showList = open && q.length > 0;

  const choose = (d: LocatedDistributor) => {
    onSelect(d);
    setQuery('');
    setOpen(false);
    setActive(-1);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      setQuery('');
      setOpen(false);
      setActive(-1);
      return;
    }
    if (!showList || !results.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      choose(results[Math.max(active, 0)]);
    }
  };

  return (
    <div className="relative">
      <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" aria-hidden="true" />
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-label="Search distributors"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList && active >= 0 ? `${listId}-${active}` : undefined}
        placeholder={placeholder}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setActive(-1);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={onKeyDown}
        className="w-full min-h-[44px] pl-9 pr-11 rounded-lg bg-slate-950 border border-slate-700 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
      />
      {query && (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => {
            setQuery('');
            setActive(-1);
            inputRef.current?.focus();
          }}
          className="absolute right-0 top-0 w-11 h-11 flex items-center justify-center text-slate-400 hover:text-white rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
        >
          <X className="w-4 h-4" aria-hidden="true" />
        </button>
      )}
      {showList && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Matching distributors"
          className="absolute z-[1100] mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-900 shadow-2xl py-1 max-h-80 overflow-y-auto"
        >
          {results.length === 0 && <li className="px-3 py-2.5 text-sm text-slate-400">No distributor or city matches.</li>}
          {results.map((d, i) => (
            <li
              key={d.id}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={active === i}
              onMouseDown={(e) => {
                e.preventDefault();
                choose(d);
              }}
              onMouseEnter={() => setActive(i)}
              className={`flex items-center justify-between gap-3 px-3 min-h-[44px] cursor-pointer ${active === i ? 'bg-slate-800' : ''}`}
            >
              <span className="min-w-0">
                <span className="block text-sm font-medium text-white truncate">{d.name}</span>
                <span className="block text-xs text-slate-400 truncate">{placeLabel(d)}</span>
              </span>
              <span className="shrink-0 text-xs text-slate-300 bg-slate-800 px-1.5 py-0.5 rounded tabular-nums">
                {`${d.total_offers} offers`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
