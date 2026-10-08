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
    "1h": "candlestick_1h",
    "4h": "candlestick_4h",
  };
  const RES_SEC = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400 };

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

  const chart = LightweightCharts.createChart(el("chart"), {
    layout: { background: { color: "#0e1117" }, textColor: "#8b949e" },
    grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
    rightPriceScale: { borderColor: "#30363d" },
    timeScale: {
      borderColor: "#30363d",
      timeVisible: true,
      secondsVisible: false,
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
  });
  const vwapSeries = chart.addLineSeries({ color: "#58a6ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  const extraSeries = [];
  const rsiHost = el("rsiChart");
  const rsiChart = rsiHost
    ? LightweightCharts.createChart(rsiHost, {
        layout: { background: { color: "#0e1117" }, textColor: "#8b949e" },
        grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
        rightPriceScale: { borderColor: "#30363d" },
        timeScale: { visible: false },
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
  if (chart && rsiChart && chart.timeScale && rsiChart.timeScale) {
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range) rsiChart.timeScale().setVisibleLogicalRange(range);
    });
  }

  function sortedCandles() {
    return [...candleMap.values()].sort((a, b) => a.time - b.time);
  }
  function applyCandles() {
    series.setData(sortedCandles());
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
    const mode = historyMode ? "HISTORY" : "LIVE";
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
    if (historyMode && newest != null && range.to >= newest - 20 * barSec) {
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

  chart.subscribeCrosshairMove((param) => {
    const box = el("ohlc");
    const tip = el("markTip");
    if (!param || !param.time || !param.seriesData) {
      box.textContent = "OHLC —";
      if (tip) tip.textContent = "";
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
  });

  function clearExtra() {
    extraSeries.forEach((s) => chart.removeSeries(s));
    extraSeries.length = 0;
  }

  async function reloadOverlay() {
    const times = [...candleMap.keys()];
    if (!times.length) return;
    const from = Math.min(...times);
    const to = Math.max(...times);
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
    const r = await fetch("/api/overlay?" + q.toString());
    const j = await r.json();
    overlayPayload = j;
    paintOverlay(j);
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
    const newest = newestCandleTime();
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
    }
    if (rsiLine && rsiObLine && rsiOsLine) {
      const rsiPts = useRsi ? (j.rsi || []) : [];
      rsiLine.setData(rsiPts);
      const ob = Number(j.rsi_ob != null ? j.rsi_ob : 70);
      const os = Number(j.rsi_os != null ? j.rsi_os : 30);
      const times = rsiPts.map((p) => p.time);
      rsiObLine.setData(times.length ? times.map((t) => ({ time: t, value: ob })) : []);
      rsiOsLine.setData(times.length ? times.map((t) => ({ time: t, value: os })) : []);
    }
  }

  ["togVwap", "togActive", "togExpired", "togMarks", "togObh", "togObl", "togLevels", "togSigs", "togRsiPane"].forEach((id) => {
    if (el(id)) el(id).addEventListener("change", () => reloadOverlay());
  });
  el("btnOverlay").addEventListener("click", () => reloadOverlay());
  el("lineTf").addEventListener("change", () => reloadOverlay());
  el("variant").addEventListener("change", () => reloadOverlay());
  el("hours").addEventListener("change", () => reloadOverlay());
  if (el("strategy")) el("strategy").addEventListener("change", () => reloadOverlay());
  ["signalTf", "rsiLen", "rsiOb", "rsiOs", "rsiExpObh", "rsiExpObl"].forEach((id) => {
    if (el(id)) el(id).addEventListener("change", () => reloadOverlay());
  });
  if (el("markerMode")) {
    el("markerMode").addEventListener("change", () => paintOverlay(overlayPayload));
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
    if (historyMode) return;
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
    const res = RES_SEC[tf] || 60;
    if (lastBarClose && t > lastBarClose) {
      if (overlayTimer) clearTimeout(overlayTimer);
      overlayTimer = setTimeout(() => reloadOverlay(), 400);
    }
    lastBarClose = t;
    void res;
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
    if (historyMode) return;
    setStatus("reconnecting");
    try {
      if (ws) ws.close();
    } catch (e) {}
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      backoff = 1000;
      setStatus("live");
      const ch = WS_CH[tf] || "candlestick_1m";
      ws.send(JSON.stringify({ type: "subscribe", payload: { channels: [{ name: ch, symbols: [SYMBOL] }] } }));
      ws.send(JSON.stringify({ type: "enable_heartbeat" }));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      const typ = String(msg.type || "");
      if (typ.startsWith("candlestick_")) applyLiveBar(msg);
    };
    ws.onclose = () => {
      if (historyMode) return;
      setStatus("reconnecting");
      scheduleReconnect();
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    if (historyMode) return;
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

  el("tf").addEventListener("change", async () => {
    tf = el("tf").value;
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

  window.addEventListener("resize", () => chart.applyOptions({ width: el("chart").clientWidth, height: el("chart").clientHeight }));
  chart.applyOptions({ width: el("chart").clientWidth, height: el("chart").clientHeight });

  loadFileList();
  loadInitial().then(connectWs);
})();
