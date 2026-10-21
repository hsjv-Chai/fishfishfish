// ─────────────────────────────────────────────────────────────────────────────
// 鱼干练手盘 · 多周期对照回测（修正版）
//   修正：上一版用 1s 数据自建 K 线，导致预热不足 → 各变体测试窗口不同、结果不可比。
//   本版：指标用币安 K 线（含 2025-03 起预热），价格路径用 1 秒数据 —— 全部变体覆盖
//         完全相同的窗口 2025-10-01 → 2026-09-30，差别只有"信号周期"这一个变量。
// ─────────────────────────────────────────────────────────────────────────────
import { readFileSync, writeFileSync } from 'node:fs';

const DATA = '/Users/hsjv/Documents/lianghua/data/btc_1s';
const BT = '/Users/hsjv/Documents/lianghua/backtest';
const FEE = 0.0002, SCALE = 10000, FISH = (u) => u / SCALE, SEC_PER_DAY = 86400;
const T0 = Date.UTC(2025, 9, 1) / 1000;      // 1s 数据的起点（秒）

const settleClose = (u, e, x, L) => {
  const r = x / e, no = u * L, raw = u + no * (r - 1);
  return Math.max(0, Math.floor(raw - no * r * FEE));
};
const liqPrice = (e, L) => (L > 1 ? e * (1 - 1 / L) : 0);
function koRate(x) {
  if (x >= 1.5) return Math.min(1, 0.279 * Math.pow(x, -2.46));
  const a = Math.log(0.348 / 0.584) / Math.log(0.847 / 0.424);
  return Math.min(0.95, 0.584 * Math.pow(x / 0.424, a));
}
const TIERS = [1, 2, 3, 5, 10, 20];
const lowerL = (L) => { const i = TIERS.indexOf(L); return i > 0 ? TIERS[i - 1] : 1; };
function selectLeverage(sigma, H, eps, stakePct, acctRisk = 0.01) {
  let best = 1;
  for (const L of [2, 3, 5, 10, 20]) if (1 - Math.pow(1 - koRate((1 / L) / sigma), H) <= eps) best = L;
  while (best > 1 && best * sigma * stakePct > acctRisk) best = lowerL(best);
  return best;
}

// ── 数据 ─────────────────────────────────────────────────────────────────────
const rd = (f) => { const b = readFileSync(`${DATA}/${f}`); return new Float32Array(b.buffer, b.byteOffset, b.length / 4); };
const sClose = rd('close.f32'), sHigh = rd('high.f32'), sLow = rd('low.f32');
const N = sClose.length;

// 日线（用于 σ̂ / ATR / H 的"天"口径，始终不变）
const daily = JSON.parse(readFileSync(`${BT}/data/btc_1d.json`, 'utf8'));
const DC = daily.map(d => d[4]), DH = daily.map(d => d[2]), DL = daily.map(d => d[3]);
const dayIndex = new Map(daily.map((d, i) => [new Date(d[0]).toISOString().slice(0, 10), i]));
function dayStats(dIdx) {                      // 用 dIdx 之前（不含）的完整日线
  const i = dIdx - 1;
  const r = []; for (let k = 1; k <= i; k++) r.push(Math.log(DC[k] / DC[k - 1]));
  let v = r[0] * r[0]; for (let k = 1; k < r.length; k++) v = 0.94 * v + 0.06 * r[k] * r[k];
  const rv90 = Math.sqrt(r.slice(-90).reduce((s, x) => s + x * x, 0) / Math.min(90, r.length));
  let a = 0; for (let k = i - 13; k <= i; k++) a += Math.max(DH[k] - DL[k], Math.abs(DH[k] - DC[k - 1]), Math.abs(DL[k] - DC[k - 1]));
  return { sigma: Math.max(Math.sqrt(v), 0.6 * rv90, 0.015), atr: a / 14 };
}

