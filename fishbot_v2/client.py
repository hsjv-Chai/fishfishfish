"""Symbol-aware read-only website access. No V2 method can submit a real order."""
import json,math,time,urllib.parse
from pathlib import Path
from fishbot.client import Site,Market,Quote,APIError,SchemaError,number,fish_units
from fishbot.strategy import Candle,closed_candles
from .model import SYMBOLS,HOUR,atomic_json,fingerprint

class ReadSite(Site):
    def call(self,path,body=None,raw=False):
        if body is not None:raise RuntimeError('V2 is read-only; real order submission disabled')
        return super().call(path,body=None,raw=raw)
    def quotes(self,now):
        data=self.call('/api/fish/trade/quote')
        if data.get('fee_rate')!=.0002 or data.get('min_stake')!=1 or data.get('ok') is not True:raise SchemaError('trading rules changed')
        result={}
        for q in data.get('quotes',[]):
            if q.get('symbol') in SYMBOLS:
                if not isinstance(q.get('stale'),bool):raise SchemaError('quote stale field changed')
                result[q['symbol']]=Quote(number(q['price'],True),int(number(q['age_ms'])),q['stale'],now)
        if set(result)!=set(SYMBOLS):raise SchemaError('required quote missing')
        return result
    def candles_for(self,symbol,interval):
        if symbol not in SYMBOLS or interval not in ('1h','4h'):raise ValueError('unsupported market')
        data=self.call('/api/fish/trade/candles?'+urllib.parse.urlencode(dict(symbol=symbol,interval=interval)))
        if data.get('ok') is not True or data.get('symbol')!=symbol:raise SchemaError('candle response changed')
        return [Candle.row(r) for r in data['candles']]
    def buy(self,*a,**kw):raise RuntimeError('V2 real trading disabled')
    def sell(self,*a,**kw):raise RuntimeError('V2 real trading disabled')

class PublicMarket(Market):
    def candles_for(self,symbol,interval,now):
        if symbol not in SYMBOLS or interval not in ('1h','4h'):raise ValueError('unsupported market')
        width=HOUR if interval=='1h' else 4*HOUR
        query=urllib.parse.urlencode(dict(symbol=symbol,interval=interval,limit=1000,endTime=(now//width)*width-1))
        rows,_=self.http.request(self.base+'/api/v3/klines?'+query)
        return closed_candles([Candle.row(r) for r in rows],width,now)
    def price_for(self,symbol):
        if symbol not in SYMBOLS:raise ValueError('unsupported market')
        rows,_=self.http.request(self.base+'/api/v3/aggTrades?'+urllib.parse.urlencode(dict(symbol=symbol,limit=1)),timeout=5)
        if not isinstance(rows,list) or not rows:raise SchemaError('trade feed changed')
        price=number(float(rows[-1]['p']),True);stamp=int(rows[-1]['T'])
        if not -5000<=time.time()*1000-stamp<=15000:raise SchemaError('upstream stale')
        return price,stamp

class ShadowBroker:
    def __init__(self,store,slippage):self.store,self.slippage=store,slippage;self.quotes={}
    def cancel_unfilled(self,order):return order['key'] not in self.store.get('receipts',{})
    def submit(self,order,positions,prices,now):
        cache=self.store.get('receipts',{})
        if order['key'] in cache:return cache[order['key']]
        q=self.quotes.get(order['symbol'])
        if not q or not q.fresh(now):raise ValueError('no fresh site quote for simulated execution')
        from fishbot.math import settle
        if order['kind']=='buy':receipt=dict(id='shadow-'+order['key'],price=q.price*(1+self.slippage),slippage=self.slippage)
        else:
            p=positions[order['position_id']];price=q.price*(1-self.slippage)
            receipt=dict(price=price,payout=settle(p['stake'],p['entry'],price),slippage=self.slippage)
        # The same key recovers the identical simulated receipt after response loss/restart.
        receipt['filled_at']=now;cache[order['key']]=receipt;self.store.put('receipts',cache);return receipt

def reconcile_shadow(store,engine):
    state=engine.state;rows=store.db.execute("SELECT kind,data FROM events WHERE kind IN ('buy_filled','sell_filled') ORDER BY id").fetchall()
    cash=500000;known={}
    for kind,payload in rows:
        data=json.loads(payload)
        if kind=='buy_filled':
            if data['id'] in known:raise SchemaError('duplicate simulated buy')
            known[data['id']]=data;cash-=data['stake']
        else:
            pid=data['position_id']
            if pid not in known:raise SchemaError('unknown simulated close')
            known.pop(pid);cash+=data['payout']
    if cash!=state['cash'] or set(known)!=set(state['positions']):raise SchemaError('shadow ledger mismatch')
    for pid,p in known.items():
        saved=state['positions'][pid]
        if any(saved[k]!=p[k] for k in ('symbol','stake','entry','mechanism','signal_time')):raise SchemaError('position mismatch')
    return True

def verify_remote_positions(snapshot,owned):
    """Pure read-only reconciliation contract for a future explicitly authorized adapter."""
    actual={p['id']:p for p in snapshot['positions']}
    if set(actual)!=set(owned):raise SchemaError('unknown or externally closed position')
    for pid,p in owned.items():
        remote=actual[pid]
        if remote['symbol']!=p['symbol'] or remote['leverage']!=1 or fish_units(remote['stake'])!=p['stake'] or remote['entryPrice']!=p['entry']:raise SchemaError('remote position mismatch')
    return True

def audit(cfg,output):
    site=ReadSite(cfg);market=PublicMarket(cfg.market_url);now=int(time.time()*1000)
    q=site.quotes(now);snapshot=site.snapshot();details={}
    for sym in SYMBOLS:
        px,_=market.price_for(sym);hour=market.candles_for(sym,'1h',now);four=market.candles_for(sym,'4h',now);web=site.candles_for(sym,'1h')
        mapped={c.time:c for c in hour};pairs=[(c,mapped[c.time]) for c in web if c.time in mapped][-100:]
        if len(pairs)<100 or any(abs(a.close/b.close-1)>.003 for a,b in pairs):raise SchemaError('candle source mismatch')
        details[sym]=dict(quote_fresh=q[sym].fresh(int(time.time()*1000)),deviation=abs(px/q[sym].price-1),hourly=len(hour),four_hourly=len(four),matched_candles=len(pairs))
    value=dict(code_sha256=fingerprint(),created_at=time.time(),symbols=details,position_schema_verified=True,observed_positions=len(snapshot['positions']),
               source_allows_independent_positions=True,source_explicit_position_limit=None,
               online_multi_position_limit_verified=False,real_order_probe_performed=False,
               limitation='Read-only endpoints expose position IDs but cannot empirically prove 10 simultaneous positions; live trading remains disabled')
    atomic_json(output,value);return value
