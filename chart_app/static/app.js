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
  let overlayPayload = { lines: [], markers: [] };
  let tradeMarks = [];
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

  async function fetchCandles(end, limit) {
    const q = new URLSearchParams({ tf, limit: String(limit || 1500) });
    if (end) q.set("end", String(end));
    const r = await fetch("/api/candles?" + q.toString());
    const j = await r.json();
    return j.candles || [];
  }

  async function loadInitial(end) {
    candleMap = new Map();
    const rows = await fetchCandles(end, 1500);
    rows.forEach((c) => candleMap.set(c.time, c));
    applyCandles();
    if (rows.length) chart.timeScale().scrollToRealTime();
    await reloadOverlay();
  }

  async function loadOlder() {
    if (loadingLeft || candleMap.size === 0) return;
    const oldest = Math.min(...candleMap.keys());
    loadingLeft = true;
    try {
      const rows = await fetchCandles(oldest - 1, 1500);
      let n = 0;
      rows.forEach((c) => {
        if (!candleMap.has(c.time)) {
          candleMap.set(c.time, c);
          n += 1;
        }
      });
      if (n) applyCandles();
    } finally {
      loadingLeft = false;
    }
  }

  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (!range || loadingLeft) return;
    if (range.from < 8) loadOlder();
  });

  chart.subscribeCrosshairMove((param) => {
    const box = el("ohlc");
    if (!param || !param.time || !param.seriesData) {
      box.textContent = "OHLC —";
      return;
    }
    const d = param.seriesData.get(series);
    if (!d) {
      box.textContent = fmtIst(param.time);
      return;
    }
    box.innerHTML =
      "<span>" + fmtIst(param.time) + "</span>" +
      "<span>O " + d.open.toFixed(1) + "</span>" +
      "<span>H " + d.high.toFixed(1) + "</span>" +
      "<span>L " + d.low.toFixed(1) + "</span>" +
      "<span>C " + d.close.toFixed(1) + "</span>";
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
    const q = new URLSearchParams({
      strategy: "S020",
      tf,
      line_tf: lineTf,
      variant,
      from: String(from),
      to: String(to),
      hours: String(hours),
    });
    const r = await fetch("/api/overlay?" + q.toString());
    const j = await r.json();
    overlayPayload = j;
    paintOverlay(j);
  }

  function paintOverlay(j) {
    const showVwap = el("togVwap").checked;
    const showActive = el("togActive").checked;
    const showExpired = el("togExpired").checked;
    const showMarks = el("togMarks").checked;
    const vwap = (j.lines || []).find((x) => x.kind === "vwap");
    vwapSeries.applyOptions({ visible: showVwap });
    vwapSeries.setData(showVwap && vwap ? vwap.points : []);
    clearExtra();
    (j.lines || []).forEach((ln) => {
      if (ln.kind === "vwap") return;
      if (ln.active && !showActive) return;
      if (!ln.active && !showExpired) return;
      const color = ln.kind === "high"
        ? (ln.active ? "#f85149" : "rgba(248,81,73,0.35)")
        : (ln.active ? "#3fb950" : "rgba(63,185,80,0.35)");
      const s = chart.addLineSeries({
        color,
        lineWidth: ln.active ? 2 : 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      s.setData(ln.points || []);
      extraSeries.push(s);
    });
    const marks = showMarks
      ? (j.markers || []).map((m) => ({
          time: m.time,
          position: m.side === "long" ? "belowBar" : "aboveBar",
          color: m.side === "long" ? "#3fb950" : "#f85149",
          shape: m.side === "long" ? "arrowUp" : "arrowDown",
          text: m.text || "",
        }))
      : [];
    series.setMarkers(marks.concat(tradeMarks));
  }

  ["togVwap", "togActive", "togExpired", "togMarks"].forEach((id) => {
    el(id).addEventListener("change", () => reloadOverlay());
  });
  el("btnOverlay").addEventListener("click", () => reloadOverlay());
  el("lineTf").addEventListener("change", () => reloadOverlay());
  el("variant").addEventListener("change", () => reloadOverlay());
  el("hours").addEventListener("change", () => reloadOverlay());

  function wsCandleTime(raw) {
    let t = Number(raw.candle_start_time != null ? raw.candle_start_time : raw.time);
    if (!Number.isFinite(t)) return null;
    if (t > 1e12) t = Math.floor(t / 1e9);
    return t;
  }

  function applyLiveBar(raw) {
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
    if (prev) series.update(bar);
    else series.update(bar);
    const res = RES_SEC[tf] || 60;
    if (lastBarClose && t > lastBarClose) {
      if (overlayTimer) clearTimeout(overlayTimer);
      overlayTimer = setTimeout(() => reloadOverlay(), 400);
    }
    lastBarClose = t;
    void res;
  }

  function connectWs() {
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
      setStatus("reconnecting");
      scheduleReconnect();
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    if (wsTimer) clearTimeout(wsTimer);
    wsTimer = setTimeout(connectWs, backoff);
    backoff = Math.min(backoff * 2, 15000);
  }

  el("tf").addEventListener("change", async () => {
    tf = el("tf").value;
    await loadInitial();
    connectWs();
  });

  el("btnJump").addEventListener("click", async () => {
    const v = el("jump").value;
    if (!v) return;
    const ms = new Date(v + "+05:30").getTime();
    if (!Number.isFinite(ms)) return;
    const end = Math.floor(ms / 1000);
    await loadInitial(end);
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
        color: t.side === "long" ? "#3fb950" : "#f85149",
        shape: "circle",
        text: "E " + (t.net != null ? Number(t.net).toFixed(0) : ""),
      });
      if (t.exit_ts) {
        tradeMarks.push({
          time: t.exit_ts,
          position: "inBar",
          color: "#d29922",
          shape: "square",
          text: (t.reason || "X").slice(0, 3),
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
