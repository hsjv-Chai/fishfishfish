"""One shared multi-position state machine for minute research and shadow runs."""
from collections import Counter
import math, uuid, copy
from datetime import datetime
from zoneinfo import ZoneInfo
from fishbot.math import settle
from .model import HOUR,DAY,SYMBOLS

FEE=.0002
EXECUTION_BUDGET=.001 # each side, independent of stress scenario

def initial():
    return dict(schema=2,cash=500000,positions={},orders={},high_water=500000,
                day='',day_base=500000,day_pause='',halt='',last_signal={},last_exit={},
                filters={},shadow_started=None,shadow_last_tick=None,shadow_continuous_ms=0)

class Portfolio:
    def __init__(self,store,policy,broker=None):
        self.store,self.policy,self.broker=store,policy,broker
        self.state=store.get('state',initial());self.counter=Counter(self.state.get('filters',{}))
        if self.state.get('schema')!=2:raise ValueError('V2 requires isolated database')
        if store.get('policy') and store.get('policy')!=policy.dict():raise ValueError('frozen policy changed; use new database')
        store.put('policy',policy.dict());store.put('mode','shadow')
        self.prices={};self.trades=[];self.max_positions=len(self.state['positions'])
    def save(self):
        self.state['filters']=dict(self.counter);self.store.put('state',self.state)
    def event(self,kind,data,now):self.store.event(kind,data,now)
    def commit(self,kind,data,now):
        self.state['filters']=dict(self.counter)
        self.store.save(self.state,kind,data,now)
    def equity(self,prices):
        return self.state['cash']+sum(settle(p['stake'],p['entry'],prices[p['symbol']]) for p in self.state['positions'].values())
    def reserved(self,symbol=None):
        ps=[p for p in self.state['positions'].values() if symbol is None or p['symbol']==symbol]
        orders=[o for o in self.state['orders'].values() if o['kind']=='buy' and (symbol is None or o['symbol']==symbol)]
        return sum(p['stake'] for p in ps)+sum(o['stake'] for o in orders),sum(p['risk_units'] for p in ps)+sum(o['risk_units'] for o in orders)
    def size(self,symbol,distance,equity):
        if not .01<=distance<=.04:return 0,'stop_distance'
        if len(self.state['positions'])>=10:return 0,'slots'
        used,risk=self.reserved();coin_used,coin_risk=self.reserved(symbol)
        limit=equity*self.policy.allocation;risk_limit=equity*self.policy.risk
        cost=distance+FEE+2*EXECUTION_BUDGET
        # Reserve room for the new fill's immediate mark-to-market fee/slippage loss.
        mark_cost=FEE+self.policy.slippage+.003
        available_cash=self.state['cash']-sum(o.get('stake',0) for o in self.state['orders'].values() if o['kind']=='buy')
        amount=math.floor(max(0,min(limit/10,risk_limit/10/cost,available_cash,
                                   (limit-used)/(1+self.policy.allocation*mark_cost),(risk_limit-risk)/(cost+self.policy.risk*mark_cost),
                                   (.6*limit-coin_used)/(1+.6*self.policy.allocation*mark_cost),
                                   (.6*risk_limit-coin_risk)/(cost+.6*self.policy.risk*mark_cost))))
        return (amount,'') if amount>=10000 else (0,'minimum_or_budget')
    def exit_intent(self,p,reason,now):
        key='sell:'+p['id']
        if key not in self.state['orders']:
            self.state['orders'][key]=dict(key=key,kind='sell',position_id=p['id'],symbol=p['symbol'],reason=reason,created=now,attempts=0,next_try=now)
            self.commit('sell_intent',dict(position_id=p['id'],reason=reason),now)
    def buy_intent(self,symbol,sig,mechanism,amount,now):
        key='v2-'+uuid.uuid4().hex
        order=dict(key=key,kind='buy',symbol=symbol,stake=amount,distance=sig.distance,
                   risk_units=math.ceil(amount*(sig.distance+FEE+2*EXECUTION_BUDGET)),
                   signal_time=sig.time,signal_close=sig.close,mechanism=mechanism,created=now,attempts=0,next_try=now,uncertain=False)
        self.state['orders'][key]=order;self.commit('buy_intent',{k:v for k,v in order.items() if k!='key'},now)
    def apply_buy(self,order,receipt,now):
        pid=receipt['id'];price=receipt['price'];symbol=order['symbol']
        if not isinstance(pid,str) or not math.isfinite(price) or price<=0:raise ValueError('invalid buy receipt')
        if pid in self.state['positions']:raise ValueError('duplicate buy receipt')
        if self.state['cash']<order['stake']:raise ValueError('cash deficit')
        p=dict(id=pid,symbol=symbol,mechanism=order['mechanism'],signal_time=order['signal_time'],stake=order['stake'],
               entry=price,stop=price*(1-order['distance']),initial_stop=price*(1-order['distance']),
               peak=price,opened=receipt.get('filled_at',now),risk_units=order['risk_units'],min_price=price,max_price=price,
               buy_slippage=receipt.get('slippage',0))
        self.state['cash']-=p['stake'];self.state['positions'][pid]=p;self.state['orders'].pop(order['key'],None)
        self.max_positions=max(self.max_positions,len(self.state['positions']));self.commit('buy_filled',dict(p),now)
    def apply_sell(self,order,receipt,now):
        p=self.state['positions'].get(order['position_id'])
        if not p:return
        payout=receipt['payout'];exit_price=receipt['price']
        if not isinstance(payout,int) or not math.isfinite(exit_price) or exit_price<=0 or payout!=settle(p['stake'],p['entry'],exit_price):raise ValueError('settlement mismatch')
        trade=dict(position_id=p['id'],symbol=p['symbol'],mechanism=p['mechanism'],signal_time=p['signal_time'],
                   opened=p['opened'],closed=now,entry=p['entry'],exit=exit_price,stake=p['stake'],payout=payout,
                   profit=payout-p['stake'],reason=order['reason'],holding_hours=(now-p['opened'])/HOUR,
                   mfe=max(0,p['max_price']/p['entry']-1),mae=max(0,1-p['min_price']/p['entry']),
                   fee_units=p['stake']*(exit_price/p['entry'])*FEE,
                   buy_slippage=p['buy_slippage'],sell_slippage=receipt.get('slippage',0))
        self.state['cash']+=payout;self.state['positions'].pop(p['id']);self.state['orders'].pop(order['key'],None)
        self.state['last_exit'][p['symbol']]=now;self.commit('sell_filled',trade,now);self.trades.append(trade)
    def execute(self,prices,now,exit_prices=None):
        # Exits are independent: one failed close must not starve other positions.
        orders=sorted(list(self.state['orders'].values()),key=lambda o:(o['kind']=='buy',o['created'],o['key']))
        for o in orders:
            if o['next_try']>now:continue
            if o['kind']=='buy' and (self.state['halt'] or (self.state['day_pause'] and self.state['day_pause']==self.state['day'])):
                if self.broker is None or (hasattr(self.broker,'cancel_unfilled') and self.broker.cancel_unfilled(o)):
                    self.state['orders'].pop(o['key']);self.commit('buy_cancelled',dict(reason='risk_pause'),now);continue
            if o['kind']=='buy' and any(x['kind']=='buy' and x.get('uncertain') and x['key']!=o['key'] for x in self.state['orders'].values()):continue
            if o['kind']=='buy':
                expired=now-o['signal_time']-HOUR>60000
                drift=abs(prices[o['symbol']]/o.get('signal_close',prices[o['symbol']])-1)>.003
                cancellable=self.broker is None or (hasattr(self.broker,'cancel_unfilled') and self.broker.cancel_unfilled(o))
                if (expired or drift) and cancellable:
                    reason='expired_unfilled_buy' if expired else 'execution_deviation'
                    self.state['orders'].pop(o['key']);self.counter[reason]+=1
                    self.commit('buy_cancelled',dict(reason=reason),now);continue
            o['attempts']+=1;self.save();before=copy.deepcopy(self.state)
            try:
                if self.broker:
                    receipt=self.broker.submit(o,dict(self.state['positions']),prices,now)
                elif o['kind']=='buy':
                    receipt=dict(id='shadow-'+o['key'],price=prices[o['symbol']]*(1+self.policy.slippage),slippage=self.policy.slippage)
                else:
                    p=self.state['positions'][o['position_id']];price=(exit_prices or {}).get(p['id'],prices[p['symbol']])*(1-self.policy.slippage)
                    receipt=dict(price=price,payout=settle(p['stake'],p['entry'],price),slippage=self.policy.slippage)
                if o['kind']=='buy':self.apply_buy(o,receipt,now)
                else:self.apply_sell(o,receipt,now)
            except Exception as exc:
                self.state=before;o=self.state['orders'][o['key']]
                if o['kind']=='buy':o['uncertain']=True
                o['next_try']=now+min(300000,5000*2**min(o['attempts']-1,6))
                self.save();self.event('order_retry',dict(kind=o['kind'],attempt=o['attempts'],error=type(exc).__name__),now)
    def mark(self,prices,now):
        self.prices=prices
        for p in self.state['positions'].values():
            price=prices[p['symbol']];p['min_price']=min(p['min_price'],price);p['max_price']=max(p['max_price'],price)
        equity=self.equity(prices);day=datetime.fromtimestamp(now/1000,ZoneInfo('Asia/Shanghai')).date().isoformat()
        if day!=self.state['day']:self.state.update(day=day,day_base=equity,day_pause='')
        self.state['high_water']=max(self.state['high_water'],equity)
        if equity<=self.state['high_water']*.95:self.state['halt']='max_drawdown'
        if equity<=self.state['day_base']*.99:self.state['day_pause']=day
        return equity
    def protective_exits(self,prices,now):
        self.mark(prices,now)
        for p in list(self.state['positions'].values()):
            reason=self.state['halt'] or ('daily_loss' if self.state['day_pause']==self.state['day'] else '')
            if not reason and prices[p['symbol']]<=p['stop']:reason='stop'
            if reason:self.exit_intent(p,reason,now)
        self.execute(prices,now)
    def on_signals(self,signals,prices,now,entry_enabled=True):
        self.protective_exits(prices,now)
        fresh={sym:sig for sym,sig in signals.items() if sig and sig.time>self.state['last_signal'].get(sym,-1) and now>=sig.time+HOUR+10000}
        exiting=set()
        for p in list(self.state['positions'].values()):
            sig=fresh.get(p['symbol']);reason=None
            if not sig:continue
            if p['mechanism']=='reversion':
                if sig.close>=sig.middle:reason='mean_target'
                elif now-p['opened']>=12*HOUR:reason='time_limit'
                elif not sig.ranging:reason='range_off'
            else:
                if sig.time+HOUR>p['opened']:
                    p['peak']=max(p['peak'],sig.close);p['stop']=max(p['stop'],p['peak']-3*sig.atr)
                if sig.breakdown:reason='breakdown'
                elif not sig.trend:reason='trend_off'
                elif prices[p['symbol']]<=p['stop']:reason='trailing_stop'
            if reason:self.exit_intent(p,reason,now);exiting.add(p['symbol'])
        self.execute(prices,now)
        for symbol in SYMBOLS:
            sig=fresh.get(symbol)
            if not sig:continue
            self.state['last_signal'][symbol]=sig.time
            mechanism=sig.entry(self.policy)
            if not mechanism:continue
            reason=''
            if self.state['halt'] or self.state['day_pause']==self.state['day']:reason='risk_pause'
            elif not entry_enabled or now-sig.time-HOUR>60000:reason='market_or_signal_stale'
            elif any(o['kind']=='buy' for o in self.state['orders'].values()):reason='uncertain_order'
            elif symbol in exiting:reason='exit_same_bar'
            elif symbol in self.state['last_exit'] and now<((self.state['last_exit'][symbol]+HOUR-1)//HOUR+6)*HOUR:reason='cooldown'
            elif abs(prices[symbol]/sig.close-1)>.003:reason='signal_deviation'
            elif any(p['symbol']==symbol and prices[symbol]<p['entry'] for p in self.state['positions'].values()):reason='no_averaging_down'
            if reason:self.counter[reason]+=1;continue
            amount,reason=self.size(symbol,sig.distance,self.equity(prices))
            if not amount:self.counter[reason]+=1;continue
            self.buy_intent(symbol,sig,mechanism,amount,now);self.execute(prices,now)
        self.save()
    def shutdown(self,prices,now,reason='shutdown'):
        for o in list(self.state['orders'].values()):
            if o['kind']=='buy' and (self.broker is None or (hasattr(self.broker,'cancel_unfilled') and self.broker.cancel_unfilled(o))):
                self.state['orders'].pop(o['key']);self.commit('buy_cancelled',dict(reason=reason),now)
        self.execute(prices,now) # Recover already persisted simulated receipts first.
        for p in list(self.state['positions'].values()):self.exit_intent(p,reason,now)
        self.execute(prices,now);self.save()
