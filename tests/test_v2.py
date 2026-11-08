"""Tests for the v2 reliability changes. Run inside the agent image:
    SENSORA_DATA_DIR=$(mktemp -d) python -m pytest -q tests
(a config.ini must exist in the project root; config.ini.example is enough)."""
import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from bsvlib import PrivateKey, Transaction, TxOutput, Unspent
from bsvlib.script import Script
from bsvlib.script.type import P2pkhScriptType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from src import config, database
    monkeypatch.setattr(config, "DATABASE_NAME", str(tmp_path / "test.db"))
    database.init_db()
    return database


def utxo(key, txid, vout, sats):
    return Unspent(txid=txid, vout=vout, satoshi=sats, script_pubkey_hex=key.public_key().locking_script().hex(),
                   script_type=P2pkhScriptType(), private_keys=[key])


# --- coin selection ---------------------------------------------------------

def test_select_skips_spent_and_uses_own_change():
    from src.bsv_utils import select_utxos
    key = PrivateKey()
    spent_coin = utxo(key, "aa" * 32, 0, 5)
    other = utxo(key, "bb" * 32, 1, 50)
    change = utxo(key, "cc" * 32, 1, 10)
    # The indexer still lists the coin we already spent and does not yet list our new change.
    chosen = select_utxos([spent_coin, other], {("aa" * 32, 0)}, {("cc" * 32, 1): change}, 3)
    assert [(u.txid, u.vout) for u in chosen] == [("cc" * 32, 1)]


def test_select_returns_empty_when_insufficient():
    from src.bsv_utils import select_utxos
    key = PrivateKey()
    assert select_utxos([utxo(key, "aa" * 32, 0, 2)], set(), {}, 3) == []


def test_conflict_detection():
    from src.bsv_utils import is_conflict_error, is_already_known
    assert is_conflict_error('{"message":"Missing inputs"}')
    assert is_conflict_error("txn-mempool-conflict")
    assert not is_conflict_error("txn-already-known")
    assert not is_conflict_error("no unspent outputs for address")
    assert is_already_known("txn-already-known")
    assert not is_conflict_error("")


class _Resp:
    def __init__(self, status, body):
        self.status_code, self.text = status, body

    def json(self):
        import json
        return json.loads(self.text)


def test_http_201_with_node_error_is_classified(monkeypatch):
    # Seen in run 1: Bitails returns HTTP 201 with {"error": {...}} for node rejections.
    from src import bsv_utils
    tx = Transaction(tx_outputs=[TxOutput(out=Script(bytes.fromhex("006a")), satoshi=0)])
    raw = tx.raw()
    meta = ',"__meta":{"notice":"pruned mode"}'
    for body, expected in [('{"error":{"code":-25,"message":"missing-inputs"}' + meta + '}', False),
                           ('{"error":{"code":-27,"message":"already-in-mempool"}' + meta + '}', True),
                           ('{"txid":"%s"}' % tx.txid(), True),
                           ('{"txid":"%s"}' % ("00" * 32), None),
                           # Seen in run 2 during a Bitails outage: not a node verdict, so the outcome is unknown.
                           ('{"error":{"errno":-111,"code":"ECONNREFUSED","syscall":"connect"}' + meta + '}', None)]:
        monkeypatch.setattr(bsv_utils.requests, "post", lambda *a, **k: _Resp(201, body))
        result = bsv_utils.broadcast_bsv_transaction_bitails(raw)
        assert result.accepted is expected, body
    assert bsv_utils.is_conflict_error('{"error":{"code":-25,"message":"missing-inputs"}}')


def test_select_keeps_fee_margin_for_extra_inputs(monkeypatch):
    from src import bsv_utils, config
    monkeypatch.setattr(config, "BSV_FEE_SATOSHIS_PER_BYTE", 0.05)
    key = PrivateKey()
    coins = [utxo(key, f"{i:02x}" * 32, 0, 10) for i in range(1, 6)]
    # One 10-sat coin cannot pay a 3-sat fee plus ~7 sats for its own input; two can.
    assert len(bsv_utils.select_utxos(coins, set(), {}, 3)) == 2
    # Coins worth less than the fee of spending them are never enough.
    dust = [utxo(key, f"{i:02x}" * 32, 0, 3) for i in range(1, 6)]
    assert bsv_utils.select_utxos(dust, set(), {}, 3) == []


