import { getLogger } from '../utils/logger';
import axios from 'axios';
import type {
  MarketOverview,
  MoversResponse,
  ProjectionsSummary,
  OpportunitiesResponse,
  StockDetail,
  HistoricalData,
  HistoricalSummaryResponse,
  RunProjectionsResponse,
  MarketSummaryResponse,
  ProjectionAccuracyResponse,
  AlertsConfigResponse,
  AlertsConfig,
  AlertTestResponse,
  AlertsStatus,
  AlertsRunResponse,
  AuthResponse,
  User,
  QuoteMeta,
} from '../types';

const logger = getLogger('services/api');

const API_BASE_URL = import.meta.env.VITE_API_URL ?? '';

export const AUTH_TOKEN_KEY = 'markethelm-token';

export function getAuthToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return localStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    // Quota / private-mode / blocked storage must not crash auth init.
    return null;
  }
}

export function setAuthToken(token: string): void {
  try {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } catch {
    // Persist best-effort; login can still proceed with in-memory interceptor reads.
  }
}

export function clearAuthToken(): void {
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch {
    // Ignore storage failures on logout/clear.
  }
}

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 10000,
});

api.interceptors.request.use((config) => {
  const token = getAuthToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Log API failures in development only
if (import.meta.env.DEV) {
  api.interceptors.response.use(
    (res) => res,
    (err) => {
      logger.error('Request failed:', err.config?.url, err.response?.status, err.message);
      return Promise.reject(err);
    },
  );
}

// Market endpoints
export const marketApi = {
  getOverview: () => api.get<MarketOverview>('/api/market/overview'),
  getMovers: (type: 'gainers' | 'losers', limit: number = 10) =>
    api.get<MoversResponse>('/api/market/movers', { params: { type, limit } }),
};

// Projections endpoints
export const projectionsApi = {
  getSummary: () => api.get<ProjectionsSummary>('/api/projections/summary'),
  getOpportunities: (type: string, limit: number = 10) =>
    api.get<OpportunitiesResponse>('/api/projections/opportunities', {
      params: { type, limit },
    }),
};

// Summary endpoint (market AI/demo summary)
export const summaryApi = {
  getSummary: () => api.get<MarketSummaryResponse>('/api/summary'),
};

// History endpoints
export const historyApi = {
  getDates: () => api.get<{ dates: string[] }>('/api/history/dates'),
  getSummary: (days: number = 30) =>
    api.get<HistoricalSummaryResponse>('/api/history/summary', { params: { days } }),
  getRunProjections: (date: string) =>
    api.get<RunProjectionsResponse>(`/api/history/runs/${encodeURIComponent(date)}/projections`),
  getAccuracy: (days: number = 90) =>
    api.get<ProjectionAccuracyResponse>('/api/history/accuracy', { params: { days } }),
  getSymbols: () =>
    api.get<{ symbols: string[]; names: Record<string, string>; date: string }>(
      '/api/history/symbols',
    ),
};

// Stocks endpoints — encode path segments so class-share tickers like BRK/B
// cannot split the route or inject query/fragment characters.
export const stocksApi = {
  getDetail: (symbol: string) => api.get<StockDetail>(`/api/stocks/${encodeURIComponent(symbol)}`),
  getHistorical: (symbol: string, days: number = 30) =>
    api.get<HistoricalData>(`/api/stocks/${encodeURIComponent(symbol)}/historical`, {
      params: { days },
    }),
};

export const alertsApi = {
  getConfig: () => api.get<AlertsConfigResponse>('/api/alerts/config'),
  saveConfig: (config: AlertsConfig) => api.put<AlertsConfigResponse>('/api/alerts/config', config),
  initConfig: (force = false) =>
    api.post<{ message: string }>('/api/alerts/init', null, {
      params: force ? { force: true } : undefined,
    }),
  testAlert: (id: string, dryRun = false) =>
    api.post<AlertTestResponse>('/api/alerts/test', { id, dry_run: dryRun }, { timeout: 30000 }),
  getSymbols: () =>
    api.get<{
      symbols: string[];
      names: Record<string, string>;
      count: number;
      tracked_symbols?: string[];
      prices?: Record<string, number>;
      quote_meta?: Record<string, QuoteMeta>;
    }>('/api/alerts/symbols'),
  getQuotes: (symbols: string[]) =>
    api.get<{ prices: Record<string, number>; quote_meta?: Record<string, QuoteMeta> }>(
      '/api/alerts/quotes',
      {
        params: { symbols: symbols.join(',') },
        timeout: 45000,
      },
    ),
  getStatus: () => api.get<AlertsStatus>('/api/alerts/status'),
  runCheck: () => api.post<AlertsRunResponse>('/api/alerts/run'),
};

export const authApi = {
  register: (body: { email: string; password: string }) =>
    api.post<AuthResponse>('/api/auth/register', body),
  login: (body: { email: string; password: string }) =>
    api.post<AuthResponse>('/api/auth/login', body),
  me: () => api.get<User>('/api/auth/me'),
  logout: () => api.post<{ message: string }>('/api/auth/logout'),
  changePassword: (currentPassword: string, newPassword: string) =>
    api.post<{ message: string }>('/api/auth/password/change', {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  deleteAccount: (currentPassword: string, confirmation: string) =>
    api.delete<{ message: string }>('/api/auth/account', {
      data: { current_password: currentPassword, confirmation },
    }),
  requestPasswordReset: (email: string) =>
    api.post<{ message: string }>('/api/auth/password-reset/request', { email }),
  confirmPasswordReset: (token: string, password: string) =>
    api.post<{ message: string }>('/api/auth/password-reset/confirm', { token, password }),
  requestEmailVerification: (email: string) =>
    api.post<{ message: string }>('/api/auth/verify-email/request', { email }),
  confirmEmailVerification: (token: string) =>
    api.post<{ message: string }>('/api/auth/verify-email/confirm', { token }),
};

export default api;
