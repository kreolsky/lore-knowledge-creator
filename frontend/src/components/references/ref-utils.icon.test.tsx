/** RefIcon media_type dispatch — the file branch (agent-shared archives). */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { Reference } from '../../types';
import { RefIcon } from './ref-utils';

function renderIcon(mediaType: Reference['media_type']): SVGSVGElement | null {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root: Root = createRoot(container);
  act(() => {
    root.render(<RefIcon item={{ media_type: mediaType } as Reference} />);
  });
  const svg = container.querySelector('svg');
  root.unmount();
  container.remove();
  return svg;
}

describe('RefIcon', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('renders a distinct icon for a file reference (not the markdown one)', () => {
    const file = renderIcon('file');
    const markdown = renderIcon('markdown');
    expect(file).toBeTruthy();
    expect(markdown).toBeTruthy();
    expect(file!.innerHTML).not.toBe(markdown!.innerHTML);
  });

  it('still renders the audio and image icons', () => {
    expect(renderIcon('audio')).toBeTruthy();
    expect(renderIcon('image')).toBeTruthy();
  });
});
