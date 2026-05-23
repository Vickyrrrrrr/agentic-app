import { useState, useEffect, useRef } from 'react';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import {
    ArrowRight,
    Bot,
    Fingerprint,
    KeyRound,
    ShieldCheck,
    Sparkles,
    Waypoints,
} from 'lucide-react';
import { ActivityFeed } from '../components/ActivityFeed';
import { StageProgressBar } from '../components/StageProgressBar';
import { ApprovalCard } from '../components/ApprovalCard';
import ElaborationCard from '../components/ElaborationCard';
import { BillingModal } from '../components/BillingModal';
import { api, API_BASE, getSseHeaders } from '../api';
import { toUserError, isNetworkError } from '../utils/errorFormatter';
import '../hitl.css';

const PIPELINE_STAGES = [
    'INIT', 'SPEC', 'SPEC_VALIDATE', 'HIERARCHY_EXPAND', 'FEASIBILITY_CHECK', 'VERIFICATION_PLAN', 'RTL_GEN', 'RTL_FIX', 'CDC_ANALYZE', 'VERIFICATION', 'FORMAL_VERIFY',
    'COVERAGE_CHECK', 'REGRESSION', 'SDC_GEN', 'SYNTHESIS',
    'FLOORPLAN', 'HARDENING', 'CONVERGENCE_REVIEW', 'ECO_PATCH', 'POWER_ANALYSIS', 'TIMING_ANALYSIS', 'PHYSICAL_VERIFY', 'SIGNOFF', 'IP_PACKAGE',
];
const TOTAL = PIPELINE_STAGES.length;

const STAGE_ENCOURAGEMENTS: Record<string, string> = {
    INIT:              'Setting up your build environment…',
    SPEC:              'Translating your description into a chip specification…',
    SPEC_VALIDATE:     'Validating spec — classifying design, checking completeness, generating assertions…',
    HIERARCHY_EXPAND:   'Expanding complex submodules into nested specifications…',
    FEASIBILITY_CHECK: 'Checking node contract, memory macros, area, and PDK feasibility…',
    VERIFICATION_PLAN: 'Building verification plan & SVA properties…',
    RTL_GEN:           'Writing Verilog — your chip is taking shape…',
    RTL_FIX:           'Fixing any RTL issues automatically…',
    CDC_ANALYZE:      'Analyzing clock domain crossings…',
    VERIFICATION:     'Running simulation — making sure your logic is correct…',
    FORMAL_VERIFY:    'Proving your chip is correct with formal methods…',
    COVERAGE_CHECK:   'Measuring how thoroughly the tests cover your design…',
    REGRESSION:       'Running the full regression suite…',
    SDC_GEN:          'Generating timing constraints for synthesis…',
    SYNTHESIS:        'Synthesizing RTL to gate-level netlist…',
    FLOORPLAN:        'Planning the physical layout of your chip…',
    HARDENING:        'Running place-and-route — turning RTL into real silicon…',
    TIMING_ANALYSIS:   'Running static timing analysis…',
    CONVERGENCE_REVIEW:'Checking that timing is met across all corners…',
    ECO_PATCH:        'Applying final tweaks for clean sign-off…',
    POWER_ANALYSIS:    'Analyzing power consumption…',
    PHYSICAL_VERIFY:   'Running physical verification checks…',
    SIGNOFF:         'Reviewing OSS signoff evidence and commercial blockers…',
    IP_PACKAGE:      'Packaging IP for reuse…',
};

const MILESTONE_TOASTS: Record<string, { title: string; msg: string }> = {
    RTL_GEN:   { title: 'RTL Complete', msg: 'Your chip can now run instructions. Verilog is ready.' },
    VERIFICATION: { title: 'Verification Passed', msg: 'All simulation tests passed. Design is logically correct.' },
    SYNTHESIS: { title: 'Synthesis Complete', msg: 'RTL converted to gate-level netlist.' },
    HARDENING: { title: 'Silicon Layout Done', msg: 'Place-and-route complete. Your chip has a physical form.' },
    SIGNOFF:   { title: 'OSS Signoff Reviewed', msg: 'Evidence is collected; commercial blockers remain explicit.' },
};

const STAGE_LABELS: Record<string, string> = {
    INIT: 'Initialization', SPEC: 'Specification', SPEC_VALIDATE: 'Spec Validation', HIERARCHY_EXPAND: 'Hierarchy Expansion',
    FEASIBILITY_CHECK: 'Feasibility Check', VERIFICATION_PLAN: 'Verification Plan', RTL_GEN: 'RTL Generation', RTL_FIX: 'RTL Fix',
    CDC_ANALYZE: 'CDC Analysis', VERIFICATION: 'Verification', FORMAL_VERIFY: 'Formal Verification',
    COVERAGE_CHECK: 'Coverage Check', REGRESSION: 'Regression', SDC_GEN: 'SDC Generation', SYNTHESIS: 'Synthesis',
    FLOORPLAN: 'Floorplan', HARDENING: 'Hardening', TIMING_ANALYSIS: 'Timing Analysis',
    CONVERGENCE_REVIEW: 'Convergence Review', ECO_PATCH: 'ECO Patch', POWER_ANALYSIS: 'Power Analysis',
    PHYSICAL_VERIFY: 'Physical Verification', SIGNOFF: 'Signoff', IP_PACKAGE: 'IP Package', FAIL: 'Failed',
};

const MANDATORY_STAGES = new Set([
    'INIT', 'SPEC', 'SPEC_VALIDATE', 'HIERARCHY_EXPAND', 'FEASIBILITY_CHECK', 'VERIFICATION_PLAN', 'RTL_GEN', 'RTL_FIX', 'CDC_ANALYZE',
    'VERIFICATION', 'FORMAL_VERIFY', 'COVERAGE_CHECK', 'REGRESSION', 'SDC_GEN', 'SYNTHESIS',
    'FLOORPLAN', 'HARDENING', 'TIMING_ANALYSIS', 'CONVERGENCE_REVIEW', 'POWER_ANALYSIS',
    'PHYSICAL_VERIFY', 'SIGNOFF', 'IP_PACKAGE',
]);

