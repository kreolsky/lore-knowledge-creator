import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { families } from './palettes.mjs';

const require = createRequire(import.meta.url);
const { JSDOM, VirtualConsole } = process.env.LORE_JSDOM_PATH ? require(process.env.LORE_JSDOM_PATH) : await import('jsdom');
const html = await readFile(new URL('./index.html', import.meta.url), 'utf8');
const failures = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => failures.push(error.message));

function open(query = '') {
  return new JSDOM(html, { url: `https://lore-preview.invalid/${query}`, runScripts: 'dangerously', virtualConsole });
}
const dom = open();
try {
  const document = dom.window.document;
  for (const family of families) for (const mode of ['light', 'dark']) {
    document.querySelector(`[data-family="${family.id}"]`).click();
    document.querySelector(`[data-mode="${mode}"]`).click();
    assert.equal(document.getElementById('variant-name').textContent, family[mode].name);
    assert.equal(document.documentElement.dataset.lorePalette, family.id);
    assert.equal(document.documentElement.classList.contains('dark'), mode === 'dark');
    assert.equal(document.querySelector('.swatch-value').textContent, family[mode].bg);
    assert.equal(document.querySelectorAll('.swatch-value').length, 8);
    assert.equal(document.querySelectorAll('[data-family][aria-pressed="true"]').length, 1);
    assert.equal(new URL(dom.window.location.href).searchParams.get('palette'), family.id);
    assert.equal(new URL(dom.window.location.href).searchParams.get('mode'), mode);
  }
  document.querySelector('.original-toggle').click();
  assert.equal(document.documentElement.dataset.lorePalette, undefined);
  assert.equal(document.querySelector('.swatch-value').textContent, '#1E1E1E');
  document.querySelector('.original-toggle').click();
  assert.equal(document.querySelector('.swatch-value').textContent, families[2].dark.bg);
  assert.equal(document.querySelector('label[for="preview-message"]').htmlFor, document.querySelector('textarea').id);
  assert.equal(document.querySelectorAll('script[src],link[rel="stylesheet"]').length, 0);
  assert.equal(document.querySelector('textarea').getAttribute('aria-label'), 'Сообщение — поле предпросмотра, без отправки');
} finally { dom.window.close(); }
for (const query of ['?palette=unknown&mode=unknown', '?palette=paper&mode=dark&export=1']) {
  const fresh = open(query);
  try {
    assert.equal(fresh.window.document.getElementById('variant-name').textContent, query.includes('unknown') ? 'Porcelain' : 'Walnut');
    if (query.includes('export')) assert.equal(fresh.window.document.documentElement.dataset.export, 'true');
  } finally { fresh.window.close(); }
}
assert.deepEqual(failures, []);
console.log('PASS: six live theme switches, resolved palette values, original/restore, URL state, invalid-URL fallback, export mode, field label and zero script/CSS parse errors.');
