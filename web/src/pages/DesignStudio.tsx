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
  Code2,
  FileText,
  Settings2,
  Terminal,
  ChevronRight,
  ChevronDown,
  Folder,
  FolderOpen,
} from 'lucide-react';
import { BillingModal } from '../components/BillingModal';
import { api, API_BASE } from '../api';

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  tone?: 'normal' | 'success' | 'error';
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

const HELP_RE = /\b(help|what can you do|capabilit|possible|not possible|can you|how do i|suggest|prompt)\b/i;
const ERROR_RE = /(%error|%warning|syntax error|parse error|error:|fatal|failed|traceback|critical|lint|violation|unmapped|pinmissing|pinnotfound)/i;
const CASUAL_RE = /^(hi+|hello+|hey+|yo+|sup|thanks|thank you|ok|okay|test)$/i;

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
  if (/\/pnr\//.test(name) || name.startsWith('pnr/')) return 'physical';
  if (/\/sta\//.test(name) || name.startsWith('sta/')) return 'timing';
  if (/\/sim\//.test(name) || name.startsWith('sim/')) return 'simulation';
  if (/\/reports\//.test(name) || name.startsWith('reports/')) return 'report';
  // Fallback to extension-based matching
  if (/\.(v|sv|svh|vh)$/.test(name) && !name.includes('tb')) return 'rtl';
  if (name.includes('tb') || name.endsWith('.sby') || name.includes('formal') || name.endsWith('.vcd')) return 'verification';
  if (name.endsWith('.gds') || name.endsWith('.def') || name.endsWith('.lef') || name.includes('openlane')) return 'physical';
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
    { label: 'Physical (PnR)', types: ['physical', 'layout'] },
    { label: 'Timing (STA)', types: ['timing'] },
    { label: 'Simulation', types: ['simulation'] },
    { label: 'Reports', types: ['report', 'log', 'config', 'script', 'other'] },
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

export const DesignStudio = () => {
  const [prompt, setPrompt] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [artifactPreview, setArtifactPreview] = useState('');
  const [newArtifactNames, setNewArtifactNames] = useState<Set<string>>(new Set());
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({});
  const [pdkOptions, setPdkOptions] = useState<PdkOption[]>([]);
  const [pdkProfile, setPdkProfile] = useState('');
  const [profile, setProfile] = useState<{ has_byok_key?: boolean } | null>(null);
  const [showBillingModal, setShowBillingModal] = useState(false);
  const [designName, setDesignName] = useState('');
  const [thinking, setThinking] = useState('');
  const [isChatting, setIsChatting] = useState(false);

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
      const endpoint = targetDesign ? `/build/artifacts/${targetDesign}` : '/build/artifacts';
      const res = await api.get(endpoint);
      const incoming: Artifact[] = uniqueArtifacts(Array.isArray(res.data) ? res.data : Array.isArray(res.data?.artifacts) ? res.data.artifacts : []);
      setArtifacts((previous) => {
        const merged = uniqueArtifacts([...previous, ...incoming]);
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

    try {
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      await fetchEventSource(`${API_BASE}/chat/converse`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: nextMessages.map((m) => ({ role: m.role, content: m.content })),
          plan_type: 'byok',
          api_key: byokConfig.apiKey,
          base_url: byokConfig.baseUrl,
          model: byokConfig.model,
          pdk_profile: pdkProfile,
        }),
        signal: ctrl.signal,
        onmessage(event) {
          if (done) return;
          try {
            const data = JSON.parse(event.data);
            const eventType = data.type || '';
            const content = data.content || data.message || '';

            if (eventType === 'reasoning') {
              setThinking(content);
            } else if (eventType === 'tool-call') {
              setThinking(`⚡ ${content}`);
            } else if (eventType === 'needs_input') {
              assistantContent = content;
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last?.role === 'assistant') {
                  return [...prev.slice(0, -1), { role: 'assistant', content }];
                }
                return [...prev, { role: 'assistant', content }];
              });
            } else if (eventType === 'response') {
              assistantContent = content;
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last?.role === 'assistant') {
                  return [...prev.slice(0, -1), { role: 'assistant', content }];
                }
                return [...prev, { role: 'assistant', content }];
              });
            } else if (eventType === 'stream_end') {
              done = true;
            } else if (eventType === 'error') {
              assistantContent = `Error: ${content}`;
              setMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${content}` }]);
            }
          } catch {
            // ignore parse errors
          }
        },
        onclose() {
          done = true;
        },
        onerror(_err) {
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
      void fetchArtifacts(designName, true);
    }
  };

  const handlePrimaryAction = async () => {
    const text = prompt.trim();
    if (isBusy) return;
    if (!text) return;

    setPrompt('');
    const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: text }];
    await sendChatMessage(text, nextMessages);
  };

  const handleEditorDidMount = (editor: any, monaco: any) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
  };

  const requestByokSetup = () => setShowBillingModal(true);

  useEffect(() => {
    setDesignName('');
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
    if (selectedArtifact && designName) {
      api.get(`/build/artifacts/${designName}/${selectedArtifact.name}`)
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

          {thinking && (
            <div className="codex-vlsi-thinking">
              <span className="codex-vlsi-pulse" />
              <span>{thinking}</span>
            </div>
          )}
          <div ref={scrollRef} />
        </div>

        <div className="codex-vlsi-composer">
          <div className="codex-vlsi-routebar">
            <div className="codex-vlsi-route-label">API Key</div>
            <button
              type="button"
              className="codex-vlsi-model-chip is-byok"
              onClick={requestByokSetup}
              disabled={isBusy}
              title="Configure API key"
            >
              {hasByok ? 'Configured' : 'Configure API key'}
            </button>
          </div>
          <div className="codex-vlsi-input-shell">
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="Describe the chip block, interface, target PDK, or next task..."
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
              disabled={isBusy || !prompt.trim()}
              aria-label="Send prompt"
              title="Send prompt"
            >
              <ArrowUp size={17} />
            </button>
          </div>
          <div className="codex-vlsi-composer-meta">
            {hasByok && <span className="is-auto">Autonomous agent</span>}
            <button type="button" onClick={requestByokSetup} title="Configure API key">
              <Settings2 size={13} />
            </button>
          </div>
        </div>
      </aside>

      <main className="codex-vlsi-workspace">
        <header className="codex-vlsi-workspace-head">
          <div>
            <div className="codex-vlsi-kicker">Live workspace</div>
            <h2>{designName || 'No active chip yet'}</h2>
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

        <div className="codex-vlsi-body">
          <aside className="codex-vlsi-files">
            <div className="codex-vlsi-files-head">
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
              <div className="codex-vlsi-editor-tab">
                <Code2 size={14} />
                <span>{selectedArtifact?.name || 'Workspace preview'}</span>
              </div>
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
