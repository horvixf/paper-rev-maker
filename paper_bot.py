"""
LIVE PAPER ENGINE v3 - REV-MAKER v2 spec, full money cycle, dry run. NO orders.
Adds over v2: virtual bankroll ($100 default), compounding half-Kelly stakes,
pair-splitting when both assets fire the same window, PARTIAL fills limited by
actually-printed volume, per-trade P&L records, equity/ROI/drawdown in
results.json, -20% session-drawdown halt failsafe.
Usage: python3 paper_bot.py [minutes] [start_bankroll]
"""
import requests, time, json, math, sys, os, traceback
from statistics import stdev
from math import erf

ASSETS = {
    'BTC': {'inst': 'BTC-USDT', 'slug': 'btc-updown-5m', 'thr': 0.00117},
    'ETH': {'inst': 'ETH-USDT', 'slug': 'eth-updown-5m', 'thr': 0.00146},
}
BID = 0.495                 # maker bid price; win pays 1.0 per share, no fee (maker)
PAYOUT_B = (1 - BID) / BID  # net odds per $ staked
KELLY_F = 0.0486            # half-Kelly at planning win rate 0.55
STAKE_CAP_FRAC = 0.08       # never more than 8% of bankroll on one window
CANCEL_AT = 0.50
REPOST_AT = 0.52
BID_WINDOW = 90
HALT_DD = 0.20              # failsafe: stop arming if session drawdown exceeds 20%
LOG_PATH = 'paper_log.jsonl'
SUM_PATH = 'results.json'

S = requests.Session()
S.headers['User-Agent'] = 'paper-research-bot/3.0'

def req(url, params=None, tries=4, timeout=7):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            time.sleep(0.5 + i)
        except Exception:
            time.sleep(0.5 + i)
    return None

def log_event(rec):
    rec['logged_at'] = int(time.time())
    with open(LOG_PATH, 'a') as f:
        f.write(json.dumps(rec) + '\n')

def phi(z):
    return 0.5 * (1 + erf(z / math.sqrt(2)))

class Book:
    """virtual bankroll + trade ledger (carries over from previous results.json)"""
    def __init__(self, start):
        self.carried = {'armed': 0, 'filled': 0, 'closed': 0, 'wins': 0, 'origin': start}
        if os.path.exists(SUM_PATH):
            try:
                prev = json.load(open(SUM_PATH))
                pb = float(prev.get('bankroll', start))
                if pb > 1:
                    start = pb
                self.carried = {
                    'armed': int(prev.get('cum_armed', prev.get('armed_windows', 0)) or 0),
                    'filled': int(prev.get('cum_filled', prev.get('filled_windows', 0)) or 0),
                    'closed': int(prev.get('cum_closed', prev.get('closed_trades', 0)) or 0),
                    'wins': int(prev.get('cum_wins', prev.get('wins', 0)) or 0),
                    'origin': float(prev.get('origin', prev.get('start', start)) or start),
                }
                print(f'carryover: bankroll ${start:.2f}, cumulative fills {self.carried["filled"]}', flush=True)
            except Exception:
                pass
        self.start = start
        self.bank = start
        self.peak = max(start, self.carried.get('origin', start))
        self.max_dd = 0.0
        self.trades = []      # closed trades
        self.open_risk = {}   # (asset, ts) -> staked $
        self.halted = False

    def reserve(self, key, stake):
        stake = round(min(stake, self.available()), 2)
        if stake < 0.5:
            return 0.0
        self.open_risk[key] = stake
        return stake

    def available(self):
        return max(self.bank - sum(self.open_risk.values()), 0.0)

    def settle(self, key, asset, ts, side, stake, filled_frac, win):
        self.open_risk.pop(key, None)
        eff = round(stake * filled_frac, 2)
        if eff <= 0:
            return None
        pnl = round(eff * PAYOUT_B, 2) if win else -eff
        self.bank = round(self.bank + pnl, 2)
        self.peak = max(self.peak, self.bank)
        self.max_dd = max(self.max_dd, (self.peak - self.bank) / self.peak)
        t = {'asset': asset, 'ts': ts, 'side': side, 'stake': eff,
             'filled_frac': round(filled_frac, 2), 'win': win, 'pnl': pnl,
             'bank_after': self.bank}
        self.trades.append(t)
        if (self.peak - self.bank) / self.peak >= HALT_DD:
            self.halted = True
        return t

