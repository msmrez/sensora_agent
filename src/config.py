# sensora_agent/src/config.py

import os
import configparser

# --- 1. Define Project Root and Read the Config File ---
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config_file_path = os.path.join(project_root, 'config.ini')

cfg = configparser.ConfigParser()
cfg.read(config_file_path)

# --- 2. Define the Data Directory (Handles Docker vs. Local) ---
data_directory = os.getenv('SENSORA_DATA_DIR', project_root)
DATABASE_NAME = os.path.join(data_directory, 'sensora_device.db')

# --- 3. LOAD ALL CONFIGURATIONS FROM 'cfg' USING CORRECT SECTIONS ---

# [Device] Section
try:
    DEVICE_WIF = cfg.get('Device', 'wif')
    DEVICE_IPV6_ADDRESS = cfg.get('Device', 'ipv6_address')
    DEVICE_PORT = cfg.getint('Device', 'port')
    API_HOST = cfg.get('Device', 'host')
except configparser.NoSectionError as e:
    print(f"CRITICAL: Missing required section [Device] in 'config.ini'.")
    raise e

# [Location] Section
DEVICE_GEOHASH = cfg.get('Location', 'geohash', fallback=None)

# [Service] Section
try:
    PRICE_SATS_PER_READING = cfg.getint('Service', 'price_sats')
    ACTIVE_SENSOR_TYPE = cfg.get('Service', 'active_sensor_type')
except configparser.NoSectionError as e:
    print(f"CRITICAL: Missing required section [Service] in 'config.ini'.")
    raise e

# [Timing] Section
DATA_CAPTURE_INTERVAL_SECONDS = cfg.getint('Timing', 'data_capture_interval_seconds', fallback=300)
ADVERTISEMENT_INTERVAL_SECONDS = cfg.getint('Timing', 'advertisement_interval_seconds', fallback=43200)

# [BSV] Section
BSV_FEE_RATE_SATS_PER_BYTE = cfg.getfloat('BSV', 'bsv_fee_rate_sats_per_byte', fallback=0.01)

# [Token] Section
TOKEN_EXPIRY_SECONDS = cfg.getint('Token', 'expiry_seconds', fallback=3600)
TOKEN_MAX_USAGE = cfg.getint('Token', 'max_usage', fallback=3)

# [batch] Section
BATCH_MAX_SIZE = cfg.getint('batch', 'max_batch_size', fallback=1000)


# --- 4. PROTOCOL CONSTANTS & DATA TYPE REGISTRY ---
SENSORA_PROOF_PREFIX = b'SENSORA_PROOF'
SENSORA_ADV_PREFIX = b'SENSORA_ADV'
SENSORA_DEVICE_ID_TYPE_IPV6_FULL = 0x01

DATA_TYPES = {
    1: {"name": "Temperature/Humidity"},
    10: {"name": "CPU Metrics"},
    11: {"name": "Memory Usage"}
}

DATA_TYPE_TEMP_HUMID = 1
DATA_TYPE_CPU_METRICS = 10
DATA_TYPE_MEMORY_USAGE = 11