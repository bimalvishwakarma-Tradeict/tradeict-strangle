import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
} from 'lightweight-charts'

const BG = '#0b1220'
const GRID = '#1f2937'
const TEXT = '#9ca3af'

function filterPoints(series) {
  if (!Array.isArray(series)) return []
  return series
    .filter((p) => p && p.value != null && Number.isFinite(Number(p.value)))
    .map((p) => ({ time: Number(p.time), value: Number(p.value) }))
}

function modeInitial(mode) {
  const m = String(mode || '').toUpperCase()
  if (m.startsWith('E')) return 'E'
  return 'R'
}

function syncCharts(charts) {
  const cleanups = []
  charts.forEach((source, i) => {
    const handler = (range) => {
      if (!range) return
      charts.forEach((target, j) => {
        if (i === j) return
        try {
          target.timeScale().setVisibleLogicalRange(range)
        } catch {
          /* ignore */
        }
      })
    }
    source.timeScale().subscribeVisibleLogicalRangeChange(handler)
    cleanups.push(() => {
      try {
        source.timeScale().unsubscribeVisibleLogicalRangeChange(handler)
      } catch {
        /* ignore */
      }
    })
  })
  return () => cleanups.forEach((fn) => fn())
}

function applyCommonOptions(chart, { showTimeAxis }) {
  chart.applyOptions({
    layout: {
      background: { type: ColorType.Solid, color: BG },
      textColor: TEXT,
    },
    grid: {
      vertLines: { color: GRID },
      horzLines: { color: GRID },
    },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: GRID },
    timeScale: {
      borderColor: GRID,
      timeVisible: true,
      secondsVisible: false,
      visible: showTimeAxis,
    },
  })
}

