import sqlite3, csv, statistics as st, sys
SINCE, C24, C48, CFULL = 1793432850, 1793519250, 1793605650, 1793802722
def ro(p):
    c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row; return c
def pct(v,p):
    v=sorted(v); return v[min(len(v)-1,int(p/100*len(v)))] if v else None
def gaps(ts, cut):
    ts=sorted(t for t in ts if SINCE<=t<=cut)
    g=[b-a for a,b in zip(ts,ts[1:])]
    big=[x for x in g if x>400]
    return len(ts), (min(g),st.median(g),max(g)) if g else None, len(big), sum(round(x/300)-1 for x in big)
for name,db,cut in [("baseline","/d/baseline.db",C24),("v2 24h","/d/v2_final.db",C24),("v2 48h","/d/v2_final.db",C48),("v2 full","/d/v2_final.db",CFULL)]:
    c=ro(db); ts=[r[0] for r in c.execute("select timestamp from sensor_readings")]
    n,g,nbig,missing=gaps(ts,cut)
    print(f"{name}: readings={n} gap(min,med,max)={g} gaps>400s={nbig} est.missing slots={missing}")
# v2: per-reading latency from reading timestamp to FIRST mined proof, vs audit's tx-created latency
c=ro("/d/v2_final.db")
rts={str(r[0]):r[1] for r in c.execute("select reading_id,timestamp from sensor_readings")}
for label,f,cut in [("24h","/d/audit24_v2_at_24h_audit.csv",C24),("48h","/d/audit48_v2.csv",C48),("full","/d/auditfull_v2.csv",CFULL)]:
    rows=list(csv.DictReader(open(f)))
    first={}
    for r in rows:
        if r["kind"] in("proof","proof_rejected") and r["mined"]=="True" and r["block_time"]:
            first[r["ref"]]=min(first.get(r["ref"],1e12),int(r["block_time"]))
    inwin={k for k,t in rts.items() if SINCE<=t<=cut}
    mined_in=set(first)&inwin
    lat=[(first[k]-rts[k])/60 for k in mined_in]
    restamped={r["ref"] for r in rows if r["kind"]=="proof_rejected"} | {r["ref"] for r in rows if r["agent_status"]=="dropped"}
    lat_r=[(first[k]-rts[k])/60 for k in mined_in & restamped]
    extra=set(first)-inwin
    print(f"v2 {label}: readings in window={len(inwin)} mined (intersect)={len(mined_in)} mined refs outside window={len(extra)}")
    print(f"   reading->first mined proof latency min p10/p50/p90/max = {[round(pct(lat,p),1) for p in (10,50,90)]} / {round(max(lat),1)}")
    if lat_r: print(f"   restamped readings n={len(lat_r)} latency p50={round(st.median(lat_r),1)} max={round(max(lat_r),1)} min")
# baseline: same per-reading latency (t0 = reading ts already)
rows=list(csv.DictReader(open("/d/audit24_baseline.csv")))
lat=[(int(r["block_time"])-int(r["t0"]))/60 for r in rows if r["mined"]=="True"]
print("baseline 24h latency p10/p50/p90/max", [round(pct(lat,p),1) for p in (10,50,90)], round(max(lat),1))
