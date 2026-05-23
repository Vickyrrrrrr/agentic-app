import React from 'react';

interface BenchmarkingProps {
    selectedDesign: string;
}

export const Benchmarking: React.FC<BenchmarkingProps> = ({ selectedDesign }) => {
    const comparisons = [
        { metric: 'RTL to GDSII Time', agenticVal: '~15 Minutes', tradVal: 'Days/Weeks' },
        { metric: 'Spec Decomposition', agenticVal: 'Automated specification analysis', tradVal: 'Manual architecture review (weeks)' },
        { metric: 'Verification Methodology', agenticVal: 'Intelligent multi-class diagnosis', tradVal: 'Manual waveform debugging' },
        { metric: 'Agent Collaboration', agenticVal: 'Multi-agent collaborative pipeline', tradVal: 'Siloed engineer teams' },
        { metric: 'Self-Healing', agenticVal: 'Convergence-aware automated recovery', tradVal: 'Manual iteration' },
        { metric: 'Log Triage', agenticVal: 'Automated LLM Parsing', tradVal: 'Manual Grepping' },
        { metric: 'Licensing Cost', agenticVal: 'Open Source + API', tradVal: '$1M+ / seat' },
        { metric: 'DRC / LVS Violations', agenticVal: 'Auto-heal assisted', tradVal: 'Manual closure process' },
    ];

    const modules = [
        { name: 'Architect', cap: 'Natural language specification decomposition and structured contract generation' },
        { name: 'Reasoning Agent', cap: 'Iterative reasoning with observation-driven action planning' },
        { name: 'Self-Healing Pipeline', cap: 'Convergence-aware retry with metric-driven optimization' },
        { name: 'Deep Debugger', cap: 'Causal failure analysis with multi-perspective reasoning' },
        { name: 'Waveform Analyst', cap: 'Signal-level diagnostic analysis and root cause identification' },
    ];

    return (
        <div className="bench-page">
            <section className="app-hero-card bench-hero-card">
                <div className="app-hero-copy">
                    <span className="app-hero-kicker">BENCHMARKING</span>
                    <h2 className="app-hero-title">Compare AgentIC against conventional silicon delivery loops.</h2>
                    <p className="app-hero-subtitle">
                        Frame the selected design against slower, more manual engineering workflows to show where
                        automation, recovery, and multi-agent reasoning compress delivery time.
                    </p>
                </div>
                <div className="app-hero-meta">
                    <span className="app-hero-pill">{selectedDesign || 'No design selected'}</span>
                    <span className="app-hero-pill">{comparisons.length} benchmark dimensions</span>
                </div>
            </section>

            {!selectedDesign ? (
                <div className="app-empty-card">
                    <h3 className="app-empty-title">Select a design before comparing delivery paths.</h3>
                    <p className="app-empty-copy">
                        Benchmarking is most useful once a real design is in context. Open a build from history or launch
                        one from Design Studio, then return here to frame the result against traditional flows.
                    </p>
                </div>
            ) : (
                <>
                    <div className="bench-card">
                        <h3 className="bench-card-title">Cost & Efficiency Analysis</h3>
                        <div className="bench-table-wrap">
                            <table className="bench-table">
                                <thead>
                                    <tr>
                                        <th>Metric</th>
                                        <th>AgentIC</th>
                                        <th>Traditional Flow</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {comparisons.map((c) => (
                                        <tr key={c.metric}>
                                            <td className="bench-metric-name">{c.metric}</td>
                                            <td className="bench-val-good">{c.agenticVal}</td>
                                            <td className="bench-val-dim">{c.tradVal}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div className="bench-card">
                        <h3 className="bench-card-title">Core Module Architecture</h3>
                        <div className="bench-table-wrap">
                            <table className="bench-table">
                                <thead>
                                    <tr>
                                        <th>Module</th>
                                        <th>Capability</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {modules.map((m) => (
                                        <tr key={m.name}>
                                            <td className="bench-metric-name">{m.name}</td>
                                            <td>{m.cap}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </>
            )}
        </div>
    );
};