class Summary:
    def __init__(self, book):
        self.book = book
        self.windows = {}
        self.flush()

    def flush(self):
        w = list(self.windows.values())
        armed = len(w)
        filled = [x for x in w if x.get('filled_frac', 0) > 0]
        closed = self.book.trades
        wins = sum(1 for t in closed if t['win'])
        b = self.book
        c = b.carried
        out = {
            'updated_utc': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'bankroll': b.bank, 'start': b.start, 'origin': c['origin'],
            'roi_session_pct': round(100 * (b.bank / b.start - 1), 2),
            'roi_total_pct': round(100 * (b.bank / c['origin'] - 1), 2),
            'cum_armed': c['armed'] + armed, 'cum_filled': c['filled'] + len(filled),
            'cum_closed': c['closed'] + len(closed), 'cum_wins': c['wins'] + wins,
            'cum_win_given_fill_pct': round(100 * (c['wins'] + wins) / max(c['closed'] + len(closed), 1), 1),
            'gate_A_progress': f"{c['closed'] + len(closed)}/100 fills",
            'max_drawdown_pct': round(100 * b.max_dd, 2),
            'halted': b.halted,
            'armed_windows': armed,
            'filled_windows': len(filled),
            'fill_rate_pct': round(100 * len(filled) / armed, 1) if armed else None,
            'closed_trades': len(closed),
            'wins': wins,
            'win_given_fill_pct': round(100 * wins / len(closed), 1) if closed else None,
            'benchmark': 'proxy 61.2% win-given-fill; gate >= 53%; planning 55%',
            'trades': closed[-40:],
            'windows': sorted(w, key=lambda x: x['ts'])[-40:],
        }
        tmp = SUM_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(out, f, indent=1)
        os.replace(tmp, SUM_PATH)

    def upsert(self, key, **kw):
        self.windows.setdefault(key, {}).update(kw)
        self.flush()

