import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { SCALE, settle, normalizeBars, aggregateSeconds, prepare, simulate } from './strategy.mjs';

const here = path.dirname(fileURLToPath(import.meta.url)), root = path.resolve(here, '../..');
const secondsDir = process.env.SUPPORT_SECONDS_DATA || '/Users/hsjv/Documents/lianghua/data/btc_1s';
const barsDir = process.env.SUPPORT_BARS_DATA || '/Users/hsjv/Documents/lianghua/backtest/data';
const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
const output = path.resolve(process.argv[2] || path.join(root, 'reports/support-seconds-v1', stamp));
if (fs.existsSync(output)) throw Error('use a new output directory; do not overwrite experiments');
fs.mkdirSync(output, { recursive: true });
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const sourceHashes = Object.fromEntries(['policy.json', 'strategy.mjs', 'strategy.test.mjs', 'run.mjs', 'audit.mjs', 'spotcheck.py'].map(f => [f, sha(fs.readFileSync(path.join(here, f)))]));
const policy = JSON.parse(fs.readFileSync(path.join(here, 'policy.json')));
fs.copyFileSync(path.join(here, 'policy.json'), path.join(output, 'frozen-policy.json'));
fs.writeFileSync(path.join(output, 'preregistration.json'), JSON.stringify({ startedAt: new Date().toISOString(), sourceHashes, policy,
  statement: 'Rules and scenarios fixed before reading results; previously viewed data; no optimization or live qualification.' }, null, 2));
fs.writeFileSync(path.join(output, 'tests.txt'), execFileSync(process.execPath, ['--test', path.join(here, 'strategy.test.mjs')], { encoding: 'utf8' }));

const hashes = {};
function loadFloat(name) {
  const filename = path.join(secondsDir, name + '.f32'), b = fs.readFileSync(filename);
  if (b.length % 4) throw Error('invalid float array length'); hashes[filename] = sha(b);
  return new Float32Array(b.buffer, b.byteOffset, b.length / 4);
}
function loadBars(name, width) {
  const filename = path.join(barsDir, name), b = fs.readFileSync(filename); hashes[filename] = sha(b);
  return normalizeBars(JSON.parse(b), width);
}
const data = { close: loadFloat('close'), high: loadFloat('high'), low: loadFloat('low') };
const hourly = loadBars('btc_1h_klines.json', 3600), four = loadBars('btc_4h_klines.json', 14400);
for (const name of ['meta.json', 'fetch_1s.mjs']) { const f = path.join(secondsDir, name); hashes[f] = sha(fs.readFileSync(f)); }
const start = Date.parse(policy.dataStart) / 1000, end = Date.parse(policy.dataEndExclusive) / 1000;
if (data.close.length !== end - start || data.high.length !== data.close.length || data.low.length !== data.close.length) throw Error('expected exactly one complete year of seconds');
for (let i = 0; i < data.close.length; i++) {
  if (!(Number.isFinite(data.high[i]) && data.low[i] > 0 && data.low[i] <= data.close[i] && data.close[i] <= data.high[i])) throw Error('invalid second OHLC at ' + i);
}
function crossCheck(width, reference) {
  const aggregated = aggregateSeconds(data, width, start), byTime = new Map(reference.map(b => [b.time, b]));
  let maxDifference = 0;
  for (const b of aggregated) {
    const ref = byTime.get(b.time); if (!ref) throw Error('reference bar missing');
    for (const k of ['high', 'low', 'close']) {
      const diff = Math.abs(b[k] - ref[k]); maxDifference = Math.max(maxDifference, diff);
      if (diff > .02) throw Error(`aggregation mismatch ${width} ${b.time} ${k} ${diff}`);
    }
  }
  return { compared: aggregated.length, fields: ['high', 'low', 'close'], toleranceUSD: .02, maximumDifferenceUSD: maxDifference };
}
const audit = { symbol: 'BTCUSDT', seconds: data.close.length, start: policy.dataStart, endExclusive: policy.dataEndExclusive,
  indexTimelineOrigin: 'pinned from existing downloader job list; original timestamps not retained',
  firstClose: data.close[0], lastClose: data.close.at(-1), hourly: crossCheck(3600, hourly), fourHourly: crossCheck(14400, four),
  sha256: hashes, originalArchiveChecksumsIndependentlyVerified: false,
  limitations: ['Original ZIP checksums and per-second timestamps were not preserved.',
    'Old importer has a gap-counter bug; aggregation consistency is corroboration, not proof of every timestamp.',
    'Float32 rounding; 1s OHLC without intrasecond ordering; last traded price is not a measured site fill.'] };
