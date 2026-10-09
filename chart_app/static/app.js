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
    layout: { background: { color: "#0e1117" }, textColor: "#8b949e" },
    grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
    rightPriceScale: { borderColor: "#30363d", minimumWidth: PRICE_SCALE_W },
    timeScale: {
      borderColor: "#30363d",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 0,
      shiftVisibleRangeOnNewBar: false,
      tickMarkFormatter: (t) => {
        const d = new Date(t * 1000);
        return d.toLocaleString("en-IN", { timeZone: IST, hour12: false, hour: "2-digit", minute: "2-digit" });
      },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { timeFormatter: (t) => fmtIst(t) },
  });
  const series = chart.addCandlestickSeries({
    upColor: "#3fb950",
    downColor: "#f85149",
    borderVisible: false,
    wickUpColor: "#3fb950",
    wickDownColor: "#f85149",
    lastValueVisible: true,
    priceLineVisible: true,
  });
  const vwapSeries = chart.addLineSeries({ color: "#58a6ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  const extraSeries = [];
  const rsiHost = el("rsiChart");
  const rsiChart = rsiHost
    ? LightweightCharts.createChart(rsiHost, {
        layout: { background: { color: "#0e1117" }, textColor: "#8b949e" },
        grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
        rightPriceScale: { borderColor: "#30363d", minimumWidth: PRICE_SCALE_W },
        timeScale: { visible: false, rightOffset: 0, shiftVisibleRangeOnNewBar: false },
        height: 130,
      })
    : null;
  const rsiLine = rsiChart
    ? rsiChart.addLineSeries({ color: "#d2a8ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
    : null;
  const rsiObLine = rsiChart
    ? rsiChart.addLineSeries({ color: "#f85149", lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false })
    : null;
  const rsiOsLine = rsiChart
    ? rsiChart.addLineSeries({ color: "#3fb950", lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false })
    : null;
  function rsiPaneOn() {
    return Boolean(rsiHost && rsiChart && !rsiHost.classList.contains("hidden"));
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
      if (rsiPaneOn()) rsiChart.timeScale().setVisibleLogicalRange(range);
    } catch (e) {}
    syncingTs = false;
    requestAnimationFrame(() => { applyingRange = false; });
  }
  function pushRangePeer(fromChart, range) {
    if (syncingTs || !range) return;
    if (replayOn && replayFollow && fromChart === "rsi") return;
    syncingTs = true;
    try {
      if (fromChart === "main" && rsiPaneOn()) rsiChart.timeScale().setVisibleLogicalRange(range);
      if (fromChart === "rsi") chart.timeScale().setVisibleLogicalRange(range);
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
  if (rsiChart) {
    rsiChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      pushRangePeer("rsi", range);
    });
  }
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
  function sigTfSec() {
    const v = el("signalTf") ? el("signalTf").value : "15m";
    return RES_SEC[v] || 900;
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
    series.setData(visibleCandles());
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
    box.textContent =
      "Candles: " + n + " | from " + fromTxt + " | to " + toTxt + " | " + mode + " | " + phase;
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

  function rsiValueAt(time) {
    const t = Number(time);
    const pts = overlayPayload.rsi || [];
    for (let i = 0; i < pts.length; i += 1) {
      if (Number(pts[i].time) === t && Number.isFinite(Number(pts[i].value))) return Number(pts[i].value);
    }
    return 50;
  }
  function syncCrosshairToRsi(param) {
    if (!rsiPaneOn() || !rsiLine || typeof rsiChart.setCrosshairPosition !== "function") return;
    if (!param || param.time == null) {
      if (typeof rsiChart.clearCrosshairPosition === "function") rsiChart.clearCrosshairPosition();
      return;
    }
    rsiChart.setCrosshairPosition(rsiValueAt(param.time), param.time, rsiLine);
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
    const box = el("ohlc");
    const tip = el("markTip");
    if (!param || !param.time || !param.seriesData) {
      box.textContent = "OHLC —";
      if (tip) tip.textContent = "";
      if (!syncingXh) {
        syncingXh = true;
        try { syncCrosshairToRsi(param); } catch (e) {}
        syncingXh = false;
      }
      return;
    }
    const d = param.seriesData.get(series);
    if (!d) {
      box.textContent = fmtIst(param.time);
    } else {
      box.innerHTML =
        "<span>" + fmtIst(param.time) + "</span>" +
        "<span>O " + d.open.toFixed(1) + "</span>" +
        "<span>H " + d.high.toFixed(1) + "</span>" +
        "<span>L " + d.low.toFixed(1) + "</span>" +
        "<span>C " + d.close.toFixed(1) + "</span>";
    }
    if (tip) tip.textContent = markerTooltip(param.time);
    if (!syncingXh) {
      syncingXh = true;
      try { syncCrosshairToRsi(param); } catch (e) {}
      syncingXh = false;
    }
  });
  if (rsiChart) {
    rsiChart.subscribeCrosshairMove((param) => {
      if (syncingXh) return;
      syncingXh = true;
      try { syncCrosshairToMain(param); } catch (e) {}
      syncingXh = false;
    });
  }

  function clearExtra() {
    extraSeries.forEach((s) => chart.removeSeries(s));
    extraSeries.length = 0;
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
      if (sigClose <= lastOverlaySigClose) return;
    }
    const hours = el("hours").value || "24";
    const lineTf = el("lineTf").value;
    const variant = el("variant").value;
    const strategy = el("strategy") ? el("strategy").value : "S020";
    const q = new URLSearchParams({
      strategy,
      tf,
      line_tf: lineTf,
      variant,
      from: String(from),
      to: String(to),
      hours: String(hours),
      signal_tf: el("signalTf") ? el("signalTf").value : "15m",
      rsi_len: el("rsiLen") ? el("rsiLen").value : "14",
      ob: el("rsiOb") ? el("rsiOb").value : "70",
      os: el("rsiOs") ? el("rsiOs").value : "30",
      exp_obh: el("rsiExpObh") ? el("rsiExpObh").value : "40",
      exp_obl: el("rsiExpObl") ? el("rsiExpObl").value : "60",
      show_obh: el("togObh") && el("togObh").checked ? "true" : "false",
      show_obl: el("togObl") && el("togObl").checked ? "true" : "false",
      show_levels: el("togLevels") && el("togLevels").checked ? "true" : "false",
      show_signals: el("togSigs") && el("togSigs").checked ? "true" : "false",
    });
    const gen = ++overlayGen;
    const r = await fetch("/api/overlay?" + q.toString());
    const j = await r.json();
    if (gen !== overlayGen) return;
    overlayPayload = j;
    lastOverlaySigClose = completedSigClose(cursorClose);
    paintOverlay(j);
    if (replayOn) ingestReplaySignals(j);
    else scanLiveAlerts(j);
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
    for (const t of times) {
      while (i + 1 < src.length && Number(src[i + 1].time) <= t) i += 1;
      if (Number(src[i].time) <= t) out.push({ time: t, value: src[i].value });
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
    const showVwap = el("togVwap").checked;
    const showActive = el("togActive").checked;
    const showExpired = el("togExpired").checked;
    const showMarks = el("togMarks").checked;
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
    const mode = el("markerMode") ? el("markerMode").value : "raw";
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
    const useRsi = el("strategy") && el("strategy").value === "S020_RSI" && el("togRsiPane") && el("togRsiPane").checked;
    if (rsiHost) {
      rsiHost.classList.toggle("hidden", !useRsi);
      document.getElementById("layout").style.gridTemplateRows = useRsi
        ? "48px 1fr 132px 28px"
        : "48px 1fr 0px 28px";
      sizeCharts();
    }
    if (rsiLine && rsiObLine && rsiOsLine) {
      const candles = visibleCandles();
      const rsiByT = new Map();
      (j.rsi || []).forEach((p) => {
        const t = Number(p.time);
        const v = Number(p.value);
        if (!Number.isFinite(t) || !Number.isFinite(v)) return;
        if (cursorClose != null && t > cursorClose) return;
        rsiByT.set(t, v);
      });
      const rsiPts = useRsi
        ? candles.map((c) => {
            const t = Number(c.time);
            return rsiByT.has(t) ? { time: t, value: rsiByT.get(t) } : { time: t };
          })
        : [];
      rsiLine.setData(rsiPts);
      const ob = Number(j.rsi_ob != null ? j.rsi_ob : 70);
      const os = Number(j.rsi_os != null ? j.rsi_os : 30);
      rsiObLine.setData(useRsi ? candles.map((c) => ({ time: Number(c.time), value: ob })) : []);
      rsiOsLine.setData(useRsi ? candles.map((c) => ({ time: Number(c.time), value: os })) : []);
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
  }

  ["togVwap", "togActive", "togExpired", "togMarks", "togObh", "togObl", "togLevels", "togSigs", "togRsiPane"].forEach((id) => {
    if (el(id)) el(id).addEventListener("change", () => reloadOverlay(true));
  });
  el("btnOverlay").addEventListener("click", () => reloadOverlay(true));
  el("lineTf").addEventListener("change", () => reloadOverlay(true));
  el("variant").addEventListener("change", () => reloadOverlay(true));
  el("hours").addEventListener("change", () => reloadOverlay(true));
  if (el("strategy")) el("strategy").addEventListener("change", () => { lastOverlaySigClose = -1; reloadOverlay(true); });
  ["signalTf", "rsiLen", "rsiOb", "rsiOs", "rsiExpObh", "rsiExpObl"].forEach((id) => {
    if (el(id)) {
      el(id).addEventListener("change", () => {
        lastOverlaySigClose = -1;
        lastSigTfOpen = 0;
        reloadOverlay(true);
        if (id === "signalTf" && !historyMode && !replayOn) connectWs();
      });
    }
  });
  if (el("markerMode")) {
    el("markerMode").addEventListener("change", () => paintOverlay(overlayPayload));
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
    const s = el("strategy") ? el("strategy").value : "S020";
    return s === "S020_RSI" ? "RSI Div" : "VWAP";
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
    const tfLab = opts.tf || (el("signalTf") ? el("signalTf").value : "15m");
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
    const st = el("strategy") ? el("strategy").value : "S020";
    const sigTf = el("signalTf") ? el("signalTf").value : "15m";
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
    if (btn) btn.textContent = alertsOn ? "🔔 Alerts ON" : "🔔 Alerts";
    const skip = el("togAlertSkipped");
    if (skip) skip.checked = lsGet("chart_app.alertsSkipped", "0") === "1";
  }
  function initAlertUi() {
    alertsOn = lsGet("chart_app.alertsOn", "0") === "1";
    syncAlertUi();
    renderAlertLog();
    const banner = el("alertBanner");
    if (banner) banner.addEventListener("click", hideAlertBanner);
    if (el("btnAlerts")) {
      el("btnAlerts").addEventListener("click", async () => {
        alertsOn = !alertsOn;
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
          tf: el("signalTf") ? el("signalTf").value : "15m",
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
      const sigTf = el("signalTf") ? el("signalTf").value : "15m";
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
      const sigTf = el("signalTf") ? el("signalTf").value : "15m";
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
      el("btnReplayPlay").textContent = replayPlaying ? "Pause" : "Play";
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
    if (el("markerMode")) el("markerMode").value = "backtest";
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
    if (rsiChart && rsiHost) {
      rsiChart.applyOptions({ width: rsiHost.clientWidth, height: rsiHost.clientHeight || 130 });
    }
  }
  window.addEventListener("resize", () => sizeCharts());
  sizeCharts();

  loadFileList();
  initAlertUi();
  loadInitial().then(connectWs);

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
      const rr = rsiChart ? rsiChart.timeScale().getVisibleLogicalRange() : null;
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
          if (!last || !rsiChart) return null;
          const x = chart.timeScale().timeToCoordinate(last.time);
          const tM = chart.timeScale().coordinateToTime(x);
          const tR = rsiChart.timeScale().coordinateToTime(x);
          return { x, tM, tR };
        })(),
      };
    }
  });
})();
