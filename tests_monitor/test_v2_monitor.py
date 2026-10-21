import json,tempfile,unittest
from pathlib import Path
from fishbot.storage import Store
from fishbot_v2.model import Policy,fingerprint
from fishbot_v2.portfolio import Portfolio
from monitor.v2 import snapshot

class V2MonitorTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def read(self):return snapshot(self.data,1800000000000,Path(__file__).resolve().parent.parent)
    def test_missing_report_not_invented_running(self):
        d=self.read();self.assertEqual(d['status'],'research_not_available');self.assertFalse(d['shadow']['running']);self.assertFalse(d['live_enabled'])
    def test_failed_research_and_current_code(self):
        p=self.data/'v2/evidence';p.mkdir(parents=True)
        (p/'research.json').write_text(json.dumps({'code_sha256':fingerprint(),'status':'no_qualified_candidate','candidates':{'reversion-moderate':{'passed':False,'stress':{'0.001':{'net_return':-.01}}}}}))
        d=self.read();self.assertTrue(d['code_matches']);self.assertEqual(len(d['candidates']),1);self.assertFalse(d['candidates'][0]['passed'])
    def test_readonly_position_summary_and_stale(self):
        store=Store(self.data/'v2/shadow.sqlite3');p=Portfolio(store,Policy());p.save()
        store.equity(1799999900000,500000,False);store.put('heartbeat',{'at':1799999900000,'healthy':True})
        try:
            d=self.read();self.assertEqual(d['shadow']['status'],'stale');self.assertEqual(d['shadow']['stats']['equity'],50.)
            self.assertNotIn('receipts',json.dumps(d))
        finally:store.close()

if __name__=='__main__':unittest.main()
