const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000";

export interface Control {
  state: string;
  flat_all_requested: boolean;
  scope: string | null;
}

export interface StrategyInfo {
  id: string;
  name: string;
  description: string;
  version: string;
  params_schema: { properties?: Record<string, { type?: string; default?: unknown }> };
}

export interface Position {
  lot_id: string;
  ticker: string;
  market: string;
  state: string;
  strategy_id: string;
  qty_filled: string;
  avg_entry_price: string;
  realized_pnl: string;
}

function headers(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export async function login(username: string, password: string): Promise<string> {
  const res = await fetch(`${BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await json<{ access_token: string }>(res);
  return data.access_token;
}

export const getControl = (t: string) =>
  fetch(`${BASE}/api/control`, { headers: headers(t) }).then(json<Control>);

export const setControl = (t: string, action: "pause" | "resume" | "stop") =>
  fetch(`${BASE}/api/control`, {
    method: "POST",
    headers: headers(t),
    body: JSON.stringify({ action }),
  }).then(json<Control>);

export const getStrategies = (t: string) =>
  fetch(`${BASE}/api/strategies`, { headers: headers(t) }).then(json<StrategyInfo[]>);

export const getPositions = (t: string) =>
  fetch(`${BASE}/api/positions`, { headers: headers(t) }).then(json<Position[]>);
