import {useState} from 'react';
import {useTranslation} from 'react-i18next';

// Login gate. Preserves the selectors the onboarding/attention tests use:
// #token input and the #login-form submit button.
export function Login({onLogin}: {onLogin: (token: string) => Promise<void>}) {
  const {t} = useTranslation();
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  return (
    <section id="login" className="grid h-full place-items-center bg-surface">
      <form
        id="login-form"
        className="w-80 rounded-2xl border border-line bg-raised p-8 shadow-sm"
        onSubmit={async (e) => {
          e.preventDefault();
          try { await onLogin(token); setToken(''); } catch (err: any) { setError(err?.message || t('login.failed')); }
        }}
      >
        <p className="text-xs tracking-widest text-muted">PANGPANG</p>
        <h1 className="mt-2 text-xl font-semibold">{t('login.headline')}</h1>
        <p className="mt-1 text-sm text-muted">{t('login.subtitle')}</p>
        <label className="mt-6 block text-sm">
          {t('login.accessKey')}
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="mt-1 w-full rounded-lg border border-line px-3 py-2"
          />
        </label>
        <button className="mt-4 w-full rounded-lg bg-brand py-2 font-medium text-white">{t('login.enter')}</button>
        {error ? <p role="alert" className="mt-2 text-sm text-warn">{error}</p>: null}
      </form>
    </section>
);
}
