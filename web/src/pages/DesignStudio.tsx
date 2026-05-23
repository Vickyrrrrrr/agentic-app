// @ts-nocheck
import { useEffect, useMemo, useRef, useState } from 'react';
import '../studio-3pane.css';
import { motion } from 'framer-motion';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import Editor from '@monaco-editor/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
    ArrowUp,
    Cpu,
    FileText,
    Folder,
    PanelLeftClose,
    PanelLeftOpen,
    Terminal,
    AlertCircle
} from 'lucide-react';
import { BillingModal } from '../components/BillingModal';
import { api, API_BASE, getSseHeaders } from '../api';
import { isNetworkError, toUserError } from '../utils/errorFormatter';

type Phase = 'idle' | 'building' | 'done';
type ModelChoice = 'infinite' | 'byok';
type BillingMode = 'agentic' | 'byok';
type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'cancelling';
type InspectorTab = 'overview' | 'files';
type FlowChoice = 'sky130_oss_executable' | 'sky130_oss_experimental_complete';

interface ChatMessage {
    role: 'user' | 'assistant';
    content: string;
    tone?: 'normal' | 'success' | 'error';
}

interface BuildEvent {
    type: string;
    state: string;
    message: string;
    step: number;
    total_steps: number;
    timestamp: number | string;
    status?: string;
    agent_name?: string;
    content?: string;
    changes?: Array<Record<string, unknown>>;
    target?: string;
    category?: string;
    original_request?: string;
    constraint?: string;
    chosen_substitute?: string;
    stage_name?: string;
    summary?: string;
    thought_type?: string;
    readiness_level?: string;
    signoff_blockers?: string[];
    node_contract?: Record<string, unknown>;
}

interface StageSchemaItem {
    state: string;
    label: string;
    icon: string;
    description?: string;
    capability?: string;
}

interface Artifact {
    name: string;
    size?: number;
    type?: string;
}

interface PdkOption {
    key: string;
    gds_ready?: boolean;
}

const FALLBACK_STAGES: StageSchemaItem[] = [
    { state: 'INIT', label: 'Init', icon: '01' },
    { state: 'SPEC', label: 'Spec', icon: '02' },
    { state: 'SPEC_VALIDATE', label: 'Validate', icon: '03' },
    { state: 'HIERARCHY_EXPAND', label: 'Hierarchy', icon: '04' },
    { state: 'FEASIBILITY_CHECK', label: 'Feasibility', icon: '05' },
    { state: 'VERIFICATION_PLAN', label: 'Verify Plan', icon: '06' },
    { state: 'RTL_GEN', label: 'RTL', icon: '07' },
    { state: 'RTL_FIX', label: 'RTL Fix', icon: '08' },
    { state: 'CDC_ANALYZE', label: 'CDC', icon: '09' },
    { state: 'VERIFICATION', label: 'Sim', icon: '10' },
    { state: 'FORMAL_VERIFY', label: 'Formal', icon: '11' },
    { state: 'COVERAGE_CHECK', label: 'Coverage', icon: '12' },
    { state: 'REGRESSION', label: 'Regression', icon: '13' },
    { state: 'SDC_GEN', label: 'SDC', icon: '14' },
    { state: 'SYNTHESIS', label: 'Synth', icon: '15' },
    { state: 'FLOORPLAN', label: 'Floorplan', icon: '16' },
    { state: 'HARDENING', label: 'Layout', icon: '17' },
    { state: 'CONVERGENCE_REVIEW', label: 'Converge', icon: '18' },
    { state: 'ECO_PATCH', label: 'ECO', icon: '19' },
    { state: 'POWER_ANALYSIS', label: 'Power', icon: '20' },
    { state: 'TIMING_ANALYSIS', label: 'Timing', icon: '21' },
    { state: 'PHYSICAL_VERIFY', label: 'PV', icon: '22' },
    { state: 'SIGNOFF', label: 'Signoff', icon: '23' },
    { state: 'IP_PACKAGE', label: 'Package', icon: '24' },
];

const FALLBACK_BLOCKED_EXTENSIONS: StageSchemaItem[] = [
    { state: 'DFT_SCAN', label: 'DFT Scan', icon: 'C1', capability: 'commercial_dft' },
    { state: 'DFT_ATPG', label: 'ATPG', icon: 'C2', capability: 'commercial_dft' },
    { state: 'MBIST', label: 'MBIST', icon: 'C3', capability: 'commercial_dft' },
    { state: 'GLS_SIMULATION', label: 'SDF GLS', icon: 'O1', capability: 'optional_oss' },
    { state: 'POST_LAYOUT_SPICE', label: 'Scoped SPICE', icon: 'O2', capability: 'optional_oss' },
];

const STAGE_NOTES: Record<string, string> = {
    INIT: 'Preparing the workspace, PDK profile, model routing, and build ledger.',
    SPEC: 'Turning the request into an implementation spec with clocks, resets, interfaces, and forbidden assumptions made explicit.',
    SPEC_VALIDATE: 'Checking that the architecture is implementable as synthesizable digital logic before code generation.',
    HIERARCHY_EXPAND: 'Breaking the chip into blocks, interfaces, verification targets, and physical-design responsibilities.',
    FEASIBILITY_CHECK: 'Checking the node contract, PDK collateral, memory macro assumptions, target frequency, and signoff blockers.',
    VERIFICATION_PLAN: 'Writing the verification intent before RTL so the design has measurable pass/fail evidence.',
    RTL_GEN: 'Generating synthesizable RTL with clean reset, no internal tri-states, and tool-friendly structure.',
    RTL_FIX: 'Treating parser, lint, width, and elaboration issues as gates before simulation.',
    CDC_ANALYZE: 'Looking for unsafe clock-domain crossings, reset release hazards, and missing synchronizers.',
    VERIFICATION: 'Running simulation against the planned scenarios and checking waveform-level behavior.',
    FORMAL_VERIFY: 'Proving safety properties such as protocol legality, FIFO bounds, and reset convergence.',
    COVERAGE_CHECK: 'Checking that branches, FSM states, and important scenarios were actually exercised.',
    REGRESSION: 'Re-running the suite after fixes so old behavior does not quietly break.',
    SDC_GEN: 'Creating timing constraints that match the actual clocks, IO assumptions, and generated clocks.',
    SYNTHESIS: 'Mapping RTL to standard cells and reading area, timing, unmapped logic, and constraint warnings.',
    DFT_SCAN: 'Adding scan/test hooks where the flow supports it and flagging unsupported DFT collateral.',
    DFT_ATPG: 'Checking whether test patterns and controllability evidence exist before claiming production readiness.',
    MBIST: 'Checking memory-test strategy for any inferred or supplied memory macros.',
    GLS_SIMULATION: 'Running gate-level simulation where netlists and libraries are available.',
    FLOORPLAN: 'Choosing die/core geometry, IO placement, utilization, and macro placement assumptions.',
    HARDENING: 'Running placement, routing, extraction, and GDS generation through the selected physical flow.',
    CONVERGENCE_REVIEW: 'Reading timing, congestion, DRC, LVS, and power evidence before deciding to tune or ECO.',
    ECO_PATCH: 'Applying a bounded implementation fix without changing the approved architecture.',
    POWER_ANALYSIS: 'Checking switching/power estimates, rail assumptions, and available EM/IR evidence.',
    TIMING_ANALYSIS: 'Checking setup/hold timing across corners that the selected flow can actually analyze.',
    PHYSICAL_VERIFY: 'Running DRC/LVS/antenna-style checks and treating violations as signoff blockers.',
    POST_LAYOUT_SPICE: 'Checking post-layout parasitic risk for critical paths or analog/macro interfaces when supported.',
    SIGNOFF: 'Only calling the chip ready when required evidence exists for RTL, verification, timing, physical checks, and packaging.',
    IP_PACKAGE: 'Packaging RTL, constraints, reports, layout outputs, and the final signoff/readiness statement.',
};

