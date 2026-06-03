import axios, { AxiosError } from 'axios';
import type { ApiError } from './lib/types';
import { toUserError } from './utils/errorFormatter';
import { getAuthHeader } from './authSession';

const isDesktopApp = typeof window !== 'undefined' && (
  'electronAPI' in window || 
  window.location.protocol === 'file:' || 
  window.location.protocol.startsWith('agentic') ||
  (typeof navigator !== 'undefined' && navigator.userAgent.includes('Electron'))
);
const desktopApiOverride = typeof window !== 'undefined'
  ? localStorage.getItem('agentic_api_base_url') || ''
  : '';
const base = import.meta.env.VITE_API_BASE_URL
  || desktopApiOverride
  || (isDesktopApp
    ? (import.meta.env.VITE_DESKTOP_API_BASE_URL || 'http://localhost:7860')
    : (import.meta.env.DEV ? '/api' : 'http://localhost:7860'));

const cleanBase = base.replace(/\/$/, '');
export const API_BASE = cleanBase || '';

export const api = axios.create({
  baseURL: API_BASE,
  headers: { 
    'ngrok-skip-browser-warning': 'true',
    'Content-Type': 'application/json',
  },
});

export const AUTH_ENABLED = Boolean(import.meta.env.VITE_SUPABASE_URL) || isDesktopApp;

export const getAuthHeaders = async (
  extra: Record<string, string> = {}
): Promise<Record<string, string>> => {
  const headers: Record<string, string> = { ...extra };
  return { ...headers, ...(await getAuthHeader()) };
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
  const authHeader = await getAuthHeader();
  if (authHeader.Authorization) {
    config.headers.Authorization = authHeader.Authorization;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiError>) => {
    if (error.response) {
      const data = error.response.data;
      if (data?.detail) {
        return Promise.reject(Object.assign(new Error(toUserError(data)), error));
      }
      if (data?.message) {
        return Promise.reject(Object.assign(new Error(toUserError(data)), error));
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
