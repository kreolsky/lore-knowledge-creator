/** Cabinet section: recolor the active theme token by token, reset one or all, export/import both themes as a JSON backup.
 * Store slices: ui-store (theme, colorOverrides, setColorOverride, replaceColorOverrides, flushColorSave). */

import { useEffect, useState } from 'react';
import { Download, RotateCcw, Upload } from 'lucide-react';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { COLOR_GROUPS, COLOR_TOKENS, type ColorOverrides } from '../../theme/color-tokens';
import { buildColorBackup, parseColorBackup, readDefaultColor } from '../../theme/apply-colors';
import { contrastRatio } from '../../theme/color-math';
import { Button, CenterHeading, ColorField, IconButton, Modal } from '../ui';
import { useTranslation, type TranslationKey } from '../../i18n';

/** Rows that show a WCAG warning, and the backgrounds they are checked against. */
const CONTRAST_CHECKS: Record<string, string[]> = {
  '--text': ['--bg', '--surface'],
  '--text-muted': ['--bg', '--surface'],
};
const MIN_CONTRAST = 4.5;

type PendingImport = { colors: ColorOverrides; dropped: number };

function readDefaults(): Record<string, string> {
  return Object.fromEntries(COLOR_TOKENS.map(t => [t.token, readDefaultColor(t.token)]));
}

