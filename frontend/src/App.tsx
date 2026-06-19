import { useCallback, useEffect, useState } from "react";
import {
  Control,
  Position,
  StrategyInfo,
  getControl,
  getPositions,
  getStrategies,
  login,
  setControl,
} from "./api";

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
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [c, s, p] = await Promise.all([
        getControl(token),
        getStrategies(token),
        getPositions(token),
      ]);
      setControlState(c);
      setStrategies(s);
      setPositions(p);
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

  return (
    <div>
      <div style={{ ...card, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <strong>엔진 상태:</strong>
        <span style={{ color: control?.state === "RUNNING" ? "#36d399" : "#fbbd23" }}>
          {control?.state ?? "..."}
        </span>
        <span style={{ flex: 1 }} />
        <button onClick={() => void act("pause")}>일시중지</button>
        <button onClick={() => void act("resume")}>재개</button>
        <button onClick={() => void act("stop")} style={{ background: "#f87272", color: "#fff" }}>
          긴급중지 (전량청산)
        </button>
        <button onClick={onLogout}>로그아웃</button>
      </div>
      {err && <div style={{ color: "#f87272" }}>{err}</div>}

      <div style={card}>
        <h2 style={{ fontSize: 16 }}>보유 포지션 ({positions.length})</h2>
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
