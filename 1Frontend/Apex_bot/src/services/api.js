const API = "https://fuzzy-space-spork-w4r46vq96g73gv66-8000.app.github.dev/api";


export const api = {
  // Status
  getStatus: () => fetch(`${API}/status`).then(r => r.json()),
  activateBot: () => fetch(`${API}/bot/activate`, { method: "POST" }).then(r => r.json()),
  pauseBot: () => fetch(`${API}/bot/pause`, { method: "POST" }).then(r => r.json()),
  forceExitAll: () => fetch(`${API}/bot/force-exit-all`, { method: "POST" }).then(r => r.json()),

  // Trades
  getTrades: () => fetch(`${API}/trades`).then(r => r.json()),
  getOpenTrades: () => fetch(`${API}/trades/open`).then(r => r.json()),
  exitTrade: (tradeId) =>
    fetch(`${API}/trades/${tradeId}/exit`, {
      method: "POST",
    }).then(r => r.json()),

  // Signals
  getSignals: () => fetch(`${API}/signals`).then(r => r.json()),

  // Analytics
  getSummary: () => fetch(`${API}/analytics/summary`).then(r => r.json()),
  getDailyPnL: () => fetch(`${API}/analytics/daily-pnl`).then(r => r.json()),
  getStrategyPerformance: () =>
    fetch(`${API}/analytics/strategy-performance`).then(r => r.json()),

  getRegimeHistory: () =>
    fetch(`${API}/analytics/regime-history`).then(r => r.json()),

  // Settings
  getSettings: () => fetch(`${API}/settings`).then(r => r.json()),

  updateSettings: (data) =>
    fetch(`${API}/settings`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(data),
    }).then(r => r.json()),

  updateWeights: (weights) =>
    fetch(`${API}/settings/weights`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ weights }),
    }).then(r => r.json()),

  // Market
  getFunds: () => fetch(`${API}/market/funds`).then(r => r.json()),
  getPositions: () => fetch(`${API}/market/positions`).then(r => r.json()),

  // Backtests
  getBacktests: () =>
    fetch(`${API}/backtest/results`).then(r => r.json()),

  runBacktest: () =>
    fetch(`${API}/backtest/run`, {
      method: "POST",
    }).then(r => r.json()),
};