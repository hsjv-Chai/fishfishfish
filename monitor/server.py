"""Read-only observability sidecar. No trading imports, secrets or mutations."""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from urllib.parse import parse_qs, urlsplit

DAY = 86_400_000
RANGES = {'1h': 3_600_000, '24h': DAY, '7d': 7 * DAY, 'all': None}
ROOT = Path(__file__).resolve().parent


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def report(path):
    try:
        result = json.loads(Path(path).read_text())
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def evidence_gates(evidence, code_hash, tests_hash, state, healthy, now):
    records = {name: report(Path(evidence) / (name + '.json'))
               for name in ('backtest', 'drills', 'shadow', 'canary', 'email')}
    def valid(name):
        r = records[name]
        return bool(r and code_hash and r.get('code_sha256') == code_hash)
    def gate(label, status, detail):
        return dict(label=label, status=status, detail=detail)
    bt = records['backtest']
    gates = []
    if not valid('backtest'):
        gates.append(gate('样本外回测', 'unknown', '报告缺失或与运行代码不一致'))
    else:
        reasons = []
        for key, label in [('0', '基础'), ('0.001', '0.10% 偏差')]:
            m = bt.get('partitions', {}).get('test', {}).get(key, {})
            if m.get('net_return', -1) <= 0: reasons.append(label + '净收益未为正')
            if m.get('max_drawdown', 1) > .05: reasons.append(label + '回撤超过 5%')
            if m.get('trades', 0) < 30: reasons.append(f"{label}交易数 {m.get('trades', 0)}／30")
        gates.append(gate('样本外回测', 'blocked' if reasons else 'passed', '；'.join(reasons) or '收益、回撤与样本数全部通过'))
    drills = records['drills'] or {}
    drill_ok = valid('drills') and drills.get('passed') and drills.get('tests_sha256') == tests_hash
    gates.append(gate('工程故障测试', 'passed' if drill_ok else 'unknown',
                      f"{drills.get('count', 0)} 项通过 · 代码与测试版本一致" if drill_ok else '测试证据缺失、失败或版本变化'))
    elapsed = state.get('shadow_continuous_ms', 0)
    s = records['shadow'] or {}
    shadow_ok = (valid('shadow') and s.get('continuous_ms', 0) >= 14 * DAY and not s.get('halt')
                 and s.get('reconciled') and 0 <= now / 1000 - s.get('created_at', 0) <= 86400)
    gates.append(gate('14 天影子验收', 'passed' if shadow_ok else 'pending',
                      '已完成人工导出与对账验收' if shadow_ok else
                      f"当前累计 {elapsed / DAY:.3f} 天；达标后仍需导出验收，不会自动放行"))
    ca = records['canary'] or {}
    canary_ok = valid('canary') and ca.get('passed') and ca.get('stake_units') == 10000
    gates.append(gate('1 条鱼干开平仓验证', 'passed' if canary_ok else 'pending',
                      '回执与账目验证完成' if canary_ok else '前置条件通过后才能执行'))
    mail = records['email'] or {}
    mail_ok = valid('email') and mail.get('sent') and 0 <= now / 1000 - mail.get('created_at', 0) <= 86400
    gates.append(gate('上线前邮件测试', 'passed' if mail_ok else 'pending',
                      '最近 24 小时内已提交测试邮件' if mail_ok else '需要当前代码下 24 小时内的邮件测试；并非断言邮件故障'))
    return gates, records


def test_fingerprint(directory):
    digest = hashlib.sha256()
    files = sorted(Path(directory).glob('test*.py'))
    if not files: return None
    for file in files:
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def alerts_from(db, source):
    rows = db.execute('SELECT key,active,sent,last_sent,message FROM alerts ORDER BY active DESC,last_sent DESC LIMIT 30').fetchall()
    return [dict(r, source=source) for r in rows]


