import React, { useEffect, useRef } from 'react';
import { motion } from 'framer-motion';
import { api } from '../api';

const STATES_DISPLAY: Record<string, { label: string; icon: string }> = {
    INIT: { label: 'Initializing Workspace', icon: '01' },
    SPEC: { label: 'Architectural Planning', icon: '02' },
    SPEC_VALIDATE: { label: 'Specification Validation', icon: '03' },
    HIERARCHY_EXPAND: { label: 'Hierarchy Expansion', icon: '04' },
    FEASIBILITY_CHECK: { label: 'Feasibility Check', icon: '05' },
    VERIFICATION_PLAN: { label: 'Verification Planning', icon: '06' },
    RTL_GEN: { label: 'RTL Generation', icon: '07' },
    RTL_FIX: { label: 'RTL Syntax Fixing', icon: '08' },
    CDC_ANALYZE: { label: 'CDC Analysis', icon: '09' },
    VERIFICATION: { label: 'Verification & Testbench', icon: '10' },
    FORMAL_VERIFY: { label: 'Formal Verification', icon: '11' },
    COVERAGE_CHECK: { label: 'Coverage Analysis', icon: '12' },
    REGRESSION: { label: 'Regression Testing', icon: '13' },
    SDC_GEN: { label: 'SDC Generation', icon: '14' },
    SYNTHESIS: { label: 'RTL Synthesis', icon: '15' },
    FLOORPLAN: { label: 'Floorplanning', icon: '16' },
    HARDENING: { label: 'GDSII Hardening', icon: '17' },
    CONVERGENCE_REVIEW: { label: 'Convergence Review', icon: '18' },
    ECO_PATCH: { label: 'ECO Patch', icon: '19' },
    POWER_ANALYSIS: { label: 'Power Analysis', icon: '20' },
    TIMING_ANALYSIS: { label: 'Static Timing Analysis', icon: '21' },
    PHYSICAL_VERIFY: { label: 'Physical Verification', icon: '22' },
    SIGNOFF: { label: 'OSS Signoff Review', icon: '23' },
    IP_PACKAGE: { label: 'IP Packaging', icon: '24' },
    SUCCESS: { label: 'Build Complete', icon: '25' },
};

interface BuildEvent {
    type: string;
    state: string;
    message: string;
    step: number;
    total_steps: number;
    timestamp: number;
}

interface StageSchemaItem {
    state: string;
    label: string;
    icon: string;
}

interface Props {
    designName: string;
    jobId: string;
    events: BuildEvent[];
    jobStatus: string;
    stageSchema?: StageSchemaItem[];
    onReset?: () => void;
    artifacts?: any[];
}