const spotFile = process.env.SUPPORT_SPOTCHECK_REPORT || path.join(root, 'data/support-seconds-v1/spotcheck-20261002.json');
if (fs.existsSync(spotFile)) {
  const bytes = fs.readFileSync(spotFile); hashes[spotFile] = sha(bytes);
  audit.originalArchiveSpotCheck = JSON.parse(bytes);
  fs.writeFileSync(path.join(output, 'original-archive-spotcheck.json'), bytes);
}
fs.writeFileSync(path.join(output, 'data-audit.json'), JSON.stringify(audit, null, 2));
console.log(JSON.stringify({ phase: 'data_audit_passed', seconds: audit.seconds, hourly: audit.hourly, fourHourly: audit.fourHourly, output }));

const prepared = prepare(data, hourly, four, policy);
const eventDiagnostics = { ...prepared.counts, supportTouchEvents: prepared.events.length,
  eventsByEndReason: prepared.events.reduce((r, e) => { r[e.endReason] = (r[e.endReason] || 0) + 1; return r; }, {}), methods: {} };
for (const method of policy.methods) {
  const eligible = prepared.events.filter(e => e.triggers[method] !== undefined);
  const improvements = eligible.map(e => (data.close[e.touch] / data.close[e.triggers[method]] - 1) * 10000);
  const waits = eligible.map(e => e.triggers[method] - e.touch).sort((a, b) => a - b);
  eventDiagnostics.methods[method] = { confirmations: eligible.length, coverage: eligible.length / Math.max(1, prepared.events.length),
    meanTriggerPriceImprovementBpOnConfirmedSubset: improvements.length ? improvements.reduce((a, b) => a + b, 0) / improvements.length : null,
    medianWaitSeconds: waits.length ? waits[Math.floor(waits.length / 2)] : null,
    warning: 'Conditional on confirmation; includes unexecuted events; not profit or proof of alpha.' };
}
fs.writeFileSync(path.join(output, 'events.jsonl'), prepared.events.map(e => JSON.stringify(e)).join('\n') + '\n');
console.log(JSON.stringify({ phase: 'events_prepared', ...eventDiagnostics }));
const trials = [];
for (const scenario of policy.scenarios) for (const method of policy.methods) {
  const t0 = performance.now(), trial = simulate(data, prepared, policy, method, scenario); trials.push(trial);
  const name = method + '--' + scenario.name;
  fs.writeFileSync(path.join(output, name + '-trades.json'), JSON.stringify(trial.trades, null, 2));
  fs.writeFileSync(path.join(output, name + '-curve.json'), JSON.stringify(trial.curve));
  console.log(JSON.stringify({ phase: 'trial', method, scenario: scenario.name, ...trial.metrics, elapsedSeconds: (performance.now() - t0) / 1000 }));
}
function benchmark(fraction) {
  const initial = Math.round(policy.startingFish * SCALE), stake = Math.floor(initial * fraction), entryIndex = policy.warmupSeconds + 2;
  const entry = data.close[entryIndex], cash = initial - stake, curve = []; let peak = initial, dd = 0;
  for (let i = entryIndex; i < data.close.length; i++) {
    const eq = cash + settle(stake, entry, data.close[i], policy.fee); peak = Math.max(peak, eq); dd = Math.max(dd, 1 - eq / peak);
    if ((i + 1) % 3600 === 0 || i === data.close.length - 1) curve.push({ time: start + i + 1, equity: eq / SCALE });
  }
  return { fraction, netReturn: curve.at(-1).equity / policy.startingFish - 1, maxDrawdown: dd, finalFish: curve.at(-1).equity, curve,
    assumptions: 'Fixed initial allocation; 2s entry delay; zero extra slippage; no strategy stops/account halts.' };
}
const benchmarks = [benchmark(.2), benchmark(1)];
const result = { version: policy.version, completedAt: new Date().toISOString(), sourceHashes, evidenceStatus: policy.evidenceStatus,
  liveQualified: false, parameterOptimizationPerformed: false, policy,
  evaluation: { start: new Date((start + policy.warmupSeconds) * 1000).toISOString(), endExclusive: policy.dataEndExclusive,
    quartersCarryCashAndPositions: true, independentOutOfSample: false },
  audit, eventDiagnostics, benchmarks: benchmarks.map(({ curve, ...b }) => b), trials: trials.map(({ trades, curve, ...t }) => t),
  limitations: ['One previously researched BTC year; ETH not tested; no independent out-of-sample evidence.',
    'Delay/slippage are sensitivity assumptions, not measured platform distributions.',
    '1s close triggers; stops detect completed 1s lows then execute after delay; intrasecond ordering unknown.',
    'No API failures, outages, rate-limit rejections or session expiry simulation. No live adapter.',
    'Order amount fixed at submission; delayed fills may exceed planned risk; no hindsight cancellation.',
    'Quarter figures describe one continuous portfolio, not reset or pristine test folds.'] };
