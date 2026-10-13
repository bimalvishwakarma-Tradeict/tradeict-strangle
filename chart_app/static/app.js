/* Delta India BTCUSD chart. History via local API; live via public WS candlestick_{tf}. */
(function () {
  const IST = "Asia/Kolkata";
  const WS_URL = "wss://socket.india.delta.exchange";
  const SYMBOL = "BTCUSD";
  const WS_CH = {
    "1m": "candlestick_1m",
    "3m": "candlestick_3m",
    "5m": "candlestick_5m",
    "15m": "candlestick_15m",
    "30m": "candlestick_30m",
    "1h": "candlestick_1h",
    "4h": "candlestick_4h",
  };
  const RES_SEC = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400 };
  const REPLAY_MS = { 1: 900, 3: 300, 10: 90 };
  const PRICE_SCALE_W = 80;
  const DEFAULT_REPLAY_BARS = 80;

  const el = (id) => document.getElementById(id);
  const fmtIst = (ts) =>
    new Date(ts * 1000).toLocaleString("en-IN", {
      timeZone: IST,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });

  let tf = "1m";
  let candleMap = new Map();
  let loadingLeft = false;
  let loadingRight = false;
  let historyEnd = false;
  let historyMode = false;
  let overlayPayload = { lines: [], markers: [] };
  let tradeMarks = [];
  let loadedTrades = [];
  let overlayTimer = null;
  let ws = null;
  let wsTimer = null;
  let backoff = 1000;
  let lastBarClose = 0;
  let replayOn = false;
  let replayPicking = false;
  let replayPlaying = false;
  let replayBarOpen = null;
  let replayTimer = null;
  let lastOverlaySigClose = -1;
  let replaySigs = [];
  let overlayGen = 0;
  let lastSigTfOpen = 0;
  let audioCtx = null;
  let alertBannerTimer = null;
  const pageLoadTs = Math.floor(Date.now() / 1000);
  let alertsOn = false;
  let replayFollow = true;
  let replayViewWidth = DEFAULT_REPLAY_BARS;
  let applyingRange = false;
  let syncingTs = false;
  let syncingXh = false;

  const chart = LightweightCharts.createChart(el("chart"), {
    layout: { background: { color: "#131722" }, textColor: "#787b86" },
    grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
    rightPriceScale: { borderColor: "#2a2e39", minimumWidth: PRICE_SCALE_W },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: "#758696", labelBackgroundColor: "#363a45" },
      horzLine: { color: "#758696", labelBackgroundColor: "#363a45" },
    },
    timeScale: {
      borderColor: "#2a2e39",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 48,
      shiftVisibleRangeOnNewBar: false,
      tickMarkFormatter: (t) => {
        const d = new Date(t * 1000);
        return d.toLocaleString("en-IN", { timeZone: IST, hour12: false, hour: "2-digit", minute: "2-digit" });
      },
    },
    localization: { timeFormatter: (t) => fmtIst(t) },
  });
  const series = chart.addCandlestickSeries({
    upColor: "#089981",
    downColor: "#f23645",
    borderVisible: false,
    wickUpColor: "#089981",
    wickDownColor: "#f23645",
    lastValueVisible: true,
    priceLineVisible: true,
    priceLineColor: "#089981",
  });
  let drawingsApi = null;
  let indApi = null;
  let stratCatalog = [];
  let stratInstances = [];
  const LS_STRAT = "chart_app.strategies.BTCUSD";
  const LS_PANEL = "chart_app.rightPanel";
  const vwapSeries = chart.addLineSeries({ color: "#58a6ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  const extraSeries = [];
  const paneCharts = [];
  function paneList() {
    return paneCharts;
  }
  function upsertPane(entry) {
    const i = paneCharts.findIndex((p) => p.id === entry.id);
    if (i >= 0) paneCharts[i] = Object.assign(paneCharts[i], entry);
    else {
      paneCharts.push(entry);
      entry.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        pushRangePeer("pane", range);
      });
      entry.chart.subscribeCrosshairMove((param) => {
        if (syncingXh) return;
        syncingXh = true;
        try {
          syncCrosshairToMain(param);
          syncCrosshairToPanes(param, entry.id);
        } catch (e) {}
        syncingXh = false;
      });
    }
  }
  function dropPane(id) {
    const i = paneCharts.findIndex((p) => p.id === id);
    if (i >= 0) paneCharts.splice(i, 1);
  }
  function rememberViewWidth(range) {
    if (!range || range.from == null || range.to == null) return;
    const w = Number(range.to) - Number(range.from);
    if (Number.isFinite(w) && w >= 8) replayViewWidth = w;
  }
  function applyLogicalRange(range) {
    if (!range) return;
    applyingRange = true;
    syncingTs = true;
    try {
      chart.timeScale().setVisibleLogicalRange(range);
      paneCharts.forEach((p) => {
        try { p.chart.timeScale().setVisibleLogicalRange(range); } catch (e) {}
      });
    } catch (e) {}
    syncingTs = false;
    requestAnimationFrame(() => { applyingRange = false; });
  }
  function pushRangePeer(fromChart, range) {
    if (syncingTs || !range) return;
    if (replayOn && replayFollow && fromChart === "rsi") return;
    syncingTs = true;
    try {
      if (fromChart === "main") {
        paneCharts.forEach((p) => {
          try { p.chart.timeScale().setVisibleLogicalRange(range); } catch (e) {}
        });
      }
      if (fromChart === "pane") chart.timeScale().setVisibleLogicalRange(range);
    } catch (e) {}
    syncingTs = false;
  }
  function onUserMovedRange(range) {
    if (applyingRange || syncingTs) return;
    if (!replayOn || !replayFollow || !range) return;
    const vis = visibleCandles();
    if (!vis.length) return;
    const lastIdx = vis.length - 1;
    const mid = (Number(range.from) + Number(range.to)) / 2;
    if (!Number.isFinite(mid) || Math.abs(mid - lastIdx) < 1.5) return;
    replayFollow = false;
    syncFollowBtn();
  }
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (range) rememberViewWidth(range);
    pushRangePeer("main", range);
    onUserMovedRange(range);
  });
  function centerReplayCursor() {
    if (!replayOn || replayBarOpen == null) return;
    const vis = visibleCandles();
    if (!vis.length) return;
    const idx = vis.length - 1;
    const width = replayViewWidth >= 8 ? replayViewWidth : DEFAULT_REPLAY_BARS;
    const half = width / 2;
    applyLogicalRange({ from: idx - half, to: idx + half });
  }

  function sortedCandles() {
    return [...candleMap.values()].sort((a, b) => a.time - b.time);
  }
  function barSecNow() {
    return RES_SEC[tf] || 60;
  }
  function rsiInst() {
    return stratInstances.find((s) => s.id === "S020_RSI" && s.visible !== false)
      || stratInstances.find((s) => s.id === "S020_RSI")
      || null;
  }
  function sigTfValue() {
    const it = rsiInst();
    return (it && it.params && it.params.signal_tf) || "15m";
  }
  function sigTfSec() {
    return RES_SEC[sigTfValue()] || 900;
  }
  function vwapParams() {
    const it = stratInstances.find((s) => s.id === "S020" && s.visible !== false)
      || stratInstances.find((s) => s.id === "S020");
    return (it && it.params) || {};
  }
  function replayCursorClose() {
    if (!replayOn || replayBarOpen == null) return null;
    return Number(replayBarOpen) + barSecNow();
  }
  function viewNewestOpen() {
    if (replayOn && replayBarOpen != null) return Number(replayBarOpen);
    return newestCandleTime();
  }
  function visibleCandles() {
    const all = sortedCandles();
    if (!replayOn || replayBarOpen == null) return all;
    const cap = Number(replayBarOpen);
    return all.filter((c) => Number(c.time) <= cap);
  }
  function applyCandles() {
    const vis = visibleCandles();
    series.setData(vis);
    const last = vis.length ? vis[vis.length - 1] : null;
    if (last) {
      const up = Number(last.close) >= Number(last.open);
      series.applyOptions({
        priceLineColor: up ? "#089981" : "#f23645",
        lastValueVisible: true,
        priceLineVisible: true,
      });
    }
    if (drawingsApi) drawingsApi.refresh();
    if (indApi) indApi.refresh();
    paintOhlcLegend(last);
    updateGoRtBtn();
  }
  function completedSigClose(cursorClose) {
    const sec = sigTfSec();
    if (sec <= 0) return 0;
    return Math.floor((Number(cursorClose) - 1) / sec) * sec + sec;
  }
  function setStatus(mode) {
    const dot = el("dot");
    const lab = el("statusLabel");
    dot.className = "";
    if (mode === "live") {
      dot.classList.add("live");
      lab.textContent = "live";
    } else {
      dot.classList.add("reconnecting");
      lab.textContent = "reconnecting";
    }
  }

  function oldestCandleTime() {
    if (candleMap.size === 0) return null;
    return Math.min(...candleMap.keys());
  }
  function newestCandleTime() {
    if (candleMap.size === 0) return null;
    return Math.max(...candleMap.keys());
  }

  function mergeRows(rows) {
    let n = 0;
    (rows || []).forEach((c) => {
      const t = Number(c && c.time);
      if (!Number.isFinite(t) || t < 1e9) return;
      if (!candleMap.has(t)) n += 1;
      candleMap.set(t, c);
    });
    return n;
  }

  function updateHistStatus() {
    const box = el("histStatus");
    if (!box) return;
    const n = candleMap.size;
    const oldest = oldestCandleTime();
    const newest = newestCandleTime();
    const fromTxt = oldest != null ? fmtIst(oldest) : "—";
    const toTxt = newest != null ? fmtIst(newest) : "—";
    const mode = replayOn ? "REPLAY" : (historyMode ? "HISTORY" : "LIVE");
    const phase = loadingLeft || loadingRight ? "loading…" : "idle";
    box.textContent = mode + " · " + n;
    box.title = "Candles: " + n + " | from " + fromTxt + " | to " + toTxt + " | " + mode + " | " + phase;
  }

  async function fetchCandles(opts) {
    const q = new URLSearchParams({ tf, limit: String((opts && opts.limit) || 1500) });
    if (opts && opts.start != null) q.set("start", String(opts.start));
    if (opts && opts.end != null) q.set("end", String(opts.end));
    const r = await fetch("/api/candles?" + q.toString());
    return r.json();
  }

  async function loadInitial() {
    candleMap = new Map();
    historyEnd = false;
    historyMode = false;
    loadingLeft = true;
    updateHistStatus();
    try {
      const j = await fetchCandles({ limit: 1500 });
      mergeRows(j.candles || []);
      applyCandles();
      if (candleMap.size) chart.timeScale().scrollToRealTime();
      await new Promise((r) => requestAnimationFrame(r));
      await reloadOverlay();
      pushRangePeer("main", chart.timeScale().getVisibleLogicalRange());
    } finally {
      loadingLeft = false;
      updateHistStatus();
    }
  }

  async function loadAround(centerTs) {
    candleMap = new Map();
    historyEnd = false;
    loadingLeft = true;
    loadingRight = true;
    clearExtra();
    vwapSeries.setData([]);
    series.setMarkers([]);
    updateHistStatus();
    const barSec = RES_SEC[tf] || 60;
    try {
      const [back, fwd] = await Promise.all([
        fetchCandles({ end: centerTs, limit: 750 }),
        fetchCandles({ start: centerTs, limit: 750 }),
      ]);
      mergeRows(back.candles || []);
      mergeRows(fwd.candles || []);
      applyCandles();
      chart.timeScale().setVisibleRange({
        from: centerTs - 150 * barSec,
        to: centerTs + 150 * barSec,
      });
      if (fwd.reached_now) historyMode = false;
      await reloadOverlay();
    } finally {
      loadingLeft = false;
      loadingRight = false;
      updateHistStatus();
    }
  }

  async function loadOlder() {
    if (loadingLeft || historyEnd || candleMap.size === 0) return;
    const oldest = oldestCandleTime();
    if (oldest == null) return;
    loadingLeft = true;
    updateHistStatus();
    try {
      const j = await fetchCandles({ end: oldest - 1, limit: 1500 });
      const n = mergeRows(j.candles || []);
      if (n === 0) {
        historyEnd = true;
      } else {
        applyCandles();
        await reloadOverlay();
        pushRangePeer("main", chart.timeScale().getVisibleLogicalRange());
      }
    } finally {
      loadingLeft = false;
      updateHistStatus();
    }
  }

  async function loadNewer() {
    if (!historyMode || loadingRight || candleMap.size === 0) return;
    const newest = newestCandleTime();
    if (newest == null) return;
    const barSec = RES_SEC[tf] || 60;
    loadingRight = true;
    updateHistStatus();
    try {
      const j = await fetchCandles({ start: newest + barSec, limit: 1500 });
      mergeRows(j.candles || []);
      applyCandles();
      await reloadOverlay();
      pushRangePeer("main", chart.timeScale().getVisibleLogicalRange());
      if (j.reached_now) {
        historyMode = false;
        updateHistStatus();
        connectWs();
      }
    } finally {
      loadingRight = false;
      updateHistStatus();
    }
  }

  chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
    if (!range || loadingLeft || loadingRight) return;
    const barSec = RES_SEC[tf] || 60;
    const oldest = oldestCandleTime();
    const newest = newestCandleTime();
    if (!historyEnd && oldest != null && range.from <= oldest + 20 * barSec) {
      loadOlder();
    }
    if (!replayOn && historyMode && newest != null && range.to >= newest - 20 * barSec) {
      loadNewer();
    }
  });

  function markerTooltip(time) {
    const t = Number(time);
    const bits = [];
    (overlayPayload.markers || []).forEach((m) => {
      if (Number(m.time) !== t) return;
      bits.push(
        (m.text || m.side || "") +
          " level=" + (m.level != null ? Number(m.level).toFixed(1) : "—") +
          (m.reason ? " " + m.reason : "") +
          " entry_allowed=" + (m.entry_allowed ? "true" : "false")
      );
    });
    loadedTrades.forEach((tr) => {
      if (Number(tr.entry_ts) === t) {
        const ov = (overlayPayload.markers || []).find((m) => Number(m.time) === t);
        bits.push(
          "ENTRY " + (tr.side || "") +
            " level=" + (ov && ov.level != null ? Number(ov.level).toFixed(1) : "—") +
            " created=" + (ov && ov.create_ts ? fmtIst(ov.create_ts) : "—") +
            " entry_allowed=true"
        );
      }
      if (tr.exit_ts && Number(tr.exit_ts) === t) {
        bits.push("EXIT " + (tr.reason || "") + " net=" + (tr.net != null ? Number(tr.net).toFixed(1) : "—"));
      }
    });
    return bits.join(" | ");
  }

  function syncCrosshairToPanes(param, exceptId) {
    paneCharts.forEach((p) => {
      if (exceptId && p.id === exceptId) return;
      if (typeof p.chart.setCrosshairPosition !== "function") return;
      if (!param || param.time == null) {
        if (typeof p.chart.clearCrosshairPosition === "function") p.chart.clearCrosshairPosition();
        return;
      }
      const ser = p.mainSeries;
      if (!ser) return;
      const y = p.valueAt ? p.valueAt(param.time) : 50;
      p.chart.setCrosshairPosition(y, param.time, ser);
    });
  }
  function syncCrosshairToMain(param) {
    if (typeof chart.setCrosshairPosition !== "function") return;
    if (!param || param.time == null) {
      if (typeof chart.clearCrosshairPosition === "function") chart.clearCrosshairPosition();
      return;
    }
    const bar = candleMap.get(Number(param.time));
    const px = bar && Number.isFinite(Number(bar.close)) ? Number(bar.close) : 0;
    chart.setCrosshairPosition(px, param.time, series);
  }
  chart.subscribeCrosshairMove((param) => {
    const tip = el("markTip");
    if (!param || !param.time || !param.seriesData) {
      paintOhlcLegend(null);
      if (tip) tip.textContent = "";
      if (!syncingXh) {
        syncingXh = true;
        try { syncCrosshairToPanes(param); } catch (e) {}
        syncingXh = false;
      }
      return;
    }
    const d = param.seriesData.get(series);
    if (d) {
      paintOhlcLegend({ time: param.time, open: d.open, high: d.high, low: d.low, close: d.close });
    } else paintOhlcLegend(null);
    if (tip) tip.textContent = markerTooltip(param.time);
    if (!syncingXh) {
      syncingXh = true;
      try { syncCrosshairToPanes(param); } catch (e) {}
      syncingXh = false;
    }
  });

  function clearExtra() {
    extraSeries.forEach((s) => chart.removeSeries(s));
    extraSeries.length = 0;
  }

  function indicatorRange() {
    const times = [...candleMap.keys()];
    if (!times.length) return null;
    const capOpen = viewNewestOpen();
    const to = replayOn && replayBarOpen != null
      ? Number(replayBarOpen) + barSecNow() - 1
      : (capOpen != null ? capOpen : Math.max(...times));
    return { from: Math.min(...times), to: to };
  }

  async function reloadOverlay(force) {
    const times = [...candleMap.keys()];
    if (!times.length) return;
    const from = Math.min(...times);
    const capOpen = viewNewestOpen();
    const cursorClose = replayOn && replayBarOpen != null
      ? replayCursorClose()
      : (capOpen != null ? Number(capOpen) + barSecNow() : Math.max(...times));
    const to = replayOn && replayBarOpen != null
      ? Number(replayBarOpen) + barSecNow() - 1
      : (capOpen != null ? capOpen : Math.max(...times));
    if (replayOn && !force) {
      const sigClose = completedSigClose(cursorClose);
      if (sigClose <= lastOverlaySigClose) {
        if (indApi) await indApi.reload();
        return;
      }
    }
    const gen = ++overlayGen;
    const visible = stratInstances.filter((s) => s.visible !== false);
    if (!visible.length) {
      overlayPayload = { lines: [], markers: [] };
      lastOverlaySigClose = completedSigClose(cursorClose);
      paintOverlay(overlayPayload);
      if (indApi) await indApi.reload();
      return;
    }
    await Promise.all(visible.map(async (it) => {
      const q = new URLSearchParams({
        strategy: it.id,
        tf,
        from: String(from),
        to: String(to),
        params: JSON.stringify(it.params || {}),
      });
      const r = await fetch("/api/overlay?" + q.toString());
      const j = await r.json();
      it._payload = j;
    }));
    if (gen !== overlayGen) return;
    overlayPayload = mergeStratPayloads();
    lastOverlaySigClose = completedSigClose(cursorClose);
    paintOverlay(overlayPayload);
    if (replayOn) ingestReplaySignals(overlayPayload);
    else scanLiveAlerts(overlayPayload);
    if (indApi) await indApi.reload();
  }

  function mergeStratPayloads() {
    const lines = [];
    const markers = [];
    let rsi = [];
    let rsi_ob = 70;
    let rsi_os = 30;
    stratInstances.forEach((it) => {
      if (it.visible === false || !it._payload) return;
      const j = it._payload;
      const p = it.params || {};
      (j.lines || []).forEach((ln) => {
        if (ln.kind === "vwap" && p.show_vwap === false) return;
        if (ln.kind !== "vwap" && ln.active && p.show_active === false) return;
        if (ln.kind !== "vwap" && !ln.active && p.show_expired === false) return;
        lines.push(ln);
      });
      const showMarks = p.show_marks !== false;
      const backtest = p.marker_mode === "backtest";
      if (showMarks && !backtest) markers.push.apply(markers, j.markers || []);
      if (it.id === "S020_RSI" && p.show_rsi_pane !== false) {
        rsi = j.rsi || rsi;
        if (j.rsi_ob != null) rsi_ob = j.rsi_ob;
        if (j.rsi_os != null) rsi_os = j.rsi_os;
      }
    });
    return { lines, markers, rsi, rsi_ob, rsi_os };
  }

  function clipPts(pts, oldest, newest) {
    const src = (pts || [])
      .filter((p) => Number(p.time) >= oldest && (newest == null || Number(p.time) <= newest))
      .sort((a, b) => Number(a.time) - Number(b.time));
    if (!src.length) return [];
    const times = sortedCandles()
      .map((c) => c.time)
      .filter((t) => t >= oldest && (newest == null || t <= newest));
    if (!times.length) return src;
    const out = [];
    let i = 0;
    let prevDay = null;
    for (const t of times) {
      const day = Math.floor(Number(t) / 86400);
      while (i + 1 < src.length && Number(src[i + 1].time) <= t) i += 1;
      if (prevDay != null && day !== prevDay) {
        out.push({ time: t });
        prevDay = day;
        continue;
      }
      prevDay = day;
      const srcT = Number(src[i].time);
      const srcVal = Number(src[i].value);
      if (!Number.isFinite(srcVal) || srcT > t) continue;
      if (Math.floor(srcT / 86400) !== day) continue;
      out.push({ time: t, value: srcVal });
    }
    return out;
  }

  function clipLinePoints(ln, oldest, newest) {
    const pts = ln.points || [];
    if (!pts.length) return [];
    if (ln.kind === "vwap") return clipPts(pts, oldest, newest);
    const a = pts[0];
    const b = pts[pts.length - 1];
    if (Number(b.time) < oldest) return [];
    if (newest != null && Number(a.time) > newest) return [];
    const t0 = Math.max(Number(a.time), oldest);
    const t1 = newest != null ? Math.min(Number(b.time), newest) : Number(b.time);
    if (t1 <= t0) return [];
    return [
      { time: t0, value: a.value },
      { time: t1, value: b.value },
    ];
  }

  function paintOverlay(j) {
    const oldest = oldestCandleTime();
    const newest = viewNewestOpen();
    const cursorClose = replayOn ? replayCursorClose() : (newest != null ? newest + barSecNow() : null);
    const vp = vwapParams();
    const showVwap = vp.show_vwap !== false;
    const showActive = vp.show_active !== false;
    const showExpired = vp.show_expired !== false;
    const showMarks = vp.show_marks !== false;
    const vwap = (j.lines || []).find((x) => x.kind === "vwap");
    vwapSeries.applyOptions({ visible: showVwap });
    const vwapPts = showVwap && vwap && oldest != null ? clipLinePoints(vwap, oldest, newest) : [];
    vwapSeries.setData(vwapPts);
    clearExtra();
    (j.lines || []).forEach((ln) => {
      if (ln.kind === "vwap") return;
      if (ln.active && !showActive) return;
      if (!ln.active && !showExpired) return;
      const pts = oldest != null ? clipLinePoints(ln, oldest, newest) : (ln.points || []);
      if (pts.length < 2) return;
      const color = ln.kind === "high"
        ? (ln.active ? "#ef4444" : "rgba(239,68,68,0.5)")
        : (ln.active ? "#22c55e" : "rgba(34,197,94,0.5)");
      const s = chart.addLineSeries({
        color,
        lineWidth: ln.active ? 2 : 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      s.setData(pts);
      extraSeries.push(s);
    });
    const inRange = (t) =>
      (oldest == null || Number(t) >= oldest) && (newest == null || Number(t) <= newest);
    const mode = vp.marker_mode || "raw";
    let marks = [];
    if (showMarks && mode === "backtest") {
      marks = tradeMarks.filter((m) => inRange(m.time));
    } else if (showMarks) {
      const candleTimes = sortedCandles().map((c) => Number(c.time)).sort((a, b) => a - b);
      const snap = (t) => {
        const n = Number(t);
        if (!candleTimes.length) return n;
        for (let i = 0; i < candleTimes.length; i += 1) {
          if (candleTimes[i] >= n) return candleTimes[i];
        }
        return candleTimes[candleTimes.length - 1];
      };
      marks = (j.markers || [])
          .filter((m) => inRange(m.time))
          .map((m) => {
          const large = m.size === "large" || m.text === "SHORT" || m.text === "LONG";
          const skipped = Boolean(m.reason) || m.entry_allowed === false;
          const longish = m.text === "OBL" || m.text === "LONG" || m.side === "long";
          const hot = longish ? "#22c55e" : "#ef4444";
          const faded = longish ? "rgba(34,197,94,0.4)" : "rgba(239,68,68,0.4)";
          return {
            time: snap(m.time),
            position: longish ? "belowBar" : "aboveBar",
            color: skipped ? faded : hot,
            shape: longish ? "arrowUp" : "arrowDown",
            text: m.text || "",
            size: large ? 2 : 0,
          };
        });
    }
    marks.sort((a, b) => Number(a.time) - Number(b.time) || String(a.text).length - String(b.text).length);
    try {
      series.setMarkers(marks);
    } catch (err) {
      console.warn("setMarkers failed", err, marks.slice(0, 8));
    }
    const rp = rsiInst();
    const useRsi = Boolean(rp && rp.visible !== false && rp.params && rp.params.show_rsi_pane !== false);
    if (indApi && typeof indApi.setAuxPane === "function") {
      if (!useRsi) indApi.setAuxPane("s020rsi", { visible: false });
      else {
        const candles = visibleCandles();
        const rsiByT = new Map();
        (j.rsi || []).forEach((p) => {
          const t = Number(p.time);
          const v = Number(p.value);
          if (!Number.isFinite(t) || !Number.isFinite(v)) return;
          if (cursorClose != null && t > cursorClose) return;
          rsiByT.set(t, v);
        });
        const rsiPts = candles.map((c) => {
          const t = Number(c.time);
          return rsiByT.has(t) ? { time: t, value: rsiByT.get(t) } : { time: t };
        });
        const ob = Number(j.rsi_ob != null ? j.rsi_ob : 70);
        const os = Number(j.rsi_os != null ? j.rsi_os : 30);
        const lvl = (val) => candles.map((c) => ({ time: Number(c.time), value: val }));
        indApi.setAuxPane("s020rsi", {
          visible: true,
          title: "RSI Div",
          series: [
            { key: "rsi", color: "#d2a8ff", width: 1, style: "solid", lastValueVisible: true, points: rsiPts },
            { key: "ob", color: "#f85149", width: 1, style: "dashed", lastValueVisible: false, points: lvl(ob) },
            { key: "os", color: "#3fb950", width: 1, style: "dashed", lastValueVisible: false, points: lvl(os) },
          ],
          fills: [{ kind: "hband", top: ob, bottom: os, fill: "rgba(126,87,194,0.08)" }],
        });
      }
    }
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (replayOn && replayFollow) centerReplayCursor();
        else {
          const r = chart.timeScale().getVisibleLogicalRange();
          if (r) pushRangePeer("main", r);
        }
      });
    });
  }

  function syncRsiPaneLabel(ob, os) {
    return { ob, os };
  }

  function lsGet(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }
  function lsSet(key, val) {
    try {
      localStorage.setItem(key, val);
    } catch (e) {}
  }
  function loadJsonLs(key, fallback) {
    try {
      const parsed = JSON.parse(lsGet(key, JSON.stringify(fallback)));
      return parsed == null ? fallback : parsed;
    } catch (e) {
      return fallback;
    }
  }
  function stratLabel() {
    const it = rsiInst() || stratInstances[0];
    if (it && it.id === "S020_RSI") return "RSI Div";
    return "VWAP";
  }
  function fmtHmIst(ts) {
    return new Date(Number(ts) * 1000).toLocaleString("en-IN", {
      timeZone: IST,
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  function fmtPx(n) {
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    return Math.round(x).toLocaleString("en-US");
  }
  function signalPrice(m) {
    const t = Number(m.time);
    const bar = candleMap.get(t);
    if (bar && Number.isFinite(Number(bar.close))) return Number(bar.close);
    const newest = newestCandleTime();
    const last = newest != null ? candleMap.get(newest) : null;
    if (last && Number.isFinite(Number(last.close))) return Number(last.close);
    return Number(m.price);
  }
  function unlockAudio() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!audioCtx) audioCtx = new AC();
      if (audioCtx.state === "suspended") void audioCtx.resume();
      const buf = audioCtx.createBuffer(1, 1, 22050);
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioCtx.destination);
      src.start(0);
    } catch (e) {}
  }
  function playBeeps(side) {
    unlockAudio();
    if (!audioCtx) return;
    const rising = String(side).toUpperCase() === "LONG";
    const freqs = rising ? [523.25, 659.25, 783.99] : [783.99, 659.25, 523.25];
    freqs.forEach((f, i) => {
      const t0 = audioCtx.currentTime + i * 0.18;
      const osc = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(f, t0);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(0.18, t0 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.14);
      osc.connect(g);
      g.connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.16);
    });
  }
  function hideAlertBanner() {
    const b = el("alertBanner");
    if (b) b.className = "";
    if (alertBannerTimer) {
      clearTimeout(alertBannerTimer);
      alertBannerTimer = null;
    }
  }
  function showAlertBanner(text, side) {
    const b = el("alertBanner");
    if (!b) return;
    b.textContent = text;
    b.className = "show " + (String(side).toUpperCase() === "LONG" ? "long" : "short");
    if (alertBannerTimer) clearTimeout(alertBannerTimer);
    alertBannerTimer = setTimeout(hideAlertBanner, 10000);
  }
  function showDesktopNote(text) {
    try {
      if (!window.Notification || Notification.permission !== "granted") return;
      const n = new Notification(text, { body: text });
      n.onclick = () => {
        try { window.focus(); } catch (e) {}
        n.close();
      };
    } catch (e) {}
  }
  function renderAlertLog() {
    const ul = el("alertLog");
    if (!ul) return;
    const rows = loadJsonLs("chart_app.alertLog", []);
    ul.innerHTML = "";
    (Array.isArray(rows) ? rows : []).slice(0, 20).forEach((row) => {
      const li = document.createElement("li");
      li.className = String(row.side || "").toLowerCase();
      li.textContent = fmtHmIst(row.ts) + " IST  " + (row.side || "") + "  " + (row.tf || "") + "  " + fmtPx(row.price);
      ul.appendChild(li);
    });
  }
  function pushAlertLog(row) {
    const rows = loadJsonLs("chart_app.alertLog", []);
    const next = [row].concat(Array.isArray(rows) ? rows : []).slice(0, 20);
    lsSet("chart_app.alertLog", JSON.stringify(next));
    renderAlertLog();
  }
  function loadDedupe() {
    const a = loadJsonLs("chart_app.alertDedupe", []);
    return Array.isArray(a) ? a : [];
  }
  function markDedupe(key) {
    const a = loadDedupe();
    if (a.indexOf(key) >= 0) return;
    a.push(key);
    lsSet("chart_app.alertDedupe", JSON.stringify(a.slice(-500)));
  }
  function fireAlert(opts) {
    const side = String(opts.side || "").toUpperCase();
    const tfLab = opts.tf || sigTfValue();
    const ts = Number(opts.ts) || Math.floor(Date.now() / 1000);
    const price = opts.price;
    const text = opts.text || (
      side + " signal — " + stratLabel() + " " + tfLab + " — " + fmtHmIst(ts) + " IST — price " + fmtPx(price)
    );
    showAlertBanner(text, side);
    playBeeps(side);
    showDesktopNote(text);
    pushAlertLog({ ts, side, tf: tfLab, price });
  }
  function isSkippedMarker(m) {
    if (m.entry_allowed === false) return true;
    const r = m.reason != null ? String(m.reason).trim() : "";
    return r.length > 0;
  }
  function scanLiveAlerts(j) {
    if (!alertsOn || replayOn || historyMode) return;
    const includeSkipped = el("togAlertSkipped") && el("togAlertSkipped").checked;
    const st = (rsiInst() && rsiInst().id) || (stratInstances[0] && stratInstances[0].id) || "S020";
    const sigTf = sigTfValue();
    const seen = loadDedupe();
    (j.markers || []).forEach((m) => {
      const side = String(m.text || "").toUpperCase();
      if (side !== "LONG" && side !== "SHORT") return;
      if (isSkippedMarker(m) && !includeSkipped) return;
      const ts = Number(m.signal_close_ts != null ? m.signal_close_ts : m.time);
      if (!Number.isFinite(ts) || ts <= pageLoadTs) return;
      const key = st + "|" + sigTf + "|" + ts + "|" + side;
      if (seen.indexOf(key) >= 0) return;
      seen.push(key);
      markDedupe(key);
      fireAlert({ side, tf: sigTf, ts, price: signalPrice(m) });
    });
  }
  function syncAlertUi() {
    const btn = el("btnAlerts");
    if (btn) btn.textContent = alertsOn ? "Alert ON" : "Alert";
    const tog = el("togAlerts");
    if (tog) tog.checked = alertsOn;
    const skip = el("togAlertSkipped");
    if (skip) skip.checked = lsGet("chart_app.alertsSkipped", "0") === "1";
  }
  function initAlertUi() {
    alertsOn = lsGet("chart_app.alertsOn", "0") === "1";
    syncAlertUi();
    renderAlertLog();
    const banner = el("alertBanner");
    if (banner) banner.addEventListener("click", hideAlertBanner);
    async function setAlertsOn(on) {
      alertsOn = Boolean(on);
      if (alertsOn) {
        unlockAudio();
        try {
          if (window.Notification && Notification.permission === "default") {
            await Notification.requestPermission();
          }
        } catch (e) {}
      }
      lsSet("chart_app.alertsOn", alertsOn ? "1" : "0");
      syncAlertUi();
    }
    if (el("btnAlerts")) {
      el("btnAlerts").addEventListener("click", async () => {
        await setAlertsOn(!alertsOn);
      });
    }
    if (el("togAlerts")) {
      el("togAlerts").addEventListener("change", async () => {
        await setAlertsOn(el("togAlerts").checked);
      });
    }
    if (el("togAlertSkipped")) {
      el("togAlertSkipped").addEventListener("change", () => {
        lsSet("chart_app.alertsSkipped", el("togAlertSkipped").checked ? "1" : "0");
      });
    }
    if (el("btnTestAlert")) {
      el("btnTestAlert").addEventListener("click", () => {
        unlockAudio();
        const newest = newestCandleTime();
        const last = newest != null ? candleMap.get(newest) : null;
        const px = last ? last.close : 0;
        fireAlert({
          side: "LONG",
          tf: sigTfValue(),
          ts: Math.floor(Date.now() / 1000),
          price: px,
        });
      });
    }
  }

  function wsCandleTime(raw) {
    let t = Number(raw.candle_start_time != null ? raw.candle_start_time : raw.time);
    if (!Number.isFinite(t)) return null;
    if (t > 1e15) t = Math.floor(t / 1e9);
    else if (t > 1e12) t = Math.floor(t / 1000);
    if (t < 1e9) return null;
    return t;
  }

  function applyLiveBar(raw) {
    if (historyMode || replayOn) return;
    const t = wsCandleTime(raw);
    if (t == null) return;
    const bar = {
      time: t,
      open: Number(raw.open),
      high: Number(raw.high),
      low: Number(raw.low),
      close: Number(raw.close),
      volume: Number(raw.volume || 0),
    };
    const prev = candleMap.get(t);
    candleMap.set(t, bar);
    series.update(bar);
    updateHistStatus();
    if (lastBarClose && t > lastBarClose) {
      scheduleLiveOverlayOnSigClose(lastBarClose + barSecNow());
      if (indApi) void indApi.reload();
    }
    lastBarClose = t;
  }

  function onSigTfCandle(raw) {
    if (historyMode || replayOn) return;
    const t = wsCandleTime(raw);
    if (t == null) return;
    if (lastSigTfOpen && t > lastSigTfOpen) {
      scheduleLiveOverlayOnSigClose(lastSigTfOpen + sigTfSec());
    }
    lastSigTfOpen = t;
  }

  function scheduleLiveOverlayOnSigClose(barCloseUnix) {
    if (historyMode || replayOn) return;
    const sc = completedSigClose(barCloseUnix);
    if (sc <= lastOverlaySigClose) return;
    if (overlayTimer) clearTimeout(overlayTimer);
    overlayTimer = setTimeout(() => reloadOverlay(true), 400);
  }

  function disconnectWs() {
    if (wsTimer) {
      clearTimeout(wsTimer);
      wsTimer = null;
    }
    try {
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    } catch (e) {}
    ws = null;
    const lab = el("statusLabel");
    if (lab) lab.textContent = "history";
    el("dot").className = "";
  }

  function connectWs() {
    if (historyMode || replayOn) return;
    setStatus("reconnecting");
    try {
      if (ws) ws.close();
    } catch (e) {}
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      backoff = 1000;
      setStatus("live");
      const ch = WS_CH[tf] || "candlestick_1m";
      const sigTf = sigTfValue();
      const sigCh = WS_CH[sigTf];
      const channels = [{ name: ch, symbols: [SYMBOL] }];
      if (sigCh && sigCh !== ch) channels.push({ name: sigCh, symbols: [SYMBOL] });
      ws.send(JSON.stringify({ type: "subscribe", payload: { channels } }));
      ws.send(JSON.stringify({ type: "enable_heartbeat" }));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      const typ = String(msg.type || "");
      const chartCh = WS_CH[tf] || "candlestick_1m";
      const sigTf = sigTfValue();
      const sigCh = WS_CH[sigTf];
      if (typ === chartCh) applyLiveBar(msg);
      else if (sigCh && typ === sigCh) onSigTfCandle(msg);
    };
    ws.onclose = () => {
      if (historyMode || replayOn) return;
      setStatus("reconnecting");
      scheduleReconnect();
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    if (historyMode || replayOn) return;
    if (wsTimer) clearTimeout(wsTimer);
    wsTimer = setTimeout(connectWs, backoff);
    backoff = Math.min(backoff * 2, 15000);
  }

  function jumpCenterTime() {
    const vis = chart.timeScale().getVisibleRange();
    if (vis && vis.from != null && vis.to != null) {
      return Math.floor((Number(vis.from) + Number(vis.to)) / 2);
    }
    const newest = newestCandleTime();
    const oldest = oldestCandleTime();
    if (newest != null && oldest != null) return Math.floor((oldest + newest) / 2);
    return Math.floor(Date.now() / 1000);
  }

  function snapBarOpen(t) {
    const all = sortedCandles();
    let best = null;
    for (let i = 0; i < all.length; i += 1) {
      if (Number(all[i].time) <= t) best = Number(all[i].time);
      else break;
    }
    return best;
  }

  function closeAt(ts) {
    const all = sortedCandles();
    let bar = null;
    for (let i = 0; i < all.length; i += 1) {
      if (Number(all[i].time) <= ts) bar = all[i];
      else break;
    }
    return bar && Number.isFinite(bar.close) ? Number(bar.close) : null;
  }

  function dirMovePct(side, px0, px1) {
    if (px0 == null || px1 == null || px0 === 0) return null;
    const raw = (px1 - px0) / px0;
    return (side === "long" || side === "LONG" ? raw : -raw) * 100;
  }

  function fmtPct(x) {
    if (x == null || !Number.isFinite(x)) return "—";
    const s = x >= 0 ? "+" : "";
    return s + x.toFixed(2) + "%";
  }

  function ingestReplaySignals(j) {
    const cap = replayCursorClose();
    (j.markers || []).forEach((m) => {
      const txt = String(m.text || "");
      if (txt !== "SHORT" && txt !== "LONG") return;
      const t = Number(m.time);
      if (!Number.isFinite(t) || (cap != null && t > replayBarOpen)) return;
      const id = txt + "@" + t;
      if (replaySigs.some((s) => s.id === id)) return;
      replaySigs.push({
        id,
        time: t,
        closeTs: Number(m.signal_close_ts || t + sigTfSec()),
        side: txt === "LONG" ? "long" : "short",
        text: txt,
        level: m.level,
        ob_rsi: m.ob_rsi,
        sig_rsi: m.sig_rsi,
      });
    });
    replaySigs.sort((a, b) => a.time - b.time);
    renderReplayStats();
  }

  function renderReplayStats() {
    const lab = el("replayCursorLab");
    const ul = el("replayStats");
    if (lab) {
      if (!replayOn || replayBarOpen == null) lab.textContent = "off";
      else lab.textContent = "cursor close " + fmtIst(replayCursorClose()) + " | n=" + replaySigs.length;
    }
    if (!ul) return;
    ul.innerHTML = "";
    const cur = replayCursorClose();
    replaySigs.forEach((s) => {
      const px0 = closeAt(s.closeTs - 1);
      const h1ok = cur != null && cur >= s.closeTs + 3600;
      const h4ok = cur != null && cur >= s.closeTs + 14400;
      const m1 = h1ok ? dirMovePct(s.side, px0, closeAt(s.closeTs + 3600 - 1)) : null;
      const m4 = h4ok ? dirMovePct(s.side, px0, closeAt(s.closeTs + 14400 - 1)) : null;
      const li = document.createElement("li");
      li.className = s.side;
      li.textContent =
        fmtIst(s.time) + " " + s.text +
        " L=" + (s.level != null ? Number(s.level).toFixed(1) : "—") +
        "  1h " + (h1ok ? fmtPct(m1) : "pending") +
        "  4h " + (h4ok ? fmtPct(m4) : "pending");
      ul.appendChild(li);
    });
  }

  function setReplayButtons() {
    const on = replayOn;
    if (el("btnSelectBar")) el("btnSelectBar").classList.toggle("active", replayPicking);
    if (el("btnReplayPlay")) {
      el("btnReplayPlay").disabled = !on;
      el("btnReplayPlay").textContent = replayPlaying ? "⏸" : "▶";
    }
    if (el("btnReplayStep")) el("btnReplayStep").disabled = !on;
    syncFollowBtn();
    const lab = el("statusLabel");
    if (on && lab) {
      lab.textContent = replayPlaying ? "replay play" : "replay";
      el("dot").className = "";
    }
  }

  function syncFollowBtn() {
    const btn = el("btnReplayFollow");
    if (!btn) return;
    btn.disabled = !replayOn;
    btn.classList.toggle("active", Boolean(replayOn && replayFollow));
  }

  function stopReplayTimer() {
    if (replayTimer) {
      clearTimeout(replayTimer);
      replayTimer = null;
    }
    replayPlaying = false;
  }

  async function ensureNextBar() {
    const all = sortedCandles();
    const cap = Number(replayBarOpen);
    const nxt = all.find((c) => Number(c.time) > cap);
    if (nxt) return nxt;
    const start = cap + barSecNow();
    const j = await fetchCandles({ start, limit: 500 });
    mergeRows(j.candles || []);
    return sortedCandles().find((c) => Number(c.time) > cap) || null;
  }

  async function startReplayAt(openTs) {
    const snapped = snapBarOpen(Number(openTs));
    if (snapped == null) return;
    stopReplayTimer();
    replayOn = true;
    replayPicking = false;
    replayBarOpen = snapped;
    replaySigs = [];
    lastOverlaySigClose = -1;
    historyMode = true;
    disconnectWs();
    if (overlayTimer) {
      clearTimeout(overlayTimer);
      overlayTimer = null;
    }
    replayFollow = true;
    replayViewWidth = DEFAULT_REPLAY_BARS;
    applyingRange = true;
    applyCandles();
    showReplayBar(true);
    setReplayButtons();
    updateHistStatus();
    renderReplayStats();
    await reloadOverlay(true);
    centerReplayCursor();
    requestAnimationFrame(() => { applyingRange = false; });
  }

  async function replayStep() {
    if (!replayOn || replayBarOpen == null) return;
    const nxt = await ensureNextBar();
    if (!nxt) {
      stopReplayTimer();
      setReplayButtons();
      return;
    }
    replayBarOpen = Number(nxt.time);
    replayFollow = true;
    syncFollowBtn();
    applyingRange = true;
    applyCandles();
    updateHistStatus();
    renderReplayStats();
    await reloadOverlay(false);
    centerReplayCursor();
    requestAnimationFrame(() => {
      centerReplayCursor();
      applyingRange = false;
    });
  }

  function replayPlayLoop() {
    if (!replayOn || !replayPlaying) return;
    const spd = Number(el("replaySpeed") ? el("replaySpeed").value : 1);
    const ms = REPLAY_MS[spd] || 900;
    replayTimer = setTimeout(async () => {
      await replayStep();
      if (replayPlaying) replayPlayLoop();
    }, ms);
  }

  async function exitReplayLive() {
    stopReplayTimer();
    replayOn = false;
    replayPicking = false;
    replayBarOpen = null;
    replaySigs = [];
    lastOverlaySigClose = -1;
    historyMode = false;
    showReplayBar(false);
    setReplayButtons();
    renderReplayStats();
    await loadInitial();
    connectWs();
  }

  if (el("btnSelectBar")) {
    el("btnSelectBar").addEventListener("click", () => {
      replayPicking = !replayPicking;
      setReplayButtons();
    });
  }
  chart.subscribeClick((param) => {
    if (drawingsApi && drawingsApi.isToolActive()) return;
    if (!replayPicking || !param || param.time == null) return;
    startReplayAt(Number(param.time));
  });
  if (el("btnReplayPlay")) {
    el("btnReplayPlay").addEventListener("click", () => {
      if (!replayOn) return;
      if (replayPlaying) {
        stopReplayTimer();
        setReplayButtons();
        return;
      }
      replayPlaying = true;
      setReplayButtons();
      replayPlayLoop();
    });
  }
  if (el("btnReplayStep")) el("btnReplayStep").addEventListener("click", () => replayStep());
  if (el("btnReplayFollow")) {
    el("btnReplayFollow").addEventListener("click", () => {
      if (!replayOn) return;
      replayFollow = true;
      syncFollowBtn();
      centerReplayCursor();
    });
  }
  if (el("btnReplayLive")) el("btnReplayLive").addEventListener("click", () => exitReplayLive());

  el("tf").addEventListener("change", async () => {
    tf = el("tf").value;
    if (replayOn) {
      const cur = replayCursorClose() || jumpCenterTime();
      disconnectWs();
      await loadAround(cur);
      replayBarOpen = snapBarOpen(cur - 1);
      lastOverlaySigClose = -1;
      applyCandles();
      await reloadOverlay(true);
      if (replayFollow) centerReplayCursor();
      setReplayButtons();
      return;
    }
    if (historyMode) {
      const center = jumpCenterTime();
      disconnectWs();
      await loadAround(center);
    } else {
      await loadInitial();
      connectWs();
    }
  });

  el("btnJump").addEventListener("click", async () => {
    const v = el("jump").value;
    if (!v) return;
    const ms = new Date(v + "+05:30").getTime();
    if (!Number.isFinite(ms)) return;
    historyMode = true;
    disconnectWs();
    if (el("gotoModal")) el("gotoModal").classList.remove("open");
    await loadAround(Math.floor(ms / 1000));
  });

  el("btnLive").addEventListener("click", async () => {
    if (replayOn) {
      await exitReplayLive();
      return;
    }
    historyMode = false;
    await loadInitial();
    connectWs();
  });

  async function loadFileList() {
    const r = await fetch("/api/trades/files");
    const j = await r.json();
    const sel = el("tradeFile");
    sel.innerHTML = "";
    (j.files || []).forEach((f) => {
      const o = document.createElement("option");
      o.value = f;
      o.textContent = f.split("/").slice(-2).join("/");
      sel.appendChild(o);
    });
  }

  el("btnTrades").addEventListener("click", async () => {
    const file = el("tradeFile").value;
    if (!file) return;
    const r = await fetch("/api/trades?file=" + encodeURIComponent(file));
    const j = await r.json();
    const ul = el("trades");
    ul.innerHTML = "";
    tradeMarks = [];
    loadedTrades = j.trades || [];
    stratInstances.forEach((it) => {
      if (it.id === "S020") it.params.marker_mode = "backtest";
    });
    saveStrats();
    (j.trades || []).forEach((t) => {
      const li = document.createElement("li");
      li.className = t.side;
      li.textContent = (t.side || "") + " " + (t.entry_ist || "") + " " + (t.reason || "") + " net=" + (t.net != null ? Number(t.net).toFixed(1) : "");
      li.title = JSON.stringify(t);
      li.onclick = () => {
        const from = t.entry_ts - 3600;
        const to = (t.exit_ts || t.entry_ts) + 3600;
        chart.timeScale().setVisibleRange({ from, to });
      };
      ul.appendChild(li);
      tradeMarks.push({
        time: t.entry_ts,
        position: t.side === "long" ? "belowBar" : "aboveBar",
        color: t.side === "long" ? "#22c55e" : "#ef4444",
        shape: t.side === "long" ? "arrowUp" : "arrowDown",
        text: t.side === "long" ? "▲" : "▼",
      });
      if (t.exit_ts) {
        tradeMarks.push({
          time: t.exit_ts,
          position: "inBar",
          color: "#d29922",
          shape: "square",
          text: (t.reason || "X").toUpperCase().slice(0, 8),
        });
      }
    });
    paintOverlay(overlayPayload);
  });

  function sizeCharts() {
    chart.applyOptions({ width: el("chart").clientWidth, height: el("chart").clientHeight });
    paneCharts.forEach((p) => {
      const h = p.host ? p.host.clientHeight : 132;
      try { p.chart.applyOptions({ width: (p.host || el("chart")).clientWidth, height: Math.max(60, h - 6) }); } catch (e) {}
    });
    if (drawingsApi) drawingsApi.refresh();
    if (indApi) indApi.refresh();
  }
  window.addEventListener("resize", () => sizeCharts());
  sizeCharts();
  if (typeof window.createChartDrawings === "function") {
    drawingsApi = window.createChartDrawings({
      chart: chart,
      series: series,
      host: el("chart"),
      getCandles: function () { return sortedCandles(); },
      getTfSec: function () { return RES_SEC[tf] || 60; },
      isReplayPicking: function () { return replayPicking; },
      getMarkPrice: function () {
        const vis = visibleCandles();
        if (!vis.length) return null;
        return Number(vis[vis.length - 1].close);
      },
      getPaneCharts: function () { return paneList().map((p) => p.chart); },
    });
  }
  if (typeof window.createChartIndicators === "function") {
    indApi = window.createChartIndicators({
      chart: chart,
      series: series,
      getTf: function () { return tf; },
      getRange: function () { return indicatorRange(); },
      paneHost: el("paneHost"),
      priceScaleW: PRICE_SCALE_W,
      registerPane: upsertPane,
      unregisterPane: dropPane,
      onResize: function () { sizeCharts(); },
      onAuxSettings: function () { openRightPanel("paneStrat"); },
      onAuxRemove: function () {
        const it = rsiInst();
        if (it && it.params) {
          it.params.show_rsi_pane = false;
          saveStrats();
          lastOverlaySigClose = -1;
          reloadOverlay(true);
        }
      },
    });
  }

  function defaultParamsFromSchema(schema) {
    const p = {};
    (schema || []).forEach((f) => {
      p[f.key] = f.default;
    });
    return p;
  }
  function saveStrats() {
    try {
      lsSet(LS_STRAT, JSON.stringify(stratInstances.map((s) => ({
        uid: s.uid, id: s.id, params: s.params, visible: s.visible !== false,
      }))));
    } catch (e) {}
  }
  function stratShort(it) {
    if (it.id === "S020") return "VWAP " + ((it.params && it.params.line_tf) || "1m") + " " + ((it.params && it.params.variant) || "V0");
    if (it.id === "S020_RSI") return "RSI Div " + ((it.params && it.params.signal_tf) || "15m");
    const rec = stratCatalog.find((c) => c.id === it.id);
    return rec ? rec.short_name : it.id;
  }
  function paintStratLegend() {
    const host = el("stratLegend");
    if (!host) return;
    host.innerHTML = "";
    stratInstances.forEach((it) => {
      const row = document.createElement("div");
      row.className = "st-row" + (it.visible === false ? " hidden" : "");
      const name = document.createElement("span");
      name.className = "ind-name";
      name.textContent = stratShort(it);
      function icobtn(title, txt, fn) {
        const b = document.createElement("button");
        b.type = "button";
        b.title = title;
        b.textContent = txt;
        b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
        return b;
      }
      row.appendChild(name);
      row.appendChild(icobtn("Hide/show", it.visible === false ? "🙈" : "👁", () => {
        it.visible = it.visible === false;
        saveStrats();
        paintStratLegend();
        lastOverlaySigClose = -1;
        reloadOverlay(true);
      }));
      row.appendChild(icobtn("Settings", "⚙", () => {
        openRightPanel("paneStrat");
        renderStratSettings();
      }));
      row.appendChild(icobtn("Remove", "🗑", () => {
        stratInstances = stratInstances.filter((x) => x.uid !== it.uid);
        saveStrats();
        paintStratLegend();
        renderStratSettings();
        lastOverlaySigClose = -1;
        reloadOverlay(true);
      }));
      row.appendChild(icobtn("Duplicate", "⋯", () => {
        addStrat(it.id, JSON.parse(JSON.stringify(it.params || {})));
      }));
      host.appendChild(row);
    });
  }
  function renderField(f, params, onChange) {
    const row = document.createElement("div");
    row.className = "row";
    const lab = document.createElement("label");
    lab.textContent = f.label || f.key;
    row.appendChild(lab);
    const typ = f.type || "float";
    if (typ === "bool") {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = Boolean(params[f.key]);
      cb.addEventListener("change", () => { params[f.key] = cb.checked; onChange(); });
      row.appendChild(cb);
    } else if (typ === "select") {
      const sel = document.createElement("select");
      (f.options || []).forEach((op) => {
        const o = document.createElement("option");
        o.value = String(op.value != null ? op.value : op);
        o.textContent = String(op.label != null ? op.label : op);
        sel.appendChild(o);
      });
      sel.value = String(params[f.key]);
      sel.addEventListener("change", () => { params[f.key] = sel.value; onChange(); });
      row.appendChild(sel);
    } else {
      const inp = document.createElement("input");
      inp.type = "number";
      if (f.min != null) inp.min = String(f.min);
      if (f.max != null) inp.max = String(f.max);
      inp.step = String(f.step != null ? f.step : (typ === "int" ? 1 : 0.1));
      inp.value = params[f.key] != null ? params[f.key] : f.default;
      inp.addEventListener("change", () => {
        let n = typ === "int" ? parseInt(inp.value, 10) : parseFloat(inp.value);
        if (!Number.isFinite(n)) n = f.default;
        if (f.min != null) n = Math.max(Number(f.min), n);
        if (f.max != null) n = Math.min(Number(f.max), n);
        params[f.key] = n;
        inp.value = String(n);
        onChange();
      });
      row.appendChild(inp);
    }
    return row;
  }
  function renderStratSettings() {
    const host = el("stratSettingsHost");
    if (!host) return;
    host.innerHTML = "";
    if (!stratInstances.length) {
      host.textContent = "No strategy deployed. Open Indicators → Strategies.";
      return;
    }
    stratInstances.forEach((it) => {
      const rec = stratCatalog.find((c) => c.id === it.id) || { name: it.id, inputs: [] };
      const box = document.createElement("div");
      box.className = "st-set";
      const h = document.createElement("h2");
      h.textContent = rec.name + " · " + stratShort(it);
      box.appendChild(h);
      (rec.inputs || []).forEach((f) => {
        box.appendChild(renderField(f, it.params, () => {
          saveStrats();
          paintStratLegend();
          lastOverlaySigClose = -1;
          if (f.key === "signal_tf" && !historyMode && !replayOn) connectWs();
          reloadOverlay(true);
        }));
      });
      host.appendChild(box);
    });
  }
  function addStrat(id, params) {
    const rec = stratCatalog.find((c) => c.id === id);
    const it = {
      uid: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      id,
      params: params || defaultParamsFromSchema(rec ? rec.inputs : []),
      visible: true,
    };
    stratInstances.push(it);
    saveStrats();
    paintStratLegend();
    renderStratSettings();
    lastOverlaySigClose = -1;
    lastSigTfOpen = 0;
    reloadOverlay(true);
    if (id === "S020_RSI" && !historyMode && !replayOn) connectWs();
  }
  function renderStratCatalog(filter) {
    const list = el("stratList");
    if (!list) return;
    const q = String(filter || "").trim().toLowerCase();
    list.innerHTML = "";
    stratCatalog
      .filter((c) => !q || (c.name + " " + c.id + " " + (c.description || "")).toLowerCase().indexOf(q) >= 0)
      .forEach((c) => {
        const row = document.createElement("div");
        row.className = "ind-cat-row";
        const meta = document.createElement("div");
        meta.className = "ind-cat-meta";
        const nm = document.createElement("div");
        nm.className = "ind-cat-name";
        nm.textContent = c.name;
        const ds = document.createElement("div");
        ds.className = "ind-cat-desc";
        ds.textContent = c.description || "";
        meta.appendChild(nm);
        meta.appendChild(ds);
        row.appendChild(meta);
        row.addEventListener("click", () => {
          addStrat(c.id, defaultParamsFromSchema(c.inputs));
          el("indModal").classList.remove("open");
        });
        list.appendChild(row);
      });
  }
  function openRightPanel(paneId) {
    const layout = el("layout");
    const side = el("sidePanel");
    const cur = side && side.dataset.pane;
    if (layout.classList.contains("panel-open") && cur === paneId) {
      layout.classList.remove("panel-open");
      side.classList.add("closed");
      side.dataset.pane = "";
      document.querySelectorAll("#rightStrip button").forEach((b) => b.classList.remove("active"));
      try { lsSet(LS_PANEL, JSON.stringify({ open: false })); } catch (e) {}
      sizeCharts();
      return;
    }
    layout.classList.add("panel-open");
    side.classList.remove("closed");
    side.dataset.pane = paneId;
    document.querySelectorAll("#sidePanel .pane").forEach((p) => p.classList.toggle("show", p.id === paneId));
    document.querySelectorAll("#rightStrip button").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-pane") === paneId);
    });
    try { lsSet(LS_PANEL, JSON.stringify({ open: true, pane: paneId })); } catch (e) {}
    if (paneId === "paneStrat") renderStratSettings();
    sizeCharts();
  }
  function showReplayBar(on) {
    if (el("replayBar")) el("replayBar").classList.toggle("show", Boolean(on));
    if (el("btnReplay")) el("btnReplay").classList.toggle("active", Boolean(on));
  }
  function paintOhlcLegend(bar) {
    const box = el("legendOhlc");
    if (!box) return;
    const vis = visibleCandles();
    const b = bar || (vis.length ? vis[vis.length - 1] : null);
    const head = "BTCUSD · 1 · Delta Exchange India";
    if (!b) {
      box.textContent = head;
      return;
    }
    let prev = null;
    for (let i = vis.length - 2; i >= 0; i -= 1) {
      if (Number(vis[i].time) < Number(b.time)) { prev = vis[i]; break; }
    }
    const chg = prev ? Number(b.close) - Number(prev.close) : 0;
    const pct = prev && Number(prev.close) ? (chg / Number(prev.close)) * 100 : 0;
    const cls = chg >= 0 ? "up" : "dn";
    const sign = chg >= 0 ? "+" : "";
    box.innerHTML = head +
      "  O " + Number(b.open).toFixed(1) +
      "  H " + Number(b.high).toFixed(1) +
      "  L " + Number(b.low).toFixed(1) +
      "  C <span class=\"" + cls + "\">" + Number(b.close).toFixed(1) +
      "  " + sign + chg.toFixed(1) + " (" + sign + pct.toFixed(2) + "%)</span>";
  }
  function updateGoRtBtn() {
    const btn = el("btnGoRt");
    if (!btn) return;
    const vis = visibleCandles();
    if (!vis.length) { btn.classList.remove("show"); return; }
    const vr = chart.timeScale().getVisibleRange();
    if (!vr) { btn.classList.remove("show"); return; }
    const last = Number(vis[vis.length - 1].time);
    btn.classList.toggle("show", Number(vr.to) < last - barSecNow() * 2);
  }
  function jumpRange(key) {
    const newest = viewNewestOpen();
    if (newest == null) return;
    const now = newest;
    const d = new Date(now * 1000);
    let from = now - 86400;
    if (key === "5D") from = now - 5 * 86400;
    else if (key === "1M") from = now - 30 * 86400;
    else if (key === "3M") from = now - 90 * 86400;
    else if (key === "6M") from = now - 180 * 86400;
    else if (key === "1Y") from = now - 365 * 86400;
    else if (key === "YTD") {
      const y = new Date(new Date().toLocaleString("en-US", { timeZone: IST }));
      from = Math.floor(Date.UTC(y.getFullYear(), 0, 1) / 1000) - 19800;
    } else if (key === "All") {
      const oldest = oldestCandleTime();
      from = oldest != null ? oldest : now - 86400;
    }
    void loadAround(Math.floor((from + now) / 2)).then(() => {
      chart.timeScale().setVisibleRange({ from, to: now + barSecNow() * 5 });
    });
  }
  function tickClock() {
    const n = el("liveClock");
    if (!n) return;
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: IST, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date());
    n.textContent = parts + " UTC+5:30";
  }
  async function initShell() {
    try {
      const j = await fetch("/api/strategies").then((r) => r.json());
      stratCatalog = j.strategies || [];
    } catch (e) {
      stratCatalog = [];
    }
    try {
      const saved = JSON.parse(lsGet(LS_STRAT, "null"));
      if (Array.isArray(saved) && saved.length) stratInstances = saved;
    } catch (e) {}
    if (!stratInstances.length && stratCatalog.length) {
      const rec = stratCatalog.find((c) => c.id === "S020") || stratCatalog[0];
      stratInstances = [{
        uid: "sdef",
        id: rec.id,
        params: defaultParamsFromSchema(rec.inputs),
        visible: true,
      }];
      saveStrats();
    }
    paintStratLegend();
    try {
      const pan = JSON.parse(lsGet(LS_PANEL, "{}"));
      if (pan && pan.open && pan.pane) openRightPanel(pan.pane);
    } catch (e) {}
    document.querySelectorAll("#tfBar [data-tf]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const v = btn.getAttribute("data-tf");
        el("tf").value = v;
        document.querySelectorAll("#tfBar [data-tf]").forEach((b) => b.classList.toggle("active", b === btn));
        if (el("tfMore")) el("tfMore").value = "";
        el("tf").dispatchEvent(new Event("change"));
      });
    });
    if (el("tfMore")) {
      el("tfMore").addEventListener("change", () => {
        const v = el("tfMore").value;
        if (!v) return;
        el("tf").value = v;
        document.querySelectorAll("#tfBar [data-tf]").forEach((b) => b.classList.remove("active"));
        el("tf").dispatchEvent(new Event("change"));
      });
    }
    document.querySelectorAll("#rightStrip [data-pane]").forEach((btn) => {
      btn.addEventListener("click", () => openRightPanel(btn.getAttribute("data-pane")));
    });
    document.querySelectorAll("#rangeBar [data-range]").forEach((btn) => {
      btn.addEventListener("click", () => jumpRange(btn.getAttribute("data-range")));
    });
    if (el("btnReplay")) {
      el("btnReplay").addEventListener("click", () => {
        if (replayOn) {
          showReplayBar(true);
          return;
        }
        showReplayBar(true);
        replayPicking = true;
        setReplayButtons();
      });
    }
    if (el("btnReplayExit")) {
      el("btnReplayExit").addEventListener("click", () => {
        if (replayOn) void exitReplayLive();
        else {
          replayPicking = false;
          showReplayBar(false);
          setReplayButtons();
        }
      });
    }
    if (el("btnUndo")) el("btnUndo").addEventListener("click", () => { if (drawingsApi) drawingsApi.undo(); });
    if (el("btnRedo")) el("btnRedo").addEventListener("click", () => { if (drawingsApi) drawingsApi.redo(); });
    if (el("legendFold")) {
      el("legendFold").addEventListener("click", () => {
        el("legendBody").classList.toggle("collapsed");
      });
    }
    if (el("chartLegend")) {
      ["pointerdown", "click", "dblclick"].forEach((evn) => {
        el("chartLegend").addEventListener(evn, (ev) => ev.stopPropagation());
      });
    }
    if (el("btnGoRt")) {
      el("btnGoRt").addEventListener("click", () => {
        if (replayOn) {
          replayFollow = true;
          syncFollowBtn();
          centerReplayCursor();
          return;
        }
        const vis = visibleCandles();
        if (!vis.length) return;
        const lastIdx = vis.length - 1;
        const lr = chart.timeScale().getVisibleLogicalRange();
        const width = lr && Number.isFinite(Number(lr.to) - Number(lr.from))
          ? Math.max(20, Number(lr.to) - Number(lr.from))
          : 80;
        try {
          chart.timeScale().setVisibleLogicalRange({
            from: lastIdx - width + 10,
            to: lastIdx + 10,
          });
        } catch (e) {}
        try { chart.timeScale().scrollToPosition(0, false); } catch (e) {}
        try { chart.timeScale().scrollToRealTime(); } catch (e) {}
        requestAnimationFrame(() => updateGoRtBtn());
      });
    }
    if (el("btnGoto")) {
      el("btnGoto").addEventListener("click", () => el("gotoModal").classList.add("open"));
    }
    if (el("gotoCancel")) {
      el("gotoCancel").addEventListener("click", () => el("gotoModal").classList.remove("open"));
    }
    if (el("catTabInd")) {
      el("catTabInd").addEventListener("click", () => {
        el("catTabInd").classList.add("active");
        el("catTabStrat").classList.remove("active");
        el("indList").hidden = false;
        el("stratList").hidden = true;
      });
    }
    if (el("catTabStrat")) {
      el("catTabStrat").addEventListener("click", () => {
        el("catTabStrat").classList.add("active");
        el("catTabInd").classList.remove("active");
        el("indList").hidden = true;
        el("stratList").hidden = false;
        renderStratCatalog(el("indSearch") ? el("indSearch").value : "");
      });
    }
    if (el("indSearch")) {
      el("indSearch").addEventListener("input", () => {
        if (el("stratList") && !el("stratList").hidden) renderStratCatalog(el("indSearch").value);
      });
    }
    if (el("btnIndicators")) {
      el("btnIndicators").addEventListener("click", () => {
        renderStratCatalog("");
      });
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => updateGoRtBtn());
    tickClock();
    setInterval(tickClock, 1000);
  }

  loadFileList();
  initAlertUi();
  initShell().then(() => loadInitial().then(connectWs));

  window.addEventListener("ca-cmd", (ev) => {
    const d = ev.detail || {};
    if (d.cmd === "start" && d.ts != null) void startReplayAt(Number(d.ts));
    if (d.cmd === "step") void replayStep();
    if (d.cmd === "follow") {
      replayFollow = true;
      syncFollowBtn();
      centerReplayCursor();
    }
    if (d.cmd === "shift") {
      const lr = chart.timeScale().getVisibleLogicalRange();
      if (lr) {
        const delta = Number(d.delta) || -20;
        chart.timeScale().setVisibleLogicalRange({
          from: Number(lr.from) + delta,
          to: Number(lr.to) + delta,
        });
      }
    }
    if (d.cmd === "zoom") {
      const lr = chart.timeScale().getVisibleLogicalRange();
      if (lr) {
        const mid = (Number(lr.from) + Number(lr.to)) / 2;
        const half = Math.max(8, (Number(lr.to) - Number(lr.from)) / 4);
        chart.timeScale().setVisibleLogicalRange({ from: mid - half, to: mid + half });
      }
    }
    if (d.cmd === "draw" && drawingsApi) {
      const item = d.item || {};
      drawingsApi.add(item);
      window.__caDraw = drawingsApi.list();
    }
    if (d.cmd === "drawList") {
      window.__caDraw = drawingsApi ? drawingsApi.list() : [];
    }
    if (d.cmd === "xh" && d.time != null) {
      const bar = candleMap.get(Number(d.time));
      const px = bar ? Number(bar.close) : 0;
      if (typeof chart.setCrosshairPosition === "function") {
        chart.setCrosshairPosition(px, Number(d.time), series);
      }
    }
    if (d.cmd === "snap") {
      const vis = visibleCandles();
      const last = vis.length ? vis[vis.length - 1] : null;
      const lr = chart.timeScale().getVisibleLogicalRange();
      const rr = paneCharts[0] ? paneCharts[0].chart.timeScale().getVisibleLogicalRange() : null;
      window.__caSnap = {
        n: vis.length,
        lastClose: last ? last.close : null,
        lastTime: last ? last.time : null,
        lastIst: last ? fmtIst(last.time) : null,
        lr,
        rr,
        replayBarOpen,
        replayFollow,
        mid: lr ? (Number(lr.from) + Number(lr.to)) / 2 : null,
        lastIdx: vis.length ? vis.length - 1 : null,
        xAlign: (function () {
          if (!last || !paneCharts[0]) return null;
          const x = chart.timeScale().timeToCoordinate(last.time);
          const tM = chart.timeScale().coordinateToTime(x);
          const tR = paneCharts[0].chart.timeScale().coordinateToTime(x);
          return { x, tM, tR };
        })(),
      };
    }
  });
})();
