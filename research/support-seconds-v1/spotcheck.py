"""Verify three fixed days against checksummed public archives; never rewrite inputs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import mmap
from pathlib import Path
import ssl
import struct
import sys
import urllib.request
import zipfile

ROOT = Path('/Users/hsjv/Documents/lianghua/data/btc_1s')
DAYS = ('2025-10-01', '2026-03-15', '2026-09-30')
START = int(datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp())
CTX = ssl.create_default_context(cafile='/etc/ssl/cert.pem')

def fetch(url, limit):
    with urllib.request.urlopen(url, timeout=25, context=CTX) as response:
        result = response.read(limit + 1)
    if len(result) > limit:
        raise ValueError('response exceeds expected archive size')
    return result

def verify(day):
    name = 'BTCUSDT-1s-' + day + '.zip'
    url = 'https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/' + name
    try:
        expected = fetch(url + '.CHECKSUM', 2048).decode().split()[0]
        content = fetch(url, 20_000_000)
        actual = hashlib.sha256(content).hexdigest()
        if expected != actual:
            raise ValueError('archive checksum mismatch')
        beginning = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        handles = {key: (ROOT / (key + '.f32')).open('rb') for key in ('close', 'high', 'low')}
        arrays = {key: mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) for key, f in handles.items()}
        count = 0
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = [n for n in archive.namelist() if n.endswith('.csv')]
                if len(names) != 1:
                    raise ValueError('unexpected archive layout')
                with archive.open(names[0]) as stream:
                    for row in csv.reader(io.TextIOWrapper(stream)):
                        # Spot archive timestamps after Jan 2025 are microseconds.
                        stamp = int(row[0])
                        if stamp != (beginning + count) * 1_000_000:
                            raise ValueError('gap, duplicate or misplaced second in original archive')
                        index = beginning + count - START
                        for key, col in (('close', 4), ('high', 2), ('low', 3)):
                            original32 = struct.unpack('<f', struct.pack('<f', float(row[col])))[0]
                            local = struct.unpack_from('<f', arrays[key], index * 4)[0]
                            if original32 != local:
                                raise ValueError(f'local/original mismatch {key} at {stamp}')
                        count += 1
            if count != 86400:
                raise ValueError('incomplete day')
        finally:
            for array in arrays.values():
                array.close()
            for handle in handles.values():
                handle.close()
        return dict(day=day, status='verified', source=url, sha256=actual, rows=count,
                    timeline_contiguous=True, exact_float32_fields=['close', 'high', 'low'])
    except Exception as exc:
        return dict(day=day, status='unverified', source=url, error=str(exc))

if __name__ == '__main__':
    destination = Path(sys.argv[1])
    if destination.exists():
        raise SystemExit('do not overwrite an existing audit')
    with ThreadPoolExecutor(max_workers=3) as pool:
        days = list(pool.map(verify, DAYS))
    result = dict(kind='three_day_spotcheck_not_full_year_verification', days=days,
                  verified_seconds=sum(d.get('rows', 0) for d in days),
                  full_year_original_timestamps_verified=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
