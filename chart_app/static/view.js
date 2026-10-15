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
    let pinFromL = null;
    let pinToL = null;
    let pinFromT = null;
    let pinToT = null;
    let pinLastIndex = null;
    let pinOldest = null;
    let pinSpacing = null;
    let lastRangeWidth = null;
    let tfLayoutPending = false;
    const viewLog = [];
    const FOLLOW_EDGE = 10;
    window.__caModeLog = window.__caModeLog || [];

    function setMode(next, why) {
      const prev = mode;
      if (prev === next) return;
      mode = next;
      const rec = { from: prev, to: next, why: String(why || "") };
      logView("mode", rec);
      window.__caModeLog.push(rec);
      if (window.__caModeLog.length > 40) window.__caModeLog.shift();
    }

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
    function times() {
      const fn = opts.getTimes;
      const arr = typeof fn === "function" ? fn() : [];
      return arr || [];
    }
    function lastIndex() {
      const arr = times();
      return arr.length ? arr.length - 1 : 0;
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

    function timeToLogical(t) {
      const arr = times();
      const sec = barSec();
      if (!arr.length || t == null || !Number.isFinite(Number(t))) return null;
      const tt = Number(t);
      const lastI = arr.length - 1;
      const lastT = Number(arr[lastI]);
      if (tt > lastT) return lastI + (tt - lastT) / sec;
      const firstT = Number(arr[0]);
      if (tt <= firstT) return (tt - firstT) / sec;
      let lo = 0;
      let hi = lastI;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (Number(arr[mid]) <= tt) lo = mid;
        else hi = mid - 1;
      }
      const t0 = Number(arr[lo]);
      if (lo < lastI) {
        const t1 = Number(arr[lo + 1]);
        if (t1 > t0 && tt < t1) return lo + (tt - t0) / (t1 - t0);
      }
      return lo;
    }

    function logicalToTime(L) {
      const arr = times();
      if (!arr.length || L == null || !Number.isFinite(Number(L))) return null;
      const lastI = arr.length - 1;
      const lastT = Number(arr[lastI]);
      const sec = barSec();
      const x = Number(L);
      if (x >= lastI) return lastT + (x - lastI) * sec;
      if (x <= 0) return Number(arr[0]) + x * sec;
      const i0 = Math.floor(x);
      const frac = x - i0;
      const t0 = Number(arr[i0]);
      const t1 = i0 < lastI ? Number(arr[i0 + 1]) : t0 + sec;
      return t0 + frac * (t1 - t0);
    }

    function readRange() {
      if (leftLogical != null && rightLogical != null) {
        const a = logicalToTime(leftLogical);
        const b = logicalToTime(rightLogical);
        if (a != null && b != null) {
          leftTime = a;
          rightTime = b;
          return { from: leftTime, to: rightTime };
        }
      }
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

    function applyLogical(from, to) {
      if (from == null || to == null || !(to > from)) return;
      leftLogical = Number(from);
      rightLogical = Number(to);
      leftTime = logicalToTime(leftLogical);
      rightTime = logicalToTime(rightLogical);
      freezeUntil = Date.now() + 220;
      beginMute();
      allCharts().forEach(function (ch) {
        try {
          ch.timeScale().setVisibleLogicalRange({ from: leftLogical, to: rightLogical });
        } catch (e) {}
      });
      endMute();
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
      const fromL = timeToLogical(a);
      const toL = timeToLogical(b);
      if (fromL == null || toL == null || !(toL > fromL)) return;
      leftTime = a;
      rightTime = b;
      applyLogical(fromL, toL);
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

    function applyLiveRightOffset() {
      beginMute();
      const opt = { timeScale: { rightOffset: LIVE_PAD, shiftVisibleRangeOnNewBar: false } };
      allCharts().forEach(function (ch) {
        try {
          if (ch.applyOptions) ch.applyOptions(opt);
        } catch (e) {}
      });
      endMute();
    }

    function restoreTime() {
      if (leftLogical != null && rightLogical != null) {
        applyLogical(leftLogical, rightLogical);
        return;
      }
      if (leftTime == null || rightTime == null) return;
      applyTime(leftTime, rightTime);
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

    function captureTime() {
      captureLogical();
      if (leftLogical != null && rightLogical != null) {
        leftTime = logicalToTime(leftLogical);
        rightTime = logicalToTime(rightLogical);
      } else {
        readRange();
      }
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

    function capturePin() {
      captureLogical();
      if (leftLogical != null && rightLogical != null) {
        leftTime = logicalToTime(leftLogical);
        rightTime = logicalToTime(rightLogical);
      }
      pinFromL = leftLogical;
      pinToL = rightLogical;
      pinFromT = leftTime;
      pinToT = rightTime;
      pinLastIndex = lastIndex();
      pinOldest = opts.getOldestTime && opts.getOldestTime();
      pinSpacing = readBarSpacing();
    }

    function restorePinned() {
      if (pinFromL == null || pinToL == null) return false;
      applyBarSpacing(pinSpacing);
      if (opts.isReplay && opts.isReplay()) {
        applyLogical(Number(pinFromL), Number(pinToL));
        applyBarSpacing(pinSpacing);
        return true;
      }
      const oldest = opts.getOldestTime && opts.getOldestTime();
      let shift = 0;
      if (pinOldest != null && oldest != null && Number(oldest) < Number(pinOldest) - 1) {
        shift = lastIndex() - Number(pinLastIndex || 0);
      }
      applyLogical(Number(pinFromL) + shift, Number(pinToL) + shift);
      pinFromL = leftLogical;
      pinToL = rightLogical;
      pinFromT = leftTime;
      pinToT = rightTime;
      pinLastIndex = lastIndex();
      pinOldest = oldest;
      return true;
    }

    function applyTfKeepSpacing(payload) {
      const p = payload || {};
      let spacing = p.barSpacing != null ? Number(p.barSpacing) : readBarSpacing();
      if (spacing == null || !Number.isFinite(Number(spacing)) || Number(spacing) < 4 || Number(spacing) > 24) {
        spacing = 14;
      }
      let visBars = p.visibleBars != null ? Number(p.visibleBars) : visibleBarCount();
      if (!Number.isFinite(visBars) || visBars < 8) visBars = DATA_BARS;
      applyBarSpacing(spacing);
      const lastI = lastIndex();
      let lastOff = p.lastOffset;
      if (lastOff == null || !Number.isFinite(Number(lastOff))) {
        lastOff = (leftLogical != null && rightLogical != null)
          ? Number(rightLogical) - lastI
          : LIVE_PAD;
      }
      let rightL = null;
      const anchorT = (p.anchor != null && Number.isFinite(Number(p.anchor)))
        ? Number(p.anchor)
        : pinToT;
      if (anchorT != null && Number.isFinite(Number(anchorT))) {
        rightL = timeToLogical(Number(anchorT));
      }
      if (rightL == null || !Number.isFinite(Number(rightL))) {
        rightL = lastI + Number(lastOff);
      }
      const leftL = Number(rightL) - visBars;
      if (rightL > leftL) applyLogical(leftL, rightL);
      applyBarSpacing(spacing);
      captureTime();
      pinFromL = leftLogical;
      pinToL = rightLogical;
      pinFromT = leftTime;
      pinToT = rightTime;
      pinLastIndex = lastI;
      pinOldest = opts.getOldestTime && opts.getOldestTime();
      pinSpacing = spacing != null && Number.isFinite(spacing) ? spacing : readBarSpacing();
      tfLayoutPending = false;
      lastRangeWidth = (leftLogical != null && rightLogical != null) ? (rightLogical - leftLogical) : null;
      const last = opts.getLastTime && opts.getLastTime();
      if (last != null) lastFollowedBar = last;
      logView("tf-keep-spacing", {
        spacing: pinSpacing, visBars: visBars, lastOffset: lastOff, anchor: p.anchor,
        from: leftTime, to: rightTime, leftL: leftLogical, rightL: rightLogical,
      });
    }

    function lastLogical() {
      const last = opts.getLastTime && opts.getLastTime();
      if (last == null) return null;
      const byTime = timeToLogical(last);
      if (byTime != null) return byTime;
      try {
        const ts = main().timeScale();
        const x = ts.timeToCoordinate(last);
        if (x == null) return null;
        return ts.coordinateToLogical(x);
      } catch (e) {
        return null;
      }
    }

    function lastOnScreen() {
      captureLogical();
      const lastL = lastLogical();
      if (lastL == null || leftLogical == null || rightLogical == null) return false;
      return Number(lastL) >= Number(leftLogical) - 2 && Number(lastL) <= Number(rightLogical) + 2;
    }

    function lastOffRight() {
      captureLogical();
      const lastL = lastLogical();
      if (lastL == null || rightLogical == null) return false;
      return Number(lastL) > Number(rightLogical) + 2;
    }

    function syncLiveHistoryMode(why) {
      if (opts.isReplay && opts.isReplay()) return;
      if (!lastOffRight()) {
        setMode("LIVE", why || "syncLiveHistoryMode");
        if (opts.isHistory && opts.isHistory() && typeof opts.onEnterLive === "function") {
          opts.onEnterLive();
        }
      } else {
        setMode("HISTORY", why || "syncLiveHistoryMode");
        if (!(opts.isHistory && opts.isHistory()) && typeof opts.onLeaveLive === "function") {
          opts.onLeaveLive();
        }
      }
    }

    function scrollLive() {
      beginMute();
      applyLiveRightOffset();
      allCharts().forEach(function (ch) {
        try {
          ch.timeScale().scrollToRealTime();
        } catch (e) {}
      });
      endMute();
      requestAnimationFrame(function () {
        captureTime();
        setMode("LIVE", "scrollLive");
      });
    }

    function lastInView() {
      return lastOnScreen();
    }

    function layoutReplay(force) {
      const cursor = opts.getCursorTime && opts.getCursorTime();
      if (cursor == null) return;
      const last = (opts.getLastTime && opts.getLastTime()) || Number(cursor);
      lastFollowedBar = last;
      logView("layoutReplay", {
        cursor: Number(cursor), last: last, force: Boolean(force),
      });
      if (!Number.isFinite(Number(last))) return;
      captureLogical();
      const wide = leftLogical != null && rightLogical != null
        && (Number(rightLogical) - Number(leftLogical) >= 40);
      if (!force && lastInView() && wide) return;
      const lastI = lastIndex();
      applyLogical(Number(lastI) - DATA_BARS, Number(lastI));
    }

    function replayStepView() {
      const cursor = opts.getCursorTime && opts.getCursorTime();
      const last = opts.getLastTime ? opts.getLastTime() : cursor;
      const follow = Boolean(opts.isFollow && opts.isFollow());
      freezeUntil = Date.now() + 180;
      if (pinFromL == null || pinToL == null) {
        capturePin();
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
        restorePinned();
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
      applyLogical(Number(holdFrom) + 1, Number(holdTo) + 1);
      capturePin();
      logView("replay-step:shift-1", { gapBars: gapBars });
    }

    function clampReplayPan() {
      if (!(opts.isReplay && opts.isReplay())) return;
      const cursor = opts.getCursorTime && opts.getCursorTime();
      if (cursor == null) return;
      const lastL = lastLogical();
      captureLogical();
      if (lastL == null || rightLogical == null) return;
      const maxTo = Number(lastL) + RIGHT_PAD;
      if (Number(rightLogical) > maxTo + 1) {
        const width = Number(rightLogical) - Number(leftLogical);
        applyLogical(maxTo - width, maxTo);
      }
    }

    function maybeLeaveLive() {
      if (opts.isReplay && opts.isReplay()) return;
      captureLogical();
      const width = (leftLogical != null && rightLogical != null)
        ? Number(rightLogical) - Number(leftLogical)
        : null;
      const zoomed = lastRangeWidth != null && lastRangeWidth > 0 && width != null
        && Math.abs(width - lastRangeWidth) / lastRangeWidth > 0.06;
      if (width != null) lastRangeWidth = width;
      if (zoomed) return;
      syncLiveHistoryMode("maybeLeaveLive:userRange");
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
        captureLogical();
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
          if (newest != null && leftLogical != null && rightLogical != null) {
            const lastL = lastLogical();
            if (lastL != null && Number(rightLogical) >= Number(lastL) - EDGE_BARS) {
              if (typeof opts.onNeedNewer === "function") opts.onNeedNewer();
            }
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
          if (mode === "LIVE" && leftLogical == null) {
            scrollLive();
          }
          break;
        case "load-older-start":
          capturePin();
          beginMute();
          break;
        case "load-newer-start":
          capturePin();
          beginMute();
          break;
        case "load-older-end":
          restorePinned();
          syncPanes();
          endMute();
          break;
        case "load-newer-end":
          restorePinned();
          syncPanes();
          syncLiveHistoryMode();
          endMute();
          break;
        case "tf-pending":
          capturePin();
          tfLayoutPending = true;
          break;
        case "tf-change": {
          applyTfKeepSpacing(payload);
          syncPanes();
          break;
        }
        case "resize":
          capturePin();
          doResize(payload);
          restorePinned();
          syncPanes();
          break;
        case "goto":
          if (payload.center != null) centerOn(payload.center);
          break;
        case "replay-start":
          pinFromL = null;
          pinToL = null;
          pinFromT = null;
          pinToT = null;
          pinSpacing = null;
          layoutReplay(true);
          break;
        case "follow":
          capturePin();
          lastFollowedBar = (opts.getLastTime && opts.getLastTime()) || null;
          break;
        case "pin-view":
          capturePin();
          break;
        case "unpin-view":
          pinFromL = null;
          pinToL = null;
          pinFromT = null;
          pinToT = null;
          pinLastIndex = null;
          pinOldest = null;
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
        case "restore-logical":
          if (payload && payload.from != null && payload.to != null
              && Number(payload.to) > Number(payload.from)) {
            applyLogical(Number(payload.from), Number(payload.to));
          }
          break;
        case "indicator":
          if (opts.isReplay && opts.isReplay()) {
            if (!restorePinned()) restoreTime();
            break;
          }
          if (opts.isHistory && opts.isHistory()) {
            if (!restorePinned()) restoreTime();
            syncPanes();
            break;
          }
          captureLogical();
          syncPanes();
          break;
        case "live-bar": {
          if (opts.isReplay && opts.isReplay()) break;
          if (mode === "HISTORY" || (opts.isHistory && opts.isHistory())) {
            logView("live-bar:skip-history", { mode: mode });
            break;
          }
          if (userTimer) {
            captureLogical();
            if (lastOffRight()) {
              logView("live-bar:skip-user-drag", { lastL: lastLogical(), rightL: rightLogical });
              window.__caLiveBarDuringDrag = true;
              break;
            }
          }
          captureLogical();
          const lastL = lastLogical();
          const holdFrom = leftLogical;
          const holdTo = rightLogical;
          const liveLast = opts.getLastTime && opts.getLastTime();
          const offRight = lastL != null && holdTo != null
            && Number(lastL) > Number(holdTo) + 2;
          if (offRight) {
            lastFollowedBar = liveLast;
            logView("live-bar:off-right-no-shift", { lastL: lastL, holdTo: holdTo });
            break;
          }
          const prevFollow = lastFollowedBar;
          const grew = prevFollow != null && liveLast != null
            && Number(liveLast) > Number(prevFollow);
          lastFollowedBar = liveLast;
          if (prevFollow == null || !grew) break;
          if (holdFrom == null || holdTo == null) break;
          const prevL = timeToLogical(prevFollow);
          let dlt = 1;
          if (lastL != null && prevL != null) dlt = Math.max(1, Math.round(Number(lastL) - Number(prevL)));
          applyLogical(Number(holdFrom) + dlt, Number(holdTo) + dlt);
          capturePin();
          logView("live-bar:shift", { dlt: dlt, lastL: lastL });
          break;
        }
        case "jump-live": {
          setMode("LIVE", "jump-live");
          const lastI = lastIndex();
          applyLogical(lastI - DATA_BARS + LIVE_PAD, lastI + LIVE_PAD);
          applyLiveRightOffset();
          applyLogical(lastI - DATA_BARS + LIVE_PAD, lastI + LIVE_PAD);
          lastFollowedBar = opts.getLastTime && opts.getLastTime();
          if (opts.isHistory && opts.isHistory() && typeof opts.onEnterLive === "function") {
            opts.onEnterLive();
          }
          break;
        }
        case "last-mid": {
          freezeUntil = Date.now() + 450;
          captureLogical();
          const lastT = opts.getLastTime && opts.getLastTime();
          const midL = (lastT != null ? timeToLogical(lastT) : null);
          const midUse = (midL != null && Number.isFinite(Number(midL))) ? Number(midL) : lastIndex();
          let midW = (leftLogical != null && rightLogical != null)
            ? Number(rightLogical) - Number(leftLogical)
            : DATA_BARS;
          if (!Number.isFinite(midW) || midW < 16) midW = DATA_BARS;
          if (midW > 160) midW = DATA_BARS;
          applyLogical(Number(midUse) - midW / 2, Number(midUse) + midW / 2);
          capturePin();
          lastFollowedBar = lastT;
          setMode("LIVE", "last-mid");
          if (opts.isHistory && opts.isHistory() && typeof opts.onEnterLive === "function") {
            opts.onEnterLive();
          }
          break;
        }
        case "history-pad":
          captureTime();
          captureLogical();
          break;
        case "trade-range":
          if (payload.from != null && payload.to != null) applyTime(payload.from, payload.to);
          break;
        case "shift": {
          captureLogical();
          const dlt = Number(payload.delta) || -20;
          if (leftLogical != null && rightLogical != null) {
            applyLogical(Number(leftLogical) + dlt, Number(rightLogical) + dlt);
          }
          syncLiveHistoryMode("shift");
          break;
        }
        case "zoom": {
          captureLogical();
          if (leftLogical == null || rightLogical == null) break;
          const mid = (Number(leftLogical) + Number(rightLogical)) / 2;
          const half = Math.max(8, (Number(rightLogical) - Number(leftLogical)) / 4);
          applyLogical(mid - half, mid + half);
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
    beginMute();
    allCharts().forEach(function (ch) {
      try {
        if (ch.applyOptions) {
          ch.applyOptions({ timeScale: { shiftVisibleRangeOnNewBar: false } });
        }
      } catch (e) {}
    });
    endMute();

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