class WindowState:
    def __init__(self, asset, cfg, w_ts, sm, book):
        self.a = asset; self.cfg = cfg; self.ts = w_ts
        self.sm = sm; self.book = book
        self.key = (asset, w_ts)
        self.armed = False; self.done_arming = False; self.closed = False
        self.bid_live = False
        self.stake = 0.0
        self.fill_notional = 0.0   # $ of qualifying prints accumulated
        self.last_trade_seen = 0

    def filled_frac(self):
        if self.stake <= 0:
            return 0.0
        return min(1.0, self.fill_notional / self.stake)

    def try_arm(self, other_states):
        if self.done_arming or self.book.halted:
            self.done_arming = self.done_arming or self.book.halted
            return
        dt = int(time.time()) - self.ts
        if dt > 60:
            self.done_arming = True
            return
        c = okx_candles(self.cfg['inst'], 12)
        if not c:
            return
        cm = {x[0]: x for x in c}
        t_open, t_last = self.ts - 300, self.ts - 60
        if t_open not in cm or t_last not in cm:
            return
        o, cl = cm[t_open][1], cm[t_last][2]
        if cl == o:
            self.done_arming = True
            return
        pmag = abs(math.log(cl / o))
        if pmag < self.cfg['thr']:
            self.done_arming = True
            return
        c30 = okx_candles(self.cfg['inst'], 35)
        if not c30:
            return
        rets = [math.log(x[2] / x[1]) for x in c30 if x[1] > 0][:30]
        if len(rets) < 20:
            self.done_arming = True
            return
        ev = req('https://gamma-api.polymarket.com/events',
                 {'slug': f"{self.cfg['slug']}-{self.ts}"})
        if ev is None:
            return
        if not ev:
            self.done_arming = True
            return
        try:
            m = ev[0]['markets'][0]
            self.cid = m['conditionId']
        except Exception:
            self.done_arming = True
            return
        w_open = okx_last(self.cfg['inst'])
        if w_open is None:
            return
        # sizing: half-Kelly of CURRENT bankroll, capped, split across
        # simultaneously armed assets this window
        others = [s for s in other_states
                  if s is not self and s.ts == self.ts and s.armed and not s.closed]
        base = min(KELLY_F, STAKE_CAP_FRAC) * self.book.bank
        stake = base / (len(others) + 1)
        for s in others:
            # rebalance earlier leg down to the split share (only unfilled portion shrinks)
            new_stake = round(s.stake / (len(others) + 1) * len(others) + 0.0, 2)
            new_stake = round(base / (len(others) + 1), 2)
            if s.fill_notional < new_stake:
                self.book.open_risk[s.key] = new_stake
                s.stake = new_stake
                self.sm.upsert(s.key, stake=new_stake)
        stake = self.book.reserve(self.key, stake)
        if stake <= 0:
            self.done_arming = True
            return
        self.rev_up = 1 if cl < o else 0
        self.sigma = stdev(rets)
        self.w_open = w_open
        self.pmag = pmag
        self.stake = stake
        self.armed = True
        self.bid_live = True
        self.done_arming = True
        side = 'Up' if self.rev_up else 'Down'
        self.sm.upsert(self.key, asset=self.a, ts=self.ts, side=side,
                       pmag_pct=round(pmag * 100, 3), stake=stake, filled_frac=0.0, win=None)
        log_event({'type': 'armed', 'asset': self.a, 'ts': self.ts, 'side': side,
                   'pmag': pmag, 'stake': stake, 'bank': self.book.bank})
        print(f"[{self.a} {self.ts}] ARMED {side} stake=${stake:.2f} pmag={pmag*100:.3f}%", flush=True)

    def tick(self):
        if not self.armed or self.closed:
            return
        dt = int(time.time()) - self.ts
        if dt > BID_WINDOW:
            self.bid_live = False
            return
        px = okx_last(self.cfg['inst'])
        if px is None or self.sigma <= 0:
            return
        tau = (300 - dt) / 60.0
        z = math.log(px / self.w_open) / (self.sigma * math.sqrt(tau))
        pR = phi(z) if self.rev_up else 1 - phi(z)
        if self.bid_live and pR < CANCEL_AT:
            self.bid_live = False
            log_event({'type': 'cancel', 'asset': self.a, 'ts': self.ts, 't': dt,
                       'pR': round(pR, 4), 'filled_frac': round(self.filled_frac(), 2)})
            print(f"[{self.a} {self.ts}] CANCEL t+{dt}s pR={pR:.3f} filled={self.filled_frac():.0%}", flush=True)
        elif (not self.bid_live) and self.filled_frac() < 1 and pR >= REPOST_AT and dt <= 85:
            self.bid_live = True
            log_event({'type': 'repost', 'asset': self.a, 'ts': self.ts, 't': dt, 'pR': round(pR, 4)})
        if self.bid_live and self.filled_frac() < 1:
            tr = req('https://data-api.polymarket.com/trades',
                     {'market': self.cid, 'limit': 60}, tries=2, timeout=6)
            if isinstance(tr, list):
                want = 'Up' if self.rev_up else 'Down'
                newest = self.last_trade_seen
                for x in tr:
                    try:
                        tt = int(x['timestamp'])
                        newest = max(newest, tt)
                        if tt <= self.last_trade_seen or tt < self.ts or tt - self.ts > BID_WINDOW:
                            continue
                        p = float(x['price'])
                        pr = p if x.get('outcome') == want else 1 - p
                        if pr <= BID:
                            add = float(x.get('size', 0)) * BID
                            before = self.filled_frac()
                            self.fill_notional += add
                            after = self.filled_frac()
                            if after > before:
                                self.sm.upsert(self.key, filled_frac=round(after, 2))
                                log_event({'type': 'fill', 'asset': self.a, 'ts': self.ts,
                                           'fill_t': tt - self.ts, 'print_price': round(pr, 4),
                                           'print_notional': round(add, 2),
                                           'filled_frac': round(after, 2)})
                                if after >= 1 and before < 1:
                                    print(f"[{self.a} {self.ts}] FULLY FILLED t+{tt-self.ts}s", flush=True)
                    except Exception:
                        continue
                self.last_trade_seen = newest

