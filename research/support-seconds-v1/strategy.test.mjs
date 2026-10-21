import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { settle, ema, atr, confirmedPivot, factTimeline, indexAt, confirmation, size, simulate, prepare } from './strategy.mjs';
const policy = JSON.parse(fs.readFileSync(new URL('./policy.json', import.meta.url)));
const scenario = { name: 'test', delaySeconds: 2, slippagePerSide: 0 };

test('site settlement: one closing fee, flooring, no opening fee', () => {
  assert.equal(settle(10000, 100, 100), 9998);
  assert.equal(settle(10000, 100, 200), 19996);
  assert.equal(settle(10000, 100, 0), 0);
});
test('EMA seed and Wilder ATR use preceding closes', () => {
  assert.deepEqual(Array.from(ema([1, 2, 3, 4], 3)).slice(2), [2, 3]);
  const a = atr([{ high: 10, low: 9, close: 10 }, { high: 12, low: 11, close: 12 }, { high: 13, low: 12, close: 12 }, { high: 14, low: 11, close: 13 }], 2);
  assert.equal(a[2], 1.5); assert.equal(a[3], 2.25);
});
test('pivot unavailable before two right bars complete; equal lows excluded', () => {
  const bars = [5, 4, 1, 3, 4].map((low, i) => ({ low, time: i * 900 }));
  assert.equal(confirmedPivot(bars, 3, 2, 2), null);
  assert.equal(confirmedPivot(bars, 4, 2, 2).price, 1);
  bars[4].low = 1; assert.equal(confirmedPivot(bars, 4, 2, 2), null);
});
test('4h fact cannot be read at 14:30 from the bar ending at 16:00', () => {
  const facts = factTimeline([{ time: 12 * 3600, close: 123 }], 14400, b => ({ close: b.close }));
  assert.equal(indexAt(facts, 14.5 * 3600), -1);
  assert.equal(indexAt(facts, 16 * 3600), 0);
});
test('seconds confirmation excludes current high and waits since latest low', () => {
  const p = structuredClone(policy); p.confirmation = { quietSeconds: 2, breakoutLookbackSeconds: 2 };
  const close = [99, 99, 100, 100.2], high = [99, 99, 100, 200], low = [99, 99, 99.5, 100];
  const event = { minimum: 99, lastLow: 0, support: 100, maximumEntry: 101 };
  assert.equal(confirmation(event, 1, close, high, low, p), false);
  assert.equal(confirmation(event, 2, close, high, low, p), true);
  assert.equal(confirmation(event, 3, close, high, low, p), true);
  low[3] = 98; assert.equal(confirmation(event, 3, close, high, low, p), false);
});
test('sizing respects total capital, allocation, planned loss and minimum fish', () => {
  const units = size(500000, 500000, 100, 98, policy);
  assert.ok(units <= 100000);
  assert.ok(units * (.02 + policy.fee + 2 * policy.sizingExecutionBudgetPerSide) <= 1250);
  assert.equal(size(3000, 3000, 100, 98, policy), 0);
  assert.equal(size(500000, 500000, 100, 101, policy), 0);
});
function fixture(prices, stop = 95) {
  const p = structuredClone(policy); p.dataStart = '1970-01-01T00:00:00Z'; p.warmupSeconds = 0;
  p.maximumStopDistance = .1;
  p.exit.maximumHoldingSeconds = 100000; p.exit.cooldownSeconds = 100000;
  const event = { id: 'e', support: 96, atr: 2, stop };
  return { p, data: { close: prices, high: [...prices], low: [...prices] }, prepared: {
    candidates: [{ method: 'second_confirmation', index: 0, event }],
    trendFacts: [{ time: 0, enabled: true }], hourFacts: [], events: [event],
  } };
}
test('market buy executes at delayed price, not trigger price or future favourable low', () => {
  const { p, data, prepared } = fixture([100, 101, 102, 103, 104]);
  const r = simulate(data, prepared, p, 'second_confirmation', scenario);
  assert.equal(r.trades[0].entry, 102); assert.equal(r.trades[0].entryIndex, 2);
  assert.equal(r.trades[0].signalPrice, 100); assert.equal(r.trades[0].exitPrice, 104);
});
test('support fails while market buy in flight: fill still occurs, then delayed exit', () => {
  const { p, data, prepared } = fixture([100, 94, 93, 92, 91, 90], 95);
  const r = simulate(data, prepared, p, 'second_confirmation', scenario);
  assert.equal(r.trades[0].entry, 93); assert.equal(r.trades[0].exitPrice, 91);
  assert.equal(r.trades[0].reason, 'support_failed_during_entry');
});
test('stop gap is sold at delayed observed price rather than ideal stop price', () => {
  const { p, data, prepared } = fixture([100, 100, 100, 94, 93, 92, 91], 95);
  const r = simulate(data, prepared, p, 'second_confirmation', scenario);
  assert.equal(r.trades[0].exitTriggerIndex, 3); assert.equal(r.trades[0].exitPrice, 92);
});
test('exit-second extremes remain included while the position awaits its sell fill', () => {
  const { p, data, prepared } = fixture([100, 100, 100, 94, 93, 92, 91], 95);
  data.high[5] = 120;
  const r = simulate(data, prepared, p, 'second_confirmation', scenario);
  assert.ok(Math.abs(r.trades[0].mfe - .2) < 1e-10);
  assert.ok(Math.abs(r.trades[0].mae + .08) < 1e-10);
});
test('future high inside entry second cannot stop a position before it exists', () => {
  const { p, data, prepared } = fixture([100, 100, 100, 101, 102]);
  data.high[2] = 150;
  const r = simulate(data, prepared, p, 'second_confirmation', scenario);
  assert.ok(r.trades[0].mfe < .03);
});
test('event generation prefix invariant when unseen future prices change', () => {
  const p = structuredClone(policy); p.dataStart = '1970-01-01T00:00:00Z'; p.warmupSeconds = 0;
  p.trend = { intervalSeconds: 16, fastEMA: 2, slowEMA: 3 };
  p.support = { ...p.support, intervalSeconds: 4, atrPeriod: 2, maximumAgeBars: 96 };
  const lows = [105, 104, 103, 105, 106, 107, 108, 109, 110, 111, 110, 109, 108, 107, 106, 105];
  const data = { close: lows.flatMap(x => [x + 1, x + 1, x + 1, x + 1]), high: lows.flatMap(x => [x + 2, x + 2, x + 2, x + 2]), low: lows.flatMap(x => [x, x, x, x]) };
  for (let i = 40; i < 44; i++) { data.close[i] = 103.4; data.high[i] = 103.6; data.low[i] = 103.2; }
  const four = [-48, -32, -16, 0, 16, 32, 48].map((time, i) => ({ time, low: 90 + i, high: 100 + i, close: 98 + i }));
  const hourly = [{ time: -3600, low: 99, high: 102, close: 100 }];
  const changed = structuredClone(data);
  for (const k of ['close', 'high', 'low']) for (let i = 48; i < changed[k].length; i++) changed[k][i] *= 2;
  const prefix = x => prepare(x, hourly, four, p).candidates.filter(c => c.index < 48).map(c => [c.method, c.index, c.event.support, c.event.stop]);
  assert.ok(prefix(data).length > 0, 'prefix comparison must exercise an actual support event');
  assert.deepEqual(prefix(data), prefix(changed));
});
