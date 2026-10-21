// Independently reconstruct the cash ledger and validate execution/portfolio constraints.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
const output = path.resolve(process.argv[2]);
const result = JSON.parse(fs.readFileSync(path.join(output, 'results.json'))), p = result.policy;
const events = new Map(fs.readFileSync(path.join(output, 'events.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(x => { const e = JSON.parse(x); return [e.id, e]; }));
const checks = [];
for (const trial of result.trials) {
  const scenario = p.scenarios.find(s => s.name === trial.scenario);
  const trades = JSON.parse(fs.readFileSync(path.join(output, trial.method + '--' + trial.scenario + '-trades.json')));
  const curve = JSON.parse(fs.readFileSync(path.join(output, trial.method + '--' + trial.scenario + '-curve.json')));
  const seen = new Set(); let cash = p.startingFish * 10000, lastExit = -Infinity, natural = 0;
  for (const t of trades) {
    const event = events.get(t.eventId);
    assert.ok(event, 'unknown support event');
    assert.equal(event.triggers[trial.method], t.signalIndex);
    assert.equal(t.initialStop, event.stop);
    assert.ok(!seen.has(t.eventId), 'repeated support entry'); seen.add(t.eventId);
    assert.ok(t.signalIndex >= p.warmupSeconds);
    assert.equal(t.entryIndex - t.signalIndex, scenario.delaySeconds);
    assert.ok(t.signalIndex - lastExit >= p.exit.cooldownSeconds, 'cooldown violated');
    assert.ok(t.exitIndex >= t.entryIndex);
    assert.ok(Number.isInteger(t.stakeUnits) && t.stakeUnits >= p.minimumFish * 10000);
    assert.ok(t.stakeUnits <= Math.floor(cash * p.allocationCap), 'allocation violated at submission');
    const distance = 1 - t.initialStop / t.signalPrice;
    assert.ok(distance > 0 && distance <= p.maximumStopDistance);
    assert.ok(t.stakeUnits * (distance + p.fee + 2 * p.sizingExecutionBudgetPerSide) <= cash * p.plannedRisk + 1e-8, 'submission risk violated');
    assert.equal(t.profitFish, (t.payoutUnits - t.stakeUnits) / 10000);
    const ratio = t.exitPrice / t.entry;
    const theoreticalPayout = t.stakeUnits + t.stakeUnits * (ratio - 1) - t.stakeUnits * ratio * p.fee;
    assert.ok(t.payoutUnits <= theoreticalPayout && theoreticalPayout - t.payoutUnits < 1.00000001);
    if (t.reason !== 'evaluation_end') { assert.equal(t.exitIndex - t.exitTriggerIndex, scenario.delaySeconds); natural++; }
    cash += t.payoutUnits - t.stakeUnits; lastExit = t.exitIndex;
  }
  assert.equal(cash / 10000, trial.metrics.finalFish);
  assert.equal(natural, trial.metrics.naturalTrades);
  assert.ok(Math.abs((cash / (p.startingFish * 10000) - 1) - trial.metrics.netReturn) < 1e-12);
  let peak = p.startingFish, dd = 0, previousTime = -Infinity;
  for (const point of curve) {
    assert.ok(point.time >= previousTime, 'curve time reversed'); previousTime = point.time;
    assert.ok(point.equity > 0); peak = Math.max(peak, point.equity); dd = Math.max(dd, 1 - point.equity / peak);
  }
  assert.ok(dd <= trial.metrics.maxDrawdown + 1e-12, 'full-second drawdown must contain sampled-curve drawdown');
  assert.equal(curve.at(-1).equity, cash / 10000);
  checks.push({ method: trial.method, scenario: trial.scenario, trades: trades.length, finalFish: cash / 10000, status: 'passed' });
}
const frozen = fs.readFileSync(path.join(output, 'frozen-policy.json'));
assert.equal(crypto.createHash('sha256').update(frozen).digest('hex'), result.sourceHashes['policy.json']);
const evidence = { status: 'passed', checks, verifiedLedgerCount: checks.length, frozenPolicyUnchanged: true,
  verifiedConstraints: ['cash ledger', 'signal and support identity', 'single use', 'buy and sell delay', 'cooldown', 'allocation', 'submission risk', 'flooring', 'monotone curve', 'drawdown consistency'] };
fs.writeFileSync(path.join(output, 'independent-ledger-audit.json'), JSON.stringify(evidence, null, 2));
console.log(JSON.stringify(evidence));
