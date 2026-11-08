#!/usr/bin/env python3
"""Scripted Sensōra buyer for the v2 experiment.

On a fixed schedule it buys the last WINDOW seconds of provable readings as a batch, then verifies
everything itself from public indexers:
  1. each delivered reading hashes to the value in its SENSORA_PROOF payload,
  2. that payload is in a mined transaction,
  3. the batch commitment equals sha256(data hashes), and the payment (seen by an indexer, not necessarily
     mined yet at verification time) carries exactly that memo and the quoted amount.
Payment inclusion is checked later by tools/audit.py.
Every round is one CSV row (timings and verification result).

Usage (inside the agent image, which has bsvlib):
  python tools/buyer.py --agent http://127.0.0.1:8086 --wif-file /run/buyer.wif --out buyer.csv
"""
import argparse
import csv
import hashlib
import os
import sys
import time
from pathlib import Path

import requests
from bsvlib import PrivateKey, Transaction, TxOutput, Unspent
from bsvlib.script import Script
from bsvlib.script.type import P2pkhScriptType

sys.path.insert(0, str(Path(__file__).parent))
import chain  # noqa: E402

FIELDS = ["round", "t_start", "batch_id", "num_readings", "price_sats", "pay_txid", "t_quote", "t_paid",
          "t_claimed", "t_fetched", "claim_attempts", "hash_ok", "proofs_mined", "commitment_ok",
          "pay_seen", "pay_ok", "readings_in_window", "verified", "error"]


class Wallet:
    """Spends its own change immediately instead of waiting for the indexer (same fix as the agent)."""

    def __init__(self, wif: str, fee_rate: float):
        self.key = PrivateKey(wif)
        self.address = self.key.address()
        self.script_hex = self.key.public_key().locking_script().hex()
        self.fee_rate = fee_rate
        self.spent: set[tuple[str, int]] = set()
        self.change: dict[tuple[str, int], Unspent] = {}

    def _unspent(self, txid, vout, sats) -> Unspent:
        return Unspent(txid=txid, vout=vout, satoshi=sats, script_pubkey_hex=self.script_hex,
                       script_type=P2pkhScriptType(), private_keys=[self.key])

    def utxos(self) -> list[Unspent]:
        data = requests.get(f"https://api.bitails.io/address/{self.address}/unspent", timeout=15).json()
        pool = {(u["txid"], int(u["vout"])): self._unspent(u["txid"], int(u["vout"]), int(u["satoshis"]))
                for u in data.get("unspent", [])}
        pool.update({k: v for k, v in self.change.items() if k not in pool})
        return [u for k, u in pool.items() if k not in self.spent]

    def pay(self, to_address: str, sats: int, op_return: bytes) -> str:
        self.last_txid = None
        script = b"\x00\x6a" + (bytes([len(op_return)]) if len(op_return) <= 75 else b"\x4c" + bytes([len(op_return)])) + op_return
        outputs = [TxOutput(out=to_address, satoshi=sats), TxOutput(out=Script(script), satoshi=0)]
        selected, total = [], 0
        for u in sorted(self.utxos(), key=lambda u: u.satoshi):
            selected.append(u)
            total += u.satoshi
            if total >= sats + 200:
                break
        if total < sats + 200:
            raise RuntimeError(f"buyer wallet {self.address} has insufficient funds ({total} sats)")
        tx = Transaction(tx_outputs=outputs, fee_rate=self.fee_rate)
        tx.add_inputs(selected)
        tx.add_change(change_address=self.address)
        tx.sign()
        txid = chain.txid_of(bytes.fromhex(tx.raw()))
        self.last_txid = txid  # known before broadcasting, so an unknown outcome is still recorded
        try:
            r = requests.post("https://api.bitails.io/tx/broadcast", json={"raw": tx.raw()}, timeout=15)
            accepted = 200 <= r.status_code < 300 and r.json().get("txid") == txid
        except (requests.RequestException, ValueError):
            r, accepted = None, False
        if not accepted:
            # Never reuse these coins: if the payment did go through, they are spent; if a coin was our own
            # phantom change, it never existed.
            self.spent.update((u.txid, u.vout) for u in selected)
            body = f"HTTP {r.status_code}: {r.text[:300]}" if r is not None else "no response"
            raise RuntimeError(f"payment broadcast not accepted ({body})")
        self.spent.update((u.txid, u.vout) for u in selected)
        for vout, out in enumerate(tx.tx_outputs):
            if out.satoshi > 0 and out.locking_script.hex() == self.script_hex:
                self.change[(txid, vout)] = self._unspent(txid, vout, out.satoshi)
        return txid


def reading_hash(r: dict) -> bytes:
    values = [r["value_temp"], r["value_humid"]]
    if None in values:
        raise ValueError("buyer only supports temp_humid readings")
    return hashlib.sha256((f"{r['timestamp']}:" + ":".join(f"{v:.1f}" for v in values)).encode()).digest()


