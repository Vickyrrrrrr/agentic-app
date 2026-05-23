import React, { Suspense, lazy, useEffect, useState } from 'react';
import { Download, Layers3, ShieldCheck, XCircle } from 'lucide-react';
import { api, API_BASE } from '../api';

const FabricationViewer3D = lazy(() =>
  import('../components/FabricationViewer3D').then((m) => ({ default: m.FabricationViewer3D }))
);

interface FabricationProps {
    selectedDesign: string;
    hasGds?: boolean;
}

export const Fabrication: React.FC<FabricationProps> = ({ selectedDesign, hasGds }) => {
    const [viewMode, setViewMode] = useState<'2D' | '3D'>('2D');
    const [gdsArtifact, setGdsArtifact] = useState<{ name: string; size?: number } | null>(null);
    const [isLoadingArtifacts, setIsLoadingArtifacts] = useState(false);

    useEffect(() => {
        let cancelled = false;

        if (!selectedDesign) {
            void Promise.resolve().then(() => {
                if (!cancelled) setGdsArtifact(null);
            });
            return;
        }

        const loadArtifacts = async () => {
            setGdsArtifact(null);
            setIsLoadingArtifacts(true);
            try {
                const res = await api.get(`/build/artifacts/${selectedDesign}`);
                if (cancelled) return;
                const artifacts = Array.isArray(res.data?.artifacts) ? res.data.artifacts : [];
                const gds = artifacts.find((item: { name?: string; type?: string }) =>
                    item?.type === 'layout' && item?.name?.toLowerCase().endsWith('.gds')
                );
                setGdsArtifact(gds ? { name: gds.name, size: gds.size } : null);
            } catch {
                if (!cancelled) setGdsArtifact(null);
            } finally {
                if (!cancelled) setIsLoadingArtifacts(false);
            }
        };

        void loadArtifacts();

        return () => {
            cancelled = true;
        };
    }, [selectedDesign]);

    const handleDownloadGDS = () => {
        if (!gdsArtifact) return;
        window.location.href = `${API_BASE}/build/artifacts/${selectedDesign}/${encodeURIComponent(gdsArtifact.name)}`;
    };

    const effectiveHasGds = Boolean(gdsArtifact);

    return (
        <div className="fab-page">
            <section className="app-hero-card fab-hero-card">
                <div className="app-hero-copy">
                    <span className="app-hero-kicker">FABRICATION WORKSPACE</span>
                    <h2 className="app-hero-title">Inspect the handoff from design intent to manufacturable layout.</h2>
                    <p className="app-hero-subtitle">
                        Review whether a selected design has reached GDS availability, preview the layout surface,
                        and prepare the artifact set for downstream fabrication or review.
                    </p>
                </div>
                <div className="app-hero-meta">
                    <span className="app-hero-pill">
                        <Layers3 size={15} />
                        {selectedDesign || 'No design selected'}
                    </span>
                    <span className={`app-hero-pill ${effectiveHasGds ? 'is-success' : 'is-warn'}`}>
                        <ShieldCheck size={15} />
                        {effectiveHasGds ? 'GDS available' : hasGds || isLoadingArtifacts ? 'Locating GDS artifact' : 'Awaiting hardened layout'}
                    </span>
                </div>
            </section>

            {!selectedDesign ? (
                <div className="app-empty-card">
                    <h3 className="app-empty-title">Choose a design to inspect fabrication output.</h3>
                    <p className="app-empty-copy">
                        Fabrication artifacts appear after a build produces hardened layout data. Pick a design from
                        your history first, then return here to review GDS availability and layout views.
                    </p>
                </div>
            ) : (
                <>
                    <div className="fab-card">
                        <h3 className="fab-card-title">Tapeout Ready Files</h3>
                        <p className="fab-card-desc">
                            Download your final GDSII layout for physical manufacturing.
                        </p>

                        <div className="fab-download-row">
                            <input
                                type="text"
                                className="fab-design-input"
                                value={selectedDesign || 'No Design Selected'}
                                readOnly
                            />
                            <button
                                className={`fab-download-btn ${effectiveHasGds ? '' : 'fab-download-btn--disabled'}`}
                                onClick={handleDownloadGDS}
                                disabled={!effectiveHasGds}
                            >
                                {effectiveHasGds ? <><Download size={14} /> Download .gds</> : <><XCircle size={14} /> {isLoadingArtifacts ? 'Checking artifacts' : 'GDS Not Available'}</>}
                            </button>
                        </div>
                    </div>

                    <div className="fab-card">
                        <div className="fab-viewer-header">
                            <h3 className="fab-card-title">Layout Viewer</h3>
                            <div className="fab-view-toggle">
                                {(['2D', '3D'] as const).map(mode => (
                                    <button
                                        key={mode}
                                        className={`fab-view-btn ${viewMode === mode ? 'fab-view-btn--active' : ''}`}
                                        onClick={() => setViewMode(mode)}
                                    >
                                        {mode === '2D' ? '2D Top-Down' : '3D Stack'}
                                    </button>
                                ))}
                            </div>
                        </div>

                        <div className="fab-canvas-wrap">
                            {viewMode === '3D' ? (
                                <Suspense fallback={
                                    <div className="fab-canvas-loader">
                                        <div className="premium-loader">
                                            <span className="premium-loader-dot" />
                                            <span className="premium-loader-dot" />
                                            <span className="premium-loader-dot" />
                                        </div>
                                        Loading 3D Viewer...
                                    </div>
                                }>
                                    <FabricationViewer3D />
                                </Suspense>
                            ) : (
                                <div className="fab-canvas-placeholder">
                                    2D Render — Awaiting GDS parser integration. Switch to 3D Stack when you want
                                    the interactive package view.
                                </div>
                            )}
                        </div>
                    </div>
                </>
            )}
        </div>
    );
};
