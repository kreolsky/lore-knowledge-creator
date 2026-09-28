/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
    {
      // ARCH (plan: vite-e2e-report-isolation, D2): block transforms of anything
      // under e2e/.out. Playwright's outputDir (traces/video/screenshots/storage-state) still lives
      // inside the Vite-served root. server.watch.ignored (below) stops the file
      // watcher from re-triggering, but NOT the css-analysis / @tailwindcss/vite
      // content scan from pulling those assets into the module graph on transform
      // (the playwright-logo.svg ENOENT class of bug). Returning empty content for
      // any id under e2e/.out/** neutralizes the module-graph path. D1 moved the
      // HTML report itself out of the root; this guards the residual in-root artifacts.
      // See .kilo/plans/1783883552954-vite-e2e-report-isolation.md.
      name: 'block-e2e-out-artifacts',
      load(id) {
        if (id.includes('/e2e/.out/') || id.includes('\\e2e\\.out\\')) return '';
        return null;
      },
    },
  ],
  test: {
    environment: 'jsdom',
    // Vitest owns src unit tests only; e2e/ specs run under Playwright (`npm run e2e`).
    // Why: without an explicit include, Vitest globs every *.spec.ts and chokes on
    // Playwright's test.describe() (different runner). See CI run #559.
    include: ['src/**/*.test.{ts,tsx}'],
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
        // ARCH (plan: reference-loading-acceleration, Phase 3.3): split the
        // editor's heavy vendor deps into stable, cacheable chunks. mermaid is
        // intentionally ABSENT — it's dynamically imported (Phase 3.2) so Rollup
        // keeps it in its own async chunk; pinning it here would drag katex +
        // turndown into every mermaid render. Function form (not object) because
        // y-protocols has no root entry — only subpaths (y-protocols/awareness).
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (/[\\/]node_modules[\\/](react|react-dom|react-router-dom|zustand)[\\/]/.test(id)) return 'vendor';
          if (id.includes('@codemirror/') || id.includes('@lezer/')) return 'codemirror';
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
    // block-e2e-out-artifacts load hook (D2, plugins above) additionally guards the
    // transform/scan path. The HTML report itself was moved OUT of the root (D1,
    // playwright.config.ts) so it no longer lands here at all.
    watch: { ignored: ['**/e2e/.out/**'] },
    // WHY: the Dockerized e2e harness reaches the dev server by compose service
    // name (Host: frontend). Vite's host check rejects unknown hosts with 403 —
    // allow the service name so E2E_BASE_URL=http://frontend:5173 works.
    allowedHosts: ['frontend', 'localhost'],
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://localhost:8001',
        changeOrigin: true,
      },
      '/ws': {
        target: process.env.VITE_BACKEND_URL || 'http://localhost:8001',
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