type StageReview = {
    focus: string;
    gate: string;
    checks: string[];
};

const DEFAULT_REVIEW: StageReview = {
    focus: 'Keep the build moving, but do not promote the chip without evidence.',
    gate: 'Advance only when the current artifact, report, or exception is recorded.',
    checks: ['Current stage log captured', 'Blocking assumptions visible', 'Next action selected'],
};

const STAGE_REVIEWS: Record<string, StageReview> = {
    INIT: {
        focus: 'Establish the execution context before any silicon claim is made.',
        gate: 'PDK profile, tool mode, design name, and model route are known.',
        checks: ['Workspace ready', 'PDK/tool mode selected', 'Build ledger started'],
    },
    SPEC: {
        focus: 'Convert vague product language into a buildable digital spec.',
        gate: 'Clock/reset, bus/registers, IO directions, and hard-macro assumptions are explicit.',
        checks: ['Interfaces named', 'Reset strategy named', 'Unsupported analog/macro requests surfaced'],
    },
    FEASIBILITY_CHECK: {
        focus: 'Act like a tapeout reviewer: check the node contract before writing more RTL.',
        gate: 'PDK/tool collateral, macro availability, frequency envelope, and signoff blockers are logged.',
        checks: ['Node contract reviewed', 'Unsupported collateral marked', 'Closest feasible substitute chosen'],
    },
    VERIFICATION_PLAN: {
        focus: 'Define what must be proven before the design is allowed to look complete.',
        gate: 'Testbench, assertions, coverage goals, and negative cases are planned.',
        checks: ['Self-checking tests planned', 'Formal properties identified', 'Coverage target recorded'],
    },
    RTL_GEN: {
        focus: 'Generate RTL that synthesis and verification tools can survive.',
        gate: 'No unsized ambiguity, hidden latches, internal tri-states, or unmanaged reset behavior.',
        checks: ['Synchronous structure', 'Register map implemented', 'Tool-friendly ports'],
    },
    RTL_FIX: {
        focus: 'Treat lint and elaboration as first-class engineering feedback.',
        gate: 'Parser, width, hierarchy, and basic lint issues are clean or waived with reason.',
        checks: ['Syntax clean', 'Width issues fixed', 'Waivers visible'],
    },
    CDC_ANALYZE: {
        focus: 'Protect the chip from metastability and reset-release failures.',
        gate: 'Every crossing has a synchronizer, async FIFO, handshake, or explicit waiver.',
        checks: ['Clock domains listed', 'Reset crossings checked', 'Synchronizers identified'],
    },
    VERIFICATION: {
        focus: 'Exercise behavior, not just compile artifacts.',
        gate: 'Simulation passes with self-checking outcomes and useful failure context.',
        checks: ['Tests pass', 'Failures triaged', 'Wave/log evidence available'],
    },
    FORMAL_VERIFY: {
        focus: 'Use formal where simulation can miss protocol and safety bugs.',
        gate: 'Core safety properties prove or bounded failures are fixed/waived.',
        checks: ['Properties compiled', 'Proof result captured', 'Counterexamples triaged'],
    },
    COVERAGE_CHECK: {
        focus: 'Do not confuse a passing test with a tested design.',
        gate: 'Unhit branches, states, registers, and protocol scenarios are understood.',
        checks: ['Coverage report read', 'Holes categorized', 'Regression target updated'],
    },
    SYNTHESIS: {
        focus: 'Read synthesis like a silicon engineer, not like a code generator.',
        gate: 'No unmapped logic, fatal constraints, impossible timing, or unreviewed area spikes.',
        checks: ['Netlist produced', 'Warnings reviewed', 'Timing/area snapshot captured'],
    },
    GLS_SIMULATION: {
        focus: 'Catch netlist, library, reset, and X-propagation problems before layout confidence.',
        gate: 'Gate-level sim passes or unsupported collateral is recorded as a readiness blocker.',
        checks: ['Netlist available', 'Libraries available', 'GLS result captured'],
    },
    CONVERGENCE_REVIEW: {
        focus: 'Read the run like a signoff meeting: timing, congestion, DRC/LVS, and power together.',
        gate: 'Choose continue, tune, ECO, or fail based on evidence, not optimism.',
        checks: ['WNS/TNS reviewed', 'DRC/LVS status read', 'Power/congestion risks noted'],
    },
    ECO_PATCH: {
        focus: 'Make the smallest implementation change that fixes the observed blocker.',
        gate: 'ECO does not invalidate spec, verification intent, or constraints.',
        checks: ['Patch scoped', 'Regression needed', 'New risk logged'],
    },
    POWER_ANALYSIS: {
        focus: 'Check whether power numbers are meaningful for the node and activity assumptions.',
        gate: 'Activity source, rail assumptions, and EM/IR availability are documented.',
        checks: ['Activity model known', 'Power report read', 'Rail/EMIR caveats logged'],
    },
    TIMING_ANALYSIS: {
        focus: 'Close setup and hold against the corners the available flow can prove.',
        gate: 'STA evidence exists, and missing MMMC/corner support is a signoff blocker.',
        checks: ['Setup checked', 'Hold checked', 'Corner support recorded'],
    },
    PHYSICAL_VERIFY: {
        focus: 'Do not call layout fabrication-ready until physical rules agree.',
        gate: 'DRC, LVS, antenna, density/fill, and extraction evidence are clean or blocked.',
        checks: ['DRC status', 'LVS status', 'Antenna/density status'],
    },
    SIGNOFF: {
        focus: 'Separate demo-ready from fabrication-ready with a hard evidence checklist.',
        gate: 'RTL, verification, LEC/GLS, STA, DRC/LVS, power, package, and waivers are all accounted for.',
        checks: ['Evidence complete', 'Waivers explicit', 'Readiness level stated'],
    },
};

const EXAMPLES = [
    'SPI master controller with configurable divider and loopback testbench',
    '8-bit RISC CPU with interrupt support and memory-mapped IO',
    'AXI4-lite timer peripheral with formal checks and GDSII output',
];

