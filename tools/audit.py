#!/usr/bin/env python3
"""Independent on-chain audit of a Sensōra agent database (v1 or v2 schema).

For every transaction the agent recorded, ask public indexers whether it exists and was mined.
The agent's own counters are not used, because in the v1 field deployment they over-reported
(41,883 "successful" stamps, of which only ~41% could be found on-chain).

Usage:
  python tools/audit.py <sensora_device.db> <out.csv> [--sample N] [--seed S]
"""
import argparse
import csv
import json
import random
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chain  # noqa: E402


def collect(db_path: str) -> list[dict]:
    """(kind, txid, ref, t0) rows. t0 is when the agent created the record (latency baseline)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = []
    if "tx_log" in tables:
        for r in conn.execute("SELECT txid, kind, ref, created_at, status, attempts FROM tx_log"):
            # Definite broadcast rejections are their own kind so they don't dilute per-tx inclusion rates.
            kind = r["kind"] + ("_rejected" if r["status"] == "rejected" else "")
            rows.append({"kind": kind, "txid": r["txid"], "ref": r["ref"], "t0": r["created_at"],
                         "agent_status": r["status"], "attempts": r["attempts"]})
    else:
        for r in conn.execute("SELECT reading_id, timestamp, onchain_proof_txid FROM sensor_readings"):
            rows.append({"kind": "proof", "txid": r["onchain_proof_txid"], "ref": r["reading_id"],
                         "t0": r["timestamp"], "agent_status": "success", "attempts": 1})
    for r in conn.execute("SELECT DISTINCT consumer_payment_txid FROM sensor_readings WHERE consumer_payment_txid IS NOT NULL"):
        rows.append({"kind": "pay_single", "txid": r[0], "ref": None, "t0": None, "agent_status": "verified", "attempts": None})
    for r in conn.execute("SELECT batch_id, consumer_payment_txid FROM batch_quotes WHERE is_claimed = 1"):
        rows.append({"kind": "pay_batch", "txid": r[1], "ref": r[0], "t0": None, "agent_status": "verified", "attempts": None})
    readings = [dict(r) for r in conn.execute("SELECT reading_id, timestamp FROM sensor_readings")]
    return [r for r in rows if r["txid"] and not r["txid"].startswith("unstamped:")], readings


def check(row: dict) -> dict:
    """Looks a tx up with retries. 'error' rows could not be resolved and are excluded from rates."""
    for attempt in range(5):
        try:
            st = chain.lookup(row["txid"])
            latency = st["block_time"] - row["t0"] if st.get("block_time") and row["t0"] else None
            return {**row, "seen": st["seen"], "mined": bool(st["block_height"]), "block_height": st["block_height"],
                    "block_time": st["block_time"], "latency_s": latency, "error": None}
        except Exception as e:  # IndexerError, or anything unexpected: never let one row kill the audit
            last = f"{type(e).__name__}: {e}"
            time.sleep(3 * 2 ** attempt)
    return {**row, "seen": None, "mined": None, "block_height": None, "block_time": None, "latency_s": None,
            "error": last[:200]}


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))] if values else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("out_csv")
    ap.add_argument("--sample", type=int, default=0, help="audit a random sample per kind (0 = all)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cutoff", type=int, default=0,
                    help="ignore records created after this unix time (e.g. run end minus 3 h, so late proofs can mine)")
    ap.add_argument("--since", type=int, default=0, help="ignore records created before this unix time")
    args = ap.parse_args()

    rows, readings = collect(args.db)
    in_window = lambda t: t is None or ((not args.since or t >= args.since) and (not args.cutoff or t <= args.cutoff))
    rows = [r for r in rows if in_window(r["t0"])]
    readings = [r for r in readings if in_window(r["timestamp"])]
    if args.sample:
        rng = random.Random(args.seed)
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        rows = [r for group in by_kind.values() for r in (rng.sample(group, args.sample) if len(group) > args.sample else group)]

    fields = ["kind", "txid", "ref", "t0", "agent_status", "attempts", "seen", "mined", "block_height", "block_time",
              "latency_s", "error"]
    results = []
    with open(args.out_csv, "w", newline="") as f, ThreadPoolExecutor(args.workers) as pool:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for result in pool.map(check, rows):  # rows are written as they complete
            w.writerow(result)
            f.flush()
            results.append(result)

    summary = {"by_kind": {}, "agent_vs_chain": {}, "per_reading": None}
    for kind in sorted({r["kind"] for r in results}):
        group = [r for r in results if r["kind"] == kind]
        resolved = [r for r in group if r["error"] is None]
        lat = [r["latency_s"] for r in resolved if r["latency_s"] is not None]
        summary["by_kind"][kind] = {
            "n": len(group),
            "unresolved_errors": len(group) - len(resolved),
            "mined": sum(r["mined"] for r in resolved),
            "seen_unmined": sum(r["seen"] and not r["mined"] for r in resolved),
            "missing": sum(not r["seen"] for r in resolved),
            "mined_pct_of_resolved": round(100 * sum(r["mined"] for r in resolved) / len(resolved), 1) if resolved else None,
            "latency_s": {"p10": pct(lat, 10), "p50": pct(lat, 50), "p90": pct(lat, 90),
                          "max": max(lat) if lat else None,
                          "mean": round(statistics.mean(lat)) if lat else None},
        }
        # What the agent believed versus what the chain shows.
        matrix = {}
        for r in resolved:
            chain_state = "mined" if r["mined"] else ("seen" if r["seen"] else "missing")
            key = f"{r['agent_status']} -> {chain_state}"
            matrix[key] = matrix.get(key, 0) + 1
        summary["agent_vs_chain"][kind] = matrix

    if not args.sample:
        # Per reading: did at least one of its proof transactions get mined? (v1: one tx per reading.)
        mined_refs = {str(r["ref"]) for r in results if r["kind"] == "proof" and r["mined"]}
        errored_refs = {str(r["ref"]) for r in results if r["kind"] == "proof" and r["error"]} - mined_refs
        # Readings that never got an accepted broadcast still count (they are failures), as long as they are
        # inside the time window.
        total = [str(r["reading_id"]) for r in readings if str(r["reading_id"]) not in errored_refs]
        summary["per_reading"] = {"readings": len(readings), "unresolved": len(errored_refs),
                                  "with_mined_proof": len(mined_refs),
                                  "pct": round(100 * len(mined_refs) / len(total), 1) if total else None}

    print(json.dumps(summary, indent=2))
    Path(args.out_csv).with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
