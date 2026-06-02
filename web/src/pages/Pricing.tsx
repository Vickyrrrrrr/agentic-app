import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Check, Zap, Infinity as InfinityIcon, ArrowLeft, Cpu, KeyRound, AlertCircle } from 'lucide-react';
import type { Session } from '@supabase/supabase-js';
import { supabase } from '../supabaseClient';
import { api } from '../api';
import { toUserError } from '../utils/errorFormatter';

type AgenticElectronWindow = Window & {
  electronAPI?: {
    openExternal?: (url: string) => Promise<{ success: boolean }>;
  };
};

type Plan = {
  id: string;
  name: string;
  description: string;
  build_limit: number | null;
  price_display: string;
  features: string[];
  popular?: boolean;
};

const PLANS: Plan[] = [
  {
    id: 'starter',
    name: 'Starter',
    description: '10 successful chip builds',
    build_limit: 10,
    price_display: '$20',
    features: [
      '10 successful chip builds',
      'RTL generation & verification',
      'Yosys synthesis',
      'OpenLane hardening',
      'DRC & LVS',
      'Email support',
    ],
  },
  {
    id: 'pro',
    name: 'Pro (Unlimited)',
    description: 'Unlimited successful chip builds',
    build_limit: null,
    price_display: '$200',
    popular: true,
    features: [
      'Unlimited successful chip builds',
      'RTL generation & verification',
      'Yosys synthesis',
      'OpenLane hardening',
      'DRC & LVS',
      'Priority support',
      'Advanced hardening options',
    ],
  },
];

