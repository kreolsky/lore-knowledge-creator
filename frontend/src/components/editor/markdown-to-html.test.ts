/** cleanMarkdownToHtml — inlined `ref:` images in the clipboard HTML. */
import { describe, it, expect } from 'vitest';
import { cleanMarkdownToHtml } from './markdown-to-html';

const PNG = 'data:image/png;base64,iVBORw0KGgo=';

describe('cleanMarkdownToHtml — inlined images', () => {
  // TEST-WHY: pure-logic-edge — a ref: image in the map becomes a data-URI <img> with pixel width, an svg/absent one stays alt text
  it('inlines mapped raster images and keeps everything else as alt text', () => {
    const images = new Map([
      ['ref:a', PNG],
      ['ref:b#x', PNG],
      ['ref:pct', PNG],
      ['ref:svg', 'data:image/svg+xml;base64,PHN2Zz4='],
    ]);
    const md = '![photo.jpg|400](ref:a)\n\n![b.png](ref:b#x)\n\n![c.png|50%](ref:pct)\n\n![d.svg](ref:svg)\n\n![e.png](ref:missing)';
    const html = cleanMarkdownToHtml(md, [], images);
    expect(html).toContain(`<img src="${PNG}" alt="photo.jpg" width="400">`);
    expect(html).toContain(`<img src="${PNG}" alt="b.png">`);
    expect(html).toContain(`<img src="${PNG}" alt="c.png">`);
    expect(html).not.toContain('svg+xml');
    expect(html).toContain('d.svg');
    expect(html).toContain('e.png');
    expect(html.match(/<img/g)).toHaveLength(3);
  });
});
