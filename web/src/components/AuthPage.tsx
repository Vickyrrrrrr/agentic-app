import { useState } from 'react';
import { api, API_BASE } from '../api';
import { saveAuthSession } from '../authSession';
import { toUserError } from '../utils/errorFormatter';

type AuthMode = 'login' | 'signup';
type AgenticElectronWindow = Window & {
  electronAPI?: {
    openExternal?: (url: string) => Promise<{ success: boolean }>;
  };
};

export const AuthPage = ({ onAuth }: { onAuth: () => void }) => {
  const [mode, setMode] = useState<AuthMode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  const openGoogleSignIn = async () => {
    setError('');
    setSuccessMsg('');
    const url = `${API_BASE}/auth/google/start`;
    const desktopOpen = (window as AgenticElectronWindow).electronAPI?.openExternal;
    const result = desktopOpen ? await desktopOpen(url) : null;
    if (!result?.success) {
      const opened = window.open(url, '_blank', 'noopener,noreferrer');
      if (!opened) window.location.href = url;
    }
    setSuccessMsg('Google sign-in opened in your browser. Choose Open AgentIC Desktop after sign-in.');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccessMsg('');
    setLoading(true);

    try {
      if (mode === 'login') {
        const { data } = await api.post('/auth/password-login', { email, password });
        if (!data?.access_token) throw new Error('Sign-in failed. Check your email and password.');
        saveAuthSession(data);
        onAuth();
      } else {
        const { data } = await api.post('/auth/signup', { email, password });
        if (data?.access_token) {
          saveAuthSession(data);
          onAuth();
        } else {
          setSuccessMsg(data?.message || 'Account created. Check your email to continue.');
        }
      }
    } catch (err: any) {
      setError(toUserError(err, 'Authentication failed. Please try again.'));
    }
    setLoading(false);
  };

  return (
    <div className="auth-root">
      {/* ── Left Panel: Brand Story ── */}
      <div className="auth-left">
        <div className="auth-left-content">
          <div className="auth-logo">
            <div className="auth-logo-mark">
              <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                <rect width="32" height="32" rx="8" fill="currentColor" opacity="0.12"/>
                <path d="M8 24V8l8 4v8l8-4" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
                <circle cx="24" cy="20" r="3" fill="currentColor" opacity="0.5"/>
              </svg>
            </div>
            <span className="auth-logo-text">AgentIC</span>
          </div>

          <h1 className="auth-headline">
            Text to Silicon.<br/>
            <span className="auth-headline-dim">Autonomously.</span>
          </h1>

          <p className="auth-value-prop">
            Sign in to verify your AgentIC license. Chip source, prompts, logs, PDK files, and
            generated artifacts stay local on this machine.
          </p>

          <div className="auth-features">
            <div className="auth-feature">
              <div className="auth-feature-icon">A</div>
              <div>
                <div className="auth-feature-title">Local Workspace</div>
                <div className="auth-feature-desc">Design files remain under AgentIC-workspace</div>
              </div>
            </div>
            <div className="auth-feature">
              <div className="auth-feature-icon">B</div>
              <div>
                <div className="auth-feature-title">BYOK Model Access</div>
                <div className="auth-feature-desc">Use your configured OpenAI-compatible provider</div>
              </div>
            </div>
            <div className="auth-feature">
              <div className="auth-feature-icon">C</div>
              <div>
                <div className="auth-feature-title">Cloud License Only</div>
                <div className="auth-feature-desc">Account status and usage counts only</div>
              </div>
            </div>
          </div>

          <div className="auth-left-footer">
            <span>Trusted by chip designers worldwide</span>
          </div>
        </div>
      </div>

      {/* ── Right Panel: Auth Form ── */}
      <div className="auth-right">
        <div className="auth-form-wrapper">
          <h2 className="auth-form-title">
            {mode === 'login' ? 'Welcome back' : 'Get started'}
          </h2>
          <p className="auth-form-sub">
            {mode === 'login'
              ? 'Sign in to verify your license and continue to local setup'
              : 'Create your account, then complete checkout to unlock AgentIC'}
          </p>

          <div className="auth-onboarding-note">
            <strong>Local-first license check</strong>
            <span>AgentIC cloud verifies payment only. The silicon workspace stays on your machine.</span>
          </div>

          <form className="auth-form" onSubmit={handleSubmit}>
            <div className="auth-field">
              <label className="auth-label" htmlFor="auth-email">Email address</label>
              <input
                id="auth-email"
                type="email"
                className="auth-input"
                placeholder="you@company.com"
                value={email}
                onChange={e => setEmail(e.target.value)}
                required
                autoFocus
                autoComplete="email"
              />
            </div>

            <div className="auth-field">
              <label className="auth-label" htmlFor="auth-password">Password</label>
              <input
                id="auth-password"
                type="password"
                className="auth-input"
                placeholder="••••••••"
                value={password}
                onChange={e => setPassword(e.target.value)}
                required
                minLength={6}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              />
            </div>

            {error && (
              <div className="auth-alert auth-alert--error">
                <span className="auth-alert-icon">✕</span>
                {error}
              </div>
            )}
            {successMsg && (
              <div className="auth-alert auth-alert--success">
                <span className="auth-alert-icon">✓</span>
                {successMsg}
              </div>
            )}

            <button
              type="submit"
              className="auth-submit"
              disabled={loading || !email || !password}
            >
              {loading ? (
                <span className="auth-submit-loading">
                  <span className="auth-spinner" />
                  {mode === 'login' ? 'Signing in…' : 'Creating account…'}
                </span>
              ) : (
                mode === 'login' ? 'Continue' : 'Create account'
              )}
            </button>
          </form>

          <div className="auth-provider-divider">
            <span className="auth-provider-line"></span>
            <span className="auth-provider-text">OR</span>
            <span className="auth-provider-line"></span>
          </div>

          <button className="auth-google-btn" onClick={openGoogleSignIn}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
              <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
              <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
              <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
            </svg>
            Continue with Google
          </button>

          <div className="auth-divider">
            <span>{mode === 'login' ? "Don't have an account?" : 'Already have an account?'}</span>
          </div>

          <button
            className="auth-switch"
            onClick={() => { setMode(mode === 'login' ? 'signup' : 'login'); setError(''); setSuccessMsg(''); }}
          >
            {mode === 'login' ? 'Create account' : 'Sign in instead'}
          </button>

          <p className="auth-legal">
            By continuing, you agree to AgentIC's Terms of Service and Privacy Policy.
          </p>
        </div>
      </div>
    </div>
  );
};
