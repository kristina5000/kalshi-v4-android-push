"""Kalshi V4.1 public market scanner. Signals only; never submits orders."""
import datetime as dt
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

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
MAX_MARKETS = max(1, int(os.getenv('MAX_MARKETS_PER_SCAN', '2000')))
SERIES_TICKER = os.getenv('SERIES_TICKER', '').strip()
# Restrict the API result set to markets approaching their close time.
DB = os.getenv('DB_PATH', '/tmp/kalshi_alerts.sqlite3')
SEND_TEST = os.getenv('SEND_STARTUP_TEST', 'true').lower() == 'true'
LOG = logging.getLogger('kalshi-v4.1')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'kalshi-v4.1-watch/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def push(title, body):
    if not TOPIC:
        raise RuntimeError('NTFY_TOPIC must be configured')
    url = NTFY_BASE + '/' + urllib.parse.quote(TOPIC, safe='')
    req = urllib.request.Request(url, data=body.encode('utf-8'), headers={'Title': title.encode('ascii', 'ignore').decode('ascii'), 'Content-Type': 'text/plain; charset=utf-8'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def database():
    conn = sqlite3.connect(DB)
    conn.execute('CREATE TABLE IF NOT EXISTS alerts (ticker TEXT PRIMARY KEY, sent_at INTEGER NOT NULL)')
    conn.commit()
    return conn


def parse_close(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def cents(m, key):
    value = m.get(key)
    if value is not None:
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    # Kalshi fixed-point dollar fields are strings; convert to cents.
    value = m.get(key + '_dollars')
    if value is not None:
        try:
            return round(float(value) * 100)
        except (ValueError, TypeError):
            return None
    return None


def volume(m):
    for key in ('volume', 'volume_fp'):
        try:
            if m.get(key) is not None:
                return float(m[key])
        except (ValueError, TypeError):
            pass
    return 0.0


def evaluate(m, now):
    ticker = str(m.get('ticker', ''))
    if not ticker:
        return 'missing_ticker', None
    if m.get('status') != 'active':
        return 'inactive', None
    close = parse_close(m.get('close_time') or m.get('expiration_time'))
    if close is None:
        return 'missing_close_time', None
    remaining = close - now
    if not MIN_REMAIN <= remaining <= MAX_REMAIN:
        return 'outside_time_window', None
    if volume(m) < MIN_VOLUME:
        return 'low_volume', None
    yes_bid, yes_ask = cents(m, 'yes_bid'), cents(m, 'yes_ask')
    no_bid, no_ask = cents(m, 'no_bid'), cents(m, 'no_ask')
    choices = []
    for side, bid, ask in (('YES', yes_bid, yes_ask), ('NO', no_bid, no_ask)):
        if bid is None or ask is None or not (0 < bid <= ask < 100):
            continue
        spread = ask - bid
        if spread <= MAX_SPREAD:
            choices.append((spread, side, bid, ask))
    if not choices:
        return 'missing_quotes_or_wide_spread', None
    spread, side, bid, ask = min(choices)
    return 'eligible', {'ticker': ticker, 'title': str(m.get('title', ticker)), 'side': side, 'bid': bid, 'ask': ask, 'spread': spread, 'remaining': int(remaining), 'volume': volume(m)}


def fetch_markets(now=None):
    if now is None:
        now = time.time()
    markets = []
    cursor = ''
    seen = set()
    while len(markets) < MAX_MARKETS:
        query = {
            'limit': min(1000, MAX_MARKETS - len(markets)),
            'status': 'open',
            'min_close_ts': max(0, int(now + MIN_REMAIN)),
            'max_close_ts': int(now + MAX_REMAIN),
        }
        if SERIES_TICKER:
            query['series_ticker'] = SERIES_TICKER
        if cursor:
            query['cursor'] = cursor
        payload = get_json(API + '/markets?' + urllib.parse.urlencode(query))
        batch = payload.get('markets', [])
        if not isinstance(batch, list) or not batch:
            break
        markets.extend(batch)
        next_cursor = payload.get('cursor') or ''
        if not next_cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    return markets


def main():
    if not TOPIC:
        raise RuntimeError('NTFY_TOPIC must be configured')
    conn = database()
    LOG.info('Kalshi V4.2 started; signals-only; no trading; suggested cap $%.2f', MAX_TRADE)
    if SEND_TEST:
        try:
            push('Kalshi V4.1 connected', 'Startup test: your hosted scanner is running. No trade was placed.')
            LOG.info('Startup ntfy test sent')
        except Exception:
            LOG.exception('Startup ntfy test failed')
    while True:
        started = time.time()
        try:
            markets = fetch_markets(started)
            counts = Counter()
            candidates = []
            for market in markets:
                reason, candidate = evaluate(market, started)
                counts[reason] += 1
                if candidate:
                    candidates.append(candidate)
            LOG.info('Discovery window=%ds..%ds series=%s; returned markets=%d', MIN_REMAIN, MAX_REMAIN, SERIES_TICKER or 'ALL', len(markets))
            LOG.info('Scanned=%d eligible=%d rejection_reasons=%s', len(markets), len(candidates), json.dumps(dict(counts), sort_keys=True))
            # Closing soon does not prove that a market is a 15-minute contract.
            # Set SERIES_TICKER to a verified 15-minute series before treating results as such.
            # Eligibility is NOT a statistical trading edge; alerts are market watch candidates only.
            sent = 0
            hour_ago = int(started) - 3600
            conn.execute('DELETE FROM alerts WHERE sent_at < ?', (hour_ago,))
            conn.commit()
            quota = max(0, MAX_ALERTS - conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0])
            for candidate in sorted(candidates, key=lambda x: (x['spread'], -x['volume'])):
                if sent >= quota:
                    break
                ticker = candidate['ticker']
                if conn.execute('SELECT 1 FROM alerts WHERE ticker=?', (ticker,)).fetchone():
                    continue
                message = (f"{candidate['title']}\nTicker: {ticker}\nWatch side: {candidate['side']} "
                           f"bid {candidate['bid']}c / ask {candidate['ask']}c; spread {candidate['spread']}c\n"
                           f"Volume: {candidate['volume']:g}; time left: {candidate['remaining']}s\n"
                           f"Market watch only, NOT a predicted edge. Suggested cap ${MAX_TRADE:.2f}; no trade placed.")
                try:
                    push('Kalshi market watch', message)
                    conn.execute('INSERT INTO alerts(ticker, sent_at) VALUES (?, ?)', (ticker, int(time.time())))
                    conn.commit()
                    sent += 1
                except Exception:
                    LOG.exception('Failed to notify ticker=%s', ticker)
            LOG.info('notifications=%d', sent)
            if len(markets) >= MAX_MARKETS:
                LOG.info('Scan reached MAX_MARKETS_PER_SCAN=%d; this is a sample, not all open markets', MAX_MARKETS)
        except Exception:
            LOG.exception('Scan failed; will retry')
        time.sleep(max(1, POLL - (time.time() - started)))


if __name__ == '__main__':
    main()
