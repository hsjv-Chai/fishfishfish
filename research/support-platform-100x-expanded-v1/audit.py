"""Independent offline verification of exported fills, liquidation quotes and cash."""
import hashlib
import json
import math
import mmap
from pathlib import Path
import struct
import sys
from datetime import datetime, timedelta

report = Path(sys.argv[1]).resolve()
results = json.loads((report / 'results.json').read_text())
source_dir = Path(__file__).resolve().parent
for name, digest in results['sourceHashes'].items():
    assert hashlib.sha256((source_dir / name).read_bytes()).hexdigest() == digest
source = Path('/Users/hsjv/Documents/lianghua/data/btc_1s')
handles = {key: (source / (key + '.f32')).open('rb') for key in ('close', 'high', 'low')}
arrays = {key: mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) for key, handle in handles.items()}
epoch = datetime.fromisoformat('2025-10-01T00:00:00+00:00')

def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def price(key, offset):
    return struct.unpack_from('<f', arrays[key], offset * 4)[0]

def payout(stake, entry, exit):
    ratio = exit / entry
    return max(0, math.floor(stake + stake * 100 * (ratio - 1) - stake * 100 * ratio * .0002))

checks = []
work = []
for t in results['trials']:
    suffix = '--conservative' if t['liquidationMode'] == 'each_second_low' else '--phase' + str(t['phase'])
    work.append(('annual', t, results['policy']['dataStart'], t['scenario'] + suffix + '-trades.json'))
for month in results['monthlyIndependent']:
    warmup = max(epoch, instant(month['evaluationStart']) - timedelta(days=1))
    for t in month['trials']:
        work.append((month['month'], t, warmup.isoformat(), month['month'] + '--' + t['scenario'] + '--' + t['liquidationMode'] + '-trades.json'))
scenarios = {s['name']: s for s in results['policy']['scenarios']}
for period, trial, data_start, file in work:
    offset = int((instant(data_start) - epoch).total_seconds())
    scenario = scenarios[trial['scenario']]
    slip = scenario['slippagePerSide']
    trades = json.loads((report / file).read_text())
    cash = 500000
    exhausted_at = None
    for t in trades:
        i, j = offset + t['entryIndex'], offset + t['exitIndex']
        assert t['entry'] == price('close', i) * (1 + slip)
        assert t['exitObservedPrice'] == price('close', j)
        assert cash >= 10000 and t['stakeUnits'] == 10000
        if t['reason'] == 'liquidated':
            observed = price('low' if trial['liquidationMode'] == 'each_second_low' else 'close', j)
            assert observed <= t['entry'] * .99 and t['payoutUnits'] == 0
            if trial['liquidationMode'] == 'grid_close':
                assert int((epoch + timedelta(seconds=j+1)).timestamp()) % 15 == trial['phase']
        else:
            assert t['exitPrice'] == price('close', j) * (1-slip)
            assert t['payoutUnits'] == payout(10000, t['entry'], t['exitPrice'])
            if t['reason'] == 'profit_target':
                trigger = offset + t['exitTriggerIndex']
                assert payout(10000, t['entry'], price('close', trigger)) >= 11000
        cash += t['payoutUnits'] - 10000
        if cash < 10000:
            exhausted_at = (epoch + timedelta(seconds=j+1)).isoformat()
    assert cash / 10000 == trial['metrics']['finalFish']
    checks.append({'period': period, 'scenario': trial['scenario'], 'phase': trial['phase'],
                   'mode': trial['liquidationMode'], 'trades': len(trades), 'finalFish': cash/10000,
                   'belowMinimumStakeAtUTC': exhausted_at, 'status': 'passed'})
previous = report.parents[1] / 'support-platform-100x-v1' / '20261002T152954Z' / 'results.json'
old = json.loads(previous.read_text())
for t in results['monthlyIndependent'][-1]['trials']:
    original = next(x for x in old['trials'] if x['scenario']==t['scenario'] and x['phase']==0 and x['liquidationMode']==t['liquidationMode'])
    assert t['metrics'] == original['metrics']
output = {'status': 'passed', 'verifiedTrialCount': len(checks), 'verifiedTrades': sum(c['trades'] for c in checks),
          'septemberMatchesPreviousEightTrialsExactly': True,
          'auditScriptSHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'checks': checks}
(report / 'independent-audit.json').write_text(json.dumps(output, indent=2)+'\n')
print(json.dumps({key: value for key,value in output.items() if key != 'checks'}))
for array in arrays.values(): array.close()
for handle in handles.values(): handle.close()