def test_payment_check_requires_exact_memo_and_amount():
    from src import rawtx
    key, payee = PrivateKey(), PrivateKey()
    memo = b"SENSORA_PAY:" + b"ab" * 64
    script = Script(b"\x00\x6a\x4c" + bytes([len(memo)]) + memo)
    tx = Transaction(tx_outputs=[TxOutput(out=payee.address(), satoshi=90), TxOutput(out=script, satoshi=0)], fee_rate=0.05)
    tx.add_inputs([utxo(key, "ee" * 32, 0, 5000)])
    tx.add_change(change_address=key.address())
    tx.sign()
    raw, ours = bytes.fromhex(tx.raw()), payee.public_key().locking_script().hex()
    assert rawtx.pays_with_memo(raw, ours, memo, exact_sats=90)[0]
    assert not rawtx.pays_with_memo(raw, ours, memo, exact_sats=91)[0]
    assert not rawtx.pays_with_memo(raw, ours, memo[:-1], exact_sats=90)[0]
    assert rawtx.txid_of(tx.raw()) == tx.txid()


# --- proof / payment binding -----------------------------------------------

def test_buyer_hash_matches_stamped_hash():
    from src import protocols, config
    import buyer
    config.DEVICE_IPV6_ADDRESS = "2001:db8::1"
    payload = protocols.create_sensora_proof_payload(1761000000, 1, (23.4, 55.0))
    reading = {"timestamp": 1761000000, "value_temp": 23.4, "value_humid": 55.0}
    assert buyer.reading_hash(reading) == protocols.data_hash_from_proof_payload(payload)


def test_commitment_changes_with_any_reading():
    from src import protocols
    h1, h2 = b"\x01" * 32, b"\x02" * 32
    assert protocols.batch_commitment([h1, h2]) != protocols.batch_commitment([h2, h1])
    assert protocols.batch_commitment([h1, h2]) != protocols.batch_commitment([h1])


# --- chain parsing (used by buyer and audit) --------------------------------

def test_parse_tx_reads_op_return_and_change():
    import chain
    key = PrivateKey()
    data = b"SENSORA_PAY:" + b"ab" * 64
    script = Script(b"\x00\x6a\x4c" + bytes([len(data)]) + data)
    tx = Transaction(tx_outputs=[TxOutput(out=script, satoshi=0)], fee_rate=0.05)
    tx.add_inputs([utxo(key, "dd" * 32, 2, 5000)])
    tx.add_change(change_address=key.address())
    tx.sign()
    parsed = chain.parse_tx(bytes.fromhex(tx.raw()))
    assert parsed["inputs"] == [("dd" * 32, 2)]
    assert parsed["outputs"][0][2] == data
    assert parsed["outputs"][1][1] == key.address()


# --- database ---------------------------------------------------------------

def test_v1_database_is_migrated(tmp_path, monkeypatch):
    from src import config, database
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sensor_readings (reading_id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, value_temp REAL, "
                 "value_humid REAL, data_type_code INTEGER NOT NULL, op_return_payload_hex TEXT NOT NULL, "
                 "onchain_proof_txid TEXT NOT NULL UNIQUE, paid INTEGER DEFAULT 0, consumer_payment_txid TEXT, "
                 "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO sensor_readings VALUES (1, 1, 20.0, 50.0, 1, 'aa', 'tx1', 0, NULL, NULL)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DATABASE_NAME", str(path))
    database.init_db()
    row = database.get_reading(1)
    assert row["proof_status"] == "pending" and row["onchain_proof_txid"] == "tx1"


def test_only_mined_readings_are_sold(db):
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.save_reading(200, 200, (21.0, 51.0), 1, "bb", "tx-b")
    db.update_reading_proof(200, proof_status="mined", proof_block_height=900000)
    assert [r["reading_id"] for r in db.get_provable_readings_in_range(0, 1000)] == [200]


