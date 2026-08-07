import { useCallback, useEffect, useState } from "react";
import {
  Control,
  Position,
  Status,
  StrategyInfo,
  getControl,
  getPositions,
  getStatus,
  getStrategies,
  login,
  setControl,
} from "./api";

const won = (v: string | null | undefined): string => {
  if (v == null) return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return v;
  return `${n >= 0 ? "" : "-"}₩${Math.abs(Math.round(n)).toLocaleString()}`;
};

const pnlColor = (v: string | null | undefined): string => {
  const n = Number(v ?? 0);
  return n > 0 ? "#f87272" : n < 0 ? "#5ba9f7" : "#e6eef7"; // 한국식: 빨강=수익, 파랑=손실
};

const card: React.CSSProperties = {
  background: "#13283f",
  borderRadius: 12,
  padding: 16,
  marginBottom: 16,
};

export function App() {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem("token"));

  const onToken = (t: string) => {
    localStorage.setItem("token", t);
    setToken(t);
  };
  const onLogout = () => {
    localStorage.removeItem("token");
    setToken(null);
  };

  return (
    <div
      style={{
        maxWidth: 880,
        margin: "0 auto",
        padding: 16,
        color: "#e6eef7",
        fontFamily: "system-ui, -apple-system, sans-serif",
      }}
    >
      <h1 style={{ fontSize: 20 }}>KIS 자동매매 대시보드</h1>
      {token ? <Dashboard token={token} onLogout={onLogout} /> : <Login onToken={onToken} />}
    </div>
  );
}

function Login({ onToken }: { onToken: (t: string) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin");
  const [err, setErr] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      onToken(await login(username, password));
    } catch {
      setErr("로그인 실패");
    }
  };

  return (
    <form onSubmit={submit} style={card}>
      <h2 style={{ fontSize: 16 }}>로그인</h2>
      <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="아이디" />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="비밀번호"
        style={{ marginLeft: 8 }}
      />
      <button type="submit" style={{ marginLeft: 8 }}>
        접속
      </button>
      {err && <span style={{ color: "#f87272", marginLeft: 8 }}>{err}</span>}
    </form>
  );
}

