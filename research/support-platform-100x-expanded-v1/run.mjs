import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { normalizeBars, aggregateSeconds } from '../support-seconds-v1/strategy.mjs';
import { prepare, simulate, settleLeveraged } from '../support-platform-100x-v1/strategy.mjs';

const here = path.dirname(fileURLToPath(import.meta.url)), root = path.resolve(here, '../..');
const input = process.env.SUPPORT_SECONDS_DATA || '/Users/hsjv/Documents/lianghua/data/btc_1s';
const barsDir = process.env.SUPPORT_BARS_DATA || '/Users/hsjv/Documents/lianghua/backtest/data';
const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
const output = path.resolve(process.argv[2] || path.join(root, 'reports/support-platform-100x-expanded-v1', stamp));
if (fs.existsSync(output)) throw Error('use a new report directory');
fs.mkdirSync(output, { recursive: true });
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const codePaths = ['policy.json', '../support-platform-100x-v1/policy.json', '../support-platform-100x-v1/strategy.mjs', '../support-platform-100x-v1/strategy.test.mjs', 'run.mjs', '../support-seconds-v1/strategy.mjs'];
const sourceHashes = Object.fromEntries(codePaths.map(f => [f, sha(fs.readFileSync(path.join(here, f)))]));
const p = JSON.parse(fs.readFileSync(path.join(here, 'policy.json')));
fs.copyFileSync(path.join(here, 'policy.json'), path.join(output, 'frozen-policy.json'));
fs.writeFileSync(path.join(output, 'preregistration.json'), JSON.stringify({ startedAt: new Date().toISOString(), policy: p, sourceHashes,
  statement: 'User specified 5s/0.01% platform and any negative preceding 30s return. Fixed before results, no optimization.' }, null, 2));
fs.writeFileSync(path.join(output, 'tests.txt'), execFileSync(process.execPath, ['--test', path.join(here, '../support-platform-100x-v1/strategy.test.mjs')], { encoding: 'utf8' }));
const start = Date.parse(p.dataStart) / 1000, end = Date.parse(p.evaluationEndExclusive) / 1000;
const originalStart = Date.parse('2025-10-01T00:00:00Z') / 1000, offset = start - originalStart;
const hashes = {};
function readFloat(name) {
  const f = path.join(input, name + '.f32'), b = fs.readFileSync(f); hashes[f] = sha(b);
  const values = new Float32Array(b.buffer, b.byteOffset, b.length / 4);
  if (offset < 0 || offset + end - start > values.length) throw Error('requested period not covered');
  return values.subarray(offset, offset + end - start);
}
const data = { close: readFloat('close'), high: readFloat('high'), low: readFloat('low') };
const fourFile = path.join(barsDir, 'btc_4h_klines.json'), hourFile = path.join(barsDir, 'btc_1h_klines.json');
const fourBytes = fs.readFileSync(fourFile), hourBytes = fs.readFileSync(hourFile);
hashes[fourFile] = sha(fourBytes); hashes[hourFile] = sha(hourBytes);
const four = normalizeBars(JSON.parse(fourBytes), 14400), hours = normalizeBars(JSON.parse(hourBytes), 3600);
for (let i = 0; i < data.close.length; i++) if (!(Number.isFinite(data.high[i]) && data.low[i] > 0 && data.low[i] <= data.close[i] && data.close[i] <= data.high[i])) throw Error('invalid second ' + i);
function crossCheck(reference, width) {
  const lookup = new Map(reference.map(b => [b.time, b])), aggregated = aggregateSeconds(data, width, start); let max = 0;
  for (const b of aggregated) {
    const other = lookup.get(b.time); assert.ok(other);
    for (const key of ['high', 'low', 'close']) { const diff = Math.abs(b[key] - other[key]); max = Math.max(max, diff); assert.ok(diff <= .02); }
  }
  return { compared: aggregated.length, maximumErrorUSD: max, fields: ['high', 'low', 'close'] };
}
const audit = { loadedSeconds: data.close.length, evaluationSeconds: end - Date.parse(p.evaluationStart) / 1000,
  sha256: hashes, hourly: crossCheck(hours, 3600), fourHourly: crossCheck(four, 14400),
  originalTimestampsAndArchiveChecksumsFullyVerified: false,
  timeline: 'Existing implicit index timeline; corroborated by higher-timeframe bars, not raw timestamp reconstruction.' };