for (const [f, hash] of Object.entries(sourceHashes)) if (sha(fs.readFileSync(path.join(here, f))) !== hash) throw Error('source changed during experiment');
fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(result, null, 2));
const ledgerAudit = JSON.parse(execFileSync(process.execPath, [path.join(here, 'audit.mjs'), output], { encoding: 'utf8' }));
console.log(JSON.stringify({ phase: 'independent_ledger_audit', status: ledgerAudit.status, trials: ledgerAudit.verifiedLedgerCount }));

const labels = { touch: '触价入场', minute_confirmation: '每分钟采样确认', second_confirmation: '每秒采样确认' };
const pct = x => x === null ? '—' : (x * 100).toFixed(4) + '%', num = x => x === null ? '—' : x.toFixed(3);
const main = trials.filter(t => t.scenario === policy.scenarios[0].name), second = main.find(t => t.method === 'second_confirmation');
const report = [
  '# 支撑回踩＋秒级确认：第一版历史研究', '',
  '完成于 ' + new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'full', timeStyle: 'medium' }).format(new Date()) + '。', '',
  '实际执行的固定规则回测，没有按结果调参，没有提交网站订单。数据已被以前的研究查看过，全部结果是探索性证据，不是独立样本外验证。', '',
  '## 冻结规则', '',
  '- BTCUSDT、1倍、50条鱼干、单仓。秒线覆盖2025-10-01至2026-09-30 UTC；首24小时用于支撑热身，实际评估从2025-10-02 UTC起。',
  '- 最近已收盘4小时线收盘价>EMA200且EMA50>EMA200才允许入场。EMA以初始N根均值初始化，预热从2025年3月开始。',
  '- 15分钟低点严格低于左右各2根低点，右侧2根完成后确认。确认时的Wilder ATR14冻结；中心S，区域S±0.25ATR，失效线S−0.5ATR。支撑从低点开始最多保留96根15分钟线。',
  '- 每根15分钟线完成后，从未使用、未失效的支撑中选最近且低于现价的一个；秒收盘进入上沿以下形成回调事件。一处支撑只使用一次，事件最长等待30分钟。',
  '- 秒级确认：至少15秒不创新的秒线最低价，当前秒收盘≥S、≤S＋ATR，并突破此前10秒最高价。分钟对照为完全相同条件，仅每分钟结束检查；触价对照在事件开始触发。',
  '- 每笔投入≤净值20%、计划损失≤净值0.25%；预留0.02%平仓费及每侧0.05%执行预算。最低1条、向下取整4位。止损距离超过4%不入场。金额在发请求时确定，不能利用后续价格取消或改金额。',
  '- 初始止损是冻结失效线；每小时更新“入场后最高完整小时收盘−3倍小时ATR14”，只上移。趋势关闭或持有满48小时退出；平仓后冷却6小时。',
  '- 北京时间日内净值损失1%退出并暂停至次日，净值高点回撤5%退出并永久停机。跨季度继承现金、持仓与风控，不重置。阈值不能保证损失上限。',
  '- 平仓收费一次，按网站1倍公式floor至0.0001条。使用信号后延迟的秒收盘成交，买加卖减场景偏差；止损不假定能按旧止损价成交。', '',
  '## 数据检查', '',
  `检查${audit.seconds.toLocaleString('en-US')}根秒线，全部数值及高低收关系通过。对照${audit.hourly.compared}根小时线和${audit.fourHourly.compared}根4小时线的高/低/收，最大误差分别${audit.hourly.maximumDifferenceUSD.toFixed(8)}、${audit.fourHourly.maximumDifferenceUSD.toFixed(8)}美元（容差0.02美元）。全部输入和代码保存SHA-256。`, '',
  '旧导入器未保存原始时间戳和ZIP校验和，缺口计数有已知问题。聚合一致性不能替代逐条时间戳核验。原数据只读。', '',
  audit.originalArchiveSpotCheck ? `另从公开归档抽查固定3天，成功核验${audit.originalArchiveSpotCheck.verified_seconds.toLocaleString('en-US')}秒原始时间戳和高/低/收float32数值；失败请求的原因保留在original-archive-spotcheck.json。抽查不能证明全年完整性。` : '未执行原始归档抽查。', '',
  '12项策略测试通过，15组逐笔账本独立重算通过，检查成交延迟、单仓、冷却、金额和风险预算；详见tests.txt和independent-ledger-audit.json。', '',
  '## 实际结果', '',
];
for (const s of policy.scenarios) {
  report.push('### ' + s.name, '', `买卖延迟均${s.delaySeconds}秒，每侧额外不利偏差${(s.slippagePerSide * 10000).toFixed(0)}bp；平仓2bp另计。`, '',
    '| 入场方式 | 净收益 | 最大回撤 | 自然平仓数 | 胜率 | PF | 平均持有小时 | 期末鱼干 |',
    '|---|---:|---:|---:|---:|---:|---:|---:|');
  for (const t of trials.filter(t => t.scenario === s.name)) { const m = t.metrics;
    report.push(`| ${labels[t.method]} | ${pct(m.netReturn)} | ${pct(m.maxDrawdown)} | ${m.naturalTrades} | ${pct(m.winRate)} | ${num(m.profitFactor)} | ${num(m.averageHoldingHours)} | ${m.finalFish.toFixed(4)} |`);
  }
  report.push('');
}
report.push('### 基准', '', '| 基准 | 净收益 | 最大回撤 |', '|---|---:|---:|', '| 现金 | 0% | 0% |');
for (const b of benchmarks) report.push(`| 初始${b.fraction * 100}% BTC持有＋现金 | ${pct(b.netReturn)} | ${pct(b.maxDrawdown)} |`);
report.push('', '基准不再平衡，使用2秒入场延迟、零额外偏差；不含策略止损和账户停机，仅供行情与敞口背景比较。', '',
  '### 连续组合季度归因（2秒延迟、零额外偏差）', '', '| 截止日期UTC（不含） | 触价 | 分钟确认 | 秒级确认 |', '|---|---:|---:|---:|');
