import { useEffect, useRef, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';
import { authAPI, isTimeoutError } from '../services/api';
import { useElapsedSeconds } from '../services/warmup';
import WakeNotice from '../components/WakeNotice';

/**
 * How long a sign-in may run before we stop showing a bare "Loading…" and admit
 * what is actually happening: the free-tier backend is asleep and a cold start
 * takes ~100s. Silence for two minutes is indistinguishable from a broken button.
 */
const WAKE_NOTICE_AFTER_MS = 3000;

const Spinner = () => (
  <svg className="animate-spin h-4 w-4 text-current" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
  </svg>
);

export const Login = () => {
  const navigate = useNavigate();
  const { loginWithToken } = useAuthStore();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  // WHICH button is in flight, not merely THAT one is. Tracking a single boolean
  // made both buttons swap to "Signing in…" with a spinner each, so a click on
  // Sign In looked like it had also fired the demo login.
  const [pending, setPending] = useState<null | 'credentials' | 'demo'>(null);
  const loading = pending !== null;
  const [wakingUp, setWakingUp] = useState(false);
  const wakeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // When the in-flight request started, and a 1 Hz counter off it. Real elapsed time —
  // the one thing that reliably separates "slow" from "hung" for someone watching a
  // spinner. Nothing here estimates or extrapolates progress.
  const [requestStartedAt, setRequestStartedAt] = useState<number | null>(null);
  const requestSec = useElapsedSeconds(requestStartedAt);

  const clearWakeTimer = () => {
    if (wakeTimer.current !== null) {
      clearTimeout(wakeTimer.current);
      wakeTimer.current = null;
    }
  };
  useEffect(() => clearWakeTimer, []);

  const startRequest = (which: 'credentials' | 'demo') => {
    setError('');
    setPending(which);
    setWakingUp(false);
    setRequestStartedAt(performance.now());
    clearWakeTimer();
    wakeTimer.current = setTimeout(() => setWakingUp(true), WAKE_NOTICE_AFTER_MS);
  };

  const endRequest = () => {
    clearWakeTimer();
    setWakingUp(false);
    setRequestStartedAt(null);
    setPending(null);
  };

  const describeError = (err: unknown, fallback: string) => {
    if (isTimeoutError(err)) {
      return 'The backend did not respond in time. It runs on a free tier and can take up to ~2 minutes to wake from sleep — please press the button once more.';
    }
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
    return detail || fallback;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    startRequest('credentials');

    try {
      const response = await authAPI.login({ email, password });
      // Fetch the real profile rather than inventing one. The old code passed
      // `{id:1, factory_name:'', latitude:0, longitude:0}`, which showed every real
      // account a blank factory name and a depot at (0,0) in the Gulf of Guinea.
      await loginWithToken(response.data.access_token);
      navigate('/dashboard');
    } catch (err: unknown) {
      setError(describeError(err, 'Login failed'));
    } finally {
      endRequest();
    }
  };

  const handleDemoLogin = async () => {
    startRequest('demo');

    try {
      const response = await authAPI.demoLogin();
      // Same fix as above: the hardcoded "Demo Factory" at (40.7128, -74.0060) was
      // wrong — the seeded demo account is Greenville Advanced Manufacturing in SC.
      await loginWithToken(response.data.access_token);
      navigate('/dashboard');
    } catch (err: unknown) {
      setError(describeError(err, 'Demo login failed'));
    } finally {
      endRequest();
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-slate-800 rounded-lg shadow-2xl p-8">
        <h1 className="text-3xl font-bold text-white mb-2">Electronics Supply Chain Optimizer</h1>
        <p className="text-slate-400 mb-8">791 components, 92 distributors, 8,176 real price offers.</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
              required
            />
          </div>

          {error && <p className="text-red-400 text-sm" role="alert">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-800 disabled:cursor-not-allowed text-white font-medium rounded-lg transition flex items-center justify-center gap-2"
          >
            {pending === 'credentials' && <Spinner />}
            {pending === 'credentials' ? 'Signing in…' : 'Sign In'}
          </button>

          <button
            type="button"
            onClick={handleDemoLogin}
            disabled={loading}
            className="w-full py-2 bg-slate-700 hover:bg-slate-600 disabled:bg-slate-800 disabled:cursor-not-allowed text-white font-medium rounded-lg transition flex items-center justify-center gap-2"
          >
            {pending === 'demo' && <Spinner />}
            {pending === 'demo' ? 'Signing in…' : 'Demo Login'}
          </button>

          {wakingUp && <WakeNotice requestSec={requestSec} />}
        </form>

        <p className="text-center text-slate-400 text-sm mt-6">
          Don't have an account?{' '}
          <Link to="/register" className="text-blue-400 hover:text-blue-300">
            Register
          </Link>
        </p>
      </div>
    </div>
  );
};
