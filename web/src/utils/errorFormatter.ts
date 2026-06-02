/**
 * User-friendly error formatting utilities.
 * Never expose stack traces, server commands, or raw API details to end users.
 */

export const USER_FRIENDLY_ERRORS: Record<string, string> = {
  ERR_NETWORK: 'Unable to connect to AgentIC. Please check your internet connection and try again.',
  ECONNREFUSED: 'The service is currently unavailable. Please try again in a moment.',
  ECONNABORTED: 'The request took too long. Please try again.',
  default: 'Something went wrong. Please try again or contact support if the issue persists.',
};

export function toUserError(error: unknown, fallback = USER_FRIENDLY_ERRORS.default): string {
  if (!error) return fallback;

  if (typeof error === 'string') {
    const trimmed = error.trim();
    if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        return toUserError(parsed, fallback);
      } catch {
        return fallback;
      }
    }
    const lower = error.toLowerCase();
    if (
      lower.includes('stdout') ||
      lower.includes('stderr') ||
      lower.includes('tool-call') ||
      lower.includes('tool-result') ||
      lower.includes('bash(') ||
      lower.includes('"command"') ||
      lower.includes("'command'")
    ) {
      return 'The local tool action could not be completed. Please try again.';
    }
    if (lower.includes('signed entitlement') || lower.includes('entitlement')) {
      return 'We could not verify your license securely. Please try again in a moment.';
    }
    if (lower.includes('supabase') || lower.includes('database') || lower.includes('read failed') || lower.includes('write failed')) {
      return 'We could not verify your account right now. Please try again in a moment.';
    }
    if (lower.includes('traceback') || lower.includes('internal server error') || lower.includes('exception')) {
      return 'The service is temporarily unavailable. Please try again in a moment.';
    }
    if (lower.includes('uvicorn') || lower.includes('server with:') || lower.includes('backend logs')) {
      return 'The build service is currently unavailable. Please try again later.';
    }
    if (lower.includes('docker') || lower.includes('tool not found')) {
      return 'The build environment is not fully configured. Please contact support.';
    }
    if (lower.includes('api key') || lower.includes('authentication') || lower.includes('unauthorized')) {
      return 'Authentication failed. Please check your API key configuration.';
    }
    if (lower.includes('pdk') || lower.includes('install')) {
      return 'The required chip manufacturing library is not available for this workspace.';
    }
    if (lower.includes('timeout') || lower.includes('timed out')) {
      return 'The operation took too long. Please try again with a simpler design.';
    }
    return error;
  }

  if (error instanceof Error) {
    return toUserError(error.message, fallback);
  }

  if (typeof error === 'object') {
    const record = error as Record<string, unknown>;
    const detail = record.detail ?? record.message ?? record.error;
    if (typeof detail === 'string') {
      return toUserError(detail, fallback);
    }
    if (typeof detail === 'object' && detail !== null) {
      const subDetail = (detail as Record<string, unknown>).message ?? (detail as Record<string, unknown>).error;
      if (typeof subDetail === 'string') {
        return toUserError(subDetail, fallback);
      }
    }
  }

  return fallback;
}

export function isNetworkError(error: unknown): boolean {
  if (!error) return false;
  if (typeof error === 'object' && error !== null) {
    const record = error as Record<string, unknown>;
    const code = record.code as string;
    if (code === 'ERR_NETWORK' || code === 'ECONNREFUSED' || code === 'ECONNABORTED') return true;
  }
  if (error instanceof Error) {
    return error.message.includes('Network Error') || error.message.includes('ERR_NETWORK');
  }
  return false;
}
