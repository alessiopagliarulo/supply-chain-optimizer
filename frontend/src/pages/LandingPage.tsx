/**
 * Public landing page at `/`: what the app is and a link to each of its four pages.
 *
 * Renders with ZERO network requests - it must work while the free-tier API is still
 * asleep. main.tsx skips the warm-up ping on this route for the same reason, so this
 * component must never import services/api or anything that touches the network.
 */
import { Link } from 'react-router-dom';
import { ArrowRight, BarChart3, Boxes, Map as MapIcon, Route, type LucideIcon } from 'lucide-react';

interface Destination {
  to: string;
  title: string;
  body: string;
  icon: LucideIcon;
}

const PAGES: Destination[] = [
  {
    to: '/map',
    title: 'Map',
    body: 'Every electronic-component distributor in the catalogue at its real location on an OpenStreetMap map, with what each one carries.',
    icon: MapIcon,
  },
  {
    to: '/route-plan',
    title: 'Route Plan',
    body: 'Pick a real depot and destinations and plan truck routes with the capacitated vehicle routing solver, drawn on the map. The Solomon test cases are one tab away.',
    icon: Route,
  },
  {
    to: '/digital-twin',
    title: 'Digital Twin',
    body: 'Coming next: a simulated replica of the network. Today it stress-tests a solved route plan with random travel and service times and tunes its buffers.',
    icon: Boxes,
  },
  {
    to: '/benchmarks',
    title: 'Benchmarks',
    body: 'Compare the exact CP-SAT model, Clarke-Wright savings and OR-Tools routing on the Solomon benchmark set against the best-known solutions.',
    icon: BarChart3,
  },
];

export default function LandingPage() {
  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 py-12 sm:py-20 flex flex-col gap-10">
        <header className="flex flex-col gap-4">
          <p className="text-sm font-semibold text-blue-400">
            SupplyChain<span className="text-white">IQ</span>
          </p>
          <h1 className="text-3xl sm:text-4xl font-semibold text-white leading-tight">
            Truck routing on a real electronics supply network
          </h1>
          <p className="text-base text-slate-400 leading-relaxed max-w-2xl">
            See where real component distributors are, plan capacity-limited truck routes between them, and
            check the solvers against the standard benchmark instances. The distributor catalogue is a
            frozen 2024 snapshot, not live data.
          </p>
        </header>

        <nav aria-label="Pages" className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {PAGES.map(({ to, title, body, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              className="group bg-slate-900 border border-slate-800 hover:border-blue-500 rounded-xl p-5 flex flex-col gap-3 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950"
            >
              <Icon className="w-6 h-6 text-blue-400" aria-hidden="true" />
              <span className="text-lg font-semibold text-white flex items-center gap-2">
                {title}
                <ArrowRight className="w-4 h-4 text-slate-500 group-hover:text-blue-400 transition-colors" aria-hidden="true" />
              </span>
              <span className="text-sm text-slate-400 leading-relaxed">{body}</span>
            </Link>
          ))}
        </nav>
      </div>
    </main>
  );
}