# --- tracker ----------------------------------------------------------------

import time as _time


def _attempt(txid, raw="00", accepted=True, inputs=None):
    return {"txid": txid, "accepted": accepted, "raw_hex": raw, "http_status": 200, "body": "", "fee": 1,
            "size": 200, "inputs": inputs or []}


def _status(state, height=None, t=None):
    return lambda txid: {"state": state, "block_height": height, "block_time": t}


def test_tracker_counts_stamp_only_when_mined(db, monkeypatch):
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("seen"))
    tracker.check_open_transactions(now=int(_time.time()))
    assert db.get_key_value("successful_stamp_count") is None
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("mined", 920000, 1761000000))
    tracker.check_open_transactions()
    assert db.get_key_value("successful_stamp_count") == "1"
    assert db.get_reading(100)["proof_status"] == "mined"


def test_tracker_ignores_indexer_errors(db, monkeypatch):
    from src import tracker, bsv_utils
    db.log_tx("adv", None, _attempt("tx-adv"))
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("unknown"))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails", lambda raw: pytest.fail("must not rebroadcast"))
    for minutes in range(1, 60):
        tracker.check_open_transactions(now=int(_time.time()) + 60 * minutes)
    assert db.get_open_txs()[0]["attempts"] == 1


def test_tracker_drops_conflict_then_sweep_restamps(db, monkeypatch):
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    db.log_tx("proof", "200", _attempt("tx-child", inputs=[["tx-a", 1]]))
    bsv_utils._own_change[("tx-child", 1)] = "phantom"
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("unseen"))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 400, '{"message":"Missing inputs"}'))
    tracker.check_open_transactions(now=int(_time.time()) + 600)
    assert db.get_reading(100)["proof_status"] == "dropped"
    assert ("tx-child", 1) not in bsv_utils._own_change      # descendant's change forgotten
    assert db.get_open_txs() == []                            # descendant dropped with its parent
    restamped = []
    tracker.restamp_sweep(restamped.append)
    assert [r["reading_id"] for r in restamped] == [100]


def test_tracker_does_not_drop_tx_that_is_mined_on_recheck(db, monkeypatch):
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    answers = iter([_status("unseen")("x"), _status("mined", 920001, 1761000100)("x")])
    monkeypatch.setattr(bsv_utils, "get_tx_status", lambda txid: next(answers))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 400, "Missing inputs"))
    tracker.check_open_transactions(now=int(_time.time()) + 600)
    assert db.get_reading(100)["proof_status"] == "mined"


def test_tracker_rebroadcast_backs_off(db, monkeypatch):
    from src import tracker, bsv_utils
    db.log_tx("adv", None, _attempt("tx-adv"))
    calls = []
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("unseen"))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: calls.append(raw) or bsv_utils.BroadcastResult(None, 503, "busy"))
    t0 = int(_time.time())
    for minutes in range(1, 31):
        tracker.check_open_transactions(now=t0 + 60 * minutes)
    # 2, 4, 8, 16 min backoff -> at most 4 rebroadcasts in 30 min, and never dropped for "giving up".
    assert 1 <= len(calls) <= 4
    assert db.get_open_txs()[0]["status"] == "pending"


def test_rejected_broadcast_is_logged_and_reading_restamped(db):
    db.save_reading(300, 300, (20.0, 50.0), 1, "aa", "unstamped:300")
    db.update_reading_proof(300, proof_status="unstamped")
    db.log_tx("proof", "300", _attempt("tx-r", accepted=False))
    from src import tracker
    restamped = []
    tracker.restamp_sweep(restamped.append)
    assert [r["reading_id"] for r in restamped] == [300]


# --- regression: coins must never be blacklisted for good (second review, H1) ---