export const BuildMonitor: React.FC<Props> = ({ designName, jobId, events, jobStatus, stageSchema, onReset, artifacts }) => {
    const logsRef = useRef<HTMLDivElement>(null);
    const [cancelling, setCancelling] = React.useState(false);
    const [localCancelled, setLocalCancelled] = React.useState(false);

    // Flat Silicon Artifacts Viewer States
    const [selectedFile, setSelectedFile] = React.useState<string>('');
    const [fileContent, setFileContent] = React.useState<string>('');
    const [loadingFile, setLoadingFile] = React.useState<boolean>(false);

    // Fetch selected artifact content dynamically
    React.useEffect(() => {
        if (!selectedFile) {
            setFileContent('');
            return;
        }
        setLoadingFile(true);
        api.get(`/build/artifacts/${designName}/${selectedFile}`)
            .then(res => {
                setFileContent(typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2));
                setLoadingFile(false);
            })
            .catch(() => {
                setFileContent('Error loading file contents.');
                setLoadingFile(false);
            });
    }, [selectedFile, designName]);

    // Download individual file securely
    const handleDownloadFile = async (filename: string) => {
        try {
            const res = await api.get(`/build/artifacts/${designName}/${filename}`, { responseType: 'text' });
            const blob = new Blob([res.data], { type: 'text/plain' });
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
        } catch (err) {
            console.error('Failed to download file:', err);
        }
    };

    const mergedDisplay: Record<string, { label: string; icon: string }> = React.useMemo(() => {
        if (!stageSchema || stageSchema.length === 0) return STATES_DISPLAY;
        const map: Record<string, { label: string; icon: string }> = {};
        for (const stage of stageSchema) {
            map[stage.state] = { label: stage.label, icon: stage.icon };
        }
        if (!map.SUCCESS) map.SUCCESS = STATES_DISPLAY.SUCCESS;
        return map;
    }, [stageSchema]);

    const stateOrder = React.useMemo(() => Object.keys(mergedDisplay), [mergedDisplay]);

    // unused variables removed for cleanup
    const currentState = events.length > 0 ? events[events.length - 1].state : 'INIT';
    const currentStateIndex = stateOrder.indexOf(currentState);
    const furthestReachedIndex = Math.max(
        0,
        ...events
            .map(e => stateOrder.indexOf(e.state))
            .filter(idx => idx >= 0)
    );
    const currentStep = Math.max(1, (currentStateIndex >= 0 ? currentStateIndex : furthestReachedIndex) + 1);
    const logEvents = events.filter(e => e.message && e.message.trim().length > 0);
    const effectiveJobStatus = localCancelled ? 'cancelled' : jobStatus;
    const isDone = ['done', 'failed', 'cancelled', 'cancelling'].includes(effectiveJobStatus);

    useEffect(() => {
        if (logsRef.current) {
            logsRef.current.scrollTop = logsRef.current.scrollHeight;
        }
    }, [events]);

    const activeActor = React.useMemo(() => {
        const KNOWN_TOOLS = ['OPENROAD', 'MAGIC', 'YOSYS', 'IVERILOG', 'VERILATOR', 'SBY', 'SYMBIOASIS', 'KLAYOUT', 'NETGEN'];
        const LOG_LEVELS = ['WARNING', 'CRITICAL', 'ERROR', 'INFO', 'DEBUG'];
        for (let i = logEvents.length - 1; i >= 0; i--) {
            const msg = (logEvents[i].message || '').trim();
            if (msg.startsWith('agentic:')) return 'agentic';
            if (msg.startsWith('[')) {
                const match = msg.match(/^\[(.*?)\]/);
                if (match) {
                    const prefixUpper = match[1].toUpperCase();
                    if (KNOWN_TOOLS.includes(prefixUpper)) return match[1]; // e.g. 'OPENROAD'
                    if (!LOG_LEVELS.includes(prefixUpper)) return 'agentic'; // Rewrite 'Architect' to 'agentic'
                }
            }
        }
        return 'System';
    }, [logEvents]);

    const handleCancel = async () => {
        if (!jobId || cancelling) return;
        setCancelling(true);
        try {
            await api.post(`/build/cancel/${jobId}`);
            setLocalCancelled(true);
            setCancelling(false);
        } catch {
            setCancelling(false);
        }
    };

    const processedLogs = React.useMemo(() => {
        const clean: string[] = [];
        const KNOWN_TOOLS = ['OPENROAD', 'MAGIC', 'YOSYS', 'IVERILOG', 'VERILATOR', 'SBY', 'SYMBIOASIS', 'KLAYOUT', 'NETGEN'];
        const LOG_LEVELS = ['WARNING', 'CRITICAL', 'ERROR', 'INFO', 'DEBUG'];
        
        for (let i = 0; i < logEvents.length; i++) {
            let msg = (logEvents[i].message || '').trim();
            if (!msg) continue;
            
            const bracketMatch = msg.match(/^\[(.*?)\]\s*(.*)/);
            if (bracketMatch) {
                const prefixUpper = bracketMatch[1].toUpperCase();
                if (LOG_LEVELS.includes(prefixUpper)) {
                    msg = `[${prefixUpper}] ${bracketMatch[2]}`;
                } else if (!KNOWN_TOOLS.includes(prefixUpper)) {
                    msg = `agentic: ${bracketMatch[2]}`;
                }
            }

            msg = msg.replace(/⚠️\s*/g, '[WARNING] ');
            msg = msg.replace(/^agentic:\s*\[WARNING\]/i, '[WARNING]');
            msg = msg.replace(/^agentic:\s*\[CRITICAL\]/i, '[CRITICAL]');
            msg = msg.replace(/^agentic:\s*\[INFO\]/i, '[INFO]');

            const cleanMsg = msg.replace(/^agentic:\s*/, '').trim();
            const prev = clean.length > 0 ? clean[clean.length - 1] : null;
            if (prev) {
                const cleanPrev = prev.replace(/^agentic:\s*/, '').trim();
                if (cleanMsg === cleanPrev) {
                    if (msg.startsWith('agentic:') && !msg.includes('[WARNING]') && !msg.includes('[CRITICAL]') && !msg.includes('[INFO]')) {
                        clean[clean.length - 1] = msg;
                    }
                    continue; 
                }
            }

            if (/^'[a-z_]+',\s+'[a-z_]+',/.test(msg)) continue;
            if (msg.startsWith('Transitioning: ')) msg = `[INFO] ${msg}`;
            if (msg.startsWith('Running HierarchyExpander') || msg.startsWith('Spec validation complete')) msg = `[INFO] ${msg}`;

            clean.push(msg);
        }
        return clean;
    }, [logEvents]);

    return (
        <div className="monitor-root">
            {/* Header */}
            <div className="monitor-header">
                <div className="monitor-chip-badge">
                    <span className="badge-dot" data-status={effectiveJobStatus} />
                    <span className="badge-name">{designName}</span>
                </div>
                <div className="monitor-status">
                    {!isDone ? (
                        <>
                            <span className="spinner" />
                            <span>Step {currentStep} / {stateOrder.length}</span>
                            <button
                                className="cancel-btn"
                                onClick={handleCancel}
                                disabled={cancelling}
                            >
                                {cancelling ? 'Stopping...' : 'Cancel'}
                            </button>
                            {onReset && (
                                <button
                                    onClick={onReset}
                                    style={{
                                        background: 'rgba(239, 68, 68, 0.1)',
                                        border: '1px solid rgba(239, 68, 68, 0.35)',
                                        color: '#ef4444',
                                        borderRadius: '4px',
                                        padding: '0.3rem 0.65rem',
                                        fontSize: '0.72rem',
                                        cursor: 'pointer',
                                        fontFamily: 'monospace',
                                        fontWeight: 600,
                                        marginLeft: '0.5rem',
                                        transition: 'all 0.15s ease'
                                    }}
                                >
                                    FORCE RESET
                                </button>
                            )}
                        </>
                    ) : (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                            <span style={{ color: effectiveJobStatus === 'done' ? 'var(--success)' : 'var(--fail)', fontWeight: 600 }}>
                                {effectiveJobStatus === 'done' ? 'Complete' : effectiveJobStatus === 'cancelled' ? 'Cancelled' : 'Failed'}
                            </span>
                            {onReset && (
                                <button
                                    onClick={onReset}
                                    style={{
                                        background: 'rgba(59, 130, 246, 0.12)',
                                        border: '1px solid rgba(59, 130, 246, 0.45)',
                                        color: '#60a5fa',
                                        borderRadius: '4px',
                                        padding: '0.32rem 0.75rem',
                                        fontSize: '0.74rem',
                                        cursor: 'pointer',
                                        fontFamily: 'monospace',
                                        fontWeight: 600,
                                        transition: 'all 0.15s ease'
                                    }}
                                >
                                    BACK TO STUDIO
                                </button>
                            )}
                        </div>
                    )}
                </div>
            </div>

            {/* Body */}
            <div className="monitor-body">
                {/* Checkpoint Timeline removed to maximize terminal width */}

                {/* Live Terminal */}
                <div className="terminal-column" style={{ background: '#18181b', border: '1px solid #27272a', borderRadius: '4px', boxShadow: 'none' }}>
                    <div className="section-heading" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', color: '#a1a1aa' }}>
                        <span>Live Log</span>
                        {!isDone && logEvents.length > 0 && (
                            <div className="active-actor-pill" style={{ background: '#09090b', border: '1px solid #27272a', borderRadius: '4px', color: '#a1a1aa' }}>
                                <span className={activeActor === 'agentic' ? 'actor-indicator-agent' : 'actor-indicator-tool'} style={{ boxShadow: 'none' }} />
                                Active: {activeActor}
                            </div>
                        )}
                    </div>
                    <div className="live-terminal" ref={logsRef} style={{ background: '#09090b', borderRadius: '4px', border: '1px solid #27272a' }}>
                        {logEvents.length === 0 ? (
                            <span className="terminal-waiting" style={{ color: '#52525b' }}>Waiting for AgentIC to start...</span>
                        ) : (
                            <>
                                {processedLogs.map((msg, i) => {
                                    let msgType = 'normal';
                                    if (msg.startsWith('[WARNING]')) msgType = 'warn';
                                    else if (msg.startsWith('[CRITICAL]') || msg.startsWith('[ERROR]')) msgType = 'crit';
                                    else if (msg.includes('WNS is now MET') || msg.includes('0 violations found') || msg.includes('successfully') || msg.startsWith('[INFO]')) msgType = 'success';
                                    else if (msg.startsWith('agentic:')) {
                                        const isAction = /(Expanding|Patch applied|Pipeline fully repaired|Synthesizing|Fixing|Regenerating|Extracting)/i.test(msg);
                                        msgType = isAction ? 'agent-action' : 'agent-thought';
                                    }
                                    else if (msg.startsWith('[')) msgType = 'tool';

                                    return (
                                        <motion.div
                                            key={i}
                                            className="terminal-line"
                                            initial={{ opacity: 0 }}
                                            animate={{ opacity: 1 }}
                                            transition={{ duration: 0.15 }}
                                        >
                                            <span className={`terminal-msg type-${msgType}`}>
                                                {msg}
                                            </span>
                                        </motion.div>
                                    );
                                })}
                                {!isDone && (
                                    <div className="terminal-prompt" style={{ color: '#a1a1aa' }}>
                                        <span className="prompt-char" style={{ color: '#a1a1aa' }}>$</span>
                                        <span className="cursor-blink">▋</span>
                                    </div>
                                )}
                            </>
                        )}
                    </div>

                    {/* Progress */}
                    <div className="progress-bar-wrap" style={{ background: '#27272a', borderRadius: '2px' }}>
                        <div
                            className="progress-bar-fill"
                            style={{ width: `${(currentStep / stateOrder.length) * 100}%`, background: '#a1a1aa', borderRadius: '2px' }}
                        />
                    </div>
                    <div className="progress-label" style={{ color: '#a1a1aa', fontFamily: 'monospace' }}>
                        {Math.round((currentStep / stateOrder.length) * 100)}% complete
                    </div>

                    {/* Real-time Flat Tabbed Artifacts Viewer */}
                    {artifacts && artifacts.length > 0 && (
                        <div style={{
                            marginTop: '1.25rem',
                            borderTop: '1px solid #27272a',
                            paddingTop: '1.25rem',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '0.75rem',
                        }}>
                            <div style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                            }}>
                                <span style={{
                                    fontFamily: 'monospace',
                                    fontSize: '0.72rem',
                                    letterSpacing: '0.12em',
                                    textTransform: 'uppercase',
                                    color: '#a1a1aa',
                                    fontWeight: 700
                                }}>
                                    Generated Silicon Artifacts
                                </span>
                                <span style={{ fontSize: '0.7rem', color: '#52525b', fontFamily: 'monospace' }}>
                                    ({artifacts.length} files detected)
                                </span>
                            </div>

                            {/* Horizontal Tabs Scroll */}
                            <div style={{
                                display: 'flex',
                                gap: '0.4rem',
                                overflowX: 'auto',
                                paddingBottom: '0.4rem',
                            }}>
                                {artifacts.map((art, idx) => (
                                    <button
                                        key={idx}
                                        onClick={() => setSelectedFile(selectedFile === art.name ? '' : art.name)}
                                        style={{
                                            background: selectedFile === art.name ? '#27272a' : 'transparent',
                                            border: '1px solid #27272a',
                                            color: selectedFile === art.name ? '#f4f4f5' : '#a1a1aa',
                                            fontFamily: 'monospace',
                                            fontSize: '0.74rem',
                                            padding: '0.35rem 0.7rem',
                                            borderRadius: '3px',
                                            cursor: 'pointer',
                                            whiteSpace: 'nowrap',
                                            transition: 'all 0.1s ease',
                                        }}
                                    >
                                        📄 {art.name}
                                    </button>
                                ))}
                            </div>

                            {/* Code Viewer Panel */}
                            {selectedFile && (
                                <div style={{
                                    display: 'flex',
                                    flexDirection: 'column',
                                    border: '1px solid #27272a',
                                    borderRadius: '4px',
                                    background: '#09090b',
                                    overflow: 'hidden',
                                    animation: 'fadeIn 0.15s ease-out',
                                }}>
                                    <div style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                        background: '#18181b',
                                        padding: '0.45rem 0.8rem',
                                        borderBottom: '1px solid #27272a',
                                    }}>
                                        <span style={{ fontFamily: 'monospace', fontSize: '0.75rem', color: '#a1a1aa', fontWeight: 600 }}>
                                            {selectedFile}
                                        </span>
                                        <button
                                            onClick={() => handleDownloadFile(selectedFile)}
                                            style={{
                                                background: 'transparent',
                                                border: 'none',
                                                color: '#60a5fa',
                                                fontFamily: 'monospace',
                                                fontSize: '0.72rem',
                                                cursor: 'pointer',
                                                textDecoration: 'underline',
                                                padding: 0,
                                            }}
                                        >
                                            [DOWNLOAD]
                                        </button>
                                    </div>
                                    <pre style={{
                                        margin: 0,
                                        padding: '0.85rem',
                                        maxHeight: '200px',
                                        overflowY: 'auto',
                                        fontFamily: 'monospace',
                                        fontSize: '0.75rem',
                                        color: '#e4e4e7',
                                        background: '#09090b',
                                        lineHeight: '1.45',
                                        whiteSpace: 'pre-wrap',
                                        textAlign: 'left',
                                    }}>
                                        {loadingFile ? 'Fetching artifact content...' : fileContent}
                                    </pre>
                                </div>
                             )}
                        </div>
                    )}
                </div>
            </div>

            {/* Footer */}
            {!isDone && (
                <div className="monitor-footer" style={{ borderTop: '1px solid #27272a', paddingTop: '0.75rem', color: '#a1a1aa', fontFamily: 'monospace', fontSize: '0.75rem' }}>
                    <span className="spinner-small" />
                    AgentIC is building your chip autonomously.
                </div>
            )}
        </div>
    );
};