def okx_candles(inst, n):
    d = req('https://www.okx.com/api/v5/market/candles',
            {'instId': inst, 'bar': '1m', 'limit': str(n)})
    if not d or d.get('code') != '0':
        return None
    try:
        return [(int(x[0]) // 1000, float(x[1]), float(x[4])) for x in d['data']]
    except Exception:
        return None

def okx_last(inst):
    d = req('https://www.okx.com/api/v5/market/ticker', {'instId': inst}, tries=3, timeout=5)
    try:
        return float(d['data'][0]['last'])
    except Exception:
        return None

def resolve(cfg, ts):
    ev = req('https://gamma-api.polymarket.com/events', {'slug': f"{cfg['slug']}-{ts}"})
    if not ev:
        return None
    try:
        pr = json.loads(ev[0]['markets'][0].get('outcomePrices', '["",""]'))
        if pr[0] in ('0', '1'):
            return 1 if pr[0] == '1' else 0
    except Exception:
        pass
    return None

def self_test():
    """math sanity checks; abort on failure"""
    assert abs(PAYOUT_B - 1.020202) < 1e-4
    b = Book(100.0)
    s = b.reserve(('X', 1), 4.86); assert 4.85 <= s <= 4.86
    t = b.settle(('X', 1), 'BTC', 1, 'Up', s, 1.0, True)
    assert abs(t['pnl'] - round(s * PAYOUT_B, 2)) < 0.01 and b.bank > 100
    s2 = b.reserve(('X', 2), 5.0)
    t2 = b.settle(('X', 2), 'BTC', 2, 'Up', s2, 0.5, False)
    assert abs(t2['pnl'] + s2 * 0.5) < 0.01
    assert abs(phi(0) - 0.5) < 1e-9 and phi(3) > 0.99
    print('self-test passed', flush=True)

def main(minutes, start_bank):
    self_test()
    book = Book(start_bank)
    sm = Summary(book)
    states = {}
    pending = []   # (asset, cfg, state, last_try)
    deadline = time.time() + minutes * 60
    last_beat = 0
    print(f'paper engine v3 | {minutes} min | virtual bankroll ${start_bank:.2f} | NO real orders', flush=True)
    while True:
        try:
            now = int(time.time())
            if now > deadline and not pending:
                break
            if now - last_beat >= 120:
                last_beat = now
                print(f'heartbeat {time.strftime("%H:%M:%S", time.gmtime())} bank=${book.bank:.2f} '
                      f'pending={len(pending)} halted={book.halted}', flush=True)
            w_ts = now - (now % 300)
            if now <= deadline:
                for a, cfg in ASSETS.items():
                    st = states.get(a)
                    if st is None or st.ts != w_ts:
                        if st is not None and st.armed and not st.closed:
                            st.closed = True
                            st.bid_live = False
                            pending.append((a, cfg, st, 0))
                        states[a] = WindowState(a, cfg, w_ts, sm, book)
                        st = states[a]
                    st.try_arm(list(states.values()))
                    st.tick()
            still = []
            for a, cfg, st, last_try in pending:
                if now - st.ts < 320 or now - last_try < 20:
                    still.append((a, cfg, st, last_try))
                    continue
                res = resolve(cfg, st.ts)
                if res is None:
                    if now - st.ts < 1800:
                        still.append((a, cfg, st, now))
                    else:
                        book.settle(st.key, a, st.ts, 'Up' if st.rev_up else 'Down',
                                    st.stake, 0.0, False)
                        sm.upsert(st.key, win='unresolved')
                    continue
                win = int(res == st.rev_up)
                frac = st.filled_frac()
                t = book.settle(st.key, a, st.ts, 'Up' if st.rev_up else 'Down',
                                st.stake, frac, bool(win)) if frac > 0 else \
                    book.settle(st.key, a, st.ts, 'Up' if st.rev_up else 'Down',
                                st.stake, 0.0, False)
                sm.upsert(st.key, win=win, filled_frac=round(frac, 2))
                log_event({'type': 'resolution', 'asset': a, 'ts': st.ts, 'win': win,
                           'filled_frac': round(frac, 2),
                           'pnl': t['pnl'] if t else 0.0, 'bank': book.bank})
                print(f"[{a} {st.ts}] RESOLVED win={win} filled={frac:.0%} bank=${book.bank:.2f}", flush=True)
            pending = still
            time.sleep(2.0)
        except KeyboardInterrupt:
            break
        except Exception:
            print('loop error (recovered):', traceback.format_exc()[-250:], flush=True)
            time.sleep(3)
    sm.flush()
    print(f'session complete. final bankroll ${book.bank:.2f} '
          f'(ROI {100*(book.bank/book.start-1):+.2f}%)', flush=True)

if __name__ == '__main__':
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 180
    bank = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    main(mins, bank)
