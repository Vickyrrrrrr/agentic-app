export type AgenticAuthUser = {
  id?: string;
  email?: string | null;
};

export type AgenticAuthSession = {
  access_token?: string | null;
  refresh_token?: string | null;
  expires_at?: number | null;
  expires_in?: number | null;
  token_type?: string;
  user?: AgenticAuthUser | null;
};

const AUTH_STORAGE_KEY = 'agentic_auth_session';

const isDesktopApp = () => typeof window !== 'undefined' && (
  'electronAPI' in window ||
  window.location.protocol === 'file:' ||
  window.location.protocol.startsWith('agentic') ||
  (typeof navigator !== 'undefined' && navigator.userAgent.includes('Electron'))
);

const apiBase = () => {
  if (typeof window === 'undefined') return '';
  const override = localStorage.getItem('agentic_api_base_url') || '';
  const envBase = import.meta.env.VITE_API_BASE_URL || '';
  const desktopBase = import.meta.env.VITE_DESKTOP_API_BASE_URL || 'http://localhost:7860';
  const devBase = import.meta.env.DEV ? '/api' : 'http://localhost:7860';
  const base = envBase || override || (isDesktopApp() ? desktopBase : devBase);
  return base.replace(/\/$/, '');
};

export function getStoredAuthSession(): AgenticAuthSession | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AgenticAuthSession;
    return parsed?.access_token ? parsed : null;
  } catch {
    return null;
  }
}

export function saveAuthSession(session: AgenticAuthSession | null): void {
  if (typeof window === 'undefined') return;
  if (!session?.access_token) {
    localStorage.removeItem(AUTH_STORAGE_KEY);
    return;
  }
  localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
}

export function clearAuthSession(): void {
  if (typeof window === 'undefined') return;
  localStorage.removeItem(AUTH_STORAGE_KEY);
}

function isExpiringSoon(session: AgenticAuthSession): boolean {
  const expiresAt = Number(session.expires_at || 0);
  if (!expiresAt) return false;
  return expiresAt <= Math.floor(Date.now() / 1000) + 120;
}

export async function refreshAuthSession(session = getStoredAuthSession()): Promise<AgenticAuthSession | null> {
  if (!session?.refresh_token) return session;
  try {
    const response = await fetch(`${apiBase()}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: session.refresh_token }),
    });
    if (!response.ok) throw new Error('Refresh failed');
    const next = (await response.json()) as AgenticAuthSession;
    if (!next.access_token) throw new Error('Refresh failed');
    saveAuthSession(next);
    return next;
  } catch {
    clearAuthSession();
    return null;
  }
}

export async function getValidAuthSession(): Promise<AgenticAuthSession | null> {
  const session = getStoredAuthSession();
  if (!session?.access_token) return null;
  if (isExpiringSoon(session)) return refreshAuthSession(session);
  return session;
}

export async function getAuthHeader(): Promise<Record<string, string>> {
  const session = await getValidAuthSession();
  return session?.access_token ? { Authorization: `Bearer ${session.access_token}` } : {};
}
