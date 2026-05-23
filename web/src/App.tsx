import { Suspense, lazy, useEffect, useState } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import type { Session, AuthChangeEvent } from '@supabase/supabase-js';
import { supabase } from './supabaseClient';
import { LandingPage } from './pages/LandingPage';
import { api } from './api';
import { BillingModal } from './components/BillingModal';
import { ErrorBoundary, PageErrorBoundary } from './components/ErrorBoundary';
import { queryClient } from './lib/query-client';
import './index.css';
import type { LucideIcon } from 'lucide-react';
import {
  Zap,
  BookOpen,
  TerminalSquare,
  ClipboardList,
  Settings2,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react';

const AUTH_ENABLED = Boolean(import.meta.env.VITE_SUPABASE_URL);
const IS_DESKTOP_APP = typeof window !== 'undefined' && 'electronAPI' in window;
const BUILD_FLAVOR = import.meta.env.DEV ? 'dev' : 'built';

const DesignStudio = lazy(() =>
  import('./pages/DesignStudio').then((m) => ({ default: m.DesignStudio }))
);
const Pricing = lazy(() =>
  import('./pages/Pricing').then((m) => ({ default: m.Pricing }))
);
const Documentation = lazy(() =>
  import('./pages/Documentation').then((m) => ({ default: m.Documentation }))
);
const EDALab = lazy(() =>
  import('./pages/EDALab').then((m) => ({ default: m.EDALab }))
);
const BuildHistory = lazy(() =>
  import('./pages/BuildHistory').then((m) => ({ default: m.BuildHistory }))
);
const WorkspaceSettings = lazy(() =>
  import('./pages/WorkspaceSettings').then((m) => ({ default: m.WorkspaceSettings }))
);

type PageKey =
  | 'Design Studio'
  | 'Build History'
  | 'Manual EDA Lab'
  | 'Documentation'
  | 'Workspace Settings';

type DesignOption = { name: string; has_gds: boolean };

type JobSummary = {
  job_id: string;
  design_name: string;
  status: string;
  current_state: string;
  created_at: number;
  event_count: number;
};

type ProfileSummary = {
  auth_enabled: boolean;
  plan?: string;
  successful_builds?: number;
  workspace_successful_builds?: number;
  total_builds?: number;
  running_builds?: number;
  failed_builds?: number;
  active_designs?: number;
  has_byok_key?: boolean;
  email?: string;
};

type NavGroup = {
  label: string;
  items: Array<{ page: PageKey; label: string; icon: LucideIcon }>;
};

const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Workspace',
    items: [
      { page: 'Design Studio', label: 'New Conversation', icon: Zap },
      { page: 'Build History', label: 'Conversation History', icon: ClipboardList },
      { page: 'Manual EDA Lab', label: 'Manual EDA Lab', icon: TerminalSquare },
      { page: 'Documentation', label: 'Documentation', icon: BookOpen },
      { page: 'Workspace Settings', label: 'Settings', icon: Settings2 },
    ],
  }
];

const PAGE_META: Record<PageKey, { title: string; subtitle: string }> = {
  'Design Studio': {
    title: 'AgentIC Studio',
    subtitle: 'Synthesize synthesizable silicon through natural language',
  },
  'Build History': {
    title: 'Conversation History',
    subtitle: 'Track past builds, states, and execution history',
  },
  'Manual EDA Lab': {
    title: 'Manual EDA Lab',
    subtitle: 'Run syntax, synthesis, simulation, and waveform analysis directly',
  },
  Documentation: {
    title: 'Technical Documentation',
    subtitle: 'Architecture references, pipeline specs, and config contracts',
  },
  'Workspace Settings': {
    title: 'Workspace Settings',
    subtitle: 'Manage model keys, plan context, and operator configuration',
  },
};

