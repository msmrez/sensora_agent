# sensora_agent/src/bsv_utils.py

import requests
import json
import traceback
import logging
from typing import List
import uuid
import secrets
import struct
import threading
from dataclasses import dataclass
from bsvlib import PrivateKey, PublicKey, TxOutput, TxInput, Unspent
from bsvlib.keys import verify_signed_text
from bsvlib.script import Script
from bsvlib.script.type import P2pkhScriptType
from bsvlib.transaction import Transaction

from . import config, database, rawtx

logger = logging.getLogger(__name__)

# --- Key and Address Utilities ---

def generate_access_token() -> str:
    """Generates a cryptographically secure, unique access token."""
    return str(uuid.uuid4()) + "-" + secrets.token_hex(16)

def get_bsv_private_key_object(wif_string: str) -> PrivateKey | None:
    if not wif_string or wif_string in ["YOUR_PRIVATE_KEY_WIF_GOES_HERE", "xxx"]:
        logger.critical(f"CRITICAL: DEVICE_WIF is not set or is a placeholder: '{wif_string}'")
        return None
    try:
        return PrivateKey(wif_string) 
    except Exception as e:
        logger.exception(f"Error creating PrivateKey from WIF ('{wif_string[:5]}...'): {e}")
        return None

def get_bsv_public_key_object(private_key_obj: PrivateKey) -> PublicKey | None:
    if private_key_obj:
        try:
            return private_key_obj.public_key()
        except Exception as e:
            logger.exception(f"Error deriving public key from private key: {e}")
            return None
    return None

def get_address_and_locking_script(public_key_obj: PublicKey) -> tuple[str | None, str | None]:
    if public_key_obj:
        try:
            address_str = public_key_obj.address() 
            locking_script_hex = public_key_obj.locking_script().hex()
            return address_str, locking_script_hex
        except Exception as e:
            logger.exception(f"Error deriving address/locking script from public key: {e}")
            return None, None
    return None, None

# --- Blockchain Interaction via bitails.io API ---

