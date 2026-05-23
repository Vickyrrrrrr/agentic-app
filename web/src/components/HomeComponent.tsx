import {
  Activity,
  ArrowRight,
  BookOpen,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  Cpu,
  Layers3,
  Shield,
  Sparkles,
  Terminal,
  Workflow,
} from 'lucide-react';

const pipelineSteps = [
  { label: 'Interpret brief', detail: 'Natural language spec to architecture contract' },
  { label: 'Generate RTL', detail: 'Synthesis-oriented Verilog with review loop' },
  { label: 'Prove and verify', detail: 'Simulation, formal checks, and repair passes' },
  { label: 'Prepare layout', detail: 'Fabrication-ready outputs for Sky130 flow' },
];

const capabilityCards = [
  {
    icon: <BrainCircuit size={18} />,
    eyebrow: 'Autonomous Build',
    title: 'Spec to silicon with a tighter loop',
    description:
      'A single flow that moves from intent to RTL, verification, formal validation, and physical readiness without the usual handoff chaos.',
  },
  {
    icon: <Shield size={18} />,
    eyebrow: 'Recovery Logic',
    title: 'Self-healing when stages fail',
    description:
      'Retries are convergence-aware, failure fingerprints are tracked, and the system adapts instead of repeating the same broken attempt.',
  },
  {
    icon: <Workflow size={18} />,
    eyebrow: 'Control Surface',
    title: 'Human review only where it matters',
    description:
      'Run fully autonomous when speed matters, or stop at key checkpoints for approval, edits, and guided intervention.',
  },
];

const agentCards = [
  {
    icon: <Cpu size={18} />,
    title: 'Architect',
    description: 'Turns intent into ports, clocks, state machines, and implementation boundaries.',
  },
  {
    icon: <Terminal size={18} />,
    title: 'RTL Designer',
    description: 'Produces synthesizable HDL with review-oriented generation patterns.',
  },
  {
    icon: <Activity size={18} />,
    title: 'Verifier',
    description: 'Builds testbenches, simulates behavior, and isolates regressions quickly.',
  },
  {
    icon: <Shield size={18} />,
    title: 'Formal Prover',
    description: 'Uses assertions and proof workflows to check correctness beyond simulation.',
  },
  {
    icon: <Bot size={18} />,
    title: 'Deep Debugger',
    description: 'Explains failures causally and proposes the next best repair path.',
  },
];

export const HomeComponent = ({
  designsLength,
  totalBuilds,
  runningBuilds,
  successfulBuilds,
  setSelectedPage,
}: {
  designsLength: number;
  totalBuilds: number;
  runningBuilds: number;
  successfulBuilds: number;
  setSelectedPage: (page: string) => void;
}) => {
  return (
    <div className="home-minimal">
      <div className="home-grid-bg" />
      <div className="home-orb home-orb-a" />
      <div className="home-orb home-orb-b" />

      <section className="home-hero">
        <div className="home-hero-copy">
          <div className="home-hero-badge">
            <span className="home-hero-badge-dot" />
            AgentIC v3.0
          </div>

          <p className="home-kicker">Autonomous chip design, without the noisy interface.</p>

          <h1 className="home-hero-title">
            Natural language in.
            <span className="home-title-accent"> OSS layout evidence out.</span>
          </h1>

          <p className="home-hero-desc">
            Describe a digital circuit in plain English. AgentIC handles RTL generation,
            verification, formal proof, and layout preparation through a cleaner,
            production-shaped workflow.
          </p>

          <div className="home-hero-actions">
            <button className="home-btn-primary" onClick={() => setSelectedPage('Design Studio')}>
              <Terminal size={16} />
              Open Design Studio
              <ArrowRight size={14} />
            </button>
            <button className="home-btn-ghost" onClick={() => setSelectedPage('HITL Build')}>
              <Workflow size={16} />
              Review Pipeline
            </button>
            <button className="home-btn-ghost" onClick={() => setSelectedPage('Documentation')}>
              <BookOpen size={16} />
              Read Docs
            </button>
          </div>
        </div>

        <aside className="home-hero-panel">
          <div className="home-panel-topline">
            <Sparkles size={14} />
            Pipeline Outline
          </div>

          <div className="home-panel-list">
            {pipelineSteps.map((step, index) => (
              <div key={step.label} className="home-panel-item">
                <div className="home-panel-index">0{index + 1}</div>
                <div className="home-panel-body">
                  <div className="home-panel-label">{step.label}</div>
                  <div className="home-panel-detail">{step.detail}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="home-panel-footer">
            <div className="home-panel-chip">
              <CheckCircle2 size={14} />
              BYOK-first deployment
            </div>
            <div className="home-panel-chip">
              <Layers3 size={14} />
              Sky130 target flow
            </div>
          </div>
        </aside>
      </section>

      <section className="home-stats">
        <div className="home-stat">
          <span className="home-stat-meta">Workspace</span>
          <span className="home-stat-value">{designsLength || 0}</span>
          <span className="home-stat-label">active designs</span>
        </div>
        <div className="home-stat">
          <span className="home-stat-meta">Live</span>
          <span className="home-stat-value">{runningBuilds}</span>
          <span className="home-stat-label">running builds</span>
        </div>
        <div className="home-stat">
          <span className="home-stat-meta">History</span>
          <span className="home-stat-value">{totalBuilds}</span>
          <span className="home-stat-label">total builds stored</span>
        </div>
        <div className="home-stat">
          <span className="home-stat-meta">Succeeded</span>
          <span className="home-stat-value">{successfulBuilds}</span>
          <span className="home-stat-label">completed successfully</span>
        </div>
      </section>

      <section className="home-capabilities">
        <div className="home-section-heading">
          <h2 className="home-section-title">Why this feels more serious</h2>
          <p className="home-section-subtitle">
            Fewer decorative widgets, tighter hierarchy, and surfaces that explain the system
            instead of shouting over it.
          </p>
        </div>

        <div className="home-cap-grid">
          {capabilityCards.map((card) => (
            <article key={card.title} className="home-cap-card">
              <div className="home-cap-icon">{card.icon}</div>
              <p className="home-cap-eyebrow">{card.eyebrow}</p>
              <h3 className="home-cap-title">{card.title}</h3>
              <p className="home-cap-desc">{card.description}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="home-agents">
        <div className="home-section-heading">
          <h2 className="home-section-title">Agent architecture</h2>
          <p className="home-section-subtitle">
            The stack stays compact, but each role is explicit about what it owns in the flow.
          </p>
        </div>

        <div className="home-agent-list">
          {agentCards.map((agent) => (
            <article key={agent.title} className="home-agent-row">
              <div className="home-agent-icon">{agent.icon}</div>
              <div className="home-agent-info">
                <span className="home-agent-name">{agent.title}</span>
                <span className="home-agent-desc">{agent.description}</span>
              </div>
              <ChevronRight size={16} className="home-agent-arrow" />
            </article>
          ))}
        </div>
      </section>
    </div>
  );
};