function downloadBackup(overrides: ColorOverrides): void {
  const url = URL.createObjectURL(new Blob([buildColorBackup(overrides)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = 'lore-colors.json';
  a.click();
  URL.revokeObjectURL(url);
}

/** WHY: an imperative picker — the file input never renders, so no raw <input> in JSX. */
function pickJsonFile(onText: (text: string | null) => void): void {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'application/json,.json';
  input.onchange = () => {
    const file = input.files?.[0];
    if (!file) return;
    file.text().then(onText, () => onText(null));
  };
  input.click();
}

const IMPORT_ERRORS: Record<string, TranslationKey> = {
  invalidJson: 'colorsImportInvalidJson',
  wrongFormat: 'colorsImportWrongFormat',
  unsupportedVersion: 'colorsImportUnsupportedVersion',
};

export function ColorsSection() {
  const { t } = useTranslation();
  const theme = useUIStore(s => s.theme);
  const overrides = useUIStore(s => s.colorOverrides);
  const setColorOverride = useUIStore(s => s.setColorOverride);
  const replaceColorOverrides = useUIStore(s => s.replaceColorOverrides);
  const flushColorSave = useUIStore(s => s.flushColorSave);
  const showToast = useAppStore(s => s.showToast);
  const [defaults, setDefaults] = useState<Record<string, string>>(readDefaults);
  const [confirmReset, setConfirmReset] = useState(false);
  const [pendingImport, setPendingImport] = useState<PendingImport | null>(null);

  // Defaults follow the theme class, which another component's effect toggles —
  // read them on the next frame, once the class is in place.
  useEffect(() => {
    const frame = requestAnimationFrame(() => setDefaults(readDefaults()));
    return () => cancelAnimationFrame(frame);
  }, [theme]);

  // A debounced pick still saves when the user leaves the section.
  useEffect(() => flushColorSave, [flushColorSave]);

  const map = overrides[theme];
  // A token not set by the user but derived from an overridden base shows the derived
  // shade (what is on screen), not the CSS default. Pure over state, so no stale inline read.
  const derivedFor = (token: string) =>
    COLOR_TOKENS.find(e => e.token in map && e.derived?.[theme]?.[token])?.derived?.[theme]?.[token];
  const effective = (token: string) => map[token] ?? derivedFor(token) ?? defaults[token] ?? '';
  const themeName = t(theme === 'dark' ? 'colorsThemeDark' : 'colorsThemeLight');
  const tokenLabel = (token: string) => {
    const entry = COLOR_TOKENS.find(e => e.token === token);
    return entry ? t(entry.label) : token;
  };

  const handleImport = () => pickJsonFile(text => {
    if (text === null) { showToast(t('colorsImportReadFailed'), 'error'); return; }
    const result = parseColorBackup(text);
    if (!result.ok) { showToast(t(IMPORT_ERRORS[result.error]), 'error'); return; }
    setPendingImport({ colors: result.colors, dropped: result.dropped.light + result.dropped.dark });
  });

  const lowContrast = (token: string): string[] =>
    (CONTRAST_CHECKS[token] ?? []).flatMap(bg => {
      const ratio = contrastRatio(effective(token), effective(bg));
      return ratio !== null && ratio < MIN_CONTRAST
        ? [t('colorsLowContrast', { against: tokenLabel(bg), ratio: ratio.toFixed(1) })]
        : [];
    });

  return (
    <div>
      <CenterHeading>{t('cabinetSectionColors')}</CenterHeading>
      <p className="text-xs text-text-dim mb-1">{t('colorsDescription')}</p>
      <p className="text-xs text-text-muted mb-3">{t('colorsEditingTheme', { theme: themeName })}</p>
      <div className="flex gap-2 mb-6 flex-wrap">
        <Button onClick={() => downloadBackup(overrides)}><Download size={14} />{t('colorsExport')}</Button>
        <Button onClick={handleImport}><Upload size={14} />{t('colorsImport')}</Button>
        <Button onClick={() => setConfirmReset(true)} disabled={!Object.keys(map).length}>{t('colorsResetAll')}</Button>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_auto_28px] gap-x-3 gap-y-2 items-center">
        {COLOR_GROUPS.map(g => (
          <div key={g.group} className="contents">
            <div className="col-span-3 mt-4 text-ui-sm font-semibold text-text-muted">{t(g.label)}</div>
            {COLOR_TOKENS.filter(e => e.group === g.group).map(e => {
              const overridden = e.token in map;
              const warnings = lowContrast(e.token);
              return (
                <div key={e.token} className="contents">
                  <div className="text-sm">
                    {t(e.label)}
                    {warnings.map(w => <p key={w} className="text-xs text-amber m-0">{w}</p>)}
                  </div>
                  <ColorField
                    value={effective(e.token)}
                    onChange={hex => setColorOverride(theme, e.token, hex)}
                    aria-label={t(e.label)}
                  />
                  {overridden ? (
                    <IconButton onClick={() => setColorOverride(theme, e.token, null)} title={t('colorResetOne')} aria-label={t('colorResetOne')}>
                      <RotateCcw size={14} />
                    </IconButton>
                  ) : <span />}
                </div>
              );
            })}
          </div>
        ))}
      </div>

      <Modal
        open={confirmReset}
        onClose={() => setConfirmReset(false)}
        title={t('colorsResetAll')}
        closeLabel={t('close')}
        footer={(
          <>
            <Button variant="ghost" onClick={() => setConfirmReset(false)}>{t('cancel')}</Button>
            <Button variant="danger" onClick={() => {
              replaceColorOverrides({ ...overrides, [theme]: {} });
              setConfirmReset(false);
            }}>{t('colorsResetAll')}</Button>
          </>
        )}
      >
        <p className="text-ui-base m-0">{t('colorsResetAllConfirm', { theme: themeName })}</p>
      </Modal>

      <Modal
        open={pendingImport !== null}
        onClose={() => setPendingImport(null)}
        title={t('colorsImportTitle')}
        closeLabel={t('close')}
        footer={(
          <>
            <Button variant="ghost" onClick={() => setPendingImport(null)}>{t('cancel')}</Button>
            <Button variant="primary" onClick={() => {
              if (pendingImport) replaceColorOverrides(pendingImport.colors);
              setPendingImport(null);
            }}>{t('colorsImportApply')}</Button>
          </>
        )}
      >
        {pendingImport && (
          <div className="flex flex-col gap-2 text-ui-base">
            <p className="m-0">{t('colorsImportSummary', {
              light: Object.keys(pendingImport.colors.light).length,
              dark: Object.keys(pendingImport.colors.dark).length,
            })}</p>
            {pendingImport.dropped > 0 && <p className="m-0 text-amber">{t('colorsImportDropped', { count: pendingImport.dropped })}</p>}
            <p className="m-0 text-text-dim">{t('colorsImportReplace')}</p>
          </div>
        )}
      </Modal>
    </div>
  );
}