second.metrics.quarters.forEach((q, i) => report.push(`| ${q.endExclusive.slice(0, 10)} | ${main.map(t => pct(t.metrics.quarters[i].netReturn)).join(' | ')} |`));
report.push('', '### 机会覆盖与触发价格', '', `确认低点${eventDiagnostics.pivots}个，支撑回调事件${eventDiagnostics.supportTouchEvents}次。`, '',
  '| 方式 | 确认次数 | 覆盖率 | 确认样本平均价格改善bp | 等待中位秒 |', '|---|---:|---:|---:|---:|');
for (const method of policy.methods) { const d = eventDiagnostics.methods[method];
  report.push(`| ${labels[method]} | ${d.confirmations} | ${pct(d.coverage)} | ${num(d.meanTriggerPriceImprovementBpOnConfirmedSubset)} | ${d.medianWaitSeconds ?? '—'} |`);
}
report.push('', '价格改善=同一事件触价价格/确认价格−1，正值表示更便宜。只计算已确认样本，包含未被组合实际买入的机会，不是交易收益。全部事件以及忙碌/冷却/风控等过滤计数保留。', '',
  '## 判断与边界', '',
  `秒级确认主场景净收益${pct(second.metrics.netReturn)}、最大回撤${pct(second.metrics.maxDrawdown)}、自然平仓${second.metrics.naturalTrades}笔。成本与延迟敏感性见各场景。不能凭更少交易、更低回撤或更便宜的触发价判定盈利优势。`, '',
  `秒级确认全部交易的账户损益加回实际平仓费后为${pct((second.metrics.finalFish - policy.startingFish + second.metrics.feesFish) / policy.startingFish)}，因此本版损失不能全部归因于手续费。`, '',
  '无独立样本外和现场执行证据，始终liveQualified=false。1秒OHLC缺少秒内先后顺序，网站REST取价不必等于秒收盘；未模拟拒单、断线、会话到期和限频。延迟可能使计划风险超标，汇总保留fillRiskExceedances。', '',
  '继续研究应先实测平台延迟和成交偏差，再冻结下一版规则并观察新数据；不能把已查看年份重命名为封存样本。', '',
  '![净值对照](equity.svg)', '', '## 复现', '', '```sh',
  'node --test research/support-seconds-v1/strategy.test.mjs', 'node research/support-seconds-v1/run.mjs', '```', '',
  '每次创建新时间戳目录。SUPPORT_SECONDS_DATA和SUPPORT_BARS_DATA可指定同格式输入。results.json为汇总；frozen-policy.json为冻结参数；events.jsonl为全部机会；各-trades.json和-curve.json保存逐笔与曲线；data-audit.json保存数据检查；tests.txt保存本次测试输出。', '');
