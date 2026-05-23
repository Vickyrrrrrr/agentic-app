import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, ChevronDown, ChevronUp, Eye, EyeOff, Check, Fingerprint, LockKeyhole, Sparkles, Cpu, KeyRound } from 'lucide-react';
import { API_BASE } from '../api';
import { supabase } from '../supabaseClient';
import { toUserError } from '../utils/errorFormatter';

type GroupKey = 'group1' | 'group2' | 'group3';
type GroupState = Record<GroupKey, { model: string; apiKey: string; baseUrl: string }>;
type ByokServerGroup = { model?: string; api_key?: string; base_url?: string };
type ByokConfigPayload = Record<GroupKey, { model: string; api_key: string; base_url: string }>;

type ModalMode = 'agentic' | 'byok';

const DEFAULT_BYOK_MODEL = 'gpt-4o';
const DEFAULT_BYOK_BASE_URL = 'https://api.openai.com/v1';

const DEFAULT_GROUPS: GroupState = {
  group1: { model: DEFAULT_BYOK_MODEL, apiKey: '', baseUrl: DEFAULT_BYOK_BASE_URL },
  group2: { model: DEFAULT_BYOK_MODEL, apiKey: '', baseUrl: DEFAULT_BYOK_BASE_URL },
  group3: { model: DEFAULT_BYOK_MODEL, apiKey: '', baseUrl: DEFAULT_BYOK_BASE_URL },
};

const MaskedKey = ({ value }: { value: string }) => {
  const [visible, setVisible] = useState(false);
  if (!value) return null;
  const masked = value.length > 8 ? `${value.slice(0, 6)}${'•'.repeat(Math.min(20, value.length - 10))}${value.slice(-4)}` : '••••••••';
  return (
    <button
      className="byok-key-peek"
      onClick={() => setVisible(!visible)}
      type="button"
      title={visible ? 'Hide key' : 'Show key'}
    >
      {visible ? <EyeOff size={14} /> : <Eye size={14} />}
      <code>{visible ? value : masked}</code>
    </button>
  );
};

