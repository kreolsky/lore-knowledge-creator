/** Random password for the admin add / edit forms: 12 chars from an
 * unambiguous alphabet (no 0/O, 1/l/I), drawn with rejection sampling so
 * every character is equally likely. */

const CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789!@#$%&';
export const PASSWORD_LENGTH = 12;

export function generatePassword(): string {
  const limit = 256 - (256 % CHARS.length);
  let pwd = '';
  while (pwd.length < PASSWORD_LENGTH) {
    const buf = crypto.getRandomValues(new Uint8Array(16));
    for (const b of buf) {
      if (b < limit && pwd.length < PASSWORD_LENGTH) pwd += CHARS[b % CHARS.length];
    }
  }
  return pwd;
}
