/* eslint-disable @typescript-eslint/ban-ts-comment */
// @ts-nocheck
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import '../studio-3pane.css';
import { motion } from 'framer-motion';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import Editor from '@monaco-editor/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Activity,
  AlertTriangle,
  ArrowUp,
  CheckCircle2,
  CircleStop,
  Code2,
  FileText,
  GitBranch,
  KeyRound,
  Settings2,
  Sparkles,
  Terminal,
  Wrench,
} from 'lucide-react';
import { BillingModal } from '../components/BillingModal';
import { api, API_BASE, getSseHeaders } from '../api';
import { isNetworkError, toUserError } from '../utils/errorFormatter';

type Phase = 'idle' | 'building' | 'done';
type ModelChoice = 'infinite' | 'byok';
type BillingMode = 'agentic' | 'byok';
type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'cancelling';
type FlowChoice = 'sky130_oss_executable' | 'sky130_oss_experimental_complete';
type EventKind =
  | 'reasoning'
  | 'tool-call'
  | 'tool-result'
  | 'file'
  | 'error'
  | 'fix'
  | 'stage'
  | 'decision'
  | 'final'
  | 'log';

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  tone?: 'normal' | 'success' | 'error';
}

interface BuildEvent {
  type: string;
  state?: string;
  message?: string;
  step?: number;
  total_steps?: number;
  timestamp?: number | string;
  status?: string;
  agent_name?: string;
  thought_type?: string;
  content?: string;
  changes?: Array<Record<string, unknown>>;
  target?: string;
  category?: string;
  original_request?: string;
  constraint?: string;
  chosen_substitute?: string;
  stage_name?: string;
  summary?: string;
  is_live_waiting?: boolean;
  options?: Array<Record<string, string>>;
  readiness_level?: string;
  signoff_blockers?: string[];
  artifacts?: Array<Record<string, unknown>>;
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
  path?: string;
}