const CASUAL_RE = /^(hi+|hello+|hey+|yo+|sup|thanks|thank you|ok|okay|test)$/i;
const CONFIRM_RE = /^(yes|yep|yeah|sure|ok|okay|go ahead|proceed|build it|run it|start it|make it)$/i;
const HELP_RE = /\b(help|what can you do|capabilit|possible|not possible|can you|how do i|suggest|prompt)\b/i;
const HARDWARE_RE = /\b(chip|rtl|verilog|systemverilog|vlsi|asic|fpga|pdk|sky130|gf180|gds|layout|synthesis|synthesize|timer|uart|spi|i2c|axi|apb|wishbone|fifo|ram|rom|sram|cpu|risc|risc-v|mcu|microcontroller|alu|dma|pwm|watchdog|aes|sha|trng|gpio|counter|fsm|pll|adc|dac|register|bus|peripheral|accelerator|core)\b/i;
const BUILD_ACTION_RE = /\b(build|create|design|generate|make|implement|synthesize|harden|layout|verify)\b/i;
const SPEC_DETAIL_RE = /\b(with|using|include|support|clock|reset|register|interrupt|memory[- ]mapped|bit|width|mhz|khz|formal|testbench|coverage|gdsii|openlane)\b/i;

type PromptDraft = {
    spec: string;
    summary: string;
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

function normalizePrompt(text: string): string {
    return text.replace(/\s+/g, ' ').trim();
}

function wordCount(text: string): number {
    const words = normalizePrompt(text).split(/\s+/).filter(Boolean);
    return words.length;
}

function isCasualPrompt(text: string): boolean {
    const normalized = normalizePrompt(text);
    return CASUAL_RE.test(normalized) || (wordCount(normalized) <= 3 && !HARDWARE_RE.test(normalized) && !/[?]/.test(normalized));
}

function isConfirmationPrompt(text: string): boolean {
    return CONFIRM_RE.test(normalizePrompt(text));
}

function hasHardwareIntent(text: string): boolean {
    return HARDWARE_RE.test(text);
}

function isBuildReadyPrompt(text: string): boolean {
    const normalized = normalizePrompt(text);
    if (!normalized || isCasualPrompt(normalized) || !hasHardwareIntent(normalized)) return false;
    return (BUILD_ACTION_RE.test(normalized) && (SPEC_DETAIL_RE.test(normalized) || wordCount(normalized) >= 7)) || wordCount(normalized) >= 10;
}

function makeDraftFromPrompt(text: string, pdkProfile: string): PromptDraft {
    const normalized = normalizePrompt(text);
    const lower = normalized.toLowerCase();
    let spec = normalized;

    if (!isBuildReadyPrompt(normalized)) {
        if (/\bmcu|microcontroller|risc-v|risc\b/i.test(lower)) {
            spec = 'Design a synthesizable microcontroller subsystem with a simple CPU core, memory-mapped GPIO, UART, timer, interrupt controller, APB-style register bus, reset synchronization, and self-checking testbench.';
        } else if (/\bspi\b/i.test(lower)) {
            spec = 'Design a synthesizable SPI master controller with configurable clock divider, CPOL/CPHA modes, TX/RX FIFOs, memory-mapped control/status registers, interrupt output, loopback testbench, and formal checks for transaction sequencing.';
        } else if (/\buart\b/i.test(lower)) {
            spec = 'Design a synthesizable UART peripheral with configurable baud divider, TX/RX FIFOs, parity option, memory-mapped registers, interrupt output, self-checking testbench, and formal checks for FIFO safety.';
        } else if (/\baes|sha|crypto|secure\b/i.test(lower)) {
            spec = 'Design a synthesizable secure peripheral with memory-mapped control/status registers, AES/SHA-style processing hooks, key-valid handshakes, interrupt status, zeroization controls, and formal checks for register and handshake behavior.';
        } else if (/\badc|dac|pll|analog|trng\b/i.test(lower)) {
            spec = 'Design a synthesizable digital control/status wrapper for the requested analog or entropy function, exposing macro-facing ports, configuration registers, valid/error status, and a behavioral testbench. Do not claim custom analog layout generation; require a supplied macro for the physical analog block.';
        } else {
            spec = 'Design a synthesizable digital IP block with a memory-mapped register interface, clean clock/reset strategy, explicit input/output ports, self-checking testbench, formal safety checks, and implementation through the selected PDK flow.';
        }
    }

    const guardrails = `Target ${pdkProfile}. Keep the implementation synthesizable Verilog/SystemVerilog. Avoid internal tri-states; split bidirectional intent into input, output, and output-enable signals. Use macro-facing wrappers for analog blocks, PLLs, TRNGs, ADC/DACs, or large memories, and log every feasibility-driven spec change before continuing.`;
    const finalSpec = SPEC_DETAIL_RE.test(spec) || spec.length > 120 ? `${spec} ${guardrails}` : `${spec}. ${guardrails}`;
    return {
        spec: finalSpec,
        summary: isBuildReadyPrompt(normalized)
            ? 'I converted your request into a PDK-aware build prompt with explicit feasibility guardrails.'
            : 'Your request needs one more silicon-shaped spec layer before the pipeline can run safely.',
    };
}

function makeAdvisorReply(text: string, pdkProfile: string, draft?: PromptDraft): string {
    if (draft) {
        return [
            `**Draft chip prompt ready**`,
            '',
            draft.summary,
            '',
            `I can build the closest feasible digital implementation for **${pdkProfile}**. If the request includes analog, PLL, TRNG, or large memory behavior, AgentIC will use digital wrappers or macro interfaces and tell you exactly what changed.`,
            '',
            `Approve this draft to start the build, or tell me what to refine.`,
        ].join('\n');
    }

    if (isCasualPrompt(text)) {
        return 'Hey, I am here. Tell me what digital block or chip you want, and I will help turn it into a VLSI-aware prompt before starting the pipeline. AgentIC can generate synthesizable RTL, verification collateral, synthesis/layout artifacts, and reports when the selected PDK flow is available.';
    }

    if (HELP_RE.test(text) || /[?]/.test(text)) {
        return [
            `AgentIC is best at **synthesizable digital silicon**: RTL, testbenches, formal checks, synthesis, OpenLane-style physical flow, reports, and packaging.`,
            '',
            `It will not silently invent custom analog/layout macros. For ADCs, DACs, PLLs, TRNGs, SRAMs, or other hard macros, it should generate a digital wrapper or macro-facing interface, then explain the substitution.`,
            '',
            `A strong prompt names the block, interfaces, clock/reset, registers, widths, verification expectations, target PDK, and whether GDSII is required.`,
        ].join('\n');
    }

    return 'I can help shape that into a chip build. Give me the block type, interface, key registers or data widths, clock/reset assumptions, and whether you want RTL-only or a physical-flow run.';
}

function describeSpecChanges(event: BuildEvent): string {
    const changes = event.changes || [];
    if (!changes.length) return event.message || 'AgentIC changed the spec to fit the selected PDK/tool constraints.';
    return changes
        .slice(0, 6)
        .map((change, index) => {
            const original = String(change.original_request || change.target || `change ${index + 1}`);
            const constraint = String(change.constraint || change.category || 'PDK/tool constraint');
            const substitute = String(change.chosen_substitute || change.replacement || change.user_visible_explanation || 'closest feasible implementation');
            return `- **${original}**: ${constraint}; using ${substitute}.`;
        })
        .join('\n');
}

function notifyBuild(title: string, body: string) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
        new Notification(title, { body });
    } catch {
        // Notification support differs between browsers and Electron shells.
    }
}

