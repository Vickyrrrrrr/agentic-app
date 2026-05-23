const SIGNAL_PATHS = Array.from({ length: 6 }, (_, index) => index);
const FLOATING_NODES = Array.from({ length: 4 }, (_, index) => index);

export const FabricationViewer3D = () => {
  return (
    <div className="fab-stage-3d" aria-label="Stylized fabrication stack preview">
      <div className="fab-stage-grid" />
      <div className="fab-stage-orbit fab-stage-orbit--left" />
      <div className="fab-stage-orbit fab-stage-orbit--right" />

      <div className="fab-stage-chip">
        <div className="fab-stage-layer fab-stage-layer--substrate" />
        <div className="fab-stage-layer fab-stage-layer--logic" />
        <div className="fab-stage-layer fab-stage-layer--routing">
          <div className="fab-stage-vias">
            {SIGNAL_PATHS.map((path) => (
              <span key={path} className="fab-stage-via" />
            ))}
          </div>
        </div>
        <div className="fab-stage-layer fab-stage-layer--active">
          <div className="fab-stage-active-grid">
            {Array.from({ length: 16 }, (_, index) => (
              <span key={index} className="fab-stage-cell" />
            ))}
          </div>
        </div>
      </div>

      <div className="fab-stage-nodes">
        {FLOATING_NODES.map((node) => (
          <span key={node} className={`fab-stage-node fab-stage-node--${node + 1}`} />
        ))}
      </div>

      <div className="fab-stage-caption">
        <span className="fab-stage-caption-label">Preview Stack</span>
        <strong className="fab-stage-caption-title">Stylized package depth view</strong>
        <p className="fab-stage-caption-copy">
          Lightweight silicon preview for routing, layer separation, and handoff readiness.
        </p>
      </div>
    </div>
  );
};
