import os
import configparser

# --- 1. Define Project Root and Read the Config File ---
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config_file_path = os.path.join(project_root, 'config.ini')

# The parser object is named 'cfg' and is available for other modules to import.
cfg = configparser.ConfigParser(interpolation=None)
cfg.read(config_file_path)

# --- 2. Define the Data Directory ---
data_directory = os.getenv('SENSORA_DATA_DIR', project_root)
DATABASE_NAME = os.path.join(data_directory, 'sensora_device.db')

# --- 3. LOAD ALL CONFIGURATIONS FROM 'cfg' ---
try:
    # [Device] Section
    DEVICE_WIF = cfg.get('Device', 'wif')
    DEVICE_IPV6_ADDRESS = cfg.get('Device', 'ipv6_address')
    DEVICE_PORT = cfg.getint('Device', 'port')
    API_HOST = cfg.get('Device', 'host')
    
    # [Location] Section
    DEVICE_GEOHASH = cfg.get('Location', 'geohash', fallback=None)

    # [Service] Section
    PRICE_SATS_PER_READING = cfg.getint('Service', 'price_sats')
    ACTIVE_SENSOR_TYPE = cfg.get('Service', 'active_sensor_type')

    # [Timing] Section
    DATA_CAPTURE_INTERVAL_SECONDS = cfg.getint('Timing', 'data_capture_interval_seconds')
    ADVERTISEMENT_INTERVAL_SECONDS = cfg.getint('Timing', 'advertisement_interval_seconds')

    # [BSV] Section
    BSV_FEE_SATOSHIS_PER_BYTE = cfg.getfloat('BSV', 'bsv_fee_rate_sats_per_byte')
    # --- END OF CORRECTION ---

    # [Token] Section
    TOKEN_EXPIRY_SECONDS = cfg.getint('Token', 'expiry_seconds')
    TOKEN_MAX_USAGE = cfg.getint('Token', 'max_usage')

    # [batch] Section
    BATCH_MAX_SIZE = cfg.getint('batch', 'max_batch_size')

    # [Payment] Section (optional). Payments up to this amount are accepted once a public indexer has the
    # transaction (unconfirmed); larger payments must be mined first. 0 = every payment must be mined.
    MAX_UNCONFIRMED_SATS = cfg.getint('Payment', 'max_unconfirmed_sats', fallback=1000)
except (configparser.NoSectionError, configparser.NoOptionError) as e:
    print(f"CRITICAL: Missing required setting in 'config.ini'. Error: {e}")
    exit(1)

# --- 4. PROTOCOL CONSTANTS & DATA TYPE REGISTRY ---

# --- API ENDPOINTS (Not user-configurable, but defined here for consistency) ---
BITAILS_API_BASE_URL = "https://api.bitails.io"

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