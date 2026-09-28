import { describe, expect, it } from 'vitest';
import { PASSWORD_LENGTH, generatePassword } from './generate-password';

describe('generatePassword', () => {
  it('is PASSWORD_LENGTH chars from the unambiguous alphabet, and differs per call', () => {
    const a = generatePassword();
    const b = generatePassword();
    expect(a).toHaveLength(PASSWORD_LENGTH);
    expect(a).toMatch(/^[A-HJ-NP-Za-km-z2-9!@#$%&]+$/);
    expect(a).not.toBe(b);
  });
});
