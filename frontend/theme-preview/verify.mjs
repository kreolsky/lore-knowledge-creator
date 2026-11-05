import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import { families, tokens, highlights, storedHighlights } from './palettes.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const rgb = hex => hex.match(/[a-f\d]{2}/ig).map(v => parseInt(v, 16) / 255);
function luminance(hex) { const [r,g,b] = rgb(hex).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4); return .2126*r + .7152*g + .0722*b; }
function contrast(a,b) { const x = luminance(a), y = luminance(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); }
const checks = [];
for (const family of families) for (const mode of ['light','dark']) {
  const t = tokens(family, mode);
  const pairs = [['text','bg'], ['text','surface'], ['text-muted','surface'], ['text-muted','composer-control-bg'], ['text-dim','surface'], ['on-accent','accent'], ['sticky-yellow-ink','sticky-yellow-light']];
  for (const [ink,bg] of pairs) {
    const ratio = contrast(t[ink], t[bg]);
    checks.push({ theme: `${family.id}-${mode}`, pair: `${ink} / ${bg}`, ratio: +ratio.toFixed(2) });
    assert(ratio >= 4.5, `${family.id}/${mode} ${ink}/${bg}: ${ratio.toFixed(2)}`);
  }
  highlights[mode].forEach(([bg,ink],i) => {
    const ratio = contrast(ink,bg); checks.push({ theme: `${family.id}-${mode}`, pair: `highlight ${i+1}`, ratio: +ratio.toFixed(2) });
    assert(ratio >= 4.5, `Highlight ${i+1}: ${ratio}`);
  });
}
await writeFile(path.join(root,'contrast-report.json'), JSON.stringify(checks,null,2)+'\n');
console.log(`PASS: ${checks.length} text/background contrast pairs are at least 4.5:1.`);
if (process.argv.includes('--colors-only')) process.exit(0);
const { chromium } = process.env.LORE_PLAYWRIGHT_PATH ? require(process.env.LORE_PLAYWRIGHT_PATH) : await import('playwright');
await mkdir(path.join(root,'qa'), { recursive:true });
await mkdir(path.join(root,'screenshots'), { recursive:true });
const browser = await chromium.launch({ headless:true, ...(process.env.LORE_CHROMIUM_PATH ? { executablePath: process.env.LORE_CHROMIUM_PATH } : {}) });
try {
  const page = await browser.newPage({ viewport:{ width:1600,height:1120 }, deviceScaleFactor:1 });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const url = pathToFileURL(path.join(root,'index.html')).href;
  for (const family of families) for (const mode of ['light','dark']) {
    await page.goto(`${url}?palette=${family.id}&mode=${mode}&export=1`);
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('#variant-name').textContent(), family[mode].name);
    const actual = await page.locator('.cm-color-swatch').first().evaluate(el => getComputedStyle(el).backgroundColor);
    const expected = `rgb(${rgb(highlights[mode][0][0]).map(v => Math.round(v*255)).join(', ')})`;
    assert.equal(actual, expected, 'Existing persisted marker style must be recoloured by the opt-in theme');
    await page.screenshot({ path:path.join(root,'screenshots',`${family.id}-${mode}.png`), fullPage:true });
  }
  await page.goto(url);
  await page.getByRole('button',{ name:'Тёмная',exact:true }).click();
  assert.equal(await page.locator('#variant-name').textContent(),'Graphite');
  await page.locator('[data-family="paper"]').click();
  assert.equal(await page.locator('#variant-name').textContent(),'Walnut');
  await page.getByRole('button',{ name:'Исходная тема',exact:true }).click();
  assert.equal(await page.locator('html').getAttribute('data-lore-palette'),null);
  const originalMark = await page.locator('.cm-color-swatch').first().evaluate(el => getComputedStyle(el).backgroundColor);
  assert.equal(originalMark,`rgb(${rgb(storedHighlights[0]).map(v => Math.round(v*255)).join(', ')})`);
  await page.getByRole('button',{ name:'Исходная тема',exact:true }).click();
  await page.getByRole('button',{ name:'Светлая',exact:true }).click();
  assert.equal(await page.locator('#variant-name').textContent(),'Paper');
  await page.screenshot({ path:path.join(root,'qa','studio-desktop.png'),fullPage:true });
  for (const width of [390,768,1200,1600]) {
    await page.setViewportSize({ width,height:1000 });
    const dimensions = await page.evaluate(() => ({ scroll:document.documentElement.scrollWidth,width:innerWidth }));
    assert(dimensions.scroll <= dimensions.width, `Horizontal overflow at ${width}: ${JSON.stringify(dimensions)}`);
    await page.screenshot({ path:path.join(root,'qa',`studio-${width}.png`),fullPage:true });
  }
  assert.deepEqual(errors,[]);
  console.log(`PASS: ${checks.length} contrast pairs, six renders, theme/original toggles, persisted highlights, four viewport widths, zero JS errors.`);
} finally { await browser.close(); }
