import React, { useEffect, useRef, useState } from 'react';

/* Left-border accent colors per thought type — no emojis */
const BORDER_COLORS: Record<string, string> = {
    thought:     '#5b8fb9',
    tool_call:   '#d47c3a',
    tool_result: '#4a7c59',
    observation: '#5b8fb9',
    decision:    '#8b6cc1',
    user_action: '#8b6cc1',
    warning:     '#c0392b',
};

interface ActivityEvent {
    type: string;
    message: string;
    state: string;
    timestamp: number | string;
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
    changes?: Array<{
        original_request?: string;
        constraint?: string;
        chosen_substitute?: string;
        user_explanation?: string;
    }>;
}

interface Props {
    events: ActivityEvent[];
    thinkingData?: { agent_name: string; message: string } | null;
}

/* Noise patterns to suppress from the live log */
const NOISE_PATTERNS = [
    /^Transitioning:/i,
    /^Beginning stage:/i,
    /^Generating approval summary/i,
    /^Waiting for user approval/i,
    /^User approved stage/i,
    /^User rejected stage/i,
    /^Human-in-the-loop mode enabled/i,
    /^LLM backend:/i,
    /^Approved:/i,
    /^AgentIC Compute Engine selected/i,
    /^Compute engine ready/i,
    /^Starting Build Process for/i,
    /^Build started for/i,
    /^Stage \w+ complete.*awaiting approval/i,
    /^\[Orchestrator\] (Beginning|Generating|Waiting|Human-in)/i,
];

/* Filter noise: skip internal transitions, raw file paths, empty messages, known noise */
function isNoise(evt: ActivityEvent): boolean {
    const msg = (evt.content || evt.message || '').trim();
    if (!msg) return true;
    if (/^\/[a-zA-Z0-9/_.-]+$/.test(msg)) return true;
    if (evt.type === 'transition') return true;
    for (const pattern of NOISE_PATTERNS) {
        if (pattern.test(msg)) return true;
    }
    return false;
}

/* Truncate full file paths to just the filename */
function shortenPaths(text: string): string {
    return text.replace(/\/(?:home|tmp|var|opt)\/[^\s]+\/([^\s/]+)/g, '$1');
}

/* Truncate long messages at ~120 chars */
function truncate(text: string, max = 120): { display: string; isTruncated: boolean } {
    if (text.length <= max) return { display: text, isTruncated: false };
    return { display: `${text.slice(0, max)}...`, isTruncated: true };
}

export const ActivityFeed: React.FC<Props> = ({ events, thinkingData }) => {
    const ref = useRef<HTMLDivElement>(null);
    const [autoScroll, setAutoScroll] = useState(true);
    const [expanded, setExpanded] = useState<Set<number>>(new Set());
    const lastTop = useRef(0);

    useEffect(() => {
        if (autoScroll && ref.current) {
            ref.current.scrollTop = ref.current.scrollHeight;
        }
    }, [events, autoScroll]);

    const onScroll = () => {
        if (!ref.current) return;
        const el = ref.current;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        if (el.scrollTop < lastTop.current && !atBottom) setAutoScroll(false);
        else if (atBottom) setAutoScroll(true);
        lastTop.current = el.scrollTop;
    };

    const toggle = (i: number) =>
        setExpanded(prev => {
            const s = new Set(prev);
            if (s.has(i)) {
                s.delete(i);
            } else {
                s.add(i);
            }
            return s;
        });

    /* Filter + deduplicate consecutive identical messages */
    const filtered = events.filter(e => {
        if (e.type === 'ping' || e.type === 'stream_end' || e.type === 'stage_complete') return false;
        if (isNoise(e)) return false;
        return true;
    });
    const rows: typeof filtered = [];
    for (const evt of filtered) {
        const prev = rows[rows.length - 1];
        if (prev && (prev.content || prev.message) === (evt.content || evt.message)) continue;
        rows.push(evt);
    }

    return (
        <div className="hitl-log">
            <div className="hitl-log-header">
                <span className="hitl-log-title">Live Log</span>
                {!autoScroll && (
                    <button
                        className="hitl-log-scroll"
                        onClick={() => {
                            setAutoScroll(true);
                            ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: 'smooth' });
                        }}
                    >
                        Latest
                    </button>
                )}
            </div>
            <div className="hitl-log-body" ref={ref} onScroll={onScroll}>
                {rows.length === 0 ? (
                    <div className="hitl-log-empty">Waiting for agent activity...</div>
                ) : (
                    rows.map((evt, i) => {
                        const type = evt.thought_type || 'thought';
                        const border = BORDER_COLORS[type] || BORDER_COLORS.thought;
                        const raw = shortenPaths(evt.content || evt.message || '');
                        const isExp = expanded.has(i);
                        const { display, isTruncated } = isExp
                            ? { display: raw, isTruncated: false }
                            : truncate(raw);

                        const ts =
                            typeof evt.timestamp === 'string'
                                ? evt.timestamp.split('T')[1]?.substring(0, 8) || evt.timestamp
                                : new Date((evt.timestamp as number) * 1000).toLocaleTimeString('en-US', {
                                      hour12: false,
                                  });
                        const stage = evt.state?.replace(/_/g, ' ') || '';
                        const changes = evt.type === 'spec_reconciled' ? (evt.changes || []) : [];

                        return (
                            <div
                                key={i}
                                className={`hitl-log-row${isTruncated || isExp ? ' hitl-log-row--expandable' : ''}`}
                                style={{ borderLeftColor: border }}
                                onClick={() => (isTruncated || isExp) && toggle(i)}
                            >
                                <span className="hitl-log-ts">{ts}</span>
                                {stage && <span className="hitl-log-badge">{stage}</span>}
                                <span className="hitl-log-msg">
                                    {display}
                                    {isTruncated && <span className="hitl-log-expand"> more</span>}
                                    {changes.length > 0 && (
                                        <span style={{ display: 'block', marginTop: 6 }}>
                                            {changes.slice(0, 3).map((change, idx) => (
                                                <span key={idx} style={{ display: 'block' }}>
                                                    {change.original_request || 'requested behavior'} → {change.chosen_substitute || 'feasible substitute'}
                                                </span>
                                            ))}
                                        </span>
                                    )}
                                </span>
                            </div>
                        );
                    })
                )}
                {/* Thinking indicator */}
                {thinkingData && (
                    <div className="hitl-log-row hitl-log-row--thinking" style={{ borderLeftColor: '#d47c3a' }}>
                        <span className="hitl-thinking-pulse" />
                        <span className="hitl-log-badge" style={{ fontStyle: 'italic' }}>
                            {thinkingData.agent_name || 'Agent'}
                        </span>
                        <span className="hitl-log-msg" style={{ fontStyle: 'italic', color: 'var(--text-secondary, #6b6560)' }}>
                            {thinkingData.message}
                        </span>
                    </div>
                )}
            </div>
        </div>
    );
};