export function Pricing({ onBack }: { onBack?: () => void }) {
  const [loading, setLoading] = useState<string | null>(null);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [currentPlan, setCurrentPlan] = useState<string | null>(null);
  const [checkoutUrl, setCheckoutUrl] = useState<string | null>(null);

  async function loadBillingStatus() {
    try {
      const { data } = await api.get('/license/status', { validateStatus: () => true });
      if (data?.active) {
        const normalizedPlan = String(data.plan || 'pro').toLowerCase().includes('starter') ? 'starter' : 'pro';
        setCurrentPlan(normalizedPlan);
      } else {
        setCurrentPlan(null);
      }
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    supabase.auth.getSession().then(({ data }: { data: { session: Session | null } }) => setSession(data.session));
    loadBillingStatus();
  }, []);

  const openCheckoutUrl = async (url: string) => {
    const desktopOpen = (window as AgenticElectronWindow).electronAPI?.openExternal;
    const result = desktopOpen ? await desktopOpen(url) : null;
    if (!result?.success) {
      const opened = window.open(url, '_blank', 'noopener,noreferrer');
      if (!opened) {
        window.location.href = url;
      }
    }
  };

  const handlePurchase = async (planId: string) => {
    if (!session?.user) {
      setMessage({ type: 'error', text: 'Please sign in first to purchase a plan.' });
      return;
    }

    setLoading(planId);
    setMessage(null);
    setCheckoutUrl(null);

    try {
      const { data, status } = await api.post('/checkout/create', {
        plan: planId,
      }, { validateStatus: () => true });

      if (status < 200 || status >= 300 || !data?.checkout_url) {
        throw new Error(toUserError(data, 'Unable to start checkout. Please try again in a moment.'));
      }
      setCheckoutUrl(data.checkout_url);
      await openCheckoutUrl(data.checkout_url);
      setMessage({ type: 'success', text: 'Checkout is ready. If your browser did not open, use the checkout button below.' });
    } catch (err: unknown) {
      setMessage({ type: 'error', text: toUserError(err, 'Unable to start checkout. Please try again in a moment.') });
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="pricing-page">
      {/* Header */}
      <div className="pricing-header">
        <button className="pricing-back" onClick={() => {
          if (onBack) {
            onBack();
          } else {
            window.history.back();
          }
        }}>
          <ArrowLeft size={16} />
          Back
        </button>
        <div className="pricing-title-wrap">
          <h1 className="pricing-title">Use Infinite for chip builds</h1>
          <p className="pricing-subtitle">
            Minimal pricing for the hosted AgentIC model. BYOK stays available when you prefer your own model.
          </p>
        </div>
      </div>

      {/* Current Plan Badge */}
      {currentPlan && (
        <div className="pricing-current">
          <div className="pricing-current-badge">
            <Check size={15} />
            <span>
              You have an active <strong>{PLANS.find(p => p.id === currentPlan)?.name}</strong> plan.
            </span>
          </div>
        </div>
      )}

      {/* Message */}
      {message && (
        <div className={`pricing-message pricing-message--${message.type}`}>
          {message.type === 'success' ? <Check size={15} /> : <AlertCircle size={15} />}
          {message.text}
        </div>
      )}

      {checkoutUrl && (
        <div className="pricing-checkout-fallback">
          <div>
            <strong>Checkout link ready</strong>
            <p>Open the secure Lemon Squeezy checkout in your browser, then return here and recheck your license.</p>
          </div>
          <div className="pricing-checkout-actions">
            <button className="pricing-btn pricing-checkout-btn" onClick={() => openCheckoutUrl(checkoutUrl)}>
              Open checkout
            </button>
            <button
              className="pricing-btn pricing-checkout-btn pricing-checkout-btn--ghost"
              onClick={() => {
                navigator.clipboard?.writeText(checkoutUrl);
                setMessage({ type: 'success', text: 'Checkout link copied.' });
              }}
            >
              Copy link
            </button>
          </div>
        </div>
      )}

      {/* Plan Cards */}
      <div className="pricing-cards">
        {PLANS.map((plan, i) => (
          <motion.div
            key={plan.id}
            className={`pricing-card${plan.popular ? ' pricing-card--popular' : ''}`}
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.1 }}
          >
            {plan.popular && (
              <div className="pricing-popular-badge">Most Popular</div>
            )}

            <div className="pricing-card-header">
              <div className="pricing-plan-icon">
                {plan.id === 'pro' ? <InfinityIcon size={22} /> : <Zap size={22} />}
              </div>
              <h2 className="pricing-plan-name">{plan.name}</h2>
              <p className="pricing-plan-desc">{plan.description}</p>
            </div>

            <div className="pricing-price-wrap">
              <span className="pricing-price">{plan.price_display}</span>
              {plan.id === 'pro' && (
                <span className="pricing-price-note">one-time</span>
              )}
              {plan.id === 'starter' && (
                <span className="pricing-price-note">one-time</span>
              )}
            </div>

            <ul className="pricing-features">
              {plan.features.map((f) => (
                <li key={f} className="pricing-feature">
                  <Check size={14} className="pricing-check" />
                  {f}
                </li>
              ))}
            </ul>

            <div className="pricing-card-actions">
              {!session?.user ? (
                <button className="pricing-btn" onClick={() => setMessage({ type: 'error', text: 'Please sign in first.' })}>
                  Sign in to Purchase
                </button>
              ) : currentPlan === plan.id ? (
                <button className="pricing-btn pricing-btn--active" disabled>
                  <Check size={15} />
                  Current Plan
                </button>
              ) : (
                <>
                  <button
                    className="pricing-btn"
                    disabled={loading !== null}
                    onClick={() => handlePurchase(plan.id)}
                  >
                    {loading === plan.id ? 'Processing…' : `Get ${plan.name}`}
                  </button>
                </>
              )}
            </div>
          </motion.div>
        ))}
      </div>

      {/* Compare with BYOK */}
      <div className="pricing-compare">
        <div className="pricing-compare-card">
          <div className="pricing-compare-icon">
            <Cpu size={20} />
          </div>
          <div>
            <strong>Infinite</strong>
            <p>AgentIC hosted model for autonomous chip generation. No model key setup required.</p>
          </div>
        </div>
        <div className="pricing-compare-vs">vs</div>
        <div className="pricing-compare-card">
          <div className="pricing-compare-icon">
            <KeyRound size={20} />
          </div>
          <div>
            <strong>Bring Your Own Key</strong>
            <p>Use your own model and API keys. You manage provider billing directly.</p>
          </div>
        </div>
      </div>

      {/* FAQ */}
      <div className="pricing-faq">
        <h2 className="pricing-faq-title">Frequently Asked Questions</h2>
        <div className="pricing-faq-list">
          <details className="pricing-faq-item">
            <summary>What counts as a "successful build"?</summary>
            <p>A successful build is one that reaches the final state without errors — from RTL generation through DRC/LVS signoff. Failed builds due to verification or DRC errors do not count against your limit.</p>
          </details>
          <details className="pricing-faq-item">
            <summary>Can I switch between AgentIC Model and BYOK?</summary>
            <p>Yes. You can switch at any time from the workspace settings. BYOK builds use your own API keys and are unlimited.</p>
          </details>
          <details className="pricing-faq-item">
            <summary>Is there a refund policy?</summary>
            <p>Since builds are consumed upon successful completion, we don't offer refunds for completed builds. Contact support for special cases.</p>
          </details>
          <details className="pricing-faq-item">
            <summary>What happens if I run out of builds?</summary>
            <p>You can purchase another plan or switch to BYOK mode to continue building chips with your own API keys.</p>
          </details>
        </div>
      </div>
    </div>
  );
}
