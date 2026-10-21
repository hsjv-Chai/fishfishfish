from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path

SYMBOLS = ('BTCUSDT', 'ETHUSDT')
HOUR = 3600000
DAY = 24 * HOUR
BUDGETS = {'conservative': (.2, .0025), 'moderate': (.3, .00375), 'expanded': (.4, .005)}
FAMILIES = {'breakout': ('breakout',), 'pullback': ('pullback',),
            'reversion': ('reversion',), 'trend_mix': ('breakout', 'pullback'),
            'all_mix': ('breakout', 'pullback', 'reversion')}

@dataclass(frozen=True)
class Policy:
    family: str = 'breakout'
    budget: str = 'conservative'
    breakout: int = 20
    slippage: float = .001
    def __post_init__(self):
        if self.family not in FAMILIES or self.budget not in BUDGETS or self.breakout not in (10,20):
            raise ValueError('invalid preregistered policy')
        if self.slippage not in (0,.0005,.001,.002): raise ValueError('invalid stress level')
    @property
    def allocation(self): return BUDGETS[self.budget][0]
    @property
    def risk(self): return BUDGETS[self.budget][1]
    @property
    def key(self): return f'{self.family}-{self.budget}-b{self.breakout}'
    def dict(self): return asdict(self)

@dataclass(frozen=True)
class Signal:
    time: int  # hour OPEN timestamp; all information available after time+HOUR
    close: float
    atr: float
    distance: float
    trend: bool
    ranging: bool
    breakout10: bool
    breakout20: bool
    pullback: bool
    reversion: bool
    breakdown: bool
    middle: float
    def entry(self, policy):
        for name in FAMILIES[policy.family]:
            if name == 'breakout' and self.trend and getattr(self,'breakout'+str(policy.breakout)): return name
            if name == 'pullback' and self.trend and self.pullback: return name
            if name == 'reversion' and self.ranging and self.reversion: return name
        return None

def fingerprint():
    # Shared modules are included so unchanged V1 dependencies are still versioned.
    d=hashlib.sha256()
    roots=[Path(__file__).parent,Path(__file__).parent.parent/'fishbot']
    for root in roots:
        for p in sorted(root.glob('*.py')):
            d.update((root.name+'/'+p.name).encode());d.update(p.read_bytes())
    return d.hexdigest()

def atomic_json(path, value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.new');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False));tmp.replace(p)
