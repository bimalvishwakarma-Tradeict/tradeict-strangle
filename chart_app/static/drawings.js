/* Chart drawings via Lightweight Charts v4.2.0 series.attachPrimitive. Time+price coords. */
(function (global) {
  const LS_KEY = "chart_app.drawings.BTCUSD";
  const LS_STYLES = "chart_app.drawStyles.BTCUSD";
  const COLORS = ["#58a6ff", "#3fb950", "#f85149", "#d29922", "#d2a8ff"];
  const HIT = 8;
  const MINTICK = 0.5;
  const FIB_SPEC = [
    { v: 0, on: true, color: "#787b86" },
    { v: 0.236, on: true, color: "#f23645" },
    { v: 0.382, on: true, color: "#ff9800" },
    { v: 0.5, on: true, color: "#4caf50" },
    { v: 0.618, on: true, color: "#089981" },
    { v: 0.702, on: true, color: "#26c6da" },
    { v: 0.786, on: true, color: "#2962ff" },
    { v: 1, on: true, color: "#787b86" },
    { v: 1.272, on: false, color: "#e91e63" },
    { v: 1.414, on: false, color: "#9c27b0" },
    { v: 2, on: false, color: "#673ab7" },
    { v: 2.272, on: false, color: "#3f51b5" },
    { v: 2.414, on: false, color: "#2196f3" },
    { v: 2.618, on: false, color: "#03a9f4" },
    { v: 3, on: false, color: "#009688" },
    { v: 3.272, on: false, color: "#8bc34a" },
    { v: 3.414, on: false, color: "#cddc39" },
    { v: 3.618, on: false, color: "#ffeb3b" },
    { v: 4, on: false, color: "#ffc107" },
    { v: 4.236, on: false, color: "#ff9800" },
    { v: 4.272, on: false, color: "#ff7043" },
    { v: 4.414, on: false, color: "#ff5722" },
    { v: 4.618, on: false, color: "#795548" },
    { v: 4.764, on: false, color: "#9e9e9e" },
  ];
  function defaultFibLevels() {
    return FIB_SPEC.map(function (x) {
      return { v: x.v, on: x.on, color: x.color };
    });
  }
  function fibPrice(p1, p2, level) {
    return Number(p2) + (Number(p1) - Number(p2)) * Number(level);
  }

  function uid() {
    return "d" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }
  function lsGet(key, fb) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fb : v;
    } catch (e) {
      return fb;
    }
  }
  function lsSet(key, val) {
    try {
      localStorage.setItem(key, val);
    } catch (e) {}
  }
  function dist(ax, ay, bx, by) {
    return Math.hypot(ax - bx, ay - by);
  }
  function distSeg(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const l2 = dx * dx + dy * dy;
    if (l2 < 1e-9) return dist(px, py, x1, y1);
    let t = ((px - x1) * dx + (py - y1) * dy) / l2;
    t = Math.max(0, Math.min(1, t));
    return dist(px, py, x1 + t * dx, y1 + t * dy);
  }
  function rrRatio(entry, target, sl) {
    const risk = Math.abs(Number(entry) - Number(sl));
    const reward = Math.abs(Number(target) - Number(entry));
    if (!(risk > 0)) return null;
    return reward / risk;
  }
  function pad2(n) {
    return String(n).padStart(2, "0");
  }
  function fmtSpanMin(totalMin) {
    const m = Math.max(0, Math.round(Math.abs(Number(totalMin) || 0)));
    if (m < 60) return m + "m";
    if (m < 1440) {
      const h = Math.floor(m / 60);
      const mm = m % 60;
      return mm ? h + "h " + mm + "m" : h + "h";
    }
    const d = Math.floor(m / 1440);
    const rem = m % 1440;
    const h = Math.floor(rem / 60);
    const mm = rem % 60;
    let s = d + "d";
    if (h) s += " " + h + "h";
    if (mm) s += " " + mm + "m";
    return s;
  }
  function measureBox(top, bottom, tLeft, tRight, tfSec) {
    const pts = Math.abs(Number(top) - Number(bottom));
    const bot = Math.min(Number(top), Number(bottom));
    const pct = bot > 0 ? (pts / bot) * 100 : 0;
    const sec = Number(tfSec) > 0 ? Number(tfSec) : 60;
    const bars = Math.round(Math.abs(Number(tRight) - Number(tLeft)) / sec);
    return pts.toFixed(1) + " pts (" + pct.toFixed(2) + "%) | " + bars + " bars | " + fmtSpanMin((bars * sec) / 60);
  }
  function measureTrend(p1, p2, t1, t2, tfSec) {
    const dp = Number(p2) - Number(p1);
    const pts = Math.abs(dp);
    const base = Math.abs(Number(p1)) > 0 ? Number(p1) : 1;
    const pct = (dp / base) * 100;
    const sign = dp >= 0 ? "+" : "-";
    const pctSign = pct >= 0 ? "+" : "";
    const sec = Number(tfSec) > 0 ? Number(tfSec) : 60;
    const bars = Math.round(Math.abs(Number(t2) - Number(t1)) / sec);
    return sign + pts.toFixed(1) + " pts (" + pctSign + pct.toFixed(2) + "%) | " + bars + " bars | " + fmtSpanMin((bars * sec) / 60);
  }
  function fmtIstDt(unix) {
    const d = new Date(Number(unix) * 1000);
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(d);
    const g = {};
    parts.forEach(function (p) { g[p.type] = p.value; });
    return g.day + "-" + g.month + "-" + g.year + " " + g.hour + ":" + g.minute;
  }
  function parseIstDt(s) {
    const m = String(s || "").trim().match(/^(\d{1,2})-(\d{1,2})-(\d{4})\s+(\d{1,2}):(\d{2})$/);
    if (!m) return null;
    const iso = m[3] + "-" + pad2(m[2]) + "-" + pad2(m[1]) + "T" + pad2(m[4]) + ":" + m[5] + ":00+05:30";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return null;
    return Math.floor(t / 1000);
  }
  function snapTf(unix, tfSec) {
    const sec = Number(tfSec) > 0 ? Number(tfSec) : 60;
    return Math.floor(Number(unix) / sec) * sec;
  }
  function posValid(type, entry, target, sl) {
    const e = Number(entry);
    const t = Number(target);
    const s = Number(sl);
    if (![e, t, s].every(Number.isFinite)) return false;
    if (type === "long") return t > e && e > s;
    if (type === "short") return t < e && e < s;
    return true;
  }

  function PriceLabel(getY, getText, getColor) {
    this._y = 0;
    this._text = "";
    this._color = COLORS[0];
    this._getY = getY;
    this._getText = getText;
    this._getColor = getColor;
  }
  PriceLabel.prototype.update = function () {
    const y = this._getY();
    this._y = y == null ? NaN : y;
    this._text = this._getText();
    this._color = this._getColor();
  };
  PriceLabel.prototype.coordinate = function () {
    return this._y;
  };
  PriceLabel.prototype.text = function () {
    return this._text;
  };
  PriceLabel.prototype.textColor = function () {
    return "#0e1117";
  };
  PriceLabel.prototype.backColor = function () {
    return this._color;
  };
  PriceLabel.prototype.visible = function () {
    return Number.isFinite(this._y);
  };

  function DrawPrimitive(api) {
    this.api = api;
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
    this._priceViews = [];
    const self = this;
    this._paneView = {
      zOrder: function () {
        return "top";
      },
      renderer: function () {
        return {
          draw: function (target) {
            self._draw(target);
          },
        };
      },
    };
  }
  DrawPrimitive.prototype.attached = function (p) {
    this._chart = p.chart;
    this._series = p.series;
    this._requestUpdate = p.requestUpdate;
  };
  DrawPrimitive.prototype.detached = function () {};
  DrawPrimitive.prototype.updateAllViews = function () {
    this._priceViews = this.api.buildPriceViews();
    this._priceViews.forEach(function (v) {
      if (v.update) v.update();
    });
  };
  DrawPrimitive.prototype.paneViews = function () {
    return [this._paneView];
  };
  DrawPrimitive.prototype.priceAxisViews = function () {
    return this._priceViews;
  };
  DrawPrimitive.prototype.requestUpdate = function () {
    if (this._requestUpdate) this._requestUpdate();
  };
  DrawPrimitive.prototype._draw = function (target) {
    const self = this;
    const run = function (ctx, w, h) {
      self.api.render(ctx, w, h);
    };
    if (target.useMediaCoordinateSpace) {
      target.useMediaCoordinateSpace(function (scope) {
        const w = scope.mediaSize ? scope.mediaSize.width : 0;
        const h = scope.mediaSize ? scope.mediaSize.height : 0;
        run(scope.context, w, h);
      });
      return;
    }
    target.useBitmapCoordinateSpace(function (scope) {
      const ctx = scope.context;
      ctx.save();
      ctx.scale(scope.horizontalPixelRatio, scope.verticalPixelRatio);
      const w = scope.mediaSize ? scope.mediaSize.width : 0;
      const h = scope.mediaSize ? scope.mediaSize.height : 0;
      run(ctx, w, h);
      ctx.restore();
    });
  };

  function createChartDrawings(opts) {
    const chart = opts.chart;
    const series = opts.series;
    const host = opts.host;
    const getCandles = opts.getCandles;
    let drawings = [];
    let undo = [];
    let redo = [];
    let tool = "cursor";
    let magnet = false;
    let hideAll = false;
    let selectedId = null;
    let draft = null;
    let drag = null;
    let pointerDown = false;
    let hoverId = null;

    const prim = new DrawPrimitive({
      buildPriceViews: buildPriceViews,
      render: renderAll,
    });
    series.attachPrimitive(prim);

    function save() {
      lsSet(LS_KEY, JSON.stringify({ drawings: drawings, magnet: magnet, hideAll: hideAll }));
    }
    function load() {
      try {
        const raw = JSON.parse(lsGet(LS_KEY, "null"));
        if (!raw) return;
        if (Array.isArray(raw.drawings)) drawings = raw.drawings;
        magnet = Boolean(raw.magnet);
        hideAll = Boolean(raw.hideAll);
      } catch (e) {}
    }
    function pushUndo() {
      undo.push(JSON.stringify(drawings));
      if (undo.length > 20) undo.shift();
      redo = [];
    }
    function doUndo() {
      if (!undo.length) return;
      redo.push(JSON.stringify(drawings));
      drawings = JSON.parse(undo.pop());
      selectedId = null;
      save();
      prim.requestUpdate();
    }
    function doRedo() {
      if (!redo.length) return;
      undo.push(JSON.stringify(drawings));
      drawings = JSON.parse(redo.pop());
      selectedId = null;
      save();
      prim.requestUpdate();
    }
    function bump() {
      save();
      padRight();
      prim.requestUpdate();
      syncToolbar();
    }
    function isToolActive() {
      return tool !== "cursor";
    }

    function candlesNow() {
      return getCandles() || [];
    }
    function timeToX(t) {
      const ts = chart.timeScale();
      const n = Number(t);
      const direct = ts.timeToCoordinate(n);
      if (direct != null) return direct;
      const candles = candlesNow();
      if (!candles.length || !Number.isFinite(n)) return null;
      const lastI = candles.length - 1;
      const firstT = Number(candles[0].time);
      const lastT = Number(candles[lastI].time);
      const sec = tfSec();
      let logical;
      if (n > lastT) logical = lastI + (n - lastT) / sec;
      else if (n < firstT) logical = (n - firstT) / sec;
      else {
        logical = lastI;
        for (let i = 0; i < candles.length; i += 1) {
          if (Number(candles[i].time) >= n) {
            logical = i;
            break;
          }
        }
      }
      const x = ts.logicalToCoordinate(logical);
      return x == null ? null : x;
    }
    function p2y(p) {
      const n = Number(p);
      if (!Number.isFinite(n)) return null;
      const direct = series.priceToCoordinate(n);
      if (direct != null) return direct;
      const candles = candlesNow();
      if (!candles.length) return null;
      let pA = Number(candles[0].close);
      let pB = Number(candles[candles.length - 1].close);
      if (pA === pB) {
        pA = Number(candles[0].low);
        pB = Number(candles[candles.length - 1].high);
      }
      if (pA === pB) pB = pA + 1;
      const yA = series.priceToCoordinate(pA);
      const yB = series.priceToCoordinate(pB);
      if (yA == null || yB == null) return null;
      return yA + (n - pA) * (yB - yA) / (pB - pA);
    }
    function xToTime(x) {
      const ts = chart.timeScale();
      const t = ts.coordinateToTime(x);
      if (t != null) return Number(t);
      const logical = ts.coordinateToLogical(x);
      if (logical == null) return null;
      const candles = candlesNow();
      if (!candles.length) return null;
      const lastI = candles.length - 1;
      const sec = tfSec();
      if (logical > lastI) {
        return Number(candles[lastI].time) + (logical - lastI) * sec;
      }
      if (logical < 0) {
        return Number(candles[0].time) + logical * sec;
      }
      const i0 = Math.max(0, Math.min(lastI, Math.floor(logical)));
      const i1 = Math.min(lastI, i0 + 1);
      const f = logical - i0;
      const t0 = Number(candles[i0].time);
      const t1 = Number(candles[i1].time);
      return t0 + f * (t1 - t0);
    }
    function y2p(y) {
      const p = series.coordinateToPrice(y);
      return p == null ? null : Number(p);
    }
    function padRight() {
      const candles = candlesNow();
      if (!candles.length) return;
      const lastT = Number(candles[candles.length - 1].time);
      const sec = tfSec();
      let extra = 48;
      drawings.concat(draft ? [draft] : []).forEach(function (d) {
        [d.t1, d.t2, d.time].forEach(function (tm) {
          if (tm == null) return;
          const bars = (Number(tm) - lastT) / sec;
          if (bars + 12 > extra) extra = Math.ceil(bars + 12);
        });
      });
      extra = Math.max(32, Math.min(240, extra));
      const opt = { timeScale: { rightOffset: extra } };
      chart.applyOptions(opt);
      const extras = typeof opts.getPaneCharts === "function"
        ? opts.getPaneCharts()
        : (opts.rsiChart ? [opts.rsiChart] : []);
      (extras || []).forEach(function (ch) {
        if (ch && ch.applyOptions) ch.applyOptions(opt);
      });
    }
    function paneXY(ev) {
      const r = host.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    }
    function allCharts() {
      const list = [chart];
      const extras = typeof opts.getPaneCharts === "function"
        ? opts.getPaneCharts()
        : (opts.rsiChart ? [opts.rsiChart] : []);
      (extras || []).forEach(function (ch) {
        if (ch && list.indexOf(ch) < 0) list.push(ch);
      });
      return list;
    }
    function setChartInteraction(on) {
      const opt = { handleScroll: Boolean(on), handleScale: Boolean(on) };
      allCharts().forEach(function (ch) {
        if (ch && ch.applyOptions) ch.applyOptions(opt);
      });
    }
    function lockPan(ev) {
      if (ev) {
        if (ev.preventDefault) ev.preventDefault();
        if (ev.stopPropagation) ev.stopPropagation();
      }
      setChartInteraction(false);
    }
    function unlockPan() {
      setChartInteraction(true);
    }
    function snapTP(time, price) {
      if (!magnet) return { time: time, price: price };
      const candles = getCandles() || [];
      let best = null;
      let bestDt = Infinity;
      for (let i = 0; i < candles.length; i += 1) {
        const c = candles[i];
        const dt = Math.abs(Number(c.time) - Number(time));
        if (dt < bestDt) {
          bestDt = dt;
          best = c;
        }
      }
      if (!best) return { time: time, price: price };
      if (Math.abs(Number(best.time) - Number(time)) > tfSec() * 0.75) {
        return { time: time, price: price };
      }
      const lv = [best.open, best.high, best.low, best.close].map(Number);
      let bp = lv[0];
      let bd = Infinity;
      for (let i = 0; i < lv.length; i += 1) {
        const d = Math.abs(lv[i] - Number(price));
        if (d < bd) {
          bd = d;
          bp = lv[i];
        }
      }
      return { time: Number(best.time), price: bp };
    }

    function visibleList() {
      if (hideAll) return [];
      return drawings.filter(function (d) {
        return !d.hidden;
      });
    }

    function tfSec() {
      if (opts.getTfSec) {
        const n = Number(opts.getTfSec());
        if (n > 0) return n;
      }
      return 60;
    }
    let lastStyles = {};
    try {
      lastStyles = JSON.parse(lsGet(LS_STYLES, "{}")) || {};
    } catch (e) {
      lastStyles = {};
    }
    function rememberStyle(d) {
      if (!d || !d.type) return;
      lastStyles[d.type] = {
        color: d.color,
        width: d.width,
        style: d.style,
        fillColor: d.fillColor,
        fillOpacity: d.fillOpacity,
        textColor: d.textColor,
        fontSize: d.fontSize,
        targetColor: d.targetColor,
        slColor: d.slColor,
        showMeasurement: d.showMeasurement,
        showLabels: d.showLabels,
        accountSize: d.accountSize,
        riskMode: d.riskMode,
        riskValue: d.riskValue,
        qtyMode: d.qtyMode,
        lotSize: d.lotSize,
        trendOn: d.trendOn,
        trendColor: d.trendColor,
        extend: d.extend,
        oneColor: d.oneColor,
        fibBg: d.fibBg,
        fibBgOp: d.fibBgOp,
        labelMode: d.labelMode,
        labelSide: d.labelSide,
        reverse: d.reverse,
        levels: d.levels ? JSON.parse(JSON.stringify(d.levels)) : undefined,
      };
      lsSet(LS_STYLES, JSON.stringify(lastStyles));
    }

    function stroke(ctx, d) {
      ctx.strokeStyle = d.color || COLORS[0];
      ctx.lineWidth = d.width || 1;
      if (d.style === "dashed") ctx.setLineDash([6, 4]);
      else if (d.style === "dotted") ctx.setLineDash([2, 3]);
      else ctx.setLineDash([]);
    }
    function drawHandle(ctx, x, y, hot) {
      if (x == null || y == null) return;
      ctx.setLineDash([]);
      ctx.fillStyle = hot ? "#fff" : "#0e1117";
      ctx.strokeStyle = "#58a6ff";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.rect(x - 4, y - 4, 8, 8);
      ctx.fill();
      ctx.stroke();
    }
    function fmtPx(p) {
      const n = Number(p);
      if (!Number.isFinite(n)) return "";
      return Math.round(n).toLocaleString("en-US");
    }

    function renderAll(ctx, w, h) {
      if (hideAll) return;
      const list = visibleList();
      const preview = draft ? [draft] : [];
      list.concat(preview).forEach(function (d) {
        drawOne(ctx, w, h, d, d.id === selectedId);
      });
    }

    function drawOne(ctx, w, h, d, sel) {
      stroke(ctx, d);
      if (d.type === "hline") {
        const y = p2y(d.price);
        if (y == null) return;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
        if (sel) drawHandle(ctx, w / 2, y, true);
        return;
      }
      if (d.type === "hray") {
        const y = p2y(d.price);
        const x = timeToX(d.time);
        if (y == null) return;
        const x0 = x == null ? 0 : x;
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
        if (sel) drawHandle(ctx, x0, y, true);
        return;
      }
      if (d.type === "trend") {
        const x1 = timeToX(d.t1);
        const y1 = p2y(d.p1);
        const x2 = timeToX(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        if (d.showMeasurement) {
          drawMeasLabel(ctx, d, measureTrend(d.p1, d.p2, d.t1, d.t2, tfSec()), (x1 + x2) / 2, Math.min(y1, y2));
        }
        if (sel) {
          drawHandle(ctx, x1, y1, true);
          drawHandle(ctx, x2, y2, true);
        }
        return;
      }
      if (d.type === "vline") {
        const x = timeToX(d.time);
        if (x == null) return;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
        if (sel) drawHandle(ctx, x, h / 2, true);
        return;
      }
      if (d.type === "rect") {
        const x1 = timeToX(d.t1);
        const y1 = p2y(d.p1);
        const x2 = timeToX(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return;
        const x = Math.min(x1, x2);
        const y = Math.min(y1, y2);
        const rw = Math.abs(x2 - x1);
        const rh = Math.abs(y2 - y1);
        const op = (d.fillOpacity != null ? Number(d.fillOpacity) : 15) / 100;
        ctx.fillStyle = hexA(d.fillColor || d.color || COLORS[0], op);
        ctx.fillRect(x, y, rw, rh);
        ctx.strokeRect(x, y, rw, rh);
        if (d.showMeasurement !== false) {
          drawMeasLabel(ctx, d, measureBox(d.p1, d.p2, d.t1, d.t2, tfSec()), x + rw / 2, y);
        }
        if (sel) {
          drawHandle(ctx, x1, y1, true);
          drawHandle(ctx, x2, y2, true);
        }
        return;
      }
      if (d.type === "text") {
        const x = timeToX(d.time);
        const y = p2y(d.price);
        if (x == null || y == null) return;
        ctx.setLineDash([]);
        const fs = Number(d.fontSize) || 12;
        ctx.font = fs + "px Segoe UI, sans-serif";
        ctx.fillStyle = d.textColor || d.color || COLORS[0];
        ctx.fillText(d.text || "", x + 6, y - 6);
        if (sel) drawHandle(ctx, x, y, true);
        return;
      }
      if (d.type === "long" || d.type === "short") {
        drawPosition(ctx, w, h, d, sel || d.id === hoverId);
        return;
      }
      if (d.type === "fib") {
        drawFib(ctx, w, h, d, sel);
      }
    }

    function drawMeasLabel(ctx, d, text, cx, cy) {
      ctx.save();
      ctx.setLineDash([]);
      const fs = Number(d.fontSize) || 12;
      ctx.font = fs + "px Segoe UI, sans-serif";
      ctx.fillStyle = d.textColor || "#e6edf3";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText(text, cx, cy - 4);
      ctx.restore();
    }

    function hexA(hex, a) {
      const h = String(hex || "").replace("#", "");
      const n = parseInt(h.length === 3 ? h.split("").map(function (c) { return c + c; }).join("") : h, 16);
      const r = (n >> 16) & 255;
      const g = (n >> 8) & 255;
      const b = n & 255;
      return "rgba(" + r + "," + g + "," + b + "," + a + ")";
    }

    function posQty(d) {
      const riskPts = Math.abs(Number(d.entry) - Number(d.sl));
      const lot = Number(d.lotSize) > 0 ? Number(d.lotSize) : 0.01;
      if (d.qtyMode === "manual" && Number(d.qty) > 0) return Number(d.qty);
      const acct = Number(d.accountSize != null ? d.accountSize : 1000);
      const riskVal = Number(d.riskValue != null ? d.riskValue : 250);
      const riskUsd = d.riskMode === "pct" ? acct * (riskVal / 100) : riskVal;
      if (!(riskPts > 0) || !(riskUsd > 0)) return 0;
      let q = riskUsd / riskPts;
      if (lot > 0) q = Math.round(q / lot) * lot;
      return Math.max(lot, q);
    }
    function fmtAmt(n) {
      if (!Number.isFinite(n)) return "—";
      const a = Math.abs(n);
      if (a >= 100) return n.toFixed(0);
      return n.toFixed(1);
    }
    function posLineText(kind, pts, entry, qty) {
      const pct = Number(entry) > 0 ? (pts / Number(entry)) * 100 : 0;
      const ticks = Math.round(pts / MINTICK);
      const amt = qty * pts;
      const name = kind === "target" ? "Target" : "Stop";
      return name + ": " + pts.toFixed(1) + " (" + pct.toFixed(3) + "%) " + ticks + ", Amount: " + fmtAmt(amt);
    }
    function markPrice() {
      if (opts.getMarkPrice) {
        const p = Number(opts.getMarkPrice());
        if (Number.isFinite(p)) return p;
      }
      const c = candlesNow();
      return c.length ? Number(c[c.length - 1].close) : null;
    }

    function drawPosition(ctx, w, h, d, sel) {
      const x1 = timeToX(d.t1);
      const x2 = timeToX(d.t2);
      const yE = p2y(d.entry);
      const yT = p2y(d.target);
      const yS = p2y(d.sl);
      if (x1 == null || x2 == null || yE == null || yT == null || yS == null) return;
      const left = Math.min(x1, x2);
      const right = Math.max(x1, x2);
      const bw = Math.max(24, right - left);
      const topT = Math.min(yE, yT);
      const htT = Math.abs(yT - yE);
      const topS = Math.min(yE, yS);
      const htS = Math.abs(yS - yE);
      const op = (d.fillOpacity != null ? Number(d.fillOpacity) : 22) / 100;
      const tCol = d.targetColor || "#3fb950";
      const sCol = d.slColor || "#f85149";
      ctx.setLineDash([]);
      ctx.fillStyle = hexA(tCol, op);
      ctx.fillRect(left, topT, bw, Math.max(2, htT));
      ctx.fillStyle = hexA(sCol, op);
      ctx.fillRect(left, topS, bw, Math.max(2, htS));
      ctx.strokeStyle = tCol;
      ctx.lineWidth = d.width || 1;
      ctx.strokeRect(left, topT, bw, Math.max(2, htT));
      ctx.strokeStyle = sCol;
      ctx.strokeRect(left, topS, bw, Math.max(2, htS));
      ctx.strokeStyle = d.color || "#58a6ff";
      ctx.beginPath();
      ctx.moveTo(left, yE);
      ctx.lineTo(left + bw, yE);
      ctx.stroke();
      const showLab = d.showLabels !== false && sel;
      if (showLab) {
        const qty = posQty(d);
        const rew = Math.abs(Number(d.target) - Number(d.entry));
        const risk = Math.abs(Number(d.entry) - Number(d.sl));
        const ratio = rrRatio(d.entry, d.target, d.sl);
        const cur = markPrice();
        let pnl = 0;
        if (cur != null) {
          pnl = d.type === "short"
            ? (Number(d.entry) - cur) * qty
            : (cur - Number(d.entry)) * qty;
        }
        const fs = Number(d.fontSize) || 11;
        ctx.font = fs + "px Segoe UI, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "alphabetic";
        ctx.fillStyle = tCol;
        const tY = yT < yE ? topT - 4 : topT + htT + 12;
        ctx.fillText(posLineText("target", rew, d.entry, qty), left + 4, tY);
        ctx.fillStyle = sCol;
        const sY = yS > yE ? topS + htS + 12 : topS - 4;
        ctx.fillText(posLineText("stop", risk, d.entry, qty), left + 4, sY);
        ctx.fillStyle = d.textColor || "#e6edf3";
        const rrTxt = ratio == null ? "—" : ratio.toFixed(2);
        const midY = yE - 4;
        ctx.fillText(
          "Open PnL: " + fmtAmt(pnl) + ", Qty: " + qty.toFixed(2) + " / Risk/reward ratio: " + rrTxt,
          left + 4,
          midY
        );
      }
      if (sel && d.id === selectedId) {
        drawHandle(ctx, left, yT, true);
        drawHandle(ctx, left, yS, true);
        drawHandle(ctx, left, yE, true);
        drawHandle(ctx, left + bw, yE, true);
      }
    }

    function fibLevelsOf(d) {
      return Array.isArray(d.levels) && d.levels.length ? d.levels : defaultFibLevels();
    }
    function drawCircleHandle(ctx, x, y) {
      if (x == null || y == null) return;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fillStyle = "#0e1117";
      ctx.fill();
      ctx.strokeStyle = "#58a6ff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    function drawFib(ctx, w, h, d, sel) {
      const p1 = d.reverse ? d.p2 : d.p1;
      const p2 = d.reverse ? d.p1 : d.p2;
      const x1 = timeToX(d.t1);
      const x2 = timeToX(d.t2);
      const y1 = p2y(d.p1);
      const y2 = p2y(d.p2);
      if (x1 == null || x2 == null) return;
      const left = Math.min(x1, x2);
      const right = Math.max(x1, x2);
      const ext = d.extend || "none";
      let xL = left;
      let xR = right;
      if (ext === "left" || ext === "both") xL = 0;
      if (ext === "right" || ext === "both") xR = w;
      const lv = fibLevelsOf(d).filter(function (x) { return x.on; }).slice().sort(function (a, b) { return a.v - b.v; });
      const bgOn = d.fibBg !== false;
      const bgOp = (d.fibBgOp != null ? Number(d.fibBgOp) : 20) / 100;
      const one = Boolean(d.oneColor);
      const lineCol = d.color || COLORS[0];
      for (let i = 0; i + 1 < lv.length; i += 1) {
        if (!bgOn) break;
        const ya = p2y(fibPrice(p1, p2, lv[i].v));
        const yb = p2y(fibPrice(p1, p2, lv[i + 1].v));
        if (ya == null || yb == null) continue;
        const col = one ? lineCol : (lv[i].color || lineCol);
        ctx.fillStyle = hexA(col, bgOp);
        ctx.fillRect(xL, Math.min(ya, yb), Math.max(1, xR - xL), Math.abs(yb - ya));
      }
      if (d.trendOn !== false && y1 != null && y2 != null) {
        ctx.strokeStyle = d.trendColor || "#787b86";
        ctx.lineWidth = d.width || 1;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      const fs = Number(d.fontSize) || 11;
      ctx.font = fs + "px Segoe UI, sans-serif";
      ctx.setLineDash(d.style === "dotted" ? [2, 3] : (d.style === "solid" ? [] : [6, 4]));
      ctx.lineWidth = d.width || 1;
      const labMode = d.labelMode || "percent";
      const labSide = d.labelSide || "right";
      lv.forEach(function (lvItem) {
        const price = fibPrice(p1, p2, lvItem.v);
        const y = p2y(price);
        if (y == null) return;
        const col = one ? lineCol : (lvItem.color || lineCol);
        ctx.strokeStyle = col;
        ctx.beginPath();
        ctx.moveTo(xL, y);
        ctx.lineTo(xR, y);
        ctx.stroke();
        let lab = (lvItem.v * 100).toFixed(2).replace(/\.00$/, "") + "%";
        if (Number(lvItem.v) === 0.786) lab = "78.60%";
        else if (Number(lvItem.v) === 0.618) lab = "61.80%";
        if (labMode === "price") lab = price.toFixed(1);
        if (labMode === "both") lab = lab + " (" + price.toFixed(1) + ")";
        ctx.setLineDash([]);
        ctx.fillStyle = col;
        ctx.textBaseline = "bottom";
        if (labSide === "left") {
          ctx.textAlign = "left";
          ctx.fillText(lab, xL + 4, y - 2);
        } else {
          ctx.textAlign = "right";
          ctx.fillText(lab, xR - 4, y - 2);
        }
        ctx.setLineDash(d.style === "dotted" ? [2, 3] : (d.style === "solid" ? [] : [6, 4]));
      });
      if (sel) {
        drawCircleHandle(ctx, x1, y1);
        drawCircleHandle(ctx, x2, y2);
      }
    }

    function buildPriceViews() {
      const views = [];
      if (hideAll) return views;
      visibleList().forEach(function (d) {
        if (d.type === "hline" || d.type === "hray") {
          views.push(new PriceLabel(function () { return p2y(d.price); }, function () { return fmtPx(d.price); }, function () { return d.color || COLORS[0]; }));
        }
        if ((d.type === "long" || d.type === "short") && (d.id === selectedId || d.id === hoverId)) {
          views.push(new PriceLabel(function () { return p2y(d.entry); }, function () { return "E " + fmtPx(d.entry); }, function () { return d.color || "#58a6ff"; }));
          views.push(new PriceLabel(function () { return p2y(d.target); }, function () { return "TP " + fmtPx(d.target); }, function () { return d.targetColor || "#3fb950"; }));
          views.push(new PriceLabel(function () { return p2y(d.sl); }, function () { return "SL " + fmtPx(d.sl); }, function () { return d.slColor || "#f85149"; }));
        }
      });
      return views;
    }

    function hitHandles(d, x, y) {
      const hs = [];
      if (d.type === "hline") hs.push({ name: "price", x: 40, y: p2y(d.price) });
      if (d.type === "hray") hs.push({ name: "anchor", x: timeToX(d.time), y: p2y(d.price) });
      if (d.type === "trend") {
        hs.push({ name: "a", x: timeToX(d.t1), y: p2y(d.p1) });
        hs.push({ name: "b", x: timeToX(d.t2), y: p2y(d.p2) });
      }
      if (d.type === "vline") hs.push({ name: "time", x: timeToX(d.time), y: y });
      if (d.type === "rect") {
        hs.push({ name: "a", x: timeToX(d.t1), y: p2y(d.p1) });
        hs.push({ name: "b", x: timeToX(d.t2), y: p2y(d.p2) });
      }
      if (d.type === "text") hs.push({ name: "anchor", x: timeToX(d.time), y: p2y(d.price) });
      if (d.type === "long" || d.type === "short") {
        const x1 = timeToX(d.t1);
        const x2 = timeToX(d.t2);
        const left = Math.min(x1, x2);
        const right = Math.max(x1, x2);
        hs.push({ name: "target", x: left, y: p2y(d.target) });
        hs.push({ name: "sl", x: left, y: p2y(d.sl) });
        hs.push({ name: "left", x: left, y: p2y(d.entry) });
        hs.push({ name: "right", x: right, y: p2y(d.entry) });
      }
      if (d.type === "fib") {
        hs.push({ name: "a", x: timeToX(d.t1), y: p2y(d.p1) });
        hs.push({ name: "b", x: timeToX(d.t2), y: p2y(d.p2) });
      }
      for (let i = 0; i < hs.length; i += 1) {
        const h = hs[i];
        if (h.x == null || h.y == null) continue;
        if (dist(x, y, h.x, h.y) <= HIT) return h.name;
      }
      return null;
    }

    function hitBody(d, x, y, w, h) {
      if (d.type === "hline") {
        const yy = p2y(d.price);
        return yy != null && Math.abs(y - yy) <= HIT;
      }
      if (d.type === "hray") {
        const yy = p2y(d.price);
        const xx = timeToX(d.time);
        return yy != null && Math.abs(y - yy) <= HIT && (xx == null || x >= xx - HIT);
      }
      if (d.type === "trend") {
        const x1 = timeToX(d.t1);
        const y1 = p2y(d.p1);
        const x2 = timeToX(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return false;
        return distSeg(x, y, x1, y1, x2, y2) <= HIT;
      }
      if (d.type === "vline") {
        const xx = timeToX(d.time);
        return xx != null && Math.abs(x - xx) <= HIT;
      }
      if (d.type === "rect") {
        const x1 = timeToX(d.t1);
        const y1 = p2y(d.p1);
        const x2 = timeToX(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return false;
        const l = Math.min(x1, x2);
        const r = Math.max(x1, x2);
        const t = Math.min(y1, y2);
        const b = Math.max(y1, y2);
        return x >= l - 2 && x <= r + 2 && y >= t - 2 && y <= b + 2;
      }
      if (d.type === "text") {
        const xx = timeToX(d.time);
        const yy = p2y(d.price);
        return xx != null && yy != null && dist(x, y, xx, yy) <= 16;
      }
      if (d.type === "long" || d.type === "short") {
        const x1 = timeToX(d.t1);
        const x2 = timeToX(d.t2);
        const yE = p2y(d.entry);
        const yT = p2y(d.target);
        const yS = p2y(d.sl);
        if (x1 == null || x2 == null || yE == null || yT == null || yS == null) return false;
        const l = Math.min(x1, x2);
        const r = Math.max(x1, x2);
        const t = Math.min(yE, yT, yS);
        const b = Math.max(yE, yT, yS);
        return x >= l && x <= r && y >= t && y <= b;
      }
      if (d.type === "fib") {
        const x1 = timeToX(d.t1);
        const y1 = p2y(d.p1);
        const x2 = timeToX(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return false;
        if (distSeg(x, y, x1, y1, x2, y2) <= HIT) return true;
        const lv = fibLevelsOf(d).filter(function (z) { return z.on; });
        const pA = d.reverse ? d.p2 : d.p1;
        const pB = d.reverse ? d.p1 : d.p2;
        const l = Math.min(x1, x2);
        const r = Math.max(x1, x2);
        for (let i = 0; i < lv.length; i += 1) {
          const yy = p2y(fibPrice(pA, pB, lv[i].v));
          if (yy != null && Math.abs(y - yy) <= HIT && x >= l - 2 && x <= r + 2) return true;
        }
      }
      return false;
    }

    function hitTop(x, y) {
      const w = host.clientWidth;
      const h = host.clientHeight;
      const list = visibleList();
      for (let i = list.length - 1; i >= 0; i -= 1) {
        const d = list[i];
        if (d.locked) {
          if (hitBody(d, x, y, w, h)) return { d: d, handle: null };
          continue;
        }
        const handle = hitHandles(d, x, y);
        if (handle) return { d: d, handle: handle };
        if (hitBody(d, x, y, w, h)) return { d: d, handle: "body" };
      }
      return null;
    }

    function applyDrag(d, handle, time, price, dxT, dxP) {
      if (d.locked) return;
      if (d.type === "hline") d.price = price;
      else if (d.type === "hray") {
        if (handle === "anchor" || handle === "body") {
          d.time = time;
          d.price = price;
        }
      } else if (d.type === "trend") {
        if (handle === "a") {
          d.t1 = time;
          d.p1 = price;
        } else if (handle === "b") {
          d.t2 = time;
          d.p2 = price;
        } else {
          d.t1 += dxT;
          d.t2 += dxT;
          d.p1 += dxP;
          d.p2 += dxP;
        }
      } else if (d.type === "vline") d.time = time;
      else if (d.type === "rect") {
        if (handle === "a") {
          d.t1 = time;
          d.p1 = price;
        } else if (handle === "b") {
          d.t2 = time;
          d.p2 = price;
        } else {
          d.t1 += dxT;
          d.t2 += dxT;
          d.p1 += dxP;
          d.p2 += dxP;
        }
      } else if (d.type === "text") {
        d.time = time;
        d.price = price;
      } else if (d.type === "long" || d.type === "short") {
        if (handle === "target") d.target = price;
        else if (handle === "sl") d.sl = price;
        else if (handle === "right") d.t2 = time;
        else if (handle === "left") d.t1 = time;
        else {
          d.t1 += dxT;
          d.t2 += dxT;
          d.entry += dxP;
          d.target += dxP;
          d.sl += dxP;
        }
      } else if (d.type === "fib") {
        if (handle === "a") {
          d.t1 = time;
          d.p1 = price;
        } else if (handle === "b") {
          d.t2 = time;
          d.p2 = price;
        } else {
          d.t1 += dxT;
          d.t2 += dxT;
          d.p1 += dxP;
          d.p2 += dxP;
        }
      }
    }

    function defaultStyle(type) {
      const t = type || tool;
      const last = lastStyles[t] || {};
      const base = {
        color: COLORS[0],
        width: 1,
        style: "solid",
        locked: false,
        fillColor: COLORS[0],
        fillOpacity: t === "long" || t === "short" ? 22 : 15,
        textColor: "#e6edf3",
        fontSize: 12,
        targetColor: "#3fb950",
        slColor: "#f85149",
        showMeasurement: t === "rect",
        showLabels: true,
        accountSize: 1000,
        riskMode: "usd",
        riskValue: 250,
        qtyMode: "auto",
        qty: 0,
        lotSize: 0.01,
        levels: t === "fib" ? defaultFibLevels() : undefined,
        trendOn: true,
        trendColor: "#787b86",
        extend: "none",
        oneColor: false,
        fibBg: true,
        fibBgOp: 20,
        labelMode: "percent",
        labelSide: "right",
        reverse: false,
      };
      const merged = Object.assign(base, last, { locked: false });
      if (t === "fib") {
        merged.levels = Array.isArray(merged.levels)
          ? JSON.parse(JSON.stringify(merged.levels))
          : defaultFibLevels();
      }
      return merged;
    }

    function finishDraft() {
      if (!draft) return;
      pushUndo();
      draft.id = uid();
      rememberStyle(draft);
      drawings.push(draft);
      selectedId = draft.id;
      draft = null;
      tool = "cursor";
      bump();
    }

    function startText(time, price, clientX, clientY) {
      const inp = document.getElementById("drawTextInput");
      if (!inp) return;
      inp.value = "";
      inp.style.left = clientX + "px";
      inp.style.top = clientY + "px";
      inp.hidden = false;
      inp.focus();
      const done = function () {
        inp.removeEventListener("keydown", onKey);
        inp.removeEventListener("blur", onBlur);
        inp.hidden = true;
        const txt = String(inp.value || "").trim();
        if (!txt) {
          tool = "cursor";
          syncToolbar();
          return;
        }
        pushUndo();
        const d = Object.assign(defaultStyle("text"), { id: uid(), type: "text", time: time, price: price, text: txt });
        rememberStyle(d);
        drawings.push(d);
        selectedId = d.id;
        tool = "cursor";
        bump();
      };
      function onKey(e) {
        if (e.key === "Enter") done();
        if (e.key === "Escape") {
          inp.hidden = true;
          tool = "cursor";
          syncToolbar();
        }
      }
      function onBlur() {
        done();
      }
      inp.addEventListener("keydown", onKey);
      inp.addEventListener("blur", onBlur);
    }

    function onPointerDown(ev) {
      if (ev.button === 2) return;
      if (tool === "cursor" && opts.isReplayPicking && opts.isReplayPicking()) return;
      const pt = paneXY(ev);
      pointerDown = true;
      const time0 = xToTime(pt.x);
      const price0 = y2p(pt.y);
      if (tool !== "cursor") {
        lockPan(ev);
        if (time0 == null || price0 == null) return;
        const sn = snapTP(time0, price0);
        if (tool === "hline") {
          draft = Object.assign(defaultStyle(), { type: "hline", price: sn.price });
          finishDraft();
          return;
        }
        if (tool === "hray") {
          draft = Object.assign(defaultStyle(), { type: "hray", time: sn.time, price: sn.price });
          finishDraft();
          return;
        }
        if (tool === "vline") {
          draft = Object.assign(defaultStyle(), { type: "vline", time: sn.time });
          finishDraft();
          return;
        }
        if (tool === "text") {
          startText(sn.time, sn.price, ev.clientX, ev.clientY);
          return;
        }
        if (tool === "trend" || tool === "rect" || tool === "long" || tool === "short" || tool === "fib") {
          if (!draft) {
            draft = Object.assign(defaultStyle(), {
              type: tool,
              t1: sn.time,
              p1: sn.price,
              t2: sn.time,
              p2: sn.price,
              time: sn.time,
              price: sn.price,
              entry: sn.price,
              target: tool === "short" ? sn.price * 0.99 : sn.price * 1.01,
              sl: tool === "short" ? sn.price * 1.005 : sn.price * 0.995,
            });
            prim.requestUpdate();
            return;
          }
          draft.t2 = sn.time;
          draft.p2 = sn.price;
          if (tool === "long" || tool === "short") {
            draft.entry = draft.p1;
            draft.target = sn.price;
            const rew = Math.abs(draft.target - draft.entry);
            if (tool === "long") draft.sl = draft.entry - rew / 2;
            else draft.sl = draft.entry + rew / 2;
            draft.t1 = Math.min(draft.t1, sn.time);
            draft.t2 = Math.max(Number(draft.time || draft.t1), sn.time);
            if (draft.t2 === draft.t1) draft.t2 = draft.t1 + 3600;
          }
          finishDraft();
        }
        return;
      }
      const hit = hitTop(pt.x, pt.y);
      if (!hit) {
        selectedId = null;
        prim.requestUpdate();
        return;
      }
      lockPan(ev);
      selectedId = hit.d.id;
      if (hit.d.locked) {
        prim.requestUpdate();
        return;
      }
      if (time0 == null || price0 == null) return;
      const sn = snapTP(time0, price0);
      drag = {
        id: hit.d.id,
        handle: hit.handle || "body",
        t0: sn.time,
        p0: sn.price,
        clone: JSON.parse(JSON.stringify(hit.d)),
      };
      prim.requestUpdate();
    }

    function onPointerMove(ev) {
      const pt = paneXY(ev);
      const time0 = xToTime(pt.x);
      const price0 = y2p(pt.y);
      if (time0 == null || price0 == null) return;
      const sn = snapTP(time0, price0);
      if (draft && (tool === "trend" || tool === "rect" || tool === "long" || tool === "short" || tool === "fib")) {
        draft.t2 = sn.time;
        draft.p2 = sn.price;
        if (tool === "long" || tool === "short") {
          draft.target = sn.price;
          const rew = Math.abs(draft.target - draft.entry);
          if (tool === "long") draft.sl = draft.entry - rew / 2;
          else draft.sl = draft.entry + rew / 2;
          draft.t2 = sn.time;
        }
        prim.requestUpdate();
        return;
      }
      if (!pointerDown) {
        const hit = hitTop(pt.x, pt.y);
        const nid = hit && hit.d ? hit.d.id : null;
        if (nid !== hoverId) {
          hoverId = nid;
          prim.requestUpdate();
        }
        return;
      }
      if (!drag) return;
      const d = drawings.find(function (x) { return x.id === drag.id; });
      if (!d) return;
      const dxT = sn.time - drag.t0;
      const dxP = sn.price - drag.p0;
      Object.assign(d, JSON.parse(JSON.stringify(drag.clone)));
      applyDrag(d, drag.handle, sn.time, sn.price, dxT, dxP);
      padRight();
      prim.requestUpdate();
    }

    function onPointerUp() {
      pointerDown = false;
      unlockPan();
      if (drag) {
        pushUndo();
        drag = null;
        save();
      }
    }

    function onContext(ev) {
      const pt = paneXY(ev);
      const hit = hitTop(pt.x, pt.y);
      if (!hit) return;
      ev.preventDefault();
      selectedId = hit.d.id;
      prim.requestUpdate();
      showMenu(ev.clientX, ev.clientY, hit.d);
    }

    function showMenu(x, y, d) {
      const menu = document.getElementById("drawMenu");
      if (!menu) return;
      menu.style.left = x + "px";
      menu.style.top = y + "px";
      menu.hidden = false;
      menu.dataset.id = d.id;
    }
    function hideMenu() {
      const menu = document.getElementById("drawMenu");
      if (menu) menu.hidden = true;
    }

    function selected() {
      return drawings.find(function (d) { return d.id === selectedId; }) || null;
    }

    function deleteSelected() {
      const d = selected();
      if (!d || d.locked) return;
      pushUndo();
      drawings = drawings.filter(function (x) { return x.id !== d.id; });
      selectedId = null;
      bump();
    }

    function dlgEl(id) {
      return document.getElementById(id);
    }
    function dialogOpen() {
      const box = dlgEl("drawDialog");
      return box && box.classList.contains("open");
    }
    function closeDialog() {
      const box = dlgEl("drawDialog");
      if (box) box.classList.remove("open");
    }
    function showTab(name) {
      const map = { style: "dlgStyle", inputs: "dlgInputs", coords: "dlgCoords", vis: "dlgVis" };
      Object.keys(map).forEach(function (k) {
        const n = dlgEl(map[k]);
        if (n) n.hidden = k !== name;
      });
      [["tabStyle", "style"], ["tabInputs", "inputs"], ["tabCoords", "coords"], ["tabVis", "vis"]].forEach(function (pair) {
        const n = dlgEl(pair[0]);
        if (n) n.classList.toggle("active", pair[1] === name);
      });
    }
    function renderFibGrid(d) {
      const host = dlgEl("dlgFibGrid");
      if (!host) return;
      host.innerHTML = "";
      const levels = fibLevelsOf(d);
      d.levels = levels;
      levels.forEach(function (lv, i) {
        const row = document.createElement("div");
        row.className = "fib-lv";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = Boolean(lv.on);
        cb.addEventListener("change", function () { lv.on = cb.checked; });
        const num = document.createElement("input");
        num.type = "number";
        num.step = "0.001";
        num.value = String(lv.v);
        num.addEventListener("change", function () {
          const n = parseFloat(num.value);
          if (Number.isFinite(n)) lv.v = n;
        });
        const col = document.createElement("input");
        col.type = "color";
        col.value = toHex(lv.color || COLORS[0]);
        col.addEventListener("input", function () { lv.color = col.value; });
        row.appendChild(cb);
        row.appendChild(num);
        row.appendChild(col);
        host.appendChild(row);
      });
    }
    function setRow(id, on) {
      const n = dlgEl(id);
      if (n) n.hidden = !on;
    }
    function fillDialog(d) {
      if (dlgEl("dlgCustom")) dlgEl("dlgCustom").value = toHex(d.color || COLORS[0]);
      if (dlgEl("dlgWidth")) dlgEl("dlgWidth").value = String(d.width || 1);
      if (dlgEl("dlgLineStyle")) dlgEl("dlgLineStyle").value = d.style || "solid";
      if (dlgEl("dlgFill")) dlgEl("dlgFill").value = toHex(d.fillColor || d.color || COLORS[0]);
      if (dlgEl("dlgFillOp")) dlgEl("dlgFillOp").value = String(d.fillOpacity != null ? d.fillOpacity : 15);
      if (dlgEl("dlgFillOpLab")) dlgEl("dlgFillOpLab").textContent = (d.fillOpacity != null ? d.fillOpacity : 15) + "%";
      if (dlgEl("dlgTextColor")) dlgEl("dlgTextColor").value = toHex(d.textColor || "#e6edf3");
      if (dlgEl("dlgFont")) dlgEl("dlgFont").value = String(d.fontSize || 12);
      if (dlgEl("dlgTargetCol")) dlgEl("dlgTargetCol").value = toHex(d.targetColor || "#3fb950");
      if (dlgEl("dlgSlCol")) dlgEl("dlgSlCol").value = toHex(d.slColor || "#f85149");
      if (dlgEl("dlgShowMeas")) dlgEl("dlgShowMeas").checked = d.type === "rect" ? d.showMeasurement !== false : Boolean(d.showMeasurement);
      if (dlgEl("dlgPrice")) dlgEl("dlgPrice").value = price1(d.price);
      if (dlgEl("dlgTime")) dlgEl("dlgTime").value = d.time != null ? fmtIstDt(d.time) : "";
      if (dlgEl("dlgT1")) dlgEl("dlgT1").value = d.t1 != null ? fmtIstDt(d.t1) : "";
      if (dlgEl("dlgP1")) dlgEl("dlgP1").value = price1(d.p1);
      if (dlgEl("dlgT2")) dlgEl("dlgT2").value = d.t2 != null ? fmtIstDt(d.t2) : "";
      if (dlgEl("dlgP2")) dlgEl("dlgP2").value = price1(d.p2);
      if (dlgEl("dlgTop")) dlgEl("dlgTop").value = price1(Math.max(Number(d.p1), Number(d.p2)));
      if (dlgEl("dlgBot")) dlgEl("dlgBot").value = price1(Math.min(Number(d.p1), Number(d.p2)));
      if (dlgEl("dlgLeft")) dlgEl("dlgLeft").value = d.t1 != null ? fmtIstDt(Math.min(Number(d.t1), Number(d.t2))) : "";
      if (dlgEl("dlgRight")) dlgEl("dlgRight").value = d.t2 != null ? fmtIstDt(Math.max(Number(d.t1), Number(d.t2))) : "";
      if (dlgEl("dlgNote")) dlgEl("dlgNote").value = d.text || "";
      if (dlgEl("dlgEntry")) dlgEl("dlgEntry").value = price1(d.entry);
      if (dlgEl("dlgTarget")) dlgEl("dlgTarget").value = price1(d.target);
      if (dlgEl("dlgSl")) dlgEl("dlgSl").value = price1(d.sl);
      if (dlgEl("dlgStart")) dlgEl("dlgStart").value = d.t1 != null ? fmtIstDt(d.t1) : "";
      if (dlgEl("dlgEnd")) dlgEl("dlgEnd").value = d.t2 != null ? fmtIstDt(d.t2) : "";
      if (dlgEl("dlgShowPosLab")) dlgEl("dlgShowPosLab").checked = d.showLabels !== false;
      if (dlgEl("dlgAcct")) dlgEl("dlgAcct").value = String(d.accountSize != null ? d.accountSize : 1000);
      if (dlgEl("dlgRiskMode")) dlgEl("dlgRiskMode").value = d.riskMode === "pct" ? "pct" : "usd";
      if (dlgEl("dlgRiskVal")) dlgEl("dlgRiskVal").value = String(d.riskValue != null ? d.riskValue : 250);
      if (dlgEl("dlgQtyAuto")) dlgEl("dlgQtyAuto").checked = d.qtyMode !== "manual";
      if (dlgEl("dlgQty")) dlgEl("dlgQty").value = String(posQty(d).toFixed(2));
      if (dlgEl("dlgLot")) dlgEl("dlgLot").value = String(d.lotSize != null ? d.lotSize : 0.01);
      if (dlgEl("dlgFibTrend")) dlgEl("dlgFibTrend").checked = d.trendOn !== false;
      if (dlgEl("dlgFibTrendCol")) dlgEl("dlgFibTrendCol").value = toHex(d.trendColor || "#787b86");
      if (dlgEl("dlgFibExtend")) dlgEl("dlgFibExtend").value = d.extend || "none";
      if (dlgEl("dlgFibOneColor")) dlgEl("dlgFibOneColor").checked = Boolean(d.oneColor);
      if (dlgEl("dlgFibBg")) dlgEl("dlgFibBg").checked = d.fibBg !== false;
      if (dlgEl("dlgFibBgOp")) dlgEl("dlgFibBgOp").value = String(d.fibBgOp != null ? d.fibBgOp : 20);
      if (dlgEl("dlgFibBgOpLab")) dlgEl("dlgFibBgOpLab").textContent = (d.fibBgOp != null ? d.fibBgOp : 20) + "%";
      if (dlgEl("dlgFibLabelMode")) dlgEl("dlgFibLabelMode").value = d.labelMode || "percent";
      if (dlgEl("dlgFibLabelSide")) dlgEl("dlgFibLabelSide").value = d.labelSide || "right";
      if (dlgEl("dlgFibReverse")) dlgEl("dlgFibReverse").checked = Boolean(d.reverse);
      if (d.type === "fib") renderFibGrid(d);
      const t = d.type;
      setRow("gFill", t === "rect" || t === "long" || t === "short");
      setRow("gText", t === "text" || t === "rect" || t === "trend" || t === "long" || t === "short" || t === "fib");
      setRow("gPosCol", t === "long" || t === "short");
      setRow("gPosLab", t === "long" || t === "short");
      setRow("gMeas", t === "rect" || t === "trend");
      setRow("gFib", t === "fib");
      setRow("gLine", t !== "fib");
      setRow("tabInputs", t === "long" || t === "short");
      setRow("tabVis", t === "fib");
      setRow("cPrice", t === "hline" || t === "hray" || t === "text");
      setRow("cTime", t === "hray" || t === "vline" || t === "text");
      setRow("cT1", t === "trend" || t === "fib");
      setRow("cP1", t === "trend" || t === "fib");
      setRow("cT2", t === "trend" || t === "fib");
      setRow("cP2", t === "trend" || t === "fib");
      setRow("cTop", t === "rect");
      setRow("cBot", t === "rect");
      setRow("cLeft", t === "rect");
      setRow("cRight", t === "rect");
      setRow("cText", t === "text");
      setRow("cEntry", t === "long" || t === "short");
      setRow("cTarget", t === "long" || t === "short");
      setRow("cSl", t === "long" || t === "short");
      setRow("cStart", t === "long" || t === "short");
      setRow("cEnd", t === "long" || t === "short");
      setRow("cRr", t === "long" || t === "short");
      validateDialog(d);
    }
    function toHex(c) {
      const s = String(c || "#58a6ff");
      if (/^#[0-9a-fA-F]{6}$/.test(s)) return s;
      if (/^#[0-9a-fA-F]{3}$/.test(s)) {
        return "#" + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
      }
      return "#58a6ff";
    }
    function price1(p) {
      const n = Number(p);
      if (!Number.isFinite(n)) return "";
      return n.toFixed(1);
    }
    function readTime(id) {
      const raw = parseIstDt(dlgEl(id) && dlgEl(id).value);
      if (raw == null) return null;
      return snapTf(raw, tfSec());
    }
    function readPrice(id) {
      const n = Number(dlgEl(id) && dlgEl(id).value);
      if (!Number.isFinite(n)) return null;
      return Math.round(n * 10) / 10;
    }
    function validateDialog(d) {
      const err = dlgEl("dlgErr");
      const apply = dlgEl("dlgApply");
      if (err) err.textContent = "";
      if (apply) apply.disabled = false;
      if (!d || (d.type !== "long" && d.type !== "short")) return true;
      const entry = readPrice("dlgEntry");
      const target = readPrice("dlgTarget");
      const sl = readPrice("dlgSl");
      const rew = Math.abs((target || 0) - (entry || 0));
      const risk = Math.abs((entry || 0) - (sl || 0));
      const ratio = rrRatio(entry, target, sl);
      if (dlgEl("dlgRew")) dlgEl("dlgRew").textContent = Number.isFinite(rew) ? rew.toFixed(1) : "—";
      if (dlgEl("dlgRisk")) dlgEl("dlgRisk").textContent = Number.isFinite(risk) ? risk.toFixed(1) : "—";
      if (dlgEl("dlgRr")) dlgEl("dlgRr").textContent = ratio == null ? "—" : ratio.toFixed(2);
      if (!posValid(d.type, entry, target, sl)) {
        if (err) err.textContent = d.type === "long" ? "Long: Target > Entry > SL" : "Short: Target < Entry < SL";
        if (apply) apply.disabled = true;
        return false;
      }
      return true;
    }
    let dlgTarget = null;
    function openDialog(d) {
      if (!d || !dlgEl("drawDialog")) return;
      dlgTarget = d;
      fillDialog(d);
      showTab("style");
      dlgEl("drawDialog").classList.add("open");
    }
    function applyDialog() {
      const d = dlgTarget && drawings.find(function (x) { return x.id === dlgTarget.id; });
      if (!d) {
        closeDialog();
        return;
      }
      if (!validateDialog(d)) return;
      const next = JSON.parse(JSON.stringify(d));
      next.color = dlgEl("dlgCustom").value;
      next.width = Number(dlgEl("dlgWidth").value) || 1;
      next.style = dlgEl("dlgLineStyle").value || "solid";
      next.fillColor = dlgEl("dlgFill").value;
      next.fillOpacity = Number(dlgEl("dlgFillOp").value);
      next.textColor = dlgEl("dlgTextColor").value;
      next.fontSize = Math.max(10, Math.min(24, Number(dlgEl("dlgFont").value) || 12));
      next.targetColor = dlgEl("dlgTargetCol").value;
      next.slColor = dlgEl("dlgSlCol").value;
      next.showMeasurement = dlgEl("dlgShowMeas").checked;
      if (dlgEl("dlgShowPosLab")) next.showLabels = dlgEl("dlgShowPosLab").checked;
      if (dlgEl("dlgAcct")) next.accountSize = Number(dlgEl("dlgAcct").value) || 1000;
      if (dlgEl("dlgRiskMode")) next.riskMode = dlgEl("dlgRiskMode").value === "pct" ? "pct" : "usd";
      if (dlgEl("dlgRiskVal")) next.riskValue = Number(dlgEl("dlgRiskVal").value);
      if (dlgEl("dlgLot")) next.lotSize = Number(dlgEl("dlgLot").value) || 0.01;
      if (dlgEl("dlgQtyAuto")) next.qtyMode = dlgEl("dlgQtyAuto").checked ? "auto" : "manual";
      if (dlgEl("dlgQty") && next.qtyMode === "manual") next.qty = Number(dlgEl("dlgQty").value) || 0;
      if (dlgEl("dlgFibTrend")) next.trendOn = dlgEl("dlgFibTrend").checked;
      if (dlgEl("dlgFibTrendCol")) next.trendColor = dlgEl("dlgFibTrendCol").value;
      if (dlgEl("dlgFibExtend")) next.extend = dlgEl("dlgFibExtend").value;
      if (dlgEl("dlgFibOneColor")) next.oneColor = dlgEl("dlgFibOneColor").checked;
      if (dlgEl("dlgFibBg")) next.fibBg = dlgEl("dlgFibBg").checked;
      if (dlgEl("dlgFibBgOp")) next.fibBgOp = Number(dlgEl("dlgFibBgOp").value);
      if (dlgEl("dlgFibLabelMode")) next.labelMode = dlgEl("dlgFibLabelMode").value;
      if (dlgEl("dlgFibLabelSide")) next.labelSide = dlgEl("dlgFibLabelSide").value;
      if (dlgEl("dlgFibReverse")) next.reverse = dlgEl("dlgFibReverse").checked;
      if (d.type === "fib" && Array.isArray(d.levels)) next.levels = JSON.parse(JSON.stringify(d.levels));
      if (d.type === "hline") {
        const p = readPrice("dlgPrice");
        if (p == null) return failCoord();
        next.price = p;
      } else if (d.type === "hray") {
        const p = readPrice("dlgPrice");
        const t = readTime("dlgTime");
        if (p == null || t == null) return failCoord();
        next.price = p;
        next.time = t;
      } else if (d.type === "vline") {
        const t = readTime("dlgTime");
        if (t == null) return failCoord();
        next.time = t;
      } else if (d.type === "trend" || d.type === "fib") {
        const t1 = readTime("dlgT1");
        const t2 = readTime("dlgT2");
        const p1 = readPrice("dlgP1");
        const p2 = readPrice("dlgP2");
        if (t1 == null || t2 == null || p1 == null || p2 == null) return failCoord();
        next.t1 = t1;
        next.t2 = t2;
        next.p1 = p1;
        next.p2 = p2;
      } else if (d.type === "rect") {
        const top = readPrice("dlgTop");
        const bot = readPrice("dlgBot");
        const left = readTime("dlgLeft");
        const right = readTime("dlgRight");
        if (top == null || bot == null || left == null || right == null) return failCoord();
        next.p1 = top;
        next.p2 = bot;
        next.t1 = left;
        next.t2 = right;
      } else if (d.type === "text") {
        const t = readTime("dlgTime");
        const p = readPrice("dlgPrice");
        if (t == null || p == null) return failCoord();
        next.time = t;
        next.price = p;
        next.text = String(dlgEl("dlgNote").value || "");
      } else if (d.type === "long" || d.type === "short") {
        const entry = readPrice("dlgEntry");
        const target = readPrice("dlgTarget");
        const sl = readPrice("dlgSl");
        const t1 = readTime("dlgStart");
        const t2 = readTime("dlgEnd");
        if (entry == null || target == null || sl == null || t1 == null || t2 == null) return failCoord();
        next.entry = entry;
        next.target = target;
        next.sl = sl;
        next.t1 = t1;
        next.t2 = t2;
      }
      pushUndo();
      Object.assign(d, next);
      rememberStyle(d);
      closeDialog();
      bump();
    }
    function failCoord() {
      const err = dlgEl("dlgErr");
      if (err) err.textContent = "Invalid date-time or price (use DD-MM-YYYY HH:MM)";
    }

    function onKey(ev) {
      if (dialogOpen() && ev.key === "Escape") {
        ev.preventDefault();
        closeDialog();
        return;
      }
      const tag = (ev.target && ev.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (ev.key === "Escape") {
        draft = null;
        drag = null;
        pointerDown = false;
        tool = "cursor";
        unlockPan();
        hideMenu();
        bump();
        return;
      }
      if (ev.key === "Delete" || ev.key === "Backspace") {
        deleteSelected();
        return;
      }
      if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) doRedo();
        else doUndo();
      }
      if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === "y") {
        ev.preventDefault();
        doRedo();
      }
    }

    function setTool(name) {
      tool = name;
      draft = null;
      if (tool !== "cursor") {
        selectedId = null;
      }
      hideMenu();
      syncToolbar();
      prim.requestUpdate();
    }

    function syncToolbar() {
      document.querySelectorAll("#drawBar [data-tool]").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-tool") === tool);
      });
      const mag = document.getElementById("drawMagnet");
      if (mag) mag.classList.toggle("active", magnet);
      const hide = document.getElementById("drawHide");
      if (hide) hide.classList.toggle("active", hideAll);
    }

    host.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    host.addEventListener("pointerleave", onPointerUp);
    host.addEventListener("contextmenu", onContext);
    host.addEventListener("dblclick", function (ev) {
      if (tool !== "cursor") return;
      const pt = paneXY(ev);
      const hit = hitTop(pt.x, pt.y);
      if (!hit) return;
      ev.preventDefault();
      selectedId = hit.d.id;
      openDialog(hit.d);
    });
    window.addEventListener("keydown", onKey);
    document.addEventListener("click", function (ev) {
      const menu = document.getElementById("drawMenu");
      if (menu && !menu.hidden && !menu.contains(ev.target)) hideMenu();
    });

    const bar = document.getElementById("drawBar");
    if (bar) {
      bar.addEventListener("click", function (ev) {
        const btn = ev.target.closest("button");
        if (!btn) return;
        const t = btn.getAttribute("data-tool");
        if (t) {
          setTool(tool === t ? "cursor" : t);
          return;
        }
        if (btn.id === "drawMagnet") {
          magnet = !magnet;
          save();
          syncToolbar();
          return;
        }
        if (btn.id === "drawHide") {
          hideAll = !hideAll;
          bump();
          return;
        }
        if (btn.id === "drawDeleteAll") {
          if (!drawings.length) return;
          if (!window.confirm("Delete all drawings?")) return;
          pushUndo();
          drawings = [];
          selectedId = null;
          bump();
        }
      });
    }
    const menu = document.getElementById("drawMenu");
    if (menu) {
      menu.addEventListener("click", function (ev) {
        const d = drawings.find(function (x) { return x.id === menu.dataset.id; });
        if (!d) return;
        const c = ev.target.getAttribute("data-color");
        const w = ev.target.getAttribute("data-width");
        const st = ev.target.getAttribute("data-style");
        if (c) {
          pushUndo();
          d.color = c;
          rememberStyle(d);
        }
        if (w) {
          pushUndo();
          d.width = Number(w);
          rememberStyle(d);
        }
        if (st) {
          pushUndo();
          d.style = st;
          rememberStyle(d);
        }
        if (ev.target.getAttribute("data-act") === "settings") {
          hideMenu();
          openDialog(d);
          return;
        }
        if (ev.target.getAttribute("data-act") === "lock") {
          pushUndo();
          d.locked = !d.locked;
        }
        if (ev.target.getAttribute("data-act") === "delete") {
          selectedId = d.id;
          hideMenu();
          deleteSelected();
          return;
        }
        hideMenu();
        bump();
      });
    }

    const dlgRoot = document.getElementById("drawDialog");
    if (dlgRoot) {
      dlgRoot.addEventListener("click", function (ev) {
        if (ev.target === dlgRoot) closeDialog();
      });
      if (dlgEl("tabStyle")) dlgEl("tabStyle").addEventListener("click", function () { showTab("style"); });
      if (dlgEl("tabInputs")) dlgEl("tabInputs").addEventListener("click", function () { showTab("inputs"); });
      if (dlgEl("tabCoords")) dlgEl("tabCoords").addEventListener("click", function () { showTab("coords"); });
      if (dlgEl("tabVis")) dlgEl("tabVis").addEventListener("click", function () { showTab("vis"); });
      if (dlgEl("dlgCancel")) dlgEl("dlgCancel").addEventListener("click", closeDialog);
      if (dlgEl("dlgApply")) dlgEl("dlgApply").addEventListener("click", applyDialog);
      if (dlgEl("dlgSaveDef")) {
        dlgEl("dlgSaveDef").addEventListener("click", function () {
          if (dlgTarget) {
            applyDialog();
            const d = drawings.find(function (x) { return dlgTarget && x.id === dlgTarget.id; });
            if (d) rememberStyle(d);
          }
        });
      }
      if (dlgEl("dlgResetDef")) {
        dlgEl("dlgResetDef").addEventListener("click", function () {
          if (!dlgTarget) return;
          const t = dlgTarget.type;
          delete lastStyles[t];
          lsSet(LS_STYLES, JSON.stringify(lastStyles));
          const fresh = defaultStyle(t);
          Object.keys(fresh).forEach(function (k) {
            if (k !== "locked") dlgTarget[k] = fresh[k];
          });
          fillDialog(dlgTarget);
        });
      }
      if (dlgEl("dlgFibBgOp")) {
        dlgEl("dlgFibBgOp").addEventListener("input", function () {
          if (dlgEl("dlgFibBgOpLab")) dlgEl("dlgFibBgOpLab").textContent = dlgEl("dlgFibBgOp").value + "%";
        });
      }
      if (dlgEl("dlgFillOp")) {
        dlgEl("dlgFillOp").addEventListener("input", function () {
          if (dlgEl("dlgFillOpLab")) dlgEl("dlgFillOpLab").textContent = dlgEl("dlgFillOp").value + "%";
        });
      }
      dlgRoot.querySelectorAll("[data-qcolor]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          if (dlgEl("dlgCustom")) dlgEl("dlgCustom").value = btn.getAttribute("data-qcolor");
        });
      });
      ["dlgEntry", "dlgTarget", "dlgSl"].forEach(function (id) {
        const n = dlgEl(id);
        if (n) n.addEventListener("input", function () { if (dlgTarget) validateDialog(dlgTarget); });
      });
    }

    load();
    syncToolbar();
    padRight();
    prim.requestUpdate();

    return {
      isToolActive: isToolActive,
      setTool: setTool,
      list: function () { return drawings.slice(); },
      add: function (d) {
        pushUndo();
        d.id = d.id || uid();
        drawings.push(d);
        bump();
        return d;
      },
      refresh: function () { padRight(); prim.requestUpdate(); },
      openSettings: openDialog,
      undo: doUndo,
      redo: doRedo,
    };
  }

  global.createChartDrawings = createChartDrawings;
  global.chartDrawMeasure = {
    measureBox: measureBox,
    measureTrend: measureTrend,
    fmtSpanMin: fmtSpanMin,
    parseIstDt: parseIstDt,
    fmtIstDt: fmtIstDt,
    snapTf: snapTf,
    posValid: posValid,
    rrRatio: rrRatio,
    fibPrice: fibPrice,
  };
})(window);
