import { NavLink, Link } from 'react-router-dom';
import { BarChart3, Route, Timer, type LucideIcon } from 'lucide-react';

interface NavItem {
  path: string;
  label: string;
  icon: LucideIcon;
}

const NAV_ITEMS: NavItem[] = [
  { path: '/route-plan', label: 'Route Plan', icon: Route },
  { path: '/simulation', label: 'Simulation', icon: Timer },
  { path: '/benchmarks', label: 'Benchmarks', icon: BarChart3 },
];

/**
 * Three links fit on one row at any width down to a phone, so there is no hamburger:
 * below `sm` the labels drop and each link keeps its icon plus an accessible name.
 */
export default function NavBar() {
  return (
    <nav
      aria-label="Main"
      className="bg-slate-900 border-b border-slate-800 px-3 sm:px-4 flex items-center gap-2 h-14 shrink-0"
    >
      <Link
        to="/"
        className="text-white font-bold text-sm mr-2 sm:mr-4 whitespace-nowrap min-h-[44px] flex items-center hover:text-blue-400 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
      >
        SupplyChain<span className="text-blue-400">IQ</span>
      </Link>
      <div className="flex items-center gap-1 flex-1 min-w-0">
        {NAV_ITEMS.map(({ path, label, icon: Icon }) => (
          <NavLink
            key={path}
            to={path}
            aria-label={label}
            className={({ isActive }) =>
              `flex items-center justify-center gap-1.5 min-h-[44px] min-w-[44px] px-2 sm:px-3 rounded-lg text-sm font-medium whitespace-nowrap transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 ${
                isActive ? 'bg-blue-600 text-white' : 'text-slate-300 hover:text-white hover:bg-slate-800'
              }`
            }
          >
            <Icon size={18} className="shrink-0" aria-hidden="true" />
            <span className="hidden sm:inline">{label}</span>
          </NavLink>
        ))}
      </div>
      <span
        className="text-xs text-slate-400 whitespace-nowrap font-mono hidden md:inline"
        title={`Built ${new Date(__BUILD_TIME__).toLocaleString()}`}
      >
        build {__BUILD_COMMIT__.slice(0, 7)}
      </span>
    </nav>
  );
}
