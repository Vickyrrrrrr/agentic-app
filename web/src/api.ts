import axios, { AxiosError } from 'axios';
import { supabase } from './supabaseClient';
import type { ApiError } from './lib/types';

const isDesktopApp = typeof window !== 'undefined' && ('electronAPI' in window || window.location.protocol === 'file:' || window.location.protocol.startsWith('agentic'));
const desktopApiOverride = typeof window !== 'undefined'
  ? localStorage.getItem('agentic_api_base_url') || ''
  : '';
const base = import.meta.env.VITE_API_BASE_URL
  || desktopApiOverride
  || (isDesktopApp
    ? (import.meta.env.VITE_DESKTOP_API_BASE_URL || 'https://api.buildstack.live')
    : (import.meta.env.DEV ? '/api' : ''));

const cleanBase = base.replace(/\/$/, '');
export const API_BASE = cleanBase || '';

export const api = axios.create({
  baseURL: API_BASE,
  headers: { 
    'ngrok-skip-browser-warning': 'true',
    'Content-Type': 'application/json',
  },
});

export const AUTH_ENABLED = Boolean(import.meta.env.VITE_SUPABASE_URL);

export const getAuthHeaders = async (
  extra: Record<string, string> = {}
): Promise<Record<string, string>> => {
  const headers: Record<string, string> = { ...extra };
  if (!AUTH_ENABLED) return headers;

  try {
    const { data: { session } } = await supabase.auth.getSession();
    if (session?.access_token) {
      headers.Authorization = `Bearer ${session.access_token}`;
    }
  } catch {
    // Keep unauthenticated requests possible in local/dev mode.
  }

  return headers;
};

export const getSseHeaders = async (
  extra: Record<string, string> = {}
): Promise<Record<string, string>> =>
  getAuthHeaders({
    'ngrok-skip-browser-warning': 'true',
    Accept: 'text/event-stream',
    ...extra,
  });

api.interceptors.request.use(async (config) => {
  if (!AUTH_ENABLED) return config;
  
  try {
    const { data: { session } } = await supabase.auth.getSession();
    if (session?.access_token) {
      config.headers.Authorization = `Bearer ${session.access_token}`;
    }
  } catch {
    // No session — request goes without auth
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiError>) => {
    if (error.response) {
      const data = error.response.data;
      if (data?.detail) {
        return Promise.reject(Object.assign(new Error(String(data.detail)), error));
      }
      if (data?.message) {
        return Promise.reject(Object.assign(new Error(String(data.message)), error));
      }
    }
    return Promise.reject(error);
  }
);

export const unwrap = async <T,>(
  promise: Promise<{ data: T | null; status: number }>
): Promise<[T | null, Error | null]> => {
  try {
    const response = await promise;
    return [response.data, null];
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Unknown error';
    return [null, new Error(message)];
  }
};