const prepared = prepare(data, four, p);
fs.writeFileSync(path.join(output, 'events.jsonl'), prepared.events.map(e => JSON.stringify(e)).join('\n') + '\n');
const eventSummary = { ...prepared.stats, supportTouchEvents: prepared.events.length, platformSignals: prepared.candidates.length,
  endings: prepared.events.reduce((r, e) => { r[e.endReason] = (r[e.endReason] || 0) + 1; return r; }, {}) };
console.log(JSON.stringify({ phase: 'prepared', output, ...eventSummary }));

const trials = [], ledgerChecks = [];
function validate(t, currentPrepared = prepared, currentPolicy = p) {
  const p = currentPolicy;
  const start = Date.parse(p.dataStart) / 1000;
  const scenario = p.scenarios.find(s => s.name === t.scenario);
  let cash = p.startingFish * 10000, previousExit = -Infinity; const seen = new Set();
  const events = new Map(currentPrepared.events.map(e => [e.id, e]));
  for (const trade of t.trades) {
    const e = events.get(trade.eventId); assert.ok(e);
    assert.equal(trade.signalIndex, e.trigger); assert.ok(trade.signalIndex >= previousExit);
    assert.ok(trade.entryIndex > previousExit, 'positions must never overlap');
    assert.ok(!seen.has(e.id)); seen.add(e.id);
    assert.equal(trade.entryIndex - trade.signalIndex, scenario.delaySeconds);
    assert.equal(trade.stakeUnits, p.stakeFish * 10000); assert.ok(cash >= trade.stakeUnits);
    assert.equal(trade.liquidationPrice, trade.entry * .99);
    assert.ok(e.platform.first >= e.breach && e.platform.range <= p.platform.maximumRangeFraction);
    assert.ok(e.platform.high < e.support && e.platform.precedingDeclineFraction > 0);
    if (trade.reason === 'liquidated') {
      assert.equal(trade.payoutUnits, 0);
      if (t.liquidationMode === 'grid_close') assert.equal((start + trade.exitIndex + 1) % p.liquidationIntervalSeconds, t.phase);
    } else {
      assert.equal(trade.payoutUnits, settleLeveraged(trade.stakeUnits, trade.entry, trade.exitPrice, p.leverage, p.fee));
      if (trade.reason === 'profit_target') {
        assert.equal(trade.exitIndex - trade.exitTriggerIndex, scenario.delaySeconds);
        assert.ok(trade.targetTriggerMarginReturn >= p.targetNetMarginReturn - 1e-12);
      }
    }
    cash += trade.payoutUnits - trade.stakeUnits; previousExit = trade.exitIndex;
  }
  assert.equal(t.metrics.finalFish, cash / 10000);
  let peak = p.startingFish, curveDD = 0, previousTime = -Infinity;
  for (const q of t.curve) { assert.ok(q.time >= previousTime); previousTime = q.time; peak = Math.max(peak, q.equity); curveDD = Math.max(curveDD, 1 - q.equity / peak); }
  assert.ok(curveDD <= t.metrics.maxDrawdown + 1e-12);
  assert.equal(t.curve.at(-1).equity, cash / 10000);
  // Signals are one per 15m support selection, nevertheless check actual rolling
  // request counts against the site's shared buy/sell limits (20/min, 300/24h).
  const requests = t.trades.flatMap(x => [x.signalIndex, ...(x.reason === 'profit_target' ? [x.exitTriggerIndex] : [])]).sort((a, b) => a - b);
  let minuteLeft = 0, dayLeft = 0, maxMinute = 0, maxDay = 0;
  for (let i = 0; i < requests.length; i++) {
    while (requests[minuteLeft] <= requests[i] - 60) minuteLeft++;
    while (requests[dayLeft] <= requests[i] - 86400) dayLeft++;
    maxMinute = Math.max(maxMinute, i - minuteLeft + 1); maxDay = Math.max(maxDay, i - dayLeft + 1);
  }
  assert.ok(maxMinute <= 20 && maxDay <= 300);
  ledgerChecks.push({ scenario: t.scenario, phase: t.phase, mode: t.liquidationMode, trades: t.trades.length,
    finalFish: cash / 10000, maxRequestsPerMinute: maxMinute, maxRequestsPer24h: maxDay, status: 'passed' });
}
for (const scenario of p.scenarios) {
  for (let phase = 0; phase < p.liquidationIntervalSeconds; phase++) {
    const t = simulate(data, prepared, p, scenario, phase); validate(t); trials.push(t);
    const name = scenario.name + '--phase' + phase;
    fs.writeFileSync(path.join(output, name + '-trades.json'), JSON.stringify(t.trades, null, 2));
    if (phase === p.primaryLiquidationPhaseSeconds) fs.writeFileSync(path.join(output, name + '-curve.json'), JSON.stringify(t.curve));
  }
  const conservative = simulate(data, prepared, p, scenario, 0, 'each_second_low'); validate(conservative); trials.push(conservative);
  fs.writeFileSync(path.join(output, scenario.name + '--conservative-trades.json'), JSON.stringify(conservative.trades, null, 2));
  console.log(JSON.stringify({ phase: 'scenario_completed', scenario: scenario.name,
    nominal: trials.find(t => t.scenario === scenario.name && t.liquidationMode === 'grid_close' && t.phase === 0).metrics,
    conservative: conservative.metrics }));
}
const monthly = [];
for (let k = 0; k < 12; k++) {
  const monthStart = Date.UTC(2025, 9 + k, 1) / 1000;
  const monthEnd = Date.UTC(2025, 10 + k, 1) / 1000;
  const evaluation = Math.max(Date.parse(p.evaluationStart) / 1000, monthStart);
  const warmup = Math.max(start, evaluation - 86400), from = warmup - start, to = monthEnd - start;
  const d = Object.fromEntries(Object.entries(data).map(([name, values]) => [name, values.subarray(from, to)]));
  const mp = { ...p, dataStart: new Date(warmup * 1000).toISOString(), evaluationStart: new Date(evaluation * 1000).toISOString(),
    evaluationEndExclusive: new Date(monthEnd * 1000).toISOString() };
  const ready = prepare(d, four, mp);
  const month = new Date(monthStart * 1000).toISOString().slice(0,7);
  const mts = [];
  fs.writeFileSync(path.join(output, month + '-events.jsonl'), ready.events.map(e => JSON.stringify(e)).join('\n') + '\n');
  for (const scenario of p.scenarios) for (const mode of ['grid_close', 'each_second_low']) {
    const t = simulate(d, ready, mp, scenario, 0, mode); validate(t, ready, mp);
    ledgerChecks.at(-1).period = month;
    fs.writeFileSync(path.join(output, month + '--' + scenario.name + '--' + mode + '-trades.json'), JSON.stringify(t.trades, null, 2));
    const { trades, curve, ...summary } = t; mts.push(summary);
  }
  monthly.push({ month, evaluationStart: mp.evaluationStart, endExclusive: mp.evaluationEndExclusive,
    startingFish: mp.startingFish, accountReset: true, candidateSignals: ready.candidates.length,
    btcReturn: d.close.at(-1) / d.close[evaluation - warmup] - 1, trials: mts });
  console.log(JSON.stringify({ phase: 'month_completed', month, scenarios: mts.filter(t => t.liquidationMode === 'grid_close').map(t => ({ scenario: t.scenario, ...t.metrics })) }));
}
for (const [f, digest] of Object.entries(sourceHashes)) assert.equal(sha(fs.readFileSync(path.join(here, f))), digest);
fs.writeFileSync(path.join(output, 'ledger-audit.json'), JSON.stringify({ status: 'passed', checks: ledgerChecks }, null, 2));
const phases = p.scenarios.map(s => {
  const ts = trials.filter(t => t.scenario === s.name && t.liquidationMode === 'grid_close');
  return { scenario: s.name, minimumReturn: Math.min(...ts.map(t => t.metrics.netReturn)), maximumReturn: Math.max(...ts.map(t => t.metrics.netReturn)),
    minimumLiquidations: Math.min(...ts.map(t => t.metrics.liquidations)), maximumLiquidations: Math.max(...ts.map(t => t.metrics.liquidations)) };
});
const results = { completedAt: new Date().toISOString(), version: p.version, sourceHashes, policy: p, evidenceStatus: p.evidenceStatus,
  liveQualified: false, parameterOptimizationPerformed: false, audit, eventSummary, liquidationPhaseSensitivity: phases,
  trials: trials.map(({ trades, curve, ...t }) => t), monthlyIndependent: monthly, limitations: [
    'BTC only. Available seconds Oct 1 2025 to Sep 30 2026. First day excluded as support warmup; annual evaluation Oct 2 2025 to Sep 30 2026 UTC.',
    'Fixed strategy expanded retrospectively to earlier months; not independent out-of-sample evidence. No parameter optimization.',
    'Default 15s venue liquidation setting assumed; actual period, phase and quote latency not observed.',
    '1s candle-close observation; phase tests do not resolve sub-second timing. Conservative low scan is a sensitivity bound, not venue behaviour.',
    'Delay and slippage scenarios are assumptions, not measured live fills; no outages or rejected orders simulated.',
    'No manual stops, holding limit or account halt; one-fish fixed stake, single position. Evaluation-end open position forcibly closed and separately marked.',
    'Monthly accounts reset to 50 fish, each month force-closes positions. Monthly results cannot be compounded into the annual account.',
    'Original second timestamps/ZIP checksums unavailable for full period; original import has a known gap-counter issue.' ] };
fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
const pct = x => x === null ? '—' : (x * 100).toFixed(4) + '%', num = x => x === null ? '—' : x.toFixed(3);
const nominal = trials.filter(t => t.liquidationMode === 'grid_close' && t.phase === 0);
const primary = nominal[0];
const report = [
  '# 支撑下方5秒平台＋100倍＋净盈利10%：扩大范围回测', '',
  '固定规则实际回测，没有网站买卖。参数来自本轮用户修改，没有按结果调参；所有结果属于探索性历史研究。', '',
  '## 冻结规则', '',
  '- BTCUSDT，连续评估2025-10-02 00:00至2026-10-01 00:00 UTC（364天），2025年10月1日仅用于支撑热身；此外逐月重置50条本金独立测试，2025年10月为10月2日起的部分月。',
  '- 支撑沿用15分钟已确认低点：严格低于左右各2根低点，右侧完成后才使用；确认时ATR14冻结，接触区域上沿为S＋0.25ATR，低点起24小时内有效。一处支撑只买一次。',
  '- 不要求4小时上涨。接触支撑后，秒收盘下降至S以下，最多30分钟内寻找平台。平台开始前30秒收盘价收益必须严格为负，不设跌幅门槛。',
  '- 平台为连续5根完整秒线，其最高价全部低于S，最高价/最低价−1≤0.01%；条件成立直接发买入，不等待突破或反弹。',
  '- 50条本金，每笔固定1条，100倍，最多1仓。不使用旧版6小时冷却、日亏损暂停或5%回撤停机，也不添加人工止损和持有超时；亏损退出由平台强平决定。',
  '- 以保证金扣除网站平仓手续费和取整后的净收益≥10%作为卖出触发条件。100倍平价平仓费约为保证金2%，目标对应标的上涨约0.120024%。',
  '- 买卖按触发后的延迟秒收盘成交；场景额外偏差买加卖减。达到10%只发卖出请求，最终收益由延迟成交决定，不保证实际实现10%。',
  '- 强平线为真实入场价×0.99，默认每15秒以当前秒收盘检查，强平到账为0。扫描偏移0秒为展示口径，另外测试0—14秒全部偏移；卖出请求途中仍可能被强平。',
  '- 每秒低价强平另列保守敏感性对照；评估期末未退出仓位按最后秒收盘强制结算，独立列作边界平仓。', '',
  '## 数据与验证', '',
  `评估${audit.evaluationSeconds.toLocaleString('en-US')}秒，连同热身载入${audit.loadedSeconds.toLocaleString('en-US')}秒。全部高低收合法，与${audit.hourly.compared}根小时线和${audit.fourHourly.compared}根4小时线高/低/收一致；最大误差${audit.hourly.maximumErrorUSD.toFixed(8)}美元。`, '',
  `9项专项测试通过，${ledgerChecks.length}组逐笔账本、延迟、强平时刻、单仓、固定金额及限频检查通过。源代码、参数和数据保存SHA-256。`, '',
  '原始时间戳与归档校验和未完整保留，聚合一致性不能替代全年原始时间轴核验。平台真实强平周期和取价延迟未实测。', '',
  '## 15秒强平，展示偏移0秒', '',
  '| 场景 | 净收益 | 最大回撤 | 自然平仓 | 胜率 | 爆仓 | 盈利目标退出 | 实际低于10%的目标退出 | 期末鱼干 |',
  '|---|---:|---:|---:|---:|---:|---:|---:|---:|',
];
for (const t of nominal) { const m = t.metrics;
  report.push(`| ${t.scenario} | ${pct(m.netReturn)} | ${pct(m.maxDrawdown)} | ${m.naturalTrades} | ${pct(m.winRate)} | ${m.liquidations} | ${m.targetClosures} | ${m.targetClosuresBelowRequestedReturn} | ${m.finalFish.toFixed(4)} |`);
}
report.push('', '无额外偏差场景仍收取网站平仓费。2bp、5bp指每侧额外不利偏差；delay为买卖共同延迟秒数。', '',
  '## 强平扫描相位敏感性', '', '| 场景 | 15种偏移最低收益 | 最高收益 | 爆仓次数范围 |', '|---|---:|---:|---:|');