def test_first_broadcast_conflict_does_not_persist_blacklist(db, monkeypatch):
    from src import bsv_utils
    import threading
    key = PrivateKey()
    script_hex = key.public_key().locking_script().hex()
    bsv_utils._spent_outpoints.clear(); bsv_utils._own_change.clear()
    coins = [utxo(key, "a1" * 32, 0, 1000), utxo(key, "b2" * 32, 0, 2000)]
    monkeypatch.setattr(bsv_utils, "find_spendable_utxos_bitails", lambda *a: (coins, 3000))
    answers = iter([bsv_utils.BroadcastResult(False, 400, "Missing inputs"), None])

    def fake_broadcast(raw):
        answer = next(answers)
        return answer or bsv_utils.BroadcastResult(True, 200, "{}")
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails", fake_broadcast)
    attempt = bsv_utils.create_and_broadcast_op_return_tx(threading.Lock(), key, key.public_key(), script_hex, b"x")
    assert attempt["accepted"] is True and len(attempt["previous_attempts"]) == 1
    assert ("a1" * 32, 0) not in set(db.get_spent_outpoints())     # rejected coin not blacklisted
    assert ("b2" * 32, 0) in set(db.get_spent_outpoints())         # coin actually spent is


def test_drop_releases_inputs_and_restores_parent_change(db, monkeypatch):
    from src import bsv_utils, tracker
    key = PrivateKey()
    script_hex = key.public_key().locking_script().hex()
    bsv_utils.set_wallet(key, script_hex)
    bsv_utils._spent_outpoints.clear(); bsv_utils._own_change.clear()
    parent = Transaction(tx_outputs=[TxOutput(out=Script(b"\x00\x6a\x01p"), satoshi=0)], fee_rate=0.05)
    parent.add_inputs([utxo(key, "c3" * 32, 0, 5000)])
    parent.add_change(change_address=key.address())
    parent.sign()
    p_txid = parent.txid()
    db.log_tx("proof", "1", {**_attempt(p_txid, raw=parent.raw())})
    db.save_reading(2, 2, (20.0, 50.0), 1, "aa", "tx-child")
    db.log_tx("proof", "2", _attempt("tx-child", inputs=[[p_txid, 1]]))
    bsv_utils._mark_spent([(p_txid, 1)])
    statuses = {p_txid: _status("seen")("x"), "tx-child": _status("unseen")("x")}
    monkeypatch.setattr(bsv_utils, "get_tx_status", lambda txid: statuses[txid])
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 400, "Missing inputs"))
    tracker.check_open_transactions(now=int(_time.time()) + 600)
    assert (p_txid, 1) not in bsv_utils._spent_outpoints
    assert (p_txid, 1) in bsv_utils._own_change                     # parent's change is spendable again
    assert (p_txid, 1) not in set(db.get_spent_outpoints())


# --- fixes after run 2 (2026-11-08) -------------------------------------------

def test_seen_tx_with_repeated_conflicts_is_dropped_and_restamped(db, monkeypatch):
    # Run 1: 88 txs stayed 'seen' forever because a conflict on rebroadcast only dropped 'unseen' txs.
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    db.update_tx("tx-a", status="seen", first_seen_at=1)
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("seen"))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 201, '{"error":{"code":-25,"message":"missing-inputs"}}'))
    t = int(_time.time())
    tracker.check_open_transactions(now=t + tracker.REBROADCAST_IF_UNMINED_AFTER + 10)
    assert db.get_open_txs()[0]["seen_conflicts"] == 1          # one answer could be indexer lag
    assert db.get_reading(100)["proof_status"] != "dropped"
    tracker.check_open_transactions(now=t + 2 * tracker.REBROADCAST_IF_UNMINED_AFTER + 20)
    assert db.get_open_txs() == []
    assert db.get_reading(100)["proof_status"] == "dropped"
    restamped = []
    tracker.restamp_sweep(restamped.append)
    assert [r["reading_id"] for r in restamped] == [100]


def test_accepted_rebroadcast_resets_seen_conflicts(db, monkeypatch):
    from src import tracker, bsv_utils
    db.log_tx("adv", None, _attempt("tx-adv"))
    db.update_tx("tx-adv", status="seen", first_seen_at=1, seen_conflicts=1)
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("seen"))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails", lambda raw: bsv_utils.BroadcastResult(True, 200, "{}"))
    tracker.check_open_transactions(now=int(_time.time()) + tracker.REBROADCAST_IF_UNMINED_AFTER + 10)
    assert db.get_open_txs()[0]["seen_conflicts"] == 0


