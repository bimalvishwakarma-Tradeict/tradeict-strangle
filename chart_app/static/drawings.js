/* Chart drawings via Lightweight Charts v4.2.0 series.attachPrimitive. Time+price coords. */
(function (global) {
  const LS_KEY = "chart_app.drawings.BTCUSD";
  const COLORS = ["#58a6ff", "#3fb950", "#f85149", "#d29922", "#d2a8ff"];
  const HIT = 8;

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
    let tool = "cursor";
    let magnet = false;
    let hideAll = false;
    let selectedId = null;
    let draft = null;
    let drag = null;
    let pointerDown = false;

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
    }
    function bump() {
      save();
      prim.requestUpdate();
      syncToolbar();
    }
    function isToolActive() {
      return tool !== "cursor";
    }

    function t2x(t) {
      const x = chart.timeScale().timeToCoordinate(Number(t));
      return x == null ? null : x;
    }
    function p2y(p) {
      const y = series.priceToCoordinate(Number(p));
      return y == null ? null : y;
    }
    function x2t(x) {
      const t = chart.timeScale().coordinateToTime(x);
      return t == null ? null : Number(t);
    }
    function y2p(y) {
      const p = series.coordinateToPrice(y);
      return p == null ? null : Number(p);
    }
    function paneXY(ev) {
      const r = host.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
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

    function stroke(ctx, d) {
      ctx.strokeStyle = d.color || COLORS[0];
      ctx.lineWidth = d.width || 1;
      ctx.setLineDash(d.style === "dashed" ? [6, 4] : []);
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
        const x = t2x(d.time);
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
        const x1 = t2x(d.t1);
        const y1 = p2y(d.p1);
        const x2 = t2x(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        if (sel) {
          drawHandle(ctx, x1, y1, true);
          drawHandle(ctx, x2, y2, true);
        }
        return;
      }
      if (d.type === "vline") {
        const x = t2x(d.time);
        if (x == null) return;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
        if (sel) drawHandle(ctx, x, h / 2, true);
        return;
      }
      if (d.type === "rect") {
        const x1 = t2x(d.t1);
        const y1 = p2y(d.p1);
        const x2 = t2x(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return;
        const x = Math.min(x1, x2);
        const y = Math.min(y1, y2);
        const rw = Math.abs(x2 - x1);
        const rh = Math.abs(y2 - y1);
        ctx.fillStyle = hexA(d.color || COLORS[0], 0.15);
        ctx.fillRect(x, y, rw, rh);
        ctx.strokeRect(x, y, rw, rh);
        if (sel) {
          drawHandle(ctx, x1, y1, true);
          drawHandle(ctx, x2, y2, true);
        }
        return;
      }
      if (d.type === "text") {
        const x = t2x(d.time);
        const y = p2y(d.price);
        if (x == null || y == null) return;
        ctx.setLineDash([]);
        ctx.font = "12px Segoe UI, sans-serif";
        ctx.fillStyle = d.color || COLORS[0];
        ctx.fillText(d.text || "", x + 6, y - 6);
        if (sel) drawHandle(ctx, x, y, true);
        return;
      }
      if (d.type === "long" || d.type === "short") {
        drawPosition(ctx, w, h, d, sel);
      }
    }

    function hexA(hex, a) {
      const h = String(hex || "").replace("#", "");
      const n = parseInt(h.length === 3 ? h.split("").map(function (c) { return c + c; }).join("") : h, 16);
      const r = (n >> 16) & 255;
      const g = (n >> 8) & 255;
      const b = n & 255;
      return "rgba(" + r + "," + g + "," + b + "," + a + ")";
    }

    function drawPosition(ctx, w, h, d, sel) {
      const x1 = t2x(d.t1);
      const x2 = t2x(d.t2);
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
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(63,185,80,0.22)";
      ctx.fillRect(left, topT, bw, Math.max(2, htT));
      ctx.fillStyle = "rgba(248,81,73,0.22)";
      ctx.fillRect(left, topS, bw, Math.max(2, htS));
      ctx.strokeStyle = "#3fb950";
      ctx.lineWidth = d.width || 1;
      ctx.strokeRect(left, topT, bw, Math.max(2, htT));
      ctx.strokeStyle = "#f85149";
      ctx.strokeRect(left, topS, bw, Math.max(2, htS));
      ctx.strokeStyle = "#e6edf3";
      ctx.beginPath();
      ctx.moveTo(left, yE);
      ctx.lineTo(left + bw, yE);
      ctx.stroke();
      const ratio = rrRatio(d.entry, d.target, d.sl);
      const rrTxt = ratio == null ? "R:R —" : "R:R " + ratio.toFixed(1);
      const rew = Math.abs(d.target - d.entry);
      const risk = Math.abs(d.entry - d.sl);
      ctx.font = "11px Segoe UI, sans-serif";
      ctx.fillStyle = "#e6edf3";
      ctx.fillText(rrTxt + "  +" + fmtPx(rew) + " / -" + fmtPx(risk), left + 6, topT + 14);
      if (sel) {
        drawHandle(ctx, left + bw / 2, yT, true);
        drawHandle(ctx, left + bw / 2, yE, true);
        drawHandle(ctx, left + bw / 2, yS, true);
        drawHandle(ctx, left, yE, true);
        drawHandle(ctx, left + bw, yE, true);
      }
    }

    function buildPriceViews() {
      const views = [];
      if (hideAll) return views;
      visibleList().forEach(function (d) {
        if (d.type === "hline" || d.type === "hray") {
          views.push(new PriceLabel(function () { return p2y(d.price); }, function () { return fmtPx(d.price); }, function () { return d.color || COLORS[0]; }));
        }
        if (d.type === "long" || d.type === "short") {
          views.push(new PriceLabel(function () { return p2y(d.entry); }, function () { return "E " + fmtPx(d.entry); }, function () { return "#e6edf3"; }));
          views.push(new PriceLabel(function () { return p2y(d.target); }, function () { return "TP " + fmtPx(d.target); }, function () { return "#3fb950"; }));
          views.push(new PriceLabel(function () { return p2y(d.sl); }, function () { return "SL " + fmtPx(d.sl); }, function () { return "#f85149"; }));
        }
      });
      return views;
    }

    function hitHandles(d, x, y) {
      const hs = [];
      if (d.type === "hline") hs.push({ name: "price", x: 40, y: p2y(d.price) });
      if (d.type === "hray") hs.push({ name: "anchor", x: t2x(d.time), y: p2y(d.price) });
      if (d.type === "trend") {
        hs.push({ name: "a", x: t2x(d.t1), y: p2y(d.p1) });
        hs.push({ name: "b", x: t2x(d.t2), y: p2y(d.p2) });
      }
      if (d.type === "vline") hs.push({ name: "time", x: t2x(d.time), y: y });
      if (d.type === "rect") {
        hs.push({ name: "a", x: t2x(d.t1), y: p2y(d.p1) });
        hs.push({ name: "b", x: t2x(d.t2), y: p2y(d.p2) });
      }
      if (d.type === "text") hs.push({ name: "anchor", x: t2x(d.time), y: p2y(d.price) });
      if (d.type === "long" || d.type === "short") {
        const x1 = t2x(d.t1);
        const x2 = t2x(d.t2);
        const left = Math.min(x1, x2);
        const right = Math.max(x1, x2);
        const mid = (left + right) / 2;
        hs.push({ name: "target", x: mid, y: p2y(d.target) });
        hs.push({ name: "entry", x: mid, y: p2y(d.entry) });
        hs.push({ name: "sl", x: mid, y: p2y(d.sl) });
        hs.push({ name: "left", x: left, y: p2y(d.entry) });
        hs.push({ name: "right", x: right, y: p2y(d.entry) });
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
        const xx = t2x(d.time);
        return yy != null && Math.abs(y - yy) <= HIT && (xx == null || x >= xx - HIT);
      }
      if (d.type === "trend") {
        const x1 = t2x(d.t1);
        const y1 = p2y(d.p1);
        const x2 = t2x(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return false;
        return distSeg(x, y, x1, y1, x2, y2) <= HIT;
      }
      if (d.type === "vline") {
        const xx = t2x(d.time);
        return xx != null && Math.abs(x - xx) <= HIT;
      }
      if (d.type === "rect") {
        const x1 = t2x(d.t1);
        const y1 = p2y(d.p1);
        const x2 = t2x(d.t2);
        const y2 = p2y(d.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return false;
        const l = Math.min(x1, x2);
        const r = Math.max(x1, x2);
        const t = Math.min(y1, y2);
        const b = Math.max(y1, y2);
        return x >= l - 2 && x <= r + 2 && y >= t - 2 && y <= b + 2;
      }
      if (d.type === "text") {
        const xx = t2x(d.time);
        const yy = p2y(d.price);
        return xx != null && yy != null && dist(x, y, xx, yy) <= 16;
      }
      if (d.type === "long" || d.type === "short") {
        const x1 = t2x(d.t1);
        const x2 = t2x(d.t2);
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
        if (handle === "entry") d.entry = price;
        else if (handle === "target") d.target = price;
        else if (handle === "sl") d.sl = price;
        else if (handle === "left") d.t1 = time;
        else if (handle === "right") d.t2 = time;
        else {
          d.t1 += dxT;
          d.t2 += dxT;
          d.entry += dxP;
          d.target += dxP;
          d.sl += dxP;
        }
      }
    }

    function defaultStyle() {
      return { color: COLORS[0], width: 1, style: "solid", locked: false };
    }

    function finishDraft() {
      if (!draft) return;
      pushUndo();
      draft.id = uid();
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
        const d = Object.assign(defaultStyle(), { id: uid(), type: "text", time: time, price: price, text: txt });
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
      const time0 = x2t(pt.x);
      const price0 = y2p(pt.y);
      if (time0 == null || price0 == null) return;
      const sn = snapTP(time0, price0);
      if (tool !== "cursor") {
        ev.preventDefault();
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
        if (tool === "trend" || tool === "rect" || tool === "long" || tool === "short") {
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
      selectedId = hit.d.id;
      if (hit.d.locked) {
        prim.requestUpdate();
        return;
      }
      drag = {
        id: hit.d.id,
        handle: hit.handle || "body",
        t0: sn.time,
        p0: sn.price,
        clone: JSON.parse(JSON.stringify(hit.d)),
      };
      chart.applyOptions({ handleScroll: false, handleScale: false });
      prim.requestUpdate();
    }

    function onPointerMove(ev) {
      const pt = paneXY(ev);
      const time0 = x2t(pt.x);
      const price0 = y2p(pt.y);
      if (time0 == null || price0 == null) return;
      const sn = snapTP(time0, price0);
      if (draft && (tool === "trend" || tool === "rect" || tool === "long" || tool === "short")) {
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
      if (!pointerDown || !drag) return;
      const d = drawings.find(function (x) { return x.id === drag.id; });
      if (!d) return;
      const dxT = sn.time - drag.t0;
      const dxP = sn.price - drag.p0;
      Object.assign(d, JSON.parse(JSON.stringify(drag.clone)));
      applyDrag(d, drag.handle, sn.time, sn.price, dxT, dxP);
      prim.requestUpdate();
    }

    function onPointerUp() {
      pointerDown = false;
      chart.applyOptions({ handleScroll: true, handleScale: true });
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

    function onKey(ev) {
      const tag = (ev.target && ev.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (ev.key === "Escape") {
        draft = null;
        tool = "cursor";
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
        if (!undo.length) return;
        drawings = JSON.parse(undo.pop());
        selectedId = null;
        save();
        prim.requestUpdate();
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

    host.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    host.addEventListener("contextmenu", onContext);
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
        }
        if (w) {
          pushUndo();
          d.width = Number(w);
        }
        if (st) {
          pushUndo();
          d.style = st;
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

    load();
    syncToolbar();
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
      refresh: function () { prim.requestUpdate(); },
    };
  }

  global.createChartDrawings = createChartDrawings;
})(window);
