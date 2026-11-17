"""Re-check the field deployment on chain with fresh random samples (seed fixed, reproducible).
Proofs: 30 per calendar month (all if fewer). Batch payments: all 171. Single payments: 200 random.
Each txid is looked up in JungleBus; if not found there, in Bitails. Output: fieldcheck.csv + summary."""
from pathlib import Path
import json, random, time, csv, sys, datetime as dt, urllib.request, urllib.error, statistics as st
from concurrent.futures import ThreadPoolExecutor

D = json.load(open(Path(__file__).with_name('dbtxids.json')))
rnd = random.Random(20261116)
bymonth = {}
for ts, txid in D['proofs']:
    bymonth.setdefault(dt.datetime.fromtimestamp(ts, dt.UTC).strftime('%Y-%m'), []).append((ts, txid))
sample = []
for m in sorted(bymonth):
    for ts, txid in rnd.sample(bymonth[m], min(30, len(bymonth[m]))): sample.append(('proof', m, ts, txid))
for b in D['batchpays']: sample.append(('batchpay', '', None, b[0]))
for p in rnd.sample(D['pays'], 200): sample.append(('singlepay', '', None, p if isinstance(p, str) else p[0]))

def fetch(url):
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl'}), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(2 + 3 * i)
        except Exception:
            time.sleep(2 + 3 * i)
    raise RuntimeError('lookup failed ' + url)

def check(item):
    kind, m, ts, txid = item
    try:
        j = fetch(f'https://junglebus.gorillapool.io/v1/transaction/get/{txid}')
        if j and j.get('block_height'):
            return item + ('mined', j['block_height'], j.get('block_time'), 'junglebus')
        b = fetch(f'https://api.bitails.io/tx/{txid}')
        if b and b.get('blockheight'):
            return item + ('mined', b['blockheight'], b.get('blocktime'), 'bitails')
        if b or j: return item + ('seen_unmined', None, None, 'bitails' if b else 'junglebus')
        return item + ('missing', None, None, '')
    except Exception as e:
        return item + ('error', None, None, str(e)[:60])

with ThreadPoolExecutor(3) as ex: res = list(ex.map(check, sample))
with open('fieldcheck.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['kind', 'month', 'reading_ts', 'txid', 'status', 'height', 'block_time', 'source']); w.writerows(res)

def summ(rows):
    c = {}
    for r in rows: c[r[4]] = c.get(r[4], 0) + 1
    return c
P = [r for r in res if r[0] == 'proof']
print('PROOFS all', len(P), summ(P))
for m in sorted(bymonth):
    rows = [r for r in P if r[1] == m]; mined = sum(r[4] == 'mined' for r in rows)
    print(f'  {m}: {mined}/{len(rows)} mined  (month has {len(bymonth[m])} readings)')
# weighted estimate over all 41,883 proofs
est = sum(len(bymonth[m]) * sum(r[4] == 'mined' for r in P if r[1] == m) / max(1, len([r for r in P if r[1] == m])) for m in bymonth)
print(f'  weighted share mined: {est / len(D["proofs"]) * 100:.1f}%')
mj = [r for r in P if r[1] in ('2026-05', '2026-06', '2026-07')]; print('  May-Jul 2026:', sum(r[4] == 'mined' for r in mj), '/', len(mj))
lat = sorted((r[6] - r[2]) for r in P if r[4] == 'mined' and r[6])
if lat:
    q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]
    print(f'  latency n={len(lat)} p10 {q(.1)/60:.1f} min, p50 {st.median(lat)/60:.1f} min, p90 {q(.9)/3600:.2f} h, max {lat[-1]/3600:.2f} h, negative {sum(x < 0 for x in lat)}')
for k in ('batchpay', 'singlepay'):
    rows = [r for r in res if r[0] == k]; print(k.upper(), len(rows), summ(rows))
print('sources', {s: sum(r[7] == s for r in res) for s in ('junglebus', 'bitails')})
