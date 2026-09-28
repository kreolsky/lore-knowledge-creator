/**
 * ARCH: ESLint enforces UI component library usage.
 * Raw <button>, <input>, <textarea>, <select> are errors in new/unmigrated files.
 * This prevents agents from introducing unstyled interactive elements.
 *
 * Files with intentional raw HTML (inline rename, resizer handles, file inputs,
 * CSS state machines) are listed in the legacy-exceptions block below.
 * Migrate them to UI components gradually — then remove the exception.
 */
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11y from 'eslint-plugin-jsx-a11y';

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { react, 'react-hooks': reactHooks, 'jsx-a11y': jsxA11y },
    settings: { react: { version: 'detect' } },
    rules: {
      // --- Hooks safety ---
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // --- Accessibility baseline (warn-only — gradual adoption) ---
      ...Object.fromEntries(
        Object.entries(jsxA11y.configs.recommended.rules)
          .map(([k, v]) => [k, Array.isArray(v) ? ['warn', ...v.slice(1)] : 'warn']),
      ),
      // --- Agent safety: enforce UI component library ---
      'react/forbid-elements': ['error', {
        forbid: [
          { element: 'button', message: 'Use <Button> or <IconButton> from ./ui' },
          { element: 'input', message: 'Use <FieldInput> from ./ui' },
          { element: 'textarea', message: 'Use <FieldTextarea> from ./ui' },
          { element: 'select', message: 'Use <Dropdown> from ./ui (size="lg" next to a FieldInput)' },
        ],
      }],
      // --- Agent safety: no implicit any ---
      '@typescript-eslint/no-explicit-any': 'warn',
      // Permit unused vars prefixed with _
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // Allow empty catch blocks and ternary expressions used for side effects
      'no-empty': ['error', { allowEmptyCatch: true }],
      '@typescript-eslint/no-unused-expressions': 'off',
    },
    linterOptions: {
      reportUnusedDisableDirectives: 'off',
    },
  },
  // UI primitives define the raw elements — exempt by design
  {
    files: ['src/components/ui/**'],
    rules: { 'react/forbid-elements': 'off' },
  },
  // Editor integrations (CM6 widgets, selection toolbar, etc.) — exempt
  {
    files: ['src/components/editor/**'],
    rules: { 'react/forbid-elements': 'off' },
  },
  // Legacy exceptions: files with intentional raw HTML not yet migrated.
  // Each has inline rename inputs, hover-revealed action buttons, resizer handles,
  // file inputs, or CSS state-machine elements that don't fit UI components yet.
  // TODO: migrate these to UI components and remove from this list.
  {
    files: [
      'src/components/Breadcrumb.tsx',    // inline rename input + button (ch-unit sized)
      'src/components/Sidebar.tsx',       // inline rename input + doc-item action buttons
      'src/components/DocumentTree.tsx',  // doc-item action buttons + inline rename (extracted from Sidebar)
      'src/components/references/RefCard.tsx', // gear zone action buttons (same pattern as DocumentTree)
      'src/components/ParentPickerPopup.tsx', // link-suggest-item buttons (same pattern as LinkSuggestionsPopup)
      'src/components/ReferencesPanel.tsx', // file input type="file" (hidden)
      'src/components/ApiKeyManager.tsx',  // copy button + API key input
      'src/components/PinLockScreen.tsx',  // PIN input (special styling)
      'src/pages/ProjectPage.tsx',        // resizer drag handles
      'src/components/ProjectShell.tsx',  // tab-bar buttons + resizer handles (shared chrome extracted from ProjectPage)
      'src/components/SectionShell.tsx',  // tab-bar buttons + resizer (same chrome as ProjectShell, section surfaces)
      'src/pages/AdminPage.tsx',          // user management forms + action buttons
      'src/components/Header.tsx',        // recording button (CSS state machine: recording/idle/recognizing)
    ],
    rules: { 'react/forbid-elements': 'off' },
  },
  // Ignore test files, config, and the generated dsh conversation bundle
  // (built inside the harness image, see harness-driver/conversation/).
  {
    ignores: ['**/*.test.ts', '**/*.test.tsx', 'vite.config.ts', 'src/dsh/**'],
  },
);
