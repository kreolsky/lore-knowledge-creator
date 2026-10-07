import type React from 'react';
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { Button, FieldInput } from '../components/ui';
import { useTranslation } from '../i18n';
import type { TranslationKey } from '../i18n/en';
import { LandingLayout } from './LandingLayout';

// WHY only 401 reads "invalid credentials": any other refusal (an Origin 403, a
// 503) is not about what the user typed, and saying so hides the real cause.
const LOGIN_ERROR_KEYS: Partial<Record<number, TranslationKey>> = {
  401: 'invalidCredentials',
  429: 'tooManyAttempts',
};

/**
 * Combined landing + login page at `/`, rendered in the shared two-column
 * landing frame (`LandingLayout`): brand left, login form right.
 * Both columns start their primary content at 25vh from the top,
 * so "Lore" logo and "Sign in" heading are vertically aligned.
 *
 * Store slices: setCurrentUser.
 */
export function LandingPage() {
  const navigate = useNavigate();
  const setCurrentUser = useAppStore(s => s.setCurrentUser);
  const { t } = useTranslation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then(r => { if (r.ok) navigate('/projects', { replace: true }); })
      .catch(() => { /* Auth check on page load — silent: user simply stays on login page */ });
  }, [navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        setError(t(LOGIN_ERROR_KEYS[res.status] ?? 'signInFailed'));
        return;
      }
      const user = await res.json();
      setCurrentUser(user);
      navigate('/projects');
    } catch {
      setError(t('signInFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <LandingLayout>
      <h2 className="text-lg font-bold text-text mb-1.5 tracking-[-0.3px]">
        {t('signIn')}
      </h2>
      <p className="text-ui-base text-text-dim mb-7">
        {t('accessInviteOnly')}
      </p>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3.5">
        <div>
          <label className="block text-xs text-text-dim mb-1.5">
            {t('email')}
          </label>
          <FieldInput
            type="email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            autoFocus
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
            autoComplete="current-password"
          />
        </div>

        {error && (
          <p className="text-xs text-red">{error}</p>
        )}

        <Button
          type="submit"
          variant="primary"
          disabled={loading || !email || !password}
        >
          {loading ? t('signingIn') : t('signInAction')}
        </Button>
      </form>
    </LandingLayout>
  );
}
