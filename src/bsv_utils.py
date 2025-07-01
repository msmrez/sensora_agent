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
from bsvlib import PrivateKey, PublicKey, TxOutput, TxInput, Unspent
from bsvlib.script import Script
from bsvlib.script.type import P2pkhScriptType
from bsvlib.transaction import Transaction

from . import config

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

def broadcast_bsv_transaction_bitails(raw_tx_hex: str) -> str | None:
    broadcast_url = f"{config.BITAILS_API_BASE_URL}/tx/broadcast"
    headers = {"Content-Type": "application/json"}
    payload = {"raw": raw_tx_hex} 
    logger.info(f"[BSV] Broadcasting TX to bitails.io: {raw_tx_hex[:60]}...")
    try:
        response = requests.post(broadcast_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if "txid" in data and data["txid"]:
            logger.info(f"[BSV] Broadcast successful via bitails.io. TXID: {data['txid']}")
            return data['txid']
        else:
            error_msg = data.get("message", "Unknown error from bitails.io broadcast") if isinstance(data, dict) else str(data)
            logger.error(f"[BSV] Broadcast to bitails.io failed or TXID not returned. Response: {data}. Error: {error_msg}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"[BSV] Error broadcasting transaction via bitails.io: {e}")
        if e.response is not None: logger.error(f"[BSV] bitails.io error response: {e.response.text}")
    except json.JSONDecodeError as e: logger.error(f"[BSV] Error decoding JSON from bitails.io broadcast response: {e}")
    except Exception as e:
        logger.exception(f"[BSV] Unexpected error in broadcast_bsv_transaction_bitails: {e}")
    return None

def create_and_broadcast_op_return_tx(lock: threading.Lock, priv_key_obj: PrivateKey, pub_key_obj: PublicKey, source_locking_script_hex: str, op_return_data: bytes) -> str | None:
    """
    Thread-safe function to create, sign, and broadcast a transaction with an OP_RETURN.
    It acquires a lock to prevent UTXO race conditions between threads.
    """
    
    with lock:
        # The lock is now acquired. Only one thread can execute this code block at a time.
        logger.info("[BSV_LOCK] Acquired transaction lock.")
        
        source_address_str = pub_key_obj.address() 
        estimated_tx_size = 148 + (9 + len(op_return_data)) + 34 + 10 
        min_fee_needed = int(estimated_tx_size * config.BSV_FEE_SATOSHIS_PER_BYTE) or 1
        
        logger.info(f"[BSV] Attempting to create OP_RETURN for {source_address_str}. Estimated fee: {min_fee_needed} sats.")
        
        # This UTXO fetch is now protected by the lock.
        available_unspents, total_sats = find_spendable_utxos_bitails(
            source_address_str, source_locking_script_hex, [priv_key_obj] 
        )

        if not available_unspents or total_sats < min_fee_needed:
            logger.warning(f"[BSV] Insufficient funds. Available: {total_sats}, Need: {min_fee_needed}. Please fund {source_address_str}")
            logger.info("[BSV_LOCK] Releasing transaction lock (insufficient funds).")
            return None

        # Simple coin selection
        available_unspents.sort(key=lambda u: u.satoshi)
        selected_unspents: list[Unspent] = []; sats_in_selected = 0
        for utxo in available_unspents:
            selected_unspents.append(utxo)
            sats_in_selected += utxo.satoshi
            if sats_in_selected >= min_fee_needed: break 
        
        if sats_in_selected < min_fee_needed:
            logger.error(f"[BSV] Could not select enough UTXOs. Selected: {sats_in_selected}, Need: {min_fee_needed}")
            logger.info("[BSV_LOCK] Releasing transaction lock (UTXO selection failed).")
            return None

        try:
            if not isinstance(op_return_data, bytes):
                raise ValueError(f"op_return_data must be bytes, got {type(op_return_data)}")
            
            # Manual OP_RETURN script serialization
            script_bytes_list = [b'\x00', b'\x6a']
            data_len = len(op_return_data)
            if data_len <= 75: script_bytes_list.append(bytes([data_len])) 
            elif data_len <= 255: script_bytes_list.extend([b'\x4c', bytes([data_len])]) 
            elif data_len <= 65535: script_bytes_list.extend([b'\x4d', struct.pack('<H', data_len)]) 
            else: script_bytes_list.extend([b'\x4e', struct.pack('<I', data_len)])
            script_bytes_list.append(op_return_data)
            serialized_script_bytes = b''.join(script_bytes_list)
            op_return_script = Script(serialized_script_bytes)

            outputs_for_tx = [TxOutput(out=op_return_script, satoshi=0)] 
            
            tx_obj = Transaction(tx_outputs=outputs_for_tx, fee_rate=config.BSV_FEE_SATOSHIS_PER_BYTE)
            tx_obj.add_inputs(selected_unspents)
            tx_obj.add_change(change_address=source_address_str)
            tx_obj.sign() 
            
            logger.info(f"[BSV] TX constructed. Size: {len(tx_obj.serialize())} bytes. Fee: {tx_obj.fee()} sats.")
            logger.debug(f"[BSV RAW TX HEX TO BROADCAST]: {tx_obj.raw()}")
            
            # The broadcast is also within the lock, ensuring the whole process is atomic.
            txid = broadcast_bsv_transaction_bitails(tx_obj.raw())
            
            logger.info("[BSV_LOCK] Releasing transaction lock (broadcast successful).")
            return txid
            
        except Exception as e:
            logger.exception(f"[BSV] Error during OP_RETURN transaction creation/signing: {e}")
            logger.info("[BSV_LOCK] Releasing transaction lock (exception).")
            return None
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

def check_payment_on_chain(reading_id_to_check: int, payment_to_address: str, device_p2pkh_script_hex: str, specific_txid_to_check: str | None = None) -> tuple[bool, str | None]:
    
    candidate_tx_ids: list[str] = []
    if specific_txid_to_check:
        logger.info(f"[PAY_CHECK] Verifying specific TXID: {specific_txid_to_check} for reading {reading_id_to_check}")
        candidate_tx_ids = [specific_txid_to_check]
    else:
        history_url = f"{config.BITAILS_API_BASE_URL}/address/{payment_to_address}/history"
        logger.info(f"[PAY_CHECK] Fetching history for {payment_to_address} to check for reading {reading_id_to_check}")
        try:
            response = requests.get(history_url, timeout=15)
            response.raise_for_status()
            for tx in response.json().get("history", []):
                if tx.get("outputSatoshis", 0) >= config.PRICE_SATS_PER_READING:
                    candidate_tx_ids.append(tx["txid"])
                    if len(candidate_tx_ids) >= 10: break
        except Exception as e:
            logger.error(f"[PAY_CHECK] Error fetching history: {e}")
            return False, None
    
    if not candidate_tx_ids:
        logger.warning(f"[PAY_CHECK] No candidate payment TXIDs found.")
        return False, None

    logger.info(f"[PAY_CHECK] Found {len(candidate_tx_ids)} candidates. Fetching details...")
    expected_op_return_str = f"SENSORA_PAY:{reading_id_to_check}"

    for txid in candidate_tx_ids:
        tx_detail_url = f"{config.BITAILS_API_BASE_URL}/tx/{txid}"
        try:
            response = requests.get(tx_detail_url, timeout=15)
            if response.status_code == 404:
                logger.warning(f"[PAY_CHECK] TX {txid} not found on bitails.io (404).")
                continue
            response.raise_for_status()
            tx_detail = response.json()
            
            outputs = tx_detail.get("outputs", [])
            payment_found = any(o.get("script") == device_p2pkh_script_hex and o.get("satoshis", 0) >= config.PRICE_SATS_PER_READING for o in outputs)
            
            op_return_found = False
            for o in outputs:
                script_hex = o.get("script", "")
                if script_hex.startswith("006a") or script_hex.startswith("6a"): # Added check for 6a as well
                    op_return_data = parse_op_return_data_from_hex(script_hex)
                    if op_return_data is not None:
                        try:
                            if op_return_data.decode('utf-8') == expected_op_return_str:
                                op_return_found = True
                                break 
                        except UnicodeDecodeError:
                            continue 

            if payment_found and op_return_found:
                logger.info(f"[PAY_CHECK] SUCCESS! Payment confirmed for reading {reading_id_to_check} in TX {txid}.")
                return True, txid
        except Exception as e:
            logger.exception(f"[PAY_CHECK] Error processing TX {txid}: {e}")
            if specific_txid_to_check: return False, None

    logger.warning(f"[PAY_CHECK] Payment not confirmed for reading {reading_id_to_check} after checking candidates.")
    return False, None