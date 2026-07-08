import { useState, useEffect, useCallback, useRef } from "react";
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend
} from "recharts";
import {
  Activity, TrendingUp, TrendingDown, Shield, Zap, Settings,
  Power, PauseCircle, AlertTriangle, CheckCircle, XCircle,
  RefreshCw, BarChart2, Target, Brain, ChevronRight,
  ArrowUpRight, ArrowDownRight, DollarSign, Clock, Cpu,
  Eye, PlayCircle, StopCircle, Edit3, Save, X, Bell
} from "lucide-react";
import { api } from "./services/api";
// ── Mock API (replace with real fetch in production) ───────────────────────--------
// ── Color tokens ──────────────────────────────────────────────────────────────
const C = {
  bg: "#0a0e1a",
  surface: "#111827",
  card: "#161d2e",
  border: "#1e2a3a",
  borderHigh: "#2a3a50",
  lime: "#a3e635",
  limeD: "#84cc16",
  limeGlow: "rgba(163,230,53,0.15)",
  cyan: "#22d3ee",
  red: "#f87171",
  orange: "#fb923c",
  yellow: "#fbbf24",
  purple: "#a78bfa",
  text: "#e2e8f0",
  muted: "#64748b",
  dim: "#334155",
};

// ── Reusable components ───────────────────────────────────────────────────────
const Badge = ({ children, color = C.lime }) => (
  <span style={{ background: `${color}22`, color, border: `1px solid ${color}44`, borderRadius: 4, padding: "2px 8px", fontSize: 11, fontWeight: 600, letterSpacing: 0.5 }}>
    {children}
  </span>
);

const Card = ({ children, style = {}, glow = false }) => (
  <div style={{
    background: C.card, border: `1px solid ${glow ? C.limeD : C.border}`,
    borderRadius: 12, padding: 20,
    boxShadow: glow ? `0 0 20px ${C.limeGlow}` : "0 2px 8px rgba(0,0,0,0.3)",
    ...style
  }}>{children}</div>
);

const StatCard = ({ icon: Icon, label, value, sub, color = C.lime, delta }) => (
  <Card>
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
      <div>
        <p style={{ color: C.muted, fontSize: 12, margin: 0, letterSpacing: 0.5 }}>{label}</p>
        <p style={{ color, fontSize: 26, fontWeight: 700, margin: "6px 0 4px", fontFamily: "monospace" }}>{value}</p>
        {sub && <p style={{ color: C.muted, fontSize: 12, margin: 0 }}>{sub}</p>}
        {delta !== undefined && (
          <p style={{ color: delta >= 0 ? C.lime : C.red, fontSize: 12, margin: "4px 0 0", display: "flex", alignItems: "center", gap: 3 }}>
            {delta >= 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
            {Math.abs(delta)}%
          </p>
        )}
      </div>
      <div style={{ background: `${color}18`, borderRadius: 10, padding: 10 }}>
        <Icon size={20} color={color} />
      </div>
    </div>
  </Card>
);

const Toggle = ({ value, onChange, label }) => (
  <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
    <div
      onClick={() => onChange(!value)}
      style={{
        width: 44, height: 24, borderRadius: 12, position: "relative", cursor: "pointer",
        background: value ? C.lime : C.dim, transition: "background 0.2s"
      }}
    >
      <div style={{
        position: "absolute", top: 3, left: value ? 23 : 3,
        width: 18, height: 18, borderRadius: "50%", background: "#fff",
        transition: "left 0.2s"
      }} />
    </div>
    <span style={{ color: C.text, fontSize: 13 }}>{label}</span>
  </label>
);

const RegimePill = ({ regime }) => {
  const map = {
    TRENDING_BULL: { color: C.lime, label: "Trending Bull", icon: TrendingUp },
    TRENDING_BEAR: { color: C.red, label: "Trending Bear", icon: TrendingDown },
    RANGE_BOUND: { color: C.yellow, label: "Range Bound", icon: Activity },
    HIGH_VOLATILITY: { color: C.orange, label: "High Volatility", icon: Zap },
    PRE_EVENT: { color: C.purple, label: "Pre Event", icon: AlertTriangle },
  };
  const r = map[regime] || map.RANGE_BOUND;
  const Icon = r.icon;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, background: `${r.color}18`, border: `1px solid ${r.color}44`, borderRadius: 20, padding: "4px 12px", color: r.color, fontSize: 12, fontWeight: 600 }}>
      <Icon size={12} /> {r.label}
    </span>
  );
};

const StatusDot = ({ active }) => (
  <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: active ? C.lime : C.red, boxShadow: active ? `0 0 6px ${C.lime}` : "none" }} />
);

