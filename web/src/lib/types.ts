import type { AxiosResponse } from 'axios';

export interface ApiResponse<T> {
  data: T | null;
  error: string | null;
}

export interface BuildJob {
  job_id: string;
  design_name: string;
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'cancelling';
  current_state: string;
  created_at: number;
  event_count: number;
  human_in_loop: boolean;
}

export interface BuildEvent {
  type: string;
  state: string;
  message: string;
  step: number;
  total_steps: number;
  timestamp: number;
  agent_name?: string;
  thought_type?: string;
  content?: string;
  
  // Enriched fields for HUMAN-IN-LOOP stages
  is_live_waiting?: boolean;
  stage_name?: string;
  summary?: string;
  artifacts?: Array<{ name: string; path: string; description: string; type?: string }>;
  decisions?: string[];
  warnings?: string[];
  next_stage_name?: string;
  next_stage_preview?: string;
  options?: Array<{ key: string; label: string; description?: string }>;
  changes?: unknown[];
}

export interface BuildResult {
  success: boolean;
  design_name: string;
  state: string;
  artifacts?: Record<string, unknown>;
  failure_explanation?: string;
  failure_suggestion?: string;
  failed_stage?: string;
}

export interface Design {
  name: string;
  description?: string;
  has_gds: boolean;
}

export interface Profile {
  id: string;
  email: string;
  plan?: string;
  plan_type?: string;
  build_limit?: number | null;
  successful_builds?: number;
  auth_enabled?: boolean;
  has_byok_key?: boolean;
}

export interface BillingStatus {
  has_subscription: boolean;
  subscription_id?: string;
  plan?: string;
  build_limit?: number;
  used_builds?: number;
}

export interface PipelineSchema {
  stages: Array<{
    name: string;
    label: string;
    icon: string;
  }>;
}

export interface ApiError {
  detail: string;
  error?: string;
  message?: string;
}

export type ApiResult<T> = Promise<[AxiosResponse<T> | null, ApiError | null]>;

export const isApiError = (err: unknown): err is ApiError => {
  return typeof err === 'object' && err !== null && 'detail' in err;
};

export const getErrorMessage = (err: unknown): string => {
  if (isApiError(err)) {
    return err.detail || err.message || 'Unknown error';
  }
  if (err instanceof Error) {
    return err.message;
  }
  return 'Unknown error';
};