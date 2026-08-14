export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';
export const WS_URL = import.meta.env.VITE_WS_URL ?? API_URL.replace(/^http/, 'ws') + '/ws';

const STORAGE_KEY = 'ivc.token';

export function getToken(): string | null {
  return localStorage.getItem(STORAGE_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(STORAGE_KEY, token);
  else localStorage.removeItem(STORAGE_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers ?? {});
  headers.set('Accept', 'application/json');
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (res.status === 204) return undefined as unknown as T;
  if (!res.ok) {
    let code = 'ERROR';
    let message = res.statusText;
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? message;
    } catch {
      /* ignore */
    }
    if (res.status === 401) setToken(null);
    throw new ApiError(res.status, code, message);
  }
  const ct = res.headers.get('Content-Type') ?? '';
  if (ct.includes('application/json')) return res.json() as Promise<T>;
  return (await res.text()) as unknown as T;
}

export type Cage = {
  id: string;
  name: string;
  location: string;
  animal_strain: string;
  animal_age_days: number;
  last_seen: string | null;
  created_at: string;
};

export type Reading = {
  ts: string;
  sensor: 'feeding' | 'water' | 'metabolic' | 'behaviour' | 'weighing' | 'environment';
  payload: Record<string, any>;
};

export type EnvironmentPayload = {
  temperature_c: number;
  humidity_pct: number;
};

export type Alert = {
  id: number;
  cage_id: string;
  ts: string;
  severity: 'info' | 'warning' | 'critical';
  rule: string;
  message: string;
  acknowledged_at: string | null;
};

export type SummaryDay = {
  date: string;
  feeding: { intake_g: number; delivered_g: number; wasted_g: number };
  water_ml: number;
  metabolic: { avg_rer: number; ee_kcal: number };
  behaviour: { sleeping_minutes: number; active_minutes: number };
};

export type Summary = { cage_id: string; days: SummaryDay[] };

export type ScheduleWindow = {
  start_local: string;
  end_local: string;
  days: number[];
  active: boolean;
  /** IANA timezone (e.g. "Europe/Brussels"). Defaults to Belgium server-side. */
  timezone: string;
};

export type Schedule = { cage_id: string; windows: ScheduleWindow[] };
