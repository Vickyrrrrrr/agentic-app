import React from 'react';
import { Activity, ArrowRight, CheckCircle2, Clock, Layers3, XCircle } from 'lucide-react';

type JobSummary = {
  job_id: string;
  design_name: string;
  status: string;
  current_state: string;
  created_at: number;
  event_count: number;
};

interface BuildHistoryProps {
  jobs: JobSummary[];
  selectedDesign: string;
  onSelectDesign: (designName: string) => void;
  onOpenPage: (page: string) => void;
}

function statusColor(status: string): string {
  if (status === 'done') return 'var(--success)';
  if (status === 'failed') return 'var(--fail)';
  if (status === 'running' || status === 'queued' || status === 'cancelling') return 'var(--accent)';
  return 'var(--text-dim)';
}

function formatTs(ts: number): string {
  if (!ts) return 'Unknown';
  return new Date(ts * 1000).toLocaleString();
}

export const BuildHistory: React.FC<BuildHistoryProps> = ({
  jobs,
  selectedDesign,
  onSelectDesign,
  onOpenPage,
}) => {
  const sortedJobs = [...jobs].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  const filteredJobs = selectedDesign
    ? sortedJobs.filter((job) => job.design_name === selectedDesign)
    : sortedJobs;

  const doneCount = sortedJobs.filter((job) => job.status === 'done').length;
  const runningCount = sortedJobs.filter(
    (job) => job.status === 'running' || job.status === 'queued' || job.status === 'cancelling'
  ).length;
  const failedCount = sortedJobs.filter((job) => job.status === 'failed').length;

  return (
    <div className="bh-page">
      <section className="bh-hero-card">
        <div className="bh-hero-copy">
          <span className="bh-hero-kicker">OPERATIONS LOG</span>
          <h2 className="bh-hero-title">Track active runs, outcomes, and handoff readiness.</h2>
          <p className="bh-hero-subtitle">
            Every build is retained as an execution record so you can audit stage flow, revisit design context,
            and continue from the right workspace surface.
          </p>
        </div>
        <div className="bh-hero-actions">
          <div className="bh-hero-chip">
            <Layers3 size={15} />
            {selectedDesign ? selectedDesign : 'All designs'}
          </div>
          <button className="ws-btn-primary bh-primary-cta" onClick={() => onOpenPage('Design Studio')}>
            Start New Build
            <ArrowRight size={15} />
          </button>
        </div>
      </section>

      <div className="bh-stats-grid">
        <div className="bh-stat-card">
          <Activity size={16} className="bh-stat-icon" />
          <div className="bh-stat-body">
            <span className="bh-stat-label">Total Jobs</span>
            <span className="bh-stat-value">{sortedJobs.length}</span>
          </div>
        </div>
        <div className="bh-stat-card">
          <Clock size={16} className="bh-stat-icon bh-stat-icon--running" />
          <div className="bh-stat-body">
            <span className="bh-stat-label">Running</span>
            <span className="bh-stat-value bh-val--accent">{runningCount}</span>
          </div>
        </div>
        <div className="bh-stat-card">
          <CheckCircle2 size={16} className="bh-stat-icon bh-stat-icon--success" />
          <div className="bh-stat-body">
            <span className="bh-stat-label">Succeeded</span>
            <span className="bh-stat-value bh-val--success">{doneCount}</span>
          </div>
        </div>
        <div className="bh-stat-card">
          <XCircle size={16} className="bh-stat-icon bh-stat-icon--fail" />
          <div className="bh-stat-body">
            <span className="bh-stat-label">Failed</span>
            <span className="bh-stat-value bh-val--fail">{failedCount}</span>
          </div>
        </div>
      </div>

      <div className="bh-card">
        <div className="bh-card-header">
          <h3 className="bh-card-title">
            Build Timeline
            {selectedDesign ? <span className="bh-design-tag">{selectedDesign}</span> : ''}
          </h3>
          <span className="bh-table-caption">{filteredJobs.length} visible records</span>
        </div>

        {filteredJobs.length === 0 ? (
          <div className="bh-empty">
            No jobs are available for this design context yet. Launch a build to create the first durable record.
          </div>
        ) : (
          <div className="bh-table-wrap">
            <table className="bh-table">
              <thead>
                <tr>
                  <th>Job ID</th>
                  <th>Design</th>
                  <th>Status</th>
                  <th>Stage</th>
                  <th>Events</th>
                  <th>Created</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredJobs.map((job) => (
                  <tr key={job.job_id}>
                    <td className="bh-job-id">{String(job.job_id || '').slice(0, 8)}...</td>
                    <td>{job.design_name || '-'}</td>
                    <td>
                      <span className="bh-status" style={{ color: statusColor(job.status), borderColor: statusColor(job.status) }}>
                        {job.status}
                      </span>
                    </td>
                    <td>{job.current_state || '-'}</td>
                    <td>{job.event_count || 0}</td>
                    <td>{formatTs(job.created_at)}</td>
                    <td>
                      <button
                        className="bh-action-btn"
                        onClick={() => {
                          onSelectDesign(job.design_name);
                          onOpenPage('Dashboard');
                        }}
                      >
                        Open Design
                        <ArrowRight size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};
