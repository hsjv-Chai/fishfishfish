"""Public, checksummed archives. Independent ETH data; BTC files reused read-only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import csv, hashlib, io, json, logging, urllib.request, zipfile
from fishbot.client import tls_context
from fishbot.history import months, month_ms, add_months, read_minutes
from fishbot.strategy import Candle
from .model import SYMBOLS, atomic_json

def download(symbol,start,end,directory):
    if symbol not in SYMBOLS:raise ValueError('unsupported symbol')
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    def one(month):
        name=f'{symbol}-1m-{month}.zip';p=root/name
        url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{name}'
        def get(url):return urllib.request.urlopen(url,timeout=60,context=tls_context())
        with get(url+'.CHECKSUM') as r:sha=r.read(1000).decode().split()[0]
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:
            tmp=p.with_suffix('.part')
            with get(url) as r,tmp.open('wb') as f:
                while b:=r.read(1024*1024):f.write(b)
            if hashlib.sha256(tmp.read_bytes()).hexdigest()!=sha:raise ValueError('archive checksum mismatch')
            tmp.replace(p)
        logging.warning('verified %s %s',symbol,month)
        return dict(month=month,filename=name,sha256=sha)
    with ThreadPoolExecutor(max_workers=4) as pool:entries=list(pool.map(one,months(start,end)))
    output=root/'minutes.csv';tmp=output.with_suffix('.part');previous=None;count=0
    with tmp.open('w',newline='') as f:
        out=csv.writer(f);out.writerow(['time','open','high','low','close','volume'])
        for entry in entries:
            with zipfile.ZipFile(root/entry['filename']) as z:
                names=[n for n in z.namelist() if n.endswith('.csv')]
                if len(names)!=1:raise ValueError('archive layout changed')
                with z.open(names[0]) as src:
                    for row in csv.reader(io.TextIOWrapper(src)):
                        c=Candle.row(row)
                        if c.time%60000 or (previous is not None and c.time!=previous+60000):raise ValueError('minute gap')
                        if not (0<c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high):raise ValueError('invalid candle')
                        out.writerow([c.time,c.open,c.high,c.low,c.close,c.volume]);previous=c.time;count+=1
    if count!=(month_ms(add_months(end,1))-month_ms(start))//60000:raise ValueError('incomplete archive')
    tmp.replace(output)
    atomic_json(root/'manifest.json',dict(symbol=symbol,archives=entries,rows=count,csv_sha256=hashlib.sha256(output.read_bytes()).hexdigest()))
    return output
