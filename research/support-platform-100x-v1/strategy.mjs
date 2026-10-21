// Offline simulator only. Never contacts the website or submits orders.
import { ema, atr, aggregateSeconds, confirmedPivot, factTimeline, indexAt } from '../support-seconds-v1/strategy.mjs';
export const SCALE = 10000;

export function settleLeveraged(stake, entry, exit, leverage, fee) {
  const ratio = exit / entry, notional = stake * leverage;
  return Math.max(0, Math.floor(stake + notional * (ratio - 1) - notional * ratio * fee));
}
export function targetReached(stake, entry, price, policy) {
  return settleLeveraged(stake, entry, price, policy.leverage, policy.fee) >= Math.ceil(stake * (1 + policy.targetNetMarginReturn));
}
export function platformAt(data, event, index, policy) {
  const first = index - policy.platform.seconds + 1;
  if (first < event.breach || event.breach === null) return null;
  let high = -Infinity, low = Infinity;
  for (let i = first; i <= index; i++) { high = Math.max(high, data.high[i]); low = Math.min(low, data.low[i]); }
  if (!(high < event.support && high / low - 1 <= policy.platform.maximumRangeFraction)) return null;
  const before = first - 1, prior = before - policy.precedingDecline.seconds;
  if (prior < 0) return null;
  const decline = 1 - data.close[before] / data.close[prior];
  if (!(decline > 0 && decline >= policy.precedingDecline.minimumFraction)) return null;
  return { first, high, low, range: high / low - 1, precedingDeclineFraction: decline };
}

export function prepare(data, four, policy, trendFilter = policy.primaryTrendFilter) {
  const start = Date.parse(policy.dataStart) / 1000, evaluation = Date.parse(policy.evaluationStart) / 1000;
  const cs = four.map(b => b.close), fast = ema(cs, policy.trend.fastEMA), slow = ema(cs, policy.trend.slowEMA);
  const facts = factTimeline(four, policy.trend.intervalSeconds, (b, i) => ({ enabled: b.close > slow[i] && fast[i] > slow[i] }));
  const bars = aggregateSeconds(data, policy.support.intervalSeconds, start), values = atr(bars, policy.support.atrPeriod);
  const events = [], candidates = [], stats = { pivots: 0, trendAllowedSeconds: 0 };
  let supports = [], watch = null, active = null, cursor = indexAt(facts, start), serial = 0;
  for (let i = 0; i < data.close.length; i++) {
    const now = start + i + 1;
    while (cursor + 1 < facts.length && facts[cursor + 1].time <= now) cursor++;
    const enabled = !trendFilter || cursor >= 0 && facts[cursor].enabled;
    if (now > evaluation && enabled) stats.trendAllowedSeconds++;
    if (watch && i > watch.expires) watch = null;
    if (!enabled) watch = null;
    // Observe a crossing before updating the completed 15m bar; otherwise a break at
    // that boundary could retrospectively invalidate the very support being tested.
    if (!active && watch && enabled && data.close[i] <= watch.upper && now >= evaluation) {
      watch.pivot.consumed = true;
      active = { id: 'event-' + serial++, pivotId: watch.pivot.id, support: watch.support, atr: watch.atr,
        touch: i, breach: null, trigger: null, platform: null, ended: null, endReason: null };
      events.push(active); watch = null;
    }
    if ((i + 1) % policy.support.intervalSeconds === 0) {
      const b = Math.floor(i / policy.support.intervalSeconds);
      const pivot = confirmedPivot(bars, b, policy.support.pivotLeft, policy.support.pivotRight);
      if (pivot && Number.isFinite(values[b])) { supports.push({ ...pivot, atr: values[b], consumed: false, invalid: false }); stats.pivots++; }
      supports = supports.filter(s => b - s.bar <= policy.support.maximumAgeBars);
      for (const s of supports) if (bars[b].low <= s.price - .5 * s.atr) s.invalid = true;
      if (!active) {
        watch = null;
        if (enabled && now >= evaluation) for (const s of supports) {
          const upper = s.price + policy.support.zoneHalfATR * s.atr;
          if (!s.consumed && !s.invalid && data.close[i] > upper && (!watch || s.price > watch.support)) {
            watch = { pivot: s, support: s.price, atr: s.atr, upper,
              expires: (s.bar + policy.support.maximumAgeBars + 1) * policy.support.intervalSeconds - 1 };
          }
        }
      }
    }
    if (active) {
      if (active.breach === null && data.close[i] < active.support) active.breach = i;
      const expired = i - active.touch >= policy.support.maximumWaitingSeconds;
      if (!enabled || expired) { active.ended = i; active.endReason = !enabled ? 'trend_off' : 'waiting_timeout'; active = null; }
      else {
        const plateau = platformAt(data, active, i, policy);
        if (plateau) {
          active.trigger = i; active.platform = plateau; active.ended = i; active.endReason = 'platform_found';
          candidates.push({ index: i, event: active }); active = null;
        }
      }
    }
  }
  if (active) { active.ended = data.close.length - 1; active.endReason = 'data_end'; }
  return { events, candidates, stats, trendFilter };
}

