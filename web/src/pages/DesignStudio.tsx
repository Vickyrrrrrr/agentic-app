/* eslint-disable @typescript-eslint/ban-ts-comment */
// @ts-nocheck
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import '../studio-3pane.css';
import { motion } from 'framer-motion';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import Editor from '@monaco-editor/react';
import { DiagramViewer } from '../components/DiagramViewer';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Activity,
  AlertTriangle,
  ArrowUp,
  CheckCircle2,
  Code2,
  FileText,
  History,
  Settings2,
  Terminal,
  ChevronRight,
  ChevronDown,
  Folder,
  FolderOpen,
  Plus,
  Square,
  Trash2,
  X,
  Copy,
  Edit2,
  PanelLeftClose,
  PanelLeftOpen,
  Maximize,
  Minimize,
} from 'lucide-react';
import { BillingModal } from '../components/BillingModal';
import { api, API_BASE, getSseHeaders } from '../api';
import { toUserError } from '../utils/errorFormatter';

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  tone?: 'normal' | 'success' | 'error';
}

interface ChatConversation {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  designName?: string;
  messages: ChatMessage[];
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

interface LicenseStatus {
  active?: boolean;
  plan?: string;
  reason?: string;
}

interface ToolStatus {
  capability_tier?: string;
  capabilities?: Record<string, boolean>;
  missing?: Array<{ capability: string; tools: string[] }>;
  tools?: Record<string, boolean>;
  adapters?: Record<string, { env: string; tools: string[]; available: string[] }>;
  license_env?: Record<string, boolean>;
}

interface InstallPlan {
  capability: string;
  tool: string;
  strategy: string;
  command: string;
  target: string;
  requires_admin: boolean;
  purpose: string;
  options?: Array<{ key: string; label: string; description?: string }>;
}

interface RunEvent {
  run_id?: string;
  timestamp?: number;
  type?: string;
  label?: string;
  stage?: string;
  status?: string;
  design_name?: string;
}

interface DesignStudioProps {
  licenseStatus?: LicenseStatus | null;
  toolStatus?: ToolStatus | null;
  selectedDesign?: string;
  onActiveDesignChange?: (designName: string) => void;
}

const HELP_RE = /\b(help|what can you do|capabilit|possible|not possible|can you|how do i|suggest|prompt)\b/i;
const ERROR_RE = /(%error|%warning|syntax error|parse error|error:|fatal|failed|traceback|critical|lint|violation|unmapped|pinmissing|pinnotfound)/i;
const CASUAL_RE = /^(hi+|hello+|hey+|yo+|sup|thanks|thank you|ok|okay|test)$/i;
const CHAT_HISTORY_STORAGE_KEY = 'agentic_chat_conversations_v1';
const ACTIVE_CHAT_STORAGE_KEY = 'agentic_active_conversation_id';
const HISTORY_COLLAPSED_STORAGE_KEY = 'agentic_history_collapsed';
const MAX_LOCAL_CONVERSATIONS = 40;
const COLLAPSIBLE_MESSAGE_CHARS = 720;
const COLLAPSIBLE_MESSAGE_LINES = 8;
const WORKSPACE_SECTION_NAMES = new Set([
  'rtl', 'tb', 'dv', 'sim', 'synth', 'pnr', 'sta', 'reports',
  'constraints', 'formal', 'layout', 'logs', 'scripts', 'hardening',
  'signoff', 'openlane', 'openroad', 'runs',
]);

function safeNow(): number {
  return Date.now();
}

function createConversation(): ChatConversation {
  const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `conv-${safeNow()}-${Math.random().toString(16).slice(2)}`;
  const now = safeNow();
  return {
    id,
    title: 'New conversation',
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

function conversationTitle(messages: ChatMessage[], fallback = 'New conversation'): string {
  const firstUser = messages.find((message) => message.role === 'user')?.content?.trim();
  if (!firstUser) return fallback;
  const singleLine = firstUser.replace(/\s+/g, ' ');
  return singleLine.length > 58 ? `${singleLine.slice(0, 55)}...` : singleLine;
}

function formatConversationAge(updatedAt: number): string {
  const diff = Math.max(0, safeNow() - Number(updatedAt || safeNow()));
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return 'now';
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function isWorkspaceSectionName(name?: string): boolean {
  return Boolean(name && WORKSPACE_SECTION_NAMES.has(String(name).trim().toLowerCase()));
}

function normalizeActiveDesignName(name?: string): string {
  const trimmed = String(name || '').trim();
  return isWorkspaceSectionName(trimmed) ? '' : trimmed;
}

function artifactPath(path: string): string {
  return String(path || '')
    .split('/')
    .map((part) => encodeURIComponent(part))
    .join('/');
}

function loadConversations(): ChatConversation[] {
  if (typeof window === 'undefined') return [createConversation()];
  try {
    const parsed = JSON.parse(localStorage.getItem(CHAT_HISTORY_STORAGE_KEY) || '[]');
    if (!Array.isArray(parsed)) return [createConversation()];
    const conversations = parsed
      .filter((item) => item?.id && Array.isArray(item.messages))
      .map((item) => ({
        id: String(item.id),
        title: String(item.title || conversationTitle(item.messages)),
        createdAt: Number(item.createdAt || safeNow()),
        updatedAt: Number(item.updatedAt || item.createdAt || safeNow()),
        designName: item.designName ? String(item.designName) : undefined,
        messages: item.messages
          .filter((message: ChatMessage) => message?.role === 'user' || message?.role === 'assistant')
          .map((message: ChatMessage) => ({
            role: message.role,
            content: String(message.content || ''),
            tone: message.tone,
          })),
      }))
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_LOCAL_CONVERSATIONS);
    return conversations.length ? conversations : [createConversation()];
  } catch {
    return [createConversation()];
  }
}

function saveConversations(conversations: ChatConversation[]): void {
  if (typeof window === 'undefined') return;
  const safe = conversations
    .filter((conversation) => conversation.messages.length > 0 || conversation.title === 'New conversation')
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_LOCAL_CONVERSATIONS);
  localStorage.setItem(CHAT_HISTORY_STORAGE_KEY, JSON.stringify(safe));
}

function initialChatState() {
  const conversations = loadConversations();
  const storedActiveId = typeof window !== 'undefined'
    ? localStorage.getItem(ACTIVE_CHAT_STORAGE_KEY)
    : null;
  const active = conversations.find((conversation) => conversation.id === storedActiveId) || conversations[0];
  return {
    conversations,
    activeConversationId: active.id,
    messages: active.messages,
  };
}

function isTextArtifact(name: string): boolean {
  return /\.(v|sv|svh|vh|sby|sdc|tcl|json|md|txt|log|rpt|csv|ys|cfg|lef|def|lib|spice|sp)$/i.test(name);
}

function isSourceFile(name: string): boolean {
  // Hide only compiler/build artifacts — show everything else including VCDs, executables, etc.
  if (/\.(o|a|so|d|mk)$/i.test(name)) return false;
  if (name.startsWith('obj_dir/') || name.includes('/obj_dir/')) return false;
  return true;
}

function isLogArtifact(name: string): boolean {
  return /\.(log|txt)$/i.test(name) || /(^|[/_-])(log|stdout|stderr)([/_.-]|$)/i.test(name);
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
    // Check directory in path — matches "project/rtl/" or just "rtl/" layouts
  if (/\/rtl\//.test(name) || name.startsWith('rtl/')) return 'rtl';
  if (/\/tb\//.test(name) || name.startsWith('tb/')) return 'verification';
  if (/\/dv\//.test(name) || name.startsWith('dv/')) return 'verification';
  if (/\/synth\//.test(name) || name.startsWith('synth/')) return 'synthesis';
  if (/\/hardening\//.test(name) || name.startsWith('hardening/')) return 'hardening';
  if (/\/openlane\//.test(name) || name.startsWith('openlane/')) return 'hardening';
  if (/\/openroad\//.test(name) || name.startsWith('openroad/')) return 'hardening';
  if (/\/runs\//.test(name) || name.startsWith('runs/')) return 'hardening';
  if (/\/pnr\//.test(name) || name.startsWith('pnr/')) return 'physical';
  if (/\/layout\//.test(name) || name.startsWith('layout/')) return 'physical';
  if (/\/sta\//.test(name) || name.startsWith('sta/')) return 'timing';
  if (/\/signoff\//.test(name) || name.startsWith('signoff/')) return 'signoff';
  if (/\/sim\//.test(name) || name.startsWith('sim/')) return 'simulation';
  if (/\/reports\//.test(name) || name.startsWith('reports/')) return 'report';
  if (/\/scripts\//.test(name) || name.startsWith('scripts/')) return 'script';
  // Fallback to extension-based matching
  if (/\.(v|sv|svh|vh)$/.test(name) && !name.includes('tb')) return 'rtl';
  if (name.includes('tb') || name.endsWith('.sby') || name.includes('formal') || name.endsWith('.vcd')) return 'verification';
  if (name.includes('openlane') || name.includes('openroad') || name.includes('innovus') || name.includes('icc2')) return 'hardening';
  if (name.includes('drc') || name.includes('lvs') || name.includes('erc') || name.includes('signoff')) return 'signoff';
  if (name.endsWith('.gds') || name.endsWith('.def') || name.endsWith('.lef') || name.endsWith('.spef')) return 'physical';
  if (name.endsWith('.sdc') || name.includes('constraint')) return 'constraints';
  if (name.endsWith('.rpt') || name.endsWith('.pdf') || name.endsWith('.docx') || name.includes('report')) return 'report';
  if (name.endsWith('.log')) return 'log';
  if (name.endsWith('.json') || name.endsWith('.cfg') || name.endsWith('.tcl') || name.endsWith('.ys')) return 'config';
  // Binary simulation outputs
  if (name.endsWith('.vcd') || !name.includes('.')) return 'simulation';
  return 'other';
}

function artifactSections(artifacts: Artifact[]) {
  const sections = [
    { label: 'RTL', types: ['rtl'] },
    { label: 'Verification', types: ['verification', 'formal', 'waveform'] },
    { label: 'Constraints', types: ['constraints'] },
    { label: 'Synthesis', types: ['synthesis'] },
    { label: 'Hardening', types: ['hardening'] },
    { label: 'Physical Layout', types: ['physical', 'layout'] },
    { label: 'Timing (STA)', types: ['timing'] },
    { label: 'Signoff', types: ['signoff'] },
    { label: 'Simulation', types: ['simulation'] },
    { label: 'Scripts & Flow', types: ['script', 'config', 'tcl'] },
    { label: 'Reports', types: ['report', 'log', 'other'] },
  ];
  return sections
    .map((section) => ({
      ...section,
      files: artifacts.filter((artifact) => section.types.includes(inferArtifactType(artifact))),
    }))
    .filter((section) => section.files.length > 0);
}

function uniqueArtifacts(files: Artifact[]): Artifact[] {
  const byName = new Map<string, Artifact>();
  files.forEach((artifact) => {
    if (!artifact.name) return;
    byName.set(artifact.name, artifact);
  });
  return Array.from(byName.values());
}

function formatBytes(size?: number): string {
  if (!size) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} KB`;
  return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function readByokPayload(): string | null {
  const raw = localStorage.getItem('agentic_byok_key');
  if (!raw) return null;
  return raw;
}

function readByokConfig(): { apiKey: string; baseUrl: string; model: string } | null {
  const raw = localStorage.getItem('agentic_byok_key');
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    const group1 = parsed?.group1;
    if (group1?.api_key) {
      return {
        apiKey: group1.api_key,
        baseUrl: group1.base_url || 'https://api.openai.com/v1',
        model: group1.model || 'gpt-4o',
      };
    }
  } catch {
    // If it's a raw key string (old format), use defaults
    return { apiKey: raw, baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o' };
  }
  return null;
}

function isCasualPrompt(text: string): boolean {
  return CASUAL_RE.test(text.trim());
}

function cleanUserFacingAgentText(text: string): string {
  return (text || '')
    .replace(/^\s*NEEDS_INPUT:\s*/i, '')
    .replace(/\b(read|write|edit|bash|grep|glob)\s*\([^)]*\)/gis, 'a local workspace step')
    .split('\n')
    .filter((line) => {
      const trimmed = line.trim();
      if (trimmed.startsWith('$ ')) return false;
      if (/[{}]/.test(trimmed) && /\b(command|stdout|stderr|tool-call|tool-result)\b/i.test(trimmed)) return false;
      return true;
    })
    .join('\n')
    .trim();
}

function advisorReply(text: string, _pdkProfile: string): string {
  if (isCasualPrompt(text)) {
    return 'Hi. Tell me the chip or RTL block you want. I will explore the system, tools, and PDK, present a plan, then build and iterate until it works.';
  }
  if (HELP_RE.test(text) || /[?]/.test(text)) {
    return 'I can build synthesizable digital silicon. Tell me what chip or block you want and I will design, simulate, synthesize, and harden it using my 6 tools.\n\nBest prompt: describe the block, its interface, clock/reset, data width, and target PDK.';
  }
  return 'I can help refine that into a buildable chip request. What block are you looking for, and what PDK should I target?';
}

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

export const DesignStudio = ({ licenseStatus, toolStatus, selectedDesign = '', onActiveDesignChange }: DesignStudioProps = {}) => {
  const initialChatRef = useRef<ReturnType<typeof initialChatState> | null>(null);
  if (!initialChatRef.current) {
    initialChatRef.current = initialChatState();
  }
  const [prompt, setPrompt] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>(initialChatRef.current.messages);
  const [collapsedMessages, setCollapsedMessages] = useState<Record<string, boolean>>({});
  const [conversations, setConversations] = useState<ChatConversation[]>(initialChatRef.current.conversations);
  const [activeConversationId, setActiveConversationId] = useState(initialChatRef.current.activeConversationId);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [artifactPreview, setArtifactPreview] = useState('');
  const [newArtifactNames, setNewArtifactNames] = useState<Set<string>>(new Set());
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({});
  const [historyCollapsed, setHistoryCollapsed] = useState(() => {
    if (typeof window === 'undefined') return false;
    return localStorage.getItem(HISTORY_COLLAPSED_STORAGE_KEY) === 'true';
  });
  const [filesCollapsed, setFilesCollapsed] = useState(() => {
    if (typeof window === 'undefined') return false;
    return localStorage.getItem('agentic_files_collapsed') === 'true';
  });
  const [workspaceCollapsed, setWorkspaceCollapsed] = useState(() => {
    if (typeof window === 'undefined') return false;
    const stored = localStorage.getItem('agentic_workspace_collapsed');
    return stored !== null ? stored === 'true' : false;
  });

  const [chatWidth, setChatWidth] = useState(550);
  const [inspectorWidth, setInspectorWidth] = useState(280);
  const [isResizing, setIsResizing] = useState<'chat' | 'inspector' | null>(null);

  useEffect(() => {
    if (!isResizing) return;
    const handleMouseMove = (e: MouseEvent) => {
      if (isResizing === 'chat') {
        setChatWidth(Math.min(Math.max(200, e.clientX), 800));
      } else if (isResizing === 'inspector') {
        setInspectorWidth(Math.min(Math.max(200, window.innerWidth - e.clientX), 800));
      }
    };
    const handleMouseUp = () => setIsResizing(null);
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizing]);

  const [pdkOptions, setPdkOptions] = useState<PdkOption[]>([]);
  const [pdkProfile, setPdkProfile] = useState('');
  const [profile, setProfile] = useState<{ has_byok_key?: boolean } | null>(null);
  const [showBillingModal, setShowBillingModal] = useState(false);
  const [designName, setDesignName] = useState('');
  const [thinking, setThinking] = useState('');
  const [isChatting, setIsChatting] = useState(false);
  const [activeRunId, setActiveRunId] = useState('');
  const [localToolStatus, setLocalToolStatus] = useState<ToolStatus | null>(toolStatus || null);
  const [installPlan, setInstallPlan] = useState<InstallPlan | null>(null);
  const [installing, setInstalling] = useState(false);
  const [installMessage, setInstallMessage] = useState('');
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const artifactFetchAt = useRef(0);
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);

  const selectedPdk = pdkOptions.find((pdk) => pdk.key === pdkProfile);
  const selectedPdkCanHarden = pdkCanHarden(selectedPdk);
  const selectedPdkMode = pdkReadinessLabel(selectedPdk);
  const hasByok = Boolean(profile?.has_byok_key) || Boolean(readByokPayload());
  const isBusy = isChatting;
  const effectiveToolStatus = localToolStatus || toolStatus;
  const missingTools = effectiveToolStatus?.missing || [];
  const toolTier = effectiveToolStatus?.capability_tier || 'checking';
  const licenseActive = licenseStatus?.active !== false;
  const activeConversation = conversations.find((conversation) => conversation.id === activeConversationId);
  const recentConversations = conversations
    .filter((conversation) => conversation.messages.length > 0 || conversation.id === activeConversationId)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, 6);

  const visibleArtifacts = useMemo(() => {
    const priority = ['.v', '.sv', '.sby', '.sdc', '.gds', '.def', '.lef', '.rpt', '.json', '.tcl', '.lib', '.ys', '.cfg'];
    return [...artifacts]
      .filter((a) => isSourceFile(a.name))
      .sort((a, b) => {
        const ap = priority.findIndex((ext) => a.name.toLowerCase().endsWith(ext));
        const bp = priority.findIndex((ext) => b.name.toLowerCase().endsWith(ext));
        return (ap === -1 ? 99 : ap) - (bp === -1 ? 99 : bp);
      });
  }, [artifacts]);
  const groupedArtifacts = useMemo(() => artifactSections(visibleArtifacts), [visibleArtifacts]);

  const fetchArtifacts = useCallback(async (targetDesign = designName, force = false) => {
    const now = Date.now();
    if (!force && now - artifactFetchAt.current < 1200) return;
    artifactFetchAt.current = now;
    try {
      const normalizedTarget = normalizeActiveDesignName(targetDesign);
      const endpoint = normalizedTarget ? `/build/artifacts/${normalizedTarget}` : '/build/artifacts';
      const res = await api.get(endpoint);
      const incoming: Artifact[] = uniqueArtifacts(Array.isArray(res.data) ? res.data : Array.isArray(res.data?.artifacts) ? res.data.artifacts : []);
      setArtifacts((previous) => {
        const merged = force ? incoming : uniqueArtifacts([...previous, ...incoming]);
        const newNames = new Set<string>();
        for (const artifact of merged) {
          if (!previous.find((p) => p.name === artifact.name)) {
            newNames.add(artifact.name);
          }
        }
        if (newNames.size > 0) {
          setNewArtifactNames((prev) => new Set([...prev, ...newNames]));
          setTimeout(() => setNewArtifactNames((prev) => {
            const next = new Set(prev);
            newNames.forEach((n) => next.delete(n));
            return next;
          }), 3000);
        }
        return merged;
      });
    } catch {
      // workspace not ready yet
    }
  }, [designName]);

  const sendChatMessage = async (text: string, nextMessages: ChatMessage[]) => {
    if (!licenseActive) {
      setMessages((prev) => [...prev, {
        role: 'assistant',
        tone: 'error',
        content: toUserError(
          licenseStatus?.reason,
          'AgentIC requires an active purchased license before local agent execution.'
        ),
      }]);
      return;
    }

    const byokConfig = readByokConfig();
    if (!byokConfig) {
      setShowBillingModal(true);
      return;
    }

    setIsChatting(true);
    setMessages(nextMessages);
    setThinking('Agent is working...');

    let assistantContent = '';
    let done = false;
    let failed = false;
    let cancelled = false;
    const runId = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '')
      : `run-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setActiveRunId(runId);

    try {
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const headers = await getSseHeaders({ 'Content-Type': 'application/json' });

      await fetchEventSource(`${API_BASE}/chat/converse`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          messages: nextMessages.map((m) => ({ role: m.role, content: m.content })),
          plan_type: 'byok',
          api_key: byokConfig.apiKey,
          base_url: byokConfig.baseUrl,
          model: byokConfig.model,
          pdk_profile: pdkProfile,
          run_id: runId,
        }),
        signal: ctrl.signal,
        onmessage(event) {
          if (done) return;
          try {
            const data = JSON.parse(event.data);
            const eventType = data.type || '';
            const content = cleanUserFacingAgentText(data.label || data.content || data.message || '');
            if (data.run_id) setActiveRunId(String(data.run_id));

            if (eventType === 'progress') {
              setThinking(content || 'Agent is working...');
              addRunEvent({
                run_id: data.run_id,
                type: eventType,
                label: content,
                stage: data.stage,
                status: data.status,
                design_name: data.design_name,
                timestamp: data.timestamp,
              });
            } else if (eventType === 'reasoning' || eventType === 'tool-call' || eventType === 'tool-result') {
              if (data.content) {
                // Yield small, summarised reasoning status in the "Thinking" state
                let msg = '';
                if (eventType === 'reasoning') {
                  msg = `Thinking: ${data.content.slice(0, 80)}...`;
                } else if (eventType === 'tool-call') {
                  msg = `Executing: ${data.content.split('(')[0]}...`;
                } else if (eventType === 'tool-result') {
                  msg = `Completed step: ${data.content.slice(0, 50).replace(/\n/g, ' ')}...`;
                }
                setThinking(msg);
              }
              if (import.meta.env.VITE_AGENTIC_DEBUG_EVENTS === 'true') {
                console.debug('[agentic:event]', data);
              }
            } else if (eventType === 'needs_input') {
              assistantContent = content;
              addRunEvent({
                run_id: data.run_id,
                type: eventType,
                label: content,
                stage: data.stage,
                status: 'needs_input',
                design_name: data.design_name,
                timestamp: data.timestamp,
              });
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last?.role === 'assistant') {
                  return [...prev.slice(0, -1), { role: 'assistant', content }];
                }
                return [...prev, { role: 'assistant', content }];
              });
            } else if (eventType === 'response') {
              assistantContent = content;
              addRunEvent({
                run_id: data.run_id,
                type: eventType,
                label: 'Build summary ready',
                stage: data.stage,
                status: 'completed',
                design_name: data.design_name,
                timestamp: data.timestamp,
              });
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last?.role === 'assistant') {
                  return [...prev.slice(0, -1), { role: 'assistant', content }];
                }
                return [...prev, { role: 'assistant', content }];
              });
            } else if (eventType === 'stream_end') {
              done = true;
              setActiveRunId('');
              addRunEvent({
                run_id: data.run_id,
                type: eventType,
                label: 'Run complete',
                stage: data.stage,
                status: 'completed',
                design_name: data.design_name,
                timestamp: data.timestamp,
              });
            } else if (eventType === 'error') {
              failed = true;
              assistantContent = content || 'The agent hit an issue while working locally. Please check your model key, license, and local EDA setup, then try again.';
              addRunEvent({
                run_id: data.run_id,
                type: eventType,
                label: assistantContent,
                stage: data.stage,
                status: 'failed',
                design_name: data.design_name,
                timestamp: data.timestamp,
              });
              setMessages((prev) => [...prev, { role: 'assistant', tone: 'error', content: assistantContent }]);
            } else if (eventType === 'cancelled') {
              cancelled = true;
              done = true;
              setThinking('');
              setActiveRunId('');
              addRunEvent({
                run_id: data.run_id,
                type: eventType,
                label: content || 'Run stopped',
                stage: data.stage,
                status: data.status || 'cancelled',
                design_name: data.design_name,
                timestamp: data.timestamp,
              });
            }
          } catch {
            // ignore parse errors
          }
        },
        onclose() {
          done = true;
        },
        onerror(_err) {
          failed = true;
          done = true;
          if (!assistantContent) {
            const fallback = advisorReply(text, pdkProfile);
            setMessages((prev) => [...prev, { role: 'assistant', content: fallback }]);
          }
        },
      });
    } catch {
      // aborted
    } finally {
      setThinking('');
      setIsChatting(false);
      abortRef.current = null;
      setActiveRunId('');
      if (!cancelled) {
        api.post('/usage/build', {
          status: failed ? 'failed' : 'done',
          capability_tier: toolTier,
          successful_builds: failed ? 0 : 1,
          total_builds: 1,
        }).catch(() => {});
      }
      void refreshRunEvents();
      void refreshActiveDesign();
      void fetchArtifacts(designName, true);
    }
  };

  const handlePrimaryAction = async () => {
    const text = prompt.trim();
    if (isBusy) {
      if (text) {
        await handleSteerAction(text);
      }
      return;
    }
    if (!text) return;

    setPrompt('');
    const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: text }];
    await sendChatMessage(text, nextMessages);
  };

  const stopCurrentRun = useCallback(async (notice = 'Run stopped. You can steer the next step.') => {
    const runId = activeRunId;
    if (runId) {
      api.post(`/runs/${encodeURIComponent(runId)}/cancel`).catch(() => {});
    }
    abortRef.current?.abort();
    setIsChatting(false);
    setThinking('');
    setActiveRunId('');
    if (notice) {
      setMessages((prev) => [...prev, { role: 'assistant', content: notice }]);
    }
  }, [activeRunId]);

  const handleSteerAction = useCallback(async (text: string) => {
    const guidance = text.trim();
    if (!guidance) return;
    setPrompt('');
    await stopCurrentRun('');
    const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: guidance }];
    setTimeout(() => {
      void sendChatMessage(guidance, nextMessages);
    }, 250);
  }, [messages, stopCurrentRun]);

  const handleEditorDidMount = (editor: any, monaco: any) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
  };

  const requestByokSetup = () => setShowBillingModal(true);

  const refreshToolStatus = useCallback(async () => {
    try {
      const res = await api.get('/tools/status');
      setLocalToolStatus(res.data || null);
    } catch {
      // keep last known state
    }
  }, []);

  const refreshRunEvents = useCallback(async () => {
    try {
      const res = await api.get('/runs/events?limit=80');
      setRunEvents(Array.isArray(res.data?.events) ? res.data.events : []);
    } catch {
      // timeline is helpful, not required for the chat loop
    }
  }, []);

  const refreshActiveDesign = useCallback(async () => {
    try {
      const res = await api.get('/workspace/active');
      const activeName = normalizeActiveDesignName(res.data?.active?.name);
      if (res.data?.active && typeof res.data.active.name === 'string') {
        setDesignName(activeName);
        onActiveDesignChange?.(activeName);
        void fetchArtifacts(activeName, true);
      }
    } catch {
      // keep current design
    }
  }, [fetchArtifacts, onActiveDesignChange]);

  const addRunEvent = useCallback((event: RunEvent) => {
    const safeEvent = {
      ...event,
      label: cleanUserFacingAgentText(event.label || ''),
    };
    if (typeof safeEvent.design_name === 'string') {
      const nextDesignName = normalizeActiveDesignName(safeEvent.design_name);
      setDesignName(nextDesignName);
      onActiveDesignChange?.(nextDesignName);
      void fetchArtifacts(nextDesignName, true);
    }
    setRunEvents((previous) => [...previous.slice(-79), safeEvent].filter((item) => item.label || item.type === 'stream_end'));
  }, [fetchArtifacts, onActiveDesignChange]);

  const startNewConversation = useCallback(() => {
    const next = createConversation();
    setConversations((previous) => {
      const updated = [next, ...previous].slice(0, MAX_LOCAL_CONVERSATIONS);
      saveConversations(updated);
      return updated;
    });
    setActiveConversationId(next.id);
    localStorage.setItem(ACTIVE_CHAT_STORAGE_KEY, next.id);
    setMessages([]);
    setPrompt('');
    setThinking('');
  }, []);

  const selectConversation = useCallback((conversationId: string) => {
    const conversation = conversations.find((item) => item.id === conversationId);
    if (!conversation) return;
    setActiveConversationId(conversationId);
    localStorage.setItem(ACTIVE_CHAT_STORAGE_KEY, conversationId);
    setMessages(conversation.messages);
    if (conversation.designName) {
      setDesignName(conversation.designName);
      onActiveDesignChange?.(conversation.designName);
      void fetchArtifacts(conversation.designName, true);
    }
  }, [conversations, fetchArtifacts, onActiveDesignChange]);

  const deleteConversation = useCallback((conversationId: string) => {
    setConversations((previous) => {
      const remaining = previous.filter((conversation) => conversation.id !== conversationId);
      const updated = remaining.length ? remaining : [createConversation()];
      saveConversations(updated);
      const nextActive = updated[0];
      if (conversationId === activeConversationId) {
        setActiveConversationId(nextActive.id);
        localStorage.setItem(ACTIVE_CHAT_STORAGE_KEY, nextActive.id);
        setMessages(nextActive.messages);
      }
      return updated;
    });
  }, [activeConversationId]);

  const requestInstallPlan = async (capability = missingTools[0]?.capability || 'pnr') => {
    setInstallMessage('');
    try {
      const res = await api.post('/tools/install-plan', { capability });
      setInstallPlan(res.data);
    } catch (err: any) {
      setInstallMessage(err?.message || 'Unable to create install plan.');
    }
  };

  const approveInstallPlan = async () => {
    if (!installPlan) return;
    setInstalling(true);
    setInstallMessage(`Installing ${installPlan.tool}...`);
    try {
      const res = await api.post('/tools/install', {
        capability: installPlan.capability,
        command: installPlan.command,
        approved: true,
      });
      setInstallMessage(res.data?.success ? 'Install completed. Tool status refreshed.' : 'Install could not be completed. Please review the install plan and try again.');
      setInstallPlan(null);
      await refreshToolStatus();
    } catch (err: any) {
      setInstallMessage(toUserError(err, 'Install could not be completed. Please try again.'));
    } finally {
      setInstalling(false);
    }
  };

  useEffect(() => {
    const initialDesign = normalizeActiveDesignName(selectedDesign);
    setDesignName(initialDesign);
    void fetchArtifacts(initialDesign, true);
    api.get('/pdks').then((res) => {
      const data = res.data || {};
      const options: PdkOption[] = [];
      if (data.docker_images) {
        options.push({ key: 'docker', available: true, can_harden: true, gds_ready: true, description: 'Docker-based EDA tools' });
      }
      if (data.tools?.yosys) {
        options.push({ key: 'yosys', available: true, can_synthesize: true, description: 'Yosys synthesis' });
      }
      if (data.existing_pdk_dirs?.length) {
        for (const dir of data.existing_pdk_dirs) {
          const name = dir.split('/').pop() || dir;
          options.push({ key: name, pdk: name, available: true, can_harden: true, description: `PDK at ${dir}` });
        }
      }
      setPdkOptions(options);
      if (!pdkProfile && options.length) {
        setPdkProfile(options[0].key);
      }
    }).catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const normalizedSelectedDesign = normalizeActiveDesignName(selectedDesign);
    if (normalizedSelectedDesign !== designName) {
      setDesignName(normalizedSelectedDesign);
      setArtifacts([]);
      setSelectedArtifact(null);
      void fetchArtifacts(normalizedSelectedDesign, true);
    }
  }, [selectedDesign]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void refreshRunEvents();
    void refreshActiveDesign();
  }, [refreshRunEvents, refreshActiveDesign]);

