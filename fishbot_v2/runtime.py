"""Qualified shadow-only runner. Separate DB and evidence, immutable selected policy."""
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json,logging,signal,time
from fishbot.config import Config
from fishbot.storage import Store,instance_lock
from fishbot.alerts import Alerts
from fishbot.client import APIError,SchemaError
from .client import ReadSite,PublicMarket,ShadowBroker,reconcile_shadow
from .model import Policy,SYMBOLS,HOUR,DAY,fingerprint,atomic_json
from .indicators import make_signal
from .portfolio import Portfolio

def qualification(path):
    r=json.loads(Path(path).read_text())
    if r.get('status')!='qualified_for_shadow' or not r.get('selected') or r.get('code_sha256')!=fingerprint():raise ValueError('no qualified current-code V2 candidate')
    p=Policy(**r['selected']);key=p.family+'-'+p.budget
    m=r['candidates'][key]['stress']['0.001']
    if not (m['net_return']>0 and m['max_drawdown']<=.05 and m['trades']>=30 and sum(w['net_return']>0 for w in m['windows'])>=2 and not any(w['halt'] for w in m['windows'])):raise ValueError('research gates failed')
    return r,p

class Runner:
    def __init__(self,cfg,store,policy):
        self.cfg,self.store=cfg,store;self.site=ReadSite(cfg);self.market=PublicMarket(cfg.market_url)
        self.alerts=Alerts(store,cfg);self.broker=ShadowBroker(store,policy.slippage);self.engine=Portfolio(store,policy,self.broker)
        old=store.get('code_sha256')
        if old and old!=fingerprint():raise ValueError('frozen code changed: no automatic resume')
        store.put('code_sha256',fingerprint());self.signals={};self.last_hour=-1;self.last_prices={};self.stopping=False;self.last_reconcile=0
        reconcile_shadow(store,self.engine)
    def stop(self,*_):self.stopping=True
    def tick(self):
        start=int(time.time()*1000);quotes={};prices=dict(self.last_prices);external={};good=True;auth_error=False
        try:quotes=self.site.quotes(start);self.alerts.set('v2_site_quote','V2 quote recovered',False)
        except Exception as exc:
            good=False;self.alerts.set('v2_site_quote','V2 site quotes unavailable; entries blocked ('+type(exc).__name__+')')
            if isinstance(exc,APIError) and exc.code in (401,403):
                auth_error=True;self.alerts.set('v2_session','V2 session rejected; renew login')
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs={s:pool.submit(self.market.price_for,s) for s in SYMBOLS}
            for sym,job in jobs.items():
                try:prices[sym],stamp=job.result();external[sym]=True
                except Exception:external[sym]=False;good=False
                if not external[sym] and sym in quotes and quotes[sym].fresh(int(time.time()*1000)):prices[sym]=quotes[sym].price
        now=int(time.time()*1000)
        try:expiry=self.site.expiry()
        except Exception:expiry=0;good=False
        if auth_error:expiry=0
        self.alerts.set('v2_session_expiry','V2 session expires within 72h; renew',expiry-now<=3*DAY)
        if now//HOUR!=self.last_hour and now%HOUR>=10000:
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    jobs={(s,i):pool.submit(self.market.candles_for,s,i,now) for s in SYMBOLS for i in ('1h','4h')}
                    self.signals={s:make_signal(jobs[s,'1h'].result(),jobs[s,'4h'].result()) for s in SYMBOLS}
                self.last_hour=now//HOUR;self.alerts.set('v2_candles','V2 candles recovered',False)
            except Exception:good=False;self.signals={};self.alerts.set('v2_candles','V2 closed candle feed unavailable')
        now=int(time.time()*1000)
        fresh_all=all(external.get(s) or (s in quotes and quotes[s].fresh(now)) for s in SYMBOLS) and now-start<=15000
        matched=all(s in quotes and quotes[s].fresh(now) and external.get(s) and abs(prices.get(s,0)/quotes[s].price-1)<=.003 for s in SYMBOLS)
        good=bool(good and fresh_all and matched and len(self.signals)==2 and expiry-now>DAY)
        self.alerts.set('v2_market','V2 upstream/site stale or divergent; entries blocked',not matched)
        self.broker.quotes=quotes
        if len(prices)==2:
            if self.stopping or not fresh_all or expiry-now<=DAY:
                self.engine.shutdown(prices,now,'shutdown' if self.stopping else ('session_expiring' if expiry-now<=DAY else 'market_unavailable'))
            else:
                frozen=self.store.get('frozen_at',0)*1000
                eligible={sym:sig for sym,sig in self.signals.items() if sig.time+HOUR>=frozen}
                self.engine.on_signals(eligible,prices,now,good)
            eq=self.engine.equity(prices);self.store.equity(now,eq,bool(self.engine.state['positions']));self.last_prices=prices
        if now-self.last_reconcile>60000:
            try:reconcile_shadow(self.store,self.engine);self.last_reconcile=now;self.alerts.set('v2_reconcile','V2 shadow ledger recovered',False)
            except Exception:
                self.engine.state['halt']='reconciliation_failure';good=False;self.alerts.set('v2_reconcile','V2 shadow ledger mismatch; halted')
                if len(prices)==2:self.engine.shutdown(prices,now,'reconciliation_failure')
        s=self.engine.state
        if good and not s['halt'] and not s['orders']:
            prior=s['shadow_last_tick']
            if prior is None or now-prior>60000:s['shadow_started']=now;s['shadow_continuous_ms']=0
            else:s['shadow_continuous_ms']+=max(0,now-prior)
            s['shadow_last_tick']=now
        else:s['shadow_last_tick']=None;s['shadow_continuous_ms']=0
        self.engine.save();self.store.put('heartbeat',dict(at=now,mode='shadow',healthy=good,halted=s['halt'],positions=len(s['positions']),pending=len(s['orders'])))
        self.store.put('prices',prices)
        if s['halt']:self.alerts.set('v2_halt','V2 portfolio halted: '+s['halt'])
        return not (self.stopping and not s['positions'] and not s['orders'])