for (const q of phases) report.push(`| ${q.scenario} | ${pct(q.minimumReturn)} | ${pct(q.maximumReturn)} | ${q.minimumLiquidations}—${q.maximumLiquidations} |`);
report.push('', '偏移是未知执行条件，不是调参候选；不能挑最有利的一行作为现场可实现收益。', '',
  '## 每秒低价强平的保守对照', '', '| 场景 | 净收益 | 最大回撤 | 自然平仓 | 爆仓 |', '|---|---:|---:|---:|---:|');
for (const t of trials.filter(t => t.liquidationMode === 'each_second_low')) report.push(`| ${t.scenario} | ${pct(t.metrics.netReturn)} | ${pct(t.metrics.maxDrawdown)} | ${t.metrics.naturalTrades} | ${t.metrics.liquidations} |`);
report.push('', '## 主要诊断', '',
  `支撑接触${eventSummary.supportTouchEvents}次，满足“此前30秒下跌＋5秒平台”的候选${eventSummary.platformSignals}次。主场景自然平仓${primary.metrics.naturalTrades}笔，收益${pct(primary.metrics.netReturn)}，最大回撤${pct(primary.metrics.maxDrawdown)}。`, '',
  `单笔平均盈利保证金收益${pct(primary.metrics.averageWinMarginReturn)}，平均亏损${pct(primary.metrics.averageLossMarginReturn)}，PF=${num(primary.metrics.profitFactor)}，持有时长中位数${primary.metrics.medianHoldingSeconds ?? '—'}秒，最长${primary.metrics.maximumHoldingSeconds ?? '—'}秒。`, '',
  `主场景有${primary.metrics.recoveredAfterObservedLiquidationCross}笔曾在秒内最低价触及爆仓线、但未被15秒收盘扫描强平后退出。该计数不等于可重复的套利，需要结合保守对照和相位范围看待。`, '',
  '若每笔盈利恰为投入的10%、爆仓亏损100%，理论盈亏平衡胜率约90.91%；实际应按真实成交后的平均盈亏计算。不能用目标触发次数代替盈利次数，也不能遗漏期末未平仓的损失。', '',
  `期末边界平仓${primary.metrics.boundaryClosures}笔；账户收益包含这笔结算，胜率和自然交易数不包含。跳过原因：${JSON.stringify(primary.metrics.filters)}。`, '',
  '本轮固定规则扩大历史范围，不是独立样本外验证；9月及其他历史价格已被以前研究查看过。没有实测成交偏差、拒单、断线或会话失效；始终liveQualified=false。', '',
  '![净值与成本敏感性](equity.svg)', '', '## 复现与证据', '', '```sh',
  'node --test research/support-platform-100x-v1/strategy.test.mjs', 'node research/support-platform-100x-expanded-v1/run.mjs', '```', '',
  '每次创建新目录。frozen-policy.json为参数，events.jsonl为全部机会，results.json为全年64组及逐月96组汇总，各-trades.json为逐笔，ledger-audit.json为账本核验，tests.txt为测试输出。', '');
