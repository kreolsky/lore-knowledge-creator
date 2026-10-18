/** locateSnippet — a search hit's raw-markdown snippet found in its rendering. */
// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { locateSnippet } from './locate-snippet';

function rendered(html: string): HTMLElement {
  const root = document.createElement('div');
  root.innerHTML = html;
  return root;
}

describe('locateSnippet', () => {
  it('finds the paragraph where the snippet starts, across inline markup', () => {
    const root = rendered('<p>Intro words that come first.</p><p>Переход на Godot 4 <strong>сам по себе</strong> не решает задачу.</p>');
    const hit = locateSnippet(root, 'Переход на Godot 4 **сам по себе** не решает задачу консольного релиза');
    expect(hit?.textContent).toContain('Переход на Godot 4');
    expect(hit?.tagName).toBe('P');
  });

  it('a list item snippet lands on its item, list markers ignored', () => {
    const root = rendered('<ul><li>First item words here</li><li>Это <strong>новая игра</strong>, или проект ещё на ранней стадии</li></ul>');
    const hit = locateSnippet(root, '- Это **новая игра**, или проект ещё на ранней стадии и команда');
    expect(hit?.tagName).toBe('LI');
    expect(hit?.textContent).toContain('новая игра');
  });

  it('a link renders as its label only', () => {
    const root = rendered('<p>Other text.</p><p>See <a href="https://x.y/z">the porting docs</a> for consoles today</p>');
    const hit = locateSnippet(root, 'See [the porting docs](https://x.y/z) for consoles today');
    expect(hit?.textContent).toContain('the porting docs');
  });

  it('the longest leading run wins when the snippet runs past the rendered text', () => {
    const root = rendered('<p>alpha beta gamma delta</p><p>epsilon zeta eta theta iota</p>');
    expect(locateSnippet(root, 'epsilon zeta eta words that were edited away since')?.textContent)
      .toBe('epsilon zeta eta theta iota');
  });

  it('a snippet that is not in the message yields null', () => {
    const root = rendered('<p>alpha beta gamma delta</p>');
    expect(locateSnippet(root, 'nothing of this is here')).toBeNull();
  });
});
