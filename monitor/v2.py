"""Read-only V2 monitor data; no strategy imports or order controls."""
import hashlib,json,sqlite3
from pathlib import Path

def read_json(p):
    try:
        v=json.loads(Path(p).read_text());return v if isinstance(v,dict) else {}
    except (OSError,ValueError):return {}

def code_fingerprint(root):
    d=hashlib.sha256()
    for name in ('fishbot_v2','fishbot'):
        directory=Path(root)/name
        if not directory.exists():return None
        for p in sorted(directory.glob('*.py')):
            d.update((name+'/'+p.name).encode());d.update(p.read_bytes())
    return d.hexdigest()

def read_db(path,now):
    result=dict(running=False,status='not_started',stats=None,positions=[],orders=[],filters={},heartbeat_at=None,progress=None)
    if not Path(path).exists():return result
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        db.execute('BEGIN');kv={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM kv')};s=kv.get('state',{});h=kv.get('heartbeat',{})
        latest=db.execute('SELECT at,units FROM equity ORDER BY at DESC LIMIT 1').fetchone()
        fresh=-5000<=now-h.get('at',0)<=60000;healthy=bool(fresh and h.get('healthy') and not s.get('halt'))
        eq=latest[1] if latest else None;high=s.get('high_water',500000)
        attribution={'symbol':{},'mechanism':{}}
        for row in db.execute("SELECT data FROM events WHERE kind='sell_filled'"):
            trade=json.loads(row[0])
            for field in attribution:
                key=trade[field];item=attribution[field].setdefault(key,dict(trades=0,profit=0))
                item['trades']+=1;item['profit']+=trade['profit']/10000
        positions=[{k:p.get(k) for k in ('id','symbol','mechanism','stake','entry','stop','opened','risk_units')} for p in s.get('positions',{}).values()]
        total_stake=sum(p['stake'] for p in positions);risk=sum(p['risk_units'] for p in positions)
        result.update(running=healthy,status='healthy' if healthy else 'stale' if not fresh else 'degraded',heartbeat_at=h.get('at'),halt=s.get('halt'),
                      stats=dict(equity=eq/10000 if eq is not None else None,cash=s.get('cash',0)/10000,
                                 net_return=eq/500000-1 if eq is not None else None,drawdown=max(0,1-eq/high) if eq is not None and high else None,
                                 allocation=total_stake/eq if eq else None,reserved_risk=risk/eq if eq else None,
                                 completed_trades=db.execute("SELECT count(*) FROM events WHERE kind='sell_filled'").fetchone()[0]),
                      attribution=attribution,policy=kv.get('policy'),frozen_at=kv.get('frozen_at'),positions=positions,
                      orders=[{k:o.get(k) for k in ('kind','symbol','created','attempts','uncertain')} for o in s.get('orders',{}).values()],filters=s.get('filters',{}),
                      progress=dict(elapsed_ms=s.get('shadow_continuous_ms',0),started=s.get('shadow_started')))
    finally:db.close()
    return result

def snapshot(data_dir,now,root):
    evidence=Path(data_dir)/'v2/evidence';research=read_json(evidence/'research.json');drills=read_json(evidence/'drills.json');audit=read_json(evidence/'interface.json');forward=read_json(evidence/'forward.json')
    expected=code_fingerprint(root);matched=bool(expected and research.get('code_sha256')==expected)
    test_digest=hashlib.sha256();test_files=sorted((Path(root)/'tests_v2').glob('test*.py'))
    for file in test_files:test_digest.update(file.name.encode());test_digest.update(file.read_bytes())
    tests_match=bool(test_files and drills.get('tests_sha256')==test_digest.hexdigest())
    db=Path(data_dir)/'v2/shadow.sqlite3'
    try:live=read_db(db,now);zero=read_db(str(db)+'.zero',now)
    except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):live=dict(running=False,status='unavailable',stats=None,positions=[],orders=[],filters={},progress=None);zero=dict(running=False,status='unavailable',stats=None)
    candidates=[]
    for name,v in research.get('candidates',{}).items():
        fields=('net_return','max_drawdown','trades','unique_signals','average_profit','max_positions','filters','attribution','exposure','average_allocation','daily_sharpe','profit_factor','win_rate','coheld_fraction')
        summary={level:{k:m[k] for k in fields if k in m} for level,m in v.get('stress',{}).items()}
        candidates.append(dict(name=name,passed=bool(v.get('passed') and matched),stress=summary))
    candidates.sort(key=lambda x:x['stress'].get('0.001',{}).get('average_profit',-1e9),reverse=True)
    return dict(status=research.get('status','research_not_available'),code_matches=matched,candidates=candidates,
                selected=research.get('selected'),frozen_at=research.get('frozen_at'),folds=research.get('folds',[]),
                correlation=research.get('price_return_correlation'),candidate_count=research.get('candidate_count'),
                drills=dict(passed=bool(expected and drills.get('code_sha256')==expected and tests_match and drills.get('passed')),count=drills.get('count')),
                audit=dict(available=bool(audit),source_supports_multis=bool(audit.get('source_allows_independent_positions')),
                           online_limit_verified=bool(audit.get('online_multi_position_limit_verified')),symbols=audit.get('symbols',{})),
                forward=forward,shadow=live,zero=zero,live_enabled=False)