def snapshot(database, evidence, tests, window='24h', now=None):
    if window not in RANGES: raise ValueError('unsupported range')
    now = int(time.time() * 1000) if now is None else now
    db = readonly(database)
    try:
        # Pin a WAL read snapshot so state and equity queries see one coherent commit boundary.
        db.execute('BEGIN')
        kv = {r['key']: json.loads(r['value']) for r in db.execute('SELECT key,value FROM kv')}
        s, h = kv.get('state', {}), kv.get('heartbeat', {})
        latest = db.execute('SELECT at,units FROM equity ORDER BY at DESC LIMIT 1').fetchone()
        age = now - h['at'] if h.get('at') else None
        fresh = age is not None and -5000 <= age <= 60000
        healthy = bool(fresh and h.get('healthy') and not s.get('halt'))
        status = 'healthy' if healthy else ('stale' if not fresh else 'degraded')
        start = now - RANGES[window] if RANGES[window] is not None else 0
        count, first = db.execute('SELECT count(*),min(at) FROM equity WHERE at>=?', (start,)).fetchone()
        # At most ~720 buckets, retaining the final sample in each bucket. No invented interpolation.
        width = max(5000, ((now - (first or now)) // 720) + 1)
        rows = db.execute('''SELECT e.at,e.units,e.exposed FROM equity e JOIN
            (SELECT max(at) AS t FROM equity WHERE at>=? GROUP BY cast(at/? AS INTEGER)) b
            ON e.at=b.t ORDER BY e.at''', (start, width)).fetchall()
        curve = [dict(at=r['at'], equity=r['units'] / 10000, exposed=bool(r['exposed'])) for r in rows]
        fills = db.execute("SELECT kind,count(*) AS n FROM events WHERE kind IN ('buy_filled','sell_filled') GROUP BY kind").fetchall()
        total_fills = {r['kind']: r['n'] for r in fills}
        events = []
        allowed = {'id', 'price', 'stake', 'payout', 'profit', 'reason', 'status', 'kind', 'attempt', 'recovered'}
        for r in db.execute('SELECT id,at,kind,data FROM events ORDER BY id DESC LIMIT 100'):
            data = json.loads(r['data'])
            events.append(dict(id=r['id'], at=r['at'], kind=r['kind'],
                               data={k: v for k, v in data.items() if k in allowed}))
        alerts = alerts_from(db, 'trader')
        eq = latest['units'] if latest else None
        high = s.get('high_water', 500000)
        position = s.get('position')
        p = ({k: position.get(k) for k in ('stake', 'entry', 'stop', 'peak', 'opened',
                                            'direction', 'leverage', 'liquidation')} if position else None)
        pending = s.get('pending')
        gates, reports = evidence_gates(evidence, kv.get('code_sha256'), test_fingerprint(tests), s, healthy, now)
        statistics = dict(cash=s.get('cash', 0) / 10000, equity=eq / 10000 if eq is not None else None,
                          net_return=(eq / 500000 - 1) if eq is not None else None,
                          drawdown=max(0, 1 - eq / high) if eq is not None and high else None,
                          daily_return=(eq / s['day_base'] - 1) if eq is not None and s.get('day_base') else None,
                          high_water=high / 10000, buys=total_fills.get('buy_filled', 0),
                          sells=total_fills.get('sell_filled', 0), equity_at=latest['at'] if latest else None)
        response = dict(now=now, mode=kv.get('mode'), status=status, healthy=healthy,
                        heartbeat_at=h.get('at'), heartbeat_age_ms=age,
                        halt=s.get('halt'), day_pause=s.get('day_pause'), stats=statistics,
                        progress=dict(started=s.get('shadow_started'), elapsed_ms=s.get('shadow_continuous_ms', 0),
                                      target_ms=14 * DAY, running=healthy),
                        position=p, pending=({k: pending.get(k) for k in ('kind', 'created', 'attempts', 'next_try')} if pending else None),
                        curve=curve, raw_samples=count, range=window, events=events, alerts=alerts,
                        gates=gates, activation_ready=all(g['status'] == 'passed' for g in gates),
                        backtest=(reports['backtest'] or {}).get('partitions', {}),
                        backtest_matches=bool(reports['backtest'] and reports['backtest'].get('code_sha256') == kv.get('code_sha256')),
                        drills_count=(reports['drills'] or {}).get('count'),
                        email_test_at=(reports['email'] or {}).get('created_at'), watchdog_observed=False)
    finally:
        db.close()
    monitor = Path(str(database) + '.monitor')
    if monitor.exists():
        try:
            other = readonly(monitor)
            try:
                response['alerts'].extend(alerts_from(other, 'watchdog'))
                response['watchdog_observed'] = True
            finally: other.close()
        except sqlite3.Error: pass
    from monitor.v2 import snapshot as v2_snapshot
    response['v2']=v2_snapshot(Path(database).parent,now,Path(tests).parent)
    return response


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def reply(self, status, body, mime='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self): self.reply(405, b'{"error":"read_only"}')
    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/api/snapshot':
            try:
                window = parse_qs(url.query).get('range', ['24h'])[0]
                data = snapshot(self.server.database, self.server.evidence, self.server.tests, window)
                self.reply(200, json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
            except ValueError:
                self.reply(400, b'{"error":"invalid_query_or_data"}')
            except (sqlite3.Error, OSError, KeyError, TypeError):
                self.reply(503, b'{"error":"monitor_data_unavailable"}')
        elif url.path in ('/', '/app.js', '/style.css', '/v2.js'):
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/v2.js': 'v2.js'}[url.path]
            mime = {'/': 'text/html', '/app.js': 'application/javascript', '/style.css': 'text/css', '/v2.js': 'application/javascript'}[url.path]
            self.reply(200, (ROOT / name).read_bytes(), mime + '; charset=utf-8')
        else: self.reply(404, b'{"error":"not_found"}')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--database', default='/app/data/shadow.sqlite3')
    p.add_argument('--evidence', default='/app/data/evidence')
    p.add_argument('--tests', default='/app/tests')
    p.add_argument('--bind', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8787)
    args = p.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.database, server.evidence, server.tests = args.database, args.evidence, args.tests
    server.serve_forever()


if __name__ == '__main__': main()
