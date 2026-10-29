import { describe, it, expect } from 'vitest';
import { deriveKeyIndicator } from './key-icon';

describe('deriveKeyIndicator — public-share precedence (agent > public > widget)', () => {
  // All 8 combinations of {widget, agent, public}. agent wins over public wins
  // over widget; none set → null.
  const cases: Array<{ widget: boolean; agent: boolean; public?: boolean; want: string | null }> = [
    { widget: false, agent: false, public: false, want: null },
    { widget: true, agent: false, public: false, want: 'widget' },
    { widget: false, agent: true, public: false, want: 'agent' },
    { widget: false, agent: false, public: true, want: 'public' },
    { widget: true, agent: true, public: false, want: 'agent' },
    { widget: true, agent: false, public: true, want: 'public' },
    { widget: false, agent: true, public: true, want: 'agent' },
    { widget: true, agent: true, public: true, want: 'agent' },
  ];

  for (const c of cases) {
    const label = JSON.stringify(c);
    it(`${label} → ${c.want === null ? "''" : `'${c.want}'`}`, () => {
      expect(deriveKeyIndicator(c.widget || c.agent || c.public ? {
        widget: c.widget, agent: c.agent, public: !!c.public,
      } : null)).toBe(c.want);
    });
  }
});
