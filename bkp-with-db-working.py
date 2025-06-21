#!/usr/bin/env python3

import time
import hashlib
import struct 
import ipaddress
import json
import random
import threading
from flask import Flask, request, jsonify, abort, render_template
from werkzeug.exceptions import HTTPException 
import requests 
import traceback 
from typing import List 
import math 
import sqlite3
import uuid # For token generation
import secrets # For token generation

# --- BSVLIB IMPORT ---
from bsvlib import PrivateKey, PublicKey, TxOutput, TxInput, Unspent 
from bsvlib.script import Script 
from bsvlib.script.type import P2pkhScriptType 
from bsvlib.transaction import Transaction
from bsvlib.constants import P2PKH_DUST_LIMIT 

# --- MODULE-LEVEL GLOBALS ---
device_private_key_obj_loaded: PrivateKey | None = None 
device_public_key_obj_loaded: PublicKey | None = None 
device_locking_script_hex_loaded: str | None = None 
DEVICE_PAYMENT_ADDRESS: str = "1DefaultPlaceholderAddress" 

# --- DATABASE CONFIG ---
DATABASE_NAME = "sensora_device.db"

# --- TOKEN CONFIG ---
TOKEN_EXPIRY_SECONDS = 3600 # 1 hour
TOKEN_MAX_USAGE = 3 # Allow a few downloads per token
# --- ADVERTISEMENT CONFIG ---
DEVICE_GEOHASH: str = "u4pruydq" # Example Geohash (London). Empty string or None if not used. Max 12 chars for this example.
ADVERTISEMENT_INTERVAL_SECONDS: int = 12 * 3600 # Advertise every 12 hours
ADVERTISEMENT_PREFIX: bytes = b'SNSRADV1' # ASCII "SNSRADV1"
# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
DEVICE_IPV6_ADDRESS: str = "2a01:4f8:1c1b:83c3::1" 
DEVICE_PORT: int = 8080 
DEVICE_WIF: str = "KyuXbxKpRugMxJzHhJq4oUYJaxmFBnSLDNtjoccakDC9ZRMZzcS5" # YOUR ACTUAL WIF

PRICE_SATS_PER_READING: int = 150
DATA_CAPTURE_INTERVAL_SECONDS: int = 300 
BSV_FEE_SATOSHIS_PER_BYTE: float = 0.02

DATA_TYPE_TEMP_HUMID: int = 0x0001

SENSORA_PREFIX: bytes = b'SNSR'
SENSORA_VERSION: int = 0x01
SENSORA_DEVICE_ID_TYPE_IPV6_FULL: int = 0x01
# --- Add to CONFIGURATION ---
DEVICE_GEOHASH = "u4pruydq" # Example Geohash (London). Empty string or None if not used.
ADVERTISEMENT_INTERVAL_SECONDS = 12 * 3600 # Advertise every 12 hours

# --- New function in sensor_agent.py ---
def create_advertisement_op_return_payload() -> bytes | None:
    global DEVICE_IPV6_ADDRESS, DEVICE_PORT, DATA_TYPE_TEMP_HUMID, PRICE_SATS_PER_READING, DEVICE_GEOHASH
    
    prefix = b'SNSRADV1'
    device_id_type = struct.pack('>B', SENSORA_DEVICE_ID_TYPE_IPV6_FULL) # Assuming SENSORA_DEVICE_ID_TYPE_IPV6_FULL is 0x01
    device_id_bytes = get_device_id_bytes(DEVICE_IPV6_ADDRESS)
    port_bytes = struct.pack('>H', DEVICE_PORT)

    geohash_bytes = b''
    if DEVICE_GEOHASH and isinstance(DEVICE_GEOHASH, str):
        geohash_bytes = DEVICE_GEOHASH.encode('ascii')
        if len(geohash_bytes) > 12: # Max length for geohash, for example
            geohash_bytes = geohash_bytes[:12]
    geohash_len_byte = struct.pack('>B', len(geohash_bytes))

    # Define services offered by this device
    services = [
        {"data_type": DATA_TYPE_TEMP_HUMID, "price_sats": PRICE_SATS_PER_READING},
        # Add more services here if the device offers them
        # {"data_type": 0x0002, "price_sats": 200}, 
    ]
    num_services_byte = struct.pack('>B', len(services))
    
    service_bytes_list = []
    for service in services:
        service_bytes_list.append(struct.pack('>H', service["data_type"]))
        service_bytes_list.append(struct.pack('>I', service["price_sats"]))
    
    all_service_bytes = b''.join(service_bytes_list)
    timestamp_bytes = struct.pack('>Q', int(time.time()))

    op_return_parts = [
        prefix, device_id_type, device_id_bytes, port_bytes,
        geohash_len_byte, geohash_bytes, 
        num_services_byte, all_service_bytes,
        timestamp_bytes
    ]
    return b''.join(op_return_parts)

def broadcast_advertisement_tx():
    global device_private_key_obj_loaded, device_public_key_obj_loaded, device_locking_script_hex_loaded
    
    if not (device_private_key_obj_loaded and device_public_key_obj_loaded and device_locking_script_hex_loaded):
        print("[ADV_TX] Keys or locking script not loaded. Cannot broadcast advertisement.")
        return False

    print("[ADV_TX] Creating advertisement OP_RETURN payload...")
    adv_op_return_payload = create_advertisement_op_return_payload()
    if not adv_op_return_payload:
        print("[ADV_TX] Failed to create advertisement payload.")
        return False
    
    print(f"[ADV_TX] Advertisement payload hex: {adv_op_return_payload.hex()}")
    print(f"[ADV_TX] Attempting to broadcast advertisement transaction...")
    
    # Use the existing BSV transaction functions
    txid = create_and_broadcast_op_return_tx(
        device_private_key_obj_loaded,
        device_public_key_obj_loaded,
        device_locking_script_hex_loaded,
        adv_op_return_payload
    )

    if txid:
        print(f"[ADV_TX] Advertisement transaction broadcasted successfully! TXID: {txid}")
        return True
    else:
        print("[ADV_TX] Failed to broadcast advertisement transaction.")
        return False