// ── K 线序列（币安原始，含预热）──────────────────────────────────────────────
function loadBars(file) {
  const k = JSON.parse(readFileSync(file, 'utf8'));
  const H = k.map(x => x[2]), L = k.map(x => x[3]), C = k.map(x => x[4]);
  const byTime = new Map(k.map((x, i) => [x[0], i]));
  const n = k.length, d = new Array(n);
  const emaVal = (arr, nn, upto) => { const kk = 2 / (nn + 1); let e = arr.slice(0, nn).reduce((s, x) => s + x, 0) / nn; for (let i = nn; i <= upto; i++) e = arr[i] * kk + e * (1 - kk); return e; };
  for (let b = 0; b < n; b++) {
    const mx = (arr, kk) => { let m = -Infinity; for (let j = b - kk; j < b; j++) m = Math.max(m, arr[j]); return m; };
    const mn = (arr, kk) => { let m = Infinity; for (let j = b - kk; j < b; j++) m = Math.min(m, arr[j]); return m; };
    d[b] = { H: H[b], L: L[b], C: C[b],
      dh20: b >= 20 ? mx(H, 20) : Infinity, dh50: b >= 50 ? mx(H, 50) : Infinity,
      dh100: b >= 100 ? mx(H, 100) : Infinity, dl10: b >= 10 ? mn(L, 10) : -Infinity,
      e20: b >= 20 ? emaVal(C, 20, b - 1) : null, e100: b >= 100 ? emaVal(C, 100, b - 1) : null };
  }
  return { bars: d, byTime };
}

// ── 逐秒模拟 ─────────────────────────────────────────────────────────────────
function simulate(barSeries, tfSec, { startFish = 1000, stakePerTrade = 100, eps = 0.05, H = 14, acctRisk = 0.01, stakeFraction = null, dstClock = false } = {}) {
  const { bars, byTime } = barSeries;
  let cash = startFish, pos = null, lastKOday = -999, needFresh = false;
  let peakEq = startFish, mdd = 0, feeTotal = 0, koCount = 0, unfilled = 0;
  const trades = []; let stakeUnits = Math.round(stakePerTrade * SCALE);
  const k20 = 2 / 21, k100 = 2 / 101;
  const dStats = daily.map((_, i) => i > 0 ? dayStats(i) : null);

  const dayInfo = new Map();
  const ny = new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', hour: 'numeric', hourCycle:'h23'});
  for (let j=0; j<365; j++) {
    const t = T0+j*SEC_PER_DAY;
    const idx = dayIndex.get(new Date(t*1000).toISOString().slice(0,10));
    const nyAtNoonUTC = Number(ny.format(new Date((t+12*3600)*1000)));
    dayInfo.set(Math.floor(t/SEC_PER_DAY), {idx, shift: dstClock ? 8-nyAtNoonUTC : 0});
  }
  for (let i = 0; i < N; i++) {
    const tsSec = T0 + i;                                  // 该秒的 UTC 秒
    const barOpen = Math.floor(tsSec / tfSec) * tfSec * 1000;
    const bi = byTime.get(barOpen);
    if (bi === undefined) { unfilled++; continue; }
    const bar = bars[bi];
    const px = sClose[i];
    const info=dayInfo.get(Math.floor(tsSec/SEC_PER_DAY));
    const hourUTC = (tsSec % SEC_PER_DAY) / 3600 - info.shift;
    const dIdx = info.idx;
    const ds = dStats[dIdx];

    if (pos) {
      pos.hwm = Math.max(pos.hwm, px);
      if (pos.lev > 1 && px <= pos.liq) {
        trades.push({ ...pos, exitIdx: i, exitPrice: pos.liq, payout: 0, reason: 'liquidated', ret: -1 });
        pos = null; lastKOday = dIdx; needFresh = true; koCount++;
      } else {
        const holdDays = (i - pos.entryIdx) / SEC_PER_DAY;
        const dd = (pos.entry - px) / (pos.entry / pos.lev);
        const trail = Math.max(2.5 * pos.atr / px, 0.8 / pos.lev);
        let reason = null;
        if (holdDays <= 3 && dd >= 0.5) reason = 'soft';
        else if (px <= pos.hwm * (1 - trail)) reason = 'trail';
        else if (px < bar.dl10) reason = 'signal';
        else if (pos.lev >= 10 && hourUTC >= 13 && hourUTC < 13.5) reason = 'L3-derisk';
        if (reason) {
          const p = settleClose(stakeUnits, pos.entry, px, pos.lev);
          const gross = Math.max(0, stakeUnits + stakeUnits * pos.lev * (px / pos.entry - 1));
          feeTotal += FISH(gross - p); cash += FISH(p);
          trades.push({ ...pos, exitIdx: i, exitPrice: px, payout: FISH(p), reason, ret: FISH(p) / FISH(stakeUnits) - 1 });
          pos = null;
        }
      }
    }

    if (!pos && cash >= stakePerTrade && (dIdx - lastKOday > 3) && bar.e20 !== null && ds) {
      const gate = (bar.e20 + k20 * (px - bar.e20)) > (bar.e100 + k100 * (px - bar.e100));
      const hits = (px > bar.dh20) + (px > bar.dh50) + (px > bar.dh100);
      const entrySig = hits >= 2 && gate;
      if (!needFresh && entrySig) {
        const blocked = (hourUTC >= 13 && hourUTC < 14.5) || (hourUTC >= 19.5 && hourUTC < 20);
        if (!blocked) {
          if (stakeFraction !== null) {
            stakeUnits = Math.floor(cash * stakeFraction * SCALE);
            stakePerTrade = FISH(stakeUnits);
          }
          const lev = selectLeverage(ds.sigma, H, eps, stakePerTrade / cash, acctRisk);
          if (lev >= 2) {
            cash -= stakePerTrade;
            pos = { entryIdx: i, entry: px, lev, liq: liqPrice(px, lev), hwm: px, sigma: ds.sigma, atr: ds.atr };
          }
        }
      } else if (needFresh && !entrySig) needFresh = false;
    }

    const eq = pos ? cash + FISH(settleClose(stakeUnits, pos.entry, px, pos.lev)) : cash;
    if (eq > peakEq) peakEq = eq;
    const ddNow = eq / peakEq - 1; if (ddNow < mdd) mdd = ddNow;
  }
  if (pos) {
    const px = sClose[N - 1], p = settleClose(stakeUnits, pos.entry, px, pos.lev);
    cash += FISH(p);
    trades.push({ ...pos, exitIdx: N - 1, exitPrice: px, payout: FISH(p), reason: 'end', ret: FISH(p) / FISH(stakeUnits) - 1 });
  }
  const wins = trades.filter(t => t.ret > 0).length;
  return { trades, final: cash, ret: cash / startFish - 1, mdd, koCount, feeTotal, wins,
           exp: trades.length ? trades.reduce((s, t) => s + t.ret, 0) / trades.length : 0, unfilled };
}