report.push('## 逐月独立账户', '',
  '每月重置50条本金，固定每笔1条；月末强制结算。此表用于观察不同月份，不能把收益相乘当作全年账户。2025年10月从2日开始。每格为账户收益；强平15秒、偏移0。', '',
  '| 月份 | BTC涨跌 | 2秒/无额外偏差 | 2秒/每侧2bp | 5秒/每侧5bp | 5秒/无额外偏差 |', '|---|---:|---:|---:|---:|---:|');
for (const m of monthly) {
  report.push(`| ${m.month} | ${pct(m.btcReturn)} | ${m.trials.filter(t => t.liquidationMode === 'grid_close').map(t => pct(t.metrics.netReturn)).join(' | ')} |`);
}
report.push('', '## 跨月稳定性与连续账户', '');
for (const scenario of p.scenarios) {
  const rows = monthly.map(m => ({ month: m.month, ...m.trials.find(t => t.scenario === scenario.name && t.liquidationMode === 'grid_close').metrics }));
  const winners = rows.filter(r => r.netReturn > 0).length, worst = rows.reduce((a,b) => a.netReturn < b.netReturn ? a : b), best = rows.reduce((a,b) => a.netReturn > b.netReturn ? a : b);
  report.push(`- ${scenario.name}：独立账户盈利${winners}/12个月；最差${worst.month} ${pct(worst.netReturn)}，最好${best.month} ${pct(best.netReturn)}。`);
}
report.push('', '全年账户连续持仓与现金，只有全年末才强制结算。如果现金不足1条，后续信号跳过，不追加资金。全年最大回撤按每秒扣费后的可退出净值计算；逐月独立账户另行重置。', '',
  '全年64组执行条件＋逐月96组共160组均经过账本核验。逐月只展示相位0，不宣称覆盖每月全部扫描时点。', '');
