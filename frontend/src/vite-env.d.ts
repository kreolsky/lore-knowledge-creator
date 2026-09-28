/// <reference types="vite/client" />

// Side-effect css imports (the dsh lore-markdown stylesheet rides the lazy
// bundle import in chat/MarkdownContent.tsx). '*.module.css' keeps its own,
// stricter declaration in css-modules.d.ts.
declare module '*.css';

declare module 'turndown-plugin-gfm' {
  export function gfm(service: import('turndown')): void;
}
