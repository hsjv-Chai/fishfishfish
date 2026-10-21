import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fishbot.config import Config
from fishbot.client import Quote,APIError
from fishbot.storage import Store
from fishbot_v2.runtime import Runner
from fishbot_v2.model import Policy,DAY,HOUR
from test_portfolio import PRICES,T,sig

class FakeSite:
    def __init__(self,code=None):self.code=code
    def quotes(self,now):
        if self.code:raise APIError(self.code)
        return {s:Quote(100.,0,False,now) for s in PRICES}
    def expiry(self):return T+10*DAY
class FakeMarket:
    def price_for(self,s):return 100.,T
    def candles_for(self,*_):return []

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'v2.sqlite3')
        self.runner=Runner(Config(),self.store,Policy(budget='expanded'))
        self.runner.site=FakeSite();self.runner.market=FakeMarket()
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def tick(self):
        with patch('fishbot_v2.runtime.time.time',return_value=(T+10000)/1000),patch('fishbot_v2.runtime.make_signal',return_value=sig()):return self.runner.tick()
    def test_healthy_tick_has_separate_heartbeat(self):
        self.tick();self.assertTrue(self.store.get('heartbeat')['healthy']);self.assertEqual(len(self.runner.engine.state['positions']),2)
    def test_401_no_entry_alert_and_progress_reset(self):
        self.runner.site=FakeSite(401);self.tick()
        self.assertFalse(self.store.get('heartbeat')['healthy']);self.assertFalse(self.runner.engine.state['positions'])
        self.assertEqual(self.runner.engine.state['shadow_continuous_ms'],0)
        self.assertEqual(self.store.db.execute("SELECT active FROM alerts WHERE key='v2_session'").fetchone()[0],1)
    def test_429_does_not_open(self):
        self.runner.site=FakeSite(429);self.tick();self.assertFalse(self.runner.engine.state['positions'])
    def test_freeze_blocks_pre_freeze_signal(self):
        self.store.put('frozen_at',(T+1)/1000);self.tick();self.assertFalse(self.runner.engine.state['positions'])
    def test_shutdown_cancels_unfilled_simulated_buy(self):
        self.runner.engine.buy_intent('BTCUSDT',sig(),'breakout',10000,T)
        self.runner.engine.shutdown(PRICES,T+10000)
        self.assertFalse(self.runner.engine.state['orders']);self.assertFalse(self.runner.engine.state['positions'])
    def test_503_no_entry(self):
        self.runner.site=FakeSite(503);self.tick();self.assertFalse(self.runner.engine.state['positions'])

if __name__=='__main__':unittest.main()
