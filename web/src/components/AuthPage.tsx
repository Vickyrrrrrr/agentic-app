import { useState } from 'react';
import { supabase } from '../supabaseClient';

type AuthMode = 'login' | 'signup';

export const AuthPage = ({ onAuth }: { onAuth: () => void }) => {
  const [mode, setMode] = useState<AuthMode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccessMsg('');
    setLoading(true);

    try {
      if (mode === 'login') {
        const { error: err } = await supabase.auth.signInWithPassword({ email, password });
        if (err) throw err;
        onAuth();
      } else {
        const { error: err } = await supabase.auth.signUp({ 
          email, 
          password,
          options: {
            emailRedirectTo: window.location.origin
          }
        });
        if (err) throw err;
        setSuccessMsg('We sent you a confirmation link. Check your email to continue.');
      }
    } catch (err: any) {
      setError(err.message || 'Authentication failed');
    }
    setLoading(false);
  };

  const handleGoogleLogin = async () => {
    setError('');
    const { error: err } = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: window.location.origin
      }
    });
    if (err) setError(err.message || 'Google sign in failed');
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
            Describe any digital chip in plain English. Our multi-agent AI system writes RTL, 
            verifies logic, runs formal proofs, and prepares honest OSS layout evidence with
            commercial signoff blockers called out.
          </p>

          <div className="auth-features">
            <div className="auth-feature">
              <div className="auth-feature-icon">⚡</div>
              <div>
                <div className="auth-feature-title">15-Stage Pipeline</div>
                <div className="auth-feature-desc">From spec to silicon in one command</div>
              </div>
            </div>
            <div className="auth-feature">
              <div className="auth-feature-icon">🧠</div>
              <div>
                <div className="auth-feature-title">Self-Healing AI</div>
                <div className="auth-feature-desc">Auto-fixes bugs across verification loops</div>
              </div>
            </div>
            <div className="auth-feature">
              <div className="auth-feature-icon">🔬</div>
              <div>
                <div className="auth-feature-title">Sky130 PDK</div>
                <div className="auth-feature-desc">Open-source layout candidate output</div>
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
              ? 'Sign in to your workspace and continue to BYOK setup'
              : 'Create your account, configure BYOK, and start building'}
          </p>

          <div className="auth-onboarding-note">
            <strong>First-run path</strong>
            <span>Authenticate, save your BYOK key in the workspace, then launch a build. Public deployments do not use a shared backend LLM key.</span>
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

          <button className="auth-google-btn" onClick={handleGoogleLogin}>
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
