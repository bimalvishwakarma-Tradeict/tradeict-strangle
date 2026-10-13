/* TradingView-style indicator overlay + extra panes. */
(function (global) {
  const LS_ACTIVE = "chart_app.indicators.BTCUSD";
  const LS_FAV = "chart_app.indicatorFavs";
  const LS_PANE_H = "chart_app.paneHeights";

  function uid() {
    return "i" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
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
  function clone(v) {
    return JSON.parse(JSON.stringify(v));
  }
  function defaultParams(schema) {
    const p = {};
    (schema || []).forEach(function (f) {
      p[f.key] = f.default != null ? clone(f.default) : null;
    });
    return p;
  }
  function hexOf(col, fb) {
    if (col && typeof col === "object" && col.hex) return String(col.hex);
    if (typeof col === "string" && col.charAt(0) === "#") return col.slice(0, 7);
    return fb || "#089981";
  }
  function transpOf(col, fb) {
    if (col && typeof col === "object" && col.transparency != null) return Number(col.transparency);
    return fb != null ? Number(fb) : 70;
  }
  function lineStyleNum(s) {
    if (s === "dotted") return 1;
    if (s === "dashed") return 2;
    return 0;
  }

  function IndPrimitive(api) {
    this.api = api;
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
    const self = this;
    this._paneView = {
      zOrder: function () { return "bottom"; },
      renderer: function () {
        return { draw: function (target) { self._draw(target); } };
      },
    };
  }
  IndPrimitive.prototype.attached = function (p) {
    this._chart = p.chart;
    this._series = p.series;
    this._requestUpdate = p.requestUpdate;
  };
  IndPrimitive.prototype.detached = function () {};
  IndPrimitive.prototype.updateAllViews = function () {};
  IndPrimitive.prototype.paneViews = function () { return [this._paneView]; };
  IndPrimitive.prototype.requestUpdate = function () {
    if (this._requestUpdate) this._requestUpdate();
  };
  IndPrimitive.prototype._draw = function (target) {
    const self = this;
    const run = function (ctx, w, h) { self.api.render(ctx, w, h); };
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

  function createChartIndicators(opts) {
    const chart = opts.chart;
    const series = opts.series;
    const getTf = opts.getTf;
    const getRange = opts.getRange;
    const paneHost = opts.paneHost || document.getElementById("paneHost");
    const priceScaleW = opts.priceScaleW || 80;
    const registerPane = opts.registerPane || function () {};
    const unregisterPane = opts.unregisterPane || function () {};
    let catalog = [];
    let favs = {};
    let instances = [];
    let catalogReady = null;
    const aux = {};
    let paneHMap = {};
    try { paneHMap = JSON.parse(lsGet(LS_PANE_H, "{}")) || {}; } catch (e) { paneHMap = {}; }

    try { favs = JSON.parse(lsGet(LS_FAV, "{}")) || {}; } catch (e) { favs = {}; }
    try {
      const saved = JSON.parse(lsGet(LS_ACTIVE, "[]"));
      if (Array.isArray(saved)) instances = saved;
    } catch (e) { instances = []; }

    const prim = new IndPrimitive({ render: renderMainFills });
    series.attachPrimitive(prim);

    function save() {
      const slim = instances.map(function (it) {
        return {
          uid: it.uid, id: it.id, params: it.params,
          visible: it.visible !== false, short_name: it.short_name || it.id,
        };
      });
      lsSet(LS_ACTIVE, JSON.stringify(slim));
      lsSet(LS_FAV, JSON.stringify(favs));
      try { lsSet(LS_PANE_H, JSON.stringify(paneHMap)); } catch (e) {}
    }

    function catalogById(id) {
      return catalog.find(function (c) { return c.id === id; });
    }
    function isOverlay(it) {
      const rec = catalogById(it.id);
      if (rec && rec.overlay === false) return false;
      if (it.overlay === false) return false;
      return true;
    }
    function t2x(t) {
      const x = chart.timeScale().timeToCoordinate(Number(t));
      return x == null ? null : x;
    }
    function p2y(p) {
      const y = series.priceToCoordinate(Number(p));
      return y == null ? null : y;
    }

    function renderMainFills(ctx, w, h) {
      instances.forEach(function (it) {
        if (it.visible === false || !isOverlay(it)) return;
        drawFills(ctx, w, h, it.data || {}, t2x, p2y);
      });
    }
    function drawFills(ctx, w, h, data, tx, py) {
      (data.boxes || []).forEach(function (b) {
        const y1 = py(b.top);
        const y2 = py(b.bottom);
        let x1 = tx(b.t1);
        if (y1 == null || y2 == null) return;
        if (x1 == null) x1 = 0;
        let x2 = b.extend_right ? w : tx(b.t2 != null ? b.t2 : b.t1);
        if (x2 == null) x2 = w;
        const left = Math.min(x1, x2);
        const right = Math.max(x1, x2);
        const top = Math.min(y1, y2);
        const bot = Math.max(y1, y2);
        ctx.fillStyle = b.fill || "rgba(8,153,129,0.3)";
        ctx.fillRect(left, top, Math.max(1, right - left), Math.max(1, bot - top));
        ctx.strokeStyle = b.border || "rgba(8,153,129,0.4)";
        ctx.lineWidth = 1;
        ctx.strokeRect(left + 0.5, top + 0.5, Math.max(1, right - left), Math.max(1, bot - top));
      });
      (data.labels || []).forEach(function (lb) {
        const x = tx(lb.t);
        const y = py(lb.price);
        if (x == null || y == null) return;
        ctx.font = "11px Trebuchet MS, sans-serif";
        ctx.fillStyle = lb.color || "#089981";
        ctx.textBaseline = "bottom";
        ctx.textAlign = lb.side === "right" ? "left" : "right";
        ctx.fillText(String(lb.text || ""), lb.side === "right" ? x + 6 : x - 6, y - 2);
      });
      (data.bands || []).forEach(function (band) {
        const up = band.upper || [];
        const lo = band.lower || [];
        const byT = {};
        lo.forEach(function (p) { if (p && p.value != null) byT[p.time] = p.value; });
        ctx.beginPath();
        let started = false;
        up.forEach(function (p) {
          if (!p || p.value == null || byT[p.time] == null) return;
          const x = tx(p.time);
          const y = py(p.value);
          if (x == null || y == null) return;
          if (!started) { ctx.moveTo(x, y); started = true; }
          else ctx.lineTo(x, y);
        });
        for (let i = up.length - 1; i >= 0; i -= 1) {
          const p = up[i];
          if (!p || byT[p.time] == null) continue;
          const x = tx(p.time);
          const y = py(byT[p.time]);
          if (x == null || y == null) continue;
          ctx.lineTo(x, y);
        }
        if (started) {
          ctx.closePath();
          ctx.fillStyle = band.fill || "rgba(33,150,243,0.08)";
          ctx.fill();
        }
      });
      const stf = data.st_fills || [];
      if (stf.length) {
        const upFill = data.st_fill_up || "rgba(8,153,129,0.12)";
        const dnFill = data.st_fill_dn || "rgba(242,54,69,0.12)";
        for (let i = 0; i < stf.length - 1; i += 1) {
          const a = stf[i];
          const b = stf[i + 1];
          const x1 = tx(a.time);
          const x2 = tx(b.time);
          const y1c = py(a.close);
          const y1l = py(a.line);
          const y2c = py(b.close);
          const y2l = py(b.line);
          if (x1 == null || x2 == null || y1c == null || y1l == null || y2c == null || y2l == null) continue;
          ctx.beginPath();
          ctx.moveTo(x1, y1c);
          ctx.lineTo(x2, y2c);
          ctx.lineTo(x2, y2l);
          ctx.lineTo(x1, y1l);
          ctx.closePath();
          ctx.fillStyle = a.up ? upFill : dnFill;
          ctx.fill();
        }
      }
    }

    function applyLineSeries(it, hostChart) {
      const specs = ((it.data || {}).series) || [];
      if (it._lwcHost && it._lwcHost !== hostChart) {
        destroyLineSeries(it, it._lwcHost);
      }
      it._lwcHost = hostChart;
      if (!it._lwc) it._lwc = {};
      const seen = {};
      specs.forEach(function (sp) {
        const key = sp.key || "s";
        seen[key] = true;
        const opts = {
          color: sp.color || "#2962ff",
          lineWidth: Number(sp.width) || 1,
          lineStyle: lineStyleNum(sp.style),
          priceLineVisible: false,
          lastValueVisible: Boolean(sp.lastValueVisible) && it.visible !== false,
          visible: it.visible !== false,
        };
        if (!it._lwc[key]) it._lwc[key] = hostChart.addLineSeries(opts);
        else it._lwc[key].applyOptions(opts);
        const range = getRange ? getRange() : null;
        const capTo = range && range.to != null ? Number(range.to) : null;
        const pts = (sp.points || []).filter(function (p) {
          if (!p || p.time == null) return false;
          if (capTo != null && Number(p.time) > capTo) return false;
          return true;
        });
        it._lwc[key].setData(pts);
        if (sp.markers) {
          try { it._lwc[key].setMarkers(sp.markers); } catch (e) {}
        }
      });
      Object.keys(it._lwc).forEach(function (k) {
        if (!seen[k]) {
          try { hostChart.removeSeries(it._lwc[k]); } catch (e) {}
          delete it._lwc[k];
        }
      });
    }
    function destroyLineSeries(it, hostChart) {
      if (!it._lwc) return;
      Object.keys(it._lwc).forEach(function (k) {
        try { hostChart.removeSeries(it._lwc[k]); } catch (e) {}
      });
      it._lwc = {};
      if (it._lwcHost === hostChart) it._lwcHost = null;
    }

    function paneChartOpts(height) {
      return {
        layout: { background: { color: "#131722" }, textColor: "#787b86" },
        grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
        rightPriceScale: { borderColor: "#2a2e39", minimumWidth: priceScaleW },
        timeScale: { visible: false, rightOffset: 48, shiftVisibleRangeOnNewBar: false },
        crosshair: {
          mode: LightweightCharts.CrosshairMode.Normal,
          vertLine: { color: "#758696", labelBackgroundColor: "#363a45" },
          horzLine: { color: "#758696", labelBackgroundColor: "#363a45" },
        },
        height: height || 132,
      };
    }

    function attachPaneFill(it) {
      if (!it._pane || !it._pane.chart || it._pane.fillPrim) return;
      const hostChart = it._pane.chart;
      const pySeries = it._lwc && (it._lwc.rsi || it._lwc[Object.keys(it._lwc)[0]]);
      if (!pySeries || typeof pySeries.attachPrimitive !== "function") return;
      const rec = it;
      const primP = new IndPrimitive({
        render: function (ctx, w, h) {
          const data = rec.data || {};
          (data.fills || []).forEach(function (f) {
            if (f.kind !== "hband") return;
            const y1 = pySeries.priceToCoordinate(Number(f.top));
            const y2 = pySeries.priceToCoordinate(Number(f.bottom));
            if (y1 == null || y2 == null) return;
            ctx.fillStyle = f.fill || "rgba(126,87,194,0.08)";
            ctx.fillRect(0, Math.min(y1, y2), w, Math.abs(y2 - y1));
          });
        },
      });
      pySeries.attachPrimitive(primP);
      it._pane.fillPrim = primP;
    }

    function ensurePane(it) {
      if (it._pane) return it._pane;
      if (!paneHost) return null;
      const wrap = document.createElement("div");
      wrap.className = "ind-pane";
      wrap.setAttribute("data-uid", it.uid);
      const h = Number(paneHMap[it.uid] || 132);
      wrap.style.height = h + "px";
      const legend = document.createElement("div");
      legend.className = "ind-pane-legend";
      const chartEl = document.createElement("div");
      chartEl.className = "ind-pane-chart";
      const rz = document.createElement("div");
      rz.className = "ind-pane-resizer";
      wrap.appendChild(legend);
      wrap.appendChild(chartEl);
      wrap.appendChild(rz);
      paneHost.appendChild(wrap);
      const ch = LightweightCharts.createChart(chartEl, paneChartOpts(h));
      it._pane = { wrap: wrap, chart: ch, legend: legend, chartEl: chartEl };
      bindResize(it, rz, wrap);
      registerPane({
        id: it.uid,
        chart: ch,
        host: wrap,
        mainSeries: null,
        valueAt: function (t) { return valueAtInstance(it, t); },
      });
      return it._pane;
    }
    function bindResize(it, rz, wrap) {
      rz.addEventListener("pointerdown", function (ev) {
        ev.preventDefault();
        const startY = ev.clientY;
        const startH = wrap.getBoundingClientRect().height;
        function move(e) {
          const nh = Math.max(70, Math.min(360, startH + (e.clientY - startY)));
          wrap.style.height = nh + "px";
          paneHMap[it.uid] = nh;
          if (it._pane && it._pane.chart) {
            if (typeof opts.resizeChart === "function") opts.resizeChart(it._pane.chart, wrap.clientWidth, nh - 6);
            else it._pane.chart.applyOptions({ width: wrap.clientWidth, height: nh - 6 });
          }
          if (opts.onResize) opts.onResize();
        }
        function up() {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          save();
          if (opts.onResize) opts.onResize();
        }
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
    }
    function destroyPane(it) {
      if (!it._pane) return;
      destroyLineSeries(it, it._pane.chart);
      unregisterPane(it.uid);
      try { it._pane.wrap.remove(); } catch (e) {}
      it._pane = null;
    }
    function paintPaneLegend(it) {
      if (!it._pane || !it._pane.legend) return;
      const host = it._pane.legend;
      host.innerHTML = "";
      const row = document.createElement("div");
      row.className = "ind-row" + (it.visible === false ? " hidden" : "");
      const name = document.createElement("span");
      name.className = "ind-name";
      name.textContent = it.short_name || it.id;
      row.appendChild(name);
      function icobtn(title, txt, fn) {
        const b = document.createElement("button");
        b.type = "button";
        b.title = title;
        b.textContent = txt;
        b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
        return b;
      }
      row.appendChild(icobtn("Hide/show", it.visible === false ? "🙈" : "👁", function () {
        it.visible = it.visible === false;
        save();
        applyVisuals(it);
      }));
      row.appendChild(icobtn("Settings", "⚙", function () {
        if (it._aux && opts.onAuxSettings) opts.onAuxSettings(it);
        else openSettings(it);
      }));
      row.appendChild(icobtn("Remove", "🗑", function () {
        if (it._aux && opts.onAuxRemove) opts.onAuxRemove(it);
        else removeInstance(it.uid);
      }));
      host.appendChild(row);
    }
    function valueAtInstance(it, t) {
      const specs = ((it.data || {}).series) || [];
      const pts = specs.length ? (specs[0].points || []) : [];
      const n = Number(t);
      for (let i = 0; i < pts.length; i += 1) {
        if (Number(pts[i].time) === n && pts[i].value != null) return Number(pts[i].value);
      }
      return 0;
    }

    function applyVisuals(it) {
      const ov = isOverlay(it);
      if (ov) {
        if (it._pane) destroyPane(it);
        applyLineSeries(it, chart);
        prim.requestUpdate();
      } else {
        if (it._lwcHost === chart) destroyLineSeries(it, chart);
        if (it.visible === false) {
          if (it._pane) it._pane.wrap.classList.add("hidden");
          if (it._lwcHost) {
            Object.keys(it._lwc || {}).forEach(function (k) {
              try { it._lwc[k].applyOptions({ visible: false }); } catch (e) {}
            });
          }
        } else {
          const pane = ensurePane(it);
          if (pane) {
            pane.wrap.classList.remove("hidden");
            applyLineSeries(it, pane.chart);
            attachPaneFill(it);
            paintPaneLegend(it);
            const keys = it._lwc ? Object.keys(it._lwc) : [];
            registerPane({
              id: it.uid,
              chart: pane.chart,
              host: pane.wrap,
              mainSeries: keys.length ? it._lwc[keys[0]] : null,
              valueAt: function (t) { return valueAtInstance(it, t); },
            });
            resizeOne(it);
          }
        }
      }
      paintLegend();
    }
    function resizeOne(it) {
      if (!it._pane) return;
      const wrap = it._pane.wrap;
      const h = wrap.clientHeight || 132;
      const w = wrap.clientWidth;
      const nh = Math.max(60, h - 6);
      try {
        if (typeof opts.resizeChart === "function") opts.resizeChart(it._pane.chart, w, nh);
        else if (typeof it._pane.chart.resize === "function") it._pane.chart.resize(w, nh);
        else it._pane.chart.applyOptions({ width: w, height: nh });
      } catch (e) {}
      if (it._pane.fillPrim) it._pane.fillPrim.requestUpdate();
    }

    function paintLegend() {
      const host = document.getElementById("indLegend");
      if (!host) return;
      host.innerHTML = "";
      instances.forEach(function (it) {
        if (!isOverlay(it)) return;
        const row = document.createElement("div");
        row.className = "ind-row" + (it.visible === false ? " hidden" : "");
        const name = document.createElement("span");
        name.className = "ind-name";
        name.textContent = it.short_name || it.id;
        function icobtn(title, txt, fn) {
          const b = document.createElement("button");
          b.type = "button";
          b.title = title;
          b.textContent = txt;
          b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
          return b;
        }
        row.appendChild(name);
        row.appendChild(icobtn("Hide/show", it.visible === false ? "🙈" : "👁", function () {
          it.visible = it.visible === false;
          save();
          applyVisuals(it);
        }));
        row.appendChild(icobtn("Settings", "⚙", function () { openSettings(it); }));
        row.appendChild(icobtn("Remove", "🗑", function () { removeInstance(it.uid); }));
        row.appendChild(icobtn("Duplicate", "⋯", function () {
          addInstance(it.id, clone(it.params || {}));
        }));
        host.appendChild(row);
      });
    }

    function removeInstance(id) {
      const it = instances.find(function (x) { return x.uid === id; });
      instances = instances.filter(function (x) { return x.uid !== id; });
      if (it) {
        if (isOverlay(it)) destroyLineSeries(it, chart);
        destroyPane(it);
      }
      save();
      paintLegend();
      prim.requestUpdate();
      if (opts.onResize) opts.onResize();
    }

    function fieldDefault(f) {
      return f && f.default != null ? clone(f.default) : null;
    }
    function renderFields(tabName, schema, params, mount, onLive) {
      mount.innerHTML = "";
      (schema || []).filter(function (f) { return (f.tab || "inputs") === tabName; }).forEach(function (f) {
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
          cb.addEventListener("change", function () { params[f.key] = cb.checked; onLive(); });
          row.appendChild(cb);
        } else if (typ === "select") {
          const sel = document.createElement("select");
          (f.options || []).forEach(function (op) {
            const o = document.createElement("option");
            o.value = String(op.value != null ? op.value : op);
            o.textContent = String(op.label != null ? op.label : op);
            sel.appendChild(o);
          });
          sel.value = String(params[f.key]);
          sel.addEventListener("change", function () { params[f.key] = sel.value; onLive(); });
          row.appendChild(sel);
        } else if (typ === "color") {
          const cur = params[f.key] && typeof params[f.key] === "object"
            ? params[f.key]
            : { hex: hexOf(params[f.key], "#089981"), transparency: 70 };
          const col = document.createElement("input");
          col.type = "color";
          col.value = hexOf(cur, "#089981");
          const op = document.createElement("input");
          op.type = "range";
          op.min = "0";
          op.max = "100";
          op.value = String(transpOf(cur, 0));
          const opLab = document.createElement("span");
          opLab.textContent = op.value + "% transp";
          function syncCol() {
            params[f.key] = { hex: col.value, transparency: Number(op.value) };
            opLab.textContent = op.value + "% transp";
            onLive();
          }
          col.addEventListener("input", syncCol);
          op.addEventListener("input", syncCol);
          row.appendChild(col);
          row.appendChild(op);
          row.appendChild(opLab);
        } else {
          const inp = document.createElement("input");
          inp.type = "number";
          if (f.min != null) inp.min = String(f.min);
          if (f.max != null) inp.max = String(f.max);
          inp.step = f.step != null ? String(f.step) : (typ === "int" ? "1" : "0.1");
          inp.value = params[f.key] != null ? params[f.key] : fieldDefault(f);
          inp.addEventListener("change", function () {
            let n = typ === "int" ? parseInt(inp.value, 10) : parseFloat(inp.value);
            if (!Number.isFinite(n)) n = fieldDefault(f);
            if (f.min != null) n = Math.max(Number(f.min), n);
            if (f.max != null) n = Math.min(Number(f.max), n);
            params[f.key] = n;
            inp.value = String(n);
            onLive();
          });
          row.appendChild(inp);
        }
        mount.appendChild(row);
      });
    }

    function openSettings(it) {
      const rec = catalogById(it.id);
      const schema = rec ? rec.inputs : [];
      const dlg = document.getElementById("indDialog");
      const bodyIn = document.getElementById("indDlgInputs");
      const bodySt = document.getElementById("indDlgStyle");
      const title = document.getElementById("indDlgTitle");
      if (!dlg || !bodyIn || !bodySt) return;
      const draft = clone(it.params || defaultParams(schema));
      if (title) title.textContent = (rec && rec.name) || it.id;
      let tab = "inputs";
      function showTab() {
        document.getElementById("indTabInputs").classList.toggle("active", tab === "inputs");
        document.getElementById("indTabStyle").classList.toggle("active", tab === "style");
        bodyIn.hidden = tab !== "inputs";
        bodySt.hidden = tab !== "style";
      }
      document.getElementById("indTabInputs").onclick = function () { tab = "inputs"; showTab(); };
      document.getElementById("indTabStyle").onclick = function () { tab = "style"; showTab(); };
      let liveTimer = null;
      function onLive() {
        it.params = clone(draft);
        save();
        if (liveTimer) clearTimeout(liveTimer);
        liveTimer = setTimeout(function () { reloadOne(it); }, 180);
      }
      renderFields("inputs", schema, draft, bodyIn, onLive);
      renderFields("style", schema, draft, bodySt, onLive);
      showTab();
      dlg.classList.add("open");
      document.getElementById("indDlgCancel").onclick = function () { dlg.classList.remove("open"); };
      document.getElementById("indDlgDefaults").onclick = function () {
        const d = defaultParams(schema);
        Object.keys(draft).forEach(function (k) { delete draft[k]; });
        Object.keys(d).forEach(function (k) { draft[k] = d[k]; });
        renderFields("inputs", schema, draft, bodyIn, onLive);
        renderFields("style", schema, draft, bodySt, onLive);
        onLive();
      };
      document.getElementById("indDlgOk").onclick = function () {
        it.params = clone(draft);
        dlg.classList.remove("open");
        save();
        reloadOne(it);
      };
    }

    function renderCatalog(filter) {
      const list = document.getElementById("indList");
      if (!list) return;
      const q = String(filter || "").trim().toLowerCase();
      list.innerHTML = "";
      catalog
        .filter(function (c) {
          if (!q) return true;
          return (c.name + " " + c.id + " " + (c.description || "")).toLowerCase().indexOf(q) >= 0;
        })
        .sort(function (a, b) {
          const fa = favs[a.id] ? 0 : 1;
          const fb = favs[b.id] ? 0 : 1;
          return fa - fb || String(a.name).localeCompare(String(b.name));
        })
        .forEach(function (c) {
          const row = document.createElement("div");
          row.className = "ind-cat-row";
          const star = document.createElement("button");
          star.type = "button";
          star.className = "ind-star" + (favs[c.id] ? " on" : "");
          star.textContent = "★";
          star.addEventListener("click", function (ev) {
            ev.stopPropagation();
            favs[c.id] = !favs[c.id];
            save();
            renderCatalog(document.getElementById("indSearch").value);
          });
          const meta = document.createElement("div");
          meta.className = "ind-cat-meta";
          const nm = document.createElement("div");
          nm.className = "ind-cat-name";
          nm.textContent = c.name + (c.overlay === false ? "  (pane)" : "");
          const ds = document.createElement("div");
          ds.className = "ind-cat-desc";
          ds.textContent = c.description || "";
          meta.appendChild(nm);
          meta.appendChild(ds);
          row.appendChild(star);
          row.appendChild(meta);
          row.addEventListener("click", function () {
            addInstance(c.id, defaultParams(c.inputs));
            document.getElementById("indModal").classList.remove("open");
          });
          list.appendChild(row);
        });
    }

    function openPicker() {
      const modal = document.getElementById("indModal");
      if (!modal) return;
      modal.classList.add("open");
      const search = document.getElementById("indSearch");
      if (search) { search.value = ""; search.focus(); }
      renderCatalog("");
    }

    async function ensureCatalog() {
      if (catalogReady) return catalogReady;
      catalogReady = fetch("/api/indicators").then(function (r) { return r.json(); }).then(function (j) {
        catalog = j.indicators || [];
        return catalog;
      }).catch(function () {
        catalog = [];
        catalogReady = null;
        return catalog;
      });
      return catalogReady;
    }

    async function addInstance(id, params) {
      await ensureCatalog();
      const rec = catalogById(id);
      const it = {
        uid: uid(),
        id: id,
        params: params || defaultParams(rec ? rec.inputs : []),
        visible: true,
        overlay: rec ? rec.overlay !== false : true,
        short_name: rec ? rec.short_name : id,
        data: { boxes: [], labels: [], series: [] },
      };
      instances.push(it);
      save();
      paintLegend();
      await reloadOne(it);
    }

    async function reloadOne(it) {
      if (it._aux) {
        applyVisuals(it);
        return;
      }
      const range = getRange ? getRange() : null;
      if (!range) return;
      const tf = getTf ? getTf() : "1m";
      const pjson = JSON.stringify(it.params || {});
      if (it._cached && it._cached.params === pjson && Number(it._cached.to) === Number(range.to) && Number(it._cached.from) <= Number(range.from) && it._cached.tf === tf) {
        applyVisuals(it);
        if (opts.onIndTiming) opts.onIndTiming(it._cached.server_ms || 0, true);
        return;
      }
      const q = new URLSearchParams({ tf: String(tf), params: JSON.stringify(it.params || {}) });
      if (range.from != null) q.set("from", String(range.from));
      if (range.to != null) q.set("to", String(range.to));
      it._gen = (it._gen || 0) + 1;
      const my = it._gen;
      if (it._abort) {
        try { it._abort.abort(); } catch (e) {}
      }
      it._abort = typeof AbortController === "function" ? new AbortController() : null;
      const row = document.querySelector('#indLegend .ind-row');
      if (row) row.classList.add("loading");
      try {
        const r = await fetch("/api/indicator/" + encodeURIComponent(it.id) + "?" + q.toString(), it._abort ? { signal: it._abort.signal } : {});
        const j = await r.json();
        if (my !== it._gen) return;
        it.data = j;
        it._cached = {
          from: Number(range.from),
          to: Number(range.to),
          tf: tf,
          params: pjson,
          server_ms: Number(j.server_ms) || 0,
        };
        if (opts.onIndTiming) opts.onIndTiming(Number(j.server_ms) || 0, Boolean(j.cache_hit));
        if (j.short_name) it.short_name = j.short_name;
        const rec = catalogById(it.id);
        if (rec) it.overlay = rec.overlay !== false;
        save();
        applyVisuals(it);
      } catch (e) {
        if (e && e.name === "AbortError") return;
      } finally {
        document.querySelectorAll("#indLegend .ind-row.loading").forEach(function (n) { n.classList.remove("loading"); });
      }
    }

    async function reloadAll() {
      await ensureCatalog();
      paintLegend();
      await Promise.all(instances.map(function (it) { return reloadOne(it); }));
      Object.keys(aux).forEach(function (k) { applyVisuals(aux[k]); });
    }

    function setAuxPane(id, spec) {
      if (!spec || spec.visible === false) {
        const old = aux[id];
        if (old) {
          destroyPane(old);
          delete aux[id];
          if (opts.onResize) opts.onResize();
        }
        return;
      }
      let it = aux[id];
      let created = false;
      if (!it) {
        created = true;
        it = {
          uid: "aux-" + id,
          id: id,
          overlay: false,
          visible: true,
          short_name: spec.title || id,
          params: {},
          data: {},
          _aux: true,
        };
        aux[id] = it;
      }
      it.visible = true;
      it.short_name = spec.title || it.short_name;
      it.data = {
        series: spec.lines || spec.series || [],
        fills: spec.fills || [],
        bands: [],
      };
      applyVisuals(it);
      if (created && opts.onResize) opts.onResize();
    }

    function paneCharts() {
      const out = [];
      instances.forEach(function (it) {
        if (it._pane && it._pane.chart) {
          out.push({
            id: it.uid,
            chart: it._pane.chart,
            host: it._pane.wrap,
            mainSeries: it._lwc && it._lwc[Object.keys(it._lwc)[0]],
            valueAt: function (t) { return valueAtInstance(it, t); },
          });
        }
      });
      Object.keys(aux).forEach(function (k) {
        const it = aux[k];
        if (it._pane && it._pane.chart) {
          out.push({
            id: it.uid,
            chart: it._pane.chart,
            host: it._pane.wrap,
            mainSeries: it._lwc && it._lwc[Object.keys(it._lwc)[0]],
            valueAt: function (t) { return valueAtInstance(it, t); },
          });
        }
      });
      return out;
    }

    function refresh() {
      prim.requestUpdate();
      instances.concat(Object.keys(aux).map(function (k) { return aux[k]; })).forEach(function (it) {
        if (it._pane && it._pane.fillPrim) it._pane.fillPrim.requestUpdate();
        resizeOne(it);
      });
    }

    const legendHost = document.getElementById("indLegend");
    if (legendHost) {
      ["pointerdown", "pointerup", "click", "dblclick", "contextmenu"].forEach(function (evn) {
        legendHost.addEventListener(evn, function (ev) { ev.stopPropagation(); });
      });
    }
    const btn = document.getElementById("btnIndicators");
    if (btn) btn.addEventListener("click", function () { ensureCatalog().then(openPicker); });
    const search = document.getElementById("indSearch");
    if (search) search.addEventListener("input", function () { renderCatalog(search.value); });
    const closeBtn = document.getElementById("indModalClose");
    if (closeBtn) closeBtn.addEventListener("click", function () {
      document.getElementById("indModal").classList.remove("open");
    });
    const modal = document.getElementById("indModal");
    if (modal) modal.addEventListener("click", function (ev) {
      if (ev.target === modal) modal.classList.remove("open");
    });

    ensureCatalog().then(function () { paintLegend(); reloadAll(); });

    return {
      reload: reloadAll,
      refresh: refresh,
      list: function () { return instances; },
      paneCharts: paneCharts,
      setAuxPane: setAuxPane,
      renderCatalog: renderCatalog,
      add: addInstance,
      removeLast: function () {
        if (!instances.length) return;
        removeInstance(instances[instances.length - 1].uid);
      },
    };
  }

  global.createChartIndicators = createChartIndicators;
})(window);
