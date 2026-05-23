import React, { useEffect, useState } from 'react';
import { Activity, Download, Fingerprint, Info, KeyRound, ShieldCheck, User } from 'lucide-react';
import { api } from '../api';

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

interface WorkspaceSettingsProps {
  profile: ProfileSummary | null;
  sessionEmail: string;
  onOpenByok: () => void;
}

type OpsSummary = {
  platform?: {
    status?: string;
    database?: { ok?: boolean };
    redis?: { ok?: boolean };
    object_storage?: { ok?: boolean; enabled?: boolean };
  };
  usage?: {
    total_builds?: number;
    running_builds?: number;
    successful_builds?: number;
    failed_builds?: number;
    active_designs?: number;
  };
};

export const WorkspaceSettings: React.FC<WorkspaceSettingsProps> = ({
  profile,
  sessionEmail,
  onOpenByok,
}) => {
  const [localByokConfigured, setLocalByokConfigured] = useState(false);
  const [opsSummary, setOpsSummary] = useState<OpsSummary | null>(null);
  const [opsLoading, setOpsLoading] = useState(true);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    setLocalByokConfigured(Boolean(localStorage.getItem('agentic_byok_key')));
    const handleStorage = () => setLocalByokConfigured(Boolean(localStorage.getItem('agentic_byok_key')));
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setOpsLoading(true);
    api.get('/ops/summary')
      .then((res) => {
        if (!cancelled) setOpsSummary(res.data || null);
      })
      .catch(() => {
        if (!cancelled) setOpsSummary(null);
      })
      .finally(() => {
        if (!cancelled) setOpsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const resolvedEmail = profile?.email || sessionEmail || 'Anonymous workspace';
  const resolvedPlan = profile?.auth_enabled ? profile?.plan || 'free' : 'local';
  const byokReady = Boolean(profile?.has_byok_key) || localByokConfigured;
  const successfulBuilds = profile?.workspace_successful_builds ?? profile?.successful_builds ?? 0;
  const totalBuilds = profile?.total_builds ?? 0;
  const runningBuilds = profile?.running_builds ?? 0;
  const platform = opsSummary?.platform;
  const usage = opsSummary?.usage;

  const downloadBackup = async () => {
    setExporting(true);
    try {
      const res = await api.get('/ops/jobs/export', { responseType: 'blob' });
      const blob = new Blob([res.data], { type: 'application/json' });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      const disposition = res.headers['content-disposition'] as string | undefined;
      const match = disposition?.match(/filename="([^"]+)"/);
      link.href = url;
      link.download = match?.[1] || 'agentic-jobs-backup.json';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="ws-page">
      <section className="app-hero-card ws-hero-card">
        <div className="app-hero-copy">
          <span className="app-hero-kicker">WORKSPACE CONTROL</span>
          <h2 className="app-hero-title">Manage identity, BYOK routing, and operator guardrails.</h2>
          <p className="app-hero-subtitle">
            This workspace is where you confirm who is running builds, how model access is routed,
            and what assumptions AgentIC should make before it touches compute or artifacts.
          </p>
        </div>
        <div className="app-hero-meta">
          <span className="app-hero-pill">
            <Fingerprint size={15} />
            {resolvedPlan}
          </span>
          <span className={`app-hero-pill ${byokReady ? 'is-success' : 'is-warn'}`}>
            <ShieldCheck size={15} />
            {byokReady ? 'BYOK ready' : 'BYOK missing'}
          </span>
        </div>
      </section>

      <div className="ws-grid">
        {/* Account */}
        <div className="ws-card">
          <div className="ws-card-header">
            <User size={16} className="ws-card-icon" />
            <span className="ws-card-label">Account Context</span>
          </div>
          <h3 className="ws-card-title">{resolvedEmail}</h3>
          <p className="ws-card-desc">
            Auth mode: {profile?.auth_enabled ? 'Supabase' : 'Local / Auth disabled'}
          </p>
          <div className="ws-plan-row">
            <span className="workspace-plan-badge">{resolvedPlan}</span>
            <span className="ws-builds-count">
              {successfulBuilds} successful · {totalBuilds} total · {runningBuilds} running
            </span>
          </div>
          <p className="ws-note">
            Build history is persisted server-side so workspace usage survives refreshes and restarts.
          </p>
        </div>

        {/* Model Keys */}
        <div className="ws-card">
          <div className="ws-card-header">
            <KeyRound size={16} className="ws-card-icon" />
            <span className="ws-card-label">Model Keys</span>
          </div>
          <h3 className="ws-card-title">BYOK Configuration</h3>
          <p className="ws-card-desc">
            Configure model keys for reasoning, coding, and iterative agents.
          </p>
          <div className="ws-key-status">
            <strong className={byokReady ? 'ws-key-ok' : 'ws-key-missing'}>
              {byokReady ? 'Keys configured' : 'No keys configured'}
            </strong>
            <span className="ws-key-hint">
              Builds and Lab AI actions require a valid provider key.
            </span>
          </div>
          <button className="ws-btn-primary" onClick={onOpenByok}>
            Open BYOK Manager
          </button>
        </div>

        <div className="ws-card">
          <div className="ws-card-header">
            <Activity size={16} className="ws-card-icon" />
            <span className="ws-card-label">Operator Health</span>
          </div>
          <h3 className="ws-card-title">Runtime + backup controls</h3>
          <p className="ws-card-desc">
            Check whether the core services are healthy and export persisted job history for backup.
          </p>
          {opsLoading ? (
            <p className="ws-note">Loading platform summary...</p>
          ) : (
            <>
              <div className="ws-health-grid">
                <span className={`ws-health-pill ${platform?.status === 'ok' ? 'is-good' : 'is-warn'}`}>
                  API {platform?.status === 'ok' ? 'ready' : 'degraded'}
                </span>
                <span className={`ws-health-pill ${platform?.database?.ok ? 'is-good' : 'is-warn'}`}>
                  DB {platform?.database?.ok ? 'ok' : 'issue'}
                </span>
                <span className={`ws-health-pill ${platform?.redis?.ok ? 'is-good' : 'is-warn'}`}>
                  Redis {platform?.redis?.ok ? 'ok' : 'issue'}
                </span>
                <span className={`ws-health-pill ${platform?.object_storage?.ok || !platform?.object_storage?.enabled ? 'is-good' : 'is-warn'}`}>
                  Storage {platform?.object_storage?.enabled ? (platform?.object_storage?.ok ? 'ok' : 'issue') : 'off'}
                </span>
              </div>
              <div className="ws-ops-stats">
                <span>{usage?.running_builds ?? runningBuilds} running</span>
                <span>{usage?.failed_builds ?? 0} failed</span>
                <span>{usage?.active_designs ?? profile?.active_designs ?? 0} designs</span>
              </div>
            </>
          )}
          <button className="ws-btn-secondary" onClick={downloadBackup} disabled={exporting}>
            <Download size={15} />
            {exporting ? 'Preparing backup...' : 'Export Job Backup'}
          </button>
        </div>
      </div>

      {/* Operational Notes */}
      <div className="ws-card ws-notes-card">
        <div className="ws-card-header">
          <Info size={16} className="ws-card-icon" />
          <span className="ws-card-label">Operational Notes</span>
        </div>
        <ul className="ws-notes-list">
          <li>Long-running builds stream progress over SSE and recover from reconnects.</li>
          <li>HITL mode requires stage approvals before pipeline progression.</li>
          <li>Manual EDA Lab waveform behavior differs between cloud and local desktop environments.</li>
          <li>Artifacts and reports are generated per design and remain downloadable from build outputs.</li>
        </ul>
      </div>
    </div>
  );
};
