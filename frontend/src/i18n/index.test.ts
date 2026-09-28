import { describe, it, expect } from 'vitest';
import { t } from './index';

describe('t() is total — a lookup miss degrades, it does not throw', () => {
  // A caller derives its key from a lookup table (halt reasons, generation
  // phases). When the value has no arm the table yields undefined, and the
  // fallback chain's last link — the key itself — is undefined too. That used
  // to reach String.replace on undefined and take the whole app down through
  // the error boundary.
  it('renders empty rather than throwing when the key is undefined', () => {
    expect(t(undefined as never)).toBe('');
  });

  it('falls back to the key itself when it is a string with no translation', () => {
    expect(t('noSuchKeyAnywhere' as never)).toBe('noSuchKeyAnywhere');
  });

  it('still interpolates a real key', () => {
    expect(t('chatHaltReasonUnknown' as never, { reason: 'turn_timeout' }))
      .toContain('turn_timeout');
  });
});
