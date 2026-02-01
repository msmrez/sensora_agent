import threading
import time
import logging
import sys
import random
from flask import Flask
from threading import Lock
import psutil # <-- NEW IMPORT

# Import from our 'src' package
from src import config, database, protocols, bsv_utils, routes
from src.openapi_spec import SWAGGER_TEMPLATE
from flasgger import Swagger

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] %(module)s.%(funcName)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- Main Application State Class ---
class DeviceState:
    def __init__(self):
        self.private_key = None
        self.public_key = None
        self.address = None
        self.locking_script_hex = None

# --- Global Objects ---
device_state = DeviceState()
bsv_tx_lock = Lock()
app = Flask(__name__, template_folder='templates')

# --- DATA GATHERING FUNCTIONS ---

def get_simulated_sensor_data() -> tuple[int, tuple[float, float], int]:
    """Simulates temperature and humidity readings."""
    timestamp = int(time.time())
    temperature = round(random.uniform(15.0, 30.0), 1)
    humidity = round(random.uniform(40.0, 70.0), 1)
    return timestamp, (temperature, humidity), config.DATA_TYPE_TEMP_HUMID

def get_cpu_metrics_data() -> tuple[int, tuple[float, float], int] | tuple[None, None, None]:
    """Reads the system's CPU temperature and 1-minute load average."""
    try:
        timestamp = int(time.time())
        cpu_temp = -1.0
        try:
            temps = psutil.sensors_temperatures()
            if 'coretemp' in temps and temps['coretemp']:
                cpu_temp = sum(t.current for t in temps['coretemp']) / len(temps['coretemp'])
                cpu_temp = round(cpu_temp, 1)
        except (AttributeError, KeyError, IndexError):
            logger.warning("Could not read CPU temperature. psutil.sensors_temperatures() may not be supported.")

        cpu_load = (psutil.getloadavg()[0] / psutil.cpu_count()) * 100
        cpu_load = round(cpu_load, 1)
        
        return timestamp, (cpu_temp, cpu_load), config.DATA_TYPE_CPU_METRICS
    except Exception as e:
        logger.error(f"Failed to get CPU metrics: {e}"); return None, None, None

def get_memory_usage_data() -> tuple[int, tuple[float, float], int] | tuple[None, None, None]:
    """Reads the system's RAM and Swap memory usage percentages."""
    try:
        timestamp = int(time.time())
        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        ram_usage = round(ram.percent, 1)
        swap_usage = round(swap.percent, 1)
        
        return timestamp, (ram_usage, swap_usage), config.DATA_TYPE_MEMORY_USAGE
    except Exception as e:
        logger.error(f"Failed to get Memory metrics: {e}"); return None, None, None

def data_type_code_to_name(code: int) -> str:
    """Uses the central registry in config to get the human-readable name."""
    return config.DATA_TYPES.get(code, {}).get("name", f"Unknown ({code})")

# --- BACKGROUND THREADS ---

def data_capture_and_stamp_loop():
    """
    Main background loop. Dispatches to the correct data-gathering function
    based on the configuration, then stamps the proof on-chain.
    """
    logger.info("Data capture and stamp loop starting...")
    if not (device_state.private_key and device_state.public_key):
        logger.critical("Device keys not loaded. Stamper thread cannot operate."); return 

    DATA_GATHERING_FUNCTIONS = {
        "temp_humid": get_simulated_sensor_data,
        "cpu_metrics": get_cpu_metrics_data,
        "memory_usage": get_memory_usage_data
    }
    
    active_sensor_key = config.ACTIVE_SENSOR_TYPE
    data_function = DATA_GATHERING_FUNCTIONS.get(active_sensor_key)
    
    if not data_function:
        logger.critical(f"Invalid 'active_sensor_type' in config: '{active_sensor_key}'. Exiting stamper thread.")
        return
    logger.info(f"Agent configured to run in '{active_sensor_key}' mode.")
    
    while True:
        try:
            reading_ts, value_tuple, data_type = data_function()

            if value_tuple is None:
                logger.warning("Data capture failed for active sensor. Skipping this cycle.")
            else:
                logger.info(f"Captured data: {value_tuple}, type: {data_type_code_to_name(data_type)}")
                
                op_return_payload = protocols.create_sensora_proof_payload(reading_ts, data_type, value_tuple)
                
                logger.info(f"Attempting to stamp reading {reading_ts} on-chain...")
                onchain_txid = bsv_utils.create_and_broadcast_op_return_tx(
                    bsv_tx_lock,
                    device_state.private_key, 
                    device_state.public_key, 
                    device_state.locking_script_hex, 
                    op_return_payload
                )
                
                if onchain_txid:
                    database.increment_key_value('successful_stamp_count')
                    database.set_key_value('last_stamp_timestamp', str(int(time.time())))
                    logger.info(f"Successfully stamped reading {reading_ts} on-chain. TXID: {onchain_txid}")
                    database.save_reading(reading_ts, reading_ts, value_tuple, data_type, op_return_payload.hex(), onchain_txid)
                else:
                    database.increment_key_value('failed_stamp_count')
                    logger.error(f"Failed to stamp reading {reading_ts} on-chain. Will retry next cycle.")

        except Exception as e:
            database.increment_key_value('failed_stamp_count')
            logger.exception(f"CRITICAL ERROR in data_capture_and_stamp_loop: {e}")
            time.sleep(60)
        
        scan_interval = config.DATA_CAPTURE_INTERVAL_SECONDS
        logger.info(f"Stamper thread sleeping for {scan_interval} seconds...")
        time.sleep(scan_interval)