def _payment(payer, payee, memo, sats):
    script = Script(b"\x00\x6a\x4c" + bytes([len(memo)]) + memo)
    tx = Transaction(tx_outputs=[TxOutput(out=payee.address(), satoshi=sats), TxOutput(out=script, satoshi=0)], fee_rate=0.05)
    tx.add_inputs([utxo(payer, "ee" * 32, 0, 5000)])
    tx.add_change(change_address=payer.address())
    tx.sign()
    return tx


def test_payer_addresses_from_inputs():
    from src import rawtx
    payer, payee = PrivateKey(), PrivateKey()
    tx = _payment(payer, payee, b"SENSORA_PAY:x", 10)
    assert rawtx.payer_addresses(bytes.fromhex(tx.raw())) == {payer.address()}


@pytest.fixture()
def api(db, monkeypatch):
    from flask import Flask
    from src import routes, config
    agent = PrivateKey()

    class State:
        address = agent.address()
        locking_script_hex = agent.public_key().locking_script().hex()
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    routes.register_all_routes(app, State)
    monkeypatch.setattr(config, "PRICE_SATS_PER_READING", 5)
    monkeypatch.setattr(config, "TOKEN_EXPIRY_SECONDS", 3600)
    monkeypatch.setattr(config, "TOKEN_MAX_USAGE", 3)
    return app.test_client(), agent


def _quote_and_pay(client, agent, db, monkeypatch, payer):
    from src import protocols, config, rawtx
    config.DEVICE_IPV6_ADDRESS = "2001:db8::1"
    for ts in (1000, 1300):
        payload = protocols.create_sensora_proof_payload(ts, 1, (20.0, 50.0))
        db.save_reading(ts, ts, (20.0, 50.0), 1, payload.hex(), f"tx-{ts}")
        db.update_reading_proof(ts, proof_status="mined")
    quote = client.post("/batch/price", json={"start_timestamp": 0, "end_timestamp": 2000}).get_json()
    tx = _payment(payer, agent, f"SENSORA_PAY:{quote['batch_id']}:{quote['commitment']}".encode(), quote["total_price_sats"])
    raw = bytes.fromhex(tx.raw())
    monkeypatch.setattr(rawtx, "fetch_raw", lambda txid: raw if txid == tx.txid() else None)
    return quote, tx.txid()


def test_batch_claim_requires_payer_signature(api, db, monkeypatch):
    client, agent = api
    payer, thief = PrivateKey(), PrivateKey()
    quote, txid = _quote_and_pay(client, agent, db, monkeypatch, payer)
    msg = f"SENSORA_CLAIM:{quote['batch_id']}:{txid}"
    body = {"batch_id": quote["batch_id"], "payment_txid": txid}
    assert client.post("/batch/claim", json=body).status_code == 400                       # no signature
    assert client.post("/batch/claim", json={**body, "signature": thief.sign_text(msg)[1]}).status_code == 403
    assert client.post("/batch/claim", json={**body, "signature": payer.sign_text(f"SENSORA_CLAIM:other:{txid}")[1]}).status_code == 403
    r = client.post("/batch/claim", json={**body, "signature": payer.sign_text(msg)[1]})
    assert r.status_code == 201 and r.get_json()["access_token"]


def test_batch_fetch_enforces_expiry_and_usage(api, db, monkeypatch):
    from src import config
    client, agent = api
    payer = PrivateKey()
    quote, txid = _quote_and_pay(client, agent, db, monkeypatch, payer)
    sig = payer.sign_text(f"SENSORA_CLAIM:{quote['batch_id']}:{txid}")[1]
    token = client.post("/batch/claim", json={"batch_id": quote["batch_id"], "payment_txid": txid, "signature": sig}).get_json()["access_token"]
    url = f"/batch/fetch/{quote['batch_id']}?token={token}"
    assert [client.get(url).status_code for _ in range(4)] == [200, 200, 200, 403]          # max_usage = 3
    # A second, fresh token for another batch that has expired.
    quote2, txid2 = _quote_and_pay(client, agent, db, monkeypatch, payer)
    monkeypatch.setattr(config, "TOKEN_EXPIRY_SECONDS", -1)
    sig2 = payer.sign_text(f"SENSORA_CLAIM:{quote2['batch_id']}:{txid2}")[1]
    token2 = client.post("/batch/claim", json={"batch_id": quote2["batch_id"], "payment_txid": txid2, "signature": sig2}).get_json()["access_token"]
    assert client.get(f"/batch/fetch/{quote2['batch_id']}?token={token2}").status_code == 403


