/** Public registration via a single-use invite link: /register/:token.
 *
 * Rendered in the shared landing frame (`LandingLayout`) — the same brand
 * column as the login page, register form in the right column. Shows the
 * inviter's name from GET /api/register/{token}. The form asks
 * name · email · password · repeat password; the repeat is a client-side
 * guard only — the POST body stays {name, email, password}
 * (RegisterRequest). A successful POST creates the bound role='user'
 * account, sets the session cookie server-side and navigates to / (the
 * landing /me bounce then routes to /projects).
 */

import type React from 'react';
import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { Button, FieldInput } from '../components/ui';
import { useTranslation } from '../i18n';
import { LandingLayout } from './LandingLayout';
import { refusalDetailFromBody } from '../utils/server-refusal-detail';

export function RegisterPage() {
  const { token = '' } = useParams();
  const navigate = useNavigate();
  const setCurrentUser = useAppStore(s => s.setCurrentUser);
  const { t } = useTranslation();

  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [passwordRepeat, setPasswordRepeat] = useState('');
  const [inviterName, setInviterName] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  // Inline guard: visible once the repeat field is touched and differs.
  const mismatch = passwordRepeat !== '' && password !== passwordRepeat;

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/register/${encodeURIComponent(token)}`)
      .then(async r => {
        if (cancelled) return;
        if (!r.ok) {
          setInvalid(true);
          return;
        }
        setInviterName(((await r.json()) as { inviter_name: string | null }).inviter_name);
      })
      .catch(() => { if (!cancelled) setInvalid(true); });
    return () => { cancelled = true; };
  }, [token]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mismatch) {
      setError(t('passwordsDoNotMatch'));
      return;
    }
    setError('');
    setLoading(true);
    try {
      const res = await fetch(`/api/register/${encodeURIComponent(token)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ name, email, password }),
      });
      if (!res.ok) {
        // 409 = the email is already registered; 422 = a field-level refusal
        // (pydantic detail — e.g. password length); anything else = the invite
        // went missing/used/expired since the page loaded.
        if (res.status === 409) setError(t('registerEmailTaken'));
        else if (res.status === 422)
          setError(refusalDetailFromBody(await res.json()) ?? t('registerInvalid'));
        else setError(t('registerInvalid'));
        return;
      }
      setCurrentUser(await res.json());
      navigate('/');
    } catch {
      // A network failure is not a dead invite — name it, never leave the
      // form silently blank (and never as an unhandled rejection).
      setError(t('registerNetworkError'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <LandingLayout>
      <h2 className="text-lg font-bold text-text mb-1.5 tracking-[-0.3px]">
        {t('registerTitle')}
      </h2>
      <p className="text-ui-base text-text-dim mb-7">
        {invalid
          ? t('registerInvalid')
          : inviterName !== null
            ? t('registerInvitedBy', { name: inviterName ?? t('registerInviterUnknown') })
            : ''}
      </p>

      {!invalid && (
        <form onSubmit={handleSubmit} className="flex flex-col gap-3.5">
          <div>
            <label className="block text-xs text-text-dim mb-1.5">
              {t('name')}
            </label>
            <FieldInput
              value={name}
              onChange={e => setName(e.target.value)}
              autoFocus
              autoComplete="name"
            />
          </div>

          <div>
            <label className="block text-xs text-text-dim mb-1.5">
              {t('email')}
            </label>
            <FieldInput
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              autoComplete="email"
            />
          </div>

          <div>
            <label className="block text-xs text-text-dim mb-1.5">
              {t('password')}
            </label>
            <FieldInput
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>

          <div>
            <label className="block text-xs text-text-dim mb-1.5">
              {t('passwordRepeat')}
            </label>
            <FieldInput
              type="password"
              value={passwordRepeat}
              onChange={e => setPasswordRepeat(e.target.value)}
              autoComplete="new-password"
            />
          </div>

          {(error || mismatch) && (
            <p className="text-xs text-red">{error || t('passwordsDoNotMatch')}</p>
          )}

          <Button
            type="submit"
            variant="primary"
            disabled={loading || !name || !email || !password || !passwordRepeat || mismatch}
          >
            {loading ? t('registerCreating') : t('registerAction')}
          </Button>
        </form>
      )}
    </LandingLayout>
  );
}