def verify(readings: list[dict], commitment: str) -> dict:
    hashes, hash_ok, mined = [], True, 0
    for r in readings:
        payload = bytes.fromhex(r["op_return_payload_hex"])
        hash_ok &= payload.startswith(b"SENSORA_PROOF")
        h = reading_hash(r)
        hashes.append(payload[-32:])
        hash_ok &= h == payload[-32:]
        s = chain.lookup(r["onchain_proof_txid"])
        on_chain = s["raw"] and any(data == payload for _, _, data in chain.parse_tx(s["raw"])["outputs"])
        mined += bool(on_chain and s["block_height"])
    commitment_ok = hashlib.sha256(b"".join(hashes)).hexdigest() == commitment
    return {"hash_ok": hash_ok, "proofs_mined": mined, "commitment_ok": commitment_ok}


def run_round(row: dict, args, wallet: Wallet) -> dict:
    """Fills `row` in place, so a failure after payment still leaves the payment recorded."""
    row.update(t_start=int(time.time()), claim_attempts=0)
    now = int(time.time())
    q = requests.post(f"{args.agent}/batch/price", json={"start_timestamp": now - args.window, "end_timestamp": now}, timeout=15)
    if q.status_code != 200:
        row["error"] = f"quote HTTP {q.status_code}: {q.text[:200]}"
        return row
    quote = q.json()
    row.update(batch_id=quote["batch_id"], num_readings=quote["num_readings"], price_sats=quote["total_price_sats"],
               t_quote=int(time.time()))

    memo = f"SENSORA_PAY:{quote['batch_id']}:{quote['commitment']}".encode()
    try:
        row["pay_txid"] = wallet.pay(quote["payment_address"], quote["total_price_sats"], memo)
    finally:
        row.setdefault("pay_txid", getattr(wallet, "last_txid", None))
    row["t_paid"] = int(time.time())

    token = None
    while row["claim_attempts"] < args.claim_attempts:
        row["claim_attempts"] += 1
        time.sleep(args.claim_wait)
        signature = wallet.key.sign_text(f"SENSORA_CLAIM:{quote['batch_id']}:{row['pay_txid']}")[1]
        c = requests.post(f"{args.agent}/batch/claim", json={"batch_id": quote["batch_id"], "payment_txid": row["pay_txid"],
                                                             "signature": signature}, timeout=15)
        if c.status_code == 201:
            token = c.json()["access_token"]
            break
    if not token:
        row["error"] = "claim not accepted"
        return row
    row["t_claimed"] = int(time.time())

    f = requests.get(f"{args.agent}/batch/fetch/{quote['batch_id']}", params={"token": token}, timeout=30)
    f.raise_for_status()
    delivered = f.json()
    row["t_fetched"] = int(time.time())

    readings = delivered["readings"]
    row.update(verify(readings, quote["commitment"]))
    row["commitment_ok"] = row["commitment_ok"] and delivered["commitment"] == quote["commitment"] \
        and len(readings) == quote["num_readings"]
    row["readings_in_window"] = all(now - args.window <= r["timestamp"] <= now for r in readings)

    # The payment leg, from public indexers: it must exist and carry exactly the memo and the quoted amount.
    pay = chain.lookup(row["pay_txid"])
    row["pay_seen"] = pay["seen"]
    outputs = chain.parse_tx(pay["raw"])["outputs"] if pay["raw"] else []
    row["pay_ok"] = any(data == memo for _, _, data in outputs) and \
        any(addr == quote["payment_address"] and sats == quote["total_price_sats"] for sats, addr, _ in outputs)
    row["verified"] = (row["hash_ok"] and row["commitment_ok"] and row["readings_in_window"] and row["pay_ok"]
                       and row["proofs_mined"] == quote["num_readings"])
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--wif-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=int, default=1800, help="seconds between purchases")
    ap.add_argument("--window", type=int, default=7200, help="buy readings from the last N seconds")
    ap.add_argument("--rounds", type=int, default=0, help="0 = run forever")
    ap.add_argument("--fee-rate", type=float, default=0.05)
    ap.add_argument("--claim-wait", type=int, default=20)
    ap.add_argument("--claim-attempts", type=int, default=30)
    args = ap.parse_args()

    wallet = Wallet(Path(args.wif_file).read_text().strip(), args.fee_rate)
    print(f"buyer address {wallet.address}", flush=True)
    new_file = not os.path.exists(args.out)
    n = 0
    while args.rounds == 0 or n < args.rounds:
        n += 1
        row = {"round": n}
        try:
            run_round(row, args, wallet)
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"[:300]
        with open(args.out, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
                new_file = False
            w.writerow(row)
        print(row, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
