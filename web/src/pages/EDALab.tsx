import { useState, useEffect } from 'react';
import Editor from '@monaco-editor/react';
import { api } from '../api';
import { Play, CheckCircle, Wand2, TerminalSquare, Cpu, XCircle, Layers, FileDown, Eye, ExternalLink, Beaker } from 'lucide-react';
import { toUserError } from '../utils/errorFormatter';

const IS_CLOUD_DEPLOY = import.meta.env.VITE_IS_CLOUD === 'true';

export const EDALab = () => {
    const [code, setCode] = useState<string>(
`module simple_counter (
    input clk,
    input rst,
    output logic [3:0] count
);

    always_ff @(posedge clk or posedge rst) begin
        if (rst) begin
            count <= 4'b0000;
        end else begin
            count <= count + 1'b1;
        end
    end

endmodule

// Testbench
module tb_simple_counter;
    logic clk;
    logic rst;
    logic [3:0] count;

    simple_counter dut (
        .clk(clk),
        .rst(rst),
        .count(count)
    );

    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_simple_counter);
        
        rst = 1;
        #10;
        rst = 0;
        
        #100;
        $display("Completed simulation. Final count = %d", count);
        $finish;
    end
endmodule
`);
    const [output, setOutput] = useState<string>('');
    const [vcdData, setVcdData] = useState<string | null>(null);
    const [viewerOpen, setViewerOpen] = useState<boolean>(false);
    const [loading, setLoading] = useState<'syntax' | 'synthesize' | 'simulate' | 'testbench' | 'ai' | null>(null);
    const [theme, setTheme] = useState<'vs-dark' | 'vs-light'>('vs-dark');

    useEffect(() => {
        // Observers for theme change to update monaco
        const observer = new MutationObserver(() => {
            setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'vs-dark' : 'vs-light');
        });
        observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'vs-dark' : 'vs-light');
        return () => observer.disconnect();
    }, []);

    const extractTopModule = () => {
        const matches = [...code.matchAll(/module\s+([a-zA-Z0-9_]+)/g)];
        if (matches.length === 0) return 'top';
        
        // Find the first module that isn't a testbench for synthesis
        const designModule = matches.find(m => !m[1].toLowerCase().includes('tb') && !m[1].toLowerCase().includes('test'));
        return designModule ? designModule[1] : matches[0][1];
    };

    const extractTbModule = () => {
        const matches = [...code.matchAll(/module\s+([a-zA-Z0-9_]+)/g)];
        if (matches.length === 0) return 'tb_top';

        // Find the first module that IS a testbench
        const tbModule = matches.find(m => m[1].toLowerCase().includes('tb') || m[1].toLowerCase().includes('test'));
        return tbModule ? tbModule[1] : `tb_${extractTopModule()}`;
    };

    const runSyntaxCheck = async () => {
        setLoading('syntax');
        setOutput('Running Verilator syntax check...\n');
        try {
            const res = await api.post('/lab/syntax-check', { code, top_module: extractTopModule() });
            if (res.data.success) {
                setOutput(prev => prev + '\n✅ Syntax Pass (Verilator)\n' + res.data.logs);
            } else {
                setOutput(prev => prev + '\n❌ Syntax Failed (Verilator)\n' + res.data.logs);
            }
        } catch (e: any) {
            setOutput(prev => prev + '\n❌ Error: ' + toUserError(e.response?.data?.detail || e.message, 'Syntax check failed. Please try again.'));
        }
        setLoading(null);
    };

    const runSynthesis = async () => {
        setLoading('synthesize');
        setOutput('Synthesizing RTL (Yosys)...\n');
        try {
            const res = await api.post('/lab/synthesize', { code, top_module: extractTopModule() });
            if (res.data.success) {
                setOutput(prev => prev + '\n✅ Synthesis Pass (Yosys)\n' + res.data.logs);
            } else {
                setOutput(prev => prev + '\n❌ Synthesis Failed (Yosys)\n' + res.data.logs);
            }
        } catch (e: any) {
            setOutput(prev => prev + '\n❌ Error: ' + toUserError(e.response?.data?.detail || e.message, 'Synthesis failed. Please try again.'));
        }
        setLoading(null);
    };

    const genTestbench = async () => {
        setLoading('testbench');
        setOutput('Applying AgentIC LLM to generate testbench...\n');
        try {
            const res = await api.post('/lab/generate-testbench', { code });
            if (res.data.success) {
                // Prepend an extra newline and append the testbench to the existing code
                setCode(prev => prev + '\n\n// === AgentIC Auto-Generated Testbench ===\n' + res.data.testbench);
                setOutput(prev => prev + '\n✅ Testbench generated and appended successfully!\n');
            } else {
                setOutput(prev => prev + '\n❌ Failed to generate testbench: ' + res.data.error);
            }
        } catch (e: any) {
            setOutput(prev => prev + '\n❌ Error: ' + toUserError(e.response?.data?.detail || e.message, 'Testbench generation failed. Please try again.'));
        }
        setLoading(null);
    };

    const runSimulate = async () => {
        setLoading('simulate');
        setOutput('Setting up Icarus Verilog Simulation...\n');
        setVcdData(null);
        try {
            const res = await api.post('/lab/simulate', { code, top_module: extractTbModule() });
            if (res.data.success) {
                setOutput(prev => prev + '\n✅ Simulation Complete\n' + res.data.logs);
                if (res.data.vcd) {
                    setVcdData(res.data.vcd);
                    setOutput(prev => prev + '\n\n🌊 VCD Waveform captured! Click the download button above to view locally.');
                }
            } else {
                setOutput(prev => prev + '\n❌ Simulation Failed\n' + res.data.logs);
            }
        } catch (e: any) {
            setOutput(prev => prev + '\n❌ Error: ' + toUserError(e.response?.data?.detail || e.message, 'Simulation failed. Please try again.'));
        }
        setLoading(null);
    };

    const askAIAssist = async () => {
        setLoading('ai');
        setOutput(prev => prev + '\n\n🔍 Running syntax check before AI analysis...\n');
        try {
            const res = await api.post('/lab/ai-assist', { 
                query: "Analyze this Verilog code for ALL issues: syntax errors, logical bugs, and synthesizability problems. Fix everything and produce fully synthesizable, error-free Verilog.",
                code 
            });
            
            const { fixed_code, line_changes, explanation, response: responseText } = res.data;
            
            if (fixed_code) {
                setCode(fixed_code);
                
                // Build a clear diff display for the console
                let diffOutput = '\n🤖 [AI Code Fixer]: Code fixed and updated in editor!\n\n';
                
                if (line_changes && line_changes.length > 0) {
                    diffOutput += '━━━ Changes Made ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n';
                    for (const change of line_changes) {
                        if (change.type === 'modified') {
                            diffOutput += `\n📝 Line ${change.line}: Modified\n`;
                            diffOutput += `  ❌ ${change.old}\n`;
                            diffOutput += `  ✅ ${change.new}\n`;
                        } else if (change.type === 'removed') {
                            diffOutput += `\n🗑️  Line ${change.line}: Removed\n`;
                            diffOutput += `  ❌ ${change.old}\n`;
                        } else if (change.type === 'added') {
                            diffOutput += `\n➕ Line ${change.line}: Added\n`;
                            diffOutput += `  ✅ ${change.new}\n`;
                        }
                    }
                    diffOutput += '\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n';
                    diffOutput += `\n📊 Summary: ${line_changes.filter((c: any) => c.type === 'modified').length} modified, `;
                    diffOutput += `${line_changes.filter((c: any) => c.type === 'added').length} added, `;
                    diffOutput += `${line_changes.filter((c: any) => c.type === 'removed').length} removed\n`;
                } else {
                    diffOutput += '✨ No line-level changes detected (code may have been restructured).\n';
                }
                
                if (explanation) {
                    diffOutput += '\n📋 AI Explanation:\n' + explanation;
                }
                
                setOutput(prev => prev + diffOutput);
            } else {
                // Fallback: LLM didn't return structured code
                setOutput(prev => prev + '\n🤖 [AI Code Fixer]:\n' + (responseText || 'No response received.'));
            }
        } catch (e: any) {
            setOutput(prev => prev + '\n❌ Error calling AI: ' + toUserError(e.response?.data?.detail || e.message, 'AI analysis failed. Please try again.'));
        }
        setLoading(null);
    };

    const downloadVCD = () => {
        if (!vcdData) return;
        const blob = new Blob([vcdData], { type: 'text/plain' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = 'dump.vcd';
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
    };

    const openGTKWave = async () => {
        if (!vcdData) return;
        setOutput(prev => prev + '\n\nLaunch command sent to GTKWave locally on server...');
        try {
            const res = await api.post('/lab/gtkwave', { vcd_data: vcdData });
            if (res.data.success) {
                setOutput(prev => prev + `\n🌊 ${res.data.message}`);
            } else {
                setOutput(prev => prev + '\n❌ GTKWave failed to launch... Make sure the AgentIC backend is running on a desktop UI.');
            }
        } catch (e: any) {
            setOutput(prev => prev + '\n❌ API Error: ' + toUserError(e.response?.data?.detail || e.message, 'Unable to launch waveform viewer.'));
        }
    };

    const openCloudViewer = () => {
        setViewerOpen(true);
    };

    // Whenever viewer is open, we send the VCD data into the iframe via postMessage
    useEffect(() => {
        if (viewerOpen && vcdData) {
            // Need a slight delay to ensure iframe mounted
            const iframe = document.getElementById('vcdIframe') as HTMLIFrameElement;
            if (iframe) {
               iframe.onload = () => {
                   iframe.contentWindow?.postMessage({ type: 'load_vcd', vcd: vcdData }, '*');
               };
               // If already loaded
               if (iframe.contentWindow) {
                   setTimeout(() => {
                       iframe.contentWindow?.postMessage({ type: 'load_vcd', vcd: vcdData }, '*');
                   }, 500);
               }
            }
        }
    }, [viewerOpen, vcdData]);

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)', gap: '0', boxSizing: 'border-box' }}>
            {/* ── Toolbar ── */}
            <div className="eda-toolbar" style={{ animation: 'reveal-up 0.3s var(--ease) both' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem' }}>
                    <h2 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', margin: 0, fontSize: '1.1rem', fontWeight: 700, letterSpacing: '-0.02em' }}>
                        <Cpu size={20} style={{ color: 'var(--accent)' }} />
                        <span>EDA <span className="gradient-text">Lab</span></span>
                    </h2>
                </div>
                <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', alignItems: 'center' }}>
                    <button
                        onClick={runSyntaxCheck}
                        disabled={!!loading}
                        className="shimmer-btn"
                        style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'var(--accent)', opacity: loading && loading !== 'syntax' ? 0.5 : 1 }}>
                        <span className="shimmer-btn-content">
                            {loading === 'syntax' ? <span className="spinner"/> : <CheckCircle size={14} />}
                            Syntax
                        </span>
                    </button>
                    <button
                        onClick={runSynthesis}
                        disabled={!!loading}
                        className="shimmer-btn"
                        style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'linear-gradient(135deg, #d97706, #eab308)', opacity: loading && loading !== 'synthesize' ? 0.5 : 1 }}>
                        <span className="shimmer-btn-content">
                            {loading === 'synthesize' ? <span className="spinner"/> : <Layers size={14} />}
                            Synthesize
                        </span>
                    </button>
                    <button
                        onClick={runSimulate}
                        disabled={!!loading}
                        className="shimmer-btn"
                        style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'linear-gradient(135deg, #059669, #10b981)', opacity: loading && loading !== 'simulate' ? 0.5 : 1 }}>
                        <span className="shimmer-btn-content">
                            {loading === 'simulate' ? <span className="spinner"/> : <Play size={14} />}
                            Simulate
                        </span>
                    </button>
                    <button
                        onClick={genTestbench}
                        disabled={!!loading}
                        className="shimmer-btn"
                        style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: '#2563eb', color: '#fff', opacity: loading && loading !== 'testbench' ? 0.5 : 1 }}>
                        <span className="shimmer-btn-content" style={{gap: '0.4rem', display: 'flex', alignItems: 'center'}}>
                            {loading === 'testbench' ? <span className="spinner"/> : <Beaker size={14} />}
                            Generate Testbench
                        </span>
                    </button>
                    {vcdData && (
                        <>
                            <div style={{ width: 1, height: 24, background: 'var(--border)', margin: '0 0.25rem' }} />
                            <button
                                onClick={openCloudViewer}
                                className="shimmer-btn"
                                style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'linear-gradient(135deg, #0369a1, #0284c7)' }}>
                                <span className="shimmer-btn-content"><ExternalLink size={14} /> Viewer</span>
                            </button>
                            {!IS_CLOUD_DEPLOY && (
                                <button
                                    onClick={openGTKWave}
                                    className="shimmer-btn"
                                    style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'linear-gradient(135deg, #2563eb, #3b82f6)' }}>
                                    <span className="shimmer-btn-content"><Eye size={14} /> GTKWave</span>
                                </button>
                            )}
                            <button
                                onClick={downloadVCD}
                                className="shimmer-btn"
                                style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'linear-gradient(135deg, #7c3aed, #8b5cf6)' }}>
                                <span className="shimmer-btn-content"><FileDown size={14} /> .VCD</span>
                            </button>
                        </>
                    )}
                    <div style={{ flex: 1, minWidth: '0.5rem' }} />
                    <button
                        onClick={askAIAssist}
                        disabled={!!loading}
                        className="shimmer-btn"
                        style={{ padding: '0.4rem 0.85rem', fontSize: '0.8rem', background: 'linear-gradient(135deg, #2563eb, #7c3aed)', opacity: loading && loading !== 'ai' ? 0.5 : 1 }}>
                        <span className="shimmer-btn-content">
                            {loading === 'ai' ? <span className="spinner"/> : <Wand2 size={14} />}
                            AI Fixer
                        </span>
                    </button>
                </div>
            </div>

            {/* ── Editor + Console ── */}
            <div style={{ display: 'flex', gap: 0, flex: 1, minHeight: 0 }}>
                {viewerOpen ? (
                    <div className="eda-viewer-panel">
                        <div className="eda-viewer-header">
                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                <ExternalLink size={14}/> VCDrom Viewer
                            </div>
                            <button
                                onClick={() => setViewerOpen(false)}
                                className="eda-viewer-close-btn">
                                Close
                            </button>
                        </div>
                        <iframe
                            id="vcdIframe"
                            src="/vcdrom/index.html"
                            style={{ flex: 1, border: 'none', background: 'var(--bg-card)' }}
                            title="VCD Viewer"
                        />
                    </div>
                ) : (
                    <div style={{ flex: 1.2, display: 'flex', flexDirection: 'column', overflow: 'hidden', borderRight: '1px solid var(--border)' }}>
                        <div className="eda-panel-header">
                            <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                                <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--success)', display: 'inline-block' }} />
                                testbench.sv
                            </span>
                            <span style={{ fontSize: '0.72rem', fontWeight: 'normal', color: 'var(--text-dim)' }}>Monaco Editor</span>
                        </div>
                        <Editor
                            height="100%"
                            language="verilog"
                            theme={theme}
                            value={code}
                            onChange={(val) => setCode(val || '')}
                            options={{
                                minimap: { enabled: false },
                                fontSize: 14,
                                fontFamily: "'Fira Code', monospace",
                                scrollBeyondLastLine: false,
                                smoothScrolling: true,
                                padding: { top: 12 },
                                renderLineHighlight: 'gutter',
                                cursorBlinking: 'smooth',
                                cursorSmoothCaretAnimation: 'on',
                            }}
                        />
                    </div>
                )}

                {/* Console */}
                <div className="eda-console">
                    <div className="eda-console-header">
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <TerminalSquare size={14}/> Console
                        </div>
                        <button
                            onClick={() => setOutput('')}
                            className="eda-console-clear-btn"
                            title="Clear Console">
                            <XCircle size={14} />
                        </button>
                    </div>
                    <div className="eda-console-body">
                        {output || '$ System ready.\n  Write Verilog → click Syntax / Synthesize / Simulate\n  AI Fixer auto-diagnoses & repairs your code'}
                    </div>
                </div>
            </div>
        </div>
    );
};
