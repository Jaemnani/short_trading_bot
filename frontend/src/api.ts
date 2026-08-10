// 기본 = 같은 오리진 (API 서버 :8000이 빌드된 이 페이지를 직접 서빙).
// vite dev(:5173)에서만 VITE_API_BASE=http://localhost:8000 지정.
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

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

export interface OpenLot {
  ticker: string;
  resolution: string;
  strategy: string;
  state: string;
  qty: string;
  avg_entry: string;
  last_price: string | null;
  unrealized: string | null;
  initial_stop: string | null;
}

export interface Health {
  in_session: boolean;
  feed_ok: boolean;
  last_bar_at: string | null;
  last_bar_ticker: string | null;
  feed_stale_seconds: number | null;
  bars_received: number;
  feed_connects: number;
  process_errors: number;
  last_poll_ok_at: string | null;
  poll_failures: number;
}

export interface EngineSnapshot {
  ts: string;
  control: string;
  equity: string | null;
  peak_equity: string;
  daily_realized: string;
  daily_date: string | null;
  open_lots: OpenLot[];
  watching: { ticker: string; resolution: string; strategy: string }[];
  health?: Health; // 구버전 엔진 스냅샷 호환 (없을 수 있음)
}

export interface FillRow {
  time: string;
  ticker: string;
  side: string;
  qty: string;
  price: string;
  fee: string;
  tax: string;
}

export interface Status {
  engine_alive: boolean;
  engine: EngineSnapshot | null;
  today_fills: FillRow[];
}

export const getStatus = (t: string) =>
  fetch(`${BASE}/api/status`, { headers: headers(t) }).then(json<Status>);
