"""Kalshi 15-minute watch monitor; public data, ntfy push, no trading."""
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.elections.kalshi.com/trade-api/v2'
TOPIC = os.getenv('NTFY_TOPIC', '').strip()
NTFY_BASE = os.getenv('NTFY_BASE_URL', 'https://ntfy.sh').rstrip('/')
POLL = max(30, int(os.getenv('POLL_SECONDS', '60')))
MAX_TRADE = min(10.0, max(0.01, float(os.getenv('MAX_TRADE_USD', '10'))))
MAX_SPREAD = int(os.getenv('MAX_SPREAD_CENTS', '8'))
MIN_VOLUME = int(os.getenv('MIN_VOLUME', '20'))
MAX_ALERTS = int(os.getenv('MAX_ALERTS_PER_HOUR', '12'))
MIN_REMAIN = int(os.getenv('MIN_REMAIN_SECONDS', '120'))
MAX_REMAIN = int(os.getenv('MAX_REMAIN_SECONDS', '900'))
MAX_MARKETS = int(os.getenv('MAX_MARKETS_PER_SCAN', '2000'))
DB = os.getenv('DB_PATH', '/tmp/kalshi_alerts.sqlite3')
SEND_TEST = os.getenv('SEND_STARTUP_TEST', 'true').lower() == 'true'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def request_json(url):
    req = urllib.request.Request(url, headers={'Accept':'application/json', 'User-Agent':'Kalshi15mPushMonitor/4.0'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def send_push(title, body, priority='default'):
    if not TOPIC or not re.fullmatch(r'[A-Za-z0-9_-]{12,120}', TOPIC):
        raise ValueError('Set NTFY_TOPIC to a private, random 12+ character topic')
    url = NTFY_BASE + '/' + urllib.parse.quote(TOPIC, safe='')
    req = urllib.request.Request(url, data=body.encode('utf-8'), method='POST', headers={
        'Title':title.encode('ascii', errors='ignore').decode('ascii'),
        'Priority':priority,
        'Tags':'chart_with_upwards_trend',
        'Content-Type':'text/plain; charset=utf-8',
        'User-Agent':'Kalshi15mPushMonitor/4.0'
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status >= 300: raise RuntimeError(f'ntfy HTTP {r.status}')


def epoch(s):
    if not s: return None
    try: return dt.datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
    except (TypeError, ValueError): return None


def price(m, side):
    d = m.get(side+'_dollars')
    if d is not None:
        try: return round(float(d)*100)
        except (ValueError, TypeError): pass
    try: return int(m[side]) if m.get(side) is not None else None
    except (ValueError, TypeError): return None


def is_15m(m):
    # Recognize explicit 15-minute series or market duration. Not exhaustive.
    series = str(m.get('series_ticker') or m.get('ticker') or '').split('-')[0].upper()
    if re.search(r'15M(?:IN)?(?:$|[^A-Z])', series): return True
    opened = epoch(m.get('open_time') or m.get('open_date'))
    closes = epoch(m.get('close_time') or m.get('expiration_time'))
    return bool(opened and closes and 840 <= closes-opened <= 960)


def candidate(m, now):
    if m.get('status') not in ('active','open') or not is_15m(m): return None
    close = epoch(m.get('close_time') or m.get('expiration_time'))
    if close is None or not MIN_REMAIN <= close-now <= MAX_REMAIN: return None
    yb,ya,nb,na = (price(m,k) for k in ('yes_bid','yes_ask','no_bid','no_ask'))
    if ya is None and nb is not None: ya=100-nb
    if na is None and yb is not None: na=100-yb
    if yb is None and na is not None: yb=100-na
    if nb is None and ya is not None: nb=100-ya
    if any(v is None for v in (yb,ya,nb,na)): return None
    if not (0 <= yb <= ya <= 100 and 0 <= nb <= na <= 100): return None
    if ya-yb > MAX_SPREAD or na-nb > MAX_SPREAD: return None
    try: vol = int(m.get('volume') or 0)
    except (ValueError,TypeError): vol=0
    if vol < MIN_VOLUME: return None
    # Cheaper side is shown for monitoring only, not a recommendation.
    side,ask,bid = ('YES',ya,yb) if ya <= na else ('NO',na,nb)
    if not 1 <= ask <= 90: return None
    contracts = int(MAX_TRADE*100 // ask)
    if contracts < 1: return None
    return side,ask,ask-bid,vol,contracts,int(close-now)


def init_db():
    con=sqlite3.connect(DB)
    con.execute('CREATE TABLE IF NOT EXISTS alerts (ticker TEXT PRIMARY KEY, sent_at INTEGER NOT NULL)')
    con.commit()
    return con


def scan(con):
    now=time.time(); cursor=None; scanned=0; matched=0; sent=0
    recent=con.execute('SELECT COUNT(*) FROM alerts WHERE sent_at>?', (int(now-3600),)).fetchone()[0]
    while scanned < MAX_MARKETS:
        params={'status':'open','limit':str(min(200,MAX_MARKETS-scanned))}
        if cursor: params['cursor']=cursor
        data=request_json(API+'/markets?'+urllib.parse.urlencode(params))
        markets=data.get('markets') or []
        if not markets: break
        scanned += len(markets)
        for m in markets:
            c=candidate(m,now)
            if not c: continue
            matched+=1
            if recent+sent >= MAX_ALERTS: continue
            ticker=str(m.get('ticker') or '')
            if not ticker or con.execute('SELECT 1 FROM alerts WHERE ticker=?',(ticker,)).fetchone(): continue
            side,ask,spread,vol,qty,remaining=c
            title='Kalshi 15m MARKET WATCH'
            body=(f'{m.get("title","Market")}\nTicker: {ticker}\n'
                  f'Cheaper quoted side: {side} at {ask}c | spread {spread}c | volume {vol}\n'
                  f'Time remaining: {remaining//60}m {remaining%60}s\n'
                  f'Up to {qty} contracts; entry cost ${qty*ask/100:.2f} before fees (cap ${MAX_TRADE:.2f}).\n'
                  'WATCH ONLY: Not a win prediction or positive-edge signal. Check current quotes, fees and settlement rules.')
            send_push(title,body)
            con.execute('INSERT INTO alerts(ticker,sent_at) VALUES (?,?)',(ticker,int(now)))
            con.commit()
            sent+=1
        cursor=data.get('cursor')
        if not cursor: break
    logging.info('Scanned=%d eligible=%d notifications=%d',scanned,matched,sent)


def main():
    if not TOPIC: raise SystemExit('NTFY_TOPIC must be configured')
    con=init_db()
    logging.info('Kalshi V4 started; push-only; no trading; cap $%.2f',MAX_TRADE)
    if SEND_TEST:
        try: send_push('Kalshi V4 connected','Startup test: your hosted scanner is running. No trade was placed.')
        except Exception: logging.exception('Startup notification failed')
    while True:
        try: scan(con)
        except Exception: logging.exception('Scan failed; retrying')
        time.sleep(POLL)

if __name__=='__main__': main()