function Dashboard({ token, onLogout }: { token: string; onLogout: () => void }) {
  const [control, setControlState] = useState<Control | null>(null);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [c, s, p, st] = await Promise.all([
        getControl(token),
        getStrategies(token),
        getPositions(token),
        getStatus(token),
      ]);
      setControlState(c);
      setStrategies(s);
      setPositions(p);
      setStatus(st);
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  }, [token]);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 3000);
    return () => clearInterval(id);
  }, [refresh]);

  const act = async (a: "pause" | "resume" | "stop") => {
    setControlState(await setControl(token, a));
  };

  const engine = status?.engine ?? null;
  const alive = status?.engine_alive ?? false;
  const engineState = engine?.control ?? control?.state ?? "...";

  return (
    <div>
      <div style={{ ...card, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <strong>엔진:</strong>
        <span style={{ color: alive ? "#36d399" : "#f87272" }}>
          {alive ? "● 가동" : "● 끊김"}
        </span>
        <span style={{ color: engineState === "RUNNING" ? "#36d399" : "#fbbd23" }}>
          {engineState}
        </span>
        {engine && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            {new Date(engine.ts).toLocaleTimeString("ko-KR")} 갱신
          </span>
        )}
        <span style={{ flex: 1 }} />
        <button onClick={() => void act("pause")}>일시중지</button>
        <button onClick={() => void act("resume")}>재개</button>
        <button onClick={() => void act("stop")} style={{ background: "#f87272", color: "#fff" }}>
          긴급중지 (전량청산)
        </button>
        <button onClick={onLogout}>로그아웃</button>
      </div>
      {err && <div style={{ color: "#f87272" }}>{err}</div>}
      {!alive && (
        <div style={{ ...card, color: "#fbbd23" }}>
          ⚠️ 엔진 상태 파일이 15초 넘게 갱신되지 않았습니다 — 엔진이 꺼졌거나 시작 중입니다.
        </div>
      )}

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <div style={{ ...card, flex: 1, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>총자산 (모의계좌)</div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>{won(engine?.equity)}</div>
        </div>
        <div style={{ ...card, flex: 1, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>오늘 실현손익</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: pnlColor(engine?.daily_realized) }}>
            {won(engine?.daily_realized)}
          </div>
        </div>
        <div style={{ ...card, flex: 1, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>보유 / 관망</div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>
            {engine ? `${engine.open_lots.length} / ${engine.watching.length}` : "—"}
          </div>
        </div>
      </div>

      <div style={card}>
        <h2 style={{ fontSize: 16 }}>보유 중 ({engine?.open_lots.length ?? 0})</h2>
        {engine && engine.open_lots.length > 0 ? (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", opacity: 0.7 }}>
                <th>종목</th>
                <th>전략</th>
                <th>수량</th>
                <th>평단</th>
                <th>현재가</th>
                <th>평가손익</th>
                <th>손절가</th>
              </tr>
            </thead>
            <tbody>
              {engine.open_lots.map((l) => (
                <tr key={`${l.ticker}@${l.resolution}`}>
                  <td>
                    {l.ticker} <span style={{ opacity: 0.5 }}>@{l.resolution}</span>
                  </td>
                  <td style={{ opacity: 0.8 }}>{l.strategy.replace("_v1", "")}</td>
                  <td>{l.qty}</td>
                  <td>{won(l.avg_entry)}</td>
                  <td>{won(l.last_price)}</td>
                  <td style={{ color: pnlColor(l.unrealized) }}>{won(l.unrealized)}</td>
                  <td>{won(l.initial_stop)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div style={{ fontSize: 13, opacity: 0.6 }}>
            보유 없음 — 관망 중:{" "}
            {engine?.watching.map((w) => `${w.ticker}@${w.resolution}`).join(", ") || "—"}
          </div>
        )}
      </div>

      <div style={card}>
        <h2 style={{ fontSize: 16 }}>오늘 체결 ({status?.today_fills.length ?? 0})</h2>
        {status && status.today_fills.length > 0 ? (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", opacity: 0.7 }}>
                <th>시각(UTC)</th>
                <th>종목</th>
                <th>방향</th>
                <th>수량</th>
                <th>가격</th>
                <th>수수료+세</th>
              </tr>
            </thead>
            <tbody>
              {status.today_fills.map((f, i) => (
                <tr key={i}>
                  <td>{f.time}</td>
                  <td>{f.ticker}</td>
                  <td style={{ color: f.side === "BUY" ? "#f87272" : "#5ba9f7" }}>{f.side}</td>
                  <td>{f.qty}</td>
                  <td>{won(f.price)}</td>
                  <td>{won(String(Number(f.fee) + Number(f.tax)))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div style={{ fontSize: 13, opacity: 0.6 }}>오늘 체결 없음</div>
        )}
      </div>

      <div style={card}>
        <h2 style={{ fontSize: 16 }}>랏 기록 — DB ({positions.length})</h2>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", opacity: 0.7 }}>
              <th>종목</th>
              <th>시장</th>
              <th>상태</th>
              <th>수량</th>
              <th>평단</th>
              <th>실현손익</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.lot_id}>
                <td>{p.ticker}</td>
                <td>{p.market}</td>
                <td>{p.state}</td>
                <td>{p.qty_filled}</td>
                <td>{p.avg_entry_price}</td>
                <td>{p.realized_pnl}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={card}>
        <h2 style={{ fontSize: 16 }}>알고리즘 ({strategies.length})</h2>
        {strategies.map((s) => (
          <div key={s.id} style={{ borderTop: "1px solid #234", padding: "8px 0" }}>
            <strong>{s.name}</strong> <code style={{ opacity: 0.7 }}>{s.id}</code>
            <div style={{ fontSize: 13, opacity: 0.8 }}>{s.description}</div>
            <div style={{ fontSize: 12, opacity: 0.7 }}>
              파라미터: {Object.keys(s.params_schema.properties ?? {}).join(", ") || "—"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