def run(config,report,database='data/v2/shadow.sqlite3',scenario='stress'):
    r,policy=qualification(report)
    if scenario not in ('stress','zero'):raise ValueError('invalid forward scenario')
    if scenario=='zero':policy=replace(policy,slippage=0)
    from .evidence import tests_fingerprint
    drill_path=Path(report).parent/'drills.json'
    d=json.loads(drill_path.read_text())
    if not d.get('passed') or d.get('code_sha256')!=fingerprint() or d.get('tests_sha256')!=tests_fingerprint():raise ValueError('current V2 fault drills required')
    cfg=replace(Config.load(config),mode='shadow',database=database,evidence_dir='data/v2/evidence' if scenario=='stress' else 'data/v2/evidence/zero')
    if Path(database).resolve()==Path('data/shadow.sqlite3').resolve():raise ValueError('V1 database forbidden')
    with instance_lock(database):
        from .client import audit
        verified=audit(cfg,Path(cfg.evidence_dir)/'interface.json')
        if not all(x['quote_fresh'] and x['deviation']<=.003 for x in verified['symbols'].values()):raise ValueError('initial market validation failed')
        store=Store(database);runner=Runner(cfg,store,policy)
        store.put('frozen_at',r['frozen_at']);signal.signal(signal.SIGINT,runner.stop);signal.signal(signal.SIGTERM,runner.stop)
        try:
            while True:
                start=time.monotonic()
                try:
                    if not runner.tick():break
                except Exception as exc:
                    logging.error('V2 tick failed: %s',type(exc).__name__);runner.alerts.set('v2_runtime','V2 runtime failure: '+type(exc).__name__)
                    runner.engine.state.update(shadow_last_tick=None,shadow_continuous_ms=0)
                    for p in list(runner.engine.state['positions'].values()):runner.engine.exit_intent(p,'runtime_failure',int(time.time()*1000))
                    runner.engine.save()
                    store.put('heartbeat',dict(at=int(time.time()*1000),mode='shadow',healthy=False,halted=runner.engine.state['halt']))
                time.sleep(max(0,5-(time.monotonic()-start)))
        finally:store.close()
