/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

const LOCATION_STUBBING_TESTS = [
  'src/api/client.test.ts',
  'src/components/Sidebar.move.test.tsx',
  'src/hooks/useSessionRefresh.test.ts',
];

export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
    {
      // ARCH: block transforms of anything
      // under e2e/.out. Playwright's outputDir (traces/video/screenshots/storage-state) still lives
      // inside the Vite-served root. server.watch.ignored (below) stops the file
      // watcher from re-triggering, but NOT the css-analysis / @tailwindcss/vite
      // content scan from pulling those assets into the module graph on transform
      // (the playwright-logo.svg ENOENT class of bug). Returning empty content for
      // any id under e2e/.out/** neutralizes the module-graph path. The HTML
      // report itself lives outside the root; this guards the residual in-root artifacts.
      name: 'block-e2e-out-artifacts',
      load(id) {
        if (id.includes('/e2e/.out/') || id.includes('\\e2e\\.out\\')) return '';
        return null;
      },
    },
  ],
  test: {
    environment: 'jsdom',
    // WHY two projects: vmThreads keeps one jsdom per worker instead of one per
    // file (the per-file environment setup was ~2/3 of the suite's wall time;
    // module isolation per file is kept). A file that replaces window.location
    // cannot run there — jsdom's location is non-configurable in a vm context
    // ("Cannot redefine property: location") — so those run on plain threads.
    projects: [
      {
        extends: true,
        test: {
          name: 'vm',
          pool: 'vmThreads',
          // Vitest owns src unit tests only; e2e/ specs run under Playwright (`npm run e2e`).
          // Why: without an explicit include, Vitest globs every *.spec.ts and chokes on
          // Playwright's test.describe() (different runner). See CI run #559.
          // Lives per project: with `extends: true` a root include is CONCATENATED
          // into every project's, so the threads project would rerun the whole suite.
          include: ['src/**/*.test.{ts,tsx}'],
          exclude: LOCATION_STUBBING_TESTS,
        },
      },
      {
        extends: true,
        test: { name: 'threads', pool: 'threads', include: LOCATION_STUBBING_TESTS },
      },
    ],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/main.tsx'],
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, '.'),
    },
  },
  build: {
    sourcemap: false,
    rollupOptions: {
      output: {
        // ARCH: split the editor's heavy vendor deps into stable, cacheable
        // chunks. mermaid is intentionally ABSENT — it's dynamically imported so
        // Rollup keeps it in its own async chunk; pinning it here would drag katex +
        // turndown into every mermaid render. Function form (not object) because
        // y-protocols has no root entry — only subpaths (y-protocols/awareness).
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          // WHY: mermaid carries its own nested katex; the bare package-name rule below
          // would pull that second copy into the always-preloaded markdown chunk.
          if (/[\\/]mermaid[\\/]node_modules[\\/]/.test(id)) return;
          if (/[\\/]node_modules[\\/](react|react-dom|react-router-dom|zustand)[\\/]/.test(id)) return 'vendor';
          // INVARIANT: an allowlist of the editor core, never every @codemirror/@lezer id.
          // Why: @codemirror/language-data loads each language by dynamic import; pinning
          // those packages here preloads all of them for every visitor and voids the import.
          if (/[\\/]@codemirror[\\/](state|view|language|commands|search|autocomplete|lang-markdown)[\\/]|[\\/]@lezer[\\/](common|highlight|lr|markdown)[\\/]/.test(id)) return 'codemirror';
          if (/[\\/]node_modules[\\/](yjs|y-codemirror\.next|y-protocols)[\\/]/.test(id)) return 'collab';
          if (/[\\/]node_modules[\\/](katex|turndown|turndown-plugin-gfm)[\\/]/.test(id)) return 'markdown';
        },
      },
    },
  },
  optimizeDeps: {
    include: [
      'react',
      'react-dom',
      'react-router-dom',
      'zustand',
      '@codemirror/view',
      '@codemirror/state',
      '@codemirror/language',
      '@codemirror/lang-markdown',
      '@codemirror/language-data',
    ],
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    // WHY: Playwright's outputDir (traces/video/screenshots/storage-state) is under
    // e2e/.out, inside the Vite-served root. Never watch e2e artifacts — the
    // block-e2e-out-artifacts load hook (plugins above) additionally guards the
    // transform/scan path. The HTML report itself lives OUTSIDE the root
    // (playwright.config.ts), so it never lands here at all.
    watch: { ignored: ['**/e2e/.out/**'] },
    // WHY: the Dockerized e2e harness reaches the dev server by compose service
    // name (Host: frontend). Vite's host check rejects unknown hosts with 403 —
    // allow the service name so E2E_BASE_URL=http://frontend:5173 works.
    // WHY CORS_ORIGINS: an install reached through a LAN name (a reverse proxy
    // forwarding Host) declares that origin there already — the dev server
    // accepts exactly the hosts the backend accepts, with no second list.
    allowedHosts: [
      'frontend',
      'localhost',
      ...(process.env.CORS_ORIGINS ?? '')
        .split(',')
        .map((o) => o.trim())
        .filter(Boolean)
        .map((o) => new URL(o).hostname),
    ],
    // INFRA WIRING, not configuration: the dev proxy targets the backend by
    // its compose service name — nothing runs on the host, so there is no host-run fallback.
    // WHY changeOrigin false: the backend's same-host Origin guard compares the
    // Origin with the Host it receives; a rewritten Host (backend:8001) 403s every
    // write from a page opened at an address not in CORS_ORIGINS (an IP, a LAN name).
    proxy: {
      '/api': {
        target: 'http://backend:8001',
        changeOrigin: false,
      },
      '/ws': {
        target: 'http://backend:8001',
        changeOrigin: false,
        ws: true,
      },
    },
  },
});
