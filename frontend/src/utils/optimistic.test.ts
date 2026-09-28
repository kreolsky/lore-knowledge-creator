import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';
import { withOptimistic } from './optimistic';

describe('withOptimistic', () => {
  const showToast = vi.fn();

  beforeEach(() => {
    showToast.mockReset();
    useAppStore.setState({ showToast });
  });

  it('keeps the server value and stays quiet on success', async () => {
    const values: string[] = [];
    await withOptimistic('new', 'old', v => values.push(v), async () => 'server');
    expect(values).toEqual(['new', 'server']);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('rolls back and tells the user on failure', async () => {
    const values: string[] = [];
    await withOptimistic('new', 'old', v => values.push(v), async () => {
      throw new Error('boom');
    });
    expect(values).toEqual(['new', 'old']);
    expect(showToast).toHaveBeenCalledWith(t('changeNotSaved'), 'error');
  });
});
