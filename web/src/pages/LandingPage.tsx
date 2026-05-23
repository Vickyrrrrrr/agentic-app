import { useEffect, useRef, useState } from 'react';
import { supabase } from '../supabaseClient';
import { motion, AnimatePresence } from 'framer-motion';
import { gsap } from 'gsap';
import { Sparkles, ArrowRight, ShieldCheck, Terminal, Cpu, Layers } from 'lucide-react';

export const LandingPage = ({ onAuthSuccess }: { onAuthSuccess: () => void }) => {
  const [email, setEmail] = useState('');
  const [promptInput, setPromptInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [pdkChoice, setPdkChoice] = useState('sky130');
  const [netCount, setNetCount] = useState(12);

  const gridRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const textRef = useRef<HTMLHeadingElement | null>(null);
  const inputContainerRef = useRef<HTMLDivElement | null>(null);

  // Dynamic net rendering to represent creative silicon routing
  useEffect(() => {
    if (promptInput.length > 0) {
      setNetCount(Math.min(48, 12 + Math.floor(promptInput.length / 3)));
    } else {
      setNetCount(12);
    }
  }, [promptInput]);

  // GSAP animations for minimal, premium entrance
  useEffect(() => {
    // 1. Entrance animation for typography
    gsap.fromTo(
      textRef.current,
      { opacity: 0, y: 30 },
      { opacity: 1, y: 0, duration: 1.2, ease: 'power4.out', delay: 0.2 }
    );

    // 2. Entrance animation for the interactive composer capsule
    gsap.fromTo(
      inputContainerRef.current,
      { opacity: 0, scale: 0.96, y: 20 },
      { opacity: 1, scale: 1, y: 0, duration: 1, ease: 'power3.out', delay: 0.5 }
    );

    // 3. Animate the SVG silicon blocks path-drawing
    if (svgRef.current) {
      const paths = svgRef.current.querySelectorAll('.silicon-path');
      gsap.fromTo(
        paths,
        { strokeDashoffset: 1000, opacity: 0 },
        {
          strokeDashoffset: 0,
          opacity: 0.15,
          duration: 3,
          stagger: 0.12,
          ease: 'power2.inOut',
        }
      );

      const gates = svgRef.current.querySelectorAll('.silicon-gate');
      gsap.fromTo(
        gates,
        { scale: 0, opacity: 0 },
        {
          scale: 1,
          opacity: 0.25,
          duration: 1.5,
          stagger: 0.08,
          ease: 'elastic.out(1, 0.75)',
          delay: 0.8,
        }
      );
    }

    // 4. Subtle background matrix pattern scroll
    if (gridRef.current) {
      gsap.to(gridRef.current, {
        backgroundPosition: '40px 40px',
        duration: 20,
        ease: 'none',
        repeat: -1,
      });
    }
  }, []);

  const handleEmailSignup = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccessMsg('');
    setLoading(true);
    try {
      // Keep custom workspace prompt preserved in storage
      if (promptInput.trim()) {
        localStorage.setItem('agentic_landing_prompt', promptInput.trim());
        localStorage.setItem('agentic_landing_pdk', pdkChoice);
      }

      const { error: err } = await supabase.auth.signUp({
        email,
        password: crypto.randomUUID(),
        options: { emailRedirectTo: window.location.origin },
      });
      if (err) throw err;
      setSuccessMsg('Check your email to confirm and join the workspace.');
      onAuthSuccess();
    } catch (err: any) {
      setError(err.message || 'Something went wrong. Try again.');
    }
    setLoading(false);
  };

  const handleGoogle = async () => {
    setError('');
    if (promptInput.trim()) {
      localStorage.setItem('agentic_landing_prompt', promptInput.trim());
      localStorage.setItem('agentic_landing_pdk', pdkChoice);
    }
    const { error: err } = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: window.location.origin },
    });
    if (err) {
      setError(err.message);
    } else {
      onAuthSuccess();
    }
  };

  const handleStartSynthesis = () => {
    if (!promptInput.trim()) {
      setError('Describe your digital design to start synthesis.');
      return;
    }
    setError('');
    setShowAuthModal(true);
  };

  return (
    <div className="landing-premium-root">
      {/* ─── Grid Backdrop ─── */}
      <div className="landing-grid-bg" ref={gridRef} />
      <div className="landing-ambient-glow" />

      {/* ─── SVG Silicon Blueprint Canvas (Interactive GSAP layout) ─── */}
      <svg
        className="landing-silicon-blueprint"
        viewBox="0 0 1000 600"
        ref={svgRef}
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        {/* Core grid paths */}
        <path className="silicon-path" strokeDasharray="1000" stroke="#8F8F8F" strokeWidth="1" d="M100 100h800v400H100z" />
        <path className="silicon-path" strokeDasharray="1000" stroke="#8F8F8F" strokeWidth="0.5" d="M300 100v400M500 100v400M700 100v400" />
        <path className="silicon-path" strokeDasharray="1000" stroke="#8F8F8F" strokeWidth="0.5" d="M100 200h800M100 300h800M100 400h800" />

        {/* Dynamic logical nets that scale with user typing */}
        {Array.from({ length: netCount }).map((_, i) => {
          const startX = 150 + (i * 27) % 700;
          const startY = 150 + (i * 19) % 300;
          const isVert = i % 2 === 0;
          const len = 60 + (i * 13) % 150;
          const pathD = isVert
            ? `M${startX} ${startY}v${len}h${len / 2}`
            : `M${startX} ${startY}h${len}v${len / 2}`;
          return (
            <path
              key={i}
              className="silicon-net-wire"
              d={pathD}
              stroke={pdkChoice === 'sky130' ? '#60A5FA' : '#C9643E'}
              strokeWidth="1.5"
              strokeLinecap="round"
              opacity={promptInput.length > 0 ? 0.35 : 0.1}
            />
          );
        })}

        {/* Functional hardware silicon blocks */}
        <rect className="silicon-gate" x="140" y="140" width="80" height="40" rx="4" fill="#050505" stroke="#38BDF8" strokeWidth="1.5" />
        <text className="silicon-gate-label" x="180" y="164" fill="#38BDF8" textAnchor="middle" fontSize="10" fontFamily="monospace">CTRL_BUS</text>

        <rect className="silicon-gate" x="780" y="380" width="80" height="40" rx="4" fill="#050505" stroke="#C9643E" strokeWidth="1.5" />
        <text className="silicon-gate-label" x="820" y="404" fill="#C9643E" textAnchor="middle" fontSize="10" fontFamily="monospace">PDK_ALIGN</text>

        <rect className="silicon-gate" x="460" y="260" width="100" height="60" rx="6" fill="#050505" stroke="#65A30D" strokeWidth="1.5" opacity="0.8" />
        <text className="silicon-gate-label" x="510" y="295" fill="#65A30D" textAnchor="middle" fontSize="11" fontFamily="monospace">CORE_ALU_01</text>
      </svg>

      {/* ─── Premium Header ─── */}
      <header className="landing-premium-header">
        <div className="landing-logo">
          <div className="landing-logo-mark">A</div>
          <span className="landing-logo-text">AgentIC</span>
        </div>
        <div className="landing-top-actions">
          <button className="landing-top-btn" onClick={() => setShowAuthModal(true)}>
            Enter Workspace
          </button>
        </div>
      </header>

      {/* ─── Centered Canvas Content ─── */}
      <main className="landing-content-wrapper">
        <div className="landing-hero-block" ref={textRef}>
          <div className="landing-hero-badge">
            <span className="landing-hero-badge-dot" />
            Autonomous Hardware Synthesizer · v3.0
          </div>
          <h1 className="landing-premium-headline">
            Synthesize synthesizable silicon.<br />
            <span className="landing-headline-glow">Natural language in, GDSII out.</span>
          </h1>
          <p className="landing-premium-desc">
            Describe a custom CPU, peripheral, or logic gate array in plain language. 
            AgentIC handles formal bounds checking, self-healing timing verification, 
            and physical standard cell placement.
          </p>
        </div>

        {/* ─── Centered Premium Prompt Composer ─── */}
        <div className="landing-composer-wrap" ref={inputContainerRef}>
          <div className="landing-composer-inner">
            <textarea
              value={promptInput}
              onChange={(e) => setPromptInput(e.target.value)}
              placeholder="What digital design are we generating today? (e.g. AXI timer peripheral with formal verification...)"
              rows={3}
            />

            <div className="landing-composer-bar">
              <div className="landing-composer-tools">
                <div className="pdk-selection-group">
                  <button
                    className={`pdk-pill-btn ${pdkChoice === 'sky130' ? 'active' : ''}`}
                    onClick={() => setPdkChoice('sky130')}
                  >
                    <Cpu size={13} />
                    sky130 PDK
                  </button>
                  <button
                    className={`pdk-pill-btn ${pdkChoice === 'gf180' ? 'active' : ''}`}
                    onClick={() => setPdkChoice('gf180')}
                  >
                    <Layers size={13} />
                    gf180 PDK
                  </button>
                </div>
              </div>

              <button className="landing-synthesis-btn" onClick={handleStartSynthesis}>
                <span>Start Synthesis</span>
                <ArrowRight size={16} />
              </button>
            </div>
          </div>

          <div className="landing-composer-meta">
            <div className="landing-meta-item">
              <Terminal size={14} />
              <span>Full RTL design check</span>
            </div>
            <div className="landing-meta-item">
              <ShieldCheck size={14} />
              <span>DRC & timing signoff ready</span>
            </div>
          </div>
        </div>
      </main>

      {/* ─── Beautiful Glassmorphic Waitlist/Auth Modal ─── */}
      <AnimatePresence>
        {showAuthModal && (
          <div className="landing-modal-overlay">
            <motion.div
              className="landing-premium-modal"
              initial={{ opacity: 0, scale: 0.95, y: 15 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 15 }}
              transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
            >
              <div className="landing-modal-header">
                <h3>Enter Silicon Studio</h3>
                <p>Sign up or authenticate to secure your dedicated synthesizable workspace.</p>
                <button className="landing-modal-close" onClick={() => setShowAuthModal(false)}>×</button>
              </div>

              <div className="landing-modal-body">
                {/* Continue with Google */}
                <button className="landing-google" onClick={handleGoogle} disabled={loading}>
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                    <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
                    <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                    <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
                    <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
                  </svg>
                  Continue with Google
                </button>

                <div className="landing-divider">
                  <span className="landing-divider-line" />
                  <span className="landing-divider-text">or use email</span>
                  <span className="landing-divider-line" />
                </div>

                {/* Email Sign In */}
                <form className="landing-form" onSubmit={handleEmailSignup}>
                  <input
                    className="landing-input"
                    type="email"
                    placeholder="you@company.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                    autoComplete="email"
                  />
                  <button type="submit" className="landing-submit" disabled={loading || !email}>
                    {loading ? 'Entering...' : 'Reserve Access'}
                  </button>

                  {error && <div className="landing-alert landing-alert--error">{error}</div>}
                  {successMsg && <div className="landing-alert landing-alert--success">{successMsg}</div>}
                </form>
              </div>

              <div className="landing-modal-footer">
                <Sparkles size={14} />
                <span>Entering prompt will carry directly into your new build stream.</span>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      <footer className="landing-premium-footer">
        <p>© 2026 AgentIC. Built on SkyWater Open-Source Silicon Initiative.</p>
      </footer>
    </div>
  );
};
