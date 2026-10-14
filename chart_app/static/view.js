/* Sole owner of Lightweight Charts time scale: range, rightOffset, scroll, resize. */
(function (global) {
  const RIGHT_PAD = 48;
  const LIVE_PAD = 6;
  const DATA_BARS = 80;
  const EDGE_BARS = 20;
  const LOAD_DEBOUNCE_MS = 180;

  function createViewController(opts) {
    let mode = "LIVE";
    let leftTime = null;
    let rightTime = null;
    let mute = 0;
    let loadTimer = null;
    let userTimer = null;
    let bound = false;
    let lastFollowedBar = null;
    let freezeUntil = 0;
    let leftLogical = null;
    let rightLogical = null;
    let pinTimeFrom = null;
    let pinTimeTo = null;
    let pinSpacing = null;
    let lastRangeWidth = null;
    let tfLayoutPending = false;
    const viewLog = [];
    const FOLLOW_EDGE = 10;

    function barSec() {
      return (opts.getBarSec && opts.getBarSec()) || 60;
    }
    function main() {
      return opts.chart;
    }
    function panes() {
      const fn = opts.getPaneCharts;
      const list = typeof fn === "function" ? fn() : [];
      return (list || []).filter(Boolean);
    }
    function allCharts() {
      const out = [main()];
      panes().forEach(function (ch) {
        if (ch && out.indexOf(ch) < 0) out.push(ch);
      });
      return out;
    }
    function beginMute() {
      mute += 1;
    }
    function endMute() {
      requestAnimationFrame(function () {
        mute = Math.max(0, mute - 1);
      });
    }
    function isMuted() {
      return mute > 0 || Date.now() < freezeUntil || (opts.isLoading && opts.isLoading());
    }

    function logView(tag, extra) {
      let stack = "";
      try { stack = String(new Error().stack || ""); } catch (e) {}
      const rec = {
        tag: tag,
        left: leftTime,
        right: rightTime,
        stack: stack.split("\n").slice(0, 8).join(" | "),
      };
      if (extra) {
        Object.keys(extra).forEach(function (k) { rec[k] = extra[k]; });
      }
      viewLog.push(rec);
      if (viewLog.length > 60) viewLog.shift();
    }

    function readRange() {
      try {
        const vr = main().timeScale().getVisibleRange();
        if (vr && vr.from != null && vr.to != null) {
          leftTime = Number(vr.from);
          rightTime = Number(vr.to);
          return { from: leftTime, to: rightTime };
        }
      } catch (e) {}
      return leftTime != null ? { from: leftTime, to: rightTime } : null;
    }

    function applyTime(from, to) {
      if (from == null || to == null || !(to > from)) return;
      let a = Number(from);
      let b = Number(to);
      const width = b - a;
      const oldest = opts.getOldestTime && opts.getOldestTime();
      if (oldest != null && Number.isFinite(Number(oldest)) && a < Number(oldest) - barSec()) {
        a = Number(oldest);
        if (!(b > a)) b = a + Math.max(width, 8 * barSec());
      }
      if (!(b > a)) return;
      leftTime = a;
      rightTime = b;
      freezeUntil = Date.now() + 220;
      beginMute();
      allCharts().forEach(function (ch) {
        try {
          ch.timeScale().setVisibleRange({ from: leftTime, to: rightTime });
        } catch (e) {}
      });
      endMute();
    }

    function readBarSpacing() {
      try {
        const ts = main().timeScale();
        if (ts && typeof ts.options === "function") {
          const o = ts.options();
          if (o && o.barSpacing != null) return Number(o.barSpacing);
        }
      } catch (e) {}
      try {
        const o = main().options();
        if (o && o.timeScale && o.timeScale.barSpacing != null) return Number(o.timeScale.barSpacing);
      } catch (e2) {}
      return null;
    }

    function padBars() {
      if (opts.isReplay && opts.isReplay()) return 0;
      if (opts.isHistory && opts.isHistory()) return 0;
      return LIVE_PAD;
    }
    function applyPad() {
      beginMute();
      const opt = { timeScale: { rightOffset: padBars(), shiftVisibleRangeOnNewBar: false } };
      allCharts().forEach(function (ch) {
        try {
          if (ch.applyOptions) ch.applyOptions(opt);
        } catch (e) {}
      });
      endMute();
    }

    function restoreTime() {
      if (leftTime == null || rightTime == null) return;
      applyTime(leftTime, rightTime);
    }

    function captureTime() {
      readRange();
    }

    function captureLogical() {
      try {
        const lr = main().timeScale().getVisibleLogicalRange();
        if (lr && lr.from != null && lr.to != null) {
          leftLogical = Number(lr.from);
          rightLogical = Number(lr.to);
        }
      } catch (e) {}
    }

    function applyLogical(from, to) {
      if (from == null || to == null || !(to > from)) return;
      leftLogical = Number(from);
      rightLogical = Number(to);
      freezeUntil = Date.now() + 220;
      beginMute();
      allCharts().forEach(function (ch) {
        try {
          ch.timeScale().setVisibleLogicalRange({ from: leftLogical, to: rightLogical });
        } catch (e) {}
      });
      endMute();
      requestAnimationFrame(function () {
        if (pinTimeFrom != null) return;
        captureTime();
      });
    }

    function applyBarSpacing(px) {
      if (px == null || !Number.isFinite(Number(px)) || Number(px) <= 0) return;
      beginMute();
      allCharts().forEach(function (ch) {
        try { ch.timeScale().applyOptions({ barSpacing: Number(px) }); } catch (e) {}
      });
      endMute();
    }

    function visibleBarCount() {
      try {
        const lr = main().timeScale().getVisibleLogicalRange();
        if (lr && lr.from != null && lr.to != null) {
          return Math.max(8, Math.round(Number(lr.to) - Number(lr.from)));
        }
      } catch (e) {}
      return DATA_BARS;
    }

    function restorePinned() {
      if (pinTimeFrom != null && pinTimeTo != null) {
        applyTime(pinTimeFrom, pinTimeTo);
        applyBarSpacing(pinSpacing);
        return true;
      }
      return false;
    }

    function applyTfKeepSpacing(payload) {
      const p = payload || {};
      const spacing = p.barSpacing != null ? Number(p.barSpacing) : readBarSpacing();
      let visBars = p.visibleBars != null ? Number(p.visibleBars) : visibleBarCount();
      if (!Number.isFinite(visBars) || visBars < 8) visBars = DATA_BARS;
      let anchor = p.anchor != null ? Number(p.anchor) : null;
      if (anchor == null || !Number.isFinite(anchor)) {
        const vr = readRange();
        const last = opts.getLastTime && opts.getLastTime();
        if (vr && vr.to != null) anchor = Number(vr.to);
        else if (last != null) anchor = Number(last);
      }
      const sec = barSec();
      applyPad();
      applyBarSpacing(spacing);
      if (anchor != null && Number.isFinite(anchor) && visBars > 0) {
        applyTime(anchor - visBars * sec, anchor);
      }
      applyBarSpacing(spacing);
      captureTime();
      captureLogical();
      pinTimeFrom = leftTime;
      pinTimeTo = rightTime;
      pinSpacing = spacing != null && Number.isFinite(spacing) ? spacing : readBarSpacing();
      tfLayoutPending = false;
      lastRangeWidth = (leftTime != null && rightTime != null) ? (rightTime - leftTime) : null;
      const last = opts.getLastTime && opts.getLastTime();
      if (last != null) lastFollowedBar = last;
      logView("tf-keep-spacing", {
        spacing: pinSpacing, visBars: visBars, anchor: anchor, sec: sec,
        from: leftTime, to: rightTime,
      });
    }

    function lastLogical() {
      try {
        const last = opts.getLastTime && opts.getLastTime();
        if (last == null) return null;
        const ts = main().timeScale();
        const x = ts.timeToCoordinate(last);
        if (x == null) return null;
        return ts.coordinateToLogical(x);
      } catch (e) {
        return null;
      }
    }

    function scrollLive() {
      beginMute();
      allCharts().forEach(function (ch) {
        try {
          ch.timeScale().scrollToRealTime();
        } catch (e) {}
      });
      applyPad();
      endMute();
      requestAnimationFrame(function () {
        captureTime();
      });
    }

    function lastInView() {
      const vr = readRange();
      const last = opts.getLastTime && opts.getLastTime();
      if (last == null || !vr) return false;
      const sec = barSec();
      return Number(last) <= Number(vr.to) + 2 * sec && Number(last) >= Number(vr.from) - 2 * sec;
    }
    function layoutReplay(force) {
      const cursor = opts.getCursorTime && opts.getCursorTime();
      if (cursor == null) return;
      const last = (opts.getLastTime && opts.getLastTime()) || Number(cursor);
      lastFollowedBar = last;
      logView("layoutReplay", {
        cursor: Number(cursor), last: last, force: Boolean(force),
      });
      applyPad();
      const sec = barSec();
      if (!Number.isFinite(Number(last))) return;
      const vr = readRange();
      const wide = vr && (Number(vr.to) - Number(vr.from) >= 40 * sec);
      if (!force && lastInView() && wide) return;
      applyTime(Number(last) - DATA_BARS * sec, Number(last));
    }

    function replayStepView() {
      const cursor = opts.getCursorTime && opts.getCursorTime();
      const last = opts.getLastTime ? opts.getLastTime() : cursor;
      const follow = Boolean(opts.isFollow && opts.isFollow());
      freezeUntil = Date.now() + 180;
      if (pinTimeFrom == null || pinTimeTo == null) {
        captureTime();
        pinTimeFrom = leftTime;
        pinTimeTo = rightTime;
      }
      captureLogical();
      const holdFrom = leftLogical;
      const holdTo = rightLogical;
      const lastL = lastLogical();
      const gapBars = (holdTo != null && lastL != null)
        ? Number(holdTo) - Number(lastL)
        : FOLLOW_EDGE;
      logView("replay-step:before", {
        cursor: cursor, last: last, lastL: lastL, follow: follow, gapBars: gapBars,
        logFrom: holdFrom, logTo: holdTo,
      });
      if (!follow) {
        applyTime(pinTimeFrom, pinTimeTo);
        lastFollowedBar = last;
        return;
      }
      if (holdFrom == null || holdTo == null) {
        captureLogical();
        return;
      }
      const grew = lastFollowedBar != null && last != null && Number(last) > Number(lastFollowedBar);
      lastFollowedBar = last;
      if (!grew || gapBars >= FOLLOW_EDGE) {
        applyLogical(holdFrom, holdTo);
        logView("replay-step:hold", { gapBars: gapBars, grew: grew });
        return;
      }
      applyLogical(holdFrom + 1, holdTo + 1);
      captureTime();
      pinTimeFrom = leftTime;
      pinTimeTo = rightTime;
      logView("replay-step:shift-1", { gapBars: gapBars });
    }

    function clampReplayPan() {
      if (!(opts.isReplay && opts.isReplay())) return;
      const cursor = opts.getCursorTime && opts.getCursorTime();
      if (cursor == null) return;
      const sec = barSec();
      const vr = readRange();
      if (!vr) return;
      const maxTo = Number(cursor) + RIGHT_PAD * sec;
      if (Number(vr.to) > maxTo + sec) {
        const width = Number(vr.to) - Number(vr.from);
        applyTime(maxTo - width, maxTo);
      }
    }

    function maybeLeaveLive() {
      if (opts.isReplay && opts.isReplay()) return;
      if (opts.isHistory && opts.isHistory()) return;
      const last = opts.getLastTime && opts.getLastTime();
      const vr = readRange();
      if (last == null || !vr) return;
      const width = Number(vr.to) - Number(vr.from);
      const zoomed = lastRangeWidth != null && lastRangeWidth > 0
        && Math.abs(width - lastRangeWidth) / lastRangeWidth > 0.06;
      lastRangeWidth = width;
      if (zoomed) return;
      if (Number(vr.to) < Number(last) - barSec() * 2) {
        if (typeof opts.onLeaveLive === "function") opts.onLeaveLive();
      }
    }

    function maybeFollowOff() {
      if (!(opts.isReplay && opts.isReplay())) return;
      if (!(opts.isFollow && opts.isFollow())) return;
      const cursor = opts.getCursorTime && opts.getCursorTime();
      const vr = readRange();
      if (cursor == null || !vr) return;
      const c = Number(cursor);
      if (c < Number(vr.from) - barSec() || c > Number(vr.to) + barSec()) {
        if (typeof opts.onFollowOff === "function") opts.onFollowOff();
      }
    }

    function scheduleLoadCheck() {
      if (loadTimer) clearTimeout(loadTimer);
      loadTimer = setTimeout(function () {
        loadTimer = null;
        if (isMuted()) return;
        if (opts.isReplay && opts.isReplay() && Date.now() < freezeUntil) return;
        const vr = readRange();
        if (!vr) return;
        const oldest = opts.getOldestTime && opts.getOldestTime();
        const newest = opts.getLastTime && opts.getLastTime();
        const sec = barSec();
        if (opts.isReplay && opts.isReplay()) {
          return;
        }
        if (oldest != null && vr.from <= oldest + EDGE_BARS * sec) {
          if (typeof opts.onNeedOlder === "function") opts.onNeedOlder();
        }
        if (!(opts.isReplay && opts.isReplay()) && opts.isHistory && opts.isHistory()) {
          if (newest != null && vr.to >= newest - EDGE_BARS * sec) {
            if (typeof opts.onNeedNewer === "function") opts.onNeedNewer();
          }
        }
      }, LOAD_DEBOUNCE_MS);
    }

    function onUserRange() {
        if (isMuted()) return;
        if (opts.isReplay && opts.isReplay() && Date.now() < freezeUntil) return;
        captureTime();
        captureLogical();
        if (userTimer) clearTimeout(userTimer);
      userTimer = setTimeout(function () {
        userTimer = null;
        if (isMuted()) return;
        clampReplayPan();
        maybeFollowOff();
        maybeLeaveLive();
        syncPanes();
        scheduleLoadCheck();
        if (typeof opts.onUserRange === "function") opts.onUserRange(readRange());
      }, 40);
    }

    function syncPanes() {
      beginMute();
      panes().forEach(function (ch) {
        try {
          if (leftLogical != null && rightLogical != null) {
            ch.timeScale().setVisibleLogicalRange({ from: leftLogical, to: rightLogical });
          } else {
            const vr = leftTime != null ? { from: leftTime, to: rightTime } : null;
            if (vr) ch.timeScale().setVisibleRange({ from: vr.from, to: vr.to });
          }
        } catch (e) {}
      });
      endMute();
    }

    function doResize(payload) {
      beginMute();
      try {
        const ch = main();
        const w = payload && payload.width;
        const h = payload && payload.height;
        if (w && h) {
          if (typeof ch.resize === "function") ch.resize(w, h);
          else ch.applyOptions({ width: w, height: h });
        }
        (payload && payload.panes ? payload.panes : []).forEach(function (p) {
          if (!p || !p.chart) return;
          try {
            if (typeof p.chart.resize === "function") p.chart.resize(p.width, p.height);
            else p.chart.applyOptions({ width: p.width, height: p.height });
          } catch (e) {}
        });
      } catch (e) {}
      endMute();
    }

    function resizeChart(ch, w, h) {
      if (!ch) return;
      beginMute();
      try {
        if (typeof ch.resize === "function") ch.resize(w, h);
        else ch.applyOptions({ width: w, height: h });
      } catch (e) {}
      endMute();
    }

    function centerOn(ts) {
      const sec = barSec();
      const c = Number(ts);
      const half = DATA_BARS * sec;
      applyTime(c - half, c + half);
    }

    function request(event, payload) {
      payload = payload || {};
      switch (event) {
        case "set-mode":
          if (payload.mode) mode = payload.mode;
          break;
        case "mute": {
          const ms = Number(payload.ms) || 220;
          freezeUntil = Date.now() + ms;
          beginMute();
          setTimeout(function () {
            if (opts.isReplay && opts.isReplay()) {
              if (!restorePinned() && leftLogical != null && rightLogical != null) {
                applyLogical(leftLogical, rightLogical);
              }
            }
            endMute();
          }, ms);
          break;
        }
        case "capture":
          captureTime();
          break;
        case "after-data":
          if (tfLayoutPending) break;
          if (opts.isReplay && opts.isReplay()) {
            break;
          }
          if (opts.isHistory && opts.isHistory()) {
            if (!restorePinned()) restoreTime();
            syncPanes();
            break;
          }
          if (mode === "LIVE" && leftTime == null) {
            scrollLive();
          }
          break;
        case "load-older-start":
        case "load-newer-start":
          captureTime();
          beginMute();
          break;
        case "load-older-end":
        case "load-newer-end":
          restoreTime();
          syncPanes();
          endMute();
          break;
        case "tf-pending":
          tfLayoutPending = true;
          break;
        case "tf-change": {
          applyTfKeepSpacing(payload);
          syncPanes();
          break;
        }
        case "resize":
          captureTime();
          doResize(payload);
          restoreTime();
          syncPanes();
          break;
        case "goto":
          if (payload.center != null) centerOn(payload.center);
          break;
        case "replay-start":
          pinTimeFrom = null;
          pinTimeTo = null;
          pinSpacing = null;
          layoutReplay(true);
          break;
        case "follow":
          captureTime();
          captureLogical();
          pinTimeFrom = leftTime;
          pinTimeTo = rightTime;
          pinSpacing = readBarSpacing();
          lastFollowedBar = (opts.getLastTime && opts.getLastTime()) || null;
          break;
        case "pin-view":
          captureTime();
          captureLogical();
          pinTimeFrom = leftTime;
          pinTimeTo = rightTime;
          pinSpacing = readBarSpacing();
          break;
        case "unpin-view":
          pinTimeFrom = null;
          pinTimeTo = null;
          pinSpacing = null;
          break;
        case "replay-step-before":
          freezeUntil = Date.now() + 180;
          if (leftLogical == null || rightLogical == null) captureLogical();
          if (leftTime == null) captureTime();
          break;
        case "replay-step":
          replayStepView();
          break;
        case "drawing":
          break;
        case "indicator":
          if (opts.isReplay && opts.isReplay()) {
            if (!restorePinned()) restoreTime();
            break;
          }
          if (opts.isHistory && opts.isHistory()) {
            if (!restorePinned()) restoreTime();
            syncPanes();
          }
          break;
        case "live-bar":
          if (opts.isReplay && opts.isReplay()) break;
          if (opts.isHistory && opts.isHistory()) break;
          if (leftTime != null && rightTime != null) {
            const liveLast = opts.getLastTime && opts.getLastTime();
            if (liveLast != null && Number(rightTime) < Number(liveLast) - 2 * barSec()) break;
          }
          scrollLive();
          break;
        case "jump-live":
          mode = "LIVE";
          leftTime = null;
          rightTime = null;
          scrollLive();
          break;
        case "history-pad":
          applyPad();
          captureTime();
          captureLogical();
          break;
        case "trade-range":
          if (payload.from != null && payload.to != null) applyTime(payload.from, payload.to);
          break;
        case "shift": {
          captureTime();
          const sec = barSec();
          const dlt = (Number(payload.delta) || -20) * sec;
          if (leftTime != null) applyTime(leftTime + dlt, rightTime + dlt);
          if (!(opts.isReplay && opts.isReplay()) && !(opts.isHistory && opts.isHistory())) {
            if (typeof opts.onLeaveLive === "function") opts.onLeaveLive();
          } else {
            maybeLeaveLive();
          }
          break;
        }
        case "zoom": {
          captureTime();
          if (leftTime == null) break;
          const mid = (leftTime + rightTime) / 2;
          const half = Math.max(8 * barSec(), (rightTime - leftTime) / 4);
          applyTime(mid - half, mid + half);
          break;
        }
        case "sync-panes":
          captureTime();
          syncPanes();
          break;
        default:
          break;
      }
    }

    function bind() {
      if (bound) return;
      bound = true;
      main().timeScale().subscribeVisibleTimeRangeChange(function () {
        onUserRange();
      });
      main().timeScale().subscribeVisibleLogicalRangeChange(function () {
        if (isMuted()) return;
        if (typeof opts.onLogical === "function") opts.onLogical();
      });
    }

    bind();
    applyPad();

    return {
      request: request,
      resizeChart: resizeChart,
      mode: function () { return mode; },
      getTimeRange: function () {
        return leftTime != null ? { from: leftTime, to: rightTime } : readRange();
      },
      getLog: function () { return viewLog.slice(); },
        RIGHT_PAD: RIGHT_PAD,
        LIVE_PAD: LIVE_PAD,
    };
  }

  global.createViewController = createViewController;
})(window);
