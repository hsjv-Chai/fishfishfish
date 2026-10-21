import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from fishbot.storage import Store, initial_state
from monitor.server import snapshot, readonly, Handler

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.path=self.root/'test.sqlite3'; self.store=Store(self.path)
        self.now=1800000000000
        self.store.put('state',initial_state()); self.store.put('mode','shadow')
        self.store.put('code_sha256','abc'); self.store.put('heartbeat',{'at':self.now,'healthy':True})
        self.store.equity(self.now,500000,False)
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def read(self,**kw): return snapshot(self.path,self.root,self.root,now=self.now,**kw)
    def test_real_data_and_missing_evidence(self):
        d=self.read(); self.assertTrue(d['healthy']); self.assertEqual(d['stats']['equity'],50)
        self.assertFalse(d['activation_ready']); self.assertEqual(d['gates'][0]['status'],'unknown')
    def test_readonly(self):
        db=readonly(self.path)
        with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM equity')
        db.close()
    def test_stale(self):
        self.store.put('heartbeat',{'at':self.now-60001,'healthy':True})
        self.assertEqual(self.read()['status'],'stale')
    def test_no_secret_fields(self):
        self.store.event('buy_filled',{'price':123,'idempotency_key':'SECRET'},now=self.now)
        self.assertNotIn('SECRET',json.dumps(self.read()))
    def test_range_and_bounded_sampling(self):
        with self.assertRaises(ValueError): self.read(window="';DROP TABLE equity")
        with self.store.db:
            self.store.db.executemany('INSERT OR REPLACE INTO equity VALUES (?,?,0)',[(self.now-i*5000,500000) for i in range(20000)])
        d=self.read(window='all'); self.assertLessEqual(len(d['curve']),722)
        self.assertEqual(d['curve'][-1]['at'],self.now)
    def test_gate_short_sample(self):
        m={'net_return':.01,'max_drawdown':.02,'trades':25}
        (self.root/'backtest.json').write_text(json.dumps({'code_sha256':'abc','partitions':{'test':{'0':m,'0.001':m}}}))
        self.assertEqual(self.read()['gates'][0]['status'],'blocked')
    def test_http_readonly(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.database,server.evidence,server.tests=self.path,self.root,self.root
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for path,method,code in [('/api/snapshot','POST',405),('/secrets/session.json','GET',404),('/../server.py','GET',404)]:
                with self.assertRaises(urllib.error.HTTPError) as e: opener.open(urllib.request.Request(url+path,method=method),timeout=3)
                self.assertEqual(e.exception.code,code); e.exception.close()
            with opener.open(url+'/api/snapshot',timeout=3) as r:
                self.assertEqual(r.status,200); self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
        finally: server.shutdown();server.server_close();thread.join()

if __name__=='__main__': unittest.main()
