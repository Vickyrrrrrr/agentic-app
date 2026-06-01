import React, { useEffect, useState } from 'react';
import { Download, Fingerprint, Info, KeyRound, ShieldCheck, User } from 'lucide-react';
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

export const WorkspaceSettings: React.FC<WorkspaceSettingsProps> = ({
  profile,
  sessionEmail,
  onOpenByok,
}) => {
  const [localByokConfigured, setLocalByokConfigured] = useState(false);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    setLocalByokConfigured(Boolean(localStorage.getItem('agentic_byok_key')));
    const handleStorage = () => setLocalByokConfigured(Boolean(localStorage.getItem('agentic_byok_key')));
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  const resolvedEmail = profile?.auth_enabled
    ? profile?.email || sessionEmail || 'Workspace account'
    : 'Preview workspace';
  const resolvedPlan = profile?.auth_enabled ? profile?.plan || 'free' : 'preview';
  const byokReady = Boolean(profile?.has_byok_key) || localByokConfigured;
  const successfulBuilds = profile?.workspace_successful_builds ?? profile?.successful_builds ?? 0;
  const totalBuilds = profile?.total_builds ?? 0;
  const runningBuilds = profile?.running_builds ?? 0;
  const failedBuilds = profile?.failed_builds ?? 0;
  const activeDesigns = profile?.active_designs ?? 0;

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
      link.download = match?.[1] || 'agentic-build-history.json';
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
          <span className="app-hero-kicker">WORKSPACE SETTINGS</span>
          <h2 className="app-hero-title">Manage account access, model connections, and build preferences.</h2>
          <p className="app-hero-subtitle">
            Connect your preferred model provider, review workspace activity, and keep generated chip
            projects ready for download.
          </p>
        </div>
        <div className="app-hero-meta">
          <span className="app-hero-pill">
            <Fingerprint size={15} />
            {resolvedPlan}
          </span>
          <span className={`app-hero-pill ${byokReady ? 'is-success' : 'is-warn'}`}>
            <ShieldCheck size={15} />
            {byokReady ? 'Model key ready' : 'Model key needed'}
          </span>
        </div>
      </section>

      <div className="ws-grid">
        <div className="ws-card">
          <div className="ws-card-header">
            <User size={16} className="ws-card-icon" />
            <span className="ws-card-label">Account</span>
          </div>
          <h3 className="ws-card-title">{resolvedEmail}</h3>
          <p className="ws-card-desc">
            {profile?.auth_enabled
              ? 'Signed in and ready for synced builds.'
              : 'Preview mode for local workspace testing.'}
          </p>
          <div className="ws-plan-row">
            <span className="workspace-plan-badge">{resolvedPlan}</span>
            <span className="ws-builds-count">
              {successfulBuilds} successful - {totalBuilds} total - {runningBuilds} running
            </span>
          </div>
          <p className="ws-note">
            Build history and artifacts stay attached to your workspace so you can return to past chip runs.
          </p>
        </div>

        <div className="ws-card">
          <div className="ws-card-header">
            <KeyRound size={16} className="ws-card-icon" />
            <span className="ws-card-label">Model Keys</span>
          </div>
          <h3 className="ws-card-title">Model Connections</h3>
          <p className="ws-card-desc">
            Connect a managed AgentIC model or bring an OpenAI-compatible provider key for the agent flow.
          </p>
          <div className="ws-key-status">
            <strong className={byokReady ? 'ws-key-ok' : 'ws-key-missing'}>
              {byokReady ? 'Keys configured' : 'No keys configured'}
            </strong>
            <span className="ws-key-hint">
              Your selected model is used by planning, RTL, repair, verification, and reporting agents.
            </span>
          </div>
          <button className="ws-btn-primary" onClick={onOpenByok}>
            Manage Model Access
          </button>
        </div>

        <div className="ws-card">
          <div className="ws-card-header">
            <Download size={16} className="ws-card-icon" />
            <span className="ws-card-label">Workspace Data</span>
          </div>
          <h3 className="ws-card-title">Activity and exports</h3>
          <p className="ws-card-desc">
            Review current workspace activity and export build history whenever you need a backup.
          </p>
          <div className="ws-health-grid">
            <span className="ws-health-pill is-good">{runningBuilds} running</span>
            <span className={failedBuilds ? 'ws-health-pill is-warn' : 'ws-health-pill is-good'}>
              {failedBuilds} failed
            </span>
            <span className="ws-health-pill is-good">{activeDesigns} designs</span>
          </div>
          <button className="ws-btn-secondary" onClick={downloadBackup} disabled={exporting}>
            <Download size={15} />
            {exporting ? 'Preparing export...' : 'Export Build History'}
          </button>
        </div>
      </div>

      <div className="ws-card ws-notes-card">
        <div className="ws-card-header">
          <Info size={16} className="ws-card-icon" />
          <span className="ws-card-label">Notes</span>
        </div>
        <ul className="ws-notes-list">
          <li>Long-running builds continue even if the browser reconnects.</li>
          <li>The agent iterates autonomously — it explores, fails, reads PDK files, fixes, and retries without manual approval gates.</li>
          <li>Waveform previews and downloadable artifacts are available from build outputs.</li>
          <li>Artifacts and reports are generated per design and remain downloadable from build outputs.</li>
        </ul>
      </div>
    </div>
  );
};