def advertisement_loop():
    print("[ADV_LOOP] Advertisement loop starting...")
    while True:
        broadcast_advertisement_tx()
        print(f"[ADV_LOOP] Next advertisement in {ADVERTISEMENT_INTERVAL_SECONDS / 3600:.1f} hours.")
        time.sleep(ADVERTISEMENT_INTERVAL_SECONDS)


# -----------------------------------------------------------------------------
# DATABASE HELPER FUNCTIONS
# -----------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row 
    return conn

def init_db():
    conn = None 
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        print("[DB_INIT] Attempting to create table sensor_readings...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings (
                reading_id INTEGER PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                value_temp REAL,
                value_humid REAL,
                data_type_code INTEGER NOT NULL,
                op_return_payload_hex TEXT NOT NULL,
                onchain_proof_txid TEXT NOT NULL UNIQUE,
                paid INTEGER DEFAULT 0,
                consumer_payment_txid TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("[DB_INIT] CREATE TABLE sensor_readings statement executed.")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_onchain_proof_txid ON sensor_readings (onchain_proof_txid);")
        print("[DB_INIT] CREATE INDEX idx_onchain_proof_txid statement executed.")

        print("[DB_INIT] Attempting to create table access_tokens...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS access_tokens (
                token_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reading_id INTEGER NOT NULL,
                payment_txid TEXT NOT NULL, 
                access_token TEXT NOT NULL UNIQUE,
                expiry_timestamp INTEGER NOT NULL,
                usage_count INTEGER DEFAULT 0,
                max_usage INTEGER DEFAULT 1, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (reading_id) REFERENCES sensor_readings (reading_id)
            )
        ''')
        print("[DB_INIT] CREATE TABLE access_tokens statement executed.")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_access_token ON access_tokens (access_token);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_reading_id ON access_tokens (reading_id);")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_reading_payment ON access_tokens (reading_id, payment_txid);")
        print("[DB_INIT] CREATE INDEX for access_tokens statements executed.")

        conn.commit()
        print(f"[DB_INIT] Database '{DATABASE_NAME}' initialized/checked successfully. Tables should exist.")
    except Exception as e:
        print(f"[DB_INIT] CRITICAL ERROR during database initialization: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()

# -----------------------------------------------------------------------------
# TOKEN HELPER
# -----------------------------------------------------------------------------
def generate_access_token():
    return str(uuid.uuid4()) + "-" + secrets.token_hex(16)

# -----------------------------------------------------------------------------
# OP_RETURN HELPER FUNCTIONS
# -----------------------------------------------------------------------------
def get_device_id_bytes(ipv6_string: str) -> bytes:
    return ipaddress.IPv6Address(ipv6_string).packed

def format_float_for_hash(value: float, precision: int = 1) -> str:
    return f"{value:.{precision}f}"

def create_sensora_op_return_payload(device_ipv6_str: str, reading_timestamp_int: int, data_type_code: int, sensor_values_tuple: tuple) -> bytes:
    device_id_bytes = get_device_id_bytes(device_ipv6_str)
    formatted_values = []
    if data_type_code == DATA_TYPE_TEMP_HUMID:
        formatted_values.append(format_float_for_hash(sensor_values_tuple[0]))
        formatted_values.append(format_float_for_hash(sensor_values_tuple[1]))
    else:
        raise ValueError(f"Unsupported data_type_code for hashing: {data_type_code}")
    data_to_hash_str = f"{reading_timestamp_int}:" + ":".join(formatted_values)
    data_hash = hashlib.sha256(data_to_hash_str.encode('utf-8')).digest()
    op_return_parts = [
        SENSORA_PREFIX, struct.pack('>B', SENSORA_VERSION), struct.pack('>B', SENSORA_DEVICE_ID_TYPE_IPV6_FULL),
        device_id_bytes, struct.pack('>Q', reading_timestamp_int), struct.pack('>H', data_type_code), data_hash
    ]
    return b''.join(op_return_parts)

# -----------------------------------------------------------------------------
# BSV INTERACTION IMPLEMENTATIONS (Using bitails.io)
# -----------------------------------------------------------------------------
def get_bsv_private_key_object(wif_string: str) -> PrivateKey | None:
    # ... (same as before) ...
    if not wif_string or wif_string in ["YOUR_PRIVATE_KEY_WIF_GOES_HERE", "xxx"]:
        print(f"CRITICAL: DEVICE_WIF is not set or is a placeholder: '{wif_string}'")
        return None
    if wif_string == "KyuXbxKpRugMxJzHhJq4oUYJaxmFBnSLDNtjoccakDC9ZRMZzcS5": 
        print(f"INFO: Using example WIF: '{wif_string[:5]}...'. Ensure it's funded for real tests.")
    try:
        return PrivateKey(wif_string) 
    except Exception as e:
        print(f"Error creating PrivateKey from WIF ('{wif_string[:5]}...'): {e}")
        traceback.print_exc()
        return None

def get_bsv_public_key_object(private_key_obj: PrivateKey) -> PublicKey | None:
    # ... (same as before) ...
    if private_key_obj:
        try:
            return private_key_obj.public_key()
        except Exception as e:
            print(f"Error deriving public key from private key: {e}")
            traceback.print_exc()
            return None
    return None

def get_address_and_locking_script(public_key_obj: PublicKey) -> tuple[str | None, str | None]:
    # ... (same as before) ...
    if public_key_obj:
        try:
            address_str = public_key_obj.address() 
            locking_script_hex = public_key_obj.locking_script().hex()
            return address_str, locking_script_hex
        except Exception as e:
            print(f"Error deriving address/locking script from public key: {e}")
            traceback.print_exc()
            return None, None
    return None, None

def find_spendable_utxos_bitails(
    address_str: str, 
    amount_needed_sats: int, 
    expected_locking_script_hex: str | None,
    private_keys_for_address: List[PrivateKey] | None 
) -> tuple[list[Unspent], int]:
    # ... (same as before) ...
    if not expected_locking_script_hex:
        print("[BSV] CRITICAL: Expected locking script hex not provided to find_spendable_utxos_bitails.")
        return [], 0
    if not private_keys_for_address: 
        print("[BSV] CRITICAL: Private keys not provided to find_spendable_utxos_bitails for signing.")
        return [],0
        
    utxos_url = f"https://api.bitails.io/address/{address_str}/unspent"
    print(f"[BSV] Fetching UTXOs from: {utxos_url}")
    try:
        response = requests.get(utxos_url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "unspent" not in data or not isinstance(data["unspent"], list):
            print(f"[BSV] No UTXOs found or unexpected format for address {address_str} via bitails.io. Response: {data}")
            return [], 0

        parsed_unspents = []
        total_sats_found = 0
        for utxo_data in data["unspent"]:
            try:
                satoshis_val_from_api = utxo_data.get('satoshis')
                if satoshis_val_from_api is None:
                    print(f"[BSV] Warning: UTXO {utxo_data.get('txid')}:{utxo_data.get('vout')} has no 'satoshis' field or it's null. Skipping.")
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
                print(f"[BSV] Error processing UTXO data {utxo_data.get('txid')}:{utxo_data.get('vout')}: {e}")
                traceback.print_exc()
                continue
        if not parsed_unspents: print(f"[BSV] No suitable UTXOs after parsing for address {address_str}")
        return parsed_unspents, total_sats_found
    except requests.exceptions.RequestException as e: print(f"[BSV] Error fetching UTXOs from bitails.io: {e}")
    except json.JSONDecodeError as e: print(f"[BSV] Error decoding JSON from bitails.io UTXO response: {e}")
    except Exception as e:
        print(f"[BSV] Unexpected error in find_spendable_utxos_bitails: {e}")
        traceback.print_exc()
    return [], 0

def broadcast_bsv_transaction_bitails(raw_tx_hex: str) -> str | None:
    # ... (same as before) ...
    broadcast_url = "https://api.bitails.io/tx/broadcast"
    headers = {"Content-Type": "application/json"}
    payload = {"raw": raw_tx_hex} 
    print(f"[BSV] Broadcasting TX to bitails.io: {raw_tx_hex[:60]}...")
    try:
        response = requests.post(broadcast_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if "txid" in data and data["txid"]:
            print(f"[BSV] Broadcast successful via bitails.io. TXID: {data['txid']}")
            return data['txid']
        else:
            error_msg = data.get("message", "Unknown error from bitails.io broadcast") if isinstance(data, dict) else str(data)
            print(f"[BSV] Broadcast to bitails.io failed or TXID not returned. Response: {data}. Error: {error_msg}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"[BSV] Error broadcasting transaction via bitails.io: {e}")
        if e.response is not None: print(f"[BSV] bitails.io error response: {e.response.text}")
    except json.JSONDecodeError as e: print(f"[BSV] Error decoding JSON from bitails.io broadcast response: {e}")
    except Exception as e:
        print(f"[BSV] Unexpected error in broadcast_bsv_transaction_bitails: {e}")
        traceback.print_exc()
    return None

def create_and_broadcast_op_return_tx(priv_key_obj: PrivateKey, pub_key_obj: PublicKey, source_locking_script_hex: str, op_return_data: bytes) -> str | None:
    # ... (same as before) ...
    if not (priv_key_obj and pub_key_obj and source_locking_script_hex):
        print("[BSV] Missing private key, public key, or locking script for OP_RETURN tx.")
        return None

    source_address_str = pub_key_obj.address() 
    if not source_address_str:
        print("[BSV] Could not derive source address from public key.")
        return None

    estimated_tx_size = 148 + (9 + len(op_return_data)) + 34 + 10 
    min_fee_needed = int(estimated_tx_size * BSV_FEE_SATOSHIS_PER_BYTE)
    if min_fee_needed == 0 and BSV_FEE_SATOSHIS_PER_BYTE > 0: min_fee_needed = 1
    
    print(f"[BSV] Attempting to create OP_RETURN for {source_address_str}. Estimated fee: {min_fee_needed} sats.")
    
    available_unspents, total_sats_in_unspents = find_spendable_utxos_bitails(
        source_address_str, 
        min_fee_needed, 
        source_locking_script_hex,
        [priv_key_obj] 
    )

    if not available_unspents or total_sats_in_unspents < min_fee_needed:
        print(f"[BSV] Insufficient funds for OP_RETURN. Available: {total_sats_in_unspents}, Need approx: {min_fee_needed} for fee.")
        print(f"[BSV] Please send some BSV to {source_address_str}")
        return None

    available_unspents.sort(key=lambda u: u.satoshi)
    
    selected_unspents_for_tx: list[Unspent] = []
    sats_in_selected_inputs = 0
    for unspent_item in available_unspents:
        selected_unspents_for_tx.append(unspent_item)
        sats_in_selected_inputs += unspent_item.satoshi
        if sats_in_selected_inputs >= min_fee_needed: break 
    
    if sats_in_selected_inputs < min_fee_needed:
        print(f"[BSV] Could not select enough UTXOs. Selected: {sats_in_selected_inputs}, Need: {min_fee_needed}")
        return None

    try:
        if not isinstance(op_return_data, bytes):
            raise ValueError(f"op_return_data must be bytes, got {type(op_return_data)}")
        script_bytes_list = [b'\x00', b'\x6a'] 
        data_len = len(op_return_data)
        if data_len <= 75: script_bytes_list.append(bytes([data_len])) 
        elif data_len <= 255: script_bytes_list.extend([b'\x4c', bytes([data_len])]) 
        elif data_len <= 65535: script_bytes_list.extend([b'\x4d', struct.pack('<H', data_len)]) 
        else: script_bytes_list.extend([b'\x4e', struct.pack('<I', data_len)])
        script_bytes_list.append(op_return_data)
        serialized_script_bytes = b''.join(script_bytes_list)
        op_return_script = Script(serialized_script_bytes) 
    except Exception as e_scr:
        print(f"[BSV] Error creating OP_RETURN Script object: {e_scr}")
        traceback.print_exc()
        return None
        
    outputs_for_tx = [TxOutput(out=op_return_script, satoshi=0)] 
    
    tx_obj = Transaction(tx_outputs=outputs_for_tx, fee_rate=BSV_FEE_SATOSHIS_PER_BYTE)
    tx_obj.add_inputs(selected_unspents_for_tx) 

    print(f"[BSV DEBUG] Before add_change - Num Outputs in tx_obj.tx_outputs: {len(tx_obj.tx_outputs)}")
    if tx_obj.tx_outputs:
         print(f"[BSV DEBUG] Output 0 before add_change: {tx_obj.tx_outputs[0].satoshi} sats, script: {tx_obj.tx_outputs[0].locking_script.hex()[:40]}")

    try:
        tx_obj.add_change(change_address=source_address_str) 
        print(f"[BSV DEBUG] After add_change - Num Outputs: {len(tx_obj.tx_outputs)}")
        for i, out_obj_debug in enumerate(tx_obj.tx_outputs):
            print(f"[BSV DEBUG] Output {i} after add_change: {out_obj_debug.satoshi} sats to script {out_obj_debug.locking_script.hex()[:40]}...")
    except Exception as e_change:
        print(f"[BSV] Error during tx_obj.add_change(): {e_change}")
        traceback.print_exc()
        return None 
    
    try:
        tx_obj.sign() 
        
        final_tx_fee = tx_obj.fee()
        raw_tx_bytes = tx_obj.serialize() 
        print(f"[BSV] Transaction constructed. Size: {len(raw_tx_bytes)} bytes. Final Fee: {final_tx_fee} sats.")
        
        print(f"[BSV DEBUG] FINAL Num Outputs: {len(tx_obj.tx_outputs)}")
        for i, out_obj_debug_final in enumerate(tx_obj.tx_outputs):
            print(f"[BSV DEBUG] FINAL Output {i}: {out_obj_debug_final.satoshi} sats to script {out_obj_debug_final.locking_script.hex()[:40]}...")

        total_output_value_final = sum(o.satoshi for o in tx_obj.tx_outputs) 
        if sats_in_selected_inputs != total_output_value_final + final_tx_fee:
             print(f"[BSV] Sanity Check Warning: Inputs ({sats_in_selected_inputs}) != Outputs ({total_output_value_final}) + Fee ({final_tx_fee}). Delta: {sats_in_selected_inputs - (total_output_value_final + final_tx_fee)}")
        if final_tx_fee < 0:
             print(f"[BSV] Critical Error: Negative fee ({final_tx_fee}).")
             return None
    except Exception as e:
        print(f"[BSV] Error signing transaction: {e}")
        traceback.print_exc()
        return None
        
    print(f"[BSV RAW TX HEX TO BROADCAST]: {tx_obj.raw()}")
    return broadcast_bsv_transaction_bitails(tx_obj.raw()) 

def parse_op_return_data_from_hex(op_return_script_hex: str) -> bytes | None:
    # ... (same as before) ...
    try:
        if op_return_script_hex.startswith("006a"): 
            script_bytes = bytes.fromhex(op_return_script_hex[4:]) 
        elif op_return_script_hex.startswith("6a"): 
            script_bytes = bytes.fromhex(op_return_script_hex[2:]) 
        else:
            return None 

        if not script_bytes:
            return b'' 

        opcode = script_bytes[0]
        if 0x01 <= opcode <= 0x4b: 
            length_to_read = opcode
            if len(script_bytes) < 1 + length_to_read: return None 
            return script_bytes[1 : 1 + length_to_read]
        elif opcode == 0x4c: 
            if len(script_bytes) < 2: return None 
            length_to_read = script_bytes[1]
            if len(script_bytes) < 2 + length_to_read: return None 
            return script_bytes[2 : 2 + length_to_read]
        elif opcode == 0x4d: 
            if len(script_bytes) < 3: return None 
            length_to_read = int.from_bytes(script_bytes[1:3], 'little')
            if len(script_bytes) < 3 + length_to_read: return None 
            return script_bytes[3 : 3 + length_to_read]
        elif opcode == 0x4e: 
            if len(script_bytes) < 5: return None 
            length_to_read = int.from_bytes(script_bytes[1:5], 'little')
            if len(script_bytes) < 5 + length_to_read: return None 
            return script_bytes[5 : 5 + length_to_read]
        else: 
            return None
            
    except Exception as e:
        print(f"[BSV_PARSE] Error parsing OP_RETURN hex '{op_return_script_hex[:20]}...': {e}")
        return None

def check_payment_on_chain(
    reading_id_to_check: int, 
    expected_sats: int, 
    payment_to_address: str, 
    device_p2pkh_script_hex: str,
    specific_txid_to_check: str | None = None # MODIFIED: Added optional param
) -> tuple[bool, str | None]:
    
    candidate_tx_ids = []
    if specific_txid_to_check:
        print(f"[BSV_PAY_CHECK] Verifying specific TXID: {specific_txid_to_check} for reading {reading_id_to_check}")
        candidate_tx_ids = [specific_txid_to_check]
    else:
        history_url = f"https://api.bitails.io/address/{payment_to_address}/history"
        print(f"[BSV_PAY_CHECK] Fetching history for {payment_to_address} to check payment for reading {reading_id_to_check}")
        try:
            history_response = requests.get(history_url, timeout=15)
            history_response.raise_for_status()
            history_data = history_response.json()
            if "history" not in history_data or not isinstance(history_data["history"], list):
                print(f"[BSV_PAY_CHECK] No history found or unexpected format for {payment_to_address}.")
                return False, None
            for tx_summary in history_data["history"]:
                if tx_summary.get("outputSatoshis", 0) >= expected_sats:
                    candidate_tx_ids.append(tx_summary["txid"])
                    if len(candidate_tx_ids) >= 10: break 
        except requests.exceptions.RequestException as e_history:
            print(f"[BSV_PAY_CHECK] Error fetching history from bitails.io: {e_history}")
            return False, None 
        except json.JSONDecodeError as e_json_history:
            print(f"[BSV_PAY_CHECK] Error decoding JSON from bitails.io history response: {e_json_history}")
            return False, None
    
    if not candidate_tx_ids:
        print(f"[BSV_PAY_CHECK] No candidate payment TXIDs found.")
        return False, None

    print(f"[BSV_PAY_CHECK] Found {len(candidate_tx_ids)} candidate payment TXIDs. Fetching full details...")
    expected_op_return_str = f"SENSORA_PAY:{reading_id_to_check}"

    for txid in candidate_tx_ids:
        tx_detail_url = f"https://api.bitails.io/tx/{txid}"
        print(f"[BSV_PAY_CHECK] Getting details for TXID: {txid}")
        try:
            detail_response = requests.get(tx_detail_url, timeout=15)
            detail_response.raise_for_status()
            tx_detail = detail_response.json()

            if "outputs" not in tx_detail or not isinstance(tx_detail["outputs"], list):
                print(f"[BSV_PAY_CHECK] TX {txid} has no 'outputs' array or unexpected format.")
                continue

            payment_to_us_found_in_tx = False
            correct_op_return_found_in_tx = False

            for vout_item in tx_detail["outputs"]:
                output_script_hex = vout_item.get("script")
                output_satoshis = vout_item.get("satoshis", 0)

                if not output_script_hex:
                    continue

                if output_script_hex == device_p2pkh_script_hex and output_satoshis >= expected_sats:
                    payment_to_us_found_in_tx = True
                
                if output_script_hex.startswith("6a") or output_script_hex.startswith("006a"): 
                    op_return_data_bytes = parse_op_return_data_from_hex(output_script_hex)
                    if op_return_data_bytes is not None:
                        try:
                            op_return_data_str = op_return_data_bytes.decode('utf-8')
                            if op_return_data_str == expected_op_return_str:
                                correct_op_return_found_in_tx = True
                        except UnicodeDecodeError:
                            pass 
            
            if payment_to_us_found_in_tx and correct_op_return_found_in_tx:
                print(f"[BSV_PAY_CHECK] SUCCESS! Payment confirmed for reading {reading_id_to_check} in TX {txid}.")
                return True, txid

        except requests.exceptions.HTTPError as e_http_detail:
            if e_http_detail.response.status_code == 404:
                print(f"[BSV_PAY_CHECK] TX {txid} not found on bitails.io (404).")
            else:
                print(f"[BSV_PAY_CHECK] HTTP error fetching tx details for {txid}: {e_http_detail}")
        except requests.exceptions.RequestException as e_detail:
            print(f"[BSV_PAY_CHECK] Network error fetching tx details for {txid}: {e_detail}")
        except json.JSONDecodeError as e_json_detail:
            print(f"[BSV_PAY_CHECK] Error decoding JSON for tx details {txid}: {e_json_detail}")
        except Exception as e_tx_proc:
            print(f"[BSV_PAY_CHECK] Error processing tx {txid}: {e_tx_proc}")
            traceback.print_exc()
    
    print(f"[BSV_PAY_CHECK] Payment not confirmed after checking candidate TXs for reading {reading_id_to_check}.")
    return False, None

# -----------------------------------------------------------------------------
# SENSOR SIMULATION
# -----------------------------------------------------------------------------
def get_simulated_sensor_data() -> tuple[int, tuple[float, float], int]:
    timestamp = int(time.time())
    temperature = round(random.uniform(15.0, 30.0), 1)
    humidity = round(random.uniform(40.0, 70.0), 1)
    return timestamp, (temperature, humidity), DATA_TYPE_TEMP_HUMID

# -----------------------------------------------------------------------------
# FLASK APP
# -----------------------------------------------------------------------------
app = Flask(__name__)
class PaymentRequiredError(HTTPException):
    code = 402
    description = 'Payment for this resource has not been detected.'

@app.errorhandler(PaymentRequiredError)
def handle_payment_required(error: PaymentRequiredError):
    response = jsonify({"error": "Payment Required", "message": error.description, "status_code": error.code})
    response.status_code = error.code
    return response

@app.errorhandler(404)
def not_found_error_handler(error: HTTPException):
    desc = error.description if hasattr(error, 'description') and error.description else "The requested resource was not found."
    response = jsonify({"error": "Not Found", "message": desc, "status_code": 404})
    response.status_code = 404
    return response

@app.errorhandler(400)
def bad_request_error_handler(error: HTTPException):
    desc = error.description if hasattr(error, 'description') and error.description else "The request was malformed."
    response = jsonify({"error": "Bad Request", "message": desc, "status_code": 400})
    response.status_code = 400
    return response

@app.route("/", methods=['GET']) 
def dashboard():
    global DEVICE_IPV6_ADDRESS, DEVICE_PAYMENT_ADDRESS, PRICE_SATS_PER_READING, DATA_TYPE_TEMP_HUMID
    readings_from_db = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sensor_readings ORDER BY reading_id DESC LIMIT 20")
        rows = cursor.fetchall()
        for row in rows:
            reading_dict = dict(row) 
            if row["data_type_code"] == DATA_TYPE_TEMP_HUMID and \
               row["value_temp"] is not None and \
               row["value_humid"] is not None:
                reading_dict["sensor_values"] = (row["value_temp"], row["value_humid"])
            else:
                reading_dict["sensor_values"] = None
            readings_from_db.append(reading_dict)
    except Exception as e:
        print(f"Error fetching readings for dashboard: {e}")
        traceback.print_exc()
    finally:
        if conn: conn.close()
    auto_refresh_seconds = 30 
    current_year_str = time.strftime('%Y')
    return render_template(
        "index.html", 
        device_ipv6=DEVICE_IPV6_ADDRESS,
        device_payment_address=DEVICE_PAYMENT_ADDRESS,
        price_sats=PRICE_SATS_PER_READING,
        readings=readings_from_db,
        DATA_TYPE_TEMP_HUMID=DATA_TYPE_TEMP_HUMID, 
        auto_refresh_interval=auto_refresh_seconds,
        current_year=current_year_str
    )

@app.route("/price", methods=['GET'])
def get_price_route():
    global DEVICE_PAYMENT_ADDRESS 
    latest_reading_id_from_db = None
    conn = None 
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(reading_id) FROM sensor_readings")
        result = cursor.fetchone()
        if result and result[0] is not None:
            latest_reading_id_from_db = result[0]
        else: 
            latest_reading_id_from_db = int(time.time()) 
    except Exception as e:
        print(f"Error fetching latest reading_id from DB for /price: {e}")
        traceback.print_exc()
        latest_reading_id_from_db = int(time.time()) 
    finally:
        if conn: conn.close()
    return jsonify({
        "current_reading_id": str(latest_reading_id_from_db), 
        "price_sats": PRICE_SATS_PER_READING,
        "payment_address": DEVICE_PAYMENT_ADDRESS,
        "payment_instructions": f"Send {PRICE_SATS_PER_READING} sats to {DEVICE_PAYMENT_ADDRESS}. OP_RETURN: 'SENSORA_PAY:{latest_reading_id_from_db}'."
    })

# --- NEW TOKEN-BASED ACCESS CONTROL ENDPOINTS ---

@app.route("/claim_reading/<string:reading_id_str>", methods=['POST'])
def claim_reading_route(reading_id_str: str):
    global DEVICE_PAYMENT_ADDRESS, device_locking_script_hex_loaded, PRICE_SATS_PER_READING, TOKEN_EXPIRY_SECONDS, TOKEN_MAX_USAGE

    try:
        reading_id = int(reading_id_str)
    except ValueError:
        abort(400, description="Invalid reading_id format.")

    if not request.is_json:
        abort(400, description="Invalid request: Missing JSON body or incorrect Content-Type.")
    data = request.get_json()
    consumer_payment_txid = data.get("payment_txid")

    if not consumer_payment_txid or not isinstance(consumer_payment_txid, str) or len(consumer_payment_txid) != 64:
        abort(400, description="Missing or invalid 'payment_txid' in request body.")

    print(f"[CLAIM_TOKEN] Attempting to claim reading {reading_id} with payment_txid {consumer_payment_txid[:10]}...")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT reading_id FROM sensor_readings WHERE reading_id = ?", (reading_id,))
        if not cursor.fetchone():
            abort(404, description=f"Reading ID {reading_id} not found.")
        
        cursor.execute("SELECT access_token, expiry_timestamp FROM access_tokens WHERE reading_id = ? AND payment_txid = ?", 
                       (reading_id, consumer_payment_txid))
        existing_token_row = cursor.fetchone()
        if existing_token_row:
            if int(time.time()) < existing_token_row["expiry_timestamp"]:
                print(f"[CLAIM_TOKEN] Token already issued for reading {reading_id}, payment {consumer_payment_txid[:10]}. Returning existing token.")
                return jsonify({
                    "message": "Token already issued for this payment.", "access_token": existing_token_row["access_token"],
                    "data_endpoint": f"/fetch_data/{reading_id}", "expires_at": existing_token_row["expiry_timestamp"]
                }), 200
            else:
                print(f"[CLAIM_TOKEN] Existing token for reading {reading_id} (payment {consumer_payment_txid[:10]}) expired. Will re-check payment.")
    except Exception as e:
        print(f"[CLAIM_TOKEN] DB error checking reading/token for {reading_id}: {e}"); traceback.print_exc()
        abort(500, description="Server error verifying reading/token status.")
    finally:
        if conn: conn.close()

    if not device_locking_script_hex_loaded:
        print("[CLAIM_TOKEN] CRITICAL SERVER ERROR: device_locking_script_hex_loaded is not set.")
        abort(500, description="Server configuration error for payment verification.")

    print(f"[CLAIM_TOKEN] Verifying payment {consumer_payment_txid[:10]} for reading {reading_id} on-chain...")
    payment_verified, _ = check_payment_on_chain(
        reading_id, PRICE_SATS_PER_READING, DEVICE_PAYMENT_ADDRESS, 
        device_locking_script_hex_loaded, specific_txid_to_check=consumer_payment_txid
    )

    if not payment_verified:
        print(f"[CLAIM_TOKEN] Payment verification failed for {consumer_payment_txid[:10]} for reading {reading_id}.")
        abort(402, description="Payment not verified or invalid for the specified reading ID.")

    access_token = generate_access_token()
    expiry_ts = int(time.time()) + TOKEN_EXPIRY_SECONDS
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO access_tokens (reading_id, payment_txid, access_token, expiry_timestamp, max_usage)
            VALUES (?, ?, ?, ?, ?)
        ''', (reading_id, consumer_payment_txid, access_token, expiry_ts, TOKEN_MAX_USAGE))
        
        cursor.execute('''
            UPDATE sensor_readings SET paid = 1, consumer_payment_txid = ? 
            WHERE reading_id = ? AND (paid = 0 OR consumer_payment_txid IS NULL OR consumer_payment_txid != ?)
        ''', (consumer_payment_txid, reading_id, consumer_payment_txid))
        conn.commit()

        print(f"[CLAIM_TOKEN] Token {access_token[:8]}... generated for reading {reading_id}, payment {consumer_payment_txid[:10]}.")
        return jsonify({
            "message": "Access token generated successfully.", "access_token": access_token,
            "data_endpoint": f"/fetch_data/{reading_id}", "expires_at": expiry_ts
        }), 201
    except sqlite3.IntegrityError:
        print(f"[CLAIM_TOKEN] DB IntegrityError on token insert for reading {reading_id}, payment {consumer_payment_txid[:10]}. Race condition or already claimed.")
        abort(409, description="Token for this reading and payment may have just been claimed. Please check status or try fetching with existing token if known.")
    except Exception as e:
        print(f"[CLAIM_TOKEN] DB error storing access token for reading {reading_id}: {e}"); traceback.print_exc()
        abort(500, description="Server error issuing token.")
    finally:
        if conn: conn.close()