def test_single_claim_requires_payer_signature(api, db, monkeypatch):
    from src import rawtx
    client, agent = api
    payer, thief = PrivateKey(), PrivateKey()
    db.save_reading(1000, 1000, (20.0, 50.0), 1, "aa", "tx-1000")
    db.update_reading_proof(1000, proof_status="mined")
    tx = _payment(payer, agent, b"SENSORA_PAY:1000", 5)
    raw = bytes.fromhex(tx.raw())
    monkeypatch.setattr(rawtx, "fetch_raw", lambda txid: raw)
    msg = f"SENSORA_CLAIM:1000:{tx.txid()}"
    assert client.post("/claim_reading/1000", json={"payment_txid": tx.txid(), "signature": thief.sign_text(msg)[1]}).status_code == 403
    assert client.post("/claim_reading/1000", json={"payment_txid": tx.txid(), "signature": payer.sign_text(msg)[1]}).status_code == 201


def test_fresh_seen_tx_is_not_dropped_on_indexer_flips(db, monkeypatch):
    # Run 1 pattern: indexers flip between unseen and seen; a young tx must not be dropped within minutes.
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    flips = iter([_status(s)("x") for s in ["unseen", "seen"] * 20])
    monkeypatch.setattr(bsv_utils, "get_tx_status", lambda txid: next(flips))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 201, '{"error":{"code":-25,"message":"missing-inputs"}}'))
    t = int(_time.time())
    for minutes in range(1, 50):
        tracker.check_open_transactions(now=t + 60 * minutes)
    assert db.get_reading(100)["proof_status"] != "dropped"
    assert db.get_open_txs()[0]["seen_conflicts"] == 0



# --- remaining review fixes (2026-11-08) --------------------------------------

FEE_REJECTION = '{"error":{"code":-26,"message":"mempool min fee not met"}}'


def test_non_conflict_rejection_on_rebroadcast_drops_unseen_tx(db, monkeypatch):
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    monkeypatch.setattr(bsv_utils, "get_tx_status", _status("unseen"))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 201, FEE_REJECTION))
    tracker.check_open_transactions(now=int(_time.time()) + 600)
    assert db.get_open_txs() == [] and db.get_reading(100)["proof_status"] == "dropped"
    restamped = []
    tracker.restamp_sweep(restamped.append)
    assert [r["reading_id"] for r in restamped] == [100]


def test_non_conflict_rejection_of_mined_tx_marks_it_mined(db, monkeypatch):
    from src import tracker, bsv_utils
    db.save_reading(100, 100, (20.0, 50.0), 1, "aa", "tx-a")
    db.log_tx("proof", "100", _attempt("tx-a"))
    answers = iter([_status("unseen")("x"), _status("mined", 920002, 1761000200)("x")])
    monkeypatch.setattr(bsv_utils, "get_tx_status", lambda txid: next(answers))
    monkeypatch.setattr(bsv_utils, "broadcast_bsv_transaction_bitails",
                        lambda raw: bsv_utils.BroadcastResult(False, 201, FEE_REJECTION))
    tracker.check_open_transactions(now=int(_time.time()) + 600)
    assert db.get_reading(100)["proof_status"] == "mined"


def test_already_in_block_chain_counts_as_known():
    from src.bsv_utils import is_already_known, is_conflict_error
    assert is_already_known('{"error":{"code":-27,"message":"Transaction already in block chain"}}')
    assert not is_conflict_error("transaction already in block chain")
