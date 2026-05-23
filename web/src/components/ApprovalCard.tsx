import React, { useState } from 'react';
import { API_BASE } from '../api';

interface StageCompleteData {
    stage_name: string;
    summary: string;
    artifacts: Array<{ name: string; path: string; description: string }>;
    decisions: string[];
    warnings: string[];
    next_stage_name: string;
    next_stage_preview: string;
}

interface Props {
    data: StageCompleteData;
    designName: string;
    jobId: string;
    onApprove: () => void;
    onReject: (feedback: string) => void;
    isSubmitting: boolean;
}

const STAGE_ICONS: Record<string, string> = {
    INIT: '⚙', SPEC: '◈', SPEC_VALIDATE: '⊘', HIERARCHY_EXPAND: '⊞', FEASIBILITY_CHECK: '⚖', VERIFICATION_PLAN: '☑', RTL_GEN: '⌨', RTL_FIX: '◪',
    CDC_ANALYZE: '↔', VERIFICATION: '◉', FORMAL_VERIFY: '◈', COVERAGE_CHECK: '◎',
    REGRESSION: '↺', SDC_GEN: '⧗', SYNTHESIS: '⚡', DFT_SCAN: '🔬', DFT_ATPG: '📝',
    MBIST: '💾', GLS_SIMULATION: '🔬', FLOORPLAN: '▣', HARDENING: '⬡', TIMING_ANALYSIS: '⏱️', CONVERGENCE_REVIEW: '◎',
    ECO_PATCH: '⟴', POWER_ANALYSIS: '⚡', PHYSICAL_VERIFY: '🔍', POST_LAYOUT_SPICE: '⚡', SIGNOFF: '✓', IP_PACKAGE: '📦', SUCCESS: '✦', FAIL: '✗',
};

const ARTIFACT_TYPE_LABELS: Record<string, string> = {
    rtl: 'RTL', waveform: 'Waveform', layout: 'Layout',
    constraints: 'SDC', config: 'Config', script: 'Script',
    formal: 'Formal', log: 'Log', report: 'Report', other: 'File',
};

function fmtStage(name: string): string {
    return name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function guessType(name: string): string {
    const ext = name.split('.').pop()?.toLowerCase() || '';
    const map: Record<string, string> = {
        v: 'rtl', sv: 'rtl', vcd: 'waveform', gds: 'layout', def: 'layout',
        sdc: 'constraints', json: 'config', tcl: 'script', sby: 'formal',
        log: 'log', csv: 'report',
    };
    return map[ext] || 'other';
}

export const ApprovalCard: React.FC<Props> = ({ data, designName, jobId, onApprove, onReject, isSubmitting }) => {
    const [showFeedback, setShowFeedback] = useState(false);
    const [feedback, setFeedback] = useState('');
    const [artifactsExpanded, setArtifactsExpanded] = useState(false);

    const hasWarnings = data.warnings && data.warnings.length > 0;
    const hasErrors = data.decisions?.some((d: string) => /error|fail/i.test(d));
    const artifactCount = data.artifacts?.length || 0;
    const hasNext = data.next_stage_name && data.next_stage_name !== 'DONE';
    const icon = STAGE_ICONS[data.stage_name] || '◆';

    const handleReject = () => {
        onReject(feedback);
        setShowFeedback(false);
        setFeedback('');
    };

    return (
        <div className={`ac-card ${hasErrors ? 'ac-card--error' : hasWarnings ? 'ac-card--warn' : 'ac-card--ok'}`}>

            {/* Header — stage identity */}
            <div className="ac-header">
                <div className="ac-stage-id">
                    <span className="ac-stage-symbol">{icon}</span>
                    <span className="ac-stage-label">{fmtStage(data.stage_name)}</span>
                    {hasErrors && <span className="ac-badge ac-badge--error">Issue detected</span>}
                    {!hasErrors && hasWarnings && <span className="ac-badge ac-badge--warn">Warning</span>}
                </div>
                {artifactCount > 0 && (
                    <button
                        className="ac-artifact-pill ac-artifact-toggle"
                        onClick={() => setArtifactsExpanded(v => !v)}
                    >
                        {artifactCount} artifact{artifactCount !== 1 ? 's' : ''}
                        <span className={`ac-artifact-chevron ${artifactsExpanded ? 'ac-artifact-chevron--open' : ''}`}>›</span>
                    </button>
                )}
            </div>

            {/* Summary — primary content */}
            <p className="ac-summary">
                {data.summary || `${fmtStage(data.stage_name)} completed successfully.`}
            </p>

            {/* Artifact file list */}
            {artifactsExpanded && data.artifacts && data.artifacts.length > 0 && (
                <div className="ac-artifacts">
                    {data.artifacts.map((a, i) => {
                        const aType = guessType(a.name);
                        return (
                            <div key={i} className="ac-artifact-row">
                                <span className={`ac-artifact-type ac-artifact-type--${aType}`}>
                                    {ARTIFACT_TYPE_LABELS[aType] || aType}
                                </span>
                                <span className="ac-artifact-name">{a.name}</span>
                                {a.description && (
                                    <span className="ac-artifact-desc">{a.description}</span>
                                )}
                                <a
                                    className="ac-artifact-dl"
                                    href={`${API_BASE}/build/artifacts/${designName}/${encodeURIComponent(a.name)}`}
                                    download
                                    title={`Download ${a.name}`}
                                >
                                    ↓
                                </a>
                            </div>
                        );
                    })}
                </div>
            )}

            {/* Next stage preview */}
            {hasNext && data.next_stage_preview && (
                <div className="ac-next-hint">
                    <span className="ac-next-arrow">↓</span>
                    <span className="ac-next-text">{data.next_stage_preview}</span>
                </div>
            )}

            {/* Action footer */}
            <div className="ac-footer">
                {/* Stage report downloads */}
                {jobId && (
                    <div className="ac-report-downloads">
                        <a
                            className="ac-report-btn"
                            href={`${API_BASE}/report/${jobId}/stage/${data.stage_name}.pdf`}
                            download
                            title="Download stage report as PDF"
                        >
                            ↓ PDF
                        </a>
                        <a
                            className="ac-report-btn"
                            href={`${API_BASE}/report/${jobId}/stage/${data.stage_name}.docx`}
                            download
                            title="Download stage report as DOCX"
                        >
                            ↓ DOCX
                        </a>
                    </div>
                )}
                {!showFeedback ? (
                    <>
                        <button className="ac-give-feedback" onClick={() => setShowFeedback(true)}>
                            Give feedback
                        </button>
                        <button className="ac-continue-btn" onClick={onApprove} disabled={isSubmitting}>
                            {isSubmitting ? 'Continuing…' : 'Continue'}
                            {!isSubmitting && <span className="ac-chevron">→</span>}
                        </button>
                    </>
                ) : (
                    <div className="ac-feedback-row">
                        <input
                            className="ac-feedback-input"
                            type="text"
                            placeholder="What should the agent do differently?"
                            value={feedback}
                            onChange={e => setFeedback(e.target.value)}
                            autoFocus
                            onKeyDown={e => {
                                if (e.key === 'Enter') handleReject();
                                if (e.key === 'Escape') { setShowFeedback(false); setFeedback(''); }
                            }}
                        />
                        <button className="ac-reject-btn" onClick={handleReject} disabled={isSubmitting}>
                            {isSubmitting ? 'Sending…' : 'Reject & redirect'}
                        </button>
                        <button className="ac-cancel-btn" onClick={() => { setShowFeedback(false); setFeedback(''); }}>
                            Cancel
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
};