function getStageReview(stage: string, event?: BuildEvent, rtlOnly = false): StageReview {
    const base = STAGE_REVIEWS[stage] || DEFAULT_REVIEW;
    const blockers = event?.signoff_blockers?.filter(Boolean).slice(0, 3) || [];
    const readiness = event?.readiness_level ? [`Readiness: ${event.readiness_level}`] : [];
    const mode = rtlOnly && ['FLOORPLAN', 'HARDENING', 'PHYSICAL_VERIFY', 'SIGNOFF'].includes(stage)
        ? ['Physical-flow evidence is disabled in RTL-only mode']
        : [];
    return {
        focus: base.focus,
        gate: blockers.length ? `Blocked until: ${blockers.join('; ')}` : base.gate,
        checks: [...readiness, ...mode, ...base.checks].slice(0, 5),
    };
}

function formatStageAnnouncement(label: string, review: StageReview, message?: string): string {
    const checks = review.checks.map((check) => `- ${check}`).join('\n');
    return [
        `**${label}**`,
        '',
        `Senior VLSI focus: ${review.focus}`,
        '',
        `Gate before advance: ${review.gate}`,
        checks ? ['', checks].join('\n') : '',
        message ? ['', `Latest event: ${message}`].join('\n') : '',
    ].filter(Boolean).join('\n');
}

function formatBytes(size?: number): string {
    if (!size) return 'ready';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function readByokConfig(): Record<string, { model?: string; api_key?: string; base_url?: string }> | null {
    const raw = localStorage.getItem('agentic_byok_key');
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed : null;
    } catch {
        return null;
    }
}

function getByokModelLabel(): string {
    const config = readByokConfig();
    if (!config) return 'BYOK';
    const models = Object.values(config)
        .map((group) => group?.model?.trim())
        .filter(Boolean) as string[];
    const unique = Array.from(new Set(models));
    if (unique.length === 0) return 'BYOK';
    if (unique.length === 1) return unique[0];
    return `${unique[0]} + ${unique.length - 1}`;
}

function serializeByokConfig(): string | null {
    const config = readByokConfig();
    return config ? JSON.stringify(config) : null;
}

function isTextArtifact(name: string): boolean {
    return /\.(v|sv|sby|sdc|tcl|json|md|txt|log|rpt|csv|ys|cfg|lef|def)$/i.test(name);
}

function artifactLanguage(name: string): string {
    const lower = name.toLowerCase();
    if (/\.(v|sv|svh|vh)$/.test(lower)) return 'verilog';
    if (lower.endsWith('.json')) return 'json';
    if (lower.endsWith('.md')) return 'markdown';
    if (lower.endsWith('.tcl') || lower.endsWith('.sdc')) return 'tcl';
    if (lower.endsWith('.csv')) return 'csv';
    if (lower.endsWith('.ys') || lower.endsWith('.cfg')) return 'ini';
    return 'plaintext';
}