// ── Tab Definitions ───────────────────────────────────────────────────────────
const TABS = ["Dashboard", "Trades", "Signals", "Strategies", "Settings", "Backtest"];

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [tab, setTab] = useState("Dashboard");
  
  const [status, setStatus] = useState(null);
  const [trades, setTrades] = useState([]);
  const [signals, setSignals] = useState([]);
  const [strategies, setStrategies] = useState([]);
  const [pnlData, setPnlData] = useState([]);
  const [backtests, setBacktests] = useState([]);

  const [toast, setToast] = useState(null);
  const [weights, setWeights] = useState({});
  const [editingWeights, setEditingWeights] = useState(false);
  const [settings, setSettings] = useState({
    total_capital: 10000, max_risk_per_trade_pct: 0.15,
    max_daily_loss_pct: 0.25, max_open_trades: 2,
    paper_trading: true, auto_trade: false,
    min_confidence: 0.38, min_strategies_agree: 3
  });

  useEffect(() => {
    loadDashboard();
  }, []);

  useEffect(() => {
    const ws = new WebSocket(`${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`);

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload?.type === "signal") {
          setSignals((prev) => [
            { ...payload.data, id: payload.data.timestamp || Date.now() },
            ...prev,
          ].slice(0, 50));
        }
      } catch (err) {
        console.error("WebSocket parse error", err);
      }
    };

    return () => ws.close();
  }, []);

  const loadDashboard = async () => {
    try {
      const [
        status,
        trades,
        signals,
        dailyPnl,
        strategyPerf,
        settings,
        backtests,
      ] = await Promise.all([
        api.getStatus(),
        api.getTrades(),
        api.getSignals(),
        api.getDailyPnL(),
        api.getStrategyPerformance(),
        api.getSettings(),
        api.getBacktests(),
      ]);

      setStatus(status);
      setTrades(trades.trades || []);
      setSignals(signals.signals || []);
      setStrategies(strategyPerf.strategies || []);
      setPnlData(dailyPnl.data || []);
      setSettings(settings);
      setBacktests(backtests.results || []);
    } catch (err) {
      console.error(err);
    }
  };

  const showToast = (msg, type = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  const handleBotToggle = async () => {
  try {
    let result;

    if (status.active) {
      result = await api.pauseBot();
    } else {
      result = await api.activateBot();
    }

    setStatus((s) => ({
      ...s,
      active: result.active,
    }));

    showToast(
      result.active
        ? "Bot activated"
        : "Bot paused"
    );
  } catch (err) {
    console.error(err);
    showToast("Operation failed", "error");
  }
};

  const handleSaveWeights = async () => {
    try {
      const res = await api.updateWeights(weights);

      setWeights(res.weights);

      showToast("Weights updated");

      setEditingWeights(false);
    } catch {
      showToast("Failed updating weights", "error");
    }
  };
  const handleForceExit = async () => {
    await api.forceExitAll();

    loadDashboard();

    showToast("All positions exited");
  };

  const handleExitTrade = async (tradeId) => {
    await api.exitTrade(tradeId);

    loadDashboard();

    showToast(`Trade ${tradeId} exited`);
  };

  const handleSaveSettings = async () => {
    try {
      await api.updateSettings(settings);

      showToast("Settings saved");
    } catch {
      showToast("Failed saving settings", "error");
    }
  };

  const totalPnL = trades.filter(t => t.pnl != null).reduce((a, t) => a + t.pnl, 0);
  const winTrades = trades.filter(t => t.pnl > 0).length;
  const closedTrades = trades.filter(t => t.status !== "OPEN").length;
  const winRate = closedTrades > 0 ? ((winTrades / closedTrades) * 100).toFixed(1) : 0;
  
  if (!status) {
  return (
    <div
      style={{
        background: C.bg,
        color: C.text,
        minHeight: "100vh",
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      Loading dashboard...
    </div>
  );
}


  return (
    <div style={{ background: C.bg, minHeight: "100vh", color: C.text, fontFamily: "'Inter', sans-serif" }}>
      {/* Toast */}
      {toast && (
        <div style={{
          position: "fixed", top: 20, right: 20, zIndex: 999,
          background: toast.type === "success" ? "#1a2e1a" : toast.type === "warning" ? "#2e2a1a" : "#2e1a1a",
          border: `1px solid ${toast.type === "success" ? C.lime : toast.type === "warning" ? C.yellow : C.red}`,
          borderRadius: 8, padding: "12px 20px", color: C.text, fontSize: 14,
          boxShadow: "0 4px 20px rgba(0,0,0,0.5)"
        }}>
          {toast.type === "success" ? "✅" : "⚠️"} {toast.msg}
        </div>
      )}

      {/* Header */}
      <div style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, padding: "0 24px", display: "flex", alignItems: "center", justifyContent: "space-between", height: 60 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ background: C.limeGlow, border: `1px solid ${C.limeD}`, borderRadius: 8, padding: "6px 10px" }}>
            <Brain size={18} color={C.lime} />
          </div>
          <div>
            <span style={{ color: C.lime, fontWeight: 700, fontSize: 16, letterSpacing: 1 }}>APEX BOT</span>
            <span style={{ color: C.muted, fontSize: 11, display: "block", marginTop: -2 }}>AI Options Buyer</span>
          </div>
          <div style={{ width: 1, background: C.border, height: 32, margin: "0 8px" }} />
          <StatusDot active={status?.active} />
          <span style={{ color: status.active ? C.lime : C.muted, fontSize: 12 }}>
            {status.active ? "LIVE" : "PAUSED"} {status.paper_trading && <span style={{ color: C.yellow }}>• PAPER</span>}
          </span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <RegimePill regime={status.regime} />
          <span style={{ color: C.muted, fontSize: 12 }}>
            {(status.regime_confidence * 100).toFixed(0)}% conf
          </span>
          <div style={{ width: 1, background: C.border, height: 32 }} />
          <button
            onClick={handleBotToggle}
            style={{
              display: "flex", alignItems: "center", gap: 6, padding: "6px 14px",
              background: status.active ? `${C.lime}18` : `${C.red}18`,
              border: `1px solid ${status.active ? C.limeD : C.red}`,
              borderRadius: 6, color: status.active ? C.lime : C.red, cursor: "pointer", fontSize: 13
            }}
          >
            {status.active ? <PauseCircle size={14} /> : <PlayCircle size={14} />}
            {status.active ? "Pause" : "Activate"}
          </button>
          <button
            onClick={handleForceExit}
            style={{
              display: "flex", alignItems: "center", gap: 6, padding: "6px 14px",
              background: `${C.red}18`, border: `1px solid ${C.red}44`,
              borderRadius: 6, color: C.red, cursor: "pointer", fontSize: 13
            }}
          >
            <StopCircle size={14} /> Force Exit All
          </button>
        </div>
      </div>

      {/* Nav */}
      <div style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, padding: "0 24px", display: "flex", gap: 4 }}>
        {TABS.map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              padding: "12px 16px", background: "none", border: "none",
              borderBottom: tab === t ? `2px solid ${C.lime}` : "2px solid transparent",
              color: tab === t ? C.lime : C.muted, cursor: "pointer", fontSize: 13,
              fontWeight: tab === t ? 600 : 400, transition: "all 0.2s"
            }}
          >{t}</button>
        ))}
      </div>

      {/* Content */}
      <div style={{ padding: 24, maxWidth: 1400, margin: "0 auto" }}>

        {/* ── DASHBOARD ────────────────────────────────────────────── */}
        {tab === "Dashboard" && (
          <div>
            {/* KPI Row */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 16, marginBottom: 24 }}>
              <StatCard icon={DollarSign} label="PORTFOLIO VALUE" value={`₹${status.capital.toLocaleString()}`} sub="Starting: ₹10,000" color={C.lime} delta={((status.capital - 10000) / 10000 * 100).toFixed(1)} />
              <StatCard icon={TrendingUp} label="TOTAL P&L" value={`₹${totalPnL.toLocaleString()}`} sub="All closed trades" color={totalPnL >= 0 ? C.lime : C.red} />
              <StatCard icon={Target} label="WIN RATE" value={`${winRate}%`} sub={`${winTrades}/${closedTrades} trades`} color={C.cyan} />
              <StatCard icon={Shield} label="DAILY LOSS USED" value={`${status.risk_status.daily_loss_pct}%`} sub={`Limit: ${(status.config?.max_daily_loss_pct || 0.25) * 100}%`} color={status.risk_status.daily_loss_pct > 20 ? C.red : C.yellow} />
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16, marginBottom: 16 }}>
              {/* Equity Curve */}
              <Card>
                <h3 style={{ margin: "0 0 16px", color: C.text, fontSize: 14, fontWeight: 600 }}>Equity Curve</h3>
                <ResponsiveContainer width="100%" height={220}>
                  <AreaChart data={pnlData}>
                    <defs>
                      <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={C.lime} stopOpacity={0.3} />
                        <stop offset="95%" stopColor={C.lime} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                    <XAxis dataKey="date" stroke={C.muted} tick={{ fontSize: 11 }} />
                    <YAxis stroke={C.muted} tick={{ fontSize: 11 }} tickFormatter={v => `₹${v}`} />
                    <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8 }} formatter={v => [`₹${v}`, "Capital"]} />
                    <Area type="monotone" dataKey="capital" stroke={C.lime} strokeWidth={2} fill="url(#equityGrad)" />
                  </AreaChart>
                </ResponsiveContainer>
              </Card>

              {/* Risk Dashboard */}
              <Card>
                <h3 style={{ margin: "0 0 16px", color: C.text, fontSize: 14, fontWeight: 600 }}>Risk Dashboard</h3>
                <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                  {[
                    { label: "Daily Loss", val: status.risk_status.daily_loss_pct, max: 25, color: C.red },
                    { label: "Positions Used", val: (status.risk_status.open_trades / 2) * 100, max: 100, color: C.cyan },
                    { label: "Trades Today", val: (status.risk_status.daily_trades / 5) * 100, max: 100, color: C.yellow },
                  ].map(({ label, val, max, color }) => (
                    <div key={label}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                        <span style={{ color: C.muted, fontSize: 12 }}>{label}</span>
                        <span style={{ color, fontSize: 12, fontWeight: 600 }}>{val.toFixed(1)}%</span>
                      </div>
                      <div style={{ background: C.dim, borderRadius: 4, height: 6 }}>
                        <div style={{ width: `${Math.min(val, 100)}%`, height: "100%", background: color, borderRadius: 4, transition: "width 0.5s" }} />
                      </div>
                    </div>
                  ))}
                  <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 12, marginTop: 4 }}>
                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                      <span style={{ color: C.muted, fontSize: 12 }}>Can Trade?</span>
                      <span style={{ color: status.risk_status.can_trade ? C.lime : C.red, fontSize: 12, fontWeight: 600 }}>
                        {status.risk_status.can_trade ? "✅ YES" : "❌ NO"}
                      </span>
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8 }}>
                      <span style={{ color: C.muted, fontSize: 12 }}>Open Positions</span>
                      <span style={{ color: C.text, fontSize: 12, fontWeight: 600 }}>{status.risk_status.open_trades} / 2</span>
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8 }}>
                      <span style={{ color: C.muted, fontSize: 12 }}>Daily Trades</span>
                      <span style={{ color: C.text, fontSize: 12, fontWeight: 600 }}>{status.risk_status.daily_trades} / 5</span>
                    </div>
                  </div>
                </div>
              </Card>
            </div>

            {/* Daily P&L Bar */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
              <Card>
                <h3 style={{ margin: "0 0 16px", color: C.text, fontSize: 14, fontWeight: 600 }}>Daily P&L</h3>
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={pnlData}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                    <XAxis dataKey="date" stroke={C.muted} tick={{ fontSize: 11 }} />
                    <YAxis stroke={C.muted} tick={{ fontSize: 11 }} tickFormatter={v => `₹${v}`} />
                    <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8 }} formatter={v => [`₹${v}`, "P&L"]} />
                    <Bar dataKey="pnl" fill={C.lime} radius={[4, 4, 0, 0]}
                      cell={pnlData.map((d, i) => <cell key={i} fill={d.pnl >= 0 ? C.lime : C.red} />)} />
                  </BarChart>
                </ResponsiveContainer>
              </Card>

              {/* Live Signals */}
              <Card>
                <h3 style={{ margin: "0 0 12px", color: C.text, fontSize: 14, fontWeight: 600 }}>Latest Signals</h3>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {signals.slice(0, 4).map(s => (
                    <div key={s.id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 12px", background: C.surface, borderRadius: 8, border: `1px solid ${C.border}` }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                        <div style={{ width: 6, height: 6, borderRadius: "50%", background: s.signal_type === "BUY_CE" ? C.lime : s.signal_type === "BUY_PE" ? C.red : C.muted }} />
                        <span style={{ color: C.text, fontSize: 13 }}>{s.instrument}</span>
                        <Badge color={s.signal_type === "BUY_CE" ? C.lime : s.signal_type === "BUY_PE" ? C.red : C.muted}>
                          {s.signal_type}
                        </Badge>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <span style={{ color: C.lime, fontSize: 12, fontFamily: "monospace" }}>{(s.score * 100).toFixed(0)}%</span>
                        <p style={{ color: C.muted, fontSize: 10, margin: 0 }}>{s.acted_on ? "✅ Acted" : "⏭️ Skipped"}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </Card>
            </div>
          </div>
        )}

        {/* ── TRADES ────────────────────────────────────────────────── */}
        {tab === "Trades" && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>Trade History</h2>
              <div style={{ display: "flex", gap: 8 }}>
                {["ALL", "OPEN", "TARGET_HIT", "SL_HIT"].map(f => (
                  <button key={f} style={{ padding: "5px 12px", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, color: C.muted, cursor: "pointer", fontSize: 12 }}>{f}</button>
                ))}
              </div>
            </div>

            <Card style={{ padding: 0, overflow: "hidden" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ background: C.surface }}>
                    {["ID", "Instrument", "Strike", "Entry", "Exit", "P&L", "Status", "Confidence", "Strategies", "Action"].map(h => (
                      <th key={h} style={{ padding: "12px 16px", textAlign: "left", color: C.muted, fontSize: 11, fontWeight: 600, letterSpacing: 0.5, borderBottom: `1px solid ${C.border}` }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => {
                    const voted = JSON.parse(t.strategies_voted || "[]");
                    return (
                      <tr key={t.trade_id} style={{ borderBottom: `1px solid ${C.border}`, background: i % 2 === 0 ? C.card : `${C.surface}80` }}>
                        <td style={{ padding: "12px 16px", fontSize: 12, fontFamily: "monospace", color: C.lime }}>{t.trade_id}</td>
                        <td style={{ padding: "12px 16px", fontSize: 12, color: C.text }}>{t.instrument.includes("Nifty 50") ? "Nifty 50" : "BankNifty"}</td>
                        <td style={{ padding: "12px 16px" }}>
                          <span style={{ fontFamily: "monospace", fontSize: 13, color: C.text }}>{t.strike}</span>
                          <Badge color={t.option_type === "CE" ? C.lime : C.red}>{t.option_type}</Badge>
                        </td>
                        <td style={{ padding: "12px 16px", fontSize: 13, fontFamily: "monospace", color: C.text }}>₹{t.entry_price}</td>
                        <td style={{ padding: "12px 16px", fontSize: 13, fontFamily: "monospace", color: C.text }}>{t.exit_price ? `₹${t.exit_price}` : "—"}</td>
                        <td style={{ padding: "12px 16px" }}>
                          {t.pnl != null ? (
                            <span style={{ color: t.pnl >= 0 ? C.lime : C.red, fontWeight: 700, fontFamily: "monospace", fontSize: 13 }}>
                              {t.pnl >= 0 ? "+" : ""}₹{t.pnl} <span style={{ fontSize: 11 }}>({t.pnl_pct > 0 ? "+" : ""}{t.pnl_pct}%)</span>
                            </span>
                          ) : <span style={{ color: C.muted }}>Open</span>}
                        </td>
                        <td style={{ padding: "12px 16px" }}>
                          <Badge color={t.status === "OPEN" ? C.cyan : t.status === "TARGET_HIT" ? C.lime : t.status === "SL_HIT" ? C.red : C.yellow}>
                            {t.status}
                          </Badge>
                          {t.paper_trade ? <Badge color={C.muted}>PAPER</Badge> : null}
                        </td>
                        <td style={{ padding: "12px 16px" }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <div style={{ width: 40, height: 6, background: C.dim, borderRadius: 3 }}>
                              <div style={{ width: `${t.confidence_score * 100}%`, height: "100%", background: t.confidence_score > 0.7 ? C.lime : C.yellow, borderRadius: 3 }} />
                            </div>
                            <span style={{ fontSize: 11, color: C.muted }}>{(t.confidence_score * 100).toFixed(0)}%</span>
                          </div>
                        </td>
                        <td style={{ padding: "12px 16px" }}>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
                            {voted.slice(0, 3).map(s => <Badge key={s} color={C.purple}>{s}</Badge>)}
                          </div>
                        </td>
                        <td style={{ padding: "12px 16px" }}>
                          {t.status === "OPEN" && (
                            <button
                              onClick={() => handleExitTrade(t.trade_id)}
                              style={{ padding: "4px 10px", background: `${C.red}18`, border: `1px solid ${C.red}44`, borderRadius: 5, color: C.red, cursor: "pointer", fontSize: 11 }}
                            >Exit</button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Card>
          </div>
        )}

        {/* ── SIGNALS ───────────────────────────────────────────────── */}
        {tab === "Signals" && (
          <div>
            <h2 style={{ margin: "0 0 16px", fontSize: 18, fontWeight: 700 }}>Signal Log</h2>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
              <div>
                {signals.map(s => (
                  <Card key={s.id} style={{ marginBottom: 12 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                      <div>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                          <Badge color={s.signal_type === "BUY_CE" ? C.lime : s.signal_type === "BUY_PE" ? C.red : C.muted}>{s.signal_type}</Badge>
                          <span style={{ color: C.text, fontSize: 14, fontWeight: 600 }}>{s.instrument}</span>
                        </div>
                        <div style={{ display: "flex", gap: 8 }}>
                          <RegimePill regime={s.regime} />
                          <Badge color={s.acted_on ? C.lime : C.muted}>{s.acted_on ? "Executed" : "Skipped"}</Badge>
                        </div>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <p style={{ color: C.lime, fontFamily: "monospace", fontSize: 20, fontWeight: 700, margin: 0 }}>{(s.score * 100).toFixed(0)}%</p>
                        <p style={{ color: C.muted, fontSize: 11, margin: "2px 0 0" }}>Confidence</p>
                      </div>
                    </div>
                    <p style={{ color: C.muted, fontSize: 11, margin: "8px 0 0" }}>{s.timestamp}</p>
                  </Card>
                ))}
              </div>
              <Card>
                <h3 style={{ margin: "0 0 16px", color: C.text, fontSize: 14, fontWeight: 600 }}>Signal Distribution</h3>
                <ResponsiveContainer width="100%" height={250}>
                  <BarChart data={[
                    { name: "BUY CE", count: signals.filter(s => s.signal_type === "BUY_CE").length, fill: C.lime },
                    { name: "BUY PE", count: signals.filter(s => s.signal_type === "BUY_PE").length, fill: C.red },
                    { name: "NO TRADE", count: signals.filter(s => s.signal_type === "NO_TRADE").length, fill: C.muted },
                  ]}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                    <XAxis dataKey="name" stroke={C.muted} tick={{ fontSize: 12 }} />
                    <YAxis stroke={C.muted} tick={{ fontSize: 12 }} />
                    <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8 }} />
                    <Bar dataKey="count" radius={[6, 6, 0, 0]}>
                      {[C.lime, C.red, C.muted].map((c, i) => <cell key={i} fill={c} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </Card>
            </div>
          </div>
        )}

        {/* ── STRATEGIES ────────────────────────────────────────────── */}
        {tab === "Strategies" && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: C.text }}>Strategy Engine</h2>
              <div style={{ display: "flex", gap: 8 }}>
                {editingWeights ? (
                  <>
                    <button onClick={handleSaveWeights} style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", background: `${C.lime}18`, border: `1px solid ${C.limeD}`, borderRadius: 6, color: C.lime, cursor: "pointer", fontSize: 13 }}>
                      <Save size={13} /> Save Weights
                    </button>
                    <button onClick={() => setEditingWeights(false)} style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, color: C.muted, cursor: "pointer", fontSize: 13 }}>
                      <X size={13} /> Cancel
                    </button>
                  </>
                ) : (
                  <button onClick={() => setEditingWeights(true)} style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", background: C.card, border: `1px solid ${C.border}`, borderRadius: 6, color: C.text, cursor: "pointer", fontSize: 13 }}>
                    <Edit3 size={13} /> Edit Weights
                  </button>
                )}
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {strategies.map(s => (
                  <Card key={s.strategy_name}>
                    <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                      <div style={{ width: 40, height: 40, borderRadius: 10, background: `${C.lime}18`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                        <Cpu size={18} color={C.lime} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                          <span style={{ color: C.text, fontWeight: 600, fontSize: 14, textTransform: "uppercase", letterSpacing: 0.5 }}>{s.strategy_name}</span>
                          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                            <span style={{ color: C.lime, fontSize: 13, fontFamily: "monospace" }}>Win: {s.avg_win_rate.toFixed(1)}%</span>
                            <span style={{ color: C.cyan, fontSize: 13, fontFamily: "monospace" }}>Avg: +{s.avg_return.toFixed(1)}%</span>
                            <span style={{ color: C.muted, fontSize: 12 }}>{s.total_signals} signals</span>
                          </div>
                        </div>
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <div style={{ flex: 1, height: 6, background: C.dim, borderRadius: 3 }}>
                            <div style={{ width: `${s.avg_win_rate}%`, height: "100%", background: s.avg_win_rate > 65 ? C.lime : C.yellow, borderRadius: 3 }} />
                          </div>
                          {editingWeights ? (
                            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                              <span style={{ color: C.muted, fontSize: 11 }}>Weight:</span>
                              <input
                                type="number" min="0" max="1" step="0.01"
                                value={weights[s.strategy_name] || 0}
                                onChange={e => setWeights(w => ({ ...w, [s.strategy_name]: parseFloat(e.target.value) }))}
                                style={{ width: 60, background: C.surface, border: `1px solid ${C.lime}`, borderRadius: 4, padding: "2px 6px", color: C.lime, fontSize: 12, fontFamily: "monospace" }}
                              />
                            </div>
                          ) : (
                            <Badge color={C.purple}>W: {(s.current_weight * 100).toFixed(0)}%</Badge>
                          )}
                        </div>
                      </div>
                    </div>
                  </Card>
                ))}
              </div>

              <div>
                <Card style={{ marginBottom: 16 }}>
                  <h3 style={{ margin: "0 0 14px", color: C.text, fontSize: 14, fontWeight: 600 }}>Win Rate Comparison</h3>
                  <ResponsiveContainer width="100%" height={280}>
                    <BarChart data={strategies} layout="vertical">
                      <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                      <XAxis type="number" domain={[0, 100]} stroke={C.muted} tick={{ fontSize: 10 }} tickFormatter={v => `${v}%`} />
                      <YAxis type="category" dataKey="strategy_name" stroke={C.muted} tick={{ fontSize: 10 }} width={60} />
                      <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8 }} formatter={v => [`${v}%`, "Win Rate"]} />
                      <Bar dataKey="avg_win_rate" fill={C.lime} radius={[0, 4, 4, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </Card>
                <Card>
                  <h3 style={{ margin: "0 0 10px", color: C.text, fontSize: 14, fontWeight: 600 }}>Bot Learning Status</h3>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {[
                      { label: "Last Weight Retrain", val: "2 days ago", color: C.lime },
                      { label: "Total Trades Learned", val: "87", color: C.cyan },
                      { label: "Model Accuracy", val: "63.2%", color: C.lime },
                      { label: "Next Retrain", val: "Sunday 9PM", color: C.yellow },
                    ].map(({ label, val, color }) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: `1px solid ${C.border}` }}>
                        <span style={{ color: C.muted, fontSize: 12 }}>{label}</span>
                        <span style={{ color, fontSize: 12, fontWeight: 600 }}>{val}</span>
                      </div>
                    ))}
                  </div>
                </Card>
              </div>
            </div>
          </div>
        )}

        {/* ── SETTINGS ──────────────────────────────────────────────── */}
        {tab === "Settings" && (
          <div style={{ maxWidth: 760 }}>
            <h2 style={{ margin: "0 0 20px", fontSize: 18, fontWeight: 700 }}>Bot Settings</h2>

            <Card style={{ marginBottom: 16 }}>
              <h3 style={{ color: C.lime, fontSize: 13, fontWeight: 600, margin: "0 0 16px", letterSpacing: 0.5 }}>CAPITAL & RISK</h3>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                {[
                  { key: "total_capital", label: "Total Capital (₹)", type: "number", min: 5000 },
                  { key: "max_risk_per_trade_pct", label: "Max Risk Per Trade (%)", type: "number", min: 0.05, max: 0.30, step: 0.01, display: v => (v * 100).toFixed(0), parse: v => v / 100 },
                  { key: "max_daily_loss_pct", label: "Max Daily Loss (%)", type: "number", min: 0.10, max: 0.50, step: 0.01, display: v => (v * 100).toFixed(0), parse: v => v / 100 },
                  { key: "max_open_trades", label: "Max Open Positions", type: "number", min: 1, max: 5 },
                ].map(({ key, label, type, min, max, step, display, parse }) => (
                  <div key={key}>
                    <label style={{ color: C.muted, fontSize: 12, display: "block", marginBottom: 6 }}>{label}</label>
                    <input
                      type={type} min={min} max={max} step={step || 1}
                      value={display ? display(settings[key]) : settings[key]}
                      onChange={e => setSettings(s => ({ ...s, [key]: parse ? parse(parseFloat(e.target.value)) : parseFloat(e.target.value) }))}
                      style={{ width: "100%", padding: "8px 12px", background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, color: C.text, fontSize: 14, outline: "none", boxSizing: "border-box" }}
                    />
                  </div>
                ))}
              </div>
            </Card>

            <Card style={{ marginBottom: 16 }}>
              <h3 style={{ color: C.lime, fontSize: 13, fontWeight: 600, margin: "0 0 16px", letterSpacing: 0.5 }}>TRADING MODE</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <Toggle value={settings.paper_trading} onChange={v => setSettings(s => ({ ...s, paper_trading: v }))} label="Paper Trading Mode (no real money)" />
                <Toggle value={settings.auto_trade} onChange={v => setSettings(s => ({ ...s, auto_trade: v }))} label="Auto Execute Trades (when signals fire)" />
              </div>
              {settings.auto_trade && !settings.paper_trading && (
                <div style={{ marginTop: 14, padding: 12, background: `${C.red}18`, border: `1px solid ${C.red}44`, borderRadius: 8, display: "flex", alignItems: "center", gap: 8 }}>
                  <AlertTriangle size={14} color={C.red} />
                  <span style={{ color: C.red, fontSize: 12 }}>Live auto-trading enabled. Real money will be used.</span>
                </div>
              )}
            </Card>

            <Card style={{ marginBottom: 16 }}>
              <h3 style={{ color: C.lime, fontSize: 13, fontWeight: 600, margin: "0 0 16px", letterSpacing: 0.5 }}>SIGNAL FILTERS</h3>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                {[
                  { key: "min_confidence", label: "Min Confidence Score", min: 0.3, max: 0.9, step: 0.01, display: v => (v * 100).toFixed(0) + "%" },
                  { key: "min_strategies_agree", label: "Min Strategies in Agreement", min: 2, max: 6 },
                ].map(({ key, label, min, max, step, display }) => (
                  <div key={key}>
                    <label style={{ color: C.muted, fontSize: 12, display: "block", marginBottom: 6 }}>{label}</label>
                    <input type="range" min={min} max={max} step={step || 1}
                      value={settings[key]}
                      onChange={e => setSettings(s => ({ ...s, [key]: parseFloat(e.target.value) }))}
                      style={{ width: "100%", accentColor: C.lime }}
                    />
                    <span style={{ color: C.lime, fontSize: 13, fontFamily: "monospace" }}>{display ? display(settings[key]) : settings[key]}</span>
                  </div>
                ))}
              </div>
            </Card>

            <Card style={{ marginBottom: 16 }}>
              <h3 style={{ color: C.lime, fontSize: 13, fontWeight: 600, margin: "0 0 16px", letterSpacing: 0.5 }}>API CREDENTIALS</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {["Upstox API Key", "Upstox API Secret", "Upstox Access Token", "Telegram Bot Token", "Telegram Chat ID"].map(label => (
                  <div key={label}>
                    <label style={{ color: C.muted, fontSize: 12, display: "block", marginBottom: 4 }}>{label}</label>
                    <input type="password" placeholder={`Enter ${label}`}
                      style={{ width: "100%", padding: "8px 12px", background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, color: C.text, fontSize: 13, outline: "none", boxSizing: "border-box" }}
                    />
                  </div>
                ))}
              </div>
            </Card>

            <button
              onClick={() => showToast("Settings saved successfully")}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 24px", background: C.lime, border: "none", borderRadius: 8, color: "#000", fontWeight: 700, cursor: "pointer", fontSize: 14 }}>
              <Save size={15} /> Save All Settings
            </button>
          </div>
        )}

        {/* ── BACKTEST ──────────────────────────────────────────────── */}
        {tab === "Backtest" && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: C.text }}>Backtest Results</h2>
              <button
                onClick={async () => { await api.runBacktest(); showToast("Backtest started in background"); }}
                style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 16px", background: C.limeGlow, border: `1px solid ${C.limeD}`, borderRadius: 7, color: C.lime, cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
                <RefreshCw size={13} /> Run New Backtest
              </button>
            </div>

            {backtests.map(bt => (
              <Card key={bt.id} style={{ marginBottom: 16 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
                  <div>
                    <p style={{ color: C.muted, fontSize: 12, margin: 0 }}>Period: {bt.period_start} → {bt.period_end}</p>
                    <p style={{ color: C.text, fontWeight: 600, margin: "4px 0 0" }}>Run: {bt.run_date}</p>
                  </div>
                  <Badge color={bt.total_return > 0 ? C.lime : C.red}>Return: {bt.total_return > 0 ? "+" : ""}{bt.total_return}%</Badge>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "repeat(6,1fr)", gap: 12 }}>
                  {[
                    { label: "Total Trades", val: bt.total_trades, color: C.text },
                    { label: "Win Rate", val: `${bt.win_rate}%`, color: C.lime },
                    { label: "Avg Return", val: `+${bt.avg_return}%`, color: C.cyan },
                    { label: "Max Drawdown", val: `${bt.max_drawdown}%`, color: C.red },
                    { label: "Sharpe Ratio", val: bt.sharpe_ratio, color: C.yellow },
                    { label: "Total Return", val: `${bt.total_return > 0 ? "+" : ""}${bt.total_return}%`, color: bt.total_return > 0 ? C.lime : C.red },
                  ].map(({ label, val, color }) => (
                    <div key={label} style={{ textAlign: "center", padding: "12px 8px", background: C.surface, borderRadius: 8 }}>
                      <p style={{ color: C.muted, fontSize: 11, margin: "0 0 4px" }}>{label}</p>
                      <p style={{ color, fontSize: 18, fontWeight: 700, margin: 0, fontFamily: "monospace" }}>{val}</p>
                    </div>
                  ))}
                </div>
              </Card>
            ))}

            <Card>
              <h3 style={{ margin: "0 0 16px", color: C.text, fontSize: 14, fontWeight: 600 }}>Simulated Equity Curve (Latest Run)</h3>
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={pnlData}>
                  <defs>
                    <linearGradient id="btGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={C.cyan} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={C.cyan} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                  <XAxis dataKey="date" stroke={C.muted} tick={{ fontSize: 11 }} />
                  <YAxis stroke={C.muted} tick={{ fontSize: 11 }} tickFormatter={v => `₹${v}`} />
                  <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8 }} />
                  <Area type="monotone" dataKey="capital" stroke={C.cyan} strokeWidth={2} fill="url(#btGrad)" />
                </AreaChart>
              </ResponsiveContainer>
            </Card>
          </div>
        )}
      </div>
    </div>
  );
}