const S003Chart = forwardRef(function S003Chart(
  { data, config, selectedSignal },
  ref,
) {
  const priceRef = useRef(null)
  const adxRef = useRef(null)
  const rsiRef = useRef(null)
  const volRef = useRef(null)
  const warmOverlayRef = useRef(null)
  const apiRef = useRef({
    charts: [],
    candleSeries: null,
    extremeLine: null,
    confirmLine: null,
    warmupEndTime: null,
  })

  useImperativeHandle(ref, () => ({
    focusSignal(signal) {
      const chart = apiRef.current.charts[0]
      const series = apiRef.current.candleSeries
      if (!chart || !series || !signal) return
      const t = Number(signal.time)
      try {
        chart.timeScale().scrollToPosition(0, false)
        const bars = data?.candles || []
        const idx = bars.findIndex((c) => Number(c.time) === t)
        if (idx >= 0) {
          const from = Math.max(0, idx - 40)
          const to = Math.min(bars.length - 1, idx + 40)
          chart.timeScale().setVisibleLogicalRange({
            from,
            to,
          })
        }
      } catch {
        /* ignore */
      }
    },
  }))

  useEffect(() => {
    if (!priceRef.current || !adxRef.current || !rsiRef.current || !volRef.current) {
      return undefined
    }
    if (!data?.candles?.length) return undefined

    const priceChart = createChart(priceRef.current, {
      width: priceRef.current.clientWidth,
      height: Math.max(280, Math.floor(priceRef.current.clientWidth * 0.42)),
    })
    const adxChart = createChart(adxRef.current, {
      width: adxRef.current.clientWidth,
      height: 120,
    })
    const rsiChart = createChart(rsiRef.current, {
      width: rsiRef.current.clientWidth,
      height: 120,
    })
    const volChart = createChart(volRef.current, {
      width: volRef.current.clientWidth,
      height: 120,
    })

    applyCommonOptions(priceChart, { showTimeAxis: false })
    applyCommonOptions(adxChart, { showTimeAxis: false })
    applyCommonOptions(rsiChart, { showTimeAxis: false })
    applyCommonOptions(volChart, { showTimeAxis: true })

    const candleSeries = priceChart.addCandlestickSeries({
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    })
    candleSeries.setData(
      data.candles.map((c) => ({
        time: Number(c.time),
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
      })),
    )

    const vwapSeries = priceChart.addLineSeries({
      color: '#38bdf8',
      lineWidth: 2,
      title: 'VWAP',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    vwapSeries.setData(filterPoints(data.indicators?.vwap))

    const signalMarkers = (data.signals || []).map((s) => {
      const long = String(s.direction).toUpperCase() === 'LONG'
      return {
        time: Number(s.time),
        position: long ? 'belowBar' : 'aboveBar',
        color: long ? '#22c55e' : '#ef4444',
        shape: long ? 'arrowUp' : 'arrowDown',
        text: `${modeInitial(s.mode)}${s.score}`,
      }
    })

    const armMarkers = (data.arm_events || [])
      .filter((ev) =>
        ['ARM', 'INVALIDATE', 'EXPIRE', 'COOLDOWN_BLOCK'].includes(
          String(ev.type || '').toUpperCase(),
        ),
      )
      .map((ev) => {
      const type = String(ev.type || '').toUpperCase()
      let shape = 'circle'
      let color = '#64748b'
      if (type === 'INVALIDATE') {
        shape = 'square'
        color = '#f97316'
      } else if (type === 'EXPIRE') {
        shape = 'square'
        color = '#a78bfa'
      } else if (type === 'COOLDOWN_BLOCK') {
        shape = 'circle'
        color = '#eab308'
      } else if (type === 'ARM') {
        shape = 'circle'
        color = '#94a3b8'
      }
      return {
        time: Number(ev.time),
        position: 'inBar',
        color,
        shape,
        size: 0.6,
      }
    })

    // Signals on top of muted arm markers
    candleSeries.setMarkers(
      [...armMarkers, ...signalMarkers].sort((a, b) => a.time - b.time),
    )

    const adxLine = adxChart.addLineSeries({
      color: '#f59e0b',
      lineWidth: 2,
      title: 'ADX',
    })
    const plusDi = adxChart.addLineSeries({
      color: '#22c55e',
      lineWidth: 1,
      title: '+DI',
    })
    const minusDi = adxChart.addLineSeries({
      color: '#ef4444',
      lineWidth: 1,
      title: '-DI',
    })
    adxLine.setData(filterPoints(data.indicators?.adx))
    plusDi.setData(filterPoints(data.indicators?.plus_di))
    minusDi.setData(filterPoints(data.indicators?.minus_di))
    const adxTrend = Number(config?.adx_trend)
    if (Number.isFinite(adxTrend)) {
      adxLine.createPriceLine({
        price: adxTrend,
        color: '#fbbf24',
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'trend',
      })
    }

    const rsiLine = rsiChart.addLineSeries({
      color: '#a78bfa',
      lineWidth: 2,
      title: 'RSI',
    })
    rsiLine.setData(filterPoints(data.indicators?.rsi))
    const rsiOb = Number(config?.rsi_ob)
    const rsiOs = Number(config?.rsi_os)
    if (Number.isFinite(rsiOb)) {
      rsiLine.createPriceLine({
        price: rsiOb,
        color: '#f87171',
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        title: 'OB',
      })
    }
    if (Number.isFinite(rsiOs)) {
      rsiLine.createPriceLine({
        price: rsiOs,
        color: '#4ade80',
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        title: 'OS',
      })
    }

    const volHist = volChart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    })
    volHist.priceScale().applyOptions({ scaleMargins: { top: 0.2, bottom: 0 } })
    volHist.setData(
      data.candles.map((c) => ({
        time: Number(c.time),
        value: Number(c.volume),
        color:
          Number(c.close) >= Number(c.open)
            ? 'rgba(34,197,94,0.55)'
            : 'rgba(239,68,68,0.55)',
      })),
    )
    const volSma = volChart.addLineSeries({
      color: '#38bdf8',
      lineWidth: 2,
      title: 'Vol SMA',
    })
    volSma.setData(filterPoints(data.indicators?.vol_sma))

    const charts = [priceChart, adxChart, rsiChart, volChart]
    const unsubSync = syncCharts(charts)
    priceChart.timeScale().fitContent()

    const warmIdx = data.warm_from_index
    let warmupEndTime = null
    if (warmIdx != null && warmIdx > 0 && data.candles[warmIdx]) {
      warmupEndTime = Number(data.candles[Math.max(0, warmIdx - 1)]?.time)
    }

    const updateWarmOverlay = () => {
      const el = warmOverlayRef.current
      const host = priceRef.current
      if (!el || !host || warmupEndTime == null) {
        if (el) el.style.width = '0px'
        return
      }
      const x0 = priceChart.timeScale().timeToCoordinate(Number(data.candles[0].time))
      const x1 = priceChart.timeScale().timeToCoordinate(warmupEndTime)
      if (x0 == null || x1 == null) {
        el.style.width = '0px'
        return
      }
      const left = Math.min(x0, x1)
      const width = Math.abs(x1 - x0)
      el.style.left = `${Math.max(0, left)}px`
      el.style.width = `${Math.max(0, width)}px`
    }

    priceChart.timeScale().subscribeVisibleLogicalRangeChange(updateWarmOverlay)
    updateWarmOverlay()

    apiRef.current = {
      charts,
      candleSeries,
      extremeLine: null,
      confirmLine: null,
      warmupEndTime,
    }

    const onResize = () => {
      charts.forEach((ch, i) => {
        const el = [priceRef, adxRef, rsiRef, volRef][i].current
        if (!el) return
        ch.applyOptions({
          width: el.clientWidth,
          height: i === 0 ? Math.max(280, Math.floor(el.clientWidth * 0.42)) : 120,
        })
      })
      updateWarmOverlay()
    }
    window.addEventListener('resize', onResize)

    return () => {
      window.removeEventListener('resize', onResize)
      unsubSync()
      charts.forEach((ch) => ch.remove())
      apiRef.current = {
        charts: [],
        candleSeries: null,
        extremeLine: null,
        confirmLine: null,
        warmupEndTime: null,
      }
    }
  }, [data, config])

  useEffect(() => {
    const series = apiRef.current.candleSeries
    if (!series) return

    if (apiRef.current.extremeLine) {
      series.removePriceLine(apiRef.current.extremeLine)
      apiRef.current.extremeLine = null
    }
    if (apiRef.current.confirmLine) {
      series.removePriceLine(apiRef.current.confirmLine)
      apiRef.current.confirmLine = null
    }
    if (!selectedSignal) return

    apiRef.current.extremeLine = series.createPriceLine({
      price: Number(selectedSignal.signal_extreme),
      color: '#f472b6',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: 'extreme',
    })
    apiRef.current.confirmLine = series.createPriceLine({
      price: Number(selectedSignal.confirm_level),
      color: '#38bdf8',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: 'confirm',
    })
  }, [selectedSignal, data])

  return (
    <div className="overflow-hidden rounded-lg border border-gray-800 bg-gray-950">
      <div className="relative border-b border-gray-800">
        <div className="px-3 py-1 text-xs text-gray-500">Price + VWAP + signals</div>
        <div ref={priceRef} className="w-full" />
        <div
          ref={warmOverlayRef}
          className="pointer-events-none absolute top-6 bottom-0 bg-amber-500/10"
        >
          <div className="sticky left-2 top-2 inline-block rounded bg-amber-900/80 px-2 py-0.5 text-[10px] font-medium text-amber-100">
            warming up — no signals, VWAP incomplete
          </div>
        </div>
      </div>
      <div className="border-b border-gray-800">
        <div className="px-3 py-1 text-xs text-gray-500">ADX / +DI / −DI</div>
        <div ref={adxRef} className="w-full" />
      </div>
      <div className="border-b border-gray-800">
        <div className="px-3 py-1 text-xs text-gray-500">RSI</div>
        <div ref={rsiRef} className="w-full" />
      </div>
      <div>
        <div className="px-3 py-1 text-xs text-gray-500">Volume</div>
        <div ref={volRef} className="w-full" />
      </div>
    </div>
  )
})

export default S003Chart
