"""Replay unchanged V1 state machine with additional, isolated trade instrumentation."""
from dataclasses import replace
from bisect import bisect_left
from collections import Counter
from pathlib import Path
import json
from fishbot.engine import Engine,Frame
from fishbot.client import Quote
from fishbot.config import Config
from fishbot.strategy import Signal
from fishbot.backtest import metrics
from fishbot.history import month_ms
from .research import Memory
from .model import HOUR,atomic_json

class Trace(Memory):
    def __init__(self):super().__init__();self.buys={};self.trades=[]
    def equity(self,*_):pass
    def save(self,state,kind=None,data=None,now=None):
        self.kv['state']=state
        if kind=='buy_filled':
            p=dict(state['position']);p.update(min_price=p['entry'],max_price=p['entry']);self.buys[p['id']]=p
        elif kind=='sell_filled':
            p=self.buys.pop(data['id']);exit=data['price']
            self.trades.append(dict(data,opened=p['opened'],closed=now,entry=p['entry'],stake=p['stake'],
                                    holding_hours=(now-p['opened'])/HOUR,signal_time=p['entry_signal'],
                                    mfe=max(0,p['max_price']/p['entry']-1),mae=max(0,1-p['min_price']/p['entry']),
                                    fee_units=p['stake']*exit/p['entry']*.0002))

def replay(data,start,end,slippage=.001):
    first=bisect_left(data.times,start);last=bisect_left(data.times,end)
    store=Trace();engine=Engine(store,replace(Config(),shadow_slippage=slippage));curve=[];sig=None
    asset=data.assets['BTCUSDT']
    for i in range(first,last):
        now=data.times[i];hour=now//HOUR*HOUR
        source=asset['signals'].get(hour)
        if source:
            sig=Signal(source.time,source.close,source.trend,source.breakout20,source.breakdown,source.atr,source.distance)
        px=asset['open'][i];ready=sig if now%HOUR>=60000 else None
        engine.step(Frame(now,px,Quote(px,0,False,now),ready))
        p=engine.state['position']
        if p:
            tracked=store.buys[p['id']]
            limit=max(engine.state['high_water']*.95,engine.state['day_base']*.99)
            risk=max(0,(limit-engine.state['cash'])*p['entry']/(p['stake']*(1-.0002)))
            trigger=max(p['stop'],risk)
            if asset['low'][i]<=trigger:
                base=min(px,trigger);tracked['min_price']=min(tracked['min_price'],base)
                engine.step(Frame(now+30000,base,Quote(base,0,False,now+30000),None))
            else:
                tracked['min_price']=min(tracked['min_price'],asset['low'][i]);tracked['max_price']=max(tracked['max_price'],asset['high'][i])
        close=asset['close'][i]
        engine.step(Frame(now+59999,close,Quote(close,0,False,now+59999),None));curve.append((now,engine.equity(close),bool(engine.state['position'])))
    if engine.state['position']:
        engine.sell_intent('evaluation_end',end-1);engine.execute(Frame(end-1,asset['close'][last-1]))
        curve[-1]=(curve[-1][0],engine.state['cash'],False)
    return metrics(curve,store.trades),store.trades

def run(data,output):
    root=Path(output);report={}
    for name,start,end in [('development','2024-09','2025-09'),('validation','2025-09','2026-03'),('test','2026-03','2026-09')]:
        m,t=replay(data,month_ms(start),month_ms(end))
        report[name]=dict(metrics=m,exit_reasons=dict(Counter(x['reason'] for x in t)),
                          attribution={r:dict(trades=len(ts),profit=sum(x['profit'] for x in ts)/10000) for r in sorted({x['reason'] for x in t}) for ts in [[x for x in t if x['reason']==r]]})
        atomic_json(root/f'v1-{name}-diagnostic-trades.json',t)
    atomic_json(root/'v1-diagnostics.json',report);return report