interface PdkOption {
  key: string;
  pdk?: string;
  description?: string;
  fabrication_ready?: boolean;
  gds_ready?: boolean;
  can_synthesize?: boolean;
  can_harden?: boolean;
  available?: boolean;
  status?: string;
  readiness_tier?: string;
  flow_backend?: string;
  recommended_flow_profile?: string;
  reason?: string;
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

function pdkCanHarden(pdk?: PdkOption): boolean {
  if (!pdk) return true;
  return (pdk.can_harden ?? pdk.gds_ready) === true;
}

function pdkReadinessLabel(pdk?: PdkOption): string {
  if (!pdk) return 'Auto';
  if (!pdk.available) return 'Needs install';
  if (pdk.fabrication_ready && pdkCanHarden(pdk)) return 'Tapeout candidate';
  if (pdkCanHarden(pdk)) return 'Layout ready';
  if (pdk.readiness_tier?.includes('research')) return 'Research mode';
  if (pdk.can_synthesize) return 'RTL/synthesis';
  return 'RTL only';
}

function uniqueArtifacts(files: Artifact[]): Artifact[] {
  const byName = new Map<string, Artifact>();
  files.forEach((artifact) => {
    if (!artifact.name) return;
    byName.set(artifact.name, artifact);
  });
  return Array.from(byName.values());
}

const STAGE_NOTES: Record<string, string> = {
  INIT: 'Preparing the workspace, tool context, model route, and build ledger.',
  SPEC: 'Turning the request into a buildable chip specification.',
  SPEC_VALIDATE: 'Checking that the spec is implementable as synthesizable digital logic.',
  HIERARCHY_EXPAND: 'Breaking the chip into blocks, interfaces, and verification targets.',
  FEASIBILITY_CHECK: 'Checking PDK, macro, area, timing, and signoff constraints.',
  VERIFICATION_PLAN: 'Writing verification intent before RTL generation.',
  RTL_GEN: 'Writing synthesizable RTL and source collateral.',
  RTL_FIX: 'Running syntax, lint, width, hierarchy, and elaboration repair loops.',
  CDC_ANALYZE: 'Checking clock-domain and reset-domain safety.',
  VERIFICATION: 'Running simulation and testbench checks.',
  FORMAL_VERIFY: 'Running formal proof where supported.',
  COVERAGE_CHECK: 'Checking whether important states and scenarios were exercised.',
  REGRESSION: 'Re-running tests after fixes.',
  SDC_GEN: 'Generating timing constraints.',
  SYNTHESIS: 'Mapping RTL to a gate-level netlist.',
  FLOORPLAN: 'Preparing physical layout constraints.',
  HARDENING: 'Running placement, routing, and layout generation.',
  CONVERGENCE_REVIEW: 'Reviewing timing, congestion, DRC, LVS, and power evidence.',
  ECO_PATCH: 'Applying bounded implementation fixes from convergence evidence.',
  POWER_ANALYSIS: 'Reviewing power evidence and assumptions.',
  TIMING_ANALYSIS: 'Checking setup and hold timing.',
  PHYSICAL_VERIFY: 'Running physical verification checks.',
  SIGNOFF: 'Reviewing evidence before calling the chip complete.',
  IP_PACKAGE: 'Packaging RTL, constraints, reports, layout outputs, and signoff notes.',
};

const CASUAL_RE = /^(hi+|hello+|hey+|yo+|sup|thanks|thank you|ok|okay|test)$/i;
const CONFIRM_RE = /^(yes|yep|yeah|sure|ok|okay|go ahead|proceed|build it|run it|start it|make it)$/i;
const HELP_RE = /\b(help|what can you do|capabilit|possible|not possible|can you|how do i|suggest|prompt)\b/i;
const HARDWARE_RE = /\b(chip|rtl|verilog|systemverilog|vlsi|asic|fpga|pdk|sky130|gf180|gds|layout|synthesis|synthesize|timer|uart|spi|i2c|axi|apb|wishbone|fifo|ram|rom|sram|cpu|risc|risc-v|mcu|microcontroller|alu|dma|pwm|watchdog|aes|sha|trng|gpio|counter|fsm|pll|adc|dac|register|bus|peripheral|accelerator|core)\b/i;
const BUILD_ACTION_RE = /\b(build|create|design|generate|make|implement|synthesize|harden|layout|verify)\b/i;
const SPEC_DETAIL_RE = /\b(with|using|include|support|clock|reset|register|interrupt|memory[- ]mapped|bit|width|mhz|khz|formal|testbench|coverage|gdsii|openlane)\b/i;
const ERROR_RE = /(%error|%warning|syntax error|parse error|error:|fatal|failed|traceback|critical|lint|violation|unmapped|pinmissing|pinnotfound)/i;
const FIX_RE = /(rtl_fix|fixing|repair|retry|re-check|rechecking|auto-fix|applying fix|rerun|regenerat|patch)/i;

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

function suggestDesignName(text: string): string {
  const explicit = text.match(/\b(?:named|called|name(?:\s+it)?|project)\s+["']?([a-zA-Z][a-zA-Z0-9 _-]{2,48})["']?/i);
  if (explicit?.[1]) return slugify(explicit[1]);

  const stop = new Set([
    'a', 'an', 'the', 'and', 'or', 'with', 'using', 'for', 'to', 'in', 'on', 'of',
    'build', 'create', 'design', 'generate', 'make', 'implement', 'synthesize',
    'synthesizable', 'chip', 'rtl', 'verilog', 'systemverilog', 'flow', 'pdk',
    'testbench', 'formal', 'coverage', 'gds', 'gdsii', 'node', 'process',
    'controller', 'peripheral', 'engine', 'core', 'block', 'module', 'ip',
    'configurable', 'support', 'supports', 'include', 'including', 'loopback',
    'interrupt', 'registers', 'clock', 'reset',
  ]);
  const candidates = normalizePrompt(text).toLowerCase()
    .replace(/[^a-z0-9\s_-]/g, ' ')
    .split(/\s+/)
    .filter((word) => word.length > 1 && !stop.has(word) && !/^\d+nm$/.test(word));
  const words = candidates.slice(0, 4);
  if (words.length >= 2) return slugify(words.join(' '));
  if (words.length === 1) return slugify(`${words[0]} design`);
  return 'agentic_chip';
}

function normalizePrompt(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

function wordCount(text: string): number {
  return normalizePrompt(text).split(/\s+/).filter(Boolean).length;
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
      ? 'The request is build-ready. I added PDK-aware guardrails and will run the full evidence pipeline.'
      : 'I expanded the idea into a silicon-shaped prompt. Type "build it" to run it, or revise the spec in chat.',
  };
}

function advisorReply(text: string, pdkProfile: string, draft?: PromptDraft): string {
  if (draft) {
    return [
      '**Draft chip prompt ready**',
      '',
      draft.summary,
      '',
      `Target: **${pdkProfile}**`,
      '',
      draft.spec,
    ].join('\n');
  }

  if (isCasualPrompt(text)) {
    return 'Tell me the chip or RTL block you want. I will turn it into a VLSI-aware build, run the pipeline, stream the reasoning and tool output, and keep generated files visible as they land.';
  }

  if (HELP_RE.test(text) || /[?]/.test(text)) {
    return 'AgentIC is built for synthesizable digital silicon: RTL, testbenches, formal checks, simulation, synthesis, OpenLane-style layout, reports, and packaging. Describe the block, interface, clock/reset, registers or data widths, verification expectation, target PDK, and whether GDSII is required.';
  }

  return 'I can help refine that into a buildable chip request. Add the interface, clock/reset, data width, verification expectation, and target PDK.';
}

function readByokPayload(): string | null {
  const raw = localStorage.getItem('agentic_byok_key');
  if (!raw) return null;
  return raw;
}

function modelLabel(): string {
  const raw = readByokPayload();
  if (!raw) return 'BYOK';
  try {
    const parsed = JSON.parse(raw);
    const models = Object.values(parsed)
      .map((entry: any) => entry?.model)
      .filter(Boolean);
    const unique = Array.from(new Set(models));
    if (unique.length === 1) return String(unique[0]);
    if (unique.length > 1) return `${String(unique[0])} + ${unique.length - 1}`;
  } catch {
    return 'BYOK key';
  }
  return 'BYOK';
}

function isTextArtifact(name: string): boolean {
  return /\.(v|sv|svh|vh|sby|sdc|tcl|json|md|txt|log|rpt|csv|ys|cfg|lef|def|lib|spice|sp)$/i.test(name);
}

function isLogArtifact(name: string): boolean {
  return /\.(log|txt)$/i.test(name) || /(^|[/_-])(log|stdout|stderr)([/_.-]|$)/i.test(name);
}

function sanitizeOperationalText(text: string): string {
  return text
    .replace(/\/(?:app|home|opt|usr|tmp|var|workspace|designs)\/[^\s,'")]+/g, '[workspace path]')
    .replace(/\\\\[^\s,'")]+/g, '[workspace path]')
    .replace(/\{[^{}\n]*(?:resolved|hint|env|PDK_ROOT|OPENLANE_ROOT)[^{}\n]*\}/gi, '[tool readiness details]')
    .replace(/\s+/g, ' ')
    .trim();
}

function summarizeLogPreview(raw: string, artifactName: string): string {
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const joined = lines.join('\n');
  const errors = lines.filter((line) => ERROR_RE.test(line)).slice(0, 6).map(sanitizeOperationalText);
  const warnings = lines.filter((line) => /warning|not found|missing|unavailable/i.test(line) && !ERROR_RE.test(line)).slice(0, 4).map(sanitizeOperationalText);
  const stages = lines
    .map((line) => line.match(/\[(INIT|SPEC|RTL_GEN|RTL_FIX|VERIFICATION|SYNTHESIS|HARDENING|SIGNOFF|IP_PACKAGE)\]/i)?.[1])
    .filter(Boolean);
  const stage = stages[stages.length - 1];
  const toolChecks = {
    ready: (joined.match(/'ok': True|ok=True|"ok": true/gi) || []).length,
    attention: (joined.match(/'ok': False|ok=False|"ok": false/gi) || []).length,
  };

  const out = [
    `Build note: ${artifactName}`,
    '',
    stage ? `Current evidence: ${stage}` : 'Current evidence: initialization and tool readiness captured.',
    `Tool readiness: ${toolChecks.ready} checks passed${toolChecks.attention ? `, ${toolChecks.attention} need attention` : ''}.`,
  ];

  if (errors.length) {
    out.push('', 'Blockers surfaced:', ...errors.map((line) => `- ${line.slice(0, 220)}`));
  } else if (warnings.length) {
    out.push('', 'Items to watch:', ...warnings.map((line) => `- ${line.slice(0, 220)}`));
  } else {
    out.push('', 'No exact tool error is visible in this artifact yet.');
  }

  out.push('', 'Raw operational logs are kept as artifacts, but the workspace preview stays summarized.');
  return out.join('\n');
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

function inferArtifactType(artifact: Artifact): string {
  const name = artifact.name.toLowerCase();
  if (artifact.type) return artifact.type;
  if (/\.(v|sv|svh|vh)$/.test(name) && !name.includes('tb')) return 'rtl';
  if (name.includes('tb') || name.endsWith('.sby') || name.includes('formal') || name.includes('vcd')) return 'verification';
  if (name.endsWith('.gds') || name.endsWith('.def') || name.endsWith('.lef') || name.includes('openlane')) return 'physical';
  if (name.endsWith('.sdc') || name.includes('constraint')) return 'constraints';
  if (name.endsWith('.rpt') || name.endsWith('.pdf') || name.endsWith('.docx') || name.includes('report')) return 'report';
  if (name.endsWith('.log')) return 'log';
  if (name.endsWith('.json') || name.endsWith('.cfg') || name.endsWith('.tcl') || name.endsWith('.ys')) return 'config';
  return 'other';
}

function artifactSections(artifacts: Artifact[]) {
  const sections = [
    { label: 'RTL', types: ['rtl'] },
    { label: 'Verification', types: ['verification', 'formal', 'waveform'] },
    { label: 'Physical', types: ['physical', 'layout', 'timing', 'constraints'] },
    { label: 'Reports', types: ['report', 'log', 'config', 'script', 'other'] },
  ];

  return sections
    .map((section) => ({
      ...section,
      files: artifacts.filter((artifact) => section.types.includes(inferArtifactType(artifact))),
    }))
    .filter((section) => section.files.length > 0);
}

function pickBestArtifact(artifacts: Artifact[], stage: string): Artifact | null {
  if (!artifacts.length) return null;
  const nonLogs = artifacts.filter((artifact) => !isLogArtifact(artifact.name));
  const stageLower = stage.toLowerCase();
  const preferred =
    stageLower.includes('rtl') ? [/\.(sv|v)$/i, /lint|rtl/i] :
    stageLower.includes('verification') || stageLower.includes('formal') ? [/tb\.(sv|v)$/i, /\.sby$/i, /formal|coverage|vcd/i] :
    stageLower.includes('sdc') || stageLower.includes('synth') ? [/\.sdc$/i, /\.ys$/i, /synth|netlist|rpt/i] :
    stageLower.includes('floor') || stageLower.includes('hardening') || stageLower.includes('physical') ? [/\.gds$/i, /\.def$/i, /\.lef$/i, /openlane|drc|lvs/i] :
    stageLower.includes('signoff') || stageLower.includes('package') ? [/report|manifest|signoff/i, /\.pdf$/i, /\.docx$/i] :
    [/\.(sv|v)$/i, /report|manifest/i];

  for (const pattern of preferred) {
    const match = nonLogs.find((artifact) => pattern.test(artifact.name));
    if (match) return match;
  }
  return nonLogs[0] || artifacts[0];
}

function eventText(event: BuildEvent): string {
  if (event.type === 'spec_reconciled') return describeSpecChanges(event);
  return String(event.message || event.content || event.summary || event.type || '').trim();
}

function displayEventText(event: BuildEvent): string {
  const raw = eventText(event);
  const kind = classifyEvent(event);
  if (kind === 'error') return sanitizeOperationalText(raw).slice(0, 900);
  if (kind === 'fix') return sanitizeOperationalText(raw).slice(0, 520);
  if (kind === 'tool-call') return 'Running a pipeline tool and collecting evidence.';
  if (kind === 'tool-result') {
    if (ERROR_RE.test(raw)) return sanitizeOperationalText(raw).slice(0, 520);
    return 'Tool evidence captured. AgentIC is deciding the next step.';
  }
  if (kind === 'file') return sanitizeOperationalText(raw).slice(0, 260);
  if (raw.length > 420 || /\/app\/|\/opt\/|resolved|STARTUP SELF CHECK|\{'tool'/i.test(raw)) {
    return sanitizeOperationalText(raw).slice(0, 360);
  }
  return raw;
}

function eventState(event: BuildEvent): string {
  return event.stage_name || event.state || 'SYSTEM';
}

function classifyEvent(event: BuildEvent): EventKind {
  const text = `${event.type || ''} ${event.thought_type || ''} ${eventText(event)}`;
  if (event.type === 'stream_end') return 'final';
  if (event.type === 'error' || ERROR_RE.test(text)) return 'error';
  if (event.type === 'spec_reconciled' || FIX_RE.test(text)) return 'fix';
  if (event.type === 'agent_thinking' || event.type === 'agent_thought' || event.thought_type === 'thought') return 'reasoning';
  if (event.thought_type === 'tool_call' || event.type === 'agent_action') return 'tool-call';
  if (event.thought_type === 'tool_result' || /result:|output:|passed|completed|success/i.test(text)) return 'tool-result';
  if (event.type === 'stage_complete') return 'stage';
  if (event.type === 'design_decision') return 'decision';
  if (/artifact|generated|written|write file|saved|created/i.test(text)) return 'file';
  return 'log';
}

function eventTitle(event: BuildEvent): string {
  const kind = classifyEvent(event);
  const state = eventState(event);
  if (kind === 'reasoning') return event.agent_name || 'Agent reasoning';
  if (kind === 'tool-call') return `Tool call - ${state}`;
  if (kind === 'tool-result') return `Tool result - ${state}`;
  if (kind === 'file') return `Workspace update - ${state}`;
  if (kind === 'error') return `Exact issue - ${state}`;
  if (kind === 'fix') return `Fix loop - ${state}`;
  if (kind === 'stage') return `Stage complete - ${state}`;
  if (kind === 'decision') return `Design decision - ${state}`;
  if (kind === 'final') return 'Build stream closed';
  return state;
}

function describeSpecChanges(event: BuildEvent): string {
  const changes = event.changes || [];
  if (!changes.length) {
    return event.message || 'AgentIC adjusted the spec for PDK or tool feasibility.';
  }
  return changes
    .map((change) => {
      const target = change.target || change.category || 'request';
      const original = change.original_request || change.constraint || '';
      const chosen = change.chosen_substitute || change.summary || '';
      return `- ${target}: ${original}${chosen ? ` -> ${chosen}` : ''}`;
    })
    .join('\n');
}

function parseDiagnostics(rawEvents: BuildEvent[], selectedName?: string) {
  const basename = selectedName?.split(/[\\/]/).pop();
  const markers = [];
  const recentErrorText = rawEvents
    .filter((event) => classifyEvent(event) === 'error')
    .slice(-8)
    .map(eventText)
    .join('\n');

  for (const line of recentErrorText.split('\n')) {
    const verilator = line.match(/%(Warning|Error)[^:]*:\s*([^:]+):(\d+):(\d+):\s*(.*)/i);
    const generic = line.match(/([^:\s]+\.(?:sv|v|vh|svh)):(\d+):(?:(\d+):)?\s*(.*)/i);
    const match = verilator || generic;
    if (!match) continue;

    const file = verilator ? match[2] : match[1];
    const fileBase = file.split(/[\\/]/).pop();
    if (basename && fileBase && basename !== fileBase) continue;

    const lineNumber = Number(verilator ? match[3] : match[2]) || 1;
    const column = Number(verilator ? match[4] : match[3]) || 1;
    const message = String(verilator ? match[5] : match[4] || line).trim();
    const isWarning = /warning/i.test(line);

    markers.push({
      severity: isWarning ? 4 : 8,
      startLineNumber: lineNumber,
      startColumn: column,
      endLineNumber: lineNumber,
      endColumn: column + 20,
      message,
      source: 'AgentIC pipeline',
    });
  }
  return markers;
}

function formatBytes(size?: number): string {
  if (!size) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export const DesignStudio = () => {
  const [phase, setPhase] = useState<Phase>('idle');
  const [prompt, setPrompt] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draftSpec, setDraftSpec] = useState('');
  const [draftSummary, setDraftSummary] = useState('');
  const [lastBuildPrompt, setLastBuildPrompt] = useState('');
  const [events, setEvents] = useState<BuildEvent[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [artifactPreview, setArtifactPreview] = useState('');
  const [newArtifactNames, setNewArtifactNames] = useState<Set<string>>(new Set());
  const [stageSchema, setStageSchema] = useState<StageSchemaItem[]>([]);
  const [pdkOptions, setPdkOptions] = useState<PdkOption[]>([]);
  const [pdkProfile, setPdkProfile] = useState('sky130');
  const [flowProfile, setFlowProfile] = useState<FlowChoice>('sky130_oss_executable');
  const [modelChoice, setModelChoice] = useState<ModelChoice>('infinite');
  const [humanInLoop, setHumanInLoop] = useState(false);
  const [profile, setProfile] = useState<{ has_byok_key?: boolean; plan_type?: string } | null>(null);
  const [billingMode, setBillingMode] = useState<BillingMode>('byok');
  const [showBillingModal, setShowBillingModal] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [jobId, setJobId] = useState('');
  const [designName, setDesignName] = useState('');
  const [jobStatus, setJobStatus] = useState<JobStatus>('queued');
  const [thinking, setThinking] = useState('');
  const [isChatting, setIsChatting] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<any>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const artifactFetchAt = useRef(0);
  const userPickedArtifact = useRef(false);
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);

  const activeStages = stageSchema.length ? stageSchema : FALLBACK_STAGES;
  const currentEvent = [...events].reverse().find((event) => eventState(event) !== 'SYSTEM');
  const currentStage = currentEvent ? eventState(currentEvent) : 'INIT';
  const currentStageIndex = Math.max(0, activeStages.findIndex((stage) => stage.state === currentStage));
  const currentStageLabel = activeStages.find((stage) => stage.state === currentStage)?.label || currentStage;
  const selectedPdk = pdkOptions.find((pdk) => pdk.key === pdkProfile);
  const selectedPdkCanHarden = pdkCanHarden(selectedPdk);
  const skipOpenlane = !selectedPdkCanHarden;
  const selectedPdkMode = pdkReadinessLabel(selectedPdk);
  const hasByok = Boolean(profile?.has_byok_key) || Boolean(readByokPayload());
  const byokLabel = modelLabel();
  const isBusy = phase === 'building' || isChatting;
  const visibleArtifacts = useMemo(() => {
    const priority = ['.v', '.sv', '.sby', '.sdc', '.gds', '.def', '.lef', '.rpt', '.pdf', '.docx', '.json', '.log'];
    return [...artifacts].sort((a, b) => {
      const ap = priority.findIndex((ext) => a.name.toLowerCase().endsWith(ext));
      const bp = priority.findIndex((ext) => b.name.toLowerCase().endsWith(ext));
      return (ap === -1 ? 99 : ap) - (bp === -1 ? 99 : bp);
    });
  }, [artifacts]);
  const groupedArtifacts = useMemo(() => artifactSections(visibleArtifacts), [visibleArtifacts]);
  const completedStages = useMemo(() => {
    const done = new Set<string>();
    events.forEach((event) => {
      if (event.type === 'stage_complete') done.add(eventState(event));
      if (event.status === 'done' && eventState(event) !== 'SYSTEM') done.add(eventState(event));
    });
    if (phase !== 'idle' && currentStageIndex > 0) {
      for (let index = 0; index < currentStageIndex; index += 1) {
        done.add(activeStages[index].state);
      }
    }
    if (jobStatus === 'done') activeStages.forEach((stage) => done.add(stage.state));
    return done;
  }, [activeStages, currentStageIndex, events, jobStatus, phase]);
  const progress = jobStatus === 'done'
    ? 100
    : phase === 'building'
      ? Math.min(98, Math.round(((currentStageIndex + 1) / Math.max(activeStages.length, 1)) * 100))
      : 0;
  const exactIssues = useMemo(() => events.filter((event) => classifyEvent(event) === 'error').slice(-5), [events]);
  const liveFeed = useMemo(() => {
    const eventRows = events
      .filter((event) => event.type !== 'ping')
      .slice(-120);
    return eventRows;
  }, [events]);

  const fetchArtifacts = useCallback(async (targetDesign = designName, force = false) => {
    if (!targetDesign) return;
    const now = Date.now();
    if (!force && now - artifactFetchAt.current < 1200) return;
    artifactFetchAt.current = now;
    try {
      const res = await api.get(`/build/artifacts/${targetDesign}`);
      const incoming: Artifact[] = uniqueArtifacts(Array.isArray(res.data?.artifacts) ? res.data.artifacts : []);
      setArtifacts((previous) => {
        const previousNames = new Set(previous.map((artifact) => artifact.name));
        const created = incoming.map((artifact) => artifact.name).filter((name) => !previousNames.has(name));
        if (created.length) {
          setNewArtifactNames((current) => new Set([...Array.from(current), ...created]));
          userPickedArtifact.current = false;
        }
        return incoming;
      });
    } catch {
      // Early misses are expected while the backend creates the workspace.
    }
  }, [designName]);

  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, events.length, thinking]);

  useEffect(() => {
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
      }

      if (pdksRes.status === 'fulfilled') {
        const pdks: PdkOption[] = pdksRes.value.data?.pdks || [];
        const defaultPdk = pdksRes.value.data?.default || 'sky130';
        const readyDefault = pdks.find((pdk) => pdk.key === defaultPdk && pdkCanHarden(pdk));
        const firstReady = pdks.find((pdk) => pdkCanHarden(pdk));
        setPdkOptions(pdks);
        setPdkProfile(readyDefault?.key || firstReady?.key || defaultPdk);
      }
    });

    const initialPrompt = localStorage.getItem('agentic_studio_initial_prompt');
    const initialPdk = localStorage.getItem('agentic_studio_initial_pdk');
    if (initialPdk) {
      setPdkProfile(initialPdk);
      localStorage.removeItem('agentic_studio_initial_pdk');
    }
    if (initialPrompt) {
      const draft = makeDraftFromPrompt(initialPrompt, initialPdk || 'sky130');
      setPrompt(initialPrompt);
      setDraftSpec(draft.spec);
      setDraftSummary(draft.summary);
      setDesignName(suggestDesignName(draft.spec));
      setMessages((previous) => [...previous, { role: 'assistant', content: advisorReply(initialPrompt, initialPdk || 'sky130', draft) }]);
      localStorage.removeItem('agentic_studio_initial_prompt');
    }

    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (profile?.plan_type === 'byok' || profile?.has_byok_key || readByokPayload()) {
      setModelChoice('byok');
    }
  }, [profile]);

  useEffect(() => {
    api.get('/pipeline/schema', { params: { flow_profile: flowProfile, pdk_profile: pdkProfile } })
      .then((res) => setStageSchema(res.data?.stages || []))
      .catch(() => setStageSchema([]));
  }, [flowProfile, pdkProfile]);

  useEffect(() => {
    if (phase !== 'building' || !designName) return;
    const interval = window.setInterval(() => {
      void fetchArtifacts(designName);
    }, 1800);
    return () => window.clearInterval(interval);
  }, [phase, designName, fetchArtifacts]);

  useEffect(() => {
    if (!newArtifactNames.size) return;
    const timer = window.setTimeout(() => setNewArtifactNames(new Set()), 8000);
    return () => window.clearTimeout(timer);
  }, [newArtifactNames]);

  useEffect(() => {
    if (!selectedArtifact || !designName) {
      setArtifactPreview('');
      return;
    }
    if (!isTextArtifact(selectedArtifact.name)) {
      setArtifactPreview('Binary artifact. Use the generated package or report download from the artifact endpoint.');
      return;
    }
    api.get(`/build/artifacts/${designName}/${encodeURIComponent(selectedArtifact.name)}`, { responseType: 'text' })
      .then((res) => {
        const raw = typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2);
        if (isLogArtifact(selectedArtifact.name)) {
          setArtifactPreview(summarizeLogPreview(raw, selectedArtifact.name));
          return;
        }
        setArtifactPreview(raw.length > 20000 ? `${raw.slice(0, 20000)}\n\n[Preview truncated]` : raw);
      })
      .catch(() => setArtifactPreview('Preview unavailable. The file may still be streaming into the workspace.'));
  }, [selectedArtifact, designName]);

  useEffect(() => {
    if (!visibleArtifacts.length) {
      setSelectedArtifact(null);
      return;
    }
    const selectedStillExists = selectedArtifact && visibleArtifacts.some((artifact) => artifact.name === selectedArtifact.name);
    if (selectedStillExists && userPickedArtifact.current) return;
    const best = pickBestArtifact(visibleArtifacts, currentStage);
    if (best && (!selectedArtifact || selectedArtifact.name !== best.name)) {
      setSelectedArtifact(best);
    }
  }, [currentStage, selectedArtifact, visibleArtifacts]);

  useEffect(() => {
    if (!editorRef.current || !monacoRef.current) return;
    const model = editorRef.current.getModel();
    const markers = parseDiagnostics(events, selectedArtifact?.name);
    monacoRef.current.editor.setModelMarkers(model, 'agentic-pipeline', markers);
  }, [events, selectedArtifact]);

  const addAssistant = (content: string, tone: ChatMessage['tone'] = 'normal') => {
    setMessages((previous) => [...previous, { role: 'assistant', content, tone }]);
  };

  const addUser = (content: string) => {
    setMessages((previous) => [...previous, { role: 'user', content }]);
  };

  const requestByokSetup = () => {
    setBillingMode('byok');
    setShowBillingModal(true);
  };

  const proposeDraft = (text: string, nextMessages?: ChatMessage[]) => {
    const draft = makeDraftFromPrompt(text, pdkProfile);
    setDraftSpec(draft.spec);
    setDraftSummary(draft.summary);
    setLastBuildPrompt(draft.spec);
    if (!designName.trim()) setDesignName(suggestDesignName(draft.spec));
    setMessages((previous) => [
      ...(nextMessages || previous),
      { role: 'assistant', content: advisorReply(text, pdkProfile, draft) },
    ]);
  };

  const sendChatMessage = async (text: string, nextMessages: ChatMessage[]) => {
    if (modelChoice === 'byok' && !hasByok) {
      requestByokSetup();
      return;
    }

    setIsChatting(true);
    setThinking('Reasoning over your message with the selected model route.');
    try {
      const res = await api.post('/chat/converse', {
        messages: nextMessages.map((message) => ({ role: message.role, content: message.content })),
        plan_type: modelChoice === 'infinite' ? 'agentic_paid' : 'byok',
        api_key: modelChoice === 'byok' ? readByokPayload() : null,
        pdk_profile: pdkProfile,
      });
      setMessages((previous) => [...previous, { role: 'assistant', content: res.data?.reply || 'I am ready. Describe the chip block and I will start the VLSI flow.' }]);
    } catch (err: unknown) {
      const detail = typeof err === 'object' && err !== null && 'response' in err
        ? (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
        : undefined;
      setMessages((previous) => [...previous, { role: 'assistant', tone: 'error', content: toUserError(detail, 'The selected model route did not return a response. Check the provider/model settings or try again.') }]);
    } finally {
      setThinking('');
      setIsChatting(false);
    }
  };

  const launch = async (descriptionOverride?: string, visibleUserPrompt?: string) => {
    const description = normalizePrompt(descriptionOverride || prompt.trim() || draftSpec.trim() || lastBuildPrompt.trim());
    if (!description || phase === 'building') return;

    if (!isBuildReadyPrompt(description)) {
      const nextMessages = visibleUserPrompt ? [...messages, { role: 'user', content: visibleUserPrompt }] : messages;
      proposeDraft(description, nextMessages);
      setPrompt('');
      return;
    }

    if (modelChoice === 'byok' && !hasByok) {
      requestByokSetup();
      return;
    }

    const nextDesignName = (designName.trim() || suggestDesignName(description) || 'agentic_chip').slice(0, 64);
    const byok = modelChoice === 'byok' ? readByokPayload() : null;
    const shouldSkipOpenlane = skipOpenlane;
    const pdkModeNote = shouldSkipOpenlane
      ? ` ${pdkProfile} is in ${selectedPdkMode.toLowerCase()}, so hardening stays gated off until the backend exposes a verified physical-flow profile.`
      : '';

    abortRef.current?.abort();
    userPickedArtifact.current = false;
    artifactFetchAt.current = 0;
    setDraftSpec('');
    setDraftSummary('');
    setLastBuildPrompt(description);
    setPrompt('');
    setPhase('building');
    setError('');
    setResult(null);
    setEvents([]);
    setArtifacts([]);
    setSelectedArtifact(null);
    setArtifactPreview('');
    setNewArtifactNames(new Set());
    setJobStatus('queued');
    setThinking('Starting AgentIC build pipeline.');

    if (visibleUserPrompt) addUser(visibleUserPrompt);
    addAssistant(
      `Starting **${nextDesignName}**. AgentIC is planning the chip, running the VLSI pipeline, and will summarize each stage as files and evidence arrive.${pdkModeNote}`,
    );

    try {
      const res = await api.post('/build', {
        design_name: nextDesignName,
        description,
        skip_openlane: shouldSkipOpenlane,
        skip_coverage: false,
        show_thinking: true,
        flow_profile: flowProfile,
        max_retries: 5,
        min_coverage: 80.0,
        pdk_profile: pdkProfile,
        plan_type: modelChoice === 'infinite' ? 'agentic_paid' : 'byok',
        api_key: byok,
        human_in_loop: humanInLoop,
      });

      const activeJobId = res.data.job_id;
      const activeDesignName = res.data.design_name || nextDesignName;
      setJobId(activeJobId);
      setDesignName(activeDesignName);
      setJobStatus('running');
      void stream(activeJobId, byok, activeDesignName);
      void fetchArtifacts(activeDesignName, true);
    } catch (err: unknown) {
      setPhase('idle');
      setThinking('');
      if (isNetworkError(err)) {
        setError('Unable to connect to the build service.');
      } else {
        const detail = typeof err === 'object' && err !== null && 'response' in err
          ? (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
          : undefined;
        const msg = toUserError(detail, 'Build failed to start.');
        setError(msg);
        addAssistant(`**Build could not start**\n\n${msg}`, 'error');
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
            void finish(data.status || 'failed', activeJobId, activeDesignName);
            return;
          }

          if (data.type === 'agent_thinking') {
            setThinking(data.message || data.content || 'Reasoning through the next pipeline action.');
          } else if (data.type === 'stall_warning') {
            setThinking('');
          } else {
            setThinking('');
          }

          setEvents((previous) => {
            const duplicate = previous.some((item) => (
              item.timestamp === data.timestamp
              && item.type === data.type
              && eventText(item) === eventText(data)
              && eventState(item) === eventState(data)
            ));
            return duplicate ? previous : [...previous, data];
          });

          const kind = classifyEvent(data);
          if (kind === 'error') {
            const msg = eventText(data) || 'The pipeline reported an error.';
            setError(msg);
          } else if (kind === 'fix') {
            setError('');
          }
          setJobStatus(kind === 'error' && data.type === 'error' ? 'failed' : 'running');
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
          void finish('failed', activeJobId, activeDesignName);
          throw err;
        }
        return Math.min(1000 * retries, 5000);
      },
    }).catch(() => {
      if (ctrl.signal.aborted) return;
      setError('Live connection lost. Checking final build status.');
      void finish('failed', activeJobId, activeDesignName);
    });
  };

  const finish = async (status: string, activeJobId: string, activeDesignName: string) => {
    const finalStatus: JobStatus = status === 'done' ? 'done' : status === 'cancelled' ? 'cancelled' : 'failed';
    setJobStatus(finalStatus);
    setPhase('done');
    setThinking('');
    await fetchArtifacts(activeDesignName, true);

    try {
      const res = await api.get(`/build/result/${activeJobId}`);
      setResult(res.data || null);
    } catch {
      setResult(null);
    }

    addAssistant(
      finalStatus === 'done'
        ? '**Build complete.** The pipeline gates passed and the generated chip workspace is ready: RTL, verification collateral, reports, and physical artifacts when enabled.'
        : `**Build stopped with status ${finalStatus}.** The exact errors and partial files are preserved so the next prompt can continue from evidence.`,
      finalStatus === 'done' ? 'success' : 'error',
    );
  };

  const cancel = async () => {
    if (!jobId) return;
    try {
      await api.post(`/build/cancel/${jobId}`);
      setJobStatus('cancelling');
      addAssistant('Cancellation requested. Generated files and logs will remain visible.', 'normal');
    } catch {
      setError('Unable to cancel this run.');
    }
  };

  const reset = () => {
    abortRef.current?.abort();
    userPickedArtifact.current = false;
    setPhase('idle');
    setEvents([]);
    setArtifacts([]);
    setSelectedArtifact(null);
    setArtifactPreview('');
    setNewArtifactNames(new Set());
    setThinking('');
    setError('');
    setResult(null);
    setJobId('');
    setDesignName('');
    setJobStatus('queued');
  };

  const handlePrimaryAction = async () => {
    const text = prompt.trim();
    if (isBusy) return;

    if (!text && draftSpec.trim()) {
      await launch(draftSpec, 'build it');
      return;
    }
    if (!text) return;

    if (isConfirmationPrompt(text) && draftSpec.trim()) {
      setPrompt('');
      await launch(draftSpec, text);
      return;
    }

    const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: text }];
    setPrompt('');

    if (isBuildReadyPrompt(text)) {
      const draft = makeDraftFromPrompt(text, pdkProfile);
      await launch(draft.spec, text);
      return;
    }

    if (hasHardwareIntent(text)) {
      const draft = makeDraftFromPrompt(text, pdkProfile);
      await launch(draft.spec, text);
      return;
    }

    setMessages(nextMessages);
    await sendChatMessage(text, nextMessages);
  };

