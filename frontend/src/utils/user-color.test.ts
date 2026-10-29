import { describe, it, expect } from 'vitest';
import { userColor, PRESENCE_PALETTE } from './user-color';

describe('userColor', () => {

  it('handles empty string without throwing', () => {
    expect(() => userColor('')).not.toThrow();
    expect(PRESENCE_PALETTE).toContain(userColor(''));
  });
});
