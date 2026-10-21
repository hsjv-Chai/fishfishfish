import math,unittest
from array import array
from datetime import datetime,timezone
from types import SimpleNamespace
from dataclasses import replace
from fishbot.strategy import Candle
from fishbot_v2.indicators import make_signal
from fishbot_v2.research import simulate,metrics,Dataset
from fishbot_v2.model import Policy,HOUR,DAY
from fishbot_v2.evidence import six_months_after
from test_portfolio import sig

class ResearchTests(unittest.TestCase):
    def dataset(self,minutes=180,low=100.,close=100.):
        start=HOUR*10000;times=array('q',(start+i*60000 for i in range(minutes)))
        assets={}
        for symbol in ('BTCUSDT','ETHUSDT'):
            assets[symbol]=dict(times=times,open=array('d',[100.]*minutes),low=array('d',[low]*minutes),
                               high=array('d',[max(100.,close)]*minutes),close=array('d',[close]*minutes),
                               signals={start:sig(start-HOUR),start+HOUR:sig(start),start+2*HOUR:sig(start+HOUR)})
        data=Dataset.__new__(Dataset);data.times=times;data.assets=assets
        return data,start
    def test_minute_gap_stop_fills_at_gap(self):
        d,start=self.dataset();d.assets['BTCUSDT']['open'][2]=95;d.assets['BTCUSDT']['low'][2]=94;d.assets['BTCUSDT']['close'][2]=96
        m,trades,_=simulate(d,start,start+180*60000,Policy(budget='expanded',slippage=0))
        closed=[t for t in trades if t['symbol']=='BTCUSDT' and t['reason']=='stop']
        self.assertEqual(len(closed),1);self.assertEqual(closed[0]['exit'],95.)
    def test_boundary_closure_not_counted(self):
        d,start=self.dataset()
        m,trades,_=simulate(d,start,start+180*60000,Policy(budget='expanded',slippage=0))
        self.assertEqual(m['trades'],0);self.assertGreater(m['boundary_closures'],0)
        self.assertTrue(all(t['opened']%HOUR==60000 for t in trades))
    def test_stress_reduces_unchanged_path_profit(self):
        d,start=self.dataset()
        a=simulate(d,start,start+180*60000,Policy(budget='expanded',slippage=0))[0]
        b=simulate(d,start,start+180*60000,Policy(budget='expanded',slippage=.002))[0]
        self.assertLess(b['net_return'],a['net_return'])
    def test_missing_interval_rejected(self):
        d,start=self.dataset()
        with self.assertRaises(ValueError):simulate(d,start,start+181*60000,Policy())
    def test_minimum_filters_truthful(self):
        d,start=self.dataset();m,t,_=simulate(d,start,start+180*60000,Policy())
        self.assertGreater(m['filters'].get('minimum_or_budget',0),0)
    def test_indicators_constant_market(self):
        h=[Candle(i*HOUR,100,100,100,100,1) for i in range(1000)]
        f=[Candle(i*4*HOUR,100,100,100,100,1) for i in range(1000)]
        s=make_signal(h,f)
        self.assertTrue(s.ranging);self.assertFalse(s.trend);self.assertFalse(s.pullback)
        self.assertFalse(s.reversion);self.assertEqual(s.distance,.01)
    def test_indicator_warmup(self):self.assertIsNone(make_signal([],[]))
    def test_unique_signal_and_boundary_metric(self):
        curve=[(0,500000,0,0),(DAY,501000,0,0)]
        trades=[dict(reason='stop',symbol='BTCUSDT',signal_time=0,profit=100),dict(reason='stop',symbol='BTCUSDT',signal_time=0,profit=200),dict(reason='evaluation_end',symbol='ETHUSDT',signal_time=1,profit=700)]
        m=metrics(curve,trades);self.assertEqual(m['trades'],2);self.assertEqual(m['unique_signals'],1);self.assertEqual(m['boundary_closures'],1)
    def test_quarter_carries_high_water_and_halt(self):
        from fishbot_v2.portfolio import initial
        d,start=self.dataset();state=initial();state['high_water']=550000
        m,t,_=simulate(d,start,start+180*60000,Policy(budget='expanded'),carry=state)
        self.assertEqual(m['halt'],'max_drawdown');self.assertFalse(t)
        self.assertEqual(m['ending_risk_state']['high_water'],550000)
    def test_old_freeze_with_two_days_data_cannot_pass(self):
        import tempfile,time
        from pathlib import Path
        from fishbot.storage import Store
        from fishbot_v2.portfolio import Portfolio
        from fishbot_v2.model import fingerprint
        from fishbot_v2.evidence import export
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'shadow.sqlite3';frozen=time.time()-220*86400
            for suffix,slip in [('',.001),('.zero',0)]:
                store=Store(str(db)+suffix);engine=Portfolio(store,Policy(budget='expanded',slippage=slip))
                store.put('code_sha256',fingerprint());store.put('frozen_at',frozen)
                start=int(frozen*1000)+10000
                store.equity(start,500000,False)
                for i in range(30):
                    t=start+i*HOUR;engine.buy_intent('BTCUSDT',sig(t-HOUR),'breakout',10000,t)
                    engine.execute({'BTCUSDT':100.,'ETHUSDT':100.},t)
                    engine.shutdown({'BTCUSDT':101.,'ETHUSDT':100.},t+10000,'mean_target')
                store.equity(start+2*DAY,engine.state['cash'],False);store.close()
            value=export(str(db),Path(tmp)/'forward.json')
            self.assertGreater(value['stress_metrics']['net_return'],0)
            self.assertEqual(value['natural_trades'],30)
            self.assertFalse(value['six_months_observed']);self.assertFalse(value['preliminary_profit_gate'])
    def test_calendar_forward_six_months(self):
        t=datetime(2026,8,31,tzinfo=timezone.utc).timestamp()
        expected=datetime(2027,2,28,tzinfo=timezone.utc).timestamp()
        self.assertEqual(six_months_after(t),expected)

if __name__=='__main__':unittest.main()