export function simulate(data, prepared, policy, scenario, phase = 0, liquidationMode = 'grid_close') {
  const start = Date.parse(policy.dataStart) / 1000, first = Date.parse(policy.evaluationStart) / 1000 - start, n = data.close.length;
  const stake = Math.round(policy.stakeFish * SCALE), initial = Math.round(policy.startingFish * SCALE);
  let cash = initial, position = null, pending = null, cursor = 0, peak = initial, dd = 0, exposure = 0, lastPoint = first - 1;
  const trades = [], curve = [], filters = {};
  const mark = px => cash + (position ? settleLeveraged(stake, position.entry, px * (1 - scenario.slippagePerSide), policy.leverage, policy.fee) : 0);
  const bump = k => { filters[k] = (filters[k] || 0) + 1; };
  const point = (i, eq) => { curve.push({ time: start + i + 1, equity: eq / SCALE }); lastPoint = i; };
  const close = (i, reason, triggerIndex = null) => {
    const liquidated = reason === 'liquidated';
    const exit = liquidated ? position.liquidationPrice : data.close[i] * (1 - scenario.slippagePerSide);
    const payout = liquidated ? 0 : settleLeveraged(stake, position.entry, exit, policy.leverage, policy.fee);
    cash += payout;
    const gross = Math.max(0, stake + stake * policy.leverage * (exit / position.entry - 1));
    trades.push({ ...position, exitIndex: i, exitPrice: exit, exitObservedPrice: data.close[i], payoutUnits: payout,
      profitFish: (payout - stake) / SCALE, netMarginReturn: payout / stake - 1, feeFish: liquidated ? 0 : (gross - payout) / SCALE,
      reason, exitTriggerIndex: triggerIndex, holdingSeconds: i - position.entryIndex,
      observedLiquidationPriceCrossedBeforeExit: position.observedLiquidationPriceCrossed });
    position = null; pending = null; point(i, cash);
  };
  for (let i = first; i < n; i++) {
    if (!position && !pending && (cursor >= prepared.candidates.length || prepared.candidates[cursor].index > i)) {
      const next = cursor < prepared.candidates.length ? prepared.candidates[cursor].index : n - 1;
      for (let h = Math.floor((lastPoint + 1) / 3600) * 3600 + 3599; h < next; h += 3600) point(h, cash);
      i = next;
    }
    const now = start + i + 1, px = data.close[i];
    let justEntered = false;
    if (position) {
      exposure++;
      if (data.low[i] <= position.liquidationPrice) position.observedLiquidationPriceCrossed = true;
      const due = liquidationMode === 'each_second_low' || (now % policy.liquidationIntervalSeconds === phase);
      const price = liquidationMode === 'each_second_low' ? data.low[i] : px;
      if (due && price <= position.liquidationPrice) close(i, 'liquidated');
    }
    if (pending && pending.due === i) {
      if (pending.kind === 'sell') close(i, 'profit_target', pending.trigger);
      else {
        const order = pending;
        cash -= stake;
        const entry = px * (1 + scenario.slippagePerSide);
        position = { eventId: order.event.id, support: order.event.support, signalIndex: order.trigger, signalPrice: order.signalPrice,
          entryIndex: i, entry, stakeUnits: stake, liquidationPrice: entry * (1 - 1 / policy.leverage),
          observedLiquidationPriceCrossed: false, targetTriggerMarginReturn: null, targetTriggerPrice: null };
        pending = null; justEntered = true; point(i, mark(px));
      }
    }
    if (position && !pending && targetReached(stake, position.entry, px, policy)) {
      position.targetTriggerMarginReturn = settleLeveraged(stake, position.entry, px, policy.leverage, policy.fee) / stake - 1;
      position.targetTriggerPrice = px;
      pending = { kind: 'sell', due: i + scenario.delaySeconds, trigger: i };
    }
    const eq = mark(px); peak = Math.max(peak, eq); dd = Math.max(dd, 1 - eq / peak);
    while (cursor < prepared.candidates.length && prepared.candidates[cursor].index <= i) {
      const signal = prepared.candidates[cursor++];
      if (signal.index < i) { bump('expired_signal'); continue; }
      if (position || pending) { bump('position_or_order_busy'); continue; }
      if (cash < stake) { bump('insufficient_cash'); continue; }
      if (i + scenario.delaySeconds >= n) { bump('insufficient_execution_horizon'); continue; }
      pending = { kind: 'buy', due: i + scenario.delaySeconds, trigger: i, signalPrice: px, event: signal.event };
    }
    if (now % 3600 === 0 || i === n - 1) point(i, mark(px));
    if (justEntered) exposure++;
  }
  if (position) close(n - 1, 'evaluation_end');
  if (pending?.kind === 'buy') bump('unfilled_at_end');
  if (!curve.length || curve.at(-1).equity !== cash / SCALE) point(n - 1, cash);
  const natural = trades.filter(t => t.reason !== 'evaluation_end'), wins = natural.filter(t => t.profitFish > 0), losses = natural.filter(t => t.profitFish < 0);
  const sortedHolds = natural.map(t => t.holdingSeconds).sort((a, b) => a - b);
  return { scenario: scenario.name, phase, liquidationMode, trendFilter: prepared.trendFilter, platformSeconds: policy.platform.seconds,
    metrics: { finalFish: cash / SCALE, netReturn: cash / initial - 1, maxDrawdown: dd, naturalTrades: natural.length,
      wins: wins.length, losses: losses.length, winRate: natural.length ? wins.length / natural.length : null,
      liquidations: natural.filter(t => t.reason === 'liquidated').length,
      targetClosures: natural.filter(t => t.reason === 'profit_target').length,
      targetClosuresBelowRequestedReturn: natural.filter(t => t.reason === 'profit_target' && t.netMarginReturn < policy.targetNetMarginReturn).length,
      boundaryClosures: trades.length - natural.length,
      profitFactor: losses.length ? wins.reduce((s, t) => s + t.profitFish, 0) / -losses.reduce((s, t) => s + t.profitFish, 0) : null,
      averageWinMarginReturn: wins.length ? wins.reduce((s, t) => s + t.netMarginReturn, 0) / wins.length : null,
      averageLossMarginReturn: losses.length ? losses.reduce((s, t) => s + t.netMarginReturn, 0) / losses.length : null,
      averageProfitFish: natural.length ? natural.reduce((s, t) => s + t.profitFish, 0) / natural.length : null,
      feesFish: trades.reduce((s, t) => s + t.feeFish, 0), exposure: exposure / (n - first),
      medianHoldingSeconds: sortedHolds.length ? sortedHolds[Math.floor(sortedHolds.length / 2)] : null,
      maximumHoldingSeconds: sortedHolds.length ? sortedHolds.at(-1) : null,
      recoveredAfterObservedLiquidationCross: trades.filter(t => t.reason !== 'liquidated' && t.observedLiquidationPriceCrossedBeforeExit).length,
      eligibleSignals: prepared.candidates.length, filters }, trades, curve };
}
