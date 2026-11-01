import json, time, urllib.request, datetime as dt
def load(path='config.json'): return json.load(open(path))
def sol_usd(cfg, live=True):
    if live:
        try:
            r=urllib.request.Request(cfg['sol_usd_source'],headers={'User-Agent':'Mozilla/5.0'})
            p=float(json.load(urllib.request.urlopen(r,timeout=10))['solana']['usd'])
            return p, f"live CoinGecko {dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).strftime('%F %T')} UTC+3"
        except Exception as e:
            return float(cfg['sol_usd_fallback']), f"FALLBACK config value ({cfg['sol_usd_fallback_note']}); live fetch failed: {type(e).__name__}"
    return float(cfg['sol_usd_fallback']), 'config fallback (live disabled)'
