import React from 'react';

const STAGES = [
    { key: 'INIT', label: 'Initialization' },
    { key: 'SPEC', label: 'Specification' },
    { key: 'SPEC_VALIDATE', label: 'Spec Validation' },
    { key: 'HIERARCHY_EXPAND', label: 'Hierarchy Expansion' },
    { key: 'FEASIBILITY_CHECK', label: 'Feasibility Check' },
    { key: 'VERIFICATION_PLAN', label: 'Verification Plan' },
    { key: 'RTL_GEN', label: 'RTL Generation' },
    { key: 'RTL_FIX', label: 'RTL Fix' },
    { key: 'CDC_ANALYZE', label: 'CDC Analysis' },
    { key: 'VERIFICATION', label: 'Verification' },
    { key: 'FORMAL_VERIFY', label: 'Formal Verify' },
    { key: 'COVERAGE_CHECK', label: 'Coverage Check' },
    { key: 'REGRESSION', label: 'Regression' },
    { key: 'SDC_GEN', label: 'SDC Generation' },
    { key: 'SYNTHESIS', label: 'Synthesis' },
    { key: 'FLOORPLAN', label: 'Floorplan' },
    { key: 'HARDENING', label: 'Hardening' },
    { key: 'CONVERGENCE_REVIEW', label: 'Convergence' },
    { key: 'ECO_PATCH', label: 'ECO Patch' },
    { key: 'POWER_ANALYSIS', label: 'Power Analysis' },
    { key: 'TIMING_ANALYSIS', label: 'Timing Analysis' },
    { key: 'PHYSICAL_VERIFY', label: 'Physical Verify' },
    { key: 'SIGNOFF', label: 'Signoff' },
    { key: 'IP_PACKAGE', label: 'IP Package' },
];

// Brief, encouraging descriptions shown when a stage is active or just completed
const STAGE_DESCRIPTIONS: Record<string, string> = {
    INIT:              'Setting up build context',
    SPEC:              'Translating your idea into chip spec',
    SPEC_VALIDATE:     'Validating spec completeness & generating assertions',
    HIERARCHY_EXPAND:   'Expanding complex submodules into nested specs',
    FEASIBILITY_CHECK:  'Checking node contract, memory macros & physical feasibility',
    VERIFICATION_PLAN:  'Generating verification plan & SVA properties',
    RTL_GEN:           'Writing synthesizable Verilog',
    RTL_FIX:           'Resolving any RTL issues',
    CDC_ANALYZE:      'Analyzing clock domain crossings after RTL exists',
    VERIFICATION:      'Running simulation testbench',
    FORMAL_VERIFY:     'Proving correctness mathematically',
    COVERAGE_CHECK:    'Checking test coverage',
    REGRESSION:        'Running regression suite',
    SDC_GEN:           'Generating timing constraints for synthesis',
    SYNTHESIS:        'Converting RTL to gate-level netlist',
    FLOORPLAN:         'Laying out chip floorplan',
    HARDENING:         'Physical design & routing',
    CONVERGENCE_REVIEW:'Checking timing convergence',
    ECO_PATCH:         'Patching for final sign-off',
    POWER_ANALYSIS:    'Analyzing power consumption',
    TIMING_ANALYSIS:    'Running static timing analysis',
    PHYSICAL_VERIFY:   'Running physical verification checks',
    SIGNOFF:           'Final OSS evidence review',
    IP_PACKAGE:       'Packaging IP for reuse',
};

// Key milestones displayed with a special accent in the sidebar
const MILESTONES = new Set(['RTL_GEN', 'VERIFICATION', 'SYNTHESIS', 'HARDENING', 'SIGNOFF']);

interface Props {
    currentStage: string;
    completedStages: Set<string>;
    failedStage?: string;
    waitingForApproval: boolean;
    skippedStages?: Set<string>;
}

export const StageProgressBar: React.FC<Props> = ({
    currentStage,
    completedStages,
    failedStage,
    waitingForApproval,
    skippedStages,
}) => {
    return (
        <aside className="hitl-sidebar">
            <div className="hitl-sidebar-label">Build Pipeline</div>
            <nav className="hitl-sidebar-stages">
                {STAGES.map((stage) => {
                    const isCompleted = completedStages.has(stage.key);
                    const isCurrent = stage.key === currentStage;
                    const isFailed = stage.key === failedStage;
                    const isWaiting = isCurrent && waitingForApproval;
                    const isSkipped = skippedStages?.has(stage.key) && !isCompleted && !isCurrent && !isFailed;
                    const isMilestone = MILESTONES.has(stage.key);

                    let status = 'pending';
                    if (isSkipped) status = 'skipped';
                    if (isCompleted) status = 'completed';
                    if (isCurrent && !isWaiting) status = 'active';
                    if (isWaiting) status = 'waiting';
                    if (isFailed) status = 'failed';

                    const showDesc = (isCurrent || isCompleted) && !isSkipped && !isFailed;

                    return (
                        <div
                            key={stage.key}
                            className={`hitl-sidebar-stage hitl-stage--${status}${isMilestone ? ' hitl-stage--milestone' : ''}`}
                        >
                            <div className="hitl-sidebar-indicator">
                                {isCompleted && (
                                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                                        <circle cx="7" cy="7" r="6" fill="#4a7c59" />
                                        <path d="M4 7l2 2 4-4" stroke="#fff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                                    </svg>
                                )}
                                {isFailed && (
                                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                                        <circle cx="7" cy="7" r="6" fill="#c0392b" />
                                        <path d="M5 5l4 4m0-4l-4 4" stroke="#fff" strokeWidth="1.5" strokeLinecap="round" />
                                    </svg>
                                )}
                                {isSkipped && !isCompleted && !isFailed && (
                                    <span className="hitl-sidebar-dot-skipped">—</span>
                                )}
                                {(isCurrent || isWaiting) && !isCompleted && !isFailed && !isSkipped && (
                                    <span className="hitl-sidebar-dot-active" />
                                )}
                                {!isCompleted && !isCurrent && !isWaiting && !isFailed && !isSkipped && (
                                    <span className="hitl-sidebar-dot-empty" />
                                )}
                            </div>
                            <div className="hitl-sidebar-stage-info">
                                <span className="hitl-sidebar-stage-name">{stage.label}</span>
                                {showDesc && (
                                    <span className="hitl-sidebar-stage-desc">
                                        {STAGE_DESCRIPTIONS[stage.key]}
                                    </span>
                                )}
                            </div>
                            {isMilestone && isCompleted && (
                                <span className="hitl-sidebar-milestone-dot" title="Milestone reached" />
                            )}
                        </div>
                    );
                })}
            </nav>
        </aside>
    );
};