const ByokGroupCard = ({
  groupKey, index, group, onUpdate,
}: {
  groupKey: GroupKey;
  index: number;
  group: { model: string; apiKey: string; baseUrl: string };
  onUpdate: (key: GroupKey, field: 'model' | 'apiKey' | 'baseUrl', value: string) => void;
}) => {
  const [open, setOpen] = useState(index === 0 || !!group.apiKey);
  const titles: Record<GroupKey, string> = {
    group1: 'Fix & Debug Agents',
    group2: 'Core Build Agents',
    group3: 'Documentation Agents',
  };
  const roles: Record<GroupKey, string> = {
    group1: 'Fixer · Debugger · Reasoner',
    group2: 'Architect · Designer · Testbench · Verifier · Manager',
    group3: 'Documenter · Reporter',
  };

  return (
    <div className={`byok-group${open ? ' byok-group--open' : ''}`}>
      <button className="byok-group-header" onClick={() => setOpen(!open)} type="button">
        <div>
          <span className="byok-group-index">{index + 1}</span>
          <span className="byok-group-title">{titles[groupKey]}</span>
          <span className="byok-group-roles">{roles[groupKey]}</span>
        </div>
        {open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </button>
      {open && (
        <div className="byok-group-body">
          <div className="byok-field-row">
            <div className="byok-field">
              <label className="byok-field-label">Model</label>
              <input
                className="byok-field-input"
                placeholder={DEFAULT_BYOK_MODEL}
                value={group.model}
                onChange={(e) => onUpdate(groupKey, 'model', e.target.value)}
              />
            </div>
            <div className="byok-field">
              <label className="byok-field-label">Base URL <span className="byok-optional">(optional)</span></label>
              <input
                className="byok-field-input"
                placeholder="Leave blank for OpenAI-compatible endpoints"
                value={group.baseUrl}
                onChange={(e) => onUpdate(groupKey, 'baseUrl', e.target.value)}
              />
            </div>
          </div>
          <div className="byok-field">
            <label className="byok-field-label">API Key</label>
            <input
              className="byok-field-input"
              type="password"
              placeholder="sk-... or provider-specific key"
              value={group.apiKey}
              onChange={(e) => onUpdate(groupKey, 'apiKey', e.target.value)}
            />
            <MaskedKey value={group.apiKey} />
          </div>
        </div>
      )}
    </div>
  );
};

export const BillingModal = ({
  isOpen,
  onClose,
  onKeySaved,
  initialMode = 'byok',
}: {
  isOpen: boolean;
  onClose: () => void;
  onKeySaved: () => void;
  initialMode?: ModalMode;
}) => {
  const [mode, setMode] = useState<ModalMode>(initialMode);
  const [groups, setGroups] = useState<GroupState>(DEFAULT_GROUPS);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [quickMode, setQuickMode] = useState(true);
  const [quickKey, setQuickKey] = useState('');
  const [quickModel, setQuickModel] = useState(DEFAULT_BYOK_MODEL);
  const [quickBaseUrl, setQuickBaseUrl] = useState(DEFAULT_BYOK_BASE_URL);
  const [saved, setSaved] = useState(false);

  const applyParsed = (parsed: Partial<Record<GroupKey, ByokServerGroup>>) => {
    const nextGroups: GroupState = {
      group1: { model: parsed.group1?.model || DEFAULT_BYOK_MODEL, apiKey: parsed.group1?.api_key || '', baseUrl: parsed.group1?.base_url || DEFAULT_BYOK_BASE_URL },
      group2: { model: parsed.group2?.model || DEFAULT_BYOK_MODEL, apiKey: parsed.group2?.api_key || '', baseUrl: parsed.group2?.base_url || DEFAULT_BYOK_BASE_URL },
      group3: { model: parsed.group3?.model || DEFAULT_BYOK_MODEL, apiKey: parsed.group3?.api_key || '', baseUrl: parsed.group3?.base_url || DEFAULT_BYOK_BASE_URL },
    };
    setGroups(nextGroups);
    const k1 = nextGroups.group1.apiKey;
    if (k1 && k1 === nextGroups.group2.apiKey && k1 === nextGroups.group3.apiKey) {
      setQuickKey(k1);
      setQuickModel(nextGroups.group1.model);
      setQuickBaseUrl(nextGroups.group1.baseUrl);
      setQuickMode(true);
    } else {
      setQuickMode(false);
    }
  };

  useEffect(() => {
    if (!isOpen) { setSaved(false); return; }
    setError('');
    setMode(initialMode);

    const loadKeys = async () => {
      // Try server-side first
      try {
        const { data: { session } } = await supabase.auth.getSession();
        if (session?.access_token) {
          const resp = await fetch(`${API_BASE}/profile/byok`, {
            headers: { Authorization: `Bearer ${session.access_token}` },
          });
          if (resp.ok) {
            const serverData = await resp.json();
            if (serverData && serverData.group1) {
              localStorage.setItem('agentic_byok_key', JSON.stringify(serverData));
              applyParsed(serverData);
              return;
            }
          }
        }
      } catch {
        // fall through
      }

      // localStorage fallback
      try {
        const raw = localStorage.getItem('agentic_byok_key');
        if (!raw) {
          setGroups(DEFAULT_GROUPS);
          setQuickKey('');
          setQuickModel(DEFAULT_BYOK_MODEL);
          setQuickBaseUrl(DEFAULT_BYOK_BASE_URL);
          setQuickMode(true);
          return;
        }
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') applyParsed(parsed);
      } catch {
        setGroups(DEFAULT_GROUPS);
        setQuickKey('');
        setQuickModel(DEFAULT_BYOK_MODEL);
        setQuickBaseUrl(DEFAULT_BYOK_BASE_URL);
        setQuickMode(true);
      }
    };

    loadKeys();
  }, [isOpen, initialMode]);

  const hasAnyKey = quickMode
    ? quickKey.trim().length > 0
    : Object.values(groups).some((g) => g.apiKey.trim());

  const updateGroup = (key: GroupKey, field: 'model' | 'apiKey' | 'baseUrl', value: string) => {
    setGroups((prev) => ({ ...prev, [key]: { ...prev[key], [field]: value } }));
  };

  const handleSaveByok = async () => {
    if (!hasAnyKey) return;
    setSaving(true);
    setError('');

    let payload: ByokConfigPayload;
    if (quickMode) {
      const common = {
        model: quickModel.trim() || DEFAULT_BYOK_MODEL,
        api_key: quickKey.trim(),
        base_url: quickBaseUrl.trim() || DEFAULT_BYOK_BASE_URL,
      };
      payload = { group1: { ...common }, group2: { ...common }, group3: { ...common } };
    } else {
      payload = {
        group1: { model: groups.group1.model.trim() || DEFAULT_BYOK_MODEL, api_key: groups.group1.apiKey.trim(), base_url: groups.group1.baseUrl.trim() || DEFAULT_BYOK_BASE_URL },
        group2: { model: groups.group2.model.trim() || DEFAULT_BYOK_MODEL, api_key: groups.group2.apiKey.trim(), base_url: groups.group2.baseUrl.trim() || DEFAULT_BYOK_BASE_URL },
        group3: { model: groups.group3.model.trim() || DEFAULT_BYOK_MODEL, api_key: groups.group3.apiKey.trim(), base_url: groups.group3.baseUrl.trim() || DEFAULT_BYOK_BASE_URL },
      };
    }

    try {
      localStorage.setItem('agentic_byok_key', JSON.stringify(payload));

      try {
        const { data: { session } } = await supabase.auth.getSession();
        if (session?.access_token) {
          const resp = await fetch(`${API_BASE}/profile/byok`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${session.access_token}`,
            },
            body: JSON.stringify(payload),
          });
          if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            console.warn('BYOK server save failed:', errData);
          }
        }
      } catch (serverErr) {
        console.warn('BYOK server save error (non-fatal):', serverErr);
      }

      setSaved(true);
      setTimeout(() => { onKeySaved(); onClose(); }, 600);
    } catch (err: unknown) {
      setError(toUserError(err instanceof Error ? err.message : err, 'Failed to save configuration. Please try again.'));
    } finally {
      setSaving(false);
    }
  };

  if (!isOpen) return null;

  return (
    <AnimatePresence>
      <div className="byok-overlay" onClick={onClose}>
        <motion.div
          className="byok-dialog"
          initial={{ opacity: 0, scale: 0.97, y: 8 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.97, y: 8 }}
          transition={{ duration: 0.18 }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="byok-header">
            <div>
              <h2 className="byok-title">Model Settings</h2>
              <p className="byok-subtitle">
                Choose Infinite or connect your own OpenAI-compatible model.
              </p>
            </div>
            <button className="byok-close" onClick={onClose} aria-label="Close">
              <X size={18} />
            </button>
          </div>

          {/* Mode Selector Tabs */}
          <div className="byok-mode-tabs">
            <button
              className={`byok-mode-tab${mode === 'agentic' ? ' active' : ''}`}
              onClick={() => setMode('agentic')}
            >
              <Cpu size={15} />
              Infinite
            </button>
            <button
              className={`byok-mode-tab${mode === 'byok' ? ' active' : ''}`}
              onClick={() => setMode('byok')}
            >
              <KeyRound size={15} />
              Bring Your Own Key
            </button>
          </div>

          {/* ── AgentIC Model Panel ── */}
          {mode === 'agentic' && (
            <div className="byok-agentic-panel">
              <div className="byok-onboarding">
                <div className="byok-onboarding-card">
                  <span className="byok-onboarding-icon"><Fingerprint size={16} /></span>
                  <div>
                    <strong>Hosted Infinite model</strong>
                    <p>AgentIC runs the chip pipeline with its tuned RTL generation model. No user key required.</p>
                  </div>
                </div>
                <div className="byok-onboarding-card">
                  <span className="byok-onboarding-icon"><Sparkles size={16} /></span>
                  <div>
                    <strong>Available now</strong>
                    <p>Uses your server-side Infinity/AgentIC model configuration. Billing can be added later.</p>
                  </div>
                </div>
              </div>

              <div className="byok-agentic-active">
                <div className="byok-agentic-badge">
                  <Check size={16} />
                  <div>
                    <strong>Infinite Ready</strong>
                    <span>AgentIC will use the hosted Infinity model configured on this server.</span>
                  </div>
                </div>
                <button className="byok-agentic-btn" onClick={onClose}>
                  Use Infinite
                  <Sparkles size={15} />
                </button>
              </div>
            </div>
          )}

          {/* ── BYOK Panel ── */}
          {mode === 'byok' && (
            <div className="byok-byok-panel">
              <div className="byok-onboarding">
                <div className="byok-onboarding-card">
                  <span className="byok-onboarding-icon"><LockKeyhole size={16} /></span>
                  <div>
                    <strong>Encrypted &amp; synced to your account</strong>
                    <p>Keys are encrypted server-side and synced to your profile when auth is available.</p>
                  </div>
                </div>
                <div className="byok-onboarding-card">
                  <span className="byok-onboarding-icon"><KeyRound size={16} /></span>
                  <div>
                    <strong>Bring your own LLM provider</strong>
                    <p>Use any OpenAI-compatible API for model calls, or switch back to Infinite for the hosted model.</p>
                  </div>
                </div>
              </div>

              {/* Mode toggle */}
              <div className="byok-mode-toggle">
                <button
                  className={`byok-mode-btn${quickMode ? ' byok-mode-btn--active' : ''}`}
                  onClick={() => setQuickMode(true)}
                >
                  One Model
                </button>
                <button
                  className={`byok-mode-btn${!quickMode ? ' byok-mode-btn--active' : ''}`}
                  onClick={() => setQuickMode(false)}
                >
                  3 Role Groups
                </button>
              </div>

              {/* Quick mode */}
              {quickMode && (
                <div className="byok-quick">
                  <p className="byok-quick-hint">
                    One model and one API key for all agent roles. Recommended for most users.
                  </p>
                  <div className="byok-field-row">
                    <div className="byok-field">
                      <label className="byok-field-label">Model</label>
                      <input
                        className="byok-field-input"
                        placeholder={DEFAULT_BYOK_MODEL}
                        value={quickModel}
                        onChange={(e) => setQuickModel(e.target.value)}
                      />
                    </div>
                    <div className="byok-field">
                      <label className="byok-field-label">Base URL <span className="byok-optional">(optional)</span></label>
                      <input
                        className="byok-field-input"
                        placeholder="Leave blank for OpenAI-compatible endpoints"
                        value={quickBaseUrl}
                        onChange={(e) => setQuickBaseUrl(e.target.value)}
                      />
                    </div>
                  </div>
                  <div className="byok-field">
                    <label className="byok-field-label">API Key</label>
                    <input
                      className="byok-field-input"
                      type="password"
                      placeholder="sk-... or provider-specific key"
                      value={quickKey}
                      onChange={(e) => setQuickKey(e.target.value)}
                      autoFocus
                    />
                    <MaskedKey value={quickKey} />
                  </div>
                  <div className="byok-guidance-callout">
                    <strong>Quick Setup</strong>
                    <p>Paste one API key and save it. Builds can run with BYOK immediately.</p>
                  </div>
                </div>
              )}

              {/* Advanced mode */}
              {!quickMode && (
                <div className="byok-advanced">
                  <p className="byok-advanced-hint">
                    Assign different models/keys to each agent group for cost optimization.
                  </p>
                  {(['group1', 'group2', 'group3'] as GroupKey[]).map((key, index) => (
                    <ByokGroupCard
                      key={key}
                      groupKey={key}
                      index={index}
                      group={groups[key]}
                      onUpdate={updateGroup}
                    />
                  ))}
                </div>
              )}

              {error && <div className="byok-error">{error}</div>}

              {/* Actions */}
              <div className="byok-footer">
                <span className="byok-footer-note">
                  BYOK saves model credentials for your workspace.
                </span>
                <button className="byok-cancel-btn" onClick={onClose} disabled={saving}>
                  Cancel
                </button>
                <button
                  className={`byok-save-btn${saved ? ' byok-save-btn--done' : ''}`}
                  onClick={handleSaveByok}
                  disabled={saving || !hasAnyKey}
                >
                  {saved ? (
                    <><Check size={16} /> Saved</>
                  ) : saving ? (
                    'Saving…'
                  ) : (
                    'Save & Continue'
                  )}
                </button>
              </div>
            </div>
          )}
        </motion.div>
      </div>
    </AnimatePresence>
  );
};
