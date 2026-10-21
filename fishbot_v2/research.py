"""Bounded, chronological walk-forward research; all viewed data are research data."""
from array import array
from bisect import bisect_left
from dataclasses import replace
from pathlib import Path
import hashlib,json,logging,math,pickle,statistics,time,copy
from collections import Counter
from fishbot.history import read_minutes,month_ms,add_months
from fishbot.strategy import Candle
from .indicators import Bars
from .model import Policy,SYMBOLS,HOUR,DAY,fingerprint,atomic_json
from .portfolio import Portfolio

class Memory:
    def __init__(self):self.kv={};self.events=[]
    def get(self,k,d=None):return self.kv.get(k,d)
    def put(self,k,v):self.kv[k]=v
    def save(self,state,kind=None,data=None,now=None):
        self.put('state',state)
        if kind:self.event(kind,data,now)
    def event(self,kind,data,now):
        if kind.endswith('_filled'):self.events.append((kind,dict(data),now))

class Dataset:
    def __init__(self,paths,cache='data/v2/cache'):
        self.assets={};self.paths={s:str(paths[s]) for s in SYMBOLS};self.hashes={}
        cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
        indicator_sha=hashlib.sha256((Path(__file__).with_name('indicators.py').read_bytes()+Path(__file__).with_name('model.py').read_bytes()+Path(__file__).parent.parent.joinpath('fishbot/strategy.py').read_bytes())).hexdigest()
        for sym in SYMBOLS:
            source=Path(paths[sym]);sha=hashlib.sha256(source.read_bytes()).hexdigest();self.hashes[sym]=sha
            dest=cache/(sym+'-'+sha[:16]+'-'+indicator_sha[:12]+'.pickle')
            if dest.exists():
                with dest.open('rb') as f:asset=pickle.load(f) # trusted locally generated cache only
            else:
                cols={k:array('d') for k in ('open','high','low','close')};ts=array('q');signals={};bars=Bars()
                for c in read_minutes(source):
                    ts.append(c.time)
                    for k in cols:cols[k].append(getattr(c,k))
                    sig=bars.add(c)
                    if sig:signals[sig.time+HOUR]=sig
                asset=dict(times=ts,signals=signals,**cols)
                with dest.open('wb') as f:pickle.dump(asset,f,protocol=5)
            logging.warning('prepared %s: %s minutes, %s signals',sym,len(asset['times']),len(asset['signals']))
            self.assets[sym]=asset
        a,b=(self.assets[s]['times'] for s in SYMBOLS)
        if a!=b:raise ValueError('assets must have identical, contiguous minute timeline')
        self.times=a
    def prices(self,i,k='close'):return {s:self.assets[s][k][i] for s in SYMBOLS}

