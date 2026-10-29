/**
 * Tests for the link-types registry — single source of link-type knowledge.
 * Per entry: match/resolve/validate; class+tooltip; broken-toast path.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  LINK_TYPES,
  onBrokenLinkClick,
  type LinkTypeEntry,
} from './link-types';
import {
  validRefIds,
  projectRefIds,
} from './link-validity';

const entry = (kind: LinkTypeEntry['kind']) =>
  LINK_TYPES.find((e) => e.kind === kind)!;

describe('onBrokenLinkClick — a Set miss is rechecked with the server', () => {
  beforeEach(async () => {
    validRefIds.clear();
    projectRefIds.clear();
    const { clearRefPreviewCache } = await import('../../../hooks/useReferencePreview');
    clearRefPreviewCache();
    vi.restoreAllMocks();
  });

  const settle = () => new Promise((r) => setTimeout(r, 0));

  // A ref outside the client's project snapshot whose hover preview works must open on
  // click, not toast "Reference not found".
  it('ref the server has → navigates, no broken toast', async () => {
    const { apiClient } = await import('../../../api/client');
    const { useAppStore } = await import('../../../store/app-store');
    const { on, off } = await import('../../../events');
    vi.spyOn(apiClient, 'get').mockResolvedValue({ reference_id: 'r-far', title: 'Far', content: 'x' });
    const toast = vi.spyOn(useAppStore.getState(), 'showToast');
    const nav = vi.fn();
    on('navigate-to-reference', nav);
    onBrokenLinkClick(entry('ref'), 'r-far');
    await settle();
    off('navigate-to-reference', nav);
    expect(nav).toHaveBeenCalledWith({ referenceId: 'r-far' });
    expect(toast).not.toHaveBeenCalled();
  });

  it('ref the server refuses (404) → broken toast, no navigation', async () => {
    const { apiClient, HttpError } = await import('../../../api/client');
    const { useAppStore } = await import('../../../store/app-store');
    const { on, off } = await import('../../../events');
    vi.spyOn(apiClient, 'get').mockRejectedValue(new HttpError(404));
    const toast = vi.spyOn(useAppStore.getState(), 'showToast');
    const nav = vi.fn();
    on('navigate-to-reference', nav);
    onBrokenLinkClick(entry('ref'), 'r-gone');
    await settle();
    off('navigate-to-reference', nav);
    expect(nav).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(entry('ref').brokenTooltip(), 'warning');
  });

  it('any other failure → an error toast, never a false "not found"', async () => {
    const { apiClient, HttpError } = await import('../../../api/client');
    const { useAppStore } = await import('../../../store/app-store');
    vi.spyOn(apiClient, 'get').mockRejectedValue(new HttpError(500));
    // WHY mockClear: a nested spyOn on the same store method inherits the earlier tests'
    // call history (lessons/2026-09-30-vitest-nested-spyon-inherits-call-history.md).
    const toast = vi.spyOn(useAppStore.getState(), 'showToast');
    toast.mockClear();
    onBrokenLinkClick(entry('ref'), 'r-err');
    await settle();
    expect(toast.mock.calls.map((c) => c[1])).toEqual(['error']);
  });

  // The anonymous public viewer must get the "not found" toast, never an authed GET
  // whose 401 redirects it to the login page.
  it('public share: no server call, broken toast', async () => {
    const { apiClient } = await import('../../../api/client');
    const { useAppStore } = await import('../../../store/app-store');
    const { useUIStore } = await import('../../../store/ui-store');
    const get = vi.spyOn(apiClient, 'get');
    get.mockClear();
    const toast = vi.spyOn(useAppStore.getState(), 'showToast');
    toast.mockClear();
    useUIStore.setState({ isPublicShare: true });
    try {
      onBrokenLinkClick(entry('ref'), 'r-outside');
      await settle();
    } finally {
      useUIStore.setState({ isPublicShare: false });
    }
    expect(get).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(entry('ref').brokenTooltip(), 'warning');
  });

  it('types without a recheck toast immediately (doc)', async () => {
    const { useAppStore } = await import('../../../store/app-store');
    const toast = vi.spyOn(useAppStore.getState(), 'showToast');
    onBrokenLinkClick(entry('doc'), 'd-missing');
    expect(toast).toHaveBeenCalledWith(entry('doc').brokenTooltip(), 'warning');
  });
});
