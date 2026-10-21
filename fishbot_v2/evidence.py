"""V2-only drill and forward-test evidence. It never authorizes live orders."""
import calendar,hashlib,json,time,unittest
from datetime import datetime,timezone
from pathlib import Path
from fishbot.storage import Store,instance_lock
from .model import fingerprint,atomic_json,DAY
from .client import reconcile_shadow
from .portfolio import Portfolio
from .research import metrics

def tests_fingerprint():
    d=hashlib.sha256()
    for p in sorted((Path(__file__).parent.parent/'tests_v2').glob('test*.py')):
        d.update(p.name.encode());d.update(p.read_bytes())
    return d.hexdigest()

def drills(output):
    suite=unittest.defaultTestLoader.discover(str(Path(__file__).parent.parent/'tests_v2'))
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    value=dict(created_at=time.time(),code_sha256=fingerprint(),tests_sha256=tests_fingerprint(),passed=result.wasSuccessful(),count=result.testsRun)
    atomic_json(output,value)
    if not result.wasSuccessful():raise ValueError('V2 drills failed')
    return value

def six_months_after(stamp):
    dt=datetime.fromtimestamp(stamp,timezone.utc);y,m=divmod(dt.year*12+dt.month-1+6,12);m+=1
    return dt.replace(year=y,month=m,day=min(dt.day,calendar.monthrange(y,m)[1])).timestamp()

def export(database,output):
    with instance_lock(database):
        store=Store(database)
        try:
            from .model import Policy
            p=Policy(**store.get('policy'));engine=Portfolio(store,p)
            if p.slippage!=.001:raise ValueError('export the primary 0.10% forward portfolio')
            if store.get('code_sha256')!=fingerprint():raise ValueError('forward evidence code changed')
            reconcile_shadow(store,engine);s=engine.state;frozen=store.get('frozen_at')
            if not frozen:raise ValueError('missing frozen version timestamp')
            trades=[json.loads(row[0]) for row in store.db.execute("SELECT data FROM events WHERE kind='sell_filled' ORDER BY id")]
            curve=[];peak=500000;maxdd=0;last_hour=-1
            for at,units,exposed in store.db.execute('SELECT at,units,exposed FROM equity ORDER BY at'):
                if at<frozen*1000:raise ValueError('forward equity predates frozen policy')
                peak=max(peak,units);maxdd=max(maxdd,1-units/peak)
                if at//3600000!=last_hour:curve.append((at,units,int(exposed),0));last_hour=at//3600000
                else:curve[-1]=(at,units,int(exposed),0)
            if not curve:raise ValueError('no forward observations')
            m=metrics(curve,trades);m['max_drawdown']=max(m['max_drawdown'],maxdd)
            natural=[t for t in trades if t['reason'] not in ('evaluation_end','shutdown')]
            # Independent zero-bias forward portfolio, with its own fills and exit path.
            import sqlite3
            zero_metrics=None;zero_error='zero-bias forward portfolio missing'
            zero_path=Path(str(database)+'.zero')
            if zero_path.exists():
                other=sqlite3.connect(zero_path.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    other.execute('BEGIN')
                    kv={k:json.loads(v) for k,v in other.execute('SELECT key,value FROM kv')}
                    policy=dict(kv.get('policy',{}));policy['slippage']=p.slippage
                    if kv.get('code_sha256')!=fingerprint() or kv.get('frozen_at')!=frozen or policy!=p.dict():raise ValueError('zero forward policy mismatch')
                    zs=kv['state'];zt=[json.loads(r[0]) for r in other.execute("SELECT data FROM events WHERE kind='sell_filled' ORDER BY id")]
                    zcurve=[];zp=500000;zdd=0;hour=-1
                    for at,units,exposed in other.execute('SELECT at,units,exposed FROM equity ORDER BY at'):
                        if at<frozen*1000:raise ValueError('zero forward predates freeze')
                        zp=max(zp,units);zdd=max(zdd,1-units/zp)
                        if at//3600000!=hour:zcurve.append((at,units,int(exposed),0));hour=at//3600000
                        else:zcurve[-1]=(at,units,int(exposed),0)
                    if not zcurve:raise ValueError('zero forward empty')
                    zero_metrics=metrics(zcurve,zt);zero_metrics['max_drawdown']=max(zero_metrics['max_drawdown'],zdd)
                    zero_metrics['natural_trades']=sum(t['reason'] not in ('evaluation_end','shutdown') for t in zt)
                    zero_metrics['halt']=zs['halt']
                    zero_metrics['six_months_observed']=zcurve[-1][0]/1000>=six_months_after(zcurve[0][0]/1000)
                    zero_error=''
                except (ValueError,KeyError,sqlite3.Error) as exc:zero_error=type(exc).__name__
                finally:other.close()
            six_months_observed=curve[-1][0]/1000>=six_months_after(curve[0][0]/1000)
            passed=(six_months_observed and time.time()>=six_months_after(frozen) and m['net_return']>0 and m['max_drawdown']<=.05 and len(natural)>=30 and not s['halt']
                    and zero_metrics is not None and zero_metrics['net_return']>0 and zero_metrics['max_drawdown']<=.05 and zero_metrics['natural_trades']>=30 and zero_metrics['six_months_observed'] and not zero_metrics['halt'])
            value=dict(created_at=time.time(),code_sha256=fingerprint(),frozen_at=frozen,
                       earliest_profit_review=six_months_after(curve[0][0]/1000),six_months_observed=six_months_observed,continuous_ms=s['shadow_continuous_ms'],
                       engineering_passed=s['shadow_continuous_ms']>=14*DAY and not s['halt'],reconciled=True,
                       stress_metrics=m,zero_metrics=zero_metrics,zero_evidence_error=zero_error,natural_trades=len(natural),
                       preliminary_profit_gate=passed,live_qualified=False,
                       limitation='Forward portfolios must span at least six calendar months after freeze; evidence never enables live execution')
            atomic_json(output,value);return value
        finally:store.close()
