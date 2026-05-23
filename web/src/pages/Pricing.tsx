import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Check, Zap, Infinity as InfinityIcon, ArrowLeft, Cpu, KeyRound, AlertCircle } from 'lucide-react';
import type { Session } from '@supabase/supabase-js';
import { supabase } from '../supabaseClient';
import { api } from '../api';

type Plan = {
  id: string;
  name: string;
  description: string;
  build_limit: number | null;
  price_display: string;
  features: string[];
  popular?: boolean;
};

type RazorpayPaymentResponse = {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
};

type RazorpayCheckoutOptions = {
  key: string;
  order_id: string;
  name: string;
  description: string;
  handler: (response: RazorpayPaymentResponse) => Promise<void>;
  theme?: { color?: string; overlay_close?: boolean };
  modal?: { ondismiss?: () => void };
};

type RazorpayConstructor = new (options: RazorpayCheckoutOptions) => { open: () => void };

declare global {
  interface Window {
    Razorpay?: RazorpayConstructor;
  }
}

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

export function Pricing() {
  const [loading, setLoading] = useState<string | null>(null);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [testMode, setTestMode] = useState(false);
  const [currentPlan, setCurrentPlan] = useState<string | null>(null);
  const [currentPlanType, setCurrentPlanType] = useState<string | null>(null);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }: { data: { session: Session | null } }) => setSession(data.session));
    loadBillingStatus();
  }, []);

  const loadBillingStatus = async () => {
    try {
      const { data } = await api.get('/billing/status', { validateStatus: () => true });
      if (data) {
        setCurrentPlan(data.plan);
        setCurrentPlanType(data.plan_type);
        setTestMode(data.test_mode || false);
      }
    } catch {
      // ignore
    }
  };

  const handlePurchase = async (planId: string) => {
    if (!session?.user) {
      setMessage({ type: 'error', text: 'Please sign in first to purchase a plan.' });
      return;
    }

    setLoading(planId);
    setMessage(null);

    try {
      // Step 1: Create order
      const { data: order, status: orderStatus } = await api.post('/billing/create-order', {
        plan: planId,
        user_id: session.user.id,
      }, { validateStatus: () => true });

      if (orderStatus < 200 || orderStatus >= 300) {
        throw new Error(order?.detail || 'Failed to create order');
      }

      if (testMode || order.test_mode) {
        // Test mode: activate immediately without real payment
        const { data: result, status: activateStatus } = await api.post('/billing/verify-payment', {
          razorpay_order_id: order.order_id,
          razorpay_payment_id: `test_payment_${Date.now()}`,
          razorpay_signature: '0'.repeat(64),
          user_id: session.user.id,
          plan: planId,
        }, { validateStatus: () => true });

        if (activateStatus < 200 || activateStatus >= 300) {
          throw new Error(result?.detail || 'Failed to activate plan');
        }
        setMessage({
          type: 'success',
          text: `✅ ${result.plan_name || PLANS.find(p => p.id === planId)?.name} plan activated! (Test mode — no payment taken)`,
        });
        setCurrentPlan(planId);
        setCurrentPlanType('agentic_paid');
      } else {
        // Production mode: open Razorpay checkout
        const Razorpay = window.Razorpay;
        if (!Razorpay) {
          setMessage({ type: 'error', text: 'Payment SDK not loaded. Please refresh the page or use test mode.' });
          setLoading(null);
          return;
        }
        const rzp = new Razorpay({
          key: order.key_id,
          order_id: order.order_id,
          name: 'AgentIC',
          description: `${order.plan_name} — ${order.amount_display}`,
          handler: async (response: RazorpayPaymentResponse) => {
            const { status: verifyStatus } = await api.post('/billing/verify-payment', {
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
              user_id: session.user.id,
              plan: planId,
            }, { validateStatus: () => true });
            if (verifyStatus >= 200 && verifyStatus < 300) {
              setMessage({ type: 'success', text: '✅ Plan activated! Start building your chips.' });
              setCurrentPlan(planId);
              setCurrentPlanType('agentic_paid');
            }
          },
          theme: { color: '#C9643E', overlay_close: true },
          modal: {
            ondismiss: () => setLoading(null),
          },
        });
        rzp.open();
      }
    } catch (err: unknown) {
      setMessage({ type: 'error', text: err instanceof Error ? err.message : 'Something went wrong. Please try again.' });
    } finally {
      setLoading(null);
    }
  };

  const handleTestActivate = async (planId: string) => {
    if (!session?.user) {
      setMessage({ type: 'error', text: 'Please sign in first.' });
      return;
    }
    setLoading(planId);
    setMessage(null);
    try {
      const { data, status } = await api.post('/billing/test-activate', {
        plan: planId,
        user_id: session.user.id,
      }, { validateStatus: () => true });
      if (status < 200 || status >= 300) {
        throw new Error(data?.detail || 'Failed to activate');
      }
      setMessage({ type: 'success', text: data.message });
      setCurrentPlan(planId);
      setCurrentPlanType('agentic_paid');
    } catch (err: unknown) {
      setMessage({ type: 'error', text: err instanceof Error ? err.message : 'Failed to activate plan.' });
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="pricing-page">
      {/* Test Mode Banner */}
      {testMode && (
        <div className="pricing-test-banner">
          <AlertCircle size={15} />
          <span>
            <strong>Test Mode</strong> — No real payments are processed. Use the "Test Activate" buttons to simulate purchases.
            Configure <code>RAZORPAY_KEY_ID</code> in your environment to enable real billing.
          </span>
        </div>
      )}

      {/* Header */}
      <div className="pricing-header">
        <button className="pricing-back" onClick={() => window.history.back()}>
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
      {currentPlanType === 'agentic_paid' && currentPlan && (
        <div className="pricing-current">
          <div className="pricing-current-badge">
            <Check size={15} />
            <span>
              You have an active <strong>{PLANS.find(p => p.id === currentPlan)?.name}</strong> plan.
            </span>
          </div>
        </div>
      )}

      {currentPlanType === 'byok' && (
        <div className="pricing-current">
          <div className="pricing-current-badge pricing-current-badge--byok">
            <KeyRound size={15} />
            <span>
              You're on <strong>BYOK mode</strong>. Subscribe to use Infinite without managing model keys.
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
              ) : currentPlan === plan.id && currentPlanType === 'agentic_paid' ? (
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
                  {testMode && (
                    <button
                      className="pricing-btn pricing-btn--test"
                      disabled={loading !== null}
                      onClick={() => handleTestActivate(plan.id)}
                    >
                      {loading === plan.id ? 'Activating…' : `Test Activate (Free)`}
                    </button>
                  )}
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
