# sensora_agent/src/tracker.py
#
# v2: a broadcast is not a proof. Every transaction the agent sends is followed until a public indexer
# (JungleBus, then Bitails) reports it mined. Transactions neither indexer knows are rebroadcast with
# backoff; a transaction is dropped only when a rebroadcast reports a conflict and neither indexer knows
# it. Readings whose proof was dropped or never accepted are stamped again by a periodic sweep.

import json
import time
import logging
from . import database, bsv_utils

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
UNSEEN_BACKOFF_BASE = 120                # first rebroadcast 2 min after the last attempt, then doubling
UNSEEN_BACKOFF_MAX = 3600
REBROADCAST_IF_UNMINED_AFTER = 6 * 3600  # p90 inclusion in the field data was ~2.3 h
RESTAMPS_PER_PASS = 3
# A tx that an indexer still lists as unmined while the node twice reports its inputs as missing is dead
# (evicted, or a parent was evicted); before this rule it stayed 'seen' forever and its reading was never
# restamped (Run 1: 88 txs). Conflicts only count once the tx is an hour old, so indexer lag on a fresh or
# just-mined tx is not mistaken for death. If a tx is still dropped wrongly and later mines, the reading
# simply has two proofs, and its released inputs are caught by the conflict-retry path when reused.
SEEN_CONFLICTS_BEFORE_DROP = 2
SEEN_CONFLICT_MIN_AGE = 3600

def _unseen_backoff(attempts: int) -> int:
    return min(UNSEEN_BACKOFF_BASE * 2 ** max(attempts - 1, 0), UNSEEN_BACKOFF_MAX)

def _mark_mined(tx: dict, status: dict, now: int):
    database.update_tx(tx["txid"], status="mined", mined_at=now, block_height=status["block_height"],
                       block_time=status["block_time"], first_seen_at=tx["first_seen_at"] or now)
    if tx["kind"] == "proof":
        database.update_reading_proof(int(tx["ref"]), onchain_proof_txid=tx["txid"], proof_status="mined",
                                      proof_block_height=status["block_height"],
                                      proof_mined_at=status["block_time"])
        database.increment_key_value("successful_stamp_count")
        database.set_key_value("last_stamp_timestamp", str(now))
    else:
        database.increment_key_value("successful_adv_count")
    logger.info(f"[TRACKER] {tx['kind']} {tx['txid'][:12]} mined at height {status['block_height']}.")

def _drop(tx: dict, reason: str):
    """Drops tx and every open tx that spends its outputs; their change never existed."""
    doomed = {tx["txid"]} | database.get_descendant_txids(tx["txid"])
    bsv_utils.forget_change_of(doomed)
    live = [t for t in database.get_live_txs() if t["txid"] not in doomed]
    still_spent = {tuple(i) for t in live for i in json.loads(t["inputs"] or "[]")}
    bsv_utils.release_inputs(tx, still_spent, {t["txid"]: t for t in live})
    kind_label = "stamp" if tx["kind"] == "proof" else "adv"
    database.update_tx(tx["txid"], status="dropped", last_error=reason[:500])
    database.increment_key_value(f"dropped_{kind_label}_count")
    database.increment_key_value(f"failed_{kind_label}_count")
    if tx["kind"] == "proof":
        reading = database.get_reading(int(tx["ref"]))
        if reading and reading["onchain_proof_txid"] == tx["txid"] and reading.get("proof_status") != "mined":
            database.update_reading_proof(int(tx["ref"]), proof_status="dropped")
    logger.warning(f"[TRACKER] {tx['kind']} {tx['txid'][:12]} dropped ({len(doomed) - 1} descendants): {reason[:200]}")

def _rebroadcast(tx: dict, now: int):
    result = bsv_utils.broadcast_bsv_transaction_bitails(tx["raw_hex"])
    database.log_attempt(tx["txid"], result.accepted, result.http_status, result.body)
    database.update_tx(tx["txid"], attempts=tx["attempts"] + 1, last_attempt_at=now,
                       last_error=None if result.accepted else result.body[:500])
    if result.accepted:
        if tx.get("seen_conflicts"):
            database.update_tx(tx["txid"], seen_conflicts=0)
    elif result.accepted is False and bsv_utils.is_conflict_error(result.body):
        # "Missing inputs" is also what a node says for an already-mined tx; ask the indexers once more.
        status = bsv_utils.get_tx_status(tx["txid"])
        if status["state"] == "mined":
            _mark_mined(tx, status, now)
        elif status["state"] == "unseen":
            _drop(tx, result.body)
        elif status["state"] == "seen" and now - tx["created_at"] >= SEEN_CONFLICT_MIN_AGE:
            conflicts = (tx.get("seen_conflicts") or 0) + 1
            database.update_tx(tx["txid"], seen_conflicts=conflicts)
            if conflicts >= SEEN_CONFLICTS_BEFORE_DROP:
                _drop(tx, f"inputs missing on {conflicts} rebroadcasts while unmined: {result.body}")

def check_open_transactions(now: int | None = None):
    now = now or int(time.time())
    for tx in database.get_open_txs():
        try:
            _check_one(tx, now)
        except Exception as e:
            logger.exception(f"[TRACKER] Error while checking {tx['txid'][:12]}: {e}")

def _check_one(tx: dict, now: int):
    status = bsv_utils.get_tx_status(tx["txid"])
    last = tx.get("last_attempt_at") or tx["created_at"]
    if status["state"] == "unknown":
        return
    if status["state"] == "mined":
        _mark_mined(tx, status, now)
    elif status["state"] == "seen":
        if not tx["first_seen_at"]:
            database.update_tx(tx["txid"], status="seen", first_seen_at=now)
        elif now - last > REBROADCAST_IF_UNMINED_AFTER:
            _rebroadcast(tx, now)
    elif now - last > _unseen_backoff(tx["attempts"]):
        _rebroadcast(tx, now)

def restamp_sweep(restamp):
    for reading in database.get_readings_needing_restamp(RESTAMPS_PER_PASS):
        try:
            restamp(reading)
        except Exception as e:
            logger.exception(f"[TRACKER] Restamp of reading {reading['reading_id']} failed: {e}")

def tracker_loop(restamp):
    logger.info("[TRACKER] Transaction tracker starting...")
    while True:
        try:
            check_open_transactions()
            restamp_sweep(restamp)
        except Exception as e:
            logger.exception(f"[TRACKER] Error while checking transactions: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
