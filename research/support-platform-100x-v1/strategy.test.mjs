import fs from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { platformAt, settleLeveraged, targetReached, simulate } from './strategy.mjs';
const policy = JSON.parse(fs.readFileSync(new URL('./policy.json', import.meta.url)));
test('100x fees and zero clamp match website, not unleveraged settlement', () => {
  assert.equal(settleLeveraged(10000, 100, 100, 100, .0002), 9800);
  assert.equal(settleLeveraged(10000, 100, 99, 100, .0002), 0);
  assert.equal(settleLeveraged(10000, 100, 101, 100, .0002), 19798);
});
test('10% means net margin profit, not underlying change or gross leveraged gain', () => {
  assert.equal(targetReached(10000, 100, 100.1, policy), false);
  assert.equal(targetReached(10000, 100, 100.12, policy), false);
  assert.equal(targetReached(10000, 100, 100.120025, policy), true);
});
test('five full seconds below support, 1bp range, and prior decline all required', () => {
  const close = Array(45).fill(100);
  for (let i = 30; i < 45; i++) close[i] = 99.94;
  const data = { close, high: [...close], low: [...close] };
  const event = { support: 99.95, breach: 35 };
  assert.equal(platformAt(data, event, 38, policy), null);
  assert.ok(platformAt(data, event, 39, policy));
  data.high[37] = 99.951; assert.equal(platformAt(data, event, 39, policy), null);
  data.high[37] = 99.94; data.low[37] = 99.90; assert.equal(platformAt(data, event, 39, policy), null);
  data.low[37] = 99.94; data.close[4] = 99.94; assert.equal(platformAt(data, event, 39, policy), null);
});
function fixture(prices, trigger = 0, delay = 2) {
  const p = structuredClone(policy); p.dataStart = '1970-01-01T00:00:00Z'; p.evaluationStart = p.dataStart;
  const data = { close: prices, high: [...prices], low: [...prices] };
  const prepared = { candidates: [{ index: trigger, event: { id: 'e', support: 101 } }], trendFilter: false };
  const s = { name: 'test', delaySeconds: delay, slippagePerSide: 0 };
  return { p, data, prepared, s };
}
test('entry is delayed, target crossing only requests sale, later fill determines profit', () => {
  const { p, data, prepared, s } = fixture([100, 100, 100, 100.13, 100.1, 100.05, 100]);
  const r = simulate(data, prepared, p, s);
  assert.equal(r.trades[0].entryIndex, 2); assert.equal(r.trades[0].exitTriggerIndex, 3);
  assert.equal(r.trades[0].exitIndex, 5); assert.ok(r.trades[0].netMarginReturn < .10);
  assert.equal(r.metrics.targetClosuresBelowRequestedReturn, 1);
});
test('a profitable candle high cannot trigger sale if its observable close did not hit target', () => {
  const { p, data, prepared, s } = fixture([100, 100, 100, 100, 100, 100]); data.high[3] = 101;
  const r = simulate(data, prepared, p, s);
  assert.equal(r.trades[0].reason, 'evaluation_end');
});
test('liquidation is checked on venue phase, not every intrasecond low', () => {
  const { p, data, prepared, s } = fixture(Array(18).fill(100)); data.low[5] = 98;
  const r = simulate(data, prepared, p, s, 0, 'grid_close');
  assert.equal(r.metrics.liquidations, 0);
  assert.equal(r.metrics.recoveredAfterObservedLiquidationCross, 1);
  const conservative = simulate(data, prepared, p, s, 0, 'each_second_low');
  assert.equal(conservative.metrics.liquidations, 1);
  assert.equal(conservative.trades[0].payoutUnits, 0);
});
test('venue scan crossing liquidates with total margin loss; no debt', () => {
  const { p, data, prepared, s } = fixture(Array(18).fill(100)); data.close[14] = data.low[14] = 98;
  const r = simulate(data, prepared, p, s, 0, 'grid_close');
  assert.equal(r.trades[0].reason, 'liquidated'); assert.equal(r.trades[0].exitIndex, 14);
  assert.equal(r.metrics.finalFish, 49);
});
test('liquidation can win the race against an in-flight profit sale', () => {
  const { p, data, prepared, s } = fixture([100, 100, 100, 100.13, 98, 98, 98]);
  const r = simulate(data, prepared, p, s, 5, 'grid_close');
  assert.equal(r.trades[0].reason, 'liquidated'); assert.equal(r.trades[0].exitIndex, 4);
});
test('entry-second earlier low cannot liquidate a position that did not exist then', () => {
  const { p, data, prepared, s } = fixture([100, 100, 100, 100, 100]); data.low[2] = 98;
  const r = simulate(data, prepared, p, s, 0, 'each_second_low');
  assert.equal(r.metrics.liquidations, 0);
});
