// Offline research only. This module has no network or order submission code.
export const SCALE = 10000;
export const DAY = 86400;

export function settle(units, entry, exit, fee = .0002) {
  const ratio = exit / entry;
  return Math.max(0, Math.floor(units + units * (ratio - 1) - units * ratio * fee));
}

export function ema(values, period) {
  const result = new Float64Array(values.length).fill(NaN);
  if (values.length < period) return result;
  let value = 0;
  for (let i = 0; i < period; i++) value += values[i];
  value /= period; result[period - 1] = value;
  for (let i = period; i < values.length; i++) {
    value += 2 / (period + 1) * (values[i] - value); result[i] = value;
  }
  return result;
}

export function atr(bars, period) {
  const result = new Float64Array(bars.length).fill(NaN);
  let value = 0;
  for (let i = 1; i < bars.length; i++) {
    const b = bars[i], prev = bars[i - 1];
    const tr = Math.max(b.high - b.low, Math.abs(b.high - prev.close), Math.abs(b.low - prev.close));
    if (i <= period) value += tr / period;
    else value = (value * (period - 1) + tr) / period;
    if (i >= period) result[i] = value;
  }
  return result;
}

export function normalizeBars(rows, width) {
  return rows.map((r, i) => {
    const time = r[0] / 1000;
    if (!Number.isInteger(time) || time % width || (i && time !== rows[i - 1][0] / 1000 + width)) throw Error('invalid bar timeline');
    if (!(r[3] > 0 && r[3] <= r[4] && r[4] <= r[2])) throw Error('invalid bar OHLC');
    return { time, high: r[2], low: r[3], close: r[4] };
  });
}

// A fact belongs to the end of a bar, never its opening timestamp.
export function factTimeline(bars, width, makeFact) {
  return bars.map((b, i) => ({ time: b.time + width, ...makeFact(b, i) }));
}

export function indexAt(facts, time) {
  let lo = 0, hi = facts.length;
  while (lo < hi) { const m = (lo + hi) >>> 1; if (facts[m].time <= time) lo = m + 1; else hi = m; }
  return lo - 1;
}

export function aggregateSeconds(data, width, start) {
  if (data.close.length % width || start % width) throw Error('incomplete aggregation');
  const out = [];
  for (let base = 0; base < data.close.length; base += width) {
    let high = -Infinity, low = Infinity;
    for (let i = base; i < base + width; i++) { high = Math.max(high, data.high[i]); low = Math.min(low, data.low[i]); }
    out.push({ time: start + base, high, low, close: data.close[base + width - 1] });
  }
  return out;
}

export function confirmedPivot(bars, current, left, right) {
  const center = current - right;
  if (center < left) return null;
  const price = bars[center].low;
  for (let i = center - left; i <= center + right; i++) if (i !== center && bars[i].low <= price) return null;
  return { id: 'support-' + bars[center].time, bar: center, price, confirmedBar: current };
}

export function confirmation(event, i, close, high, low, policy) {
  if (low[i] < event.minimum) { event.minimum = low[i]; event.lastLow = i; }
  const count = policy.confirmation.breakoutLookbackSeconds;
  let previousHigh = -Infinity;
  for (let j = Math.max(0, i - count); j < i; j++) previousHigh = Math.max(previousHigh, high[j]);
  return i - event.lastLow >= policy.confirmation.quietSeconds && close[i] >= event.support &&
    close[i] <= event.maximumEntry && close[i] > previousHigh;
}

