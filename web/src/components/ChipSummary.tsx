import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { api, API_BASE } from '../api';

interface Props {
    designName: string;
    result: any;
    jobStatus: string;
    events: any[];
    jobId?: string;
    onReset: () => void;
}

function StatCell({ label, value, warn }: { label: string; value: any; warn?: boolean }) {
    return (
        <div className="cs-stat">
            <span className={`cs-stat-value${warn ? ' cs-stat-value--warn' : ''}`}>
                {value ?? '—'}
            </span>
            <span className="cs-stat-label">{label}</span>
        </div>
    );
}

function CheckRow({ label, passed, detail }: { label: string; passed: boolean | null; detail?: string }) {
    const status = passed === true ? 'pass' : passed === false ? 'fail' : 'skip';
    return (
        <div className="cs-check-row">
            <span className={`cs-check-icon cs-check-icon--${status}`}>
                {status === 'pass' ? '✓' : status === 'fail' ? '✕' : '–'}
            </span>
            <div className="cs-check-body">
                <span className="cs-check-label">{label}</span>
                {detail && <span className="cs-check-detail">{detail}</span>}
            </div>
        </div>
    );
}

export const ChipSummary: React.FC<Props> = ({ designName, result, jobStatus, events, jobId, onReset }) => {
    const [artifacts, setArtifacts] = useState<Array<{name: string; path: string; size: number; type: string}>>([]);

    useEffect(() => {
        if (designName) {
            api.get('/build/artifacts/' + designName)
                .then(res => {
                    setArtifacts(res.data?.artifacts || []);
                })
                .catch(() => {});
        }
    }, [designName]);

    const success = jobStatus === 'done';
    const metrics = result?.metrics || {};
    const convergence = result?.convergence_history || [];
    const spec = result?.spec || '';
    const rtlSnippet = result?.rtl_snippet || '';
    const strategy = result?.strategy || '';
    const buildTimeSec = result?.build_time_s || 0;
    const buildTimeMin = Math.round(buildTimeSec / 60);
    const coverage = result?.coverage || {};
    const formalResult = result?.formal_result || '';
    const signoffResult = result?.signoff_result || '';

    const selfHeal = result?.self_heal || {
        stage_exception_count: events.filter(e => /stage .* exception/i.test(e?.message || '')).length,
        formal_regen_count: events.filter(e => /regenerating sva/i.test(e?.message || '')).length,
        coverage_best_restore_count: events.filter(e => /restoring best testbench/i.test(e?.message || '')).length,
        coverage_regression_reject_count: events.filter(e => /regressed coverage/i.test(e?.message || '')).length,
        deterministic_tb_fallback_count: events.filter(e => /deterministic tb fallback/i.test(e?.message || '')).length,
    };

    const checkpointCount = events.filter(e => e.type === 'transition' || e.type === 'checkpoint').length;
    const syntaxPassed = events.some(e => /syntax.*pass|lint.*clean/i.test(e?.message || ''));
    const simPassed = events.some(e => /test passed|simulation.*pass/i.test(e?.message || ''));
    const formalPassed = formalResult ? /pass|success/i.test(formalResult) : events.some(e => /formal.*pass/i.test(e?.message || ''));
    const coveragePassed = events.some(e => /coverage passed/i.test(e?.message || ''));
    const signoffPassed = signoffResult ? /pass|success/i.test(signoffResult) : null;
    const lineCov = coverage?.line || coverage?.line_pct;
    const branchCov = coverage?.branch || coverage?.branch_pct;

    const totalHealActions =
        (selfHeal.stage_exception_count || 0) +
        (selfHeal.formal_regen_count || 0) +
        (selfHeal.coverage_best_restore_count || 0) +
        (selfHeal.coverage_regression_reject_count || 0) +
        (selfHeal.deterministic_tb_fallback_count || 0);

    const hasMetrics = metrics.wns !== undefined || metrics.area || metrics.power || metrics.gate_count;
    const failureExplanation = result?.failure_explanation;
    const failureSuggestion = result?.failure_suggestion;

    return (
        <div className="cs-root">

            {/* ── Banner ─────────────────────────────── */}
            <motion.div
                className={`cs-banner ${success ? 'cs-banner--success' : 'cs-banner--fail'}`}
                initial={{ opacity: 0, y: -8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.35 }}
            >
                <div className="cs-banner-dot" />
                <div className="cs-banner-body">
                    <h1 className="cs-banner-title">
                        {success ? `${designName} is ready` : 'Build stopped'}
                    </h1>
                    <p className="cs-banner-sub">
                        {success
                            ? [
                                buildTimeMin > 0 ? `${buildTimeMin} min` : null,
                                checkpointCount > 0 ? `${checkpointCount} checkpoints passed` : null,
                                strategy || null,
                              ].filter(Boolean).join(' · ')
                            : `${designName} · Review the log below for details`
                        }
                    </p>
                </div>
            </motion.div>

            {/* ── Silicon Metrics ─────────────────────── */}
            {success && hasMetrics && (
                <div className="cs-section">
                    <h2 className="cs-section-title">Silicon Metrics</h2>
                    <div className="cs-stats-row">
                        <StatCell
                            label="Worst negative slack"
                            value={metrics.wns !== undefined ? `${metrics.wns} ns` : null}
                            warn={metrics.wns !== undefined && metrics.wns < 0}
                        />
                        <div className="cs-stat-divider" />
                        <StatCell label="Die area" value={metrics.area} />
                        <div className="cs-stat-divider" />
                        <StatCell label="Total power" value={metrics.power} />
                        <div className="cs-stat-divider" />
                        <StatCell label="Gate count" value={metrics.gate_count} />
                    </div>
                </div>
            )}

            {/* ── Verification ────────────────────────── */}
            <div className="cs-section">
                <h2 className="cs-section-title">Verification</h2>
                <div className="cs-checks">
                    <CheckRow
                        label="RTL syntax & lint"
                        passed={syntaxPassed || null}
                        detail={syntaxPassed ? 'Clean — Verilator lint-only' : undefined}
                    />
                    <CheckRow
                        label="Functional simulation"
                        passed={simPassed || null}
                        detail={simPassed ? 'TEST PASSED' : undefined}
                    />
                    <CheckRow
                        label="Formal property verification"
                        passed={formalPassed}
                        detail={formalResult ? formalResult.substring(0, 60) : undefined}
                    />
                    <CheckRow
                        label="Coverage closure"
                        passed={coveragePassed || null}
                        detail={lineCov ? `Line ${lineCov}%${branchCov ? ` · Branch ${branchCov}%` : ''}` : undefined}
                    />
                    <CheckRow
                        label="DRC / LVS signoff"
                        passed={signoffPassed}
                        detail={signoffResult ? signoffResult.substring(0, 60) : (success ? 'Physical checks completed' : undefined)}
                    />
                </div>
            </div>

            {/* ── Generated Artifacts ─────────────────── */}
            {artifacts.length > 0 && (() => {
                const groups: Record<string, typeof artifacts> = {};
                artifacts.forEach(a => {
                    const key = a.type || 'other';
                    (groups[key] = groups[key] || []).push(a);
                });
                return (
                    <div className="cs-section">
                        <h2 className="cs-section-title">
                            Build Artifacts & Reports ({artifacts.length} files generated)
                        </h2>
                        <div className="hitl-fail-artifacts-grouped" style={{ marginTop: '0.75rem' }}>
                            {Object.entries(groups).map(([type, files]) => (
                                <div key={type} className="hitl-fail-artifact-group">
                                    <span className="hitl-fail-artifact-group-label">{type}</span>
                                    {files.map((a, i) => (
                                        <div key={i} className="hitl-fail-artifact-row">
                                            <span className="hitl-fail-artifact-name">{a.name}</span>
                                            <span className="hitl-fail-artifact-size">
                                                {a.size > 1024 ? `${(a.size / 1024).toFixed(1)} KB` : `${a.size} B`}
                                            </span>
                                            <a
                                                href={`${API_BASE}/build/artifacts/${designName}/${encodeURIComponent(a.name)}`}
                                                className="hitl-fail-artifact-dl"
                                                download
                                            >
                                                ↓ dl
                                            </a>
                                        </div>
                                    ))}
                                </div>
                            ))}
                        </div>
                    </div>
                );
            })()}

            {/* ── Self-Healing ────────────────────────── */}
            {totalHealActions > 0 && (
                <div className="cs-section">
                    <h2 className="cs-section-title">Self-Healing Activity</h2>
                    <div className="cs-heal-list">
                        {selfHeal.stage_exception_count > 0 && (
                            <div className="cs-heal-item">
                                Stage exception guards
                                <span className="cs-heal-count">{selfHeal.stage_exception_count}×</span>
                            </div>
                        )}
                        {selfHeal.formal_regen_count > 0 && (
                            <div className="cs-heal-item">
                                Formal SVA regeneration
                                <span className="cs-heal-count">{selfHeal.formal_regen_count}×</span>
                            </div>
                        )}
                        {selfHeal.coverage_regression_reject_count > 0 && (
                            <div className="cs-heal-item">
                                Testbench regression rejections
                                <span className="cs-heal-count">{selfHeal.coverage_regression_reject_count}×</span>
                            </div>
                        )}
                        {selfHeal.coverage_best_restore_count > 0 && (
                            <div className="cs-heal-item">
                                Best testbench restores
                                <span className="cs-heal-count">{selfHeal.coverage_best_restore_count}×</span>
                            </div>
                        )}
                        {selfHeal.deterministic_tb_fallback_count > 0 && (
                            <div className="cs-heal-item">
                                Deterministic testbench fallbacks
                                <span className="cs-heal-count">{selfHeal.deterministic_tb_fallback_count}×</span>
                            </div>
                        )}
                    </div>
                </div>
            )}

            {/* ── Architecture Spec ───────────────────── */}
            {spec && (
                <div className="cs-section">
                    <h2 className="cs-section-title">Architecture Specification</h2>
                    <div className="cs-code-block">
                        <pre className="cs-pre cs-pre--prose">
                            {spec.substring(0, 1200)}{spec.length > 1200 ? '\n…' : ''}
                        </pre>
                    </div>
                </div>
            )}

            {/* ── RTL Preview ─────────────────────────── */}
            {rtlSnippet && (
                <div className="cs-section">
                    <h2 className="cs-section-title">RTL Preview</h2>
                    <div className="cs-code-block cs-code-block--dark">
                        <pre className="cs-pre cs-pre--code">
                            {rtlSnippet.substring(0, 1200)}{rtlSnippet.length > 1200 ? '\n// …' : ''}
                        </pre>
                    </div>
                </div>
            )}

            {/* ── Convergence History ─────────────────── */}
            {Array.isArray(convergence) && convergence.length > 0 && (
                <div className="cs-section">
                    <h2 className="cs-section-title">Convergence History</h2>
                    <div className="cs-table-wrap">
                        <table className="cs-table">
                            <thead>
                                <tr>
                                    <th>Iter</th><th>WNS (ns)</th><th>TNS (ns)</th>
                                    <th>Congestion %</th><th>Area (µm²)</th><th>Power (W)</th>
                                </tr>
                            </thead>
                            <tbody>
                                {convergence.slice(-5).map((s: any, i: number) => (
                                    <tr key={i}>
                                        <td>{s.iteration}</td>
                                        <td data-good={s.wns >= 0 ? 'true' : 'false'}>{s.wns?.toFixed(3)}</td>
                                        <td>{s.tns?.toFixed(3)}</td>
                                        <td>{s.congestion?.toFixed(2)}</td>
                                        <td>{s.area_um2?.toFixed(0)}</td>
                                        <td>{s.power_w?.toExponential(2)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* ── Failure Detail ──────────────────────── */}
            {!success && (failureExplanation || result?.error) && (
                <div className="cs-section">
                    <h2 className="cs-section-title">What went wrong</h2>
                    {failureExplanation && (
                        <p className="cs-fail-explanation">{failureExplanation}</p>
                    )}
                    {failureSuggestion && (
                        <p className="cs-fail-suggestion">
                            <span className="cs-fail-suggestion-label">Try:</span>
                            {failureSuggestion}
                        </p>
                    )}
                    {result?.error && !failureExplanation && (
                        <div className="cs-code-block">
                            <pre className="cs-pre cs-pre--error">
                                {String(result.error).substring(0, 500)}
                            </pre>
                        </div>
                    )}
                </div>
            )}

            {/* ── Actions ─────────────────────────────── */}
            <div className="cs-actions">
                {/* Full build report downloads */}
                {jobId && (
                    <div className="cs-report-downloads">
                        <span className="cs-report-label">Download Build Report:</span>
                        <a
                            className="cs-report-btn"
                            href={`${API_BASE}/report/${jobId}/full.pdf`}
                            download
                            title="Download full build report as PDF"
                        >
                            ↓ PDF Report
                        </a>
                        <a
                            className="cs-report-btn"
                            href={`${API_BASE}/report/${jobId}/full.docx`}
                            download
                            title="Download full build report as DOCX"
                        >
                            ↓ DOCX Report
                        </a>
                    </div>
                )}
                <motion.button
                    className="cs-reset-btn"
                    onClick={onReset}
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                >
                    Build another chip
                </motion.button>
            </div>
        </div>
    );
};