const auditDir = '/Users/hsjv/Documents/ChatGPT/fishfishfish/strategy-v3-review';
const cases = [
  ['original_4h', 'btc_4h_klines.json', 14400, {}],
  ['stake_5pct_4h', 'btc_4h_klines.json', 14400, {stakeFraction:0.05}],
  ['dst_4h', 'btc_4h_klines.json', 14400, {dstClock:true}],
  ['stake_5pct_dst_4h', 'btc_4h_klines.json', 14400, {stakeFraction:0.05,dstClock:true}],
  ['original_1h', 'btc_1h_klines.json', 3600, {}],
];
const outputs=[];
for (const [name,file,tf,cfg] of cases) {
  const r=simulate(loadBars(`${BT}/data/${file}`),tf,cfg);
  const rets=r.trades.map(t=>t.ret), mean=rets.reduce((a,b)=>a+b,0)/rets.length;
  const sd=Math.sqrt(rets.reduce((a,b)=>a+(b-mean)**2,0)/(rets.length-1));
  const out={name,final:r.final,return:r.ret,mdd:r.mdd,trades:r.trades.length,ko:r.koCount,fees:r.feeTotal,unfilled:r.unfilled,
    levCounts:r.trades.reduce((a,t)=>(a[t.lev]=(a[t.lev]||0)+1,a),{}),mean,sd,t:mean/(sd/Math.sqrt(rets.length)),
    avgHoldDays:r.trades.reduce((a,t)=>a+(t.exitIdx-t.entryIdx)/86400,0)/rets.length,
    reasons:r.trades.reduce((a,t)=>(a[t.reason]=(a[t.reason]||0)+1,a),{}),
    detail:r.trades.map(t=>({...t,entryTime:new Date((T0+t.entryIdx)*1000).toISOString(),exitTime:new Date((T0+t.exitIdx)*1000).toISOString()}))};
  outputs.push(out);
  console.log(JSON.stringify({...out,detail:undefined}));
  writeFileSync(`${auditDir}/reproduction.json`,JSON.stringify(outputs,null,2));
}