export function prepare(data, hourly, four, policy) {
  const start = Date.parse(policy.dataStart) / 1000;
  const fourClose = four.map(b => b.close);
  const fast = ema(fourClose, policy.trend.fastEMA), slow = ema(fourClose, policy.trend.slowEMA);
  const trendFacts = factTimeline(four, policy.trend.intervalSeconds, (b, i) => ({
    enabled: b.close > slow[i] && fast[i] > slow[i], close: b.close, fast: fast[i], slow: slow[i],
  }));
  const hourATR = atr(hourly, policy.exit.hourlyATRPeriod);
  const hourFacts = factTimeline(hourly, 3600, (b, i) => ({ close: b.close, atr: hourATR[i] }));
  const bars = aggregateSeconds(data, policy.support.intervalSeconds, start), barATR = atr(bars, policy.support.atrPeriod);
  const events = [], candidates = [], counts = { pivots: 0, invalidatedBeforeTouch: 0, expiredBeforeTouch: 0 };
  let active = null, watch = null, trendIndex = indexAt(trendFacts, start), supports = [], nextId = 0;
  for (let i = 0; i < data.close.length; i++) {
    const now = start + i + 1; // complete second i is observable only now
    while (trendIndex + 1 < trendFacts.length && trendFacts[trendIndex + 1].time <= now) trendIndex++;
    const enabled = trendIndex >= 0 && trendFacts[trendIndex].enabled;
    if ((i + 1) % policy.support.intervalSeconds === 0) {
      const b = Math.floor(i / policy.support.intervalSeconds);
      const pivot = confirmedPivot(bars, b, policy.support.pivotLeft, policy.support.pivotRight);
      if (pivot && Number.isFinite(barATR[b])) {
        const a = barATR[b];
        supports.push({ ...pivot, atr: a, upper: pivot.price + policy.support.zoneHalfATR * a,
          stop: pivot.price - policy.support.invalidationATR * a,
          maximumEntry: pivot.price + policy.support.maximumEntryAboveSupportATR * a,
          consumed: false, invalid: false }); counts.pivots++;
      }
      supports = supports.filter(s => b - s.bar <= policy.support.maximumAgeBars);
      for (const s of supports) if (bars[b].low <= s.stop) s.invalid = true;
      if (!active) {
        watch = null;
        if (enabled && i + 1 >= policy.warmupSeconds && Number.isFinite(barATR[b])) {
          for (const s of supports) {
            if (!s.consumed && !s.invalid && data.close[i] > s.upper && (!watch || s.price > watch.support)) {
              watch = { pivot: s, support: s.price, atr: s.atr, upper: s.upper,
                stop: s.stop, maximumEntry: s.maximumEntry,
                expires: (s.bar + policy.support.maximumAgeBars + 1) * policy.support.intervalSeconds - 1 };
            }
          }
        }
      }
    }
    // Invalidate the pivot itself, preventing re-use after a failed test of support.
    if (watch && i > watch.expires) { watch = null; counts.expiredBeforeTouch++; }
    if (watch && data.low[i] <= watch.stop) {
      watch.pivot.invalid = true; watch = null; counts.invalidatedBeforeTouch++;
    }
    if (!enabled) watch = null;
    if (!active && watch && data.close[i] <= watch.upper && enabled) {
      watch.pivot.consumed = true;
      active = { id: 'event-' + nextId++, pivotId: watch.pivot.id, support: watch.support, atr: watch.atr,
        upper: watch.upper, stop: watch.stop, maximumEntry: watch.maximumEntry, touch: i,
        minimum: data.low[i], lastLow: i, triggers: { touch: i }, ended: null, endReason: null };
      events.push(active); candidates.push({ method: 'touch', index: i, event: active }); watch = null;
    }
    if (active) {
      let reason = !enabled ? 'trend_off' : data.low[i] <= active.stop ? 'support_failed' :
        i - active.touch >= policy.support.maximumConfirmationSeconds ? 'confirmation_timeout' : null;
      if (!reason) {
        const valid = confirmation(active, i, data.close, data.high, data.low, policy);
        if (valid && active.triggers.second_confirmation === undefined) {
          active.triggers.second_confirmation = i; candidates.push({ method: 'second_confirmation', index: i, event: active });
        }
        if (valid && now % 60 === 0 && active.triggers.minute_confirmation === undefined) {
          active.triggers.minute_confirmation = i; candidates.push({ method: 'minute_confirmation', index: i, event: active });
        }
        if (active.triggers.second_confirmation !== undefined && active.triggers.minute_confirmation !== undefined) reason = 'all_confirmed';
      }
      if (reason) { active.ended = i; active.endReason = reason; active = null; }
    }
  }
  if (active) { active.ended = data.close.length - 1; active.endReason = 'data_end'; }
  return { events, candidates, trendFacts, hourFacts, counts };
}

export function size(equity, cash, price, stop, policy) {
  const distance = 1 - stop / price;
  if (!(distance > 0 && distance <= policy.maximumStopDistance)) return 0;
  const lossBudget = distance + policy.fee + 2 * policy.sizingExecutionBudgetPerSide;
  const units = Math.floor(Math.min(cash, equity * policy.allocationCap, equity * policy.plannedRisk / lossBudget));
  return units >= policy.minimumFish * SCALE ? units : 0;
}

