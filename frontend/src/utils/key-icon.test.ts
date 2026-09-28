import { describe, it, expect } from 'vitest';
import { deriveKeyIndicator, deriveKeyIconClass, keyIconClassFromCapabilities } from './key-icon';

//
// Orange public-share indicator (plan "orange-tree-icon"): a doc covered by a
// live anonymous public link paints the tree icon orange. Precedence vs the
// existing key indicator is agent (red) > public (orange) > widget (blue) > null.
//

describe('deriveKeyIndicator', () => {
  it('returns null when no flags', () => {
    expect(deriveKeyIndicator(undefined)).toBeNull();
    expect(deriveKeyIndicator(null)).toBeNull();
  });

  it('returns null when neither kind is set', () => {
    expect(deriveKeyIndicator({ widget: false, agent: false })).toBeNull();
  });

  it('returns "widget" for a widget-only doc', () => {
    expect(deriveKeyIndicator({ widget: true, agent: false })).toBe('widget');
  });

  it('returns "agent" for an agent-only doc', () => {
    expect(deriveKeyIndicator({ widget: false, agent: true })).toBe('agent');
  });

  it('AGENT wins when both kinds exist (red, not blue)', () => {
    expect(deriveKeyIndicator({ widget: true, agent: true })).toBe('agent');
  });
});

describe('deriveKeyIconClass', () => {
  it('maps to the key-agent / key-widget CSS classes', () => {
    expect(deriveKeyIconClass({ widget: true, agent: false })).toBe('key-widget');
    expect(deriveKeyIconClass({ widget: false, agent: true })).toBe('key-agent');
    expect(deriveKeyIconClass({ widget: true, agent: true })).toBe('key-agent');
  });

  it('returns "" when no indicator', () => {
    expect(deriveKeyIconClass(undefined)).toBe('');
    expect(deriveKeyIconClass({ widget: false, agent: false })).toBe('');
  });
});

describe('keyIconClassFromCapabilities', () => {
  it('maps the server key_capabilities field (agent wins)', () => {
    expect(keyIconClassFromCapabilities(undefined)).toBe('');
    expect(keyIconClassFromCapabilities([])).toBe('');
    expect(keyIconClassFromCapabilities(['widget'])).toBe('key-widget');
    expect(keyIconClassFromCapabilities(['agent'])).toBe('key-agent');
    expect(keyIconClassFromCapabilities(['agent', 'widget'])).toBe('key-agent');
  });
});

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

  it('returns null for undefined/null flags', () => {
    expect(deriveKeyIndicator(undefined)).toBeNull();
    expect(deriveKeyIndicator(null)).toBeNull();
  });
});

describe('keyIconClassFromCapabilities — publicShare arg', () => {
  it('public-only doc → key-public', () => {
    expect(keyIconClassFromCapabilities(undefined, true)).toBe('key-public');
    expect(keyIconClassFromCapabilities([], true)).toBe('key-public');
  });

  it('agent wins over public (red, not orange)', () => {
    expect(keyIconClassFromCapabilities(['agent'], true)).toBe('key-agent');
    expect(keyIconClassFromCapabilities(['agent', 'widget'], true)).toBe('key-agent');
  });

  it('public wins over widget (orange, not blue)', () => {
    expect(keyIconClassFromCapabilities(['widget'], true)).toBe('key-public');
  });

  it('no indicator when neither key nor public', () => {
    expect(keyIconClassFromCapabilities(undefined, false)).toBe('');
    expect(keyIconClassFromCapabilities([], false)).toBe('');
    expect(keyIconClassFromCapabilities(undefined, undefined)).toBe('');
  });
});