  const handleEditorDidMount = (editor: any, monaco: any) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
  };

  const failedStage = jobStatus === 'failed' ? currentStage : undefined;
  const activeTool = liveFeed
    .slice()
    .reverse()
    .find((event) => ['tool-call', 'tool-result', 'fix', 'error'].includes(classifyEvent(event)));

  return (
    <div className="codex-vlsi-root">
      <aside className="codex-vlsi-chat">
        <div className="codex-vlsi-chat-head">
          <div>
            <div className="codex-vlsi-title">AgentIC Studio</div>
            <div className="codex-vlsi-subtitle">Describe the chip. Watch the build.</div>
          </div>
        </div>

        <div className="codex-vlsi-thread">
          {messages.map((message, index) => (
            <motion.article
              key={`${message.role}-${index}`}
              className={`codex-vlsi-message ${message.role} ${message.tone || ''}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16 }}
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            </motion.article>
          ))}

          {liveFeed.map((event, index) => {
            const kind = classifyEvent(event);
            const Icon =
              kind === 'error' ? AlertTriangle :
              kind === 'fix' ? Wrench :
              kind === 'file' ? FileText :
              kind === 'stage' ? CheckCircle2 :
              kind === 'tool-call' || kind === 'tool-result' ? Terminal :
              kind === 'decision' ? GitBranch :
              Activity;
            return (
              <motion.article
                key={`${event.timestamp || index}-${event.type}-${index}`}
                className={`codex-vlsi-event is-${kind}`}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.14 }}
              >
                <div className="codex-vlsi-event-head">
                  <Icon size={13} />
                  <span>{eventTitle(event)}</span>
                </div>
                <pre>{displayEventText(event)}</pre>
              </motion.article>
            );
          })}

          {(thinking || phase === 'building') && (
            <div className="codex-vlsi-thinking">
              <span className="codex-vlsi-pulse" />
              <span>{thinking || 'AgentIC is running the pipeline.'}</span>
            </div>
          )}
          <div ref={scrollRef} />
        </div>

        <div className="codex-vlsi-composer">
          {draftSpec && phase === 'idle' && (
            <div className="codex-vlsi-draft">
              <div className="codex-vlsi-draft-label">Draft ready</div>
              <p>{draftSummary}</p>
              <span>Type "build it" to start, or revise the chip request.</span>
            </div>
          )}
          <div className="codex-vlsi-routebar">
            <div className="codex-vlsi-route-label">Model route</div>
            <div className="codex-vlsi-route-pills" role="group" aria-label="Model route">
              <button
                type="button"
                className={modelChoice === 'infinite' ? 'active' : ''}
                onClick={() => setModelChoice('infinite')}
                disabled={phase === 'building'}
              >
                <Sparkles size={13} />
                Infinite
              </button>
              <button
                type="button"
                className={modelChoice === 'byok' ? 'active' : ''}
                onClick={() => (hasByok ? setModelChoice('byok') : requestByokSetup())}
                disabled={phase === 'building'}
              >
                <KeyRound size={13} />
                BYOK
              </button>
            </div>
            <button
              type="button"
              className={`codex-vlsi-model-chip ${modelChoice === 'byok' ? 'is-byok' : ''}`}
              onClick={requestByokSetup}
              disabled={phase === 'building'}
              title="Choose provider, model, base URL, and API key"
            >
              {modelChoice === 'byok'
                ? (hasByok ? byokLabel : 'Choose provider/model')
                : 'Managed model'}
            </button>
          </div>
          <div className="codex-vlsi-input-shell">
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="Describe the chip, fix direction, or next iteration..."
              rows={3}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  void handlePrimaryAction();
                }
              }}
            />
            <button
              type="button"
              className="codex-vlsi-send"
              onClick={() => void handlePrimaryAction()}
              disabled={isBusy || !(prompt.trim() || draftSpec.trim())}
              aria-label="Send prompt"
              title="Send prompt"
            >
              <ArrowUp size={17} />
            </button>
          </div>
          <div className="codex-vlsi-composer-meta">
            <span>PDK {pdkProfile}</span>
            <span>{selectedPdkMode}</span>
            <span className={humanInLoop ? 'is-manual' : 'is-auto'}>{humanInLoop ? 'HITL' : 'Autonomous'}</span>
            <button type="button" onClick={() => setShowAdvanced((value) => !value)} title="Workspace settings">
              <Settings2 size={13} />
            </button>
          </div>
          {showAdvanced && (
            <div className="codex-vlsi-advanced">
              <label>
                <span>PDK</span>
                <select value={pdkProfile} onChange={(event) => setPdkProfile(event.target.value)} disabled={phase === 'building'}>
                  {(pdkOptions.length ? pdkOptions : [{ key: 'sky130', gds_ready: true, can_harden: true }]).map((pdk) => (
                    <option key={pdk.key} value={pdk.key}>
                      {pdk.key} - {pdkReadinessLabel(pdk)}
                    </option>
                  ))}
                </select>
              </label>
              {selectedPdk && !selectedPdkCanHarden && (
                <div className="codex-vlsi-pdk-note">
                  {selectedPdk.reason || 'This PDK is available for exploration, but physical hardening is disabled until a verified flow is configured.'}
                </div>
              )}
              <label>
                <span>Model route</span>
                <select value={modelChoice} onChange={(event) => setModelChoice(event.target.value as ModelChoice)} disabled={phase === 'building'}>
                  <option value="infinite">Hosted AgentIC</option>
                  <option value="byok">BYOK</option>
                </select>
              </label>
              <label>
                <span>Flow</span>
                <select value={flowProfile} onChange={(event) => setFlowProfile(event.target.value as FlowChoice)} disabled={phase === 'building'}>
                  <option value="sky130_oss_executable">OSS full pipeline</option>
                  <option value="sky130_oss_experimental_complete">OSS experimental extensions</option>
                </select>
              </label>
              <label className="codex-vlsi-check">
                <input
                  type="checkbox"
                  checked={humanInLoop}
                  disabled={phase === 'building'}
                  onChange={(event) => setHumanInLoop(event.target.checked)}
                />
                <span>Pause for human approvals</span>
              </label>
              <button type="button" className="codex-vlsi-secondary" onClick={requestByokSetup}>
                Configure model keys
              </button>
            </div>
          )}
        </div>
      </aside>

      <main className="codex-vlsi-workspace">
        <header className="codex-vlsi-workspace-head">
          <div>
            <div className="codex-vlsi-kicker">Live workspace</div>
            <h2>{designName || 'No active chip yet'}</h2>
          </div>
          <div className="codex-vlsi-run-state">
            <span className={`codex-vlsi-state-dot is-${jobStatus}`} />
            <span>{phase === 'idle' ? 'Ready' : jobStatus}</span>
            {phase === 'building' && (
              <button type="button" onClick={cancel} title="Cancel build" aria-label="Cancel build">
                <CircleStop size={15} />
              </button>
            )}
            {phase === 'done' && (
              <button type="button" onClick={reset}>New run</button>
            )}
          </div>
        </header>

        <div className="codex-vlsi-stage-band">
          <div className="codex-vlsi-stage-status">
            <span>{currentStageLabel}</span>
            <p>{thinking || STAGE_NOTES[currentStage] || 'Waiting for a chip request.'}</p>
          </div>
          <div className="codex-vlsi-progress">
            <div style={{ width: `${progress}%` }} />
          </div>
          <span className="codex-vlsi-progress-text">{progress}%</span>
        </div>

        <div className="codex-vlsi-body">
          <aside className="codex-vlsi-files">
            <div className="codex-vlsi-files-head">
              <span>Files</span>
              <small>{visibleArtifacts.length}</small>
            </div>
            <div className="codex-vlsi-file-tree">
              {groupedArtifacts.map((section) => (
                <div key={section.label} className="codex-vlsi-file-section">
                  <div className="codex-vlsi-file-section-label">{section.label}</div>
                  {section.files.map((artifact) => (
                    <button
                      key={artifact.name}
                      type="button"
                      className={`codex-vlsi-file ${selectedArtifact?.name === artifact.name ? 'active' : ''} ${newArtifactNames.has(artifact.name) ? 'is-new' : ''}`}
                      onClick={() => {
                        userPickedArtifact.current = true;
                        setArtifactPreview('');
                        setSelectedArtifact(artifact);
                      }}
                      title={artifact.name}
                    >
                      <FileText size={12} />
                      <span>{artifact.name}</span>
                      {artifact.size ? <small>{formatBytes(artifact.size)}</small> : null}
                    </button>
                  ))}
                </div>
              ))}
              {!groupedArtifacts.length && (
                <div className="codex-vlsi-empty-files">
                  Generated RTL, verification, layout, and reports will appear here.
                </div>
              )}
            </div>
          </aside>

          <section className="codex-vlsi-editor">
            <div className="codex-vlsi-editor-tabs">
              <div className="codex-vlsi-editor-tab">
                <Code2 size={14} />
                <span>{selectedArtifact?.name || 'Workspace preview'}</span>
              </div>
              {newArtifactNames.size > 0 && (
                <span className="codex-vlsi-new-files">{newArtifactNames.size} new</span>
              )}
            </div>
            <div className="codex-vlsi-editor-content">
              {selectedArtifact ? (
                <Editor
                  height="100%"
                  language={artifactLanguage(selectedArtifact.name)}
                  theme="vs-dark"
                  value={artifactPreview || 'Loading preview...'}
                  onMount={handleEditorDidMount}
                  options={{
                    readOnly: true,
                    minimap: { enabled: false },
                    fontSize: 13,
                    fontFamily: "'Geist Mono', 'Fira Code', monospace",
                    scrollBeyondLastLine: false,
                    smoothScrolling: true,
                    wordWrap: 'on',
                    padding: { top: 12, bottom: 12 },
                  }}
                />
              ) : (
                <div className="codex-vlsi-editor-empty">
                  <Code2 size={22} />
                  <span>Prompt AgentIC to build a chip. Files will open here as they are created.</span>
                </div>
              )}
            </div>
          </section>
        </div>
      </main>

      <aside className="codex-vlsi-inspector">
        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Pipeline</div>
          <div className="codex-vlsi-stage-list">
            {activeStages.map((stage) => {
              const completed = completedStages.has(stage.state);
              const active = stage.state === currentStage && phase !== 'idle';
              const failed = failedStage === stage.state;
              return (
                <div
                  key={stage.state}
                  className={`codex-vlsi-stage-row ${completed ? 'done' : ''} ${active ? 'active' : ''} ${failed ? 'failed' : ''}`}
                >
                  <span>{stage.icon}</span>
                  <div>
                    <strong>{stage.label}</strong>
                    {active && <small>{STAGE_NOTES[stage.state]}</small>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Active signal</div>
          <div className="codex-vlsi-signal">
            <Terminal size={14} />
            <div>
              <strong>{activeTool ? eventTitle(activeTool) : 'Waiting'}</strong>
              <span>{activeTool ? eventText(activeTool).slice(0, 180) : 'No tool output yet.'}</span>
            </div>
          </div>
        </div>

        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Exact errors</div>
          {error && (
            <div className="codex-vlsi-issues codex-vlsi-issues-current">
              <pre>{error}</pre>
            </div>
          )}
          {exactIssues.length ? (
            <div className="codex-vlsi-issues">
              {exactIssues.map((issue, index) => (
                <pre key={`${issue.timestamp || index}-${index}`}>{eventText(issue)}</pre>
              ))}
            </div>
          ) : !error && (
            <div className="codex-vlsi-clear">
              <CheckCircle2 size={15} />
              <span>{phase === 'building' ? 'No current blocking error in the stream.' : 'No active build errors.'}</span>
            </div>
          )}
        </div>

        {result && (
          <div className="codex-vlsi-inspector-section">
            <div className="codex-vlsi-inspector-title">Result</div>
            <div className={`codex-vlsi-result ${jobStatus === 'done' ? 'success' : 'failed'}`}>
              <strong>{jobStatus === 'done' ? 'Gates passed' : 'Fail closed'}</strong>
              <span>
                {result.failure_explanation || result.failure_suggestion || result.state || 'Final build result captured.'}
              </span>
            </div>
          </div>
        )}
      </aside>

      <BillingModal
        isOpen={showBillingModal}
        onClose={() => setShowBillingModal(false)}
        initialMode={billingMode}
        onKeySaved={() => {
          setModelChoice('byok');
          setProfile((previous) => ({ ...(previous || {}), has_byok_key: true }));
          setShowBillingModal(false);
        }}
      />
    </div>
  );
};

export default DesignStudio;
