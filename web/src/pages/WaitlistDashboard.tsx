import { supabase } from '../supabaseClient';
import { motion } from 'framer-motion';

export const WaitlistDashboard = ({ email }: { email: string }) => {
  const handleLogout = async () => {
    await supabase.auth.signOut();
    window.location.reload();
  };

  return (
    <div className="waitlist-root">
      <div className="landing-grid-bg" />

      <motion.div
        className="waitlist-card"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: [0.25, 0.46, 0.45, 0.94] }}
      >
        <div className="waitlist-card-inner">
          {/* Status */}
          <div className="waitlist-status">
            <span className="waitlist-dot" />
            <span className="waitlist-status-text">Waitlist confirmed</span>
          </div>

          {/* Heading */}
          <h1 className="waitlist-heading">You&rsquo;re on the&nbsp;list.</h1>

          {/* Email */}
          <p className="waitlist-body">
            We&rsquo;ve registered{' '}
            <span className="waitlist-email">{email}</span>.
            Access is rolling out in cohorts. We&rsquo;ll notify you when your workspace is&nbsp;ready.
          </p>

          {/* Actions */}
          <div className="waitlist-actions">
            <a
              href="https://www.buildstack.live"
              target="_blank"
              rel="noreferrer"
              className="waitlist-btn waitlist-btn--ghost"
            >
              Documentation
            </a>
            <button
              onClick={handleLogout}
              className="waitlist-btn waitlist-btn--primary"
            >
              Sign out
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
};
