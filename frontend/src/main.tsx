import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import { ErrorBoundary } from './components/ui';
import { startLongTaskObserver } from './telemetry/perf';
import { publishScrollbarWidth } from './utils/scrollbar-width';
import './index.css';

// Begin capturing main-thread freezes (telemetry perf producer). Idempotent; no-op
// where PerformanceObserver/longtask is unsupported.
startLongTaskObserver();
publishScrollbarWidth();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
