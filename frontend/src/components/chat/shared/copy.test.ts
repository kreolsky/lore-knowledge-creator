/**
 * copyWithToast (SYSTEM: chat-composer) — the three degradation/feedback
 * branches: a successful write toasts + fires onSuccess, a rejected write
 * toasts the failure, and a MISSING Clipboard API (non-secure origin:
 * plain http off localhost) degrades explicitly to the failure toast
 * instead of throwing synchronously past the .catch (no-silent-degradation).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// The real t() returns the translated string, not the key — pin the key so
// the assertions read the toast's identity, not the active locale.
vi.mock('../../../i18n', () => ({ t: (k: string) => k }));

const showToast = vi.fn();
const onSuccess = vi.fn();

function stubClipboard(writeText?: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: writeText ? { writeText } : undefined,
  });
}

describe('copyWithToast', () => {
  beforeEach(() => {
    showToast.mockClear();
    onSuccess.mockClear();
  });

  afterEach(() => {
    // jsdom leaves no own clipboard property; drop whatever the test pinned.
    delete (navigator as { clipboard?: unknown }).clipboard;
  });

  it('a successful write toasts success and fires onSuccess', async () => {
    stubClipboard(vi.fn().mockResolvedValue(undefined));
    const { copyWithToast } = await import('./copy');
    copyWithToast('hello', showToast, onSuccess);
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith('copied', 'info'));
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it('a rejected write toasts the failure and never fires onSuccess', async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error('denied')));
    const { copyWithToast } = await import('./copy');
    copyWithToast('hello', showToast, onSuccess);
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith('copyFailed', 'error'));
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('a missing Clipboard API degrades to the failure toast, never throws', async () => {
    stubClipboard(undefined);
    const { copyWithToast } = await import('./copy');
    expect(() => copyWithToast('hello', showToast, onSuccess)).not.toThrow();
    expect(showToast).toHaveBeenCalledWith('copyFailed', 'error');
    expect(onSuccess).not.toHaveBeenCalled();
  });
});