def find_spendable_utxos_bitails(
    address_str: str, 
    expected_locking_script_hex: str | None,
    private_keys_for_address: List[PrivateKey] | None 
) -> tuple[list[Unspent], int]:
    
    if not expected_locking_script_hex:
        logger.critical("[BSV] CRITICAL: Expected locking script hex not provided to find_spendable_utxos_bitails.")
        return [], 0
    if not private_keys_for_address: 
        logger.critical("[BSV] CRITICAL: Private keys not provided to find_spendable_utxos_bitails for signing.")
        return [], 0
        
    utxos_url = f"{config.BITAILS_API_BASE_URL}/address/{address_str}/unspent"
    logger.info(f"[BSV] Fetching UTXOs from: {utxos_url}")
    try:
        response = requests.get(utxos_url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "unspent" not in data or not isinstance(data["unspent"], list):
            logger.error(f"[BSV] No UTXOs found or unexpected format for address {address_str} via bitails.io. Response: {data}")
            return [], 0

        parsed_unspents = []
        total_sats_found = 0
        for utxo_data in data["unspent"]:
            try:
                satoshis_val_from_api = utxo_data.get('satoshis')
                if satoshis_val_from_api is None:
                    logger.warning(f"[BSV] Skipping UTXO with no 'satoshis' field: {utxo_data.get('txid')}:{utxo_data.get('vout')}")
                    continue
                unspent_obj = Unspent(
                    txid=utxo_data['txid'],
                    vout=int(utxo_data['vout']),
                    script_pubkey_hex=expected_locking_script_hex, 
                    satoshi=int(satoshis_val_from_api),
                    script_type=P2pkhScriptType(), 
                    private_keys=private_keys_for_address 
                )
                parsed_unspents.append(unspent_obj)
                total_sats_found += unspent_obj.satoshi 
            except Exception as e:
                logger.exception(f"[BSV] Error processing UTXO data {utxo_data.get('txid')}:{utxo_data.get('vout')}: {e}")
                continue
        if not parsed_unspents: logger.warning(f"[BSV] No suitable UTXOs after parsing for address {address_str}")
        return parsed_unspents, total_sats_found
    except requests.exceptions.RequestException as e: logger.error(f"[BSV] Error fetching UTXOs from bitails.io: {e}")
    except json.JSONDecodeError as e: logger.error(f"[BSV] Error decoding JSON from bitails.io UTXO response: {e}")
    except Exception as e:
        logger.exception(f"[BSV] Unexpected error in find_spendable_utxos_bitails: {e}")
    return [], 0

@dataclass
class BroadcastResult:
    """Outcome of one broadcast attempt. v1 discarded the HTTP status and body; v2 keeps them for the tx log.
    accepted: True (2xx with our txid), False (definite rejection, including a 2xx carrying a node error),
    None (unknown: timeout, 5xx, bad body)."""
    accepted: bool | None
    http_status: int | None
    body: str

# Substrings in a broadcast error body that mean an input is already spent elsewhere.
CONFLICT_MARKERS = ("missing inputs", "missing-inputs", "missingorspent", "mempool-conflict",
                    "txn-mempool-conflict", "double spend", "double-spend", "already spent")

def is_already_known(body: str) -> bool:
    body = (body or "").lower()
    return ("already known" in body or "already-known" in body or "already in the mempool" in body
            or "already-in-mempool" in body or "txn-already" in body
            or "already in block chain" in body or "already in the block chain" in body)

def is_conflict_error(body: str) -> bool:
    body = (body or "").lower()
    return not is_already_known(body) and any(marker in body for marker in CONFLICT_MARKERS)

def broadcast_bsv_transaction_bitails(raw_tx_hex: str) -> BroadcastResult:
    broadcast_url = f"{config.BITAILS_API_BASE_URL}/tx/broadcast"
    headers = {"Content-Type": "application/json"}
    payload = {"raw": raw_tx_hex}
    expected_txid = rawtx.txid_of(raw_tx_hex)
    logger.info(f"[BSV] Broadcasting TX {expected_txid} to bitails.io")
    try:
        response = requests.post(broadcast_url, headers=headers, json=payload, timeout=15)
    except requests.exceptions.RequestException as e:
        # The node may still have received it; the tracker resolves the outcome from the indexers.
        logger.error(f"[BSV] Broadcast outcome unknown for {expected_txid[:12]}: {e}")
        return BroadcastResult(None, None, str(e)[:2000])
    body = response.text[:2000]
    node_error = None
    if 200 <= response.status_code < 300:
        try:
            data = response.json()
            returned, node_error = data.get("txid"), data.get("error")
        except (ValueError, AttributeError):
            returned = None
        if returned == expected_txid and not node_error:
            logger.info(f"[BSV] Broadcast accepted by bitails.io. TXID: {returned}")
            return BroadcastResult(True, response.status_code, body)
        # Bitails answers node rejections with HTTP 201 and {"error": {"code": -25, ...}}; classify them like a 4xx.
        # Its own connection failures ({"error": {"errno": -111, "code": "ECONNREFUSED"}}) leave the outcome unknown.
        if not (isinstance(node_error, dict) and isinstance(node_error.get("code"), int)):
            logger.error(f"[BSV] Broadcast returned unexpected body (HTTP {response.status_code}): {body}")
            return BroadcastResult(None, response.status_code, body)
    if is_already_known(body):
        return BroadcastResult(True, response.status_code, body)
    if response.status_code >= 500 or response.status_code == 429:
        logger.error(f"[BSV] Broadcast outcome unknown (HTTP {response.status_code}): {body}")
        return BroadcastResult(None, response.status_code, body)
    logger.error(f"[BSV] Broadcast rejected by bitails.io (HTTP {response.status_code}): {body}")
    return BroadcastResult(False, response.status_code, body)

# --- Local UTXO bookkeeping (v2) ---
# The indexer can lag behind the agent's own broadcasts, so v1 sometimes re-selected a coin it had just
# spent, producing a conflicting transaction. v2 remembers what it spent and the change it created.
_wallet_lock = threading.RLock()
_spent_outpoints: set[tuple[str, int]] = set()
_own_change: dict[tuple[str, int], Unspent] = {}

_wallet = {"key": None, "script_hex": None}

def set_wallet(priv_key_obj: PrivateKey, locking_script_hex: str) -> None:
    _wallet.update(key=priv_key_obj, script_hex=locking_script_hex)

def load_spent_outpoints(outpoints) -> None:
    with _wallet_lock:
        _spent_outpoints.update((txid, int(vout)) for txid, vout in outpoints)

def _own_outputs(txid: str, raw_hex: str) -> dict[tuple[str, int], Unspent]:
    """Outputs of one of our own transactions that pay back to our address."""
    found = {}
    for vout, (sats, script) in enumerate(rawtx.parse_outputs(bytes.fromhex(raw_hex))):
        if sats > 0 and script.hex() == _wallet["script_hex"]:
            found[(txid, vout)] = Unspent(txid=txid, vout=vout, satoshi=sats, script_pubkey_hex=_wallet["script_hex"],
                                          script_type=P2pkhScriptType(), private_keys=[_wallet["key"]])
    return found

def rebuild_own_change(open_txs: list[dict]) -> None:
    """At startup, re-learn the change of our still-unconfirmed transactions."""
    with _wallet_lock:
        for tx in open_txs:
            for outpoint, utxo in _own_outputs(tx["txid"], tx["raw_hex"]).items():
                if outpoint not in _spent_outpoints:
                    _own_change.setdefault(outpoint, utxo)

def release_inputs(dropped: dict, still_spent: set[tuple[str, int]], parents: dict[str, dict]) -> None:
    """
    A dropped transaction spent nothing: its inputs become spendable again, unless another live transaction
    spends them. If an input was change from one of our own live transactions, it is put back in the pool.
    """
    released = [tuple(i) for i in json.loads(dropped.get("inputs") or "[]") if tuple(i) not in still_spent]
    with _wallet_lock:
        for outpoint in released:
            _spent_outpoints.discard(outpoint)
            parent = parents.get(outpoint[0])
            if parent:
                utxo = _own_outputs(parent["txid"], parent["raw_hex"]).get(outpoint)
                if utxo:
                    _own_change[outpoint] = utxo
    if released:
        database.remove_spent_outpoints(released)

def _mark_spent(inputs: list[tuple[str, int]]) -> None:
    with _wallet_lock:
        _spent_outpoints.update(inputs)
        for outpoint in inputs:
            _own_change.pop(outpoint, None)
    try:
        database.add_spent_outpoints(inputs)
    except Exception as e:
        logger.exception(f"[BSV] Could not persist spent outpoints: {e}")

def forget_change_of(txids: set[str]) -> None:
    """Called when transactions are dropped: their change never existed, so it must not be selected."""
    with _wallet_lock:
        for outpoint in [o for o in _own_change if o[0] in txids]:
            del _own_change[outpoint]

def select_utxos(api_unspents: list[Unspent], spent: set, own_change: dict, needed_sats: int) -> list[Unspent]:
    """Smallest-first selection (as in v1) over indexer UTXOs plus own unindexed change, minus spent outpoints."""
    pool = {(u.txid, u.vout): u for u in api_unspents if (u.txid, u.vout) not in spent}
    for outpoint, utxo in own_change.items():
        if outpoint not in spent:
            pool.setdefault(outpoint, utxo)
    selected, total = [], 0
    for utxo in sorted(pool.values(), key=lambda u: u.satoshi):
        selected.append(utxo)
        total += utxo.satoshi
        # Each extra input adds ~148 bytes of fee; keep a margin so multi-input txs are not underpaid.
        if total >= needed_sats + int(148 * len(selected) * config.BSV_FEE_SATOSHIS_PER_BYTE) + 1:
            return selected
    return []

def _build_op_return_tx(priv_key_obj, source_address_str, unspents, op_return_data: bytes) -> Transaction:
    script_bytes_list = [b'\x00', b'\x6a']
    data_len = len(op_return_data)
    if data_len <= 75: script_bytes_list.append(bytes([data_len]))
    elif data_len <= 255: script_bytes_list.extend([b'\x4c', bytes([data_len])])
    elif data_len <= 65535: script_bytes_list.extend([b'\x4d', struct.pack('<H', data_len)])
    else: script_bytes_list.extend([b'\x4e', struct.pack('<I', data_len)])
    script_bytes_list.append(op_return_data)
    tx_obj = Transaction(tx_outputs=[TxOutput(out=Script(b''.join(script_bytes_list)), satoshi=0)],
                         fee_rate=config.BSV_FEE_SATOSHIS_PER_BYTE)
    tx_obj.add_inputs(unspents)
    tx_obj.add_change(change_address=source_address_str)
    tx_obj.sign()
    return tx_obj

MAX_CONFLICT_RETRIES = 2

def create_and_broadcast_op_return_tx(lock: threading.Lock, priv_key_obj: PrivateKey, pub_key_obj: PublicKey, source_locking_script_hex: str, op_return_data: bytes) -> dict | None:
    """
    Thread-safe function to create, sign, and broadcast a transaction with an OP_RETURN.
    Returns a dict describing the final attempt ("accepted" is True, False or None for unknown, and
    "txid" is always the real txid of the raw transaction), or None if no transaction could be built.
    On a conflict the spent coins are excluded and the transaction is rebuilt (up to MAX_CONFLICT_RETRIES).
    """
    if not isinstance(op_return_data, bytes):
        raise ValueError(f"op_return_data must be bytes, got {type(op_return_data)}")

    with lock:
        logger.info("[BSV_LOCK] Acquired transaction lock.")
        source_address_str = pub_key_obj.address()
        estimated_tx_size = 148 + (9 + len(op_return_data)) + 34 + 10
        min_fee_needed = int(estimated_tx_size * config.BSV_FEE_SATOSHIS_PER_BYTE) or 1

        attempts = []
        excluded: set[tuple[str, int]] = set()   # coins rejected in this call only; never persisted
        for _ in range(1 + MAX_CONFLICT_RETRIES):
            available_unspents, _ = find_spendable_utxos_bitails(
                source_address_str, source_locking_script_hex, [priv_key_obj]
            )
            with _wallet_lock:
                selected_unspents = select_utxos(available_unspents, _spent_outpoints | excluded, _own_change, min_fee_needed)
            if not selected_unspents:
                logger.warning(f"[BSV] Insufficient unspent funds for a {min_fee_needed}-sat fee. Please fund {source_address_str}")
                break
            try:
                tx_obj = _build_op_return_tx(priv_key_obj, source_address_str, selected_unspents, op_return_data)
            except Exception as e:
                logger.exception(f"[BSV] Error during OP_RETURN transaction creation/signing: {e}")
                break

            raw_hex = tx_obj.raw()
            txid = rawtx.txid_of(raw_hex)
            inputs = [(u.txid, u.vout) for u in selected_unspents]
            logger.info(f"[BSV] TX {txid[:12]} constructed. Size: {len(raw_hex) // 2} bytes. Fee: {tx_obj.fee()} sats.")
            result = broadcast_bsv_transaction_bitails(raw_hex)
            attempt = {"txid": txid, "accepted": result.accepted, "raw_hex": raw_hex,
                       "http_status": result.http_status, "body": result.body, "fee": tx_obj.fee(),
                       "size": len(raw_hex) // 2, "inputs": inputs}
            attempts.append(attempt)

            if result.accepted is not False:
                # Accepted, or unknown: treat the inputs as spent and the change as ours until the tracker
                # learns otherwise. Re-using the inputs would risk a self-conflict if it did go through.
                _mark_spent(inputs)
                with _wallet_lock:
                    for vout, out in enumerate(tx_obj.tx_outputs):
                        if out.satoshi > 0 and out.locking_script.hex() == source_locking_script_hex:
                            _own_change[(txid, vout)] = Unspent(
                                txid=txid, vout=vout, satoshi=out.satoshi,
                                script_pubkey_hex=source_locking_script_hex, script_type=P2pkhScriptType(),
                                private_keys=[priv_key_obj])
                break
            if not is_conflict_error(result.body):
                break
            # One of these coins is spent (or its parent is not yet known to this node). Try other coins,
            # without blacklisting: a coin wrongly excluded for good could stop all stamping.
            excluded.update(inputs)

        logger.info("[BSV_LOCK] Releasing transaction lock.")
        if not attempts:
            return None
        final = dict(attempts[-1])
        final["previous_attempts"] = attempts[:-1]
        return final

def parse_op_return_data_from_hex(op_return_script_hex: str) -> bytes | None:
    """
    Parses data from an OP_RETURN script hex by manually handling pushdata opcodes.
    This is the robust method we know works.
    """
    try:
        # Determine start of data payload by skipping OP_FALSE and OP_RETURN opcodes
        if op_return_script_hex.startswith("006a"):
            script_bytes = bytes.fromhex(op_return_script_hex[4:])
        elif op_return_script_hex.startswith("6a"):
            script_bytes = bytes.fromhex(op_return_script_hex[2:])
        else:
            return None # Not a recognized OP_RETURN script

        if not script_bytes:
            return b'' # Empty OP_RETURN

        opcode = script_bytes[0]
        if 0x01 <= opcode <= 0x4b: # Direct push of 1-75 bytes
            length_to_read = opcode
            if len(script_bytes) < 1 + length_to_read: return None
            return script_bytes[1 : 1 + length_to_read]
        elif opcode == 0x4c: # OP_PUSHDATA1
            if len(script_bytes) < 2: return None
            length_to_read = script_bytes[1]
            if len(script_bytes) < 2 + length_to_read: return None
            return script_bytes[2 : 2 + length_to_read]
        elif opcode == 0x4d: # OP_PUSHDATA2
            if len(script_bytes) < 3: return None
            length_to_read = int.from_bytes(script_bytes[1:3], 'little')
            if len(script_bytes) < 3 + length_to_read: return None
            return script_bytes[3 : 3 + length_to_read]
        elif opcode == 0x4e: # OP_PUSHDATA4
            if len(script_bytes) < 5: return None
            length_to_read = int.from_bytes(script_bytes[1:5], 'little')
            if len(script_bytes) < 5 + length_to_read: return None
            return script_bytes[5 : 5 + length_to_read]
        else: # Not a standard push opcode or malformed
            return None
            
    except Exception as e:
        logger.exception(f"[BSV_PARSE] Error manually parsing OP_RETURN hex '{op_return_script_hex[:20]}...': {e}")
        return None

def check_payment_on_chain(reading_id_to_check: int, payment_to_address: str, device_p2pkh_script_hex: str, specific_txid_to_check: str | None = None,
                           commitment: str | None = None) -> tuple[bool, str | None]:
    """
    Verifies a single-reading payment from the raw transaction (Bitails' /tx endpoint no longer returns
    outputs, which silently broke the v1 check). The memo is SENSORA_PAY:<reading_id>:<commitment>, where the
    commitment binds the payment to the reading's on-chain proof, exactly as for a batch of one.
    """
    if not specific_txid_to_check:
        logger.warning("[PAY_CHECK] A payment TXID is required.")
        return False, None
    raw = rawtx.fetch_raw(specific_txid_to_check)
    if raw is None:
        logger.warning(f"[PAY_CHECK] TX {specific_txid_to_check} not found yet.")
        return False, None
    try:
        memo = f"SENSORA_PAY:{reading_id_to_check}" + (f":{commitment}" if commitment else "")
        ok, reason = rawtx.pays_with_memo(raw, device_p2pkh_script_hex, memo.encode(),
                                          min_sats=config.PRICE_SATS_PER_READING)
    except Exception as e:
        logger.exception(f"[PAY_CHECK] Could not parse {specific_txid_to_check}: {e}")
        return False, None
    logger.info(f"[PAY_CHECK] {specific_txid_to_check[:12]} for reading {reading_id_to_check}: {reason}")
    return (True, specific_txid_to_check) if ok else (False, None)


def check_batch_payment_on_chain(op_return_string: str, our_address: str, our_script_hex: str, txid: str, expected_sats: int) -> (bool, str):
    """
    Verifies a payment transaction for a BATCH of readings: the OP_RETURN must equal the memo exactly and
    an output must pay our script exactly the quoted amount.
    """
    try:
        raw = rawtx.fetch_raw(txid)
        if raw is None:
            logger.warning(f"Payment check: tx {txid} not found yet.")
            return False, "Not found."
        ok, reason = rawtx.pays_with_memo(raw, our_script_hex, op_return_string.encode('utf-8'), exact_sats=expected_sats)
        (logger.info if ok else logger.warning)(f"Batch payment check for {txid}: {reason}")
        return ok, reason
    except Exception as e:
        logger.exception(f"An unexpected error occurred during payment verification for txid {txid}: {e}")
        return False, "Exception occurred."


def payment_confirmed_enough(payment_txid: str, amount_sats: int) -> bool:
    """
    Small payments are accepted unconfirmed (the double-spend risk is bounded by the amount); payments above
    config.MAX_UNCONFIRMED_SATS must be mined first.
    """
    if amount_sats <= config.MAX_UNCONFIRMED_SATS:
        return True
    return get_tx_status(payment_txid)["state"] == "mined"


def claim_message(item_id, payment_txid: str) -> str:
    """Text the payer signs (Bitcoin Signed Message) to claim the token for a payment."""
    return f"SENSORA_CLAIM:{item_id}:{payment_txid}"


def claim_signed_by_payer(payment_txid: str, item_id, signature: str | None) -> bool:
    """
    The payment txid and the batch id are public once the payment is broadcast, so anyone watching the chain
    could claim the token first. Only the payer can sign the claim message with a key that signed one of the
    payment's inputs. Replaying a seen claim is useless: access_tokens.payment_txid is UNIQUE.
    """
    if not signature:
        return False
    raw = rawtx.fetch_raw(payment_txid)
    if raw is None:
        return False
    text = claim_message(item_id, payment_txid)
    for address in rawtx.payer_addresses(raw):
        try:
            if verify_signed_text(text, address, signature):
                return True
        except Exception:
            continue
    return False


# --- Independent inclusion checks (v2) ---

JUNGLEBUS_TX_URL = "https://junglebus.gorillapool.io/v1/transaction/get/{txid}"

def get_tx_status(txid: str) -> dict:
    """
    Asks two public indexers whether a transaction is known and mined. The first indexer that reports a
    block height decides "mined".
    Returns {"state": "mined" | "seen" | "unseen" | "unknown", "block_height", "block_time"}.
    "unseen" means both indexers answered "not found"; "unknown" means at least one could not be asked
    (timeout, 429, 5xx), so the caller must not act on it.
    """
    status = {"state": "unknown", "block_height": None, "block_time": None}
    answers = []
    try:
        r = requests.get(JUNGLEBUS_TX_URL.format(txid=txid), timeout=15, headers={"User-Agent": "sensora-agent"})
        if r.status_code == 200:
            data = r.json()
            if data.get("block_height"):
                return {"state": "mined", "block_height": int(data["block_height"]),
                        "block_time": int(data.get("block_time") or 0) or None}
            answers.append("seen")
        elif r.status_code == 404:
            answers.append("unseen")
        else:
            answers.append("unknown")
    except Exception as e:
        logger.warning(f"[TX_STATUS] JungleBus lookup failed for {txid[:12]}: {e}")
        answers.append("unknown")
    try:
        r = requests.get(f"{config.BITAILS_API_BASE_URL}/tx/{txid}", timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data.get("blockheight"):
                return {"state": "mined", "block_height": int(data["blockheight"]),
                        "block_time": int(data.get("blocktime") or 0) or None}
            answers.append("seen")
        elif r.status_code == 404:
            answers.append("unseen")
        else:
            answers.append("unknown")
    except Exception as e:
        logger.warning(f"[TX_STATUS] Bitails lookup failed for {txid[:12]}: {e}")
        answers.append("unknown")
    if "seen" in answers:
        status["state"] = "seen"
    elif answers == ["unseen", "unseen"]:
        status["state"] = "unseen"
    return status