@app.route("/fetch_data/<string:reading_id_str>", methods=['GET'])
def fetch_data_route(reading_id_str: str):
    try:
        reading_id = int(reading_id_str)
    except ValueError:
        abort(400, description="Invalid reading_id format.")

    token = request.args.get("token")
    if not token:
        abort(401, description="Access token is missing.")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT token_id, expiry_timestamp, usage_count, max_usage 
            FROM access_tokens WHERE reading_id = ? AND access_token = ?
        """, (reading_id, token))
        token_row = cursor.fetchone()

        if not token_row: abort(403, description="Invalid or non-existent token for this reading ID.")
        if int(time.time()) >= token_row["expiry_timestamp"]:
            print(f"[FETCH_DATA] Token {token[:8]}... for reading {reading_id} has expired.")
            abort(403, description="Access token has expired.")
        if token_row["usage_count"] >= token_row["max_usage"]:
            print(f"[FETCH_DATA] Token {token[:8]}... for reading {reading_id} has exceeded max usage.")
            abort(403, description="Access token has exceeded maximum usage limit.")
        
        cursor.execute("UPDATE access_tokens SET usage_count = usage_count + 1 WHERE token_id = ?", (token_row["token_id"],))
        conn.commit()
        print(f"[FETCH_DATA] Token {token[:8]}... validated for reading {reading_id}. Usage: {token_row['usage_count']+1}/{token_row['max_usage']}.")

    except Exception as e:
        print(f"[FETCH_DATA] DB error validating token for reading {reading_id}: {e}"); traceback.print_exc()
        abort(500, description="Server error validating token.")
    finally:
        if conn: conn.close()

    conn_data = None
    try:
        conn_data = get_db_connection()
        cursor_data = conn_data.cursor()
        cursor_data.execute("SELECT * FROM sensor_readings WHERE reading_id = ?", (reading_id,))
        reading_db_row = cursor_data.fetchone()
        
        if not reading_db_row:
            print(f"[FETCH_DATA] CRITICAL: Reading ID {reading_id} not found after token validation!")
            abort(404, description=f"Data for reading ID {reading_id} not found (post-token validation).")

        reading_data_dict = dict(reading_db_row)
        reading_data_dict["sensor_values"] = (reading_db_row["value_temp"], reading_db_row["value_humid"]) \
            if reading_db_row["value_temp"] is not None and reading_db_row["value_humid"] is not None else None
        
        return jsonify({
            "reading_id": str(reading_data_dict["reading_id"]), "timestamp": reading_data_dict["timestamp"],
            "sensor_values": reading_data_dict["sensor_values"], "data_type_code": reading_data_dict["data_type_code"],
            "onchain_proof_txid": reading_data_dict["onchain_proof_txid"],
        }), 200
    except Exception as e:
        print(f"[FETCH_DATA] DB error fetching sensor data for reading {reading_id}: {e}"); traceback.print_exc()
        abort(500, description="Server error retrieving data.")
    finally:
        if conn_data: conn_data.close()

# --- OLD /reading route is now effectively replaced by /claim_reading and /fetch_data ---
# @app.route("/reading/<string:reading_id_str>", methods=['GET'])
# def get_reading_route(reading_id_str: str):
#     # This route might be repurposed to just show public status, 
#     # or removed if /fetch_data is the only way to get paid data.
#     # For now, it's commented out to avoid confusion.
#     abort(404, description="This endpoint is deprecated. Use /claim_reading and /fetch_data.")


# -----------------------------------------------------------------------------
# BACKGROUND THREAD
# -----------------------------------------------------------------------------
def sensor_and_stamp_loop():
    # ... (same as before) ...
    global device_private_key_obj_loaded, device_public_key_obj_loaded, device_locking_script_hex_loaded
    print("Sensor and stamp loop starting...")
    if not (device_private_key_obj_loaded and device_public_key_obj_loaded and device_locking_script_hex_loaded):
        print("CRITICAL: Device key objects or locking script not loaded. Stamper thread cannot operate.")
        return 
    while True:
        try:
            reading_ts, value_tuple, data_type = get_simulated_sensor_data()
            reading_id = reading_ts
            print(f"\nCaptured data at {reading_ts}: {value_tuple}, type: {data_type_code_to_name(data_type)}")
            op_return_payload = create_sensora_op_return_payload(DEVICE_IPV6_ADDRESS, reading_ts, data_type, value_tuple)
            print(f"Attempting to stamp reading {reading_id} on-chain...")
            onchain_txid = create_and_broadcast_op_return_tx(
                device_private_key_obj_loaded, 
                device_public_key_obj_loaded, 
                device_locking_script_hex_loaded, 
                op_return_payload
            )
            if onchain_txid:
                print(f"Successfully stamped reading {reading_id} on-chain. TXID: {onchain_txid}")
                conn = None 
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    temp_val, humid_val = None, None
                    if data_type == DATA_TYPE_TEMP_HUMID and len(value_tuple) == 2:
                        temp_val = value_tuple[0]
                        humid_val = value_tuple[1]
                    
                    cursor.execute('''
                        INSERT INTO sensor_readings 
                        (reading_id, timestamp, value_temp, value_humid, data_type_code, 
                         op_return_payload_hex, onchain_proof_txid, paid)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (reading_id, reading_ts, temp_val, humid_val, data_type, 
                          op_return_payload.hex(), onchain_txid, 0)) 
                    conn.commit()
                    print(f"Reading {reading_id} saved to database.")
                except sqlite3.IntegrityError as ie: 
                     print(f"DB IntegrityError (likely duplicate onchain_proof_txid '{onchain_txid}'): {ie}. Reading {reading_id} might already exist or TXID collision.")
                except Exception as db_e:
                    print(f"Error saving reading {reading_id} to database: {db_e}")
                    traceback.print_exc()
                finally:
                    if conn:
                        conn.close()
            else: print(f"Failed to stamp reading {reading_id} on-chain. Will retry next cycle.")
        except Exception as e:
            print(f"ERROR in sensor_and_stamp_loop: {e}")
            traceback.print_exc()
        print(f"Sleeping for {DATA_CAPTURE_INTERVAL_SECONDS} seconds...")
        time.sleep(DATA_CAPTURE_INTERVAL_SECONDS)

def data_type_code_to_name(code: int) -> str:
    if code == DATA_TYPE_TEMP_HUMID: return "Temp/Humid"
    return f"Unknown ({code})"
# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # 1. Initialize Database (must be first if other parts might log to it or use it)
    init_db() 
    
    # 2. Load WIF and derive all necessary BSV objects and addresses
    # These globals are used by both stamping and advertisement threads/functions
    temp_priv_key = get_bsv_private_key_object(DEVICE_WIF) 
    if not temp_priv_key:
        print(f"CRITICAL: Could not load device private key from WIF. Please check DEVICE_WIF variable. Exiting.")
        exit(1)
    device_private_key_obj_loaded = temp_priv_key # Set global

    temp_pub_key = get_bsv_public_key_object(device_private_key_obj_loaded)
    if not temp_pub_key:
        print("CRITICAL: Could not derive public key. Exiting.")
        exit(1)
    device_public_key_obj_loaded = temp_pub_key # Set global
    
    derived_addr, derived_script_hex = get_address_and_locking_script(device_public_key_obj_loaded)
    if not (derived_addr and derived_script_hex):
        print("CRITICAL: Could not derive payment address or locking script. Exiting.")
        exit(1)
    DEVICE_PAYMENT_ADDRESS = derived_addr # Set global for Flask routes
    device_locking_script_hex_loaded = derived_script_hex # Set global
    
    # 3. Print device info (after successful key/address setup)
    print(f"Device WIF ('{DEVICE_WIF[:5]}...') loaded.")
    print(f"Payment/Change Address: {DEVICE_PAYMENT_ADDRESS}")
    print(f"Locking Script Hex: {device_locking_script_hex_loaded[:10]}...") 
    print(f"Ensure this address ({DEVICE_PAYMENT_ADDRESS}) has some BSV to pay for OP_RETURNs.")
    print(f"Check UTXOs via bitails: https://api.bitails.io/address/{DEVICE_PAYMENT_ADDRESS}/unspent")
    print(f"Targeting fee rate: {BSV_FEE_SATOSHIS_PER_BYTE} sats/byte")

    # 4. Perform initial sensor advertisement broadcast (requires keys to be loaded)
    print("[MAIN] Broadcasting initial sensor advertisement...")
    initial_adv_success = broadcast_advertisement_tx() # Broadcast once on startup
    if initial_adv_success:
        print("[MAIN] Initial advertisement broadcasted successfully.")
    else:
        print("[MAIN] Initial advertisement broadcast FAILED. Check logs and funding for address.")
        # Optionally, you might not want to proceed if the initial ad fails,
        # or just log it and continue. For now, we continue.

    # 5. Start background threads (after all initializations and initial broadcast)
    
    # Start the periodic advertisement loop thread
    adv_thread = threading.Thread(target=advertisement_loop, daemon=True)
    adv_thread.start()
    print("[MAIN] Periodic advertisement thread started.") # Moved this print here

    # Start the data stamping thread
    stamper_thread = threading.Thread(target=sensor_and_stamp_loop, daemon=True)
    stamper_thread.start()
    print("[MAIN] Data stamper thread started.") # Moved this print here
    
    # 6. Start the Flask API server (last, as it's blocking)
    print(f"Starting Flask API server on ipv6 [{DEVICE_IPV6_ADDRESS}]:{DEVICE_PORT} (listening on [::]:{DEVICE_PORT})")
    app.run(host="::", port=DEVICE_PORT, debug=False)