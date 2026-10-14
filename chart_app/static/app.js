/* Delta India BTCUSD chart. History via local API; live via public WS candlestick_{tf}. */
(function () {
  const IST = "Asia/Kolkata";
  const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/live";
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
  let lastOverlayTo = null;
  let lastOverlayTf = "";
  let lastOverlayMs = 0;
  let lastOverlayCacheHit = false;
  let lastIndMs = 0;
  let lastIndCacheHit = false;
  let replaySigs = [];
  let overlayGen = 0;
  let lastSigTfOpen = 0;
  let audioCtx = null;
  let alertBannerTimer = null;
  const pageLoadTs = Math.floor(Date.now() / 1000);
  let alertsOn = false;
  let replayFollow = true;
  let applyingRange = false;
  let syncingTs = false;
  let syncingXh = false;
  let resizing = false;
  let resizeRaf = 0;
  let overlayAbort = null;
  let overlayDebounce = null;
  let indDebounce = null;
  let overlayBusy = false;
  let lastStepMs = 0;
  let subCount = 0;
  let lastTickLatency = null;
  let lastTickChannel = "";
  let wsConnected = false;
  let wsNeedGapFill = false;
  let liveNowOverride = null;

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
  const vwapSegSeries = [];
  let lastVwapPts = null;
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
      entry.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
        subCount += 1;
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
  let m1Map = new Map();
  const view = window.createViewController({
    chart: chart,
    host: el("chart"),
    getPaneCharts: function () { return paneList().map((p) => p.chart); },
    getBarSec: function () { return RES_SEC[tf] || 60; },
    getCursorTime: function () { return replayBarOpen; },
    getLastTime: function () {
      const vis = visibleCandles();
      return vis.length ? Number(vis[vis.length - 1].time) : null;
    },
    getBarCount: function () { return visibleCandles().length; },
    getOldestTime: function () { return oldestCandleTime(); },
    isReplay: function () { return replayOn; },
    isFollow: function () { return replayFollow; },
    isHistory: function () { return historyMode; },
    isLoading: function () { return loadingLeft || loadingRight || resizing || applyingRange; },
    onFollowOff: function () {
      replayFollow = false;
      syncFollowBtn();
    },
    onNeedOlder: function () { void loadOlder(); },
    onNeedNewer: function () { void loadNewer(); },
    onLogical: function () {
      subCount += 1;
      updateGoRtBtn();
    },
    onUserRange: function () { updateGoRtBtn(); },
  });
  window.__caView = view;
  function centerReplayCursor() {
    if (!replayOn || replayBarOpen == null) return;
    view.request("follow");
  }
  function viewModeSync() {
    if (replayOn) view.request("set-mode", { mode: replayPlaying ? "REPLAY_PLAYING" : "REPLAY_PAUSED" });
    else if (historyMode) view.request("set-mode", { mode: "HISTORY" });
    else view.request("set-mode", { mode: "LIVE" });
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
    return Number(replayBarOpen) + 60;
  }
  function maxShowTime() {
    if (replayOn && replayBarOpen != null) return Number(replayBarOpen);
    return null;
  }
  function canShowTime(t) {
    const cap = maxShowTime();
    if (cap == null) return true;
    return Number(t) <= cap;
  }
  function viewNewestOpen() {
    if (replayOn && replayBarOpen != null) return Number(replayBarOpen);
    return newestCandleTime();
  }
  function visibleCandles() {
    const all = sortedCandles();
    const cap = maxShowTime();
    if (cap == null) return all;
    return all.filter((c) => Number(c.time) <= cap);
  }
  function leakCheck(tag) {
    const vis = visibleCandles();
    const last = vis.length ? Number(vis[vis.length - 1].time) : null;
    const cap = maxShowTime();
    const leak = cap != null && last != null && last > cap;
    window.__caLeak = { tag: tag || "", cursor: cap, last: last, leak: leak, n: vis.length, lastIst: last != null ? fmtIst(last) : null };
    if (leak) console.warn("REPLAY FUTURE LEAK", window.__caLeak);
    return window.__caLeak;
  }
  function applyCandles(mode) {
    const vis = visibleCandles();
    const last = vis.length ? vis[vis.length - 1] : null;
    if (mode === "step" && last && vis.length > 1) {
      try { series.update(last); }
      catch (e) { series.setData(vis); }
    } else {
      series.setData(vis);
    }
    if (last) {
      const up = Number(last.close) >= Number(last.open);
      series.applyOptions({
        priceLineColor: up ? "#089981" : "#f23645",
        lastValueVisible: true,
        priceLineVisible: true,
      });
    }
    if (drawingsApi) drawingsApi.refresh();
    if (indApi && mode !== "step") indApi.refresh();
    paintOhlcLegend(last);
    updateGoRtBtn();
    leakCheck("applyCandles");
    if (mode !== "step") view.request("after-data", { reason: mode || "setData" });
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
      dot.classList.add("off");
      lab.textContent = "disconnected";
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

  async function fetchCandlesTf(resTf, opts) {
    const q = new URLSearchParams({ tf: resTf, limit: String((opts && opts.limit) || 1500) });
    let start = opts && opts.start != null ? Number(opts.start) : null;
    let end = opts && opts.end != null ? Number(opts.end) : null;
    const cap = maxShowTime();
    if (replayOn && cap != null && !(opts && opts.allowAhead)) {
      if (end == null || end > cap) end = cap;
      if (start != null && start > cap) {
        return { candles: [], reached_now: false, first: null, last: null };
      }
    }
    if (start != null) q.set("start", String(start));
    if (end != null) q.set("end", String(end));
    const r = await fetch("/api/candles?" + q.toString());
    return r.json();
  }
  async function fetchCandles(opts) {
    return fetchCandlesTf(tf, opts);
  }
  function mergeM1(rows) {
    let n = 0;
    (rows || []).forEach((c) => {
      const t = Number(c && c.time);
      if (!Number.isFinite(t) || t < 1e9) return;
      if (!m1Map.has(t)) n += 1;
      m1Map.set(t, c);
    });
    return n;
  }
  function bucketOpen(t, sec) {
    return Math.floor(Number(t) / sec) * sec;
  }
  function aggregateM1(capOpen, tfSec) {
    const buckets = new Map();
    const cap = Number(capOpen);
    m1Map.forEach((c) => {
      const ts = Number(c.time);
      if (!Number.isFinite(ts) || ts > cap) return;
      const b = bucketOpen(ts, tfSec);
      let o = buckets.get(b);
      if (!o) {
        buckets.set(b, {
          time: b,
          open: Number(c.open),
          high: Number(c.high),
          low: Number(c.low),
          close: Number(c.close),
          volume: Number(c.volume || 0),
        });
      } else {
        o.high = Math.max(o.high, Number(c.high));
        o.low = Math.min(o.low, Number(c.low));
        o.close = Number(c.close);
        o.volume += Number(c.volume || 0);
      }
    });
    return [...buckets.values()].sort((a, b) => a.time - b.time);
  }
  function rebuildReplayCandles(applyMode) {
    if (!replayOn || replayBarOpen == null) return;
    const rows = aggregateM1(replayBarOpen, barSecNow());
    candleMap = new Map();
    rows.forEach((c) => candleMap.set(c.time, c));
    applyCandles(applyMode);
  }
  function last1mOfBar(barOpen, barSec) {
    const sec = Number(barSec) || 60;
    const t = Number(barOpen);
    if (sec <= 60) return t;
    return t + sec - 60;
  }

  async function loadInitial(opts) {
    candleMap = new Map();
    historyEnd = false;
    historyMode = false;
    loadingLeft = true;
    updateHistStatus();
    try {
      const j = await fetchCandles({ limit: 1500 });
      mergeRows(j.candles || []);
      applyCandles();
      if (!(opts && opts.keepView) && !replayOn && candleMap.size) view.request("jump-live");
      await new Promise((r) => requestAnimationFrame(r));
      await reloadOverlay();
      view.request("sync-panes");
    } finally {
      loadingLeft = false;
      updateHistStatus();
    }
  }

  async function loadAround(centerTs, opts) {
    candleMap = new Map();
    historyEnd = false;
    loadingLeft = true;
    loadingRight = true;
    clearExtra();
    vwapSeries.setData([]);
    series.setMarkers([]);
    updateHistStatus();
    const barSec = RES_SEC[tf] || 60;
    const noFwd = Boolean(replayOn || (opts && opts.noForward));
    applyingRange = true;
    try {
      const back = await fetchCandles({ end: centerTs, limit: 1500 });
      mergeRows(back.candles || []);
      if (!noFwd) {
        const fwd = await fetchCandles({ start: centerTs, limit: 750 });
        mergeRows(fwd.candles || []);
        if (fwd.reached_now) historyMode = false;
      }
      applyCandles();
      view.request("goto", { center: centerTs });
      if (noFwd) view.request("tf-change", { center: centerTs });
      await reloadOverlay(true);
    } finally {
      loadingLeft = false;
      loadingRight = false;
      applyingRange = false;
      updateHistStatus();
    }
  }

  async function loadOlder() {
    if (loadingLeft || historyEnd || candleMap.size === 0) return;
    const oldest = oldestCandleTime();
    if (oldest == null) return;
    loadingLeft = true;
    view.request("load-older-start");
    updateHistStatus();
    try {
      if (replayOn) {
        let oldest1 = null;
        m1Map.forEach((c) => {
          const t = Number(c.time);
          if (oldest1 == null || t < oldest1) oldest1 = t;
        });
        const j = await fetchCandlesTf("1m", { end: (oldest1 != null ? oldest1 : oldest) - 1, limit: 1500 });
        const n = mergeM1(j.candles || []);
        if (n === 0) historyEnd = true;
        else {
          rebuildReplayCandles();
          await reloadOverlay();
        }
      } else {
        const j = await fetchCandles({ end: oldest - 1, limit: 1500 });
        const n = mergeRows(j.candles || []);
        if (n === 0) historyEnd = true;
        else {
          applyCandles();
          await reloadOverlay();
        }
      }
    } finally {
      view.request("load-older-end");
      loadingLeft = false;
      updateHistStatus();
    }
  }

  async function loadNewer() {
    if (replayOn) return;
    if (!historyMode || loadingRight || candleMap.size === 0) return;
    const newest = newestCandleTime();
    if (newest == null) return;
    const barSec = RES_SEC[tf] || 60;
    loadingRight = true;
    view.request("load-newer-start");
    updateHistStatus();
    try {
      const j = await fetchCandles({ start: newest + barSec, limit: 1500 });
      mergeRows(j.candles || []);
      applyCandles();
      await reloadOverlay();
      if (j.reached_now) {
        historyMode = false;
        viewModeSync();
        updateHistStatus();
        connectWs();
      }
    } finally {
      view.request("load-newer-end");
      loadingRight = false;
      updateHistStatus();
    }
  }

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

  function overlayFetchTo() {
    if (!replayOn || replayBarOpen == null) {
      const vis = visibleCandles();
      return vis.length ? Number(vis[vis.length - 1].time) : null;
    }
    let mx = Number(replayBarOpen);
    m1Map.forEach(function (c) {
      const t = Number(c.time);
      if (t > mx) mx = t;
    });
    return mx;
  }
  function indicatorRange() {
    const vis = visibleCandles();
    if (!vis.length) return null;
    const cap = maxShowTime();
    const to = replayOn ? overlayFetchTo() : (cap != null ? cap : Number(vis[vis.length - 1].time));
    return { from: Number(vis[0].time), to: to };
  }

  function setLegendLoading(on) {
    const body = el("legendBody");
    if (body) body.classList.toggle("loading", Boolean(on));
  }
  function scheduleOverlay(force) {
    if (overlayDebounce) clearTimeout(overlayDebounce);
    const wait = replayPlaying ? 140 : (force ? 0 : 40);
    overlayDebounce = setTimeout(() => {
      overlayDebounce = null;
      void reloadOverlay(Boolean(force));
    }, wait);
  }
  function scheduleIndicators() {
    if (indDebounce) clearTimeout(indDebounce);
    const wait = replayPlaying ? 140 : 40;
    indDebounce = setTimeout(() => {
      indDebounce = null;
      if (indApi) void indApi.reload();
    }, wait);
  }
  async function reloadOverlay(force) {
    const vis = visibleCandles();
    if (!vis.length) return;
    const from = Number(vis[0].time);
    const capOpen = viewNewestOpen();
    const cursorClose = replayOn && replayBarOpen != null
      ? replayCursorClose()
      : (capOpen != null ? Number(capOpen) + barSecNow() : Number(vis[vis.length - 1].time));
    const paintTo = replayOn && replayBarOpen != null
      ? Number(replayBarOpen)
      : (capOpen != null ? capOpen : Number(vis[vis.length - 1].time));
    const to = replayOn ? (overlayFetchTo() || paintTo) : paintTo;
    if (replayOn && !force && lastOverlayTo != null && Number(to) <= Number(lastOverlayTo) && lastOverlayTf === tf) {
      paintOverlay(overlayPayload);
      if (indApi && typeof indApi.applyCap === "function") indApi.applyCap();
      return;
    }
    const gen = ++overlayGen;
    if (overlayAbort) {
      try { overlayAbort.abort(); } catch (e) {}
    }
    overlayAbort = typeof AbortController === "function" ? new AbortController() : null;
    const signal = overlayAbort ? overlayAbort.signal : undefined;
    const visible = stratInstances.filter((s) => s.visible !== false);
    overlayBusy = true;
    setLegendLoading(true);
    try {
      if (!visible.length) {
        overlayPayload = { lines: [], markers: [] };
        lastOverlaySigClose = completedSigClose(cursorClose);
        lastOverlayTo = to;
        lastOverlayTf = tf;
        paintOverlay(overlayPayload);
        scheduleIndicators();
        return;
      }
      const tFetch = performance.now();
      let maxServer = 0;
      let allHit = true;
      await Promise.all(visible.map(async (it) => {
        const q = new URLSearchParams({
          strategy: it.id,
          tf,
          from: String(from),
          to: String(to),
          params: JSON.stringify(it.params || {}),
        });
        const r = await fetch("/api/overlay?" + q.toString(), signal ? { signal: signal } : {});
        const j = await r.json();
        it._payload = j;
        const sms = Number(j && j.server_ms);
        if (Number.isFinite(sms) && sms > maxServer) maxServer = sms;
        if (!(j && j.cache_hit)) allHit = false;
      }));
      lastOverlayMs = maxServer > 0 ? maxServer : (performance.now() - tFetch);
      lastOverlayCacheHit = visible.length > 0 && allHit;
      if (gen !== overlayGen) return;
      overlayPayload = mergeStratPayloads();
      lastOverlaySigClose = completedSigClose(cursorClose);
      lastOverlayTo = to;
      lastOverlayTf = tf;
      paintOverlay(overlayPayload);
      if (replayOn) ingestReplaySignals(overlayPayload);
      else scanLiveAlerts(overlayPayload);
      scheduleIndicators();
    } catch (err) {
      if (err && err.name === "AbortError") return;
      console.warn("overlay reload failed", err);
    } finally {
      if (gen === overlayGen) {
        overlayBusy = false;
        setLegendLoading(false);
      }
    }
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
    const times = visibleCandles()
      .map((c) => c.time)
      .filter((t) => t >= oldest && (newest == null || t <= newest) && canShowTime(t));
    if (!times.length) return src;
    const out = [];
    let i = 0;
    let prevDay = null;
    for (const t of times) {
      const day = Math.floor(Number(t) / 86400);
      while (i + 1 < src.length && Number(src[i + 1].time) <= t) i += 1;
      if (prevDay != null && day !== prevDay) {
        out.push({ time: Number(t) - 1 });
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

  function splitLineSegs(pts) {
    const segs = [];
    let cur = [];
    (pts || []).forEach(function (p) {
      const v = p && p.value;
      if (p == null || p.time == null || v == null || v === "" || !Number.isFinite(Number(v))) {
        if (cur.length) segs.push(cur);
        cur = [];
        return;
      }
      cur.push({ time: Number(p.time), value: Number(v) });
    });
    if (cur.length) segs.push(cur);
    return segs;
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
    const vwapKeep = vwapPts.filter((p) => p.time == null || canShowTime(p.time));
    const vwapSegs = splitLineSegs(vwapKeep);
    const v0 = vwapSegs[0] || [];
    let usedVwapUpdate = false;
    if (lastVwapPts && v0.length && lastVwapPts.length) {
      const last = v0[v0.length - 1];
      const prevLast = lastVwapPts[lastVwapPts.length - 1];
      if (last && last.value != null && prevLast && Number(prevLast.time) <= Number(last.time)
          && (v0.length === lastVwapPts.length || v0.length === lastVwapPts.length + 1)) {
        try { vwapSeries.update(last); usedVwapUpdate = true; } catch (e) { usedVwapUpdate = false; }
      }
    }
    if (!usedVwapUpdate) vwapSeries.setData(v0);
    lastVwapPts = v0;
    const extraNeed = Math.max(0, vwapSegs.length - 1);
    while (vwapSegSeries.length > extraNeed) {
      const s = vwapSegSeries.pop();
      try { chart.removeSeries(s); } catch (e) {}
    }
    for (let i = 1; i < vwapSegs.length; i += 1) {
      if (!vwapSegSeries[i - 1]) {
        vwapSegSeries[i - 1] = chart.addLineSeries({
          color: "#58a6ff",
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
        });
      }
      vwapSegSeries[i - 1].applyOptions({ visible: showVwap });
      vwapSegSeries[i - 1].setData(vwapSegs[i]);
    }
    const wanted = [];
    (j.lines || []).forEach((ln) => {
      if (ln.kind === "vwap") return;
      if (ln.active && !showActive) return;
      if (!ln.active && !showExpired) return;
      const pts = oldest != null ? clipLinePoints(ln, oldest, newest) : (ln.points || []);
      const capped = pts.filter((p) => p.time == null || canShowTime(p.time));
      if (capped.length < 2) return;
      const color = ln.kind === "high"
        ? (ln.active ? "#ef4444" : "rgba(239,68,68,0.5)")
        : (ln.active ? "#22c55e" : "rgba(34,197,94,0.5)");
      wanted.push({ pts: capped, color, width: ln.active ? 2 : 1 });
    });
    while (extraSeries.length > wanted.length) {
      const s = extraSeries.pop();
      try { chart.removeSeries(s); } catch (e) {}
    }
    wanted.forEach((w, i) => {
      if (!extraSeries[i]) {
        extraSeries[i] = chart.addLineSeries({
          color: w.color,
          lineWidth: w.width,
          priceLineVisible: false,
          lastValueVisible: false,
        });
      } else {
        extraSeries[i].applyOptions({ color: w.color, lineWidth: w.width });
      }
      extraSeries[i].setData(w.pts);
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
          const pending = Boolean(m.pending) || m.reason === "pending_bar";
          const skipped = Boolean(m.reason) || m.entry_allowed === false;
          const longish = m.text === "OBL" || m.text === "LONG" || m.side === "long";
          const hot = longish ? "#22c55e" : "#ef4444";
          const faded = longish ? "rgba(34,197,94,0.4)" : "rgba(239,68,68,0.4)";
          return {
            time: snap(m.time),
            position: longish ? "belowBar" : "aboveBar",
            color: (skipped || pending) ? faded : hot,
            shape: pending ? "circle" : (longish ? "arrowUp" : "arrowDown"),
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
        const rsiSrc = (j.rsi || [])
          .map((p) => ({ t: Number(p.time), v: Number(p.value) }))
          .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v) && canShowTime(p.t))
          .sort((a, b) => a.t - b.t);
        let ri = 0;
        let lastR = null;
        const rsiPts = candles.map((c) => {
          const t = Number(c.time);
          while (ri < rsiSrc.length && rsiSrc[ri].t <= t) {
            lastR = rsiSrc[ri].v;
            ri += 1;
          }
          return lastR != null ? { time: t, value: lastR } : { time: t };
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
        view.request("indicator");
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
      if (m.pending || m.reason === "pending_bar") return;
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

  function nowSec() {
    if (liveNowOverride != null) return Number(liveNowOverride);
    return Math.floor(Date.now() / 1000);
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
    applyLiveCandle(bar);
  }

  function applyLiveTick(price, ts, meta) {
    if (historyMode || replayOn) return;
    const px = Number(price);
    if (!Number.isFinite(px) || px <= 0) return;
    const t = Number(ts) || nowSec();
    const sec = barSecNow();
    const open = bucketOpen(t, sec);
    let bar = candleMap.get(open);
    if (!bar) {
      bar = { time: open, open: px, high: px, low: px, close: px, volume: 0 };
      applyLiveCandle(bar, false);
    } else {
      bar.high = Math.max(Number(bar.high), px);
      bar.low = Math.min(Number(bar.low), px);
      bar.close = px;
      applyLiveCandle(bar, true);
    }
    if (meta) {
      if (meta.latency_ms != null) lastTickLatency = Number(meta.latency_ms);
      if (meta.channel) lastTickChannel = String(meta.channel);
    }
    placeCountdown();
  }
  window.__caTick = applyLiveTick;

  function applyLiveCandle(bar, sameBar) {
    const t = Number(bar.time);
    candleMap.set(t, bar);
    try { series.update(bar); }
    catch (e) {
      try { series.setData(visibleCandles()); }
      catch (e2) {}
    }
    paintOhlcLegend(bar);
    updateHistStatus();
    if (!sameBar && lastBarClose && t > lastBarClose) {
      scheduleLiveOverlayOnSigClose(lastBarClose + barSecNow());
      if (indApi) void indApi.reload();
    }
    lastBarClose = t;
  }

  async function fillLiveGap() {
    if (historyMode || replayOn) return;
    const last = newestCandleTime();
    if (last == null) return;
    try {
      const j = await fetchCandles({ start: last, limit: 500 });
      mergeRows(j.candles || []);
      applyCandles();
      if (indApi) void indApi.reload();
      await reloadOverlay(true);
    } catch (e) {}
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
    wsConnected = false;
    setStatus("off");
    const lab = el("statusLabel");
    if (lab && (historyMode || replayOn)) lab.textContent = "history";
  }

  function connectWs() {
    if (historyMode || replayOn) return;
    setStatus("off");
    try {
      if (ws) ws.close();
    } catch (e) {}
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      backoff = 1000;
      wsConnected = true;
      setStatus("live");
      try { ws.send(JSON.stringify({ cmd: "status" })); } catch (e) {}
      if (wsNeedGapFill) {
        wsNeedGapFill = false;
        void fillLiveGap();
      }
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      const typ = String(msg.type || "");
      if (typ === "status") {
        wsConnected = Boolean(msg.connected);
        setStatus(wsConnected ? "live" : "off");
        if (msg.latency_ms != null) lastTickLatency = Number(msg.latency_ms);
        if (msg.channel) lastTickChannel = String(msg.channel);
        return;
      }
      if (typ === "tick") {
        applyLiveTick(msg.price, msg.ts, msg);
        return;
      }
      const chartCh = WS_CH[tf] || "candlestick_1m";
      const sigTf = sigTfValue();
      const sigCh = WS_CH[sigTf];
      if (typ === chartCh) applyLiveBar(msg);
      else if (sigCh && typ === sigCh) onSigTfCandle(msg);
    };
    ws.onclose = () => {
      wsConnected = false;
      wsNeedGapFill = true;
      if (historyMode || replayOn) return;
      setStatus("off");
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
      if (m.pending || m.reason === "pending_bar") return;
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
    const cap = Number(replayBarOpen);
    const nextT = cap + 60;
    if (m1Map.has(nextT)) return m1Map.get(nextT);
    let nxt = null;
    m1Map.forEach((c) => {
      const t = Number(c.time);
      if (t > cap && (nxt == null || t < Number(nxt.time))) nxt = c;
    });
    if (nxt) return nxt;
    const j = await fetchCandlesTf("1m", { start: nextT, end: nextT, limit: 1, allowAhead: true });
    mergeM1(j.candles || []);
    return m1Map.get(nextT) || null;
  }

  async function startReplayAt(openTs) {
    const want = Number(openTs);
    if (!Number.isFinite(want)) return;
    stopReplayTimer();
    historyMode = true;
    disconnectWs();
    replayOn = true;
    replayPicking = false;
    replayFollow = true;
    replaySigs = [];
    lastOverlaySigClose = -1;
    if (overlayTimer) {
      clearTimeout(overlayTimer);
      overlayTimer = null;
    }
    replayBarOpen = last1mOfBar(want, barSecNow());
    m1Map = new Map();
    candleMap = new Map();
    applyingRange = true;
    viewModeSync();
    try {
      const back = await fetchCandlesTf("1m", { end: replayBarOpen, limit: 1500 });
      mergeM1(back.candles || []);
      rebuildReplayCandles();
    } finally {
      applyingRange = false;
    }
    showReplayBar(true);
    setReplayButtons();
    updateHistStatus();
    renderReplayStats();
    syncFollowBtn();
    view.request("replay-start");
    try {
      const ahead = await fetchCandlesTf("1m", {
        start: Number(replayBarOpen) + 60,
        limit: 1500,
        allowAhead: true,
      });
      mergeM1(ahead.candles || []);
    } catch (e) {}
    if (indApi && typeof indApi.invalidate === "function") indApi.invalidate();
    await reloadOverlay(true);
  }

  async function replayStep() {
    if (!replayOn || replayBarOpen == null) return;
    const t0 = performance.now();
    const nxt = await ensureNextBar();
    if (!nxt) {
      stopReplayTimer();
      setReplayButtons();
      viewModeSync();
      return;
    }
    replayBarOpen = Number(nxt.time);
    applyingRange = true;
    view.request("replay-step-before");
    rebuildReplayCandles("step");
    view.request("mute", { ms: 320 });
    paintOverlay(overlayPayload);
    if (indApi && typeof indApi.applyCap === "function") indApi.applyCap();
    updateHistStatus();
    renderReplayStats();
    const needFetch = lastOverlayTo == null || Number(replayBarOpen) > Number(lastOverlayTo) || lastOverlayTf !== tf;
    if (needFetch) scheduleOverlay(false);
    else if (indApi && typeof indApi.applyCap === "function") indApi.applyCap();
    view.request("replay-step");
    lastStepMs = performance.now() - t0;
    window.__caStepMs = lastStepMs;
    requestAnimationFrame(() => {
      view.request("replay-step");
      applyingRange = false;
    });
    viewModeSync();
  }

  function replayPlayLoop() {
    if (!replayOn || !replayPlaying) return;
    const spd = Number(el("replaySpeed") ? el("replaySpeed").value : 1);
    const ms = REPLAY_MS[spd] || 900;
    replayTimer = setTimeout(() => {
      void replayStep().then(() => {
        if (replayPlaying) replayPlayLoop();
      });
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
    m1Map = new Map();
    showReplayBar(false);
    setReplayButtons();
    renderReplayStats();
    viewModeSync();
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
      view.request("follow");
    });
  }
  if (el("btnReplayLive")) el("btnReplayLive").addEventListener("click", () => exitReplayLive());

  el("tf").addEventListener("change", async () => {
    tf = el("tf").value;
    const savedCenter = jumpCenterTime();
    view.request("capture");
    if (replayOn) {
      disconnectWs();
      lastOverlaySigClose = -1;
      applyingRange = true;
      rebuildReplayCandles();
      applyingRange = false;
      await reloadOverlay(true);
      view.request("tf-change", { center: replayBarOpen });
      setReplayButtons();
      return;
    }
    if (historyMode) {
      disconnectWs();
      await loadAround(savedCenter);
      view.request("tf-change", { center: savedCenter });
    } else {
      await loadInitial({ keepView: true });
      view.request("tf-change", { center: savedCenter });
      connectWs();
    }
  });

  el("btnJump").addEventListener("click", async () => {
    const v = el("jump").value;
    if (!v) return;
    const ms = new Date(v + "+05:30").getTime();
    if (!Number.isFinite(ms)) return;
    const ts = Math.floor(ms / 1000);
    if (el("gotoModal")) el("gotoModal").classList.remove("open");
    if (replayOn) {
      stopReplayTimer();
      replayBarOpen = bucketOpen(ts, 60);
      applyingRange = true;
      const need = await fetchCandlesTf("1m", { end: replayBarOpen, limit: 1500 });
      mergeM1(need.candles || []);
      rebuildReplayCandles();
      applyingRange = false;
      viewModeSync();
      await reloadOverlay(true);
      view.request("replay-start");
      return;
    }
    historyMode = true;
    viewModeSync();
    disconnectWs();
    await loadAround(ts);
    view.request("goto", { center: ts });
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
        view.request("trade-range", { from, to });
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
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      resizing = true;
      applyingRange = true;
      const cw = el("chart").clientWidth;
      const chgt = el("chart").clientHeight;
      const panes = paneCharts.map((p) => {
        const host = p.host || el("chart");
        const h = Math.max(60, (host.clientHeight || 132) - 6);
        return { chart: p.chart, width: host.clientWidth, height: h };
      });
      view.request("resize", { width: cw, height: chgt, panes: panes });
      leakCheck("resize");
      requestAnimationFrame(() => {
        applyingRange = false;
        resizing = false;
      });
    });
  }
  window.addEventListener("resize", () => sizeCharts());
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => sizeCharts());
    if (el("mid")) ro.observe(el("mid"));
    if (el("chart")) ro.observe(el("chart"));
    if (el("paneHost")) ro.observe(el("paneHost"));
  }
  sizeCharts();
  if (typeof window.createChartDrawings === "function") {
    drawingsApi = window.createChartDrawings({
      chart: chart,
      series: series,
      host: el("chart"),
      getCandles: function () { return visibleCandles(); },
      getTfSec: function () { return RES_SEC[tf] || 60; },
      isReplayPicking: function () { return replayPicking; },
      getMarkPrice: function () {
        const vis = visibleCandles();
        if (!vis.length) return null;
        return Number(vis[vis.length - 1].close);
      },
      getPaneCharts: function () { return paneList().map((p) => p.chart); },
      onViewRequest: function (ev) { view.request(ev); },
    });
  }
  if (typeof window.createChartIndicators === "function") {
    indApi = window.createChartIndicators({
      chart: chart,
      series: series,
      getTf: function () { return tf; },
      getRange: function () { return indicatorRange(); },
      getCapTime: function () { return maxShowTime(); },
      paneHost: el("paneHost"),
      priceScaleW: PRICE_SCALE_W,
      registerPane: upsertPane,
      unregisterPane: dropPane,
      onResize: function () { sizeCharts(); },
      resizeChart: function (ch, w, h) { view.resizeChart(ch, w, h); },
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
      onIndTiming: function (ms, hit) {
        lastIndMs = Number(ms) || 0;
        lastIndCacheHit = Boolean(hit);
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
      view.request("trade-range", { from, to: now + barSecNow() * 5 });
    });
  }
  function tickClock() {
    const n = el("liveClock");
    if (n) {
      const parts = new Intl.DateTimeFormat("en-GB", {
        timeZone: IST, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      }).format(new Date());
      n.textContent = parts + " UTC+5:30";
    }
    placeCountdown();
  }
  function placeCountdown() {
    const box = el("barCountdown");
    if (!box) return;
    if (replayOn || historyMode) {
      box.hidden = true;
      return;
    }
    const last = newestCandleTime();
    const vis = last != null ? candleMap.get(last) : null;
    const sec = barSecNow();
    const now = nowSec();
    const open = last != null ? Number(last) : bucketOpen(now, sec);
    const remain = Math.max(0, open + sec - now);
    const mm = String(Math.floor(remain / 60)).padStart(2, "0");
    const ss = String(remain % 60).padStart(2, "0");
    box.textContent = mm + ":" + ss;
    box.hidden = false;
    if (vis) {
      try {
        const y = series.priceToCoordinate(Number(vis.close));
        if (y != null) box.style.top = Math.max(8, Number(y) + 16) + "px";
      } catch (e) {}
    }
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
          view.request("follow");
          return;
        }
        view.request("jump-live");
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
    if (d.cmd === "live") void exitReplayLive();
    if (d.cmd === "step") void replayStep();
    if (d.cmd === "play") {
      if (!replayOn || replayPlaying) return;
      replayPlaying = true;
      setReplayButtons();
      viewModeSync();
      replayPlayLoop();
    }
    if (d.cmd === "pause") {
      stopReplayTimer();
      setReplayButtons();
      viewModeSync();
    }
    if (d.cmd === "follow") {
      replayFollow = d.on !== false;
      syncFollowBtn();
      if (replayFollow) view.request("follow");
    }
    if (d.cmd === "followOff") {
      replayFollow = false;
      syncFollowBtn();
      view.request("pin-view");
    }
    if (d.cmd === "shift") view.request("shift", { delta: d.delta });
    if (d.cmd === "zoom") view.request("zoom");
    if (d.cmd === "tf" && d.tf) {
      el("tf").value = d.tf;
      document.querySelectorAll("#tfBar [data-tf]").forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-tf") === d.tf);
      });
      el("tf").dispatchEvent(new Event("change"));
    }
    if (d.cmd === "panel") openRightPanel(d.pane || "paneTrades");
    if (d.cmd === "size") sizeCharts();
    if (d.cmd === "addInd" && indApi && indApi.add) void indApi.add(d.id || "ULP");
    if (d.cmd === "removeInd" && indApi && indApi.removeLast) indApi.removeLast();
    if (d.cmd === "addStrat") addStrat(d.id || "S020_RSI");
    if (d.cmd === "overlayReload") void reloadOverlay(true);
    if (d.cmd === "injectTick") {
      liveNowOverride = d.ts != null ? Number(d.ts) : liveNowOverride;
      applyLiveTick(d.price, d.ts, { latency_ms: 0, channel: "mock" });
    }
    if (d.cmd === "now") {
      liveNowOverride = d.ts != null ? Number(d.ts) : null;
      placeCountdown();
    }
    if (d.cmd === "draw" && drawingsApi) {
      const item = d.item || {};
      drawingsApi.add(item);
      window.__caDraw = drawingsApi.list();
      view.request("drawing");
    }
    if (d.cmd === "drawList") {
      window.__caDraw = drawingsApi ? drawingsApi.list() : [];
    }
    if (d.cmd === "setTool" && drawingsApi && d.tool) drawingsApi.setTool(d.tool);
    if (d.cmd === "xh" && d.time != null) {
      const bar = candleMap.get(Number(d.time));
      const px = bar ? Number(bar.close) : 0;
      if (typeof chart.setCrosshairPosition === "function") {
        chart.setCrosshairPosition(px, Number(d.time), series);
      }
    }
    if (d.cmd === "snap" || d.cmd === "test") {
      const vis = visibleCandles();
      const last = vis.length ? vis[vis.length - 1] : null;
      const lr = chart.timeScale().getVisibleLogicalRange();
      const vr = chart.timeScale().getVisibleRange();
      let barSpacing = null;
      try { barSpacing = chart.timeScale().options().barSpacing; } catch (e) {}
      const rr = paneCharts[0] ? paneCharts[0].chart.timeScale().getVisibleLogicalRange() : null;
      const host = el("chart");
      const cx = host ? host.clientWidth / 2 : 0;
      let tCenter = null;
      try { tCenter = chart.timeScale().coordinateToTime(cx); } catch (e) {}
      const snap = {
        n: vis.length,
        tf,
        lastClose: last ? last.close : null,
        lastTime: last ? last.time : null,
        lastIst: last ? fmtIst(last.time) : null,
        lr,
        vr,
        rr,
        barSpacing,
        viewLog: typeof view.getLog === "function" ? view.getLog().slice(-12) : [],
        build: (function () {
          const n = document.getElementById("buildVer");
          return n ? n.textContent : "";
        })(),
        replayBarOpen,
        replayFollow,
        replayOn,
        replayPlaying,
        historyMode,
        mode: view.mode(),
        ws: Boolean(ws) && wsConnected,
        wsConnected,
        tickLatency: lastTickLatency,
        tickChannel: lastTickChannel,
        countdown: (function () {
          const n = document.getElementById("barCountdown");
          return n && !n.hidden ? n.textContent : "";
        })(),
        seriesMax: (function () {
          const cap = maxShowTime();
          const out = { cap: cap, leak: false, items: {} };
          function add(name, pts) {
            let mx = null;
            (pts || []).forEach(function (p) {
              if (!p || p.time == null) return;
              if (p.value == null || p.value === "") return;
              const t = Number(p.time);
              if (cap != null && t > cap) return;
              if (mx == null || t > mx) mx = t;
            });
            out.items[name] = mx;
          }
          add("candles", vis);
          (overlayPayload.lines || []).forEach(function (ln) { add(ln.id || ln.kind, ln.points); });
          add("overlayRsi", overlayPayload.rsi);
          if (indApi && typeof indApi.maxTimes === "function") {
            const mt = indApi.maxTimes();
            Object.keys(mt).forEach(function (k) {
              out.items[k] = mt[k].max;
              if (mt[k].max != null && cap != null && mt[k].max > cap) out.leak = true;
            });
          }
          let allMax = null;
          Object.keys(out.items).forEach(function (k) {
            const v = out.items[k];
            if (v == null) return;
            if (allMax == null || v > allMax) allMax = v;
          });
          out.max = allMax;
          if (cap != null && allMax != null && allMax > cap) out.leak = true;
          return out;
        })(),
        mid: vr ? (Number(vr.from) + Number(vr.to)) / 2 : null,
        tCenter: tCenter != null ? Number(tCenter) : null,
        lastIdx: vis.length ? vis.length - 1 : null,
        leak: leakCheck("snap"),
        stepMs: lastStepMs,
        overlayMs: lastOverlayMs,
        overlayCacheHit: lastOverlayCacheHit,
        indMs: lastIndMs,
        indCacheHit: lastIndCacheHit,
        confirmedSigs: (overlayPayload.markers || []).filter(function (m) {
          const t = String(m.text || "");
          return (t === "LONG" || t === "SHORT") && !m.pending && m.reason !== "pending_bar";
        }).map(function (m) {
          return {
            time: Number(m.time),
            closeTs: Number(m.signal_close_ts != null ? m.signal_close_ts : m.time),
            side: String(m.side || ""),
            text: String(m.text || ""),
            pending: false,
          };
        }),
        pendingSigs: (overlayPayload.markers || []).filter(function (m) {
          return m.pending || m.reason === "pending_bar";
        }).length,
        subs: subCount,
        m1n: m1Map.size,
        lastPartial: last && replayOn ? last : null,
        xAlign: (function () {
          if (!last || !paneCharts[0]) return null;
          const x = chart.timeScale().timeToCoordinate(last.time);
          const tM = chart.timeScale().coordinateToTime(x);
          const tR = paneCharts[0].chart.timeScale().coordinateToTime(x);
          return { x, tM, tR };
        })(),
      };
      window.__caSnap = snap;
      window.__caTest = snap;
    }
  });
})();