type BuildMode = 'quick' | 'verified' | 'full';
const BUILD_MODE_SKIPS: Record<BuildMode, string[]> = {
    quick: ['CDC_ANALYZE', 'FORMAL_VERIFY', 'COVERAGE_CHECK', 'REGRESSION', 'SDC_GEN', 'SYNTHESIS', 'FLOORPLAN', 'HARDENING', 'CONVERGENCE_REVIEW', 'ECO_PATCH', 'POWER_ANALYSIS', 'TIMING_ANALYSIS', 'PHYSICAL_VERIFY', 'SIGNOFF', 'IP_PACKAGE'],
    verified: ['CDC_ANALYZE', 'FORMAL_VERIFY', 'COVERAGE_CHECK', 'REGRESSION', 'CONVERGENCE_REVIEW', 'ECO_PATCH', 'POWER_ANALYSIS', 'TIMING_ANALYSIS', 'PHYSICAL_VERIFY', 'IP_PACKAGE'],
    full: [],
};

const HITL_EXAMPLES = [
    {
        title: 'Compute core',
        prompt: '8-bit RISC CPU with Harvard architecture',
        note: 'A balanced architecture for stepping through review checkpoints.',
    },
    {
        title: 'High-throughput block',
        prompt: 'AXI4 DMA engine with 4 channels',
        note: 'Useful when you want to inspect architecture and verification gates closely.',
    },
    {
        title: 'Peripheral path',
        prompt: 'UART controller at 115200 baud',
        note: 'A smaller system for validating approval flow and artifact generation.',
    },
];

interface BuildEvent {
    type: string;
    state: string;
    message: string;
    step: number;
    total_steps: number;
    timestamp: number | string;
    status?: string;
    is_live_waiting?: boolean;
    agent_name?: string;
    thought_type?: string;
    content?: string;
    stage_name?: string;
    summary?: string;
    artifacts?: Array<{ name: string; path: string; description: string }>;
    decisions?: string[];
    warnings?: string[];
    next_stage_name?: string;
    next_stage_preview?: string;
    options?: Array<Record<string, string>>;
    changes?: Array<Record<string, string>>;
    target?: string;
    category?: string;
    original_request?: string;
    constraint?: string;
    chosen_substitute?: string;
}

interface StageCompleteData {
    stage_name: string;
    summary: string;
    artifacts: Array<{ name: string; path: string; description: string }>;
    decisions: string[];
    warnings: string[];
    next_stage_name: string;
    next_stage_preview: string;
}

interface ElaborationData {
    options: Array<Record<string, string>>;
    message: string;
}

interface PdkOption {
    key: string;
    pdk: string;
    std_cell_library: string;
    description: string;
    available: boolean;
    gds_ready: boolean;
    tech_ok: boolean;
    proprietary: boolean;
    status: string;
    reason: string;
    maturity?: string;
    fabrication_ready?: boolean;
}

const PDK_MATURITY: Record<string, { label: string; warning: string }> = {
    production: { label: "Production Ready", warning: "" },
    experimental: { label: "Experimental", warning: "This PDK is not mature for fabrication. Results may be unreliable." },
    research: { label: "Research Only", warning: "This is an academic/research PDK — not suitable for real chip fabrication." },
    proprietary: { label: "Proprietary", warning: "This PDK requires foundry access and manual setup. Not available for open fabrication." },
    custom: { label: "Custom PDK", warning: "This custom PDK was detected on the server. Confirm foundry collateral and signoff flow before fabrication." },
};

function slugify(text: string): string {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9\s_]/g, '')
        .trim()
        .split(/\s+/)
        .slice(0, 4)
        .join('_')
        .substring(0, 48);
}

function notifyBuild(title: string, body: string) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
        new Notification(title, { body });
    } catch {
        // Browser/desktop notification support varies by shell.
    }
}

type Phase = 'prompt' | 'building' | 'done';

// sessionStorage keys for build resume after navigation
const SS_JOB_ID     = 'hitl_job_id';
const SS_PHASE      = 'hitl_phase';
const SS_DESIGN     = 'hitl_design_name';
const SS_PROMPT     = 'hitl_prompt';