fs.writeFileSync(path.join(output, 'monthly-independent.json'), JSON.stringify(monthly, null, 2));
fs.writeFileSync(path.join(output, '报告.md'), report.join('\n'));

const colors = ['#276baf', '#dd7f32', '#bf3f57', '#4b9d80'];
const allPoints = nominal.flatMap(t => t.curve.map(q => q.equity));
const minY = allPoints.reduce((a, b) => Math.min(a, b), p.startingFish) - .2, maxY = allPoints.reduce((a, b) => Math.max(a, b), p.startingFish) + .2;
const evalStart = Date.parse(p.evaluationStart) / 1000;
const x = time => 80 + (time - evalStart) / (end - evalStart) * 990, y = equity => 65 + (maxY - equity) / (maxY - minY) * 390;
let svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1140" height="600"><rect width="100%" height="100%" fill="#fafaf7"/><g font-family="Arial, sans-serif" fill="#24313b"><text x="80" y="30" font-size="21">支撑下方5秒平台｜100倍｜净盈利10%触发卖出</text><text x="80" y="52" font-size="12">2025-10-02—2026-09-30 UTC · 每笔1条 · 初始50条 · 15秒强平/展示偏移0 · 费用已计入</text>';
for (let j = 0; j <= 5; j++) { const e = minY + (maxY - minY) * j / 5;
  svg += `<line x1="80" x2="1070" y1="${y(e)}" y2="${y(e)}" stroke="#dce1e4"/><text x="68" y="${y(e) + 4}" text-anchor="end" font-size="12">${e.toFixed(2)}</text>`;
}
for (const [year, month] of [[2025, 9], [2026, 0], [2026, 3], [2026, 6], [2026, 9]]) { const t = Date.UTC(year, month, 1) / 1000;
  svg += `<text x="${x(t)}" y="480" text-anchor="middle" font-size="12">${year}-${String(month+1).padStart(2,'0')}</text>`;
}
const names = ['2秒/无额外偏差', '2秒/每侧2bp', '5秒/每侧5bp', '5秒/无额外偏差'];
nominal.forEach((t, i) => {
  const pts = t.curve.map(q => `${x(q.time).toFixed(2)},${y(q.equity).toFixed(2)}`).join(' ');
  svg += `<polyline points="${pts}" fill="none" stroke="${colors[i]}" stroke-width="2"/><line x1="${80 + i * 255}" x2="${102 + i * 255}" y1="515" y2="515" stroke="${colors[i]}" stroke-width="3"/><text x="${110 + i * 255}" y="519" font-size="13">${names[i]}</text>`;
});
svg += '<text x="80" y="560" font-size="12" fill="#65737c">扫描相位未知，完整范围见报告；达到10%时发请求，实际成交收益可能更低。</text></g></svg>';
fs.writeFileSync(path.join(output, 'equity.svg'), svg);
console.log(JSON.stringify({ phase: 'completed', output, report: path.join(output, '报告.md'), annualTrials: trials.length, monthlyTrials: monthly.reduce((a,m) => a+m.trials.length,0), phases }));
