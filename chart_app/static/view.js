/* Sole owner of Lightweight Charts time scale: range, rightOffset, scroll, resize. */
(function (global) {
  const RIGHT_PAD = 48;
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
      return mute > 0 || (opts.isLoading && opts.isLoading());
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
      leftTime = Number(from);
      rightTime = Number(to);
      beginMute();
      allCharts().forEach(function (ch) {
        try {
          ch.timeScale().setVisibleRange({ from: leftTime, to: rightTime });
        } catch (e) {}
      });
      endMute();
    }

    function applyPad() {
      beginMute();
      const opt = { timeScale: { rightOffset: RIGHT_PAD, shiftVisibleRangeOnNewBar: false } };
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

    function layoutReplay(force) {
      const cursor = opts.getCursorTime && opts.getCursorTime();
      if (cursor == null) return;
      const sec = barSec();
      const from = Number(cursor) - DATA_BARS * sec;
      const to = Number(cursor);
      applyPad();
      applyTime(from, to);
      if (force) captureTime();
    }

    function cursorNearRight() {
      const cursor = opts.getCursorTime && opts.getCursorTime();
      if (cursor == null) return false;
      try {
        const ts = main().timeScale();
        const x = ts.timeToCoordinate(Number(cursor));
        const w = (opts.host && opts.host.clientWidth) || 800;
        if (x == null) return true;
        return x > w * 0.88 || x < 8;
      } catch (e) {
        return true;
      }
    }

    function keepCursorVisible() {
      if (!(opts.isFollow && opts.isFollow())) return;
      if (cursorNearRight()) layoutReplay(false);
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

    function maybeFollowOff() {
      if (!(opts.isReplay && opts.isReplay())) return;
      if (!(opts.isFollow && opts.isFollow())) return;
      const cursor = opts.getCursorTime && opts.getCursorTime();
      const vr = readRange();
      if (cursor == null || !vr) return;
      const mid = (Number(vr.from) + Number(vr.to)) / 2;
      if (Math.abs(mid - Number(cursor)) > barSec() * 6) {
        if (typeof opts.onFollowOff === "function") opts.onFollowOff();
      }
    }

    function scheduleLoadCheck() {
      if (loadTimer) clearTimeout(loadTimer);
      loadTimer = setTimeout(function () {
        loadTimer = null;
        if (isMuted()) return;
        const vr = readRange();
        if (!vr) return;
        const oldest = opts.getOldestTime && opts.getOldestTime();
        const newest = opts.getLastTime && opts.getLastTime();
        const sec = barSec();
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
      captureTime();
      if (userTimer) clearTimeout(userTimer);
      userTimer = setTimeout(function () {
        userTimer = null;
        if (isMuted()) return;
        clampReplayPan();
        maybeFollowOff();
        syncPanes();
        scheduleLoadCheck();
        if (typeof opts.onUserRange === "function") opts.onUserRange(readRange());
      }, 40);
    }

    function syncPanes() {
      const vr = leftTime != null ? { from: leftTime, to: rightTime } : readRange();
      if (!vr) return;
      beginMute();
      panes().forEach(function (ch) {
        try {
          ch.timeScale().setVisibleRange({ from: vr.from, to: vr.to });
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
        case "capture":
          captureTime();
          break;
        case "after-data":
          if (mode === "LIVE" && leftTime == null) {
            scrollLive();
          } else {
            restoreTime();
            syncPanes();
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
        case "tf-change": {
          beginMute();
          applyPad();
          if (opts.isReplay && opts.isReplay() && opts.getCursorTime) {
            layoutReplay(true);
          } else if (payload.center != null) {
            centerOn(payload.center);
          } else {
            restoreTime();
          }
          syncPanes();
          endMute();
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
        case "follow":
          layoutReplay(true);
          break;
        case "replay-step":
          keepCursorVisible();
          break;
        case "drawing":
          break;
        case "indicator":
          restoreTime();
          syncPanes();
          break;
        case "jump-live":
          mode = "LIVE";
          leftTime = null;
          rightTime = null;
          scrollLive();
          break;
        case "trade-range":
          if (payload.from != null && payload.to != null) applyTime(payload.from, payload.to);
          break;
        case "shift": {
          captureTime();
          const sec = barSec();
          const dlt = (Number(payload.delta) || -20) * sec;
          if (leftTime != null) applyTime(leftTime + dlt, rightTime + dlt);
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
      RIGHT_PAD: RIGHT_PAD,
    };
  }

  global.createViewController = createViewController;
})(window);
