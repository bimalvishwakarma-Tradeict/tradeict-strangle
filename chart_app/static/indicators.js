/* TradingView-style indicator overlay. Boxes via series.attachPrimitive (draw-only, no hit steal). */
(function (global) {
  const LS_ACTIVE = "chart_app.indicators.BTCUSD";
  const LS_FAV = "chart_app.indicatorFavs";

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

  function IndPrimitive(api) {
    this.api = api;
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
    const self = this;
    this._paneView = {
      zOrder: function () {
        return "normal";
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
  IndPrimitive.prototype.attached = function (p) {
    this._chart = p.chart;
    this._series = p.series;
    this._requestUpdate = p.requestUpdate;
  };
  IndPrimitive.prototype.detached = function () {};
  IndPrimitive.prototype.updateAllViews = function () {};
  IndPrimitive.prototype.paneViews = function () {
    return [this._paneView];
  };
  IndPrimitive.prototype.requestUpdate = function () {
    if (this._requestUpdate) this._requestUpdate();
  };
  IndPrimitive.prototype._draw = function (target) {
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

  function createChartIndicators(opts) {
    const chart = opts.chart;
    const series = opts.series;
    const getTf = opts.getTf;
    const getRange = opts.getRange;
    let catalog = [];
    let favs = {};
    let instances = [];
    let catalogReady = null;

    try {
      favs = JSON.parse(lsGet(LS_FAV, "{}")) || {};
    } catch (e) {
      favs = {};
    }
    try {
      const saved = JSON.parse(lsGet(LS_ACTIVE, "[]"));
      if (Array.isArray(saved)) instances = saved;
    } catch (e) {
      instances = [];
    }

    const prim = new IndPrimitive({ render: renderAll });
    series.attachPrimitive(prim);

    function save() {
      const slim = instances.map(function (it) {
        return {
          uid: it.uid,
          id: it.id,
          params: it.params,
          visible: it.visible !== false,
          short_name: it.short_name || it.id,
        };
      });
      lsSet(LS_ACTIVE, JSON.stringify(slim));
      lsSet(LS_FAV, JSON.stringify(favs));
    }

    function catalogById(id) {
      return catalog.find(function (c) {
        return c.id === id;
      });
    }

    function t2x(t) {
      const x = chart.timeScale().timeToCoordinate(Number(t));
      return x == null ? null : x;
    }
    function p2y(p) {
      const y = series.priceToCoordinate(Number(p));
      return y == null ? null : y;
    }

    function renderAll(ctx, w, h) {
      instances.forEach(function (it) {
        if (it.visible === false) return;
        const data = it.data || {};
        (data.boxes || []).forEach(function (b) {
          const y1 = p2y(b.top);
          const y2 = p2y(b.bottom);
          let x1 = t2x(b.t1);
          if (y1 == null || y2 == null) return;
          if (x1 == null) x1 = 0;
          let x2 = b.extend_right ? w : t2x(b.t2 != null ? b.t2 : b.t1);
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
          const x = t2x(lb.t);
          const y = p2y(lb.price);
          if (x == null || y == null) return;
          ctx.font = "11px Segoe UI, sans-serif";
          ctx.fillStyle = lb.color || "#089981";
          ctx.textBaseline = "bottom";
          ctx.textAlign = lb.side === "right" ? "left" : "right";
          const tx = lb.side === "right" ? x + 6 : x - 6;
          ctx.fillText(String(lb.text || ""), tx, y - 2);
        });
      });
    }

    function paintLegend() {
      const host = document.getElementById("indLegend");
      if (!host) return;
      host.innerHTML = "";
      instances.forEach(function (it) {
        const row = document.createElement("div");
        row.className = "ind-row" + (it.visible === false ? " hidden" : "");
        row.setAttribute("data-uid", it.uid);
        const name = document.createElement("span");
        name.className = "ind-name";
        name.textContent = it.short_name || it.id;
        const vis = document.createElement("button");
        vis.type = "button";
        vis.title = "Hide/show";
        vis.textContent = it.visible === false ? "🙈" : "👁";
        vis.addEventListener("click", function (ev) {
          ev.stopPropagation();
          it.visible = it.visible === false;
          save();
          paintLegend();
          prim.requestUpdate();
        });
        const set = document.createElement("button");
        set.type = "button";
        set.title = "Settings";
        set.textContent = "⚙";
        set.addEventListener("click", function (ev) {
          ev.stopPropagation();
          openSettings(it);
        });
        const rem = document.createElement("button");
        rem.type = "button";
        rem.title = "Remove";
        rem.textContent = "🗑";
        rem.addEventListener("click", function (ev) {
          ev.stopPropagation();
          instances = instances.filter(function (x) {
            return x.uid !== it.uid;
          });
          save();
          paintLegend();
          prim.requestUpdate();
        });
        const dup = document.createElement("button");
        dup.type = "button";
        dup.title = "Duplicate";
        dup.textContent = "⋯";
        dup.addEventListener("click", function (ev) {
          ev.stopPropagation();
          addInstance(it.id, clone(it.params || {}));
        });
        row.appendChild(name);
        row.appendChild(vis);
        row.appendChild(set);
        row.appendChild(rem);
        row.appendChild(dup);
        host.appendChild(row);
      });
    }

    function fieldDefault(f) {
      return f && f.default != null ? clone(f.default) : null;
    }

    function renderFields(tabName, schema, params, mount) {
      mount.innerHTML = "";
      (schema || [])
        .filter(function (f) {
          return (f.tab || "inputs") === tabName;
        })
        .forEach(function (f) {
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
            cb.addEventListener("change", function () {
              params[f.key] = cb.checked;
            });
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
            sel.addEventListener("change", function () {
              params[f.key] = sel.value;
            });
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
            op.value = String(transpOf(cur, 70));
            const opLab = document.createElement("span");
            opLab.textContent = op.value + "% transp";
            function syncCol() {
              params[f.key] = { hex: col.value, transparency: Number(op.value) };
              opLab.textContent = op.value + "% transp";
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
            if (f.step != null) inp.step = String(f.step);
            else inp.step = typ === "int" ? "1" : "0.1";
            inp.value = params[f.key] != null ? params[f.key] : fieldDefault(f);
            inp.addEventListener("change", function () {
              let n = typ === "int" ? parseInt(inp.value, 10) : parseFloat(inp.value);
              if (!Number.isFinite(n)) n = fieldDefault(f);
              if (f.min != null) n = Math.max(Number(f.min), n);
              if (f.max != null) n = Math.min(Number(f.max), n);
              params[f.key] = n;
              inp.value = String(n);
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
      document.getElementById("indTabInputs").onclick = function () {
        tab = "inputs";
        showTab();
      };
      document.getElementById("indTabStyle").onclick = function () {
        tab = "style";
        showTab();
      };
      renderFields("inputs", schema, draft, bodyIn);
      renderFields("style", schema, draft, bodySt);
      showTab();
      dlg.classList.add("open");
      document.getElementById("indDlgCancel").onclick = function () {
        dlg.classList.remove("open");
      };
      document.getElementById("indDlgDefaults").onclick = function () {
        const d = defaultParams(schema);
        Object.keys(draft).forEach(function (k) {
          delete draft[k];
        });
        Object.keys(d).forEach(function (k) {
          draft[k] = d[k];
        });
        renderFields("inputs", schema, draft, bodyIn);
        renderFields("style", schema, draft, bodySt);
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
          nm.textContent = c.name;
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
      if (search) {
        search.value = "";
        search.focus();
      }
      renderCatalog("");
    }

    async function ensureCatalog() {
      if (catalogReady) return catalogReady;
      catalogReady = fetch("/api/indicators")
        .then(function (r) {
          return r.json();
        })
        .then(function (j) {
          catalog = j.indicators || [];
          return catalog;
        })
        .catch(function () {
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
        short_name: rec ? rec.short_name : id,
        data: { boxes: [], labels: [] },
      };
      instances.push(it);
      save();
      paintLegend();
      await reloadOne(it);
    }

    async function reloadOne(it) {
      const range = getRange ? getRange() : null;
      if (!range) return;
      const tf = getTf ? getTf() : "1m";
      const q = new URLSearchParams({
        tf: String(tf),
        params: JSON.stringify(it.params || {}),
      });
      if (range.from != null) q.set("from", String(range.from));
      if (range.to != null) q.set("to", String(range.to));
        it._gen = (it._gen || 0) + 1;
        const my = it._gen;
        try {
        const r = await fetch("/api/indicator/" + encodeURIComponent(it.id) + "?" + q.toString());
        const j = await r.json();
        if (my !== it._gen) return;
        it.data = j;
        if (j.short_name) it.short_name = j.short_name;
        save();
        paintLegend();
        prim.requestUpdate();
      } catch (e) {}
    }

    async function reloadAll() {
      await ensureCatalog();
      if (!instances.length) {
        paintLegend();
        prim.requestUpdate();
        return;
      }
      await Promise.all(
        instances.map(function (it) {
          return reloadOne(it);
        })
      );
    }

    function refresh() {
      prim.requestUpdate();
    }

    const legendHost = document.getElementById("indLegend");
    if (legendHost) {
      ["pointerdown", "pointerup", "click", "dblclick", "contextmenu"].forEach(function (evn) {
        legendHost.addEventListener(evn, function (ev) {
          ev.stopPropagation();
        });
      });
    }

    const btn = document.getElementById("btnIndicators");
    if (btn) btn.addEventListener("click", function () {
      ensureCatalog().then(openPicker);
    });
    const search = document.getElementById("indSearch");
    if (search) {
      search.addEventListener("input", function () {
        renderCatalog(search.value);
      });
    }
    const closeBtn = document.getElementById("indModalClose");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        document.getElementById("indModal").classList.remove("open");
      });
    }
    const modal = document.getElementById("indModal");
    if (modal) {
      modal.addEventListener("click", function (ev) {
        if (ev.target === modal) modal.classList.remove("open");
      });
    }

    ensureCatalog().then(function () {
      paintLegend();
      reloadAll();
    });

    return {
      reload: reloadAll,
      refresh: refresh,
      list: function () {
        return instances;
      },
    };
  }

  global.createChartIndicators = createChartIndicators;
})(window);