export const DesignStudio = () => {
    const [phase, setPhase] = useState<Phase>('idle');
    const [modelChoice, setModelChoice] = useState<ModelChoice>('infinite');
    // const [modelMenuOpen, setModelMenuOpen] = useState(false);
    const [prompt, setPrompt] = useState('');
    const [draftSpec, setDraftSpec] = useState('');
    const [draftSummary, setDraftSummary] = useState('');
    const [lastBuildPrompt, setLastBuildPrompt] = useState('');
    const [designName, setDesignName] = useState('');
    const [messages, setMessages] = useState<ChatMessage[]>([
        {
            role: 'assistant',
            content: 'AgentIC Studio',
        },
        {
            role: 'assistant',
            content: 'I can chat through VLSI intent, repair infeasible specs into the closest PDK-aware implementation, and only start the silicon pipeline when you approve a real chip build.',
        },
    ]);
    const [events, setEvents] = useState<BuildEvent[]>([]);
    const [thinking, setThinking] = useState('');
    const [isChatting, setIsChatting] = useState(false);
    const [jobId, setJobId] = useState('');
    const [jobStatus, setJobStatus] = useState<JobStatus>('queued');
    const [artifacts, setArtifacts] = useState<Artifact[]>([]);
    const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
    const [artifactPreview, setArtifactPreview] = useState('');
    const [error, setError] = useState('');
    const [stageSchema, setStageSchema] = useState<StageSchemaItem[]>([]);
    const [blockedExtensions, setBlockedExtensions] = useState<StageSchemaItem[]>(FALLBACK_BLOCKED_EXTENSIONS);
    const [pdkProfile, setPdkProfile] = useState('sky130');
    const [flowProfile, setFlowProfile] = useState<FlowChoice>('sky130_oss_executable');
    const [skipOpenlane, setSkipOpenlane] = useState(false);
    const [pdkOptions, setPdkOptions] = useState<PdkOption[]>([]);
    const [showBillingModal, setShowBillingModal] = useState(false);
    const [billingMode, setBillingMode] = useState<BillingMode>('byok');
    const [profile, setProfile] = useState<{ plan_type?: string; has_byok_key?: boolean } | null>(null);
    const [isWorkspaceCollapsed, setIsWorkspaceCollapsed] = useState(false);
    // const [inspectorTab, setInspectorTab] = useState<InspectorTab>('overview');
    // const [inspectorCollapsed, setInspectorCollapsed] = useState(true);
    // const [statusDropdownOpen, setStatusDropdownOpen] = useState(false);

    const scrollRef = useRef<HTMLDivElement | null>(null);
    const abortRef = useRef<AbortController | null>(null);
    const announcedStages = useRef<Set<string>>(new Set());
    const announcedDecisions = useRef<Set<string>>(new Set());
    const artifactFetchAt = useRef(0);
    const autoOpenedWorkspace = useRef(false);

    const byokLabel = getByokModelLabel();
    const hasByok = Boolean(profile?.has_byok_key) || Boolean(readByokConfig());
    const activeStages = stageSchema.length ? stageSchema : FALLBACK_STAGES;
    const currentEvent = [...events].reverse().find((event) => event.state && event.state !== 'UNKNOWN');
    const currentStage = currentEvent?.state || 'INIT';
    const currentStageIndex = Math.max(0, activeStages.findIndex((stage) => stage.state === currentStage));
    const currentStageLabel = activeStages.find((stage) => stage.state === currentStage)?.label || currentStage;
    const currentStageReview = getStageReview(currentStage, currentEvent, skipOpenlane);
    const liveStatusText = thinking || STAGE_NOTES[currentStage] || currentStageReview.focus || currentEvent?.message || 'AgentIC is working through the next chip-build step.';
    const progress = phase === 'done' && jobStatus === 'done'
        ? 100
        : phase === 'building'
            ? Math.min(98, Math.round(((currentStageIndex + 1) / Math.max(activeStages.length, 1)) * 100))
            : 0;
            
    const completedStages = useMemo(() => {
        const done = new Set<string>();
        events.forEach(e => {
            if (e.status === 'done' && e.state && e.state !== 'UNKNOWN') done.add(e.state);
        });
        // Also add all stages prior to current stage if we are advancing
        if (currentStageIndex > 0) {
            for (let i = 0; i < currentStageIndex; i++) {
                done.add(activeStages[i].state);
            }
        }
        return done;
    }, [events, currentStageIndex, activeStages]);

    const visibleArtifacts = useMemo(() => {
        const priority = ['.v', '.sv', '.sby', '.sdc', '.gds', '.def', '.lef', '.rpt', '.pdf', '.docx', '.json', '.log'];
        return [...artifacts].sort((a, b) => {
            const ap = priority.findIndex((ext) => a.name.toLowerCase().endsWith(ext));
            const bp = priority.findIndex((ext) => b.name.toLowerCase().endsWith(ext));
            return (ap === -1 ? 99 : ap) - (bp === -1 ? 99 : bp);
        });
    }, [artifacts]);

    const artifactSections = useMemo(() => {
        const sections = [
            { label: 'RTL', types: ['rtl'] },
            { label: 'Verification', types: ['formal', 'waveform'] },
            { label: 'Physical', types: ['layout', 'timing', 'constraints'] },
            { label: 'Reports', types: ['report', 'log', 'config', 'script', 'other'] },
        ];
        return sections
            .map((section) => ({
                ...section,
                files: visibleArtifacts.filter((artifact) => section.types.includes(artifact.type || 'other')),
            }))
            .filter((section) => section.files.length > 0);
    }, [visibleArtifacts]);

    const reasoningRows = useMemo(() => {
        const rows: Array<{ label: string; message: string }> = [];
        if (phase === 'building') {
            rows.push({
                label: 'Senior Gate',
                message: `${currentStageReview.focus} Gate: ${currentStageReview.gate}`,
            });
        }
        if (thinking) {
            rows.push({ label: 'Active', message: thinking });
        }
        [...events].reverse().forEach((event) => {
            if (rows.length >= 8) return;
            if (!event.message && event.type !== 'spec_reconciled') return;
            if (!['agent_thought', 'design_decision', 'spec_reconciled', 'stage_complete', 'error', 'checkpoint'].includes(event.type)) return;
            rows.push({
                label: event.type === 'spec_reconciled'
                    ? 'Spec Repair'
                    : event.type === 'design_decision'
                        ? 'Decision'
                        : event.type === 'agent_thought'
                            ? event.agent_name || 'Agent Thought'
                            : event.state || 'AgentIC',
                message: event.type === 'spec_reconciled'
                    ? describeSpecChanges(event).replace(/\*\*/g, '')
                    : event.content || event.message,
            });
        });
        return rows;
    }, [currentStageReview.focus, currentStageReview.gate, events, phase, thinking]);

    useEffect(() => {
        scrollRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }, [messages, thinking, events.length]);

    useEffect(() => {
        if (selectedArtifact && visibleArtifacts.some((artifact) => artifact.name === selectedArtifact.name)) {
            return;
        }
        setSelectedArtifact(visibleArtifacts[0] || null);
        setArtifactPreview('');
    }, [selectedArtifact, visibleArtifacts]);

    useEffect(() => {
        if (visibleArtifacts.length > 0 && !autoOpenedWorkspace.current) {
            autoOpenedWorkspace.current = true;
            
        }
    }, [visibleArtifacts.length]);

    useEffect(() => {
        if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
        Promise.allSettled([
            api.get('/profile'),
            api.get('/billing/status'),
            api.get('/pipeline/schema'),
            api.get('/pdks'),
        ]).then(([profileRes, billingRes, schemaRes, pdksRes]) => {
            const prof = profileRes.status === 'fulfilled' ? profileRes.value.data : null;
            const billing = billingRes.status === 'fulfilled' ? billingRes.value.data : null;
            if (prof && billing) setProfile({ ...prof, plan_type: billing.plan_type });
            else if (prof) setProfile(prof);
            if (schemaRes.status === 'fulfilled') {
                setStageSchema(schemaRes.value.data?.stages || []);
                setBlockedExtensions(schemaRes.value.data?.blocked_extensions || FALLBACK_BLOCKED_EXTENSIONS);
            }
            if (pdksRes.status === 'fulfilled') {
                const pdks: PdkOption[] = pdksRes.value.data?.pdks || [];
                const defaultPdk = pdksRes.value.data?.default || 'sky130';
                const readyDefault = pdks.find((pdk) => pdk.key === defaultPdk && pdk.gds_ready);
                const firstReady = pdks.find((pdk) => pdk.gds_ready);
                setPdkOptions(pdks);
                setPdkProfile(readyDefault?.key || firstReady?.key || defaultPdk);
                setSkipOpenlane(!readyDefault && !firstReady);
            }
        });

        // Resolve landing page carried prompts
        const initialPrompt = localStorage.getItem('agentic_studio_initial_prompt');
        const initialPdk = localStorage.getItem('agentic_studio_initial_pdk');
        if (initialPrompt) {
            const initialDraft = makeDraftFromPrompt(initialPrompt, initialPdk || pdkProfile);
            setPrompt(initialPrompt);
            setDraftSpec(initialDraft.spec);
            setDraftSummary(initialDraft.summary);
            setDesignName(slugify(initialDraft.spec));
            localStorage.removeItem('agentic_studio_initial_prompt');
        }
        if (initialPdk) {
            setPdkProfile(initialPdk);
            localStorage.removeItem('agentic_studio_initial_pdk');
        }

        return () => abortRef.current?.abort();
    }, []);

    useEffect(() => {
        if (!selectedArtifact || !designName) {
            return;
        }
        if (!isTextArtifact(selectedArtifact.name)) {
            window.setTimeout(() => setArtifactPreview('Binary artifact. Download it from the artifact list.'), 0);
            return;
        }
        api.get(`/build/artifacts/${designName}/${selectedArtifact.name}`, { responseType: 'text' })
            .then((res) => {
                const raw = typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2);
                setArtifactPreview(raw.length > 14000 ? `${raw.slice(0, 14000)}\n\n[Preview truncated]` : raw);
            })
            .catch(() => setArtifactPreview('Preview unavailable.'));
    }, [selectedArtifact, designName]);

    useEffect(() => {
        api.get('/pipeline/schema', { params: { flow_profile: flowProfile, pdk_profile: pdkProfile } })
            .then((res) => {
                setStageSchema(res.data?.stages || []);
                setBlockedExtensions(res.data?.blocked_extensions || FALLBACK_BLOCKED_EXTENSIONS);
            })
            .catch(() => {
                setStageSchema([]);
                setBlockedExtensions(FALLBACK_BLOCKED_EXTENSIONS);
            });
    }, [flowProfile, pdkProfile]);

    const addAssistant = (content: string, tone: ChatMessage['tone'] = 'normal') => {
        setMessages((previous) => [...previous, { role: 'assistant', content, tone }]);
    };

    const requestByokSetup = () => {
        setBillingMode('byok');
        setShowBillingModal(true);
    };

    const fetchArtifacts = async (targetDesign = designName, force = false) => {
        if (!targetDesign) return;
        const now = Date.now();
        if (!force && now - artifactFetchAt.current < 1400) return;
        artifactFetchAt.current = now;
        try {
            const res = await api.get(`/build/artifacts/${targetDesign}`);
            setArtifacts(Array.isArray(res.data?.artifacts) ? res.data.artifacts : []);
        } catch {
            // Artifacts are created incrementally; early misses are expected.
        }
    };

    const proposeDraft = (text: string, nextMessages?: ChatMessage[]) => {
        const draft = makeDraftFromPrompt(text, pdkProfile);
        setDraftSpec(draft.spec);
        setDraftSummary(draft.summary);
        setLastBuildPrompt(draft.spec);
        if (!designName.trim()) setDesignName(slugify(draft.spec));
        setMessages((previous) => [
            ...(nextMessages || previous),
            { role: 'assistant', content: makeAdvisorReply(text, pdkProfile, draft) },
        ]);
    };

    const sendMessage = async () => {
        const text = prompt.trim();
        if (!text || phase === 'building' || isChatting) return;

        const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: text }];
        setMessages(nextMessages);
        setPrompt('');

        if (isConfirmationPrompt(text) && draftSpec.trim()) {
            void launch(draftSpec);
            return;
        }

        if (hasHardwareIntent(text) && !isConfirmationPrompt(text)) {
            proposeDraft(text, nextMessages);
            return;
        }

        if (isCasualPrompt(text) || HELP_RE.test(text) || /[?]/.test(text)) {
            setMessages((previous) => [...previous, { role: 'assistant', content: makeAdvisorReply(text, pdkProfile) }]);
            return;
        }

        if (modelChoice === 'byok' && !hasByok) {
            requestByokSetup();
            return;
        }

        setIsChatting(true);
        setThinking('Thinking through the VLSI intent.');

        try {
            const byokPayload = modelChoice === 'byok' ? serializeByokConfig() : null;
            const res = await api.post('/chat/converse', {
                messages: nextMessages.map((message) => ({ role: message.role, content: message.content })),
                plan_type: modelChoice === 'infinite' ? 'agentic_paid' : 'byok',
                api_key: byokPayload,
            });
            const reply = res.data?.reply || makeAdvisorReply(text, pdkProfile);
            setMessages((previous) => [...previous, { role: 'assistant', content: reply }]);
        } catch (err: unknown) {
            const detail = typeof err === 'object' && err !== null && 'response' in err
                ? (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
                : undefined;
            setMessages((previous) => [
                ...previous,
                {
                    role: 'assistant',
                    tone: 'error',
                    content: toUserError(detail, makeAdvisorReply(text, pdkProfile)),
                },
            ]);
        } finally {
            setThinking('');
            setIsChatting(false);
        }
    };

    const launch = async (descriptionOverride?: string) => {
        const description = normalizePrompt(descriptionOverride || prompt.trim() || draftSpec.trim() || lastBuildPrompt.trim());
        if (!description || phase === 'building') return;

        if (!isBuildReadyPrompt(description)) {
            const nextMessages: ChatMessage[] = prompt.trim()
                ? [...messages, { role: 'user', content: prompt.trim() }]
                : messages;
            if (prompt.trim()) setPrompt('');
            proposeDraft(description, nextMessages);
            return;
        }

        if (modelChoice === 'byok' && !hasByok) {
            requestByokSetup();
            return;
        }

        const selectedPdk = pdkOptions.find((pdk) => pdk.key === pdkProfile);
        if (!skipOpenlane && selectedPdk && selectedPdk.gds_ready === false) {
            setError(`${selectedPdk.key} is not ready for GDSII. Switch to RTL-only or choose an installed PDK.`);
            return;
        }

        const nextDesignName = (designName.trim() || slugify(description) || 'agentic_chip').slice(0, 64);
        abortRef.current?.abort();
        announcedStages.current = new Set();
        announcedDecisions.current = new Set();
        autoOpenedWorkspace.current = false;
        setDraftSpec('');
        setDraftSummary('');
        setLastBuildPrompt(description);
        setPrompt('');
        setPhase('building');
        
        setError('');
        setEvents([]);
        setArtifacts([]);
        setSelectedArtifact(null);
        setArtifactPreview('');
        setJobStatus('queued');
        setMessages((previous) => [
            ...previous,
            { role: 'user', content: description },
            {
                role: 'assistant',
                content: `Running **${nextDesignName}** on **${modelChoice === 'infinite' ? 'Infinite' : byokLabel}**. I will review the build like a senior VLSI engineer: feasibility first, evidence at every gate, and no fabrication-ready claim unless signoff blockers are visible and resolved.`,
            },
        ]);

        try {
            const byok = modelChoice === 'byok' ? serializeByokConfig() : null;
            const res = await api.post('/build', {
                design_name: nextDesignName,
                description,
                skip_openlane: skipOpenlane,
                show_thinking: true,
                flow_profile: flowProfile,
                max_retries: 5,
                min_coverage: 80.0,
                pdk_profile: pdkProfile,
                plan_type: modelChoice === 'infinite' ? 'agentic_paid' : 'byok',
                api_key: byok,
            });
            const activeJobId = res.data.job_id;
            const activeDesignName = res.data.design_name || nextDesignName;
            setJobId(activeJobId);
            setDesignName(activeDesignName);
            setJobStatus('running');
            void stream(activeJobId, byok, activeDesignName);
        } catch (err: unknown) {
            setPhase('idle');
            if (isNetworkError(err)) {
                setError('Unable to connect to the build service.');
            } else {
                const detail = typeof err === 'object' && err !== null && 'response' in err
                    ? (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
                    : undefined;
                setError(toUserError(detail, 'Build failed to start.'));
            }
        }
    };

    const stream = async (activeJobId: string, byokPayload: string | null, activeDesignName: string) => {
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        const headers = await getSseHeaders(byokPayload ? { 'X-LLM-API-Key': byokPayload } : {});
        let retries = 0;

        fetchEventSource(`${API_BASE}/build/stream/${activeJobId}`, {
            method: 'GET',
            headers,
            signal: ctrl.signal,
            openWhenHidden: true,
            async onopen(response) {
                const type = response.headers.get('content-type') || '';
                if (response.ok && type.includes('text/event-stream')) {
                    retries = 0;
                    return;
                }
                throw new Error('Stream unavailable');
            },
            onmessage(event) {
                try {
                    const data: BuildEvent = JSON.parse(event.data);
                    if (data.type === 'ping') return;
                    if (data.type === 'stream_end') {
                        ctrl.abort();
                        void finish(data.status || 'failed', activeDesignName);
                        return;
                    }
                    if (data.type === 'agent_thinking') {
                        setThinking(data.message || data.content || 'Reasoning through the next pipeline action.');
                        return;
                    }
                    setThinking('');
                    if (data.type === 'spec_reconciled') {
                        const count = data.changes?.length || 0;
                        notifyBuild('AgentIC adjusted the spec', `${count} PDK feasibility change${count === 1 ? '' : 's'} applied.`);
                        addAssistant(`**Spec adjusted for feasibility**\n\n${describeSpecChanges(data)}\n\nI am continuing with this closest feasible implementation.`);
                    }
                    if (data.type === 'design_decision') {
                        notifyBuild('AgentIC design decision', data.message || 'A feasibility-driven design change was applied.');
                        const key = `${data.state || 'UNKNOWN'}:${data.message || data.content || ''}`;
                        if (!announcedDecisions.current.has(key)) {
                            announcedDecisions.current.add(key);
                            addAssistant(`**Design decision**\n\n${data.message || data.content || 'A feasibility-driven design change was applied.'}`);
                        }
                    }
                    setEvents((previous) => {
                        const duplicate = previous.some((item) => (
                            item.timestamp === data.timestamp && item.type === data.type && item.message === data.message
                        ));
                        return duplicate ? previous : [...previous, data];
                    });
                    setJobStatus(data.type === 'error' ? 'failed' : 'running');

                    const stage = data.state && data.state !== 'UNKNOWN' ? data.state : '';
                    if (stage && !announcedStages.current.has(stage) && data.type !== 'log') {
                        announcedStages.current.add(stage);
                        const label = activeStages.find((item) => item.state === stage)?.label || stage;
                        addAssistant(formatStageAnnouncement(label, getStageReview(stage, data, skipOpenlane), data.message || STAGE_NOTES[stage]));
                    }
                    if (data.type === 'stage_complete') {
                        const label = activeStages.find((item) => item.state === stage)?.label || stage || 'Stage';
                        notifyBuild('AgentIC stage complete', `${label} is ready for review.`);
                    }
                    if (data.type === 'error') {
                        const message = data.message || 'The pipeline reported an error.';
                        setError(message);
                        addAssistant(`**Pipeline error**\n\n${message}`, 'error');
                    }
                    void fetchArtifacts(activeDesignName);
                } catch {
                    // Ignore malformed keepalive payloads.
                }
            },
            onerror(err) {
                if (ctrl.signal.aborted) return;
                retries += 1;
                if (retries > 8) {
                    ctrl.abort();
                    setError('Live connection lost. Checking final build status.');
                    void finish('failed', activeDesignName);
                    throw err;
                }
                return Math.min(1000 * retries, 5000);
            },
        }).catch(() => {
            if (ctrl.signal.aborted) return;
            setError('Live connection lost. Checking final build status.');
            void finish('failed', activeDesignName);
        });
    };

    const finish = async (status: string, activeDesignName: string) => {
        const finalStatus: JobStatus = status === 'done' ? 'done' : status === 'cancelled' ? 'cancelled' : 'failed';
        setJobStatus(finalStatus);
        setPhase('done');
        setThinking('');
        await fetchArtifacts(activeDesignName, true);
        addAssistant(
            finalStatus === 'done'
                ? `**Build complete.** Generated files are ready below: RTL, testbench/formal collateral, reports, and layout outputs when enabled.`
                : `**Build stopped with status ${finalStatus}.** Partial artifacts are preserved below for inspection.`,
            finalStatus === 'done' ? 'success' : 'error'
        );
    };

    const cancel = async () => {
        if (!jobId) return;
        try {
            await api.post(`/build/cancel/${jobId}`);
            setJobStatus('cancelling');
            addAssistant('Cancellation requested. I will keep any generated artifacts.', 'normal');
        } catch {
            setError('Unable to cancel this run.');
        }
    };

    const handlePrimaryAction = async () => {
        const text = prompt.trim();
        if (phase === 'building' || isChatting) return;
        if (!text && draftSpec.trim()) {
            await launch(draftSpec);
            return;
        }
        if (isConfirmationPrompt(text) && draftSpec.trim()) {
            setMessages((previous) => [...previous, { role: 'user', content: text }]);
            setPrompt('');
            await launch(draftSpec);
            return;
        }
        if (isBuildReadyPrompt(text)) {
            const draft = makeDraftFromPrompt(text, pdkProfile);
            await launch(draft.spec);
            return;
        }
        await sendMessage();
    };

    const resetDraft = () => {
        setDraftSpec('');
        setDraftSummary('');
    };

    const reset = () => {
        abortRef.current?.abort();
        setPhase('idle');
        setEvents([]);
        setArtifacts([]);
        setSelectedArtifact(null);
        setArtifactPreview('');
        setThinking('');
        setJobId('');
        setJobStatus('queued');
        setError('');
        
        setLastBuildPrompt('');
        autoOpenedWorkspace.current = false;
        resetDraft();
    };

    const downloadArtifact = async (artifact: Artifact) => {
        try {
            const res = await api.get(`/build/artifacts/${designName}/${artifact.name}`, { responseType: 'blob' });
            const url = window.URL.createObjectURL(res.data);
            const link = document.createElement('a');
            link.href = url;
            link.download = artifact.name;
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.URL.revokeObjectURL(url);
        } catch {
            setError(`Unable to download ${artifact.name}.`);
        }
    };
    return (
        <div className="studio-3pane-root">
            {/* Middle Pane: Workspace Explorer */}
            <aside className={`studio-3pane-explorer ${isWorkspaceCollapsed ? 'collapsed' : ''}`}>
                <div className="studio-3pane-explorer-header">Workspace Explorer</div>
                <div className="studio-3pane-explorer-tree">
                    {artifactSections.map(section => (
                        <div key={section.label} style={{ marginBottom: '0.5rem' }}>
                            <div className="studio-tree-folder">
                                <Folder size={14} /> {section.label}
                            </div>
                            {section.files.map(artifact => (
                                <div
                                    key={artifact.name}
                                    className={'studio-tree-file ' + (selectedArtifact?.name === artifact.name ? 'active' : '')}
                                    onClick={() => {
                                        setArtifactPreview('');
                                        setSelectedArtifact(artifact);
                                    }}
                                >
                                    <FileText size={12} /> {artifact.name}
                                </div>
                            ))}
                        </div>
                    ))}
                    {artifactSections.length === 0 && (
                        <div style={{ padding: '1rem', fontSize: '0.8rem', color: 'var(--text-dim)' }}>Generated files will appear here.</div>
                    )}
                </div>
            </aside>

            {/* Right Pane: Editor & Logs */}
            <main className={`studio-3pane-main ${isWorkspaceCollapsed ? 'collapsed' : ''}`}>
                <div className="studio-3pane-editor">
                    <div className="studio-3pane-editor-tabs">
                        {selectedArtifact ? (
                            <div className="studio-3pane-editor-tab active">
                                <FileText size={14} /> {selectedArtifact.name}
                            </div>
                        ) : (
                            <div className="studio-3pane-editor-tab">
                                <FileText size={14} /> Select a file
                            </div>
                        )}
                        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', paddingRight: '1rem', gap: '1rem' }}>
                            {phase !== 'idle' && (
                                <span style={{ fontSize: '0.75rem', color: 'var(--text-mid)', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
                                    <div className={`studio-status-dot ${error ? 'failed' : jobStatus === 'done' ? 'done' : ''}`} />
                                    {currentStageLabel}: {liveStatusText} ({progress}%)
                                </span>
                            )}
                            {phase === 'building' && <button onClick={cancel} style={{ background: 'transparent', border: '1px solid var(--border-mid)', color: 'var(--fail)', borderRadius: '4px', padding: '0.2rem 0.5rem', fontSize: '0.75rem', cursor: 'pointer' }}>Stop</button>}
                        </div>
                    </div>
                    <div className="studio-3pane-editor-content">
                        {selectedArtifact ? (
                            <Editor
                                height="100%"
                                language={artifactLanguage(selectedArtifact.name)}
                                theme="vs-dark"
                                value={artifactPreview || 'Loading preview...'}
                                options={{
                                    readOnly: true,
                                    minimap: { enabled: false },
                                    fontSize: 13,
                                    fontFamily: "'Geist Mono', 'Fira Code', monospace",
                                    scrollBeyondLastLine: false,
                                    smoothScrolling: true,
                                    wordWrap: 'on',
                                    padding: { top: 10, bottom: 10 }
                                }}
                            />
                        ) : (
                            <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center', color: 'var(--text-dim)', fontSize: '0.9rem' }}>
                                AgentIC Studio is ready. Select a generated file to view its contents.
                            </div>
                        )}
                    </div>
                </div>
            </main>

            {/* Left Pane: Chat */}
            <aside className={`studio-3pane-chat ${isWorkspaceCollapsed ? 'expanded' : ''}`}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0.75rem 1rem', borderBottom: '1px solid var(--border)', background: 'var(--bg-sidebar)' }}>
                    <span style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-mid)' }}>AgentIC Pipeline Feed</span>
                    <button 
                        onClick={() => setIsWorkspaceCollapsed(!isWorkspaceCollapsed)}
                        style={{ background: 'transparent', border: 'none', color: 'var(--text-mid)', cursor: 'pointer', display: 'flex', alignItems: 'center' }}
                        title={isWorkspaceCollapsed ? "Expand Workspace" : "Collapse Workspace"}
                    >
                        {isWorkspaceCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
                    </button>
                </div>
                <div className="studio-3pane-chat-history">
                    {phase === 'idle' && (
                        <div className="studio-greeting" style={{ padding: '1rem' }}>
                            <h2 style={{ fontSize: '1.2rem', marginBottom: '0.5rem' }}>VLSI-aware silicon copilot</h2>
                            <p style={{ color: 'var(--text-mid)', fontSize: '0.9rem' }}>Chat first, draft the feasible spec, then launch RTL, verification, and physical flow when the chip request is ready.</p>
                        </div>
                    )}
                    {messages.map((message, index) => (
                        <motion.article
                            key={message.role + '-' + index}
                            className={'studio-min-message ' + message.role + ' ' + (message.tone || '')}
                            initial={{ opacity: 0, y: 8 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ duration: 0.18 }}
                            style={{ padding: '0.75rem', background: message.role === 'assistant' ? 'transparent' : 'var(--bg-hover)', borderRadius: '8px', borderBottom: '1px solid var(--border)' }}
                        >
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                        </motion.article>
                    ))}
                    
                    {/* Streaming Events */}
                    {events.filter(e => e.type === 'log' || e.type === 'agent_thinking' || e.type === 'agent_action' || e.type === 'error').map((e, i) => (
                        <div key={'event-'+i} className={`studio-chat-event ${e.type === 'error' ? 'error' : ''}`}>
                            <div className="studio-chat-event-header">
                                {e.type === 'error' ? <AlertCircle size={12} /> : <Terminal size={12} />}
                                <span>{e.state || 'System'}</span>
                            </div>
                            <div className="studio-chat-event-body">
                                {e.message || e.content || e.type}
                            </div>
                        </div>
                    ))}

                    {(thinking || phase === 'building') && (
                        <div className="studio-min-thinking" style={{ padding: '1rem' }}>
                            <p style={{ color: 'var(--text-dim)', fontStyle: 'italic', fontSize: '0.85rem' }}>{thinking || 'AgentIC is working...'}</p>
                        </div>
                    )}
                    <div ref={scrollRef} />
                </div>
                <div className="studio-3pane-chat-input">
                    {draftSpec && phase === 'idle' && (
                         <div style={{ marginBottom: '0.5rem', background: 'var(--accent-soft)', padding: '0.75rem', borderRadius: '6px' }}>
                             <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--accent)', marginBottom: '0.25rem' }}>Draft Ready</div>
                             <div style={{ fontSize: '0.8rem', color: 'var(--text-mid)', marginBottom: '0.5rem' }}>{draftSummary}</div>
                             <button type="button" onClick={() => void launch(draftSpec)} style={{ background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '4px', padding: '0.3rem 0.6rem', fontSize: '0.8rem', cursor: 'pointer' }}>Build this chip</button>
                         </div>
                    )}
                    <textarea
                        value={prompt}
                        onChange={(event) => setPrompt(event.target.value)}
                        placeholder="Chat about VLSI intent, or describe a chip to build..."
                        rows={3}
                        style={{ width: '100%', padding: '0.75rem', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-card)', color: 'var(--text)', resize: 'none', fontFamily: 'inherit', fontSize: '0.9rem' }}
                        onKeyDown={(event) => {
                            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                                event.preventDefault();
                                void handlePrimaryAction();
                            }
                        }}
                    />
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '0.5rem' }}>
                        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                            <span style={{ fontSize: '0.75rem', padding: '0.2rem 0.5rem', background: 'var(--bg-hover)', color: 'var(--text-mid)', borderRadius: '4px', display: 'flex', alignItems: 'center', gap: '0.25rem' }}><Cpu size={12} /> {pdkProfile}</span>
                            <span style={{ fontSize: '0.75rem', padding: '0.2rem 0.5rem', background: 'var(--bg-hover)', color: 'var(--text-mid)', borderRadius: '4px' }}>{modelChoice}</span>
                        </div>
                        <button
                            onClick={() => void handlePrimaryAction()}
                            disabled={!(prompt.trim() || draftSpec.trim()) || phase === 'building' || isChatting}
                            style={{ background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', padding: '0.4rem 0.8rem', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.3rem', fontSize: '0.85rem', fontWeight: 500 }}
                        >
                            Send <ArrowUp size={14} />
                        </button>
                    </div>
                </div>
            </aside>

            <BillingModal
                isOpen={showBillingModal}
                onClose={() => setShowBillingModal(false)}
                initialMode={billingMode}
                onKeySaved={() => {
                    setModelChoice('byok');
                    api.get('/profile')
                        .then((res) => setProfile((previous) => ({ ...previous, ...res.data, has_byok_key: true })))
                        .catch(() => setProfile((previous) => ({ ...previous, has_byok_key: true })));
                    setShowBillingModal(false);
                }}
            />
        </div>
    );
};

export default DesignStudio;
