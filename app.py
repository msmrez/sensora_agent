# sensora_agent/app.py

import threading
import time
import logging
import sys
from flask import Flask
import random # <--- FIX: IMPORT ADDED

# Import from our new 'src' package
from src import config, database, protocols, bsv_utils, routes

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] %(module)s.%(funcName)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Main Application State Class ---
class DeviceState:
    def __init__(self):
        self.private_key = None
        self.public_key = None
        self.address = None
        self.locking_script_hex = None

device_state = DeviceState()

# --- Sensor Data Simulation ---
def get_simulated_sensor_data() -> tuple[int, tuple[float, float], int]:
    timestamp = int(time.time())
    temperature = round(random.uniform(15.0, 30.0), 1)
    humidity = round(random.uniform(40.0, 70.0), 1)
    return timestamp, (temperature, humidity), config.DATA_TYPE_TEMP_HUMID

def data_type_code_to_name(code: int) -> str:
    if code == config.DATA_TYPE_TEMP_HUMID: return "Temp/Humid"
    return f"Unknown ({code})"

# --- Background Threads Logic ---

def sensor_and_stamp_loop():
    """Periodically captures data, stamps it on-chain, and records metrics."""
    logger.info("Sensor and stamp loop starting...")
    if not (device_state.private_key and device_state.public_key and device_state.locking_script_hex):
        logger.critical("Device keys not loaded. Stamper thread cannot operate.")
        return 
    
    while True:
        try:
            reading_ts, value_tuple, data_type = get_simulated_sensor_data()
            if value_tuple is None:
                logger.warning("Sensor read failed. Skipping this cycle.")
            else:
                logger.info(f"Captured data at {reading_ts}: {value_tuple}, type: {data_type_code_to_name(data_type)}")
                op_return_payload = protocols.create_sensora_op_return_payload(reading_ts, data_type, value_tuple)
                
                logger.info(f"Attempting to stamp reading {reading_ts} on-chain...")
                onchain_txid = bsv_utils.create_and_broadcast_op_return_tx(
                    device_state.private_key, 
                    device_state.public_key, 
                    device_state.locking_script_hex, 
                    op_return_payload
                )
                
                if onchain_txid:
                    # --- RECORD SUCCESS METRIC ---
                    database.increment_key_value('successful_stamp_count')
                    database.set_key_value('last_stamp_timestamp', str(int(time.time())))
                    
                    logger.info(f"Successfully stamped reading {reading_ts} on-chain. TXID: {onchain_txid}")
                    database.save_reading(reading_ts, reading_ts, value_tuple, data_type, op_return_payload.hex(), onchain_txid)
                else:
                    # --- RECORD FAILURE METRIC ---
                    database.increment_key_value('failed_stamp_count')
                    logger.error(f"Failed to stamp reading {reading_ts} on-chain. Will retry next cycle.")

        except Exception as e:
            database.increment_key_value('failed_stamp_count') # Also count exceptions as failures
            logger.exception(f"CRITICAL ERROR in sensor_and_stamp_loop: {e}")
            time.sleep(60) 

        logger.info(f"Stamper thread sleeping for {config.DATA_CAPTURE_INTERVAL_SECONDS} seconds...")
        time.sleep(config.DATA_CAPTURE_INTERVAL_SECONDS)

def broadcast_advertisement_tx() -> bool:
    """Helper function to create and broadcast one advertisement transaction and record metrics."""
    if not (device_state.private_key and device_state.public_key and device_state.locking_script_hex):
        logger.error("[ADV_TX] Keys not loaded. Cannot broadcast advertisement.")
        return False

    logger.info("[ADV_TX] Creating advertisement payload...")
    adv_op_return_payload = protocols.create_advertisement_op_return_payload()
    if not adv_op_return_payload:
        logger.error("[ADV_TX] Failed to create advertisement payload.")
        database.increment_key_value('failed_adv_count')
        return False
    
    logger.info(f"[ADV_TX] Attempting to broadcast advertisement transaction...")
    txid = bsv_utils.create_and_broadcast_op_return_tx(
        device_state.private_key, device_state.public_key, 
        device_state.locking_script_hex, adv_op_return_payload
    )

    if txid:
        # --- RECORD SUCCESS METRIC ---
        logger.info(f"[ADV_TX] Advertisement TX broadcast successful! TXID: {txid}")
        database.set_key_value('last_adv_txid', txid)
        database.set_key_value('last_adv_timestamp', str(int(time.time())))
        database.increment_key_value('successful_adv_count')
        return True
    else:
        # --- RECORD FAILURE METRIC ---
        logger.error("[ADV_TX] Failed to broadcast advertisement transaction.")
        database.increment_key_value('failed_adv_count')
        return False

def advertisement_loop():
    """Periodically broadcasts the sensor's advertisement."""
    logger.info("Advertisement loop starting...")
    while True:
        try:
            broadcast_advertisement_tx()
        except Exception as e:
            logger.exception(f"CRITICAL ERROR in advertisement_loop: {e}")
        
        logger.info(f"[ADV_LOOP] Next advertisement in {config.ADVERTISEMENT_INTERVAL_SECONDS / 3600:.1f} hours.")
        time.sleep(config.ADVERTISEMENT_INTERVAL_SECONDS)

# --- MAIN EXECUTION BLOCK ---
if __name__ == "__main__":
    logger.info("--- Starting Sensōra Device Agent ---")
    
    # 1. Initialize Database
    database.init_db() 
    
    # 2. Load WIF and derive all necessary BSV objects and addresses
    logger.info(f"Loading WIF: {config.DEVICE_WIF[:5]}...")
    device_state.private_key = bsv_utils.get_bsv_private_key_object(config.DEVICE_WIF) 
    if not device_state.private_key:
        logger.critical("Could not load device private key from WIF. Exiting.")
        sys.exit(1)

    device_state.public_key = bsv_utils.get_bsv_public_key_object(device_state.private_key)
    if not device_state.public_key:
        logger.critical("Could not derive public key. Exiting.")
        sys.exit(1)
    
    addr, script_hex = bsv_utils.get_address_and_locking_script(device_state.public_key)
    if not (addr and script_hex):
        logger.critical("Could not derive payment address or locking script. Exiting.")
        sys.exit(1)
    device_state.address = addr
    device_state.locking_script_hex = script_hex
    
    # 3. Print device info and create Flask app
    logger.info(f"Device Initialized. Payment Address: {device_state.address}")
    app = Flask(__name__, template_folder='templates')
    
    # 4. Register error handlers and routes
    routes.register_error_handlers(app)
    routes.register_routes(app, device_state)

    # 5. Perform initial sensor advertisement broadcast
    logger.info("Broadcasting initial sensor advertisement...")
    if not broadcast_advertisement_tx():
        logger.warning("Initial advertisement broadcast FAILED. Check logs and funding.")

    # 6. Start background threads
    logger.info("Starting background threads...")
    stamper_thread = threading.Thread(target=sensor_and_stamp_loop, name="StamperThread", daemon=True)
    stamper_thread.start()
    
    adv_thread = threading.Thread(target=advertisement_loop, name="AdvertiserThread", daemon=True)
    adv_thread.start()
    
    # 7. Start the Flask API server (this is the main thread's blocking call)
    logger.info(f"Starting Flask API server on http://[{config.DEVICE_IPV6_ADDRESS}]:{config.DEVICE_PORT} (listening on [::])")
    try:
        app.run(host="::", port=config.DEVICE_PORT, debug=False)
    except Exception as e:
        logger.exception(f"Flask server failed to start or crashed: {e}")