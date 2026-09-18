import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Boxes,
  ShieldAlert,
  ShoppingCart,
  BrainCircuit,
  PackageSearch,
  Menu,
  X,
  type LucideIcon,
} from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { useCartStore } from '../store/cartStore';

// Labels describe what the page actually contains, and each path is the page's own
// canonical route. Previously "Scheduler" pointed at a component browser (nothing is
// scheduled).
interface NavItem {
  path: string;
  label: string;
  icon: LucideIcon;
  /** Legacy paths that should still light this tab up. */
  aliases?: string[];
}

const NAV_ITEMS: NavItem[] = [
  { path: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/components', label: 'Components', icon: Boxes, aliases: ['/scheduler'] },
  { path: '/resilience', label: 'Resilience', icon: ShieldAlert },
  { path: '/newsvendor', label: 'Newsvendor', icon: PackageSearch },
  { path: '/cart', label: 'Cart', icon: ShoppingCart },
  { path: '/model-card', label: 'Model Card', icon: BrainCircuit },
];

export default function NavBar() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();
  const { items } = useCartStore();
  // Below `xl` the link row plus brand, user and build stamp don't fit any
  // viewport down to phone width — they used to just overflow the nav (and drag the
  // whole page into horizontal scroll with them, since nothing shrinks a flex row
  // whose items default to min-width:auto). Collapse into a hamburger instead.
  const [menuOpen, setMenuOpen] = useState(false);
  // Close the mobile menu on any route change, including browser back/forward
  // (which skip the `go()` helper below). Adjusting state during render — rather
  // than in a useEffect — is the pattern React recommends for resetting state in
  // response to a prop/route change; it avoids an extra render pass.
  const [menuOpenForPath, setMenuOpenForPath] = useState(location.pathname);
  if (menuOpenForPath !== location.pathname) {
    setMenuOpenForPath(location.pathname);
    if (menuOpen) setMenuOpen(false);
  }

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const go = (path: string) => {
    navigate(path);
    setMenuOpen(false);
  };

  // Desktop spacing is deliberately tight (px-2, gap-1, gap-0.5 between items).
  // Measured in headless Chromium at 1440px: brand + ten links + user + build
  // stamp comes to ~1388px with these values and ~1506px with the previous
  // px-3/gap-1.5/gap-1, i.e. the tenth link (Newsvendor) would have wrapped and
  // clipped inside the fixed h-12 bar. Mobile keeps its roomier padding.
  const renderNavButton = (
    { path, label, icon: Icon, aliases }: NavItem,
    variant: 'desktop' | 'mobile'
  ) => {
    const active = location.pathname === path || (aliases?.includes(location.pathname) ?? false);
    const isCart = path === '/cart';
    const badge = isCart && items.length > 0;
    return (
      <button
        key={path}
        onClick={() => go(path)}
        className={
          variant === 'desktop'
            ? `relative flex items-center gap-1 px-2 py-1.5 rounded text-xs font-medium whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 ${
                active
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-400 hover:text-white hover:bg-slate-700/60'
              }`
            : `flex items-center gap-2 px-3 py-3 min-h-[44px] rounded text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 ${
                active
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-400 hover:text-white hover:bg-slate-700/60'
              }`
        }
      >
        <Icon size={variant === 'desktop' ? 16 : 18} className="shrink-0" aria-hidden="true" />
        {label}
        {badge && (
          <span
            className={
              variant === 'desktop'
                ? 'absolute -top-1 -right-1 bg-blue-500 text-white text-[11px] font-bold rounded-full w-[18px] h-[18px] flex items-center justify-center leading-none'
                : 'ml-auto bg-blue-500 text-white text-[11px] font-bold rounded-full w-[18px] h-[18px] flex items-center justify-center leading-none'
            }
          >
            {items.length}
          </span>
        )}
      </button>
    );
  };

  return (
    <nav className="relative bg-slate-900 border-b border-slate-700 px-4 py-0 flex items-center h-12 shrink-0 z-20">
      {/* Brand */}
      <button
        onClick={() => go('/dashboard')}
        className="text-white font-bold text-sm mr-6 whitespace-nowrap min-h-[44px] flex items-center hover:text-blue-400 transition-colors"
      >
        SupplyChain<span className="text-blue-400">IQ</span>
      </button>

      {/* Nav links — full row, only once there's room for all of them */}
      {/*
        Collapse breakpoint is min-[1400px], NOT Tailwind's `xl` (1280px).
        Measured in headless Chromium with ten links: the row needs 1371px, so at
        exactly 1280 the full nav rendered into a bar 91px too narrow and clipped
        inside the fixed h-12. This is the THIRD time nav width has regressed here
        (1219px and 1313px against a 390px viewport were the first two), so the
        number is measured, not guessed — re-measure with navcheck if a link is
        added or a label changes.
      */}
      <div className="hidden min-[1400px]:flex items-center gap-0.5 flex-1">
        {NAV_ITEMS.map((item) => renderNavButton(item, 'desktop'))}
      </div>
      {/* Below xl, the links move into the hamburger menu; this spacer keeps the
          hamburger, build stamp pinned to the right the way flex-1 does above. */}
      <div className="flex-1 min-[1400px]:hidden" />

      {/* User / logout */}
      {user && (
        <div className="hidden min-[1400px]:flex items-center gap-3 ml-4">
          <span className="text-slate-400 text-xs truncate max-w-[140px]">
            {user.factory_name || user.email}
          </span>
          <button
            onClick={handleLogout}
            className="text-xs text-slate-400 hover:text-white transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 rounded"
          >
            Logout
          </button>
        </div>
      )}

      {/* Build stamp — confirms which deploy you're looking at. Deliberately low-emphasis,
          but still needs to clear 4.5:1 body-text contrast. */}
      <span
        className="ml-3 text-[11px] text-slate-400 whitespace-nowrap font-mono hidden sm:inline"
        title={`Built ${new Date(__BUILD_TIME__).toLocaleString()}`}
      >
        build {__BUILD_COMMIT__.slice(0, 7)}
      </span>

      {/* Hamburger — shown below xl instead of the link row. The only navigation entry
          point below `xl`, so it needs a real touch target and a visible focus ring. */}
      <button
        onClick={() => setMenuOpen((open) => !open)}
        className="min-[1400px]:hidden ml-3 w-11 h-11 flex items-center justify-center rounded text-slate-400 hover:text-white hover:bg-slate-700/60 transition-colors shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900"
        aria-label={menuOpen ? 'Close navigation menu' : 'Open navigation menu'}
        aria-expanded={menuOpen}
      >
        {menuOpen ? <X size={22} aria-hidden="true" /> : <Menu size={22} aria-hidden="true" />}
      </button>

      {/* Mobile/tablet menu */}
      {menuOpen && (
        <div className="min-[1400px]:hidden absolute top-full left-0 right-0 bg-slate-900 border-b border-slate-700 shadow-2xl z-50 max-h-[calc(100vh-3rem)] overflow-y-auto">
          <div className="flex flex-col p-2 gap-2">
            {NAV_ITEMS.map((item) => renderNavButton(item, 'mobile'))}
          </div>
          {user && (
            <div className="border-t border-slate-700 px-3 py-2.5 flex items-center justify-between gap-3">
              <span className="text-slate-400 text-xs truncate">
                {user.factory_name || user.email}
              </span>
              <button
                onClick={handleLogout}
                className="text-xs text-slate-400 hover:text-white transition-colors shrink-0 min-h-[44px] flex items-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 rounded"
              >
                Logout
              </button>
            </div>
          )}
        </div>
      )}
    </nav>
  );
}
