/** Global error boundary — catches React render crashes and shows fallback UI instead of white screen. */

import { Component } from 'react';
import type { ReactNode, ErrorInfo } from 'react';
import { sendTelemetry } from '../../telemetry/telemetry';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary]', error, info.componentStack);
    sendTelemetry({
      category: 'client',
      kind: 'render-crash',
      detail: { message: error.message, path: window.location.pathname, stack: info.componentStack?.slice(0, 2000) },
    });
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="p-8 font-mono">
        <h1 className="text-text mb-3">Something went wrong</h1>
        <pre className="text-text-muted whitespace-pre-wrap mb-4">
          {this.state.error.message}
        </pre>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 bg-accent text-white border-none cursor-pointer"
        >
          Reload page
        </button>
      </div>
    );
  }
}
