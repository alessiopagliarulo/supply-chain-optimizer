import { create } from 'zustand';
import { jwtDecode } from 'jwt-decode';
import { authAPI, tokenStorage, type AuthUser } from '../services/api';

type User = AuthUser;

interface AuthState {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  /**
   * False until `initializeAuth()` has finished deciding whether the stored token
   * represents a real session. Routing MUST wait on this: treating "not yet
   * resolved" as "not authenticated" is what bounced logged-in users to /login on
   * every refresh and deep link.
   */
  authResolved: boolean;
  /**
   * A stored token exists and looks like a JWT, but `GET /auth/me` could not be
   * reached to confirm it — a sleeping free-tier backend (502/503/network), never
   * a 401. It is deliberately NOT the same as logged-out: the token may well be
   * valid, we just have no profile to stand behind, so the app must say that
   * rather than render a half-session.
   */
  sessionUnverified: boolean;
  setToken: (token: string) => void;
  setUser: (user: User) => void;
  login: (token: string, user: User) => void;
  /** Store the token, then fetch the real profile from GET /auth/me. */
  loginWithToken: (token: string) => Promise<void>;
  logout: () => void;
  initializeAuth: () => Promise<void>;
  /** Re-run initializeAuth() after a `sessionUnverified` result, from a Retry button. */
  retrySession: () => Promise<void>;
}

/**
 * Render's free tier answers 502/503 for the first seconds of a cold start, and the
 * cold-start retry in api.ts deliberately cannot help here: `isTimeoutError` returns
 * false the moment there is an HTTP response, so a 502 is never retried. One bounded
 * retry, after this pause, is what turns "closed the laptop, reopened it at the fair"
 * from a broken-looking session into a wait.
 */
const COLD_START_RETRY_DELAY_MS = 3000;

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: null,
  isLoading: false,
  isAuthenticated: false,
  authResolved: false,
  sessionUnverified: false,

  setToken: (token: string) => {
    tokenStorage.set(token);
    set({ token, isAuthenticated: true });
  },

  setUser: (user: User) => {
    set({ user });
  },

  login: (token: string, user: User) => {
    tokenStorage.set(token);
    set({ token, user, isAuthenticated: true, authResolved: true });
  },

  // The only honest way to populate the user: ask the API who this token belongs to.
  // Login/Register used to invent `{id:1, factory_name:'', latitude:0, longitude:0}`,
  // which put every real account's depot in the Gulf of Guinea.
  loginWithToken: async (token: string) => {
    tokenStorage.set(token);
    // `isAuthenticated` is deliberately NOT set before the await. `PublicOnly`
    // (App.tsx:123) redirects to /dashboard on `isAuthenticated` alone, so
    // flipping it here handed ProtectedLayout a `user: null` dashboard for the
    // whole duration of /auth/me — up to COLD_START_TIMEOUT_MS on a sleeping
    // free-tier backend. Both callers (Login, Register) `await` this and then
    // navigate('/dashboard') themselves, so nothing needs the early flag.
    set({ token, isLoading: true });
    try {
      const res = await authAPI.me();
      set({
        user: res.data,
        isAuthenticated: true,
        sessionUnverified: false,
        isLoading: false,
        authResolved: true,
      });
    } catch (err) {
      // The token may be perfectly good and the backend merely asleep — but with no
      // profile we must not claim a session. Leaving `isAuthenticated: true` here
      // sent PublicOnly straight to /dashboard with `user` null: no Logout control
      // in the nav, a blank "Welcome back, ", and the map's route layer gated off.
      // The token stays in storage, so a retry or a refresh can still confirm it.
      set({ user: null, isAuthenticated: false, isLoading: false, authResolved: true });
      throw err;
    }
  },

  logout: () => {
    tokenStorage.clear();
    set({ token: null, user: null, isAuthenticated: false, sessionUnverified: false, authResolved: true });
  },

  initializeAuth: async () => {
    const token = tokenStorage.get();
    if (!token) {
      set({ isLoading: false, authResolved: true });
      return;
    }
    try {
      jwtDecode<{ sub: number }>(token); // throws if the stored value isn't a JWT at all
    } catch {
      tokenStorage.clear();
      set({ token: null, user: null, isAuthenticated: false, isLoading: false, authResolved: true });
      return;
    }
    // `isAuthenticated` is NOT set here any more. It used to flip true before
    // /auth/me was awaited, and the non-401 failure branch left it that way with
    // `user` null — a cold-start 502 therefore produced an app that believed it was
    // logged in and had nobody to show: Logout gone site-wide, "Welcome back, "
    // blank, and MapPage early-returning on !user so the flagship map drew markers
    // and zero routes, silently. Nothing renders behind `authResolved: false`, so
    // waiting for the profile costs no UI.
    set({ token, isLoading: true });
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        // Restore user profile so pages that need lat/lng work on refresh
        const res = await authAPI.me();
        set({
          user: res.data,
          isAuthenticated: true,
          sessionUnverified: false,
          isLoading: false,
          authResolved: true,
        });
        return;
      } catch (err) {
        // A 401 means the token is genuinely dead — drop it. Anything else (backend
        // down, network blip) must NOT sign the user out; the axios interceptor
        // already handles real 401s globally.
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 401 || status === 403) {
          tokenStorage.clear();
          set({
            token: null, user: null, isAuthenticated: false,
            sessionUnverified: false, isLoading: false, authResolved: true,
          });
          return;
        }
        // No response at all, or one of Render's cold-start gateway errors: worth
        // exactly one more try before we give up and say so.
        const wakingUp = status === undefined || status === 502 || status === 503 || status === 504;
        if (attempt === 0 && wakingUp) {
          await new Promise((resolve) => setTimeout(resolve, COLD_START_RETRY_DELAY_MS));
          continue;
        }
        // Keep the token — it may still be valid — but report an unverified session
        // instead of a confident one. The UI turns this into an explicit "couldn't
        // reach the backend" screen with a Retry, not a silent half-login.
        set({
          user: null, isAuthenticated: false,
          sessionUnverified: true, isLoading: false, authResolved: true,
        });
        return;
      }
    }
  },

  retrySession: async () => {
    // Drop back to the unresolved state so the app shows its normal "Restoring
    // session…" splash (with the shared wake notice) while this runs.
    set({ sessionUnverified: false, authResolved: false, isLoading: true });
    await get().initializeAuth();
  },
}));
