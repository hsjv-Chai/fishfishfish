import argparse,json,logging
from .history import download

def main():
    logging.basicConfig(level=logging.WARNING,format='%(asctime)s %(message)s')
    p=argparse.ArgumentParser(description='V2 bounded research and qualified shadow mode only')
    sub=p.add_subparsers(dest='command',required=True)
    d=sub.add_parser('download');d.add_argument('--symbol',required=True);d.add_argument('--start',default='2024-03');d.add_argument('--end',default='2026-08');d.add_argument('--directory',default='data/v2/eth')
    for name in ('research','diagnostics'):
        r=sub.add_parser(name);r.add_argument('--btc',default='data/history/minutes.csv');r.add_argument('--eth',default='data/v2/eth/minutes.csv');r.add_argument('--output',default='reports/v2' if name=='research' else 'reports/v2/diagnostics')
    a=sub.add_parser('audit');a.add_argument('--config',default='config.toml');a.add_argument('--output',default='data/v2/evidence/interface.json')
    d=sub.add_parser('drills');d.add_argument('--output',default='data/v2/evidence/drills.json')
    r=sub.add_parser('run');r.add_argument('--config',default='config.toml');r.add_argument('--report',default='data/v2/evidence/research.json');r.add_argument('--database',default='data/v2/shadow.sqlite3');r.add_argument('--scenario',choices=('stress','zero'),default='stress')
    w=sub.add_parser('watchdog');w.add_argument('--config',default='config.toml');w.add_argument('--database',default='data/v2/shadow.sqlite3')
    e=sub.add_parser('export');e.add_argument('--database',default='data/v2/shadow.sqlite3');e.add_argument('--output',default='data/v2/evidence/forward.json')
    args=p.parse_args()
    if args.command=='download':download(args.symbol,args.start,args.end,args.directory)
    elif args.command in ('research','diagnostics'):
        from .research import Dataset
        data=Dataset({'BTCUSDT':args.btc,'ETHUSDT':args.eth})
        if args.command=='research':
            from .research import run
            print(run(data,args.output)['status'])
        else:
            from .diagnostics import run
            print(json.dumps(run(data,args.output),ensure_ascii=False))
    elif args.command=='audit':
        from fishbot.config import Config
        from .client import audit
        print(json.dumps(audit(Config.load(args.config),args.output),ensure_ascii=False))
    elif args.command=='drills':
        from .evidence import drills
        print(json.dumps(drills(args.output)))
    elif args.command=='run':
        from .runtime import run
        run(args.config,args.report,args.database,args.scenario)
    elif args.command=='watchdog':
        from dataclasses import replace
        from fishbot.config import Config
        from fishbot.runtime import watchdog
        watchdog(replace(Config.load(args.config),database=args.database))
    elif args.command=='export':
        from .evidence import export
        print(json.dumps(export(args.database,args.output),ensure_ascii=False))