const App = () => {
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(AUTH_ENABLED);
  const [selectedPage, setSelectedPage] = useState<PageKey>('Design Studio');
  const [showPricing, setShowPricing] = useState(() =>
    typeof window !== 'undefined' && window.location.pathname === '/pricing'
  );
  const [designs, setDesigns] = useState<DesignOption[]>([]);
  const [selectedDesign, setSelectedDesign] = useState<string>('');
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [profile, setProfile] = useState<ProfileSummary | null>(null);
  const [showBillingModal, setShowBillingModal] = useState(false);
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('agentic-theme');
    return saved === 'light' || saved === 'dark' ? saved : 'dark';
  });
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  useEffect(() => {
    if (!AUTH_ENABLED) {
      return;
    }
    supabase.auth.getSession().then(({ data: { session: s } }: { data: { session: Session | null } }) => {
      setSession(s);
      setAuthLoading(false);
    }).catch(() => setAuthLoading(false));
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event: AuthChangeEvent, s: Session | null) => {
      setSession(s);
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('agentic-theme', theme);
  }, [theme]);

  // Capture prompt from landing page on successful session
  useEffect(() => {
    if (session) {
      const landingPrompt = localStorage.getItem('agentic_landing_prompt');
      if (landingPrompt) {
        localStorage.removeItem('agentic_landing_prompt');
        localStorage.setItem('agentic_studio_initial_prompt', landingPrompt);
        const landingPdk = localStorage.getItem('agentic_landing_pdk');
        if (landingPdk) {
          localStorage.setItem('agentic_studio_initial_pdk', landingPdk);
          localStorage.removeItem('agentic_landing_pdk');
        }
        setSelectedPage('Design Studio');
      }
    }
  }, [session]);

  // Handle browser back/forward navigation for pricing page
  useEffect(() => {
    const handlePop = () => {
      setShowPricing(window.location.pathname === '/pricing');
    };
    window.addEventListener('popstate', handlePop);
    return () => window.removeEventListener('popstate', handlePop);
  }, []);

  // Override pushState/replaceState to track pricing page in history
  useEffect(() => {
    const originalPush = window.history.pushState.bind(window.history);
    const originalReplace = window.history.replaceState.bind(window.history);

    window.history.pushState = (...args) => {
      originalPush(...args);
      const path = args[2] || '';
      if (typeof path === 'string') {
        setShowPricing(path === '/pricing');
      }
    };
    window.history.replaceState = (...args) => {
      originalReplace(...args);
      const path = args[2] || '';
      if (typeof path === 'string') {
        setShowPricing(path === '/pricing');
      }
    };

    return () => {
      window.history.pushState = originalPush;
      window.history.replaceState = originalReplace;
    };
  }, []);

  useEffect(() => {
    if (AUTH_ENABLED && !session) return;
    let cancelled = false;

    const loadWorkspaceData = async () => {
      const [designRes, jobsRes, profileRes] = await Promise.allSettled([
        api.get('/designs'),
        api.get('/jobs'),
        api.get('/profile'),
      ]);
      if (cancelled) return;

      const rawDesigns: DesignOption[] =
        designRes.status === 'fulfilled' ? designRes.value.data?.designs || [] : [];
      const rawJobs: JobSummary[] =
        jobsRes.status === 'fulfilled' ? jobsRes.value.data?.jobs || [] : [];
      const nextProfile: ProfileSummary | null =
        profileRes.status === 'fulfilled' ? profileRes.value.data || null : null;

      setJobs(rawJobs);
      setProfile(nextProfile);

      const designMap = new Map<string, DesignOption>();
      for (const design of rawDesigns) {
        if (!design?.name) continue;
        designMap.set(design.name, {
          name: design.name,
          has_gds: Boolean(design.has_gds),
        });
      }

      // Fallback when /designs is empty: derive design names from /jobs.
      for (const job of rawJobs) {
        if (!job.design_name || designMap.has(job.design_name)) continue;
        designMap.set(job.design_name, { name: job.design_name, has_gds: false });
      }

      const mergedDesigns = Array.from(designMap.values()).sort((a, b) => a.name.localeCompare(b.name));
      setDesigns(mergedDesigns);
      setSelectedDesign((prev) => {
        if (prev && mergedDesigns.some((d) => d.name === prev)) return prev;
        if (mergedDesigns.length === 0) return '';
        const withGds = mergedDesigns.find((d) => d.has_gds);
        return withGds ? withGds.name : mergedDesigns[0].name;
      });
    };

    loadWorkspaceData().catch((err) => {
      // Non-fatal: keep workspace usable even if API context fetch fails.
      console.error('Failed to load workspace context', err);
      setDesigns([]);
      setJobs([]);
      setProfile(null);
      setSelectedDesign('');
    });

    const refreshInterval = window.setInterval(() => {
      loadWorkspaceData().catch((err) => {
        console.error('Failed to refresh workspace context', err);
      });
    }, 15000);

    const handleVisibilityRefresh = () => {
      if (document.visibilityState === 'visible') {
        loadWorkspaceData().catch((err) => {
          console.error('Failed to refresh workspace context', err);
        });
      }
    };

    window.addEventListener('focus', handleVisibilityRefresh);
    document.addEventListener('visibilitychange', handleVisibilityRefresh);

    return () => {
      cancelled = true;
      window.clearInterval(refreshInterval);
      window.removeEventListener('focus', handleVisibilityRefresh);
      document.removeEventListener('visibilitychange', handleVisibilityRefresh);
    };
  }, [session]);

  const handleLogout = async () => {
    await supabase.auth.signOut();
    setSession(null);
    setSelectedPage('Design Studio');
  };

  if (authLoading) {
    return (
      <div className="workspace-page-loader">
        <div className="premium-loader">
          <span className="premium-loader-dot" />
          <span className="premium-loader-dot" />
          <span className="premium-loader-dot" />
        </div>
        <span>Loading AgentIC...</span>
      </div>
    );
  }

  if (AUTH_ENABLED && !session) {
    return (
      <LandingPage
        onAuthSuccess={() =>
          supabase.auth
            .getSession()
            .then(({ data: { session: s } }: { data: { session: Session | null } }) => setSession(s))
        }
      />
    );
  }

  // Dev mode: landing page preview with skip-to-app button
  if (!AUTH_ENABLED && !session) {
    return (
      <div style={{ position: 'relative' }}>
        <LandingPage onAuthSuccess={() => {}} />
        <button
          onClick={() => setSession({ user: { email: 'dev@localhost' } } as unknown as Session)}
          style={{
            position: 'fixed', bottom: '1rem', right: '1rem',
            background: '#27272A', color: '#71717A',
            border: '1px solid #3F3F46', borderRadius: '8px',
            padding: '0.5rem 1rem', fontSize: '0.78rem',
            cursor: 'pointer', zIndex: 100,
          }}
        >
          Skip to Dashboard
        </button>
      </div>
    );
  }

  const currentPageMeta = PAGE_META[selectedPage];

  const renderPage = () => {
    switch (selectedPage) {
      case 'Design Studio':
        return <DesignStudio />;
      case 'Manual EDA Lab':
        return <EDALab />;
      case 'Documentation':
        return <Documentation />;
      case 'Build History':
        return (
          <BuildHistory
            jobs={jobs}
            selectedDesign={selectedDesign}
            onSelectDesign={setSelectedDesign}
            onOpenPage={(page) => {
              if (page === 'Design Studio') {
                setSelectedPage(page);
              }
            }}
          />
        );
      case 'Workspace Settings':
        return (
          <WorkspaceSettings
            profile={profile}
            sessionEmail={session?.user.email || ''}
            onOpenByok={() => setShowBillingModal(true)}
          />
        );
      default:
        return <DesignStudio />;
    }
  };

  return (
    <QueryClientProvider client={queryClient}>
      <ErrorBoundary>
        <div className={`app-shell workspace-shell${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
          {/* Mobile sidebar overlay */}
          <div
            className={`sidebar-overlay${mobileMenuOpen ? ' active' : ''}`}
            onClick={() => setMobileMenuOpen(false)}
          />

          <aside className={`app-sidebar${mobileMenuOpen ? ' mobile-open' : ''}`}>
            <div className="app-brand">
              <div className="app-brand-logo">A</div>
              <div>
                <div className="app-brand-title">AgentIC</div>
                <div className="app-brand-sub">Autonomous Silicon Workspace</div>
              </div>
              <button
                className="app-sidebar-collapse"
                onClick={() => setSidebarCollapsed((value) => !value)}
                title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              >
                {sidebarCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
              </button>
            </div>

            {NAV_GROUPS.map((group) => (
              <div className="app-sidebar-group" key={group.label}>
                <div className="app-sidebar-label">{group.label}</div>
                <div className="app-nav">
                  {group.items.map((item) => {
                    const Icon = item.icon;
                    return (
                      <button
                        key={item.page}
                        className={`app-nav-btn${selectedPage === item.page ? ' active' : ''}`}
                        onClick={() => {
                          setSelectedPage(item.page);
                          setMobileMenuOpen(false);
                        }}
                      >
                        <Icon size={16} className="nav-icon" />
                        <span>{item.label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}

            <div className="app-sidebar-footer">
              <button className="theme-toggle" onClick={() => setShowBillingModal(true)}>
                Configure BYOK Keys
              </button>
              <div className="app-version">
                {profile?.plan ? `Plan: ${profile.plan}` : 'Local Workspace'} · v3.0
                {IS_DESKTOP_APP ? ` · Desktop ${BUILD_FLAVOR}` : ''}
              </div>
            </div>
          </aside>

          <main className="app-main">
            <header className="app-topbar workspace-topbar">
              <div className="workspace-title-wrap">
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <button
                    className="mobile-menu-btn"
                    onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
                    aria-label="Toggle navigation menu"
                  >
                    <Menu size={20} />
                  </button>
                  <h1>{currentPageMeta.title}</h1>
                </div>
                <div className="app-topbar-meta">{currentPageMeta.subtitle}</div>
              </div>

              <div className="workspace-topbar-actions">
                {designs.length > 0 && (
                  <select
                    className="app-design-select"
                    value={selectedDesign}
                    onChange={(e) => setSelectedDesign(e.target.value)}
                    title="Select design context"
                    aria-label="Select design context"
                    spellCheck={false}
                  >
                    {designs.map((d) => (
                      <option key={d.name} value={d.name}>
                        {d.name}
                        {d.has_gds ? ' · GDS' : ''}
                      </option>
                    ))}
                  </select>
                )}

                <span className="workspace-plan-badge">
                  {profile?.auth_enabled ? (profile?.plan || 'free') : 'local'}
                </span>

                <button
                  className="top-nav-btn"
                  onClick={() => setTheme((t) => (t === 'light' ? 'dark' : 'light'))}
                  title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
                >
                  {theme === 'light' ? 'Dark' : 'Light'}
                </button>

                {session && (
                  <button className="top-nav-btn" onClick={handleLogout} title="Sign out">
                    Sign Out
                  </button>
                )}
              </div>
            </header>

            <section className="app-content">
              <ErrorBoundary>
                <Suspense
                  fallback={
                    <div className="skeleton-page">
                      <div className="skeleton skeleton-hero" />
                      <div className="skeleton-grid">
                        <div className="skeleton skeleton-card" />
                        <div className="skeleton skeleton-card" />
                        <div className="skeleton skeleton-card" />
                        <div className="skeleton skeleton-card" />
                      </div>
                      <div className="skeleton skeleton-block" />
                      <div className="skeleton skeleton-block-sm" />
                    </div>
                  }
                >
                  <PageErrorBoundary>
                    <div key={selectedPage} className="page-transition">
                      {renderPage()}
                    </div>
                  </PageErrorBoundary>
                </Suspense>
              </ErrorBoundary>
            </section>
          </main>

          <BillingModal
            isOpen={showBillingModal}
            onClose={() => setShowBillingModal(false)}
            onKeySaved={() => {
              setProfile((prev) => (prev ? { ...prev, has_byok_key: true } : prev));
            }}
          />

          {showPricing && (
            <div className="pricing-standalone">
              <Suspense
                fallback={
                  <div className="workspace-page-loader">
                    <div className="premium-loader">
                      <span className="premium-loader-dot" />
                      <span className="premium-loader-dot" />
                      <span className="premium-loader-dot" />
                    </div>
                    <span>Loading...</span>
                  </div>
                }
              >
                <Pricing />
              </Suspense>
            </div>
          )}
        </div>
      </ErrorBoundary>
    </QueryClientProvider>
  );
};

export default App;