  useEffect(() => {
    if (toolStatus) setLocalToolStatus(toolStatus);
  }, [toolStatus]);

  useEffect(() => {
    setConversations((previous) => {
      const now = safeNow();
      const updated = previous.map((conversation) => {
        if (conversation.id !== activeConversationId) return conversation;
        return {
          ...conversation,
          title: conversationTitle(messages, conversation.title),
          updatedAt: messages.length ? now : conversation.updatedAt,
          designName: designName || conversation.designName,
          messages,
        };
      });
      saveConversations(updated);
      return updated;
    });
  }, [activeConversationId, designName, messages]);

  useEffect(() => {
    if (selectedArtifact) {
      const endpoint = designName
        ? `/build/artifacts/${encodeURIComponent(designName)}/${artifactPath(selectedArtifact.name)}`
        : `/build/artifacts/file/${artifactPath(selectedArtifact.name)}`;
      api.get(endpoint)
        .then((res) => setArtifactPreview(typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2)))
        .catch(() => setArtifactPreview('Error loading artifact'));
    } else {
      setArtifactPreview('');
    }
  }, [selectedArtifact, designName]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, thinking]);

  return (
    <div className={`codex-vlsi-root ${workspaceCollapsed ? 'is-workspace-collapsed' : ''} ${isResizing ? 'is-resizing' : ''}`}>
      <aside className="codex-vlsi-chat" style={!workspaceCollapsed ? { width: chatWidth } : {}}>
        <div className="codex-vlsi-chat-head">
          <div>
            <div className="codex-vlsi-title">AgentIC Studio</div>
            <div className="codex-vlsi-subtitle">{activeConversation?.title || 'Describe the chip. Watch the build.'}</div>
          </div>
          <div style={{ display: 'flex', gap: '0.5rem', marginLeft: 'auto' }}>
            <button
              type="button"
              className="codex-vlsi-icon-button"
              onClick={() => {
                const val = !workspaceCollapsed;
                setWorkspaceCollapsed(val);
                localStorage.setItem('agentic_workspace_collapsed', String(val));
              }}
              aria-label={workspaceCollapsed ? 'Show workspace' : 'Focus chat'}
              title={workspaceCollapsed ? 'Show workspace' : 'Focus chat'}
            >
              {workspaceCollapsed ? <Minimize size={15} /> : <Maximize size={15} />}
            </button>
            <button
              type="button"
              className="codex-vlsi-icon-button"
              onClick={startNewConversation}
              disabled={isBusy}
              aria-label="New conversation"
              title="New conversation"
            >
              <Plus size={15} />
            </button>
          </div>
        </div>

        <div className="codex-vlsi-history-strip">
          <button
            type="button"
            className="codex-vlsi-history-title"
            onClick={() => {
              setHistoryCollapsed((value) => {
                const next = !value;
                localStorage.setItem(HISTORY_COLLAPSED_STORAGE_KEY, String(next));
                return next;
              });
            }}
            aria-expanded={!historyCollapsed}
            title={historyCollapsed ? 'Show recent conversations' : 'Hide recent conversations'}
          >
            {historyCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
            <History size={13} />
            <span>Recent</span>
            <em>{recentConversations.length}</em>
          </button>
          {!historyCollapsed && (
            <div className="codex-vlsi-history-list">
              {recentConversations.map((conversation) => (
                <div
                  key={conversation.id}
                  className={`codex-vlsi-history-item${conversation.id === activeConversationId ? ' active' : ''}`}
                >
                  <button
                    type="button"
                    className="codex-vlsi-history-open"
                    onClick={() => selectConversation(conversation.id)}
                    title={conversation.title}
                  >
                    <span>{conversation.title}</span>
                    <em>{formatConversationAge(conversation.updatedAt)}</em>
                  </button>
                  <button
                    type="button"
                    className="codex-vlsi-history-delete"
                    onClick={() => {
                      deleteConversation(conversation.id);
                    }}
                    aria-label="Delete conversation"
                    title="Delete conversation"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="codex-vlsi-thread">
          {messages.map((message, index) => {
            const displayContent = cleanUserFacingAgentText(message.content);
            const messageKey = `${message.role}-${index}-${displayContent.slice(0, 32)}`;
            const isLatestAssistant = message.role === 'assistant' && index === messages.length - 1 && Boolean(thinking);
            const canCollapse =
              message.role === 'assistant' &&
              !isLatestAssistant &&
              (displayContent.length > COLLAPSIBLE_MESSAGE_CHARS ||
                displayContent.split('\n').length > COLLAPSIBLE_MESSAGE_LINES);
            const isCollapsed = canCollapse && collapsedMessages[messageKey] !== false;
            const preview = displayContent.replace(/\s+/g, ' ').trim().slice(0, 190);

            return (
              <motion.article
                key={messageKey}
                className={`codex-vlsi-message ${message.role} ${message.tone || ''} ${isCollapsed ? 'is-collapsed' : ''}`}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.16 }}
              >
                {canCollapse && (
                  <button
                    type="button"
                    className="codex-vlsi-message-toggle"
                    onClick={() =>
                      setCollapsedMessages((prev) => ({
                        ...prev,
                        [messageKey]: !isCollapsed,
                      }))
                    }
                    aria-expanded={!isCollapsed}
                  >
                    {isCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                    <span>{isCollapsed ? 'Show details' : 'Hide details'}</span>
                  </button>
                )}
                {isCollapsed ? (
                  <p className="codex-vlsi-message-preview">{preview}...</p>
                ) : (
                  <>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayContent}</ReactMarkdown>
                    {message.role === 'user' && (
                      <div className="message-actions" style={{ display: 'flex', gap: '8px', marginTop: '8px', justifyContent: 'flex-end', opacity: 0.8 }}>
                        <button
                          type="button"
                          onClick={() => {
                            navigator.clipboard.writeText(displayContent);
                            setCopiedIndex(index);
                            setTimeout(() => setCopiedIndex(null), 2000);
                          }}
                          title="Copy message"
                          style={{ background: 'var(--c-surface-sunken)', border: '1px solid var(--c-border)', borderRadius: '4px', color: 'var(--c-text-muted)', cursor: 'pointer', padding: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                        >
                          {copiedIndex === index ? <CheckCircle2 size={14} color="var(--c-success)" /> : <Copy size={14} />}
                        </button>
                        <button
                          type="button"
                          onClick={() => { setPrompt(displayContent); setTimeout(() => inputRef.current?.focus(), 10); }}
                          title="Edit prompt"
                          style={{ background: 'var(--c-surface-sunken)', border: '1px solid var(--c-border)', borderRadius: '4px', color: 'var(--c-text-muted)', cursor: 'pointer', padding: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                        >
                          <Edit2 size={14} />
                        </button>
                      </div>
                    )}
                  </>
                )}
              </motion.article>
            );
          })}

          {thinking && (
            <div className="codex-vlsi-thinking">
              <span className="codex-vlsi-pulse" />
              <span>{thinking}</span>
            </div>
          )}
          <div ref={scrollRef} />
        </div>

        <div className="codex-vlsi-composer">
          <div style={{ display: 'flex', gap: '1rem', fontSize: '0.75rem', marginBottom: '0.65rem', paddingLeft: '0.5rem' }}>
            <button
              type="button"
              onClick={requestByokSetup}
              disabled={isBusy}
              title="Configure API key"
              style={{
                background: 'none', border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.35rem',
                color: hasByok ? 'var(--text-dim)' : 'var(--accent)', padding: 0
              }}
            >
              {hasByok ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}
              {hasByok ? 'API Configured' : 'Setup API Key'}
            </button>
            {!licenseActive && (
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', color: 'var(--fail)' }}>
                <AlertTriangle size={13} /> License Required
              </span>
            )}
          </div>
          <div className="codex-vlsi-input-shell">
            <textarea
              ref={inputRef}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="Describe the chip block, interface, target PDK, or next task..."
              rows={3}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  void handlePrimaryAction();
                }
              }}
            />
            <button
              type="button"
              className="codex-vlsi-send"
              onClick={() => void handlePrimaryAction()}
              disabled={!prompt.trim()}
              aria-label="Send prompt"
              title={isBusy ? 'Stop current run and send guidance' : 'Send prompt'}
            >
              <ArrowUp size={17} />
            </button>
            {isBusy && (
              <button
                type="button"
                className="codex-vlsi-stop"
                onClick={() => void stopCurrentRun()}
                aria-label="Stop run"
                title="Stop run"
              >
                <Square size={13} />
              </button>
            )}
          </div>
          <div className="codex-vlsi-composer-meta">
            {hasByok && <span className="is-auto">Autonomous agent</span>}
            <span>{toolTier}</span>
            <button type="button" onClick={requestByokSetup} title="Configure API key">
              <Settings2 size={13} />
            </button>
          </div>
        </div>
      </aside>

      {!workspaceCollapsed && (
        <div className={`codex-vlsi-resizer ${isResizing === 'chat' ? 'is-active' : ''}`} onMouseDown={() => setIsResizing('chat')} />
      )}
      <main className="codex-vlsi-workspace">
        <header className="codex-vlsi-workspace-head">
          <div>
            <div className="codex-vlsi-kicker">Live workspace</div>
            <h2>{designName || 'AgentIC Workspace'}</h2>
          </div>
          <div className="codex-vlsi-run-state">
            <span className={`codex-vlsi-state-dot is-${isChatting ? 'running' : 'idle'}`} />
            <span>{isChatting ? 'Thinking' : 'Ready'}</span>
          </div>
        </header>

        <div className="codex-vlsi-stage-band" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', justifyContent: 'center' }}>
          <div className="codex-vlsi-stage-status">
            <span>{designName || 'AgentIC'}</span>
            <p>{thinking || 'Waiting for a chip request.'}</p>
          </div>
        </div>

        <div className={`codex-vlsi-body ${filesCollapsed ? 'is-files-collapsed' : ''}`}>
          <aside className="codex-vlsi-files">
            <div className="codex-vlsi-files-head">
              <button
                type="button"
                onClick={() => {
                  const val = !filesCollapsed;
                  setFilesCollapsed(val);
                  localStorage.setItem('agentic_files_collapsed', String(val));
                }}
                className="codex-vlsi-icon-btn"
                title={filesCollapsed ? "Expand files pane" : "Collapse files pane"}
                style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', display: 'flex', alignItems: 'center', padding: 0 }}
              >
                {filesCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
              </button>
              <span>Files</span>
              <small>{visibleArtifacts.length}</small>
            </div>
            <div className="codex-vlsi-file-tree">
              {groupedArtifacts.map((section) => {
                const isExpanded = expandedSections[section.label] !== false;
                return (
                  <div key={section.label} className="codex-vlsi-file-section">
                    <div 
                      className="codex-vlsi-file-section-label"
                      onClick={() => setExpandedSections(prev => ({ ...prev, [section.label]: !isExpanded }))}
                      style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.4rem', userSelect: 'none' }}
                    >
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      {isExpanded ? <FolderOpen size={14} /> : <Folder size={14} />}
                      <span>{section.label}</span>
                    </div>
                    {isExpanded && section.files.map((artifact) => (
                      <button
                        key={artifact.name}
                        type="button"
                        className={`codex-vlsi-file ${selectedArtifact?.name === artifact.name ? 'active' : ''} ${newArtifactNames.has(artifact.name) ? 'is-new' : ''}`}
                        onClick={() => {
                          setArtifactPreview('');
                          setSelectedArtifact(artifact);
                        }}
                        title={artifact.name}
                        style={{ paddingLeft: '1.8rem' }}
                      >
                        <FileText size={12} />
                        <span>{artifact.name}</span>
                        {artifact.size ? <small>{formatBytes(artifact.size)}</small> : null}
                      </button>
                    ))}
                  </div>
                );
              })}
              {!groupedArtifacts.length && (
                <div className="codex-vlsi-empty-files">
                  Generated RTL, verification, layout, and reports will appear here.
                </div>
              )}
            </div>
          </aside>

          <section className="codex-vlsi-editor">
            <div className="codex-vlsi-editor-tabs">
              <div className="codex-vlsi-editor-tab" style={{ flex: 1, display: 'flex', justifyContent: 'space-between', paddingRight: '0.5rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <Code2 size={14} />
                  <span>{selectedArtifact?.name || 'Workspace preview'}</span>
                </div>
                {selectedArtifact && (
                  <button
                    type="button"
                    style={{ background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer', display: 'grid', placeItems: 'center', opacity: 0.6 }}
                    onMouseEnter={(e) => e.currentTarget.style.opacity = '1'}
                    onMouseLeave={(e) => e.currentTarget.style.opacity = '0.6'}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedArtifact(null);
                      setArtifactPreview('');
                    }}
                    title="Close editor"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>
            <div className="codex-vlsi-editor-content">
              {selectedArtifact ? (
                <DiagramViewer
                  filename={selectedArtifact.name}
                  content={artifactPreview}
                  language={artifactLanguage(selectedArtifact.name)}
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

      {!workspaceCollapsed && (
        <div className={`codex-vlsi-resizer ${isResizing === 'inspector' ? 'is-active' : ''}`} onMouseDown={() => setIsResizing('inspector')} />
      )}
      <aside className="codex-vlsi-inspector" style={!workspaceCollapsed ? { width: inspectorWidth } : {}}>
        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">PDK</div>
          <div className="codex-vlsi-stage-list">
            <div className="codex-vlsi-stage-row active">
              <span>{pdkProfile || 'auto'}</span>
              <div>
                <strong>{pdkProfile || 'Not selected'}</strong>
                <small>{selectedPdkMode}</small>
              </div>
            </div>
          </div>
        </div>

        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Active signal</div>
          <div className="codex-vlsi-signal">
            <Terminal size={14} />
            <div>
              <strong>{thinking ? 'Agent working' : 'Waiting'}</strong>
              <span>{thinking || 'Describe the chip block you want to build.'}</span>
            </div>
          </div>
        </div>

        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Run timeline</div>
          <div className="codex-vlsi-stage-list">
            {runEvents.slice(-6).map((event, index) => (
              <div className={`codex-vlsi-stage-row ${index === runEvents.slice(-6).length - 1 ? 'active' : ''}`} key={`${event.run_id || 'run'}-${event.timestamp || index}-${index}`}>
                <span>{event.stage || event.type || 'step'}</span>
                <div>
                  <strong>{event.label || 'Working'}</strong>
                  <small>{event.status || 'running'}</small>
                </div>
              </div>
            ))}
            {!runEvents.length && (
              <div className="codex-vlsi-signal">
                <Activity size={14} />
                <div>
                  <strong>No run yet</strong>
                  <span>Sanitized progress will appear here.</span>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Local EDA</div>
          <div className="codex-vlsi-signal">
            {missingTools.length ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}
            <div>
              <strong>{toolTier}</strong>
              <span>
                {missingTools.length
                  ? `${missingTools.length} capability gap${missingTools.length > 1 ? 's' : ''}`
                  : 'Ready for local execution'}
              </span>
            </div>
          </div>
          <button
            type="button"
            className="codex-vlsi-model-chip is-byok"
            onClick={() => void refreshToolStatus()}
            style={{ width: '100%', marginTop: '0.65rem' }}
            title="Probe your local EDA toolchain. Or just ask the agent: 'what EDA tools do I have?'"
          >
            <Terminal size={14} />
            Detect installed tools
          </button>
          {missingTools.length > 0 && (
            <div className="codex-vlsi-stage-list" style={{ marginTop: '0.75rem' }}>
              {missingTools.slice(0, 3).map((item) => (
                <div className="codex-vlsi-stage-row" key={item.capability}>
                  <span>{item.capability}</span>
                  <div>
                    <strong>{item.tools.join(' or ')}</strong>
                    <small>Missing local capability</small>
                  </div>
                </div>
              ))}
              <button
                type="button"
                className="codex-vlsi-model-chip is-byok"
                onClick={() => void requestInstallPlan()}
                disabled={installing}
                style={{ width: '100%', marginTop: '0.65rem' }}
              >
                Plan Install
              </button>
            </div>
          )}
          {installPlan && (
            <div className="codex-vlsi-signal" style={{ marginTop: '0.75rem', alignItems: 'flex-start' }}>
              <Terminal size={14} />
              <div>
                <strong>{installPlan.tool}</strong>
                <span>{installPlan.purpose}</span>
                {installPlan.command ? (
                  <code style={{ display: 'block', marginTop: '0.5rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {installPlan.command}
                  </code>
                ) : (
                  <span style={{ display: 'block', marginTop: '0.5rem' }}>
                    Configure a tool path or Docker image first, then AgentIC can ask for approval to install.
                  </span>
                )}
                {installPlan.options?.length ? (
                  <div style={{ display: 'grid', gap: '0.4rem', marginTop: '0.65rem' }}>
                    {installPlan.options.map((option) => (
                      <button
                        type="button"
                        key={option.key}
                        className="codex-vlsi-model-chip"
                        onClick={() => {
                          if (option.key === 'install' || option.key === 'docker') {
                            setInstallMessage(installPlan.command
                              ? 'Review the install action, then approve it.'
                              : option.description || 'Configure the install source first.');
                            return;
                          }
                          if (option.key === 'skip') {
                            setInstallMessage('AgentIC will continue with available local stages and skip this capability.');
                            setInstallPlan(null);
                            return;
                          }
                          setInstallMessage(option.description || 'Configure this capability in your local environment, then refresh tool status.');
                        }}
                        title={option.description}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                ) : null}
                <button
                  type="button"
                  className="codex-vlsi-model-chip is-byok"
                  onClick={() => void approveInstallPlan()}
                  disabled={installing || !installPlan.command}
                  style={{ marginTop: '0.5rem' }}
                >
                  {installing ? 'Installing...' : installPlan.command ? 'Approve Install' : 'Needs Configuration'}
                </button>
              </div>
            </div>
          )}
          {installMessage && (
            <div className="codex-vlsi-signal" style={{ marginTop: '0.75rem' }}>
              <Activity size={14} />
              <div>
                <strong>Install status</strong>
                <span>{installMessage}</span>
              </div>
            </div>
          )}
        </div>

        <div className="codex-vlsi-inspector-section">
          <div className="codex-vlsi-inspector-title">Workspace</div>
          <div className="codex-vlsi-signal">
            <Folder size={14} />
            <div>
              <strong>{designName || 'No design'}</strong>
              <span>{artifacts.length} files generated</span>
            </div>
          </div>
        </div>
      </aside>

      <BillingModal
        isOpen={showBillingModal}
        onClose={() => setShowBillingModal(false)}
        initialMode="byok"
        onKeySaved={() => {
          setProfile((previous) => ({ ...(previous || {}), has_byok_key: true }));
          setShowBillingModal(false);
        }}
      />
    </div>
  );
};

export default DesignStudio;
