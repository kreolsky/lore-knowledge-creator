import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import { ErrorBoundary } from './components/ui';
import { startLongTaskObserver } from './telemetry/perf';
import { publishScrollbarWidth } from './utils/scrollbar-width';
import { installDomTranslateGuard } from './utils/dom-translate-guard';
import './index.css';

// Before the first render: a translated page must not crash React's DOM commits.
installDomTranslateGuard();

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
