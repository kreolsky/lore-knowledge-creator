# Lore theme preview

Six opt-in visual variants, built from the existing Lore code at `8749962`:

| Direction | Light | Dark | Accent |
| --- | --- | --- | --- |
| Neutral | Porcelain | Graphite | Violet / lavender |
| Warm | Paper | Walnut | Plum |
| Cool | Glacier | Midnight | Blue |

Open **`index.html`** directly in a browser. It is self-contained and requires no server, account or npm installation. The controls switch direction, light/dark mode and the original theme; the swatches show the selected CSS values. URL parameters preserve a view, for example `?palette=paper&mode=dark`.

This is a visual study with a static document and chat fixture, not an authenticated application or a working editor. The composer does not send messages. The main application does not load these palettes automatically.

## Additional chat colour variants

Open **`chat.html`** for four additional dark chat palettes: Lavender, Sage, Ice and Amber. Each shows the empty chat and active conversation from the supplied screenshots side by side. Select one palette or “Все варианты” to view all four; `?chat=sage` preserves a selection and `?chat=all` shows the full comparison. The HTML is self-contained.

These variants keep Lore's neutral dark surfaces and change the selected tab, empty-chat banner/input, primary buttons, links and user message together. Light accents have dark button labels. Active-chat inputs remain neutral. Chat roles are scoped to `[data-chat-palette]`; shared sticky-note tokens are not overridden. This is a preview, not a production chat skin.

Edit `chat-palettes.mjs` for colours and `chat-preview.tsx`, `chat-preview.css`, `chat-preview.js` for fixtures, layout and switching. The regular build also generates `chat.html` using Lore's actual UI primitives. The sample reply is static content from the reference, not a current weather forecast.

The chat study passed 36 calculated text-contrast checks (minimum 5.31:1) and DOM checks for four palette switches, all eight screens, URL selection/fallback, local-file switching and field labels, with no script or CSS parse errors. The six-theme checks still pass. Browser rendering remains unverified: no browser was available. JSDOM does not resolve inherited CSS custom properties, so its checks cover token declarations and interaction, not painted colours or layout.

## Relationship to the application

- The build reads `src/index.css`, `Sidebar.module.css`, `Breadcrumb.module.css` and `ui/ListPill.module.css` from the frontend.
- Buttons, icon buttons, textareas and checkboxes are server-rendered from the actual `Button`, `IconButton`, `FieldTextarea` and `FieldCheckbox` components.
- The preview frame follows `Header`, `Sidebar`, `ChatComposer` and `editor/theme.ts`; it is not a mount of the complete application. A small explicit utility sheet supplies the classes used by the imported primitives without requiring the application's build or backend.
- Square corners, the sidebar's 220px width, the 48px header, the editor's system font and the yellow context/notes convention are retained.
- The five standard persisted highlight colours get separate fill/ink pairs in each mode. The skin matches the inline `background-color` attributes emitted by `editor/live-preview/build-structural.ts`; it does not rewrite Markdown or affect arbitrary custom hex colours. Revisit those selectors if the renderer changes its inline-style format.
- The preview directory is excluded from the main app's TypeScript project and Tailwind source scan. No production routes, theme persistence or document data are changed.

## Files

- `palettes.mjs`: editable palette definitions and CSS generation.
- `preview.tsx`, `preview.css`, `preview.js`: preview content, layout and switching.
- `build.mjs`: renders the real primitives and embeds the original styles/fonts.
- `index.html`: generated, ready-to-open preview.
- `themes.css`: generated, opt-in application skin using Lore token names.
- `tokens.json`: generated values for all six variants.
- `contrast-report.json`: calculated ratios for 72 text/background pairs.
- `verify-dom.mjs`: interaction, state, CSS parsing and palette-resolution checks.
- `verify.mjs`: colour checks and optional real-browser screenshots/layout checks.

## Rebuild and validate

```bash
cd frontend/theme-preview
npm install
npm run build
npm run check:colors
npm run check:dom
```

Colour checks need only Node and can also run without installation:

```bash
node frontend/theme-preview/verify.mjs --colors-only
```

Optional real-browser checks:

```bash
npx playwright install chromium
npm run verify
```

This exports six PNGs under `screenshots/` and checks switching plus horizontal overflow at 390, 768, 1200 and 1600px. Generated browser QA output is ignored by Git.

The local implementation run completed the preview build, 72 contrast checks and DOM interaction checks. Real-browser rendering could not run because the execution environment denied Chromium's macOS process registration. Visual layout and main-application build/test coverage remain unverified; DOM checks do not replace browser rendering.

## Try a skin in a development build

Load `themes.css` after the application's `src/index.css`, then set a direction on `<html>`:

```js
document.documentElement.dataset.lorePalette = 'porcelain'; // or paper / glacier
document.documentElement.classList.toggle('dark', true); // use Lore's normal theme control in the app
```

Removing `data-lore-palette` restores the application's default colours. Choosing and persisting a new product default is deliberately a separate decision from this study.

The copied source styles and components remain covered by the repository's license. Embedded JetBrains Mono fonts retain their original license under `frontend/src/assets/fonts/`.
