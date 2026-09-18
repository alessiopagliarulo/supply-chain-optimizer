import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Human name of the thing being guarded, e.g. "Resilience Scenarios". */
  scope?: string;
  /**
   * When this value changes, the boundary resets and retries rendering. Pass the
   * route pathname at the page level so navigating away from a crashed page
   * automatically clears the error instead of stranding the user.
   */
  resetKey?: string | number;
  /** Rendered instead of the default fallback, if supplied. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Catches render-time exceptions so one bad field access cannot unmount the whole
 * app. Before this existed, a single `undefined.toFixed()` took out the root —
 * including the nav bar — leaving no way back except a browser refresh.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[ErrorBoundary${this.props.scope ? ` · ${this.props.scope}` : ''}]`, error, info.componentStack);
  }

  componentDidUpdate(prev: ErrorBoundaryProps) {
    // Navigating to a different page clears a crash from the previous one.
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);

    return (
      <div className="h-full w-full overflow-y-auto bg-slate-900 p-6 flex items-start justify-center">
        <div className="w-full max-w-xl mt-16 bg-slate-800 border border-red-700/50 rounded-xl p-6">
          {/* An SVG icon, not an emoji: emoji render differently on every OS and
              cannot take the design system's colour. */}
          <AlertTriangle className="w-8 h-8 text-red-400 mb-3" aria-hidden="true" />
          <h2 className="text-xl font-bold text-white mb-2">
            {this.props.scope ? `${this.props.scope} hit an error` : 'Something went wrong'}
          </h2>
          <p className="text-sm text-slate-400 mb-4">
            This section failed to render. The rest of the app is still working — use the
            navigation above, or try again.
          </p>
          <pre className="text-[11px] text-red-300 bg-slate-900/70 border border-slate-700 rounded p-3 mb-4 overflow-x-auto whitespace-pre-wrap">
            {error.message || String(error)}
          </pre>
          <div className="flex gap-2">
            <button
              onClick={this.reset}
              className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold transition"
            >
              Try again
            </button>
            <a
              href="/"
              className="px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-white text-sm font-semibold transition"
            >
              Back to the start
            </a>
          </div>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
