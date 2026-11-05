import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { createRequire } from 'node:module';
import { families, makeCSS, tokens } from './palettes.mjs';
import { chatPaletteCSS } from './chat-palettes.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
// Optional paths permit using already installed/cached tools without changing the app's dependencies.
const esbuild = process.env.LORE_ESBUILD_PATH ? require(process.env.LORE_ESBUILD_PATH) : await import('esbuild');
await mkdir(path.join(root, '.build'), { recursive: true });
await esbuild.build({ entryPoints: [path.join(root, 'preview.tsx')], outfile: path.join(root, '.build/render.cjs'), bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', nodePaths: [process.env.LORE_NODE_MODULES || path.join(root, 'node_modules')], logLevel: 'warning' });
const { renderPreview } = await import(pathToFileURL(path.join(root, '.build/render.cjs')));
let source = await readFile(path.join(root, '../src/index.css'), 'utf8');
source = source.replace('@import "tailwindcss";', '/* Tailwind primitives used here are supplied by preview.css. */');
source = source.replace('@source not "../theme-preview";', '');
// Browser-ready equivalent of Tailwind's variable block (the utility subset is local).
source = source.replace('@theme {', ':root {');
// The preview is portable: inline the two original local font files.
for (const name of ['Regular', 'SemiBold']) {
  const font = await readFile(path.join(root, `../src/assets/fonts/JetBrainsMono-${name}.woff2`));
  source = source.replace(`./assets/fonts/JetBrainsMono-${name}.woff2`, `data:font/woff2;base64,${font.toString('base64')}`);
}
for (const filename of ['Sidebar.module.css', 'Breadcrumb.module.css', 'ui/ListPill.module.css']) {
  const moduleCSS = await readFile(path.join(root, '../src/components', filename), 'utf8');
  source += '\n' + moduleCSS.replace(/:global\(([^)]+)\)/g, '$1');
}
const skin = makeCSS();
const shell = await readFile(path.join(root, 'preview.css'), 'utf8');
const behavior = await readFile(path.join(root, 'preview.js'), 'utf8');
const html = `<!doctype html>\n<html lang="ru" data-lore-palette="porcelain"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lore — варианты светлой и тёмной темы</title><style>${source}\n${shell}\n${skin}</style></head><body>${renderPreview()}<script type="application/json" id="palette-data">${JSON.stringify(families).replaceAll('<','\\u003c')}</script><script>${behavior}</script></body></html>\n`;
await writeFile(path.join(root, 'index.html'), html);
await writeFile(path.join(root, 'themes.css'), skin);
await writeFile(path.join(root, 'tokens.json'), JSON.stringify(Object.fromEntries(families.flatMap(f => ['light','dark'].map(mode => [`${f.id}-${mode}`, tokens(f, mode)]))), null, 2) + '\n');
await esbuild.build({ entryPoints: [path.join(root, 'chat-preview.tsx')], outfile: path.join(root, '.build/chat-render.cjs'), bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', nodePaths: [process.env.LORE_NODE_MODULES || path.join(root, 'node_modules')], logLevel: 'warning' });
const { renderChatPreview } = await import(pathToFileURL(path.join(root, '.build/chat-render.cjs')));
const chatShell = await readFile(path.join(root, 'chat-preview.css'), 'utf8');
const chatBehavior = await readFile(path.join(root, 'chat-preview.js'), 'utf8');
const chatHTML = `<!doctype html>\n<html lang="ru" class="dark"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lore — четыре цвета для чата</title><style>${source}\n${shell}\n${chatShell}\n${chatPaletteCSS()}</style></head><body>${renderChatPreview()}<script>${chatBehavior}</script></body></html>\n`;
await writeFile(path.join(root, 'chat.html'), chatHTML);
console.log('Built index.html, chat.html, themes.css, tokens.json from Lore source and UI components.');
