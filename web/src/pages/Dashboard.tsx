import { useState, useEffect } from 'react';
import { api } from '../api';
import { Gauge, Zap, Maximize2, Hash, Cpu, Users, RefreshCw, CheckCircle2, XCircle, Layers3, ShieldCheck } from 'lucide-react';

interface DashboardProps {
    selectedDesign: string;
}

export const Dashboard = ({ selectedDesign }: DashboardProps) => {
    const [metrics, setMetrics] = useState<any>({
        wns: 'N/A', power: 'N/A', area: 'N/A', gate_count: 'N/A'
    });
    const [signoffData, setSignoffData] = useState<{ report: string, pass: boolean | null }>({ report: 'Fetching sign-off analysis...', pass: null });
    const [loading, setLoading] = useState(false);
    const [recentJobs, setRecentJobs] = useState<any[]>([]);

    useEffect(() => {
        if (!selectedDesign) return;
        setLoading(true);

        api.get(`/metrics/${selectedDesign}`)
            .then(res => {
                if (res.data.metrics) setMetrics(res.data.metrics);
            })
            .catch(() => {
                setMetrics({ wns: 'N/A', power: 'N/A', area: 'N/A', gate_count: 'N/A' });
            });

        api.get(`/signoff/${selectedDesign}`)
            .then(res => {
                setSignoffData({ report: res.data.report, pass: res.data.success });
            })
            .catch(() => {
                setSignoffData({ report: 'Failed to retrieve Signoff Report. Has the device been fully hardened via OpenLane yet?', pass: false });
            })
            .finally(() => setLoading(false));

        api.get(`/jobs`)
            .then(res => {
                const jobs = (res.data?.jobs || [])
                    .filter((j: any) => j.design_name === selectedDesign)
                    .slice(0, 5);
                setRecentJobs(jobs);
            })
            .catch(() => setRecentJobs([]));

    }, [selectedDesign]);

    const statusColor = (status: string) => {
        if (status === 'done') return 'var(--success)';
        if (status === 'failed') return 'var(--fail)';
        if (status === 'running') return 'var(--accent)';
        return 'var(--text-dim)';
    };

    const METRICS_CONFIG = [
        { key: 'wns', label: 'Worst Negative Slack', tag: 'Timing', icon: <Gauge size={16} /> },
        { key: 'power', label: 'Total Power', tag: 'Energy', icon: <Zap size={16} /> },
        { key: 'area', label: 'Die Area', tag: 'Footprint', icon: <Maximize2 size={16} /> },
        { key: 'gate_count', label: 'Gate Count', tag: 'Logic', icon: <Hash size={16} /> },
    ];

    return (
        <div className="dash-page">
            <section className="app-hero-card dash-hero-card">
                <div className="app-hero-copy">
                    <span className="app-hero-kicker">DESIGN INSIGHTS</span>
                    <h2 className="app-hero-title">{selectedDesign || 'No design selected yet'}</h2>
                    <p className="app-hero-subtitle">
                        Review timing, power, signoff confidence, and the latest execution history in one place.
                        This is the operator-facing summary of how ready the current design is for the next step.
                    </p>
                </div>
                <div className="app-hero-meta">
                    <span className="app-hero-pill">
                        <Layers3 size={15} />
                        {recentJobs.length} recent runs
                    </span>
                    <span className={`app-hero-pill ${signoffData.pass === true ? 'is-success' : signoffData.pass === false ? 'is-warn' : ''}`}>
                        <ShieldCheck size={15} />
                        {signoffData.pass === true ? 'Signoff passing' : signoffData.pass === false ? 'Signoff attention needed' : 'Signoff pending'}
                    </span>
                </div>
            </section>

            {!selectedDesign ? (
                <div className="app-empty-card">
                    <h3 className="app-empty-title">No design is in focus yet.</h3>
                    <p className="app-empty-copy">
                        Start a build or choose a design from Jobs &amp; History to unlock metrics, signoff analysis,
                        and recent execution detail for a specific project.
                    </p>
                </div>
            ) : (
                <>
                    {loading ? (
                        <div className="dash-loader">
                            <div className="premium-loader">
                                <span className="premium-loader-dot" />
                                <span className="premium-loader-dot" />
                                <span className="premium-loader-dot" />
                            </div>
                            Loading metrics...
                        </div>
                    ) : (
                        <div className="dash-metrics-grid">
                            {METRICS_CONFIG.map((m) => (
                                <div className="dash-metric-card" key={m.key}>
                                    <div className="dash-metric-icon">{m.icon}</div>
                                    <div className="dash-metric-body">
                                        <span className="dash-metric-label">{m.label}</span>
                                        <span className="dash-metric-value">{metrics[m.key]}</span>
                                        <span className="dash-metric-tag">{m.tag}</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}

                    <div className="dash-card">
                        <h3 className="dash-card-title">Agent Architecture</h3>
                        <div className="dash-insight-grid">
                            {[
                                { icon: <Cpu size={16} />, title: 'Spec Decomposition', value: 'SID/JSON Contract', detail: 'ArchitectModule — validated ports, FSMs, sub-modules' },
                                { icon: <Users size={16} />, title: 'Collaborative RTL', value: 'Designer + Reviewer', detail: '2-agent Crew with syntax_check and read_file tools' },
                                { icon: <RefreshCw size={16} />, title: 'Self-Healing', value: 'Convergence-Aware', detail: 'SelfReflectPipeline with fingerprinting + stagnation detection' },
                            ].map((item, i) => (
                                <div key={i} className="dash-insight-item">
                                    <div className="dash-insight-icon">{item.icon}</div>
                                    <div className="dash-insight-content">
                                        <span className="dash-insight-title">{item.title}</span>
                                        <span className="dash-insight-value">{item.value}</span>
                                        <span className="dash-insight-detail">{item.detail}</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="dash-card">
                        <h3 className="dash-card-title">
                            Signoff Report
                            {signoffData.pass === true && (
                                <span className="dash-signoff-pass"><CheckCircle2 size={14} /> Passed</span>
                            )}
                            {signoffData.pass === false && (
                                <span className="dash-signoff-fail"><XCircle size={14} /> Failed</span>
                            )}
                        </h3>
                        <pre className="dash-report-pre">{signoffData.report}</pre>
                    </div>

                    {recentJobs.length > 0 && (
                        <div className="dash-card">
                            <h3 className="dash-card-title">Recent Builds</h3>
                            <div className="dash-table-wrap">
                                <table className="dash-table">
                                    <thead>
                                        <tr>
                                            <th>Job ID</th>
                                            <th>Status</th>
                                            <th>Stage</th>
                                            <th>Events</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {recentJobs.map((job: any) => (
                                            <tr key={job.job_id}>
                                                <td className="dash-job-id">{job.job_id.substring(0, 8)}…</td>
                                                <td>
                                                    <span className="dash-status" style={{ color: statusColor(job.status) }}>
                                                        {job.status}
                                                    </span>
                                                </td>
                                                <td>{job.current_state}</td>
                                                <td>{job.event_count}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    )}
                </>
            )}
        </div>
    );
};
