import copy,json,math,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from fishbot.storage import Store
from fishbot.client import Quote,SchemaError
from fishbot.math import settle
from fishbot_v2.model import Policy,Signal,HOUR,DAY
from fishbot_v2.portfolio import Portfolio
from fishbot_v2.research import Memory,choose,metrics
from fishbot_v2.client import ShadowBroker,reconcile_shadow,verify_remote_positions,ReadSite
from fishbot_v2.runtime import qualification

T=HOUR*10000
PRICES={'BTCUSDT':100.,'ETHUSDT':100.}
def sig(t=T-HOUR,**kw):
    values=dict(time=t,close=100.,atr=.5,distance=.01,trend=True,ranging=False,
                breakout10=True,breakout20=True,pullback=False,reversion=False,breakdown=False,middle=101.)
    values.update(kw);return Signal(**values)

class PortfolioTests(unittest.TestCase):
    def setUp(self):self.store=Memory();self.engine=Portfolio(self.store,Policy(budget='expanded',slippage=0))
    def enter(self,symbol='BTCUSDT',now=T+10000,mechanism='breakout',distance=.01):
        signal=sig(now-HOUR,distance=distance);amount,_=self.engine.size(symbol,distance,self.engine.equity(PRICES))
        self.engine.buy_intent(symbol,signal,mechanism,amount,now);self.engine.execute(PRICES,now)
        return list(self.engine.state['positions'].values())[-1]
    def test_two_assets_and_duplicate_signal(self):
        signals={s:sig() for s in PRICES};self.engine.on_signals(signals,PRICES,T+10000)
        self.assertEqual(len(self.engine.state['positions']),2)
        self.engine.on_signals(signals,PRICES,T+15000);self.assertEqual(len(self.engine.state['positions']),2)
    def test_no_lookahead(self):
        self.engine.on_signals({'BTCUSDT':sig()},PRICES,T+9999);self.assertFalse(self.engine.state['positions'])
    def test_total_and_coin_risk(self):
        for i in range(12):
            sym='BTCUSDT' if i%2 else 'ETHUSDT';now=T+i*HOUR+10000
            self.engine.on_signals({sym:sig(T+(i-1)*HOUR)},PRICES,now)
        used,risk=self.engine.reserved();e=self.engine.equity(PRICES)
        self.assertLessEqual(len(self.engine.state['positions']),10);self.assertLessEqual(used,e*.4)
        self.assertLessEqual(risk,e*.005)
        for sym in PRICES:
            u,r=self.engine.reserved(sym);self.assertLessEqual(u,e*.4*.6);self.assertLessEqual(r,e*.005*.6)
    def test_ten_slot_hard_limit(self):
        p=self.enter()
        self.engine.state['positions']={str(i):dict(p,id=str(i)) for i in range(10)}
        self.assertEqual(self.engine.size('ETHUSDT',.01,500000),(0,'slots'))
    def test_minimum_and_distance(self):
        self.assertEqual(self.engine.size('BTCUSDT',.04,500000)[0],0)
        self.assertEqual(self.engine.size('BTCUSDT',.041,500000),(0,'stop_distance'))
        self.assertEqual(self.engine.size('BTCUSDT',.01,10000)[0],0)
    def test_risk_not_released_by_stop(self):
        p=self.enter();risk=self.engine.reserved()[1];p['stop']=120
        self.assertEqual(self.engine.reserved()[1],risk)
    def test_no_averaging_down(self):
        self.enter();prices=dict(PRICES,BTCUSDT=99.9)
        self.engine.on_signals({'BTCUSDT':sig(T)},prices,T+HOUR+10000)
        self.assertEqual(len(self.engine.state['positions']),1);self.assertEqual(self.engine.counter['no_averaging_down'],1)
    def test_stop_and_cooldown(self):
        self.enter();self.engine.protective_exits(dict(PRICES,BTCUSDT=98),T+100000)
        self.assertFalse(self.engine.state['positions'])
        self.engine.on_signals({'BTCUSDT':sig(T+HOUR)},PRICES,T+2*HOUR+10000)
        self.assertEqual(self.engine.counter['cooldown'],1)
    def test_individual_stop_fill_prices(self):
        p=self.enter(now=T+10000);q=self.enter(now=T+HOUR+10000)
        self.engine.exit_intent(p,'stop',T+2*HOUR);self.engine.exit_intent(q,'stop',T+2*HOUR)
        self.engine.execute(PRICES,T+2*HOUR,{p['id']:98,q['id']:97})
        self.assertEqual({x['exit'] for x in self.engine.trades},{98,97})
    def test_joint_daily_loss_exit(self):
        self.enter();self.enter('ETHUSDT',now=T+HOUR+10000)
        self.engine.state.update(day_base=500000,day='1971-02-21')
        # Mark the day before applying a known same-day threshold.
        self.engine.mark(PRICES,T+2*HOUR);self.engine.state['day_base']=550000
        self.engine.protective_exits(PRICES,T+2*HOUR+10000)
        self.assertFalse(self.engine.state['positions']);self.assertTrue(self.engine.state['day_pause'])
    def test_drawdown_exit_all(self):
        self.enter();self.enter('ETHUSDT',now=T+HOUR+10000);self.engine.state['high_water']=550000
        self.engine.protective_exits(PRICES,T+2*HOUR)
        self.assertEqual(self.engine.state['halt'],'max_drawdown');self.assertFalse(self.engine.state['positions'])
    def test_reversion_time_limit_and_no_trailing(self):
        p=self.enter(mechanism='reversion');stop=p['stop']
        self.engine.on_signals({'BTCUSDT':sig(T+HOUR,trend=False,ranging=True,breakout10=False,breakout20=False,middle=110)},PRICES,T+2*HOUR+10000)
        self.assertEqual(p['stop'],stop)
        self.engine.on_signals({'BTCUSDT':sig(T+12*HOUR,trend=False,ranging=True,breakout10=False,breakout20=False,middle=110)},PRICES,T+13*HOUR+10000)
        self.assertEqual(self.engine.trades[-1]['reason'],'time_limit')
    def test_family_priority(self):
        s=sig(pullback=True,reversion=True,ranging=True)
        self.assertEqual(s.entry(Policy(family='all_mix')),'breakout')
    def test_stale_no_entry(self):
        self.engine.on_signals({'BTCUSDT':sig()},PRICES,T+10000,False)
        self.assertFalse(self.engine.state['positions'])
    def test_policy_change_refused(self):
        with self.assertRaises(ValueError):Portfolio(self.store,Policy(budget='moderate'))
    def test_partial_exit_failure_does_not_starve(self):
        p=self.enter();q=self.enter('ETHUSDT',now=T+HOUR+10000)
        class Broker:
            def submit(self,o,positions,prices,now):
                if o['position_id']==p['id']:raise TimeoutError()
                p2=positions[o['position_id']];return dict(price=100.,payout=settle(p2['stake'],p2['entry'],100.))
        self.engine.broker=Broker();self.engine.shutdown(PRICES,T+2*HOUR)
        self.assertEqual(set(self.engine.state['positions']),{p['id']});self.assertEqual(len(self.engine.state['orders']),1)
    def test_uncertain_buy_blocks_new_but_allows_exit(self):
        p=self.enter()
        class Broker:
            def submit(self,o,positions,prices,now):
                if o['kind']=='buy':raise TimeoutError()
                p=positions[o['position_id']];return dict(price=100.,payout=settle(p['stake'],p['entry'],100.))
        self.engine.broker=Broker();self.engine.buy_intent('ETHUSDT',sig(), 'breakout',10000,T+HOUR)
        key=next(iter(self.engine.state['orders']));self.engine.execute(PRICES,T+HOUR)
        self.assertTrue(self.engine.state['orders'][key]['uncertain'])
        self.engine.on_signals({'BTCUSDT':sig(T)},PRICES,T+HOUR+10000)
        self.assertEqual(self.engine.counter['uncertain_order'],1)
        self.engine.shutdown(PRICES,T+HOUR+15000);self.assertFalse(self.engine.state['positions'])
    def test_retry_same_key(self):
        keys=[]
        class Broker:
            def submit(self,o,positions,prices,now):
                keys.append(o['key'])
                if len(keys)==1:raise TimeoutError()
                return dict(id='fixed',price=100.)
        self.engine.broker=Broker();self.engine.buy_intent('BTCUSDT',sig(),'breakout',10000,T)
        self.engine.execute(PRICES,T);self.engine.execute(PRICES,T+5000)
        self.assertEqual(keys[0],keys[1]);self.assertEqual(len(self.engine.state['positions']),1)
    def test_invalid_receipt_keeps_intent(self):
        class Broker:
            def submit(self,*_):return dict(id='x',price=float('nan'))
        self.engine.broker=Broker();self.engine.buy_intent('BTCUSDT',sig(),'breakout',10000,T)
        self.engine.execute(PRICES,T);self.assertEqual(self.engine.state['cash'],500000);self.assertTrue(self.engine.state['orders'])
    def test_choose_tie_prefers_twenty(self):
        m=dict(trades=1,average_profit=.01,max_drawdown=.01)
        self.assertEqual(choose([(Policy(breakout=10),m),(Policy(breakout=20),m)]).breakout,20)

class PersistentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'v2.sqlite3';self.store=Store(self.path)
        self.broker=ShadowBroker(self.store,.001);self.broker.quotes={s:Quote(100.,0,False,T) for s in PRICES}
        self.engine=Portfolio(self.store,Policy(budget='expanded'),self.broker)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def test_response_loss_restart(self):
        self.engine.buy_intent('BTCUSDT',sig(),'breakout',10000,T);order=next(iter(self.engine.state['orders'].values()))
        receipt=self.broker.submit(order,{},PRICES,T)
        restarted=Portfolio(self.store,Policy(budget='expanded'),self.broker)
        restarted.execute(PRICES,T)
        self.assertIn(receipt['id'],restarted.state['positions']);self.assertEqual(restarted.state['cash'],490000)
        self.assertTrue(reconcile_shadow(self.store,restarted))
    def test_recovery_and_ledger_tamper(self):
        self.engine.on_signals({'BTCUSDT':sig()},PRICES,T+10000)
        self.assertTrue(reconcile_shadow(self.store,self.engine))
        self.engine.state['cash']+=1
        with self.assertRaises(SchemaError):reconcile_shadow(self.store,self.engine)
    def test_atomic_fill_record(self):
        self.engine.on_signals({'BTCUSDT':sig()},PRICES,T+10000)
        self.assertEqual(self.store.db.execute("select count(*) from events where kind='buy_filled'").fetchone()[0],1)
        self.engine.shutdown(PRICES,T+15000)
        self.assertTrue(reconcile_shadow(self.store,self.engine))
    def test_expired_unfilled_shadow_buy_is_cancelled(self):
        self.engine.buy_intent('BTCUSDT',sig(),'breakout',10000,T)
        self.engine.execute(PRICES,T+120000)
        self.assertFalse(self.engine.state['positions']);self.assertFalse(self.engine.state['orders'])
        self.assertEqual(self.engine.counter['expired_unfilled_buy'],1)
    def test_cached_fill_survives_expiry_and_retains_fill_time(self):
        self.engine.buy_intent('BTCUSDT',sig(),'breakout',10000,T)
        order=next(iter(self.engine.state['orders'].values()))
        receipt=self.broker.submit(order,{},PRICES,T)
        self.engine.execute(PRICES,T+120000)
        self.assertEqual(self.engine.state['positions'][receipt['id']]['opened'],T)
        self.assertTrue(reconcile_shadow(self.store,self.engine))
    def test_stale_site_preserves_exit(self):
        self.engine.on_signals({'BTCUSDT':sig()},PRICES,T+10000)
        self.broker.quotes={}
        self.engine.shutdown(PRICES,T+15000)
        self.assertTrue(self.engine.state['positions']);self.assertTrue(self.engine.state['orders'])
    def test_remote_unknown_or_changed_position(self):
        with self.assertRaises(SchemaError):verify_remote_positions({'positions':[{'id':'unknown'}]}, {})
    def test_site_real_order_forbidden(self):
        site=ReadSite(None)
        with self.assertRaises(RuntimeError):site.buy({})
        with self.assertRaises(RuntimeError):site.call('/api/fish/trade/buy',{})
    def test_no_qualification_no_shadow(self):
        p=Path(self.temp.name)/'research.json';p.write_text(json.dumps({'status':'no_qualified_candidate'}))
        with self.assertRaises(ValueError):qualification(p)
    def test_nontrade_deposit_not_counted(self):
        self.store.event('site_nontrade',dict(amount=100000),T)
        self.engine.save();self.assertTrue(reconcile_shadow(self.store,self.engine));self.assertEqual(self.engine.state['cash'],500000)

if __name__=='__main__':unittest.main()