export function simulate(data, prepared, policy, method, scenario) {
  const start = Date.parse(policy.dataStart) / 1000, n = data.close.length;
  const signals = prepared.candidates.filter(c => c.method === method);
  const curve = [], trades = [], filters = {};
  let cash = Math.round(policy.startingFish * SCALE), position = null, pending = null, signalCursor = 0;
  let highWater = cash, maxDD = 0, day = null, dayBase = cash, dayPaused = false, halted = false, lastExit = -Infinity;
  let exposureSeconds = 0, allocationSum = 0, fillRiskExceedances = 0, lastCurveIndex = policy.warmupSeconds - 1;
  let trendIndex = indexAt(prepared.trendFacts, start), hourIndex = indexAt(prepared.hourFacts, start);
  const bump = k => { filters[k] = (filters[k] || 0) + 1; };
  const equityAt = price => cash + (position ? settle(position.stakeUnits, position.entry, price * (1 - scenario.slippagePerSide), policy.fee) : 0);
  const point = (i, equity) => {
    curve.push({ time: start + i + 1, equity: equity / SCALE, allocation: position ? position.stakeUnits / equity : 0 }); lastCurveIndex = i;
  };
  const requestExit = (i, reason) => {
    if (!pending && position) pending = { kind: 'sell', due: i + scenario.delaySeconds, reason, trigger: i };
  };
  function close(i, reason, trigger) {
    const exit = data.close[i] * (1 - scenario.slippagePerSide);
    const payout = settle(position.stakeUnits, position.entry, exit, policy.fee);
    cash += payout;
    trades.push({ ...position, exitIndex: i, exitPrice: exit, payoutUnits: payout, profitFish: (payout - position.stakeUnits) / SCALE,
      netReturn: payout / position.stakeUnits - 1, reason, exitTriggerIndex: trigger,
      holdingHours: (i - position.entryIndex) / 3600,
      feeFish: (position.stakeUnits + position.stakeUnits * (exit / position.entry - 1) - payout) / SCALE,
      mfe: position.maximum / position.entry - 1, mae: position.minimum / position.entry - 1 });
    position = null; pending = null; lastExit = i; point(i, cash);
  }
  let i = policy.warmupSeconds;
  while (i < n) {
    // In a flat span there is no varying equity/risk; preserve every hourly point.
    if (!position && !pending && (signalCursor >= signals.length || signals[signalCursor].index > i)) {
      const next = signalCursor < signals.length ? signals[signalCursor].index : n - 1;
      for (let h = Math.floor((lastCurveIndex + 1) / 3600) * 3600 + 3599; h < next; h += 3600) point(h, cash);
      i = next;
    }
    const now = start + i + 1, px = data.close[i];
    while (trendIndex + 1 < prepared.trendFacts.length && prepared.trendFacts[trendIndex + 1].time <= now) trendIndex++;
    while (hourIndex + 1 < prepared.hourFacts.length && prepared.hourFacts[hourIndex + 1].time <= now) hourIndex++;
    const enabled = trendIndex >= 0 && prepared.trendFacts[trendIndex].enabled;
    if (pending?.kind === 'buy' && i > pending.trigger && data.low[i] <= pending.event.stop) pending.invalidDuringDelay = true;
    const wasHolding = Boolean(position);
    let enteredNow = false;
    if (pending && pending.due === i) {
      if (pending.kind === 'sell') {
        // The position existed through this completed second until its close-price fill.
        position.minimum = Math.min(position.minimum, data.low[i]);
        position.maximum = Math.max(position.maximum, data.high[i]);
        close(i, pending.reason, pending.trigger);
      }
      else {
        const order = pending, entry = px * (1 + scenario.slippagePerSide);
        cash -= order.stakeUnits;
        position = { eventId: order.event.id, support: order.event.support, supportATR: order.event.atr,
          signalIndex: order.trigger, signalPrice: order.signalPrice, entryIndex: i, entry, stakeUnits: order.stakeUnits,
          stop: order.event.stop, initialStop: order.event.stop, peakHourlyClose: entry, minimum: entry, maximum: entry };
        pending = null; enteredNow = true;
        const actualRisk = position.stakeUnits * (1 - position.stop / entry + policy.fee + scenario.slippagePerSide);
        if (actualRisk > order.signalEquity * policy.plannedRisk) fillRiskExceedances++;
        point(i, equityAt(px));
        // A market request cannot be cancelled/reshaped using prices observed during latency.
        if (order.invalidDuringDelay || px <= position.stop) requestExit(i, 'support_failed_during_entry');
        else if (!enabled) requestExit(i, 'trend_off_during_entry');
      }
    }
    if (wasHolding || position) exposureSeconds++;
    if (position && !enteredNow) {
      position.minimum = Math.min(position.minimum, data.low[i]); position.maximum = Math.max(position.maximum, data.high[i]);
      const oldStop = position.stop;
      if (data.low[i] <= oldStop) requestExit(i, 'structural_or_trailing_stop');
      if (now % 3600 === 0 && hourIndex >= 0) {
        const fact = prepared.hourFacts[hourIndex];
        if (Number.isFinite(fact.atr)) {
          position.peakHourlyClose = Math.max(position.peakHourlyClose, fact.close);
          position.stop = Math.max(position.stop, position.peakHourlyClose - policy.exit.trailingATR * fact.atr);
          if (px <= position.stop) requestExit(i, 'hourly_trailing_stop');
        }
      }
      if (!enabled) requestExit(i, 'trend_off');
      if (i - position.entryIndex >= policy.exit.maximumHoldingSeconds) requestExit(i, 'time_limit');
    }
    const equity = equityAt(px), newDay = Math.floor((now + 8 * 3600) / DAY);
    if (newDay !== day) { day = newDay; dayBase = equity; dayPaused = false; }
    highWater = Math.max(highWater, equity); maxDD = Math.max(maxDD, 1 - equity / highWater);
    if (equity <= dayBase * (1 - policy.dailyLoss)) { dayPaused = true; requestExit(i, 'daily_loss'); }
    if (equity <= highWater * (1 - policy.maximumDrawdown)) { halted = true; requestExit(i, 'maximum_drawdown'); }
    if (position) allocationSum += position.stakeUnits / equity;
    while (signalCursor < signals.length && signals[signalCursor].index <= i) {
      const signal = signals[signalCursor++];
      if (signal.index < i) { bump('expired_signal'); continue; }
      let reason = halted ? 'permanent_drawdown_halt' : dayPaused ? 'daily_pause' : position || pending ? 'position_or_order_busy' :
        i - lastExit < policy.exit.cooldownSeconds ? 'cooldown' : !enabled ? 'trend_off' :
        i + scenario.delaySeconds >= n ? 'insufficient_execution_horizon' : null;
      if (reason) { bump(reason); continue; }
      const units = size(equity, cash, px, signal.event.stop, policy);
      if (!units) { bump('minimum_or_stop_distance'); continue; }
      pending = { kind: 'buy', due: i + scenario.delaySeconds, trigger: i, signalPrice: px,
        signalEquity: equity, stakeUnits: units, event: signal.event, invalidDuringDelay: false };
    }
    if (now % 3600 === 0 || i === n - 1) point(i, equityAt(px));
    if (i === n - 1) break;
    i++;
  }
  if (position) close(n - 1, 'evaluation_end', n - 1);
  if (pending?.kind === 'buy') bump('unfilled_at_end');
  if (!curve.length || curve.at(-1).equity !== cash / SCALE) point(n - 1, cash);
  const natural = trades.filter(t => t.reason !== 'evaluation_end');
  const wins = natural.filter(t => t.profitFish > 0), losses = natural.filter(t => t.profitFish < 0);
  const daily = [];
  for (const p of curve) if (p.time % DAY === 0) daily.push(p.equity);
  const returns = [policy.startingFish, ...daily].slice(1).map((x, j) => x / [policy.startingFish, ...daily][j] - 1);
  const mean = returns.length ? returns.reduce((a, b) => a + b, 0) / returns.length : 0;
  const sd = returns.length > 1 ? Math.sqrt(returns.reduce((a, b) => a + (b - mean) ** 2, 0) / (returns.length - 1)) : 0;
  // Independent equity endpoints; no artificial quarter-end liquidation or capital reset.
  const quarterEnds = ['2026-01-01', '2026-04-01', '2026-07-01', '2026-10-01'].map(d => Date.parse(d + 'T00:00:00Z') / 1000);
  let priorEquity = policy.startingFish;
  const quarters = quarterEnds.map(end => {
    let e = priorEquity;
    for (const p of curve) { if (p.time <= end) e = p.equity; else break; }
    const result = { endExclusive: new Date(end * 1000).toISOString(), netReturn: e / priorEquity - 1, endingFish: e }; priorEquity = e; return result;
  });
  return { method, scenario: scenario.name, metrics: {
    finalFish: cash / SCALE, netReturn: cash / (policy.startingFish * SCALE) - 1, maxDrawdown: maxDD,
    naturalTrades: natural.length, boundaryClosures: trades.length - natural.length,
    winRate: natural.length ? wins.length / natural.length : null,
    profitFactor: losses.length ? wins.reduce((s, t) => s + t.profitFish, 0) / -losses.reduce((s, t) => s + t.profitFish, 0) : null,
    averageProfitFish: natural.length ? natural.reduce((s, t) => s + t.profitFish, 0) / natural.length : null,
    averageHoldingHours: natural.length ? natural.reduce((s, t) => s + t.holdingHours, 0) / natural.length : null,
    exposure: exposureSeconds / (n - policy.warmupSeconds), averageAllocation: allocationSum / (n - policy.warmupSeconds),
    feesFish: trades.reduce((s, t) => s + t.feeFish, 0), dailySharpe: sd ? mean / sd * Math.sqrt(365) : null,
    eligibleSignals: signals.length, filters, fillRiskExceedances, halted, quarters,
    reasons: trades.reduce((r, t) => { r[t.reason] = (r[t.reason] || 0) + 1; return r; }, {}),
  }, trades, curve };
}