fs.writeFileSync(path.join(output, '报告.md'), report.join('\n'));

const colors = ['#dc6951', '#36a6a0', '#7462c4', '#8e9ca9'];
const lines = [...main.map(t => ({ name: labels[t.method], curve: t.curve })), { name: '20% BTC 持有', curve: benchmarks[0].curve }];
const left = 78, top = 60, plotW = 980, plotH = 400;
const minY = Math.min(policy.startingFish, ...lines.flatMap(l => l.curve.map(p => p.equity))) - .15;
const maxY = Math.max(policy.startingFish, ...lines.flatMap(l => l.curve.map(p => p.equity))) + .15;
const x = t => left + (t - start - policy.warmupSeconds) / (end - start - policy.warmupSeconds) * plotW;
const y = e => top + (maxY - e) / (maxY - minY) * plotH;
let svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="560" viewBox="0 0 1100 560"><rect width="100%" height="100%" fill="#fafaf7"/><g font-family="Arial, sans-serif" fill="#24313b"><text x="78" y="30" font-size="21">支撑回踩入场对照｜BTC · 1倍 · 50条鱼干</text><text x="78" y="51" font-size="12">2025-10-02—2026-09-30 UTC · 买卖均延迟2秒 · 平仓费2bp · 无额外偏差 · 探索性研究</text>';
for (let j = 0; j <= 5; j++) { const e = minY + (maxY - minY) * j / 5;
  svg += `<line x1="${left}" x2="${left + plotW}" y1="${y(e)}" y2="${y(e)}" stroke="#dce1e4"/><text x="${left - 12}" y="${y(e) + 4}" text-anchor="end" font-size="12">${e.toFixed(2)}</text>`;
}
for (const date of ['2025-10-02', '2026-01-01', '2026-04-01', '2026-07-01', '2026-10-01']) {
  svg += `<text x="${x(Date.parse(date + 'T00:00:00Z') / 1000)}" y="485" text-anchor="middle" font-size="12">${date.slice(0, 7)}</text>`;
}
lines.forEach((l, j) => {
  const pts = l.curve.filter((p, i) => i % 6 === 0 || i === l.curve.length - 1).map(p => `${x(p.time).toFixed(2)},${y(p.equity).toFixed(2)}`).join(' ');
  svg += `<polyline points="${pts}" stroke="${colors[j]}" stroke-width="2" fill="none"/><line x1="${left + j * 240}" x2="${left + j * 240 + 22}" y1="510" y2="510" stroke="${colors[j]}" stroke-width="3"/><text x="${left + j * 240 + 30}" y="514" font-size="14">${l.name}</text>`;
});
svg += '<text x="78" y="545" font-size="12" fill="#65737c">曲线按小时记录；回撤使用完整秒收盘路径。历史表现不构成现场盈利证据。</text></g></svg>';
fs.writeFileSync(path.join(output, 'equity.svg'), svg);
console.log(JSON.stringify({ phase: 'completed', output, report: path.join(output, '报告.md') }));