export const HumanInLoopBuild = () => {
    // Restore from sessionStorage if user navigated away mid-build
    const savedJobId  = sessionStorage.getItem(SS_JOB_ID)  || '';
    const savedPhase  = (sessionStorage.getItem(SS_PHASE)  || 'prompt') as Phase;
    const savedDesign = sessionStorage.getItem(SS_DESIGN)  || '';
    const savedPrompt = sessionStorage.getItem(SS_PROMPT)  || '';

    const [phase, setPhase] = useState<Phase>(savedPhase);
    const [prompt, setPrompt] = useState(savedPrompt);
    const [designName, setDesignName] = useState(savedDesign);
    const [jobId, setJobId] = useState(savedJobId);
    const [events, setEvents] = useState<BuildEvent[]>([]);
    const [jobStatus, setJobStatus] = useState<string>('queued');
    const [result, setResult] = useState<any>(null);
    const [error, setError] = useState('');
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [showBillingModal, setShowBillingModal] = useState(false);
    // SSE abort controller — only aborted on explicit Cancel, never on unmount
    const abortCtrlRef = useRef<AbortController | null>(null);

    const [skipOpenlane, setSkipOpenlane] = useState(false);
    const [skipCoverage, setSkipCoverage] = useState(false);
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [maxRetries, setMaxRetries] = useState(5);
    const [showThinking, setShowThinking] = useState(false);
    const [minCoverage, setMinCoverage] = useState(80.0);
    const [strictGates, setStrictGates] = useState(false);
    const [pdkProfile, setPdkProfile] = useState("sky130");
    const [pdkOptions, setPdkOptions] = useState<PdkOption[]>([]);

    const [buildMode, setBuildMode] = useState<BuildMode>('verified');
    const [skipStages, setSkipStages] = useState<Set<string>>(new Set(BUILD_MODE_SKIPS.verified));
    const [showStageToggles, setShowStageToggles] = useState(false);

    const [currentStage, setCurrentStage] = useState('INIT');
    const [completedStages, setCompletedStages] = useState<Set<string>>(new Set());
    const [failedStage, setFailedStage] = useState<string | undefined>();
    const [waitingForApproval, setWaitingForApproval] = useState(false);
    const [approvalData, setApprovalData] = useState<StageCompleteData | null>(null);
    const [elaborationData, setElaborationData] = useState<ElaborationData | null>(null);

    const [partialArtifacts, setPartialArtifacts] = useState<Array<{name: string; path: string; size: number; type: string}>>([]);
    const [showFullLog, setShowFullLog] = useState(false);

    const [thinkingData, setThinkingData] = useState<{ agent_name: string; message: string } | null>(null);
    const [stallWarning, setStallWarning] = useState<string | null>(null);
    const [milestoneToast, setMilestoneToast] = useState<{ title: string; msg: string } | null>(null);
    const [profile, setProfile] = useState<{ has_byok_key?: boolean } | null>(null);
    const milestoneTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const pendingLaunchAfterByokRef = useRef(false);
    const byokKey = localStorage.getItem('agentic_byok_key');
    const hasByokReady = Boolean(byokKey) || Boolean(profile?.has_byok_key);
    const selectedPdk = pdkOptions.find((pdk) => pdk.key === pdkProfile);

    useEffect(() => {
        api.get('/profile')
            .then((res) => setProfile(res.data || null))
            .catch(() => setProfile(null));
        api.get('/pdks')
            .then((res) => {
                const pdks: PdkOption[] = res.data?.pdks || [];
                const defaultPdk = res.data?.default || 'sky130';
                const readyDefault = pdks.find((pdk) => pdk.key === defaultPdk && pdk.gds_ready);
                const firstReady = pdks.find((pdk) => pdk.gds_ready);
                setPdkOptions(pdks);
                setPdkProfile((current) => {
                    if (pdks.some((pdk) => pdk.key === current && pdk.gds_ready)) return current;
                    return readyDefault?.key || firstReady?.key || defaultPdk;
                });
            })
            .catch(() => setPdkOptions([]));
    }, []);

    // Persist active build state to sessionStorage so navigation doesn't lose it
    useEffect(() => {
        if (phase === 'building' && jobId) {
            sessionStorage.setItem(SS_JOB_ID,  jobId);
            sessionStorage.setItem(SS_PHASE,   phase);
            sessionStorage.setItem(SS_DESIGN,  designName);
            sessionStorage.setItem(SS_PROMPT,  prompt);
        }
    }, [phase, jobId, designName, prompt]);

    const clearSessionStorage = () => {
        sessionStorage.removeItem(SS_JOB_ID);
        sessionStorage.removeItem(SS_PHASE);
        sessionStorage.removeItem(SS_DESIGN);
        sessionStorage.removeItem(SS_PROMPT);
    };

    const handleLaunch = async () => {
        if (!prompt.trim()) return;
        setError('');
        setEvents([]);
        setResult(null);
        setPartialArtifacts([]);
        setJobStatus('queued');
        setCurrentStage('INIT');
        setCompletedStages(new Set());
        setFailedStage(undefined);
        setWaitingForApproval(false);
        setApprovalData(null);
        setElaborationData(null);

        const currentByokKey = localStorage.getItem('agentic_byok_key');
        const hasAnyByok = Boolean(currentByokKey) || Boolean(profile?.has_byok_key);
        if (!hasAnyByok) {
            pendingLaunchAfterByokRef.current = true;
            setShowBillingModal(true);
            return;
        }

        const effectiveSkipOpenlane = buildMode === 'quick' || skipOpenlane;
        const effectiveSkipCoverage = skipCoverage || skipStages.has('COVERAGE_CHECK');
        if (!effectiveSkipOpenlane && selectedPdk && !selectedPdk.gds_ready) {
            setError(`${selectedPdk.key} is not ready for GDSII on this VPS. Install the PDK first or choose an installed PDK.`);
            return;
        }
        try {
            const res = await api.post(`/build`, {
                design_name: designName || slugify(prompt),
                description: prompt,
                skip_openlane: effectiveSkipOpenlane,
                skip_coverage: effectiveSkipCoverage,
                max_retries: maxRetries,
                show_thinking: showThinking,
                min_coverage: minCoverage,
                strict_gates: strictGates,
                pdk_profile: pdkProfile,
                human_in_loop: true,
                skip_stages: Array.from(skipStages),
                api_key: currentByokKey || null,
                plan_type: 'byok',
            });
            const { job_id, design_name: dn } = res.data;
            setJobId(job_id);
            if (dn) setDesignName(dn);
            setJobStatus('running');
            setPhase('building');
            void startStreaming(job_id);
        } catch (e: any) {
            if (isNetworkError(e)) {
                setError('Unable to connect to the build service. Please try again later.');
            } else {
                setError(toUserError(e?.response?.data?.detail, 'Build failed. Please try again or contact support.'));
            }
        }
    };

    async function startStreaming(jid: string) {
        // Abort any existing stream before opening a new one
        if (abortCtrlRef.current) abortCtrlRef.current.abort();
        const ctrl = new AbortController();
        abortCtrlRef.current = ctrl;
        setEvents([]);
        let retryCount = 0;

        const currentByokKey = localStorage.getItem('agentic_byok_key');
        const headers = await getSseHeaders(
            currentByokKey ? { 'X-LLM-API-Key': currentByokKey } : {}
        );

        fetchEventSource(`${API_BASE}/build/stream/${jid}`, {
            method: 'GET',
            headers,
            signal: ctrl.signal,
            openWhenHidden: true,
            async onopen(response) {
                const contentType = response.headers.get('content-type') || '';
                if (response.ok && contentType.includes('text/event-stream')) {
                    retryCount = 0;
                    setStallWarning(null);
                    return;
                }
                if ([401, 403, 404].includes(response.status)) {
                    throw new Error('Live stream unavailable. Please try again.');
                }
                throw new Error('Live stream temporarily unavailable.');
            },
            onmessage(evt) {
                try {
                    const data: BuildEvent = JSON.parse(evt.data);
                    if (data.type === 'ping') return;

                    if (data.type === 'stream_end') {
                        ctrl.abort();
                        fetchResult(jid, data.status as any);
                        return;
                    }

                    if (data.type === 'stall_warning') {
                        setStallWarning(data.message || 'No activity for 5 minutes. The LLM may be stuck. You can cancel and retry.');
                        return;
                    }

                    if (data.type === 'log' || data.type === 'checkpoint' || data.type === 'transition') {
                        setStallWarning(null);
                    }

                    if (data.type === 'agent_thinking') {
                        setThinkingData({ agent_name: data.agent_name || '', message: data.message || '' });
                        return;
                    }

                    if (data.type !== 'agent_thinking') {
                        setThinkingData(null);
                    }

                    if (data.type === 'stage_complete') {
                        if (data.is_live_waiting) {
                            setApprovalData({
                                stage_name: data.stage_name || data.state || '',
                                summary: data.summary || '',
                                artifacts: data.artifacts || [],
                                decisions: data.decisions || [],
                                warnings: data.warnings || [],
                                next_stage_name: data.next_stage_name || '',
                                next_stage_preview: data.next_stage_preview || '',
                            });
                            setWaitingForApproval(true);
                        } else {
                            setCompletedStages(prev => new Set(prev).add(data.stage_name || data.state || ''));
                        }
                        setCurrentStage(data.stage_name || data.state || '');
                        notifyBuild('AgentIC stage complete', `${data.stage_name || data.state || 'Stage'} is ready for review.`);
                    }

                    if (data.type === 'elaboration_waiting') {
                        setElaborationData({
                            options: data.options || [],
                            message: data.message || 'Waiting for architectural elaboration...',
                        });
                        setWaitingForApproval(true);
                    }

                    if (data.type === 'spec_reconciled') {
                        const count = data.changes?.length || 0;
                        notifyBuild('AgentIC adjusted the spec', `${count} PDK feasibility change${count === 1 ? '' : 's'} applied.`);
                    }

                    if (data.type === 'design_decision') {
                        notifyBuild('AgentIC design decision', data.message || 'A feasibility-driven design change was applied.');
                    }

                    if (data.type === 'transition' || data.state) {
                        const newState = data.state;
                        if (newState && newState !== 'UNKNOWN') {
                            setCurrentStage(prev => {
                                if (prev !== newState && prev !== 'INIT') {
                                    setCompletedStages(cs => {
                                        const next = new Set(cs);
                                        next.add(prev);
                                        return next;
                                    });
                                }
                                return newState;
                            });
                        }
                        if (newState === 'FAIL') {
                            setFailedStage(newState);
                        }
                    }

                    setEvents(prev => {
                        const isDuplicate = prev.some(e => e.timestamp === data.timestamp && e.type === data.type && e.message === data.message);
                        if (isDuplicate) return prev;
                        return [...prev, data];
                    });

                    if (data.type === 'error') {
                        setJobStatus('failed');
                    } else if (data.type !== 'stage_complete') {
                        setJobStatus('running');
                    }
                } catch { /* ignore parse errors */ }
            },
            onerror(err) {
                if (ctrl.signal.aborted) return;
                retryCount += 1;
                if (retryCount > 8) {
                    ctrl.abort();
                    setJobStatus('failed');
                    setError('Live connection lost. Checking final build status...');
                    void fetchResult(jid, 'failed');
                    throw err;
                }
                return Math.min(1000 * retryCount, 5000);
            }
        }).catch(() => {
            if (ctrl.signal.aborted) return;
            setJobStatus('failed');
            setError('Live connection lost. Checking final build status...');
            void fetchResult(jid, 'failed');
        });
    }

    // Auto-reconnect SSE if returning to page with an active build.
    useEffect(() => {
        if (savedPhase === 'building' && savedJobId) {
            void startStreaming(savedJobId);
        }
        // IMPORTANT: do not abort on unmount; the backend job runs independently.
        // Only explicit Cancel/Reset should abort the local stream.
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const fetchResult = async (jid: string, status: string) => {
        clearSessionStorage();
        setJobStatus(status === 'done' ? 'done' : 'failed');
        try {
            const res = await api.get(`/build/result/${jid}`);
            setResult(res.data.result);
        } catch { /* */ }
        if (status !== 'done' && designName) {
            try {
                const artRes = await api.get(`/build/artifacts/${designName}`);
                setPartialArtifacts(artRes.data.artifacts || []);
            } catch { /* */ }
        }
        setPhase('done');
    };

    const handleApprove = async () => {
        if (!approvalData || isSubmitting) return;
        setIsSubmitting(true);
        try {
            await api.post(`/approve`, {
                stage: approvalData.stage_name,
                design_name: designName,
            });
            setEvents(prev => [...prev, {
                type: 'user_action',
                state: approvalData.stage_name,
                message: `[User] Approved: ${approvalData.stage_name}`,
                step: 0,
                total_steps: 15,
                timestamp: new Date().toISOString(),
                agent_name: 'User',
                thought_type: 'user_action',
                content: `Approved: ${approvalData.stage_name}`,
            }]);
            setApprovalData(null);
            setWaitingForApproval(false);
            setCompletedStages(prev => {
                const next = new Set(prev);
                next.add(approvalData.stage_name);
                return next;
            });
            const toast = MILESTONE_TOASTS[approvalData.stage_name];
            if (toast) {
                if (milestoneTimerRef.current) clearTimeout(milestoneTimerRef.current);
                setMilestoneToast(toast);
                milestoneTimerRef.current = setTimeout(() => setMilestoneToast(null), 5000);
            }
        } catch (e: any) {
            setError(toUserError(e?.response?.data?.detail, 'Unable to approve. Please try again.'));
        }
        setIsSubmitting(false);
    };

    const handleReject = async (feedback: string) => {
        if (!approvalData || isSubmitting) return;
        setIsSubmitting(true);
        try {
            await api.post(`/reject`, {
                stage: approvalData.stage_name,
                design_name: designName,
                feedback: feedback || undefined,
            });
            const feedbackMsg = feedback ? ` - Feedback: ${feedback}` : '';
            setEvents(prev => [...prev, {
                type: 'user_action',
                state: approvalData.stage_name,
                message: `[User] Rejected: ${approvalData.stage_name}${feedbackMsg}`,
                step: 0,
                total_steps: 15,
                timestamp: new Date().toISOString(),
                agent_name: 'User',
                thought_type: 'user_action',
                content: `Rejected: ${approvalData.stage_name}${feedbackMsg}`,
            }, {
                type: 'agent_thought',
                state: approvalData.stage_name,
                message: 'Retrying stage with your feedback...',
                step: 0,
                total_steps: 15,
                timestamp: new Date().toISOString(),
                agent_name: 'Orchestrator',
                thought_type: 'observation',
                content: 'Retrying stage with your feedback...',
            }]);
            setApprovalData(null);
            setWaitingForApproval(false);
        } catch (e: any) {
            setError(toUserError(e?.response?.data?.detail, 'Unable to submit feedback. Please try again.'));
        }
        setIsSubmitting(false);
    };

    const handleElaborate = async (choice: string) => {
        if (!elaborationData || isSubmitting) return;
        setIsSubmitting(true);
        try {
            await api.post(`/build/elaborate`, {
                job_id: jobId,
                choice: choice,
            });
            const displayChoice = choice.length > 25 ? choice.substring(0, 22) + '...' : choice;
            setEvents(prev => [...prev, {
                type: 'user_action',
                state: 'SPEC_VALIDATE',
                message: `[User] Selected architectural preference: ${displayChoice}`,
                step: 0,
                total_steps: 1,
                timestamp: new Date().toISOString(),
                agent_name: 'User',
                thought_type: 'user_action',
                content: `Elaboration choice: ${choice}`,
            }]);
            setElaborationData(null);
            setWaitingForApproval(false);
        } catch (e: any) {
            setError(toUserError(e?.response?.data?.detail, 'Unable to submit your choice. Please try again.'));
        }
        setIsSubmitting(false);
    };

    const handleReset = () => {
        // Abort SSE stream (explicit user reset)
        abortCtrlRef.current?.abort();
        clearSessionStorage();
        setPhase('prompt');
        setEvents([]);
        setResult(null);
        setJobId('');
        setJobStatus('queued');
        setError('');
        setPrompt('');
        setCurrentStage('INIT');
        setCompletedStages(new Set());
        setFailedStage(undefined);
        setWaitingForApproval(false);
        setApprovalData(null);
        setThinkingData(null);
        setStallWarning(null);
        setBuildMode('verified');
        setSkipStages(new Set(BUILD_MODE_SKIPS.verified));
        setSkipCoverage(false);
        setShowStageToggles(false);
        setPartialArtifacts([]);
        setShowFullLog(false);
    };

    const handleCancel = async () => {
        // Abort SSE stream + tell backend to cancel the job
        if (abortCtrlRef.current) abortCtrlRef.current.abort();
        if (jobId) {
            try { await api.post(`/build/cancel/${jobId}`); } catch { /* */ }
        }
        handleReset();
    };

    const stepNum = Math.max(1, PIPELINE_STAGES.indexOf(currentStage) + 1);
    const pct = Math.round((completedStages.size / TOTAL) * 100);
    const remaining = Math.max(0, TOTAL - completedStages.size);
    const estMinutes = remaining * 2;

    useEffect(() => {
        if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
        // No cleanup abort here — navigating away should NOT cancel the build.
        // The backend job runs independently on the server.
    }, []);

    return (
        <div className="hitl-root">
            {/* ── PROMPT PHASE ── */}
            {phase === 'prompt' && (
                <div className="hitl-prompt-screen">
                    <div className="hitl-launch-shell">
                        <section className="hitl-launch-header">
                            <div className="hitl-launch-copy">
                                <span className="hitl-launch-kicker">
                                    <Sparkles size={14} />
                                    HUMAN-IN-THE-LOOP
                                </span>
                                <h1 className="hitl-launch-title">Keep the agent autonomous, but keep every critical decision reviewable.</h1>
                                <p className="hitl-launch-subtitle">
                                    This mode pauses at key checkpoints so you can approve stage outputs, redirect the build,
                                    and inspect artifacts before AgentIC commits more compute.
                                </p>
                            </div>
                            <div className="hitl-launch-meta">
                                <button className="hitl-control-btn" onClick={() => setShowBillingModal(true)}>
                                    <KeyRound size={15} />
                                    Configure BYOK
                                </button>
                                <div className="hitl-meta-pills">
                                    <span className={`hitl-meta-pill ${hasByokReady ? 'is-ready' : 'is-warn'}`}>
                                        <Fingerprint size={14} />
                                        {hasByokReady ? 'BYOK configured' : 'BYOK required'}
                                    </span>
                                    <span className="hitl-meta-pill">
                                        <ShieldCheck size={14} />
                                        {buildMode === 'quick' ? 'Quick review path' : buildMode === 'verified' ? 'Verified review path' : 'Fabrication review path'}
                                    </span>
                                </div>
                            </div>
                        </section>

                        <div className="hitl-launch-grid">
                            <section className="hitl-prompt-card hitl-prompt-card--premium">
                                <div className="hitl-section-head">
                                    <div>
                                        <span className="hitl-section-label">Build Brief</span>
                                        <h2 className="hitl-section-title">Operator-guided launch</h2>
                                    </div>
                                    <span className="hitl-section-chip">{Array.from(skipStages).length} stages skipped</span>
                                </div>

                                <div className="hitl-examples hitl-examples--cards">
                                    {HITL_EXAMPLES.map(example => (
                                        <button key={example.prompt} className="hitl-example-card" onClick={() => {
                                            setPrompt(example.prompt);
                                            setDesignName(slugify(example.prompt));
                                        }}>
                                            <strong>{example.title}</strong>
                                            <span>{example.note}</span>
                                            <p>{example.prompt}</p>
                                        </button>
                                    ))}
                                </div>

                                <div className="hitl-textarea-wrap">
                                    <Bot size={18} className="hitl-textarea-icon" />
                                    <textarea
                                        className="hitl-prompt-textarea hitl-prompt-textarea--premium"
                                        placeholder="Describe the chip you want to build in plain English…"
                                        value={prompt}
                                        onChange={e => {
                                            const val = e.target.value;
                                            setPrompt(val);
                                            if (val.length > 8) {
                                                setDesignName(slugify(val));
                                            }
                                        }}
                                        rows={5}
                                        autoFocus
                                    />
                                </div>

                                <div className="hitl-design-name-row">
                                    <span className="hitl-design-label">Design ID</span>
                                    <input
                                        className="hitl-design-input"
                                        value={designName}
                                        onChange={e => setDesignName(e.target.value.replace(/[^a-z0-9_]/g, ''))}
                                        placeholder="Auto-generated when you describe the design"
                                    />
                                </div>

                                <div className="hitl-mode-row">
                                    <span className="hitl-mode-label">Approval depth</span>
                                    <div className="hitl-mode-pills">
                                        {(['quick', 'verified', 'full'] as BuildMode[]).map(mode => (
                                            <button
                                                key={mode}
                                                className={`hitl-mode-pill${buildMode === mode ? ' hitl-mode-pill--active' : ''}`}
                                            onClick={() => {
                                                setBuildMode(mode);
                                                setSkipStages(new Set(BUILD_MODE_SKIPS[mode]));
                                                if (mode === 'quick') setSkipOpenlane(true);
                                                if (mode === 'full' && selectedPdk && !selectedPdk.gds_ready) {
                                                    setError(`${selectedPdk.key} is not ready for GDSII on this VPS. Install the PDK first or choose an installed PDK.`);
                                                }
                                            }}
                                            >
                                                <span className="hitl-mode-pill-name">
                                                    {mode === 'quick' ? 'Quick RTL' : mode === 'verified' ? 'Verified Design' : 'Fabrication Ready'}
                                                </span>
                                                <span className="hitl-mode-pill-desc">
                                                    {mode === 'quick' ? 'RTL + basic verify' : mode === 'verified' ? 'Full verify pipeline' : 'All stages including physical'}
                                                </span>
                                            </button>
                                        ))}
                                    </div>
                                </div>

                                <div className="hitl-options-row hitl-options-row--stacked">
                                    <div className="hitl-toggle-grid">
                                        <label className="hitl-toggle">
                                            <input type="checkbox" checked={skipOpenlane} onChange={e => setSkipOpenlane(e.target.checked)} />
                                            <span>Skip OpenLane</span>
                                        </label>
                                        <label className="hitl-toggle">
                                            <input type="checkbox" checked={skipCoverage} onChange={e => setSkipCoverage(e.target.checked)} />
                                            <span>Skip Coverage</span>
                                        </label>
                                        <label className="hitl-toggle">
                                            <input type="checkbox" checked={strictGates} onChange={e => setStrictGates(e.target.checked)} />
                                            <span>Strict Gates</span>
                                        </label>
                                        <label className="hitl-toggle">
                                            <input type="checkbox" checked={showThinking} onChange={e => setShowThinking(e.target.checked)} />
                                            <span>Show Thinking</span>
                                        </label>
                                    </div>
                                    <button
                                        className="hitl-advanced-toggle"
                                        onClick={() => setShowAdvanced(!showAdvanced)}
                                    >
                                        {showAdvanced ? 'Hide advanced controls' : 'Show advanced controls'}
                                    </button>
                                </div>

                                {showAdvanced && (
                                    <div className="hitl-advanced-panel">
                                        <div className="hitl-opt-grid">
                                            <label className="hitl-opt">
                                                <span>Max Retries</span>
                                                <input type="number" value={maxRetries} onChange={e => setMaxRetries(Number(e.target.value))} />
                                            </label>
                                            <label className="hitl-opt">
                                                <span>Min Coverage %</span>
                                                <input type="number" step="0.1" value={minCoverage} onChange={e => setMinCoverage(Number(e.target.value))} />
                                            </label>
                                            <label className="hitl-opt">
                                                <span>PDK</span>
                                                <select
                                                    value={pdkProfile}
                                                    onChange={e => {
                                                        const next = e.target.value;
                                                        setPdkProfile(next);
                                                        const nextPdk = pdkOptions.find((pdk) => pdk.key === next);
                                                        if (nextPdk?.gds_ready) setError('');
                                                        if (!nextPdk?.gds_ready) setSkipOpenlane(true);
                                                    }}
                                                >
                                                    {(pdkOptions.length
                                                        ? pdkOptions
                                                        : [{
                                                            key: 'sky130',
                                                            pdk: 'sky130A',
                                                            std_cell_library: 'sky130_fd_sc_hd',
                                                            description: 'SkyWater 130nm',
                                                            available: true,
                                                            gds_ready: true,
                                                            tech_ok: true,
                                                            proprietary: false,
                                                            status: 'ready',
                                                            reason: '',
                                                            maturity: 'production',
                                                            fabrication_ready: true,
                                                        } as PdkOption]).map((pdk) => (
                                                        <option key={pdk.key} value={pdk.key} disabled={!pdk.gds_ready}>
                                                            {pdk.key} {pdk.gds_ready ? 'ready' : 'not installed'}
                                                        </option>
                                                    ))}
                                                </select>
                                                <em className="hitl-opt-help">
                                                    {selectedPdk
                                                        ? `${selectedPdk.pdk} · ${selectedPdk.std_cell_library || 'standard cells'} · ${selectedPdk.gds_ready ? 'GDSII ready' : selectedPdk.reason}`
                                                        : 'PDKs are loaded from the VPS runtime.'}
                                                </em>
                                                {selectedPdk && selectedPdk.maturity && selectedPdk.maturity !== 'production' && (
                                                    <div style={{
                                                        marginTop: '0.4rem',
                                                        padding: '0.4rem 0.6rem',
                                                        borderRadius: '4px',
                                                        background: 'rgba(217, 119, 6, 0.1)',
                                                        border: '1px solid rgba(217, 119, 6, 0.3)',
                                                        fontSize: '0.72rem',
                                                        color: '#d97706',
                                                    }}>
                                                        <strong>{PDK_MATURITY[selectedPdk.maturity]?.label || selectedPdk.maturity}:</strong>{' '}
                                                        {PDK_MATURITY[selectedPdk.maturity]?.warning || 'Use sky130 for the most mature OSS layout candidate flow.'}
                                                    </div>
                                                )}
                                            </label>
                                        </div>
                                    </div>
                                )}

                                <button
                                    className="hitl-stage-toggle-btn"
                                    onClick={() => setShowStageToggles(!showStageToggles)}
                                >
                                    {showStageToggles ? 'Hide stage customization' : 'Customize skipped stages'}
                                </button>

                                {showStageToggles && (
                                    <div className="hitl-stage-toggles">
                                        {PIPELINE_STAGES.map(stage => {
                                            const mandatory = MANDATORY_STAGES.has(stage);
                                            const skipped = skipStages.has(stage);
                                            return (
                                                <button
                                                    key={stage}
                                                    className={`hitl-stage-chip${skipped ? ' hitl-stage-chip--off' : ' hitl-stage-chip--on'}${mandatory ? ' hitl-stage-chip--locked' : ''}`}
                                                    disabled={mandatory}
                                                    onClick={() => {
                                                        if (mandatory) return;
                                                        setSkipStages(prev => {
                                                            const next = new Set(prev);
                                                            if (next.has(stage)) {
                                                                next.delete(stage);
                                                            } else {
                                                                next.add(stage);
                                                            }
                                                            return next;
                                                        });
                                                    }}
                                                >
                                                    {mandatory && <span className="hitl-stage-lock">&#x1f512;</span>}
                                                    {STAGE_LABELS[stage] || stage}
                                                </button>
                                            );
                                        })}
                                    </div>
                                )}

                                {error && <div className="hitl-error">{error}</div>}

                                <button
                                    className="hitl-launch-btn"
                                    onClick={handleLaunch}
                                    disabled={!prompt.trim()}
                                >
                                    Launch Build with Approval Gates
                                    <ArrowRight size={16} />
                                </button>
                            </section>

                            <aside className="hitl-briefing-card">
                                <div className="hitl-section-head">
                                    <div>
                                        <span className="hitl-section-label">Execution Brief</span>
                                        <h2 className="hitl-section-title">Review behavior</h2>
                                    </div>
                                </div>

                                <div className="hitl-briefing-list">
                                    {[
                                        ['Approval gates', 'Stage-complete outputs wait for your signoff before the pipeline proceeds.'],
                                        ['Artifact visibility', 'You can inspect summaries, warnings, and generated files at each checkpoint.'],
                                        ['Operator recovery', 'Reject a stage with feedback to redirect the system without restarting from scratch.'],
                                    ].map(([title, body], index) => (
                                        <div key={title} className="hitl-briefing-item">
                                            <span className="hitl-briefing-index">0{index + 1}</span>
                                            <div>
                                                <h3>{title}</h3>
                                                <p>{body}</p>
                                            </div>
                                        </div>
                                    ))}
                                </div>

                                <div className="hitl-quickstart-block">
                                    <div className="hitl-quickstart-head">
                                        <Waypoints size={16} />
                                        <span>Best for</span>
                                    </div>
                                    <ul className="hitl-quickstart-list">
                                        <li>critical architectures that need human checkpoints</li>
                                        <li>debugging pipeline regressions with explicit approvals</li>
                                        <li>demonstrating trustworthy agent behavior to collaborators</li>
                                    </ul>
                                </div>
                            </aside>
                        </div>
                    </div>
                </div>
            )}

            {/* ── BUILDING PHASE ── */}
            {phase === 'building' && (
                <div className="hitl-build-layout">
                    <header className="hitl-topbar">
                        <div className="hitl-topbar-left">
                            <span className="hitl-topbar-dot" />
                            <span className="hitl-topbar-name">{designName}</span>
                        </div>
                        <div className="hitl-topbar-right">
                            <span className="hitl-topbar-step">Step {stepNum} of {TOTAL}</span>
                            <button className="hitl-topbar-cancel" onClick={handleCancel}>
                                Cancel
                            </button>
                        </div>
                    </header>

                    {milestoneToast && (
                        <div className="hitl-milestone-toast" onClick={() => setMilestoneToast(null)}>
                            <span className="hitl-milestone-toast-icon">❖</span>
                            <div className="hitl-milestone-toast-body">
                                <span className="hitl-milestone-toast-title">{milestoneToast.title}</span>
                                <span className="hitl-milestone-toast-msg">{milestoneToast.msg}</span>
                            </div>
                            <button className="hitl-milestone-toast-close">×</button>
                        </div>
                    )}

                    <div className="hitl-build-body">
                        <StageProgressBar
                            currentStage={currentStage}
                            completedStages={completedStages}
                            failedStage={failedStage}
                            waitingForApproval={waitingForApproval}
                            skippedStages={skipStages}
                        />
                        <div className="hitl-main">
                            {stallWarning && (
                                <div className="hitl-stall-banner">
                                    <div className="hitl-stall-body">
                                        <span className="hitl-stall-icon">!</span>
                                        <span className="hitl-stall-msg">{stallWarning}</span>
                                    </div>
                                    <div className="hitl-stall-actions">
                                        <button className="hitl-stall-cancel-btn" onClick={handleCancel}>Cancel Build</button>
                                        <button className="hitl-stall-dismiss-btn" onClick={() => setStallWarning(null)}>Dismiss</button>
                                    </div>
                                </div>
                            )}
                            <ActivityFeed events={events} thinkingData={thinkingData} />
                            {approvalData && (
                                <ApprovalCard
                                    data={approvalData}
                                    designName={designName}
                                    jobId={jobId}
                                    onApprove={handleApprove}
                                    onReject={handleReject}
                                    isSubmitting={isSubmitting}
                                />
                            )}
                            {elaborationData && (
                                <ElaborationCard
                                    options={elaborationData.options}
                                    message={elaborationData.message}
                                    onSelect={handleElaborate}
                                    isSubmitting={isSubmitting}
                                />
                            )}
                        </div>
                    </div>

                    <footer className="hitl-bottombar">
                        <span className="hitl-bottombar-msg">
                            {thinkingData && <span className="hitl-thinking-pulse" />}
                            {waitingForApproval
                                ? 'Your review is needed — inspect the stage output above'
                                : thinkingData
                                    ? thinkingData.message
                                    : (STAGE_ENCOURAGEMENTS[currentStage] || 'Building autonomously…')}
                            {' · '}{pct}% complete
                            {estMinutes > 0 ? ` · ~${estMinutes} min left` : ''}
                        </span>
                        <div className="hitl-bottombar-progress">
                            <div className="hitl-bottombar-track">
                                <div className="hitl-bottombar-fill" style={{ width: `${pct}%` }} />
                            </div>
                            <span className="hitl-bottombar-pct">{pct}%</span>
                        </div>
                    </footer>
                </div>
            )}

            {/* ── DONE PHASE ── */}
            {phase === 'done' && (
                <div className="hitl-done-screen">
                    <div className={`hitl-done-card ${jobStatus === 'done' ? 'hitl-done-success' : 'hitl-done-fail'}`}>
                        {jobStatus === 'done' && (
                            <>
                                <h2>Chip Build Complete</h2>
                                <p className="hitl-done-design">{designName}</p>
                                {result && (
                                    <div className="hitl-done-details">
                                        {result.strategy && (
                                            <div className="hitl-done-detail">Strategy: {result.strategy}</div>
                                        )}
                                        {result.coverage && typeof result.coverage === 'object' && (
                                            <div className="hitl-done-detail">
                                                Coverage: {result.coverage.line_pct || 'N/A'}% line
                                            </div>
                                        )}
                                        {result.metrics && (
                                            <div className="hitl-done-detail">
                                                Gates: {result.metrics.gate_count || 'N/A'} · Area: {result.metrics.area || 'N/A'}
                                            </div>
                                        )}
                                    </div>
                                )}
                                <button className="hitl-reset-btn" onClick={handleReset}>
                                    ← Build Another Chip
                                </button>
                                {jobId && (
                                    <div className="hitl-report-downloads">
                                        <span className="hitl-report-label">Download Report:</span>
                                        <a
                                            className="hitl-report-btn"
                                            href={`${API_BASE}/report/${jobId}/full.pdf`}
                                            download
                                        >
                                            ↓ PDF
                                        </a>
                                        <a
                                            className="hitl-report-btn"
                                            href={`${API_BASE}/report/${jobId}/full.docx`}
                                            download
                                        >
                                            ↓ DOCX
                                        </a>
                                    </div>
                                )}
                            </>
                        )}

                        {jobStatus !== 'done' && (
                            <div className="hitl-fail-redesign">
                                <div className="hitl-fail-heading">
                                    <span className="hitl-fail-heading-dot" />
                                    <h2 className="hitl-fail-heading-text">
                                        Build stopped at {STAGE_LABELS[currentStage] || currentStage.replace(/_/g, ' ')}
                                    </h2>
                                </div>

                                <p className="hitl-fail-design-name">{designName}</p>

                                {result?.failure_explanation && (
                                    <div className="hitl-fail-explanation">
                                        <p>{result.failure_explanation}</p>
                                    </div>
                                )}

                                <div className="hitl-fail-stages">
                                    <span className="hitl-fail-section-label">Pipeline progress</span>
                                    <div className="hitl-fail-stage-chips">
                                        {PIPELINE_STAGES.map(stage => {
                                            const isCompleted = completedStages.has(stage);
                                            const isFailed = stage === currentStage && jobStatus !== 'done';
                                            const isSkipped = skipStages.has(stage);
                                            let chipClass = 'hitl-fail-chip--pending';
                                            let icon = '';
                                            if (isCompleted) { chipClass = 'hitl-fail-chip--done'; icon = '✓'; }
                                            else if (isFailed) { chipClass = 'hitl-fail-chip--stopped'; icon = '●'; }
                                            else if (isSkipped) { chipClass = 'hitl-fail-chip--skipped'; icon = '—'; }
                                            return (
                                                <span key={stage} className={`hitl-fail-chip ${chipClass}`}>
                                                    {icon && <span className="hitl-fail-chip-icon">{icon}</span>}
                                                    {STAGE_LABELS[stage] || stage.replace(/_/g, ' ')}
                                                </span>
                                            );
                                        })}
                                    </div>
                                </div>

                                {result?.error && (
                                    <div className="hitl-fail-error-brief">
                                        <span className="hitl-fail-error-label">Error</span>
                                        <p className="hitl-fail-error-msg">{String(result.error).slice(0, 300)}</p>
                                    </div>
                                )}

                                {partialArtifacts.length > 0 && (() => {
                                    const groups: Record<string, typeof partialArtifacts> = {};
                                    partialArtifacts.forEach(a => {
                                        const key = a.type || 'other';
                                        (groups[key] = groups[key] || []).push(a);
                                    });
                                    return (
                                        <div className="hitl-fail-artifacts-grouped">
                                            <span className="hitl-fail-section-label">
                                                Recovered artifacts ({partialArtifacts.length} files)
                                            </span>
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
                                                                ↓
                                                            </a>
                                                        </div>
                                                    ))}
                                                </div>
                                            ))}
                                        </div>
                                    );
                                })()}

                                {result && (
                                    <div className="hitl-fail-stats">
                                        {result.build_time_s != null && (
                                            <span className="hitl-fail-stat">
                                                Duration: {Math.round(result.build_time_s / 60)} min
                                            </span>
                                        )}
                                        {result.total_steps != null && (
                                            <span className="hitl-fail-stat">Steps: {result.total_steps}</span>
                                        )}
                                        {result.strategy && (
                                            <span className="hitl-fail-stat">Strategy: {result.strategy}</span>
                                        )}
                                    </div>
                                )}

                                {result?.failure_suggestion && (
                                    <div className="hitl-fail-suggestion">
                                        <span className="hitl-fail-suggestion-label">What to try next</span>
                                        <p>{result.failure_suggestion}</p>
                                    </div>
                                )}

                                <div className="hitl-fail-actions">
                                    <button className="hitl-fail-btn-primary" onClick={() => {
                                        setPhase('prompt');
                                        setEvents([]);
                                        setResult(null);
                                        setJobId('');
                                        setJobStatus('queued');
                                        setError('');
                                        setCurrentStage('INIT');
                                        setCompletedStages(new Set());
                                        setFailedStage(undefined);
                                        setWaitingForApproval(false);
                                        setApprovalData(null);
                                        setThinkingData(null);
                                        setPartialArtifacts([]);
                                        setShowFullLog(false);
                                    }}>
                                        Try Again
                                    </button>
                                    <button className="hitl-fail-btn-ghost" onClick={handleReset}>
                                        Start New Design
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>

                    <div className="hitl-done-log-section">
                        <button
                            className="hitl-done-log-toggle"
                            onClick={() => setShowFullLog(!showFullLog)}
                        >
                            {showFullLog ? '▼ Hide Full Log' : '▶ Show Full Log'} ({events.length} events)
                        </button>
                        {showFullLog && (
                            <div className="hitl-done-log">
                                <ActivityFeed events={events} />
                            </div>
                        )}
                    </div>
                </div>
            )}
            <BillingModal
                isOpen={showBillingModal}
                onClose={() => setShowBillingModal(false)}
                onKeySaved={() => {
                    setShowBillingModal(false);
                    setProfile((prev) => ({ ...(prev || {}), has_byok_key: true }));
                    if (pendingLaunchAfterByokRef.current) {
                        pendingLaunchAfterByokRef.current = false;
                        void handleLaunch();
                    }
                }}
            />
        </div>
    );
};
