/** Title plaque of the shared preview popup: one line, above every body state. */

import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { LinkPreviewPopup } from './LinkPreviewPopup';

afterEach(cleanup);

function popup(props: Partial<Parameters<typeof LinkPreviewPopup>[0]>) {
  render(<LinkPreviewPopup visible top={0} left={0} maxHeight={300} {...props} />);
  return document.body.querySelector<HTMLElement>('[data-preview-title]');
}

describe('LinkPreviewPopup — title plaque', () => {

  it.each([
    ['image', { imageUrl: '/x.png' }],
    ['loading', { loading: true }],
    ['error', { error: true }],
    ['empty', { content: '' }],
  ])('keeps the plaque over the %s state', (_state, body) => {
    expect(popup({ title: 'T', ...body })?.textContent).toBe('T');
  });
});
