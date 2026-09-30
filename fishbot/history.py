"""Verified public Binance monthly archives, normalized into a local CSV."""
from datetime import datetime, timezone
from pathlib import Path
import calendar
import csv
import hashlib
import io
import json
import logging
import urllib.error
import urllib.request
import zipfile

from .client import tls_context
from .strategy import Candle


def months(start, end):
    year, month = map(int, start.split("-"))
    while f"{year:04}-{month:02}" <= end:
        yield f"{year:04}-{month:02}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def month_ms(month):
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc).timestamp() * 1000)


def add_months(month, n):
    y, m = map(int, month.split("-"))
    y, m = divmod(y * 12 + m - 1 + n, 12)
    return f"{y:04}-{m + 1:02}"


def download(start, end, directory):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    manifest = []
    for month in months(start, end):
        name = f"BTCUSDT-1m-{month}.zip"
        target = path / name
        url = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/" + name
        logging.info("archive %s", month)
        try:
            with urllib.request.urlopen(url + ".CHECKSUM", context=tls_context(), timeout=30) as res:
                expected = res.read(1000).decode().split()[0]
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                tmp = target.with_suffix(".part")
                with urllib.request.urlopen(url, context=tls_context(), timeout=60) as res, tmp.open("wb") as out:
                    while block := res.read(1024 * 1024):
                        out.write(block)
                if hashlib.sha256(tmp.read_bytes()).hexdigest() != expected:
                    raise ValueError("archive checksum mismatch")
                tmp.replace(target)
            manifest.append(dict(month=month, filename=name, sha256=expected))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"archive unavailable: {month}; no complete dataset produced") from exc
    output = path / "minutes.csv"
    temp = output.with_suffix(".part")
    previous = None
    count = 0
    with temp.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for entry in manifest:
            with zipfile.ZipFile(path / entry["filename"]) as archive:
                names = [n for n in archive.namelist() if n.endswith(".csv")]
                if len(names) != 1:
                    raise ValueError("unexpected archive contents")
                with archive.open(names[0]) as binary:
                    for row in csv.reader(io.TextIOWrapper(binary)):
                        c = Candle.row(row)
                        if c.time % 60000 or (previous is not None and c.time != previous + 60000):
                            raise ValueError("archive minute gap/duplicate")
                        writer.writerow([c.time, c.open, c.high, c.low, c.close, c.volume])
                        previous, count = c.time, count + 1
    expected_count = (month_ms(add_months(end, 1)) - month_ms(start)) // 60000
    if count != expected_count:
        raise ValueError("incomplete history")
    temp.replace(output)
    (path / "manifest.json").write_text(json.dumps(dict(archives=manifest, rows=count,
        csv_sha256=hashlib.sha256(output.read_bytes()).hexdigest()), indent=2))
    return output


def read_minutes(path):
    with Path(path).open() as file:
        reader = csv.DictReader(file)
        previous = None
        for r in reader:
            c = Candle(int(r["time"]), *(float(r[k]) for k in ("open", "high", "low", "close", "volume")))
            if c.time % 60000 or (previous is not None and c.time != previous + 60000):
                raise ValueError("minute data must be contiguous")
            if not (0 < c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high):
                raise ValueError("invalid historical OHLC")
            previous = c.time
            yield c