def broadcast_advertisement_tx() -> bool:
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
        bsv_tx_lock,
        device_state.private_key, 
        device_state.public_key, 
        device_state.locking_script_hex, 
        adv_op_return_payload
    )

    if txid:
        logger.info(f"[ADV_TX] Advertisement TX broadcast successful! TXID: {txid}")
        database.set_key_value('last_adv_txid', txid)
        database.set_key_value('last_adv_timestamp', str(int(time.time())))
        database.increment_key_value('successful_adv_count')
        return True
    else:
        logger.error("[ADV_TX] Failed to broadcast advertisement transaction.")
        database.increment_key_value('failed_adv_count')
        return False

def advertisement_loop():
    adv_interval = config.ADVERTISEMENT_INTERVAL_SECONDS
    logger.info(f"Advertisement loop starting... will broadcast after the first interval of {adv_interval / 3600:.1f} hours.")
    time.sleep(adv_interval)
    
    while True:
        try:
            broadcast_advertisement_tx()
        except Exception as e:
            logger.exception(f"CRITICAL ERROR in advertisement_loop: {e}")
        
        logger.info(f"[ADV_LOOP] Next advertisement in {adv_interval / 3600:.1f} hours.")
        time.sleep(adv_interval)

# --- Application Factory and Main Execution ---
def create_app():
    """Initializes and returns the configured Flask application."""
    logger.info("--- Initializing Sensōra Device Agent ---")
    
    database.init_db() 
    
    logger.info(f"Loading WIF: {config.DEVICE_WIF[:5]}...")
    device_state.private_key = bsv_utils.get_bsv_private_key_object(config.DEVICE_WIF)
    if not device_state.private_key:
        logger.critical("Could not load private key. Exiting."); sys.exit(1)

    device_state.public_key = bsv_utils.get_bsv_public_key_object(device_state.private_key)
    addr, script_hex = bsv_utils.get_address_and_locking_script(device_state.public_key)
    device_state.address = addr
    device_state.locking_script_hex = script_hex
    
    logger.info(f"Device Initialized. Payment Address: {device_state.address}")

    routes.register_all_routes(app, device_state)

    Swagger(app, template=SWAGGER_TEMPLATE)

    logger.info("Broadcasting initial sensor advertisement...")
    if not broadcast_advertisement_tx():
        logger.warning("Initial advertisement broadcast FAILED. Check logs and funding.")

    logger.info("Starting background threads...")
    stamper_thread = threading.Thread(target=data_capture_and_stamp_loop, name="StamperThread", daemon=True)
    stamper_thread.start()
    
    adv_thread = threading.Thread(target=advertisement_loop, name="AdvertiserThread", daemon=True)
    adv_thread.start()
    
    return app

if __name__ == "__main__":
    application = create_app()
    
    listen_host = config.API_HOST or '::'
    logger.info(f"Starting Flask API server (listening on {listen_host}:{config.DEVICE_PORT})")
    try:
        application.run(host=listen_host, port=config.DEVICE_PORT, debug=False)
    except Exception as e:
        logger.exception(f"Flask server failed to start or crashed: {e}")