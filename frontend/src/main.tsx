import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import { ErrorBoundary } from './components/ui';
import { startLongTaskObserver } from './telemetry/perf';
import { startPagePerf } from './telemetry/page-perf';
import { publishScrollbarWidth } from './utils/scrollbar-width';
import { installDomTranslateGuard } from './utils/dom-translate-guard';
import { installStaleChunkReload } from './utils/stale-chunk-reload';
import './index.css';

// Before the first render: a translated page must not crash React's DOM commits.
installDomTranslateGuard();
// A tab from before a deploy reloads instead of crashing on a chunk the server dropped.
installStaleChunkReload();
// WHY: the vendored dsh markdown bundle calls URL.canParse while rendering a link;
// browsers before Chrome 120 / Safari 17 / Firefox 115 lack it and the page crashes.
if (typeof URL.canParse !== 'function') {
  URL.canParse = (url: string | URL, base?: string | URL): boolean => {
    try {
      new URL(url, base);
      return true;
    } catch {
      return false;
    }
  };
}

// Begin capturing main-thread freezes (telemetry perf producer). Idempotent; no-op
// where PerformanceObserver/longtask is unsupported.
startLongTaskObserver();
startPagePerf();
publishScrollbarWidth();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
