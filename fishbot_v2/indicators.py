"""Closed-bar indicators. The rolling 1000-bar EMA/ATR match V1 warmup."""
from collections import deque
import statistics
from fishbot.strategy import ema, atr, Candle
from .model import Signal

def make_signal(hourly, four):
    if len(hourly)<1000 or len(four)<1000: return None
    cs=[c.close for c in four];e50=ema(cs,50);e200=ema(cs,200)
    old=ema(cs[:-3],200)
    trend=cs[-1]>e200 and e50>e200
    ranging=abs(e50/e200-1)<=.01 and abs(e200/old-1)<=.002
    hc=[c.close for c in hourly];prev=hourly[-2];last=hourly[-1]
    lower_prev=statistics.mean(hc[-21:-1])-2*statistics.pstdev(hc[-21:-1])
    middle=statistics.mean(hc[-20:]);lower=middle-2*statistics.pstdev(hc[-20:])
    a=atr(hourly)
    return Signal(last.time,last.close,a,max(2*a/last.close,.01),trend,ranging,
                  last.close>max(c.high for c in hourly[-11:-1]),
                  last.close>max(c.high for c in hourly[-21:-1]),
                  prev.close<=ema(hc[:-1],20) and last.close>ema(hc,20) and last.close>prev.high,
                  prev.close<lower_prev and last.close>=lower,
                  last.close<min(c.low for c in hourly[-11:-1]),middle)

class Bars:
    def __init__(self):
        self.hour=deque(maxlen=1000);self.four=deque(maxlen=1000)
        self.h=[];self.f=[]
    def add(self,c):
        self.h.append(c);self.f.append(c)
        signal=None
        if (c.time+60000)%3600000==0:
            self.hour.append(self.aggregate(self.h,60));self.h=[]
        if (c.time+60000)%14400000==0:
            self.four.append(self.aggregate(self.f,240));self.f=[]
        if (c.time+60000)%3600000==0:signal=make_signal(list(self.hour),list(self.four))
        return signal
    @staticmethod
    def aggregate(chunk,n):
        if len(chunk)!=n or chunk[0].time%(n*60000):raise ValueError('incomplete bar')
        return Candle(chunk[0].time,chunk[0].open,max(c.high for c in chunk),min(c.low for c in chunk),chunk[-1].close,sum(c.volume for c in chunk))