def metrics(curve,trades,start=500000):
    natural=[t for t in trades if t['reason']!='evaluation_end'];peak=start;dd=0;daily={}
    for t,e,n,stake in curve:
        peak=max(peak,e);dd=max(dd,1-e/peak);daily[t//DAY]=e
    vals=[start,*daily.values()];rs=[b/a-1 for a,b in zip(vals,vals[1:]) if a]
    sd=statistics.stdev(rs) if len(rs)>1 else 0
    wins=[t['profit'] for t in natural if t['profit']>0];losses=[t['profit'] for t in natural if t['profit']<0]
    return dict(net_return=curve[-1][1]/start-1,max_drawdown=dd,trades=len(natural),boundary_closures=len(trades)-len(natural),
                unique_signals=len({(t['symbol'],t['signal_time']) for t in natural}),
                average_profit=sum(t['profit'] for t in natural)/max(1,len(natural))/10000,
                profit_factor=sum(wins)/-sum(losses) if losses else None,
                win_rate=len(wins)/max(1,len(natural)),daily_sharpe=statistics.mean(rs)/sd*math.sqrt(365) if sd else None,
                exposure=sum(bool(x[2]) for x in curve)/len(curve),average_allocation=statistics.mean(x[3]/x[1] for x in curve),
                final_fish=curve[-1][1]/10000)

def simulate(data,start,end,policy,capital=500000,carry=None):
    first=bisect_left(data.times,start);last=bisect_left(data.times,end)
    if first==len(data.times) or data.times[first]!=start or last-first!=(end-start)//60000:raise ValueError('incomplete evaluation interval')
    if not all(start in data.assets[s]['signals'] for s in SYMBOLS):raise ValueError('1000 closed 4h candles warmup required')
    store=Memory();engine=Portfolio(store,policy)
    if carry is not None:
        if carry['positions'] or carry['orders']:raise ValueError('boundary carry must be fully settled')
        engine.state=copy.deepcopy(carry)
        if engine.state['cash']!=capital:raise ValueError('carry capital mismatch')
    else:engine.state.update(cash=capital,high_water=capital,day_base=capital)
    hourly_curve=[];sample_peak=capital;max_dd=0;exposed_minutes=0;allocation_sum=0;seen_minutes=0;coheld=0
    by_symbol_exposed=Counter();holding_samples={s:[] for s in SYMBOLS};i=first
    while i<last:
        now=data.times[i];prices=data.prices(i,'open')
        if now%HOUR==60000:
            signals={s:data.assets[s]['signals'].get(now-60000) for s in SYMBOLS}
            engine.on_signals(signals,prices,now)
        elif engine.state['positions'] or engine.state['orders']:
            engine.protective_exits(prices,now)
        if engine.state['positions']:
            # No future hourly trailing levels. Individual stops fill at gap open or stop.
            lows=data.prices(i,'low');exit_prices={}
            for p in list(engine.state['positions'].values()):
                if lows[p['symbol']]<=p['stop']:
                    engine.exit_intent(p,'stop',now+30000);exit_prices[p['id']]=min(prices[p['symbol']],p['stop'])
            # Capture minute extremes only for positions actually exposed, before selling.
            for p in engine.state['positions'].values():
                p['min_price']=min(p['min_price'],max(lows[p['symbol']],exit_prices[p['id']]) if 'sell:'+p['id'] in engine.state['orders'] else lows[p['symbol']])
                # Minute high after a close cannot be attributed to a closed position.
                if 'sell:'+p['id'] not in engine.state['orders']:p['max_price']=max(p['max_price'],data.assets[p['symbol']]['high'][i])
            engine.execute(prices,now+30000,exit_prices)
            if engine.state['positions']:
                threshold=max(engine.state['high_water']*.95,engine.state['day_base']*.99)
                if engine.equity(lows)<=threshold:
                    # Conservative joint-low path; BTC/ETH minute lows may not be simultaneous.
                    lo,hi=0.,1.
                    for _ in range(24):
                        mid=(lo+hi)/2;q={s:prices[s]+mid*(lows[s]-prices[s]) for s in SYMBOLS}
                        if engine.equity(q)>threshold:lo=mid
                        else:hi=mid
                    q={s:prices[s]+hi*(lows[s]-prices[s]) for s in SYMBOLS}
                    engine.protective_exits(q,now+30001)
        close=data.prices(i);engine.mark(close,now+59999)
        e=engine.equity(close);sample_peak=max(sample_peak,e,engine.state['high_water']);max_dd=max(max_dd,1-e/sample_peak)
        n=len(engine.state['positions']);stake=sum(p['stake'] for p in engine.state['positions'].values())
        holding={p['symbol'] for p in engine.state['positions'].values()}
        if now%HOUR==0:
            for sym in SYMBOLS:holding_samples[sym].append(sum(p['stake'] for p in engine.state['positions'].values() if p['symbol']==sym)/e)
        # Flat spans can skip prices without changing any signal, daily boundary, or position.
        advance=1
        if not n and not engine.state['orders']:
            # Visit both :00 (Beijing day reset) and :01 (signal execution) each hour.
            offset=(now%HOUR)//60000
            if offset>=1:advance=min(60-offset,last-i)
        exposed_minutes+=bool(n)*advance;allocation_sum+=stake/e*advance;seen_minutes+=advance
        coheld+=int(len(holding)==2)*advance
        for s in holding:by_symbol_exposed[s]+=advance
        if (now+60000)%HOUR==0 or advance>1 or i==first:hourly_curve.append((now,e,n,stake))
        i+=advance
    final_price=data.prices(last-1)
    engine.shutdown(final_price,end-1,'evaluation_end')
    hourly_curve.append((end-1,engine.state['cash'],0,0))
    m=metrics(hourly_curve,engine.trades,capital)
    m.update(max_drawdown=max(m['max_drawdown'],max_dd),exposure=exposed_minutes/max(1,seen_minutes),
             average_allocation=allocation_sum/max(1,seen_minutes),max_positions=engine.max_positions,
             coheld_fraction=coheld/max(1,seen_minutes),symbol_exposure={s:by_symbol_exposed[s]/max(1,seen_minutes) for s in SYMBOLS},
             filters=dict(engine.counter),halt=engine.state['halt'])
    grouped={}
    for field in ('symbol','mechanism'):
        grouped[field]={key:dict(trades=len(ts),profit=sum(t['profit'] for t in ts)/10000) for key in
                        sorted({t[field] for t in engine.trades}) for ts in [[t for t in engine.trades if t[field]==key and t['reason']!='evaluation_end']]}
    m['attribution']=grouped
    hs=list(holding_samples.values())
    m['holding_weight_correlation']=statistics.correlation(*hs) if len(hs[0])>1 and all(statistics.pstdev(x)>0 for x in hs) else None
    m['ending_risk_state']=copy.deepcopy(engine.state)
    return m,engine.trades,hourly_curve

def choose(results):
    # Development choice; no validation/test values are consulted.
    def rank(item):
        p,m=item
        return (m['average_profit'] if m['trades'] else -float('inf'),-m['max_drawdown'],-p.allocation,p.breakout)
    return max(results,key=rank)[0]

def correlation(data,start,end):
    first=bisect_left(data.times,start);last=bisect_left(data.times,end);rs=[]
    for sym in SYMBOLS:
        c=data.assets[sym]['close'];r=[math.log(c[i]/c[i-60]) for i in range(first+60,last,60)];rs.append(r)
    if len(rs[0])<2 or not statistics.pstdev(rs[0]) or not statistics.pstdev(rs[1]):return None
    return statistics.correlation(*rs)

def run(data,output='reports/v2',start_month='2024-09'):
    root=Path(output);root.mkdir(parents=True,exist_ok=True)
    code=fingerprint();policies=[Policy(f,b,n) for f in ('breakout','pullback','reversion','trend_mix','all_mix') for b in ('conservative','moderate','expanded') for n in ((10,20) if f in ('breakout','trend_mix','all_mix') else (20,))]
    experiment=dict(schema=2,code_sha256=code,data_sha256=data.hashes,created_at=time.time(),status='research_running',
                    viewed_data='2024-09..2026-08 are research data, never pristine out-of-sample',
                    candidate_count=len(policies),policies=[p.dict() for p in policies],folds=[],candidates={},
                    selection_rule='breakout lookback locked on first 12m development; mean net trade profit; OOS pass gates; ties lower DD, lower allocation, 20-bar preference',
                    assumptions=['1m OHLC, fills at :01; joint BTC/ETH low path is conservative and may not be simultaneous',
                                 'high-water uses open/close/risk checks; 5-second highs are unavailable',
                                 'funding, leverage and shorts absent; fee=0.02%; two-sided adverse slippage',
                                 'each development window resets 50 fish; assessment folds carry high-water, pause, halt and cooldown, compound one portfolio with boundary closure'])
    if (root/'research.json').exists():raise ValueError('use new experiment directory')
    atomic_json(root/'research.json',experiment)
    trials=root/'trials.jsonl'
    if trials.exists():raise ValueError('use a new output directory; never overwrite previous experiments')
    # Breakout lookback is chosen ONCE on the first 12m development window.
    # Fixed budget and family remain distinct candidates throughout OOS assessment.
    aggregates={};fold_start=start_month;locked={}
    for fold in range(4):
        train_start=month_ms(fold_start);test_month=add_months(fold_start,12);test_start=month_ms(test_month);test_end=month_ms(add_months(test_month,3))
        develop={}
        for p in policies:
            logging.warning('fold %s development %s',fold,p.key)
            m,t,c=simulate(data,train_start,test_start,p)
            m.pop('ending_risk_state',None);develop[p.key]=m
            with trials.open('a') as f:f.write(json.dumps(dict(fold=fold,stage='development',policy=p.dict(),metrics=m,code_sha256=code),allow_nan=False)+'\n')
        fold_record=dict(train=[fold_start,test_month],assessment=[test_month,add_months(test_month,3)],choices={})
        for family in ('breakout','pullback','reversion','trend_mix','all_mix'):
            for budget in ('conservative','moderate','expanded'):
                candidates=[p for p in policies if p.family==family and p.budget==budget]
                key=family+'-'+budget
                if fold==0:locked[key]=choose([(p,develop[p.key]) for p in candidates])
                best=locked[key]
                fold_record['choices'][key]=best.dict()
                if key not in aggregates:aggregates[key]={str(s):dict(curve=[],trades=[],windows=[],capital=500000,carry=None) for s in (0,.0005,.001,.002)}
                for slip in (0,.0005,.001,.002):
                    a=aggregates[key][str(slip)];p=replace(best,slippage=slip)
                    logging.warning('fold %s assessment %s slip %s',fold,p.key,slip)
                    m,t,c=simulate(data,test_start,test_end,p,a['capital'],a['carry']);a['capital']=round(m['final_fish']*10000);a['carry']=m.pop('ending_risk_state')
                    a['curve'].extend(c);a['trades'].extend(t);a['windows'].append(m)
                    atomic_json(root/f'{key}-fold{fold}-{slip}-trades.json',t)
                    with trials.open('a') as f:f.write(json.dumps(dict(fold=fold,stage='assessment',policy=p.dict(),metrics=m,code_sha256=code),allow_nan=False)+'\n')
        experiment['folds'].append(fold_record);atomic_json(root/'research.json',experiment);fold_start=add_months(fold_start,3)
    for key,stresses in aggregates.items():
        result={}
        for slip,a in stresses.items():
            m=metrics(a['curve'],a['trades']);m['max_drawdown']=max(m['max_drawdown'],max(w['max_drawdown'] for w in a['windows']))
            m.update(exposure=sum(w['exposure'] for w in a['windows'])/len(a['windows']),
                     average_allocation=sum(w['average_allocation'] for w in a['windows'])/len(a['windows']),
                     coheld_fraction=sum(w['coheld_fraction'] for w in a['windows'])/len(a['windows']),
                     attribution={field:{key:dict(trades=len(ts),profit=sum(t['profit'] for t in ts)/10000) for key in sorted({t[field] for t in a['trades']}) for ts in [[t for t in a['trades'] if t[field]==key and t['reason']!='evaluation_end']]} for field in ('symbol','mechanism')},
                     windows=a['windows'],max_positions=max(w['max_positions'] for w in a['windows']),filters=dict(sum((Counter(w['filters']) for w in a['windows']),Counter())))
            result[slip]=m
        m=result['0.001'];passed=m['net_return']>0 and m['max_drawdown']<=.05 and m['trades']>=30 and sum(w['net_return']>0 for w in m['windows'])>=2 and not any(w['halt'] for w in m['windows'])
        experiment['candidates'][key]=dict(stress=result,passed=passed)
    experiment['price_return_correlation']=correlation(data,month_ms('2025-09'),month_ms('2026-09'))
    qualified=[k for k,v in experiment['candidates'].items() if v['passed']]
    if qualified:
        def rank(k):
            m=experiment['candidates'][k]['stress']['0.001'];budget=k.rsplit('-',1)[1]
            return (m['average_profit'],-m['max_drawdown'],-Policy(budget=budget).allocation,k)
        key=max(qualified,key=rank);family,budget=key.rsplit('-',1)
        # Preserve the first-development-period lookback; no retuning on assessment data.
        best=locked[key]
        experiment.update(status='qualified_for_shadow',selected=best.dict(),frozen_at=time.time(),live_qualified=False)
    else:experiment.update(status='no_qualified_candidate',selected=None,live_qualified=False)
    if fingerprint()!=code:
        experiment.update(status='invalid_code_changed_during_research',selected=None,live_qualified=False)
    atomic_json(root/'research.json',experiment)
    return experiment
