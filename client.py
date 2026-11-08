#!/usr/bin/env python3

import requests
import time
import sys
import json
from bsvlib import PrivateKey, PublicKey, TxOutput, TxInput, Unspent, Transaction 
from bsvlib.script import Script # Keep for OP_RETURN script
from bsvlib.script.type import P2pkhScriptType
import traceback 

# --- Configuration ---
BSV_FEE_SATOSHIS_PER_BYTE_CONSUMER = 0.1

# --- Bitails API interaction functions (Keep as they are, they seem to work) ---
def find_spendable_utxos_for_consumer(
    address_str: str, 
    amount_needed_sats: int, 
    expected_locking_script_hex: str, 
    private_keys_for_address: list[PrivateKey]
) -> tuple[list[Unspent], int]:
    # ... (Working implementation from your previous client.py) ...
    utxos_url = f"https://api.bitails.io/address/{address_str}/unspent"
    print(f"[CONSUMER_BSV] Fetching UTXOs for consumer {address_str} from: {utxos_url}")
    try:
        response = requests.get(utxos_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if "unspent" not in data or not isinstance(data["unspent"], list):
            print(f"[CONSUMER_BSV] No UTXOs found or unexpected format for address {address_str}. Response: {data}")
            return [], 0
        parsed_unspents = []
        total_sats_found = 0
        for utxo_data in data["unspent"]:
            try:
                satoshis_val_from_api = utxo_data.get('satoshis')
                if satoshis_val_from_api is None: continue
                unspent_obj = Unspent(
                    txid=utxo_data['txid'], vout=int(utxo_data['vout']),
                    script_pubkey_hex=expected_locking_script_hex, 
                    satoshi=int(satoshis_val_from_api),
                    script_type=P2pkhScriptType(), 
                    private_keys=private_keys_for_address
                )
                parsed_unspents.append(unspent_obj)
                total_sats_found += unspent_obj.satoshi
            except Exception as e:
                print(f"[CONSUMER_BSV] Error processing UTXO {utxo_data.get('txid')}:{utxo_data.get('vout')}: {e}")
                continue
        return parsed_unspents, total_sats_found
    except Exception as e:
        print(f"[CONSUMER_BSV] Error in find_spendable_utxos_for_consumer: {e}")
        traceback.print_exc()
    return [], 0

def broadcast_bsv_transaction_consumer(raw_tx_hex: str) -> str | None:
    # ... (Working implementation from your previous client.py) ...
    broadcast_url = "https://api.bitails.io/tx/broadcast"
    headers = {"Content-Type": "application/json"}
    payload = {"raw": raw_tx_hex}
    print(f"[CONSUMER_BSV] Broadcasting consumer payment TX to bitails.io: {raw_tx_hex[:60]}...")
    try:
        response = requests.post(broadcast_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if "txid" in data and data["txid"]:
            print(f"[CONSUMER_BSV] Broadcast successful. TXID: {data['txid']}")
            return data['txid']
        else:
            print(f"[CONSUMER_BSV] Broadcast failed or TXID not returned. Response: {data}")
            return None
    except Exception as e:
        print(f"[CONSUMER_BSV] Error broadcasting consumer TX: {e}")
        traceback.print_exc()
    return None

def main(sensor_agent_ipv6: str, sensor_agent_port: int, reading_id_to_buy_str: str, consumer_wif_str: str):
    base_api_url = f"http://[{sensor_agent_ipv6}]:{sensor_agent_port}"
    print(f"Consumer client starting. Targeting sensor agent at {base_api_url}")
    print(f"Attempting to buy reading_id: {reading_id_to_buy_str}")

    if not consumer_wif_str or consumer_wif_str == "LYourConsumerPrivateKeyWIFGoesHere":
        print("CRITICAL: CONSUMER_WIF is not set. Please provide a valid WIF via command line.")
        return

    try:
        consumer_priv_key = PrivateKey(consumer_wif_str)
        consumer_pub_key = consumer_priv_key.public_key()
        consumer_address_str = consumer_pub_key.address()
        consumer_locking_script_hex = consumer_pub_key.locking_script().hex()
        print(f"Consumer Wallet Address: {consumer_address_str}")
    except Exception as e:
        print(f"Error with consumer WIF or key derivation: {e}"); traceback.print_exc(); return

    try:
        price_url = f"{base_api_url}/price"
        print(f"Fetching price info from: {price_url}")
        response_price = requests.get(price_url, timeout=5)
        response_price.raise_for_status()
        price_data = response_price.json()
        device_payment_address = price_data["payment_address"]
        price_sats = int(price_data["price_sats"])
        print(f"Sensor Agent Payment Address: {device_payment_address}, Price: {price_sats} sats")
    except Exception as e: print(f"Error fetching /price: {e}"); traceback.print_exc(); return

    op_return_payment_string = f"SENSORA_PAY:{reading_id_to_buy_str}"
    op_return_payment_bytes = op_return_payment_string.encode('utf-8')
    try:
        script_bytes_list = [b'\x00', b'\x6a']
        data_len = len(op_return_payment_bytes)
        if data_len <= 75: script_bytes_list.append(bytes([data_len]))
        elif data_len <= 255: script_bytes_list.extend([b'\x4c', bytes([data_len])])
        else: print("OP_RETURN data too large for simple push."); return
        script_bytes_list.append(op_return_payment_bytes)
        payment_op_return_script_obj = Script(b''.join(script_bytes_list)) # This way of creating OP_RETURN script is fine
    except Exception as e_scr: print(f"Error creating payment OP_RETURN Script: {e_scr}"); traceback.print_exc(); return
    payment_op_return_output = TxOutput(out=payment_op_return_script_obj, satoshi=0)

    estimated_payment_tx_size = 148 + 34 + (9 + len(op_return_payment_bytes)) + 34 + 10 
    estimated_fee_for_payment = int(estimated_payment_tx_size * BSV_FEE_SATOSHIS_PER_BYTE_CONSUMER) or 1
    sats_needed_from_consumer_wallet = price_sats + estimated_fee_for_payment
    print(f"Consumer needs ~{sats_needed_from_consumer_wallet} sats (payment: {price_sats}, est. fee: {estimated_fee_for_payment})")

    available_unspents, total_sats = find_spendable_utxos_for_consumer(
        consumer_address_str, sats_needed_from_consumer_wallet, consumer_locking_script_hex, [consumer_priv_key]
    )
    if not available_unspents or total_sats < sats_needed_from_consumer_wallet:
        print(f"Insufficient funds. Available: {total_sats}, Need: {sats_needed_from_consumer_wallet}."); return

    selected_unspents: list[Unspent] = []
    sats_in_selected = 0
    available_unspents.sort(key=lambda u: u.satoshi)
    for unspent in available_unspents:
        selected_unspents.append(unspent); sats_in_selected += unspent.satoshi
        if sats_in_selected >= sats_needed_from_consumer_wallet: break
    if sats_in_selected < sats_needed_from_consumer_wallet:
        print(f"Could not select enough UTXOs. Selected: {sats_in_selected}, Need: {sats_needed_from_consumer_wallet}"); return
    
    # --- THE ONLY CORRECTED PART ---
    try:
        # Create the TxOutput for payment directly using the address string for the 'out' parameter
        payment_to_device_output = TxOutput(out=device_payment_address, satoshi=price_sats)
    except Exception as e: 
        print(f"Error creating TxOutput for device payment address {device_payment_address}: {e}")
        traceback.print_exc()
        return
    # --- END CORRECTION ---
    
    consumer_tx_outputs = [payment_to_device_output, payment_op_return_output] # Order: P2PKH, then OP_RETURN
    consumer_tx = Transaction(tx_outputs=consumer_tx_outputs, fee_rate=BSV_FEE_SATOSHIS_PER_BYTE_CONSUMER)
    consumer_tx.add_inputs(selected_unspents)
    try:
        consumer_tx.add_change(change_address=consumer_address_str)
        consumer_tx.sign()
        final_consumer_tx_fee = consumer_tx.fee()
        print(f"Consumer payment TX constructed. Size: {len(consumer_tx.serialize())} bytes. Fee: {final_consumer_tx_fee} sats.")
    except Exception as e: 
        print(f"Error constructing/signing consumer TX: {e}")
        traceback.print_exc()
        return
    
    consumer_payment_txid = broadcast_bsv_transaction_consumer(consumer_tx.raw())
    if not consumer_payment_txid: print("Failed to broadcast consumer payment TX."); return
    print(f"Consumer payment TX broadcasted: {consumer_payment_txid}. Waiting to claim token...")
    time.sleep(10) 

    # --- Token Claim and Data Fetch Logic (Keep as provided previously) ---
    try:
        claim_url = f"{base_api_url}/claim_reading/{reading_id_to_buy_str}"
        print(f"Attempting to claim token from: {claim_url} with payment_txid: {consumer_payment_txid}")
        # Proves we are the payer: the payment txid is public, so without this anyone could claim our token.
        claim_signature = consumer_priv_key.sign_text(f"SENSORA_CLAIM:{reading_id_to_buy_str}:{consumer_payment_txid}")[1]
        claim_payload = {"payment_txid": consumer_payment_txid, "signature": claim_signature}
        response_claim = requests.post(claim_url, json=claim_payload, timeout=10)
        response_claim.raise_for_status()
        claim_data = response_claim.json()
        access_token = claim_data.get("access_token")
        data_endpoint = claim_data.get("data_endpoint")
        if not access_token or not data_endpoint:
            print(f"Failed to get access token. Response: {claim_data}"); return
        print(f"Access token received: {access_token[:8]}... Data endpoint: {data_endpoint}")
    except requests.exceptions.HTTPError as e:
        print(f"Error claiming token (HTTP {e.response.status_code}): {e.response.text if e.response else e}"); return
    except Exception as e: print(f"Error claiming token: {e}"); traceback.print_exc(); return

    try:
        fetch_url_full = f"{base_api_url}{data_endpoint}?token={access_token}"
        print(f"Attempting to fetch data from: {fetch_url_full}")
        response_fetch = requests.get(fetch_url_full, timeout=10)
        print(f"Sensor Agent Response Status Code: {response_fetch.status_code}")
        print("Sensor Agent Response Body:")
        try:
            fetched_data = response_fetch.json()
            print(json.dumps(fetched_data, indent=2))
            if response_fetch.status_code == 200:
                print("\nSUCCESS! Data retrieved with token.")
            else:
                print("\nData not retrieved. Check server logs on sensor agent.")
        except json.JSONDecodeError: print(response_fetch.text)
    except Exception as e: print(f"Error fetching data with token: {e}"); traceback.print_exc(); return

if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: python client.py <SENSOR_AGENT_IPV6> <SENSOR_AGENT_PORT> <READING_ID_TO_BUY> <CONSUMER_WIF>")
        sys.exit(1)
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])