# sensora_agent/src/config.py

import configparser
import os
import sys
import logging

logger = logging.getLogger(__name__)

# --- CONFIGURATION LOADING ---

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE_PATH = os.path.join(PROJECT_ROOT, 'config.ini')

if not os.path.exists(CONFIG_FILE_PATH):
    logger.critical(f"CRITICAL: Configuration file not found at {CONFIG_FILE_PATH}.")
    logger.critical("Please copy 'config.ini.example' to 'config.ini' and fill in your details.")
    sys.exit(1)

parser = configparser.ConfigParser()
parser.read(CONFIG_FILE_PATH)

# --- Expose Settings ---
try:
    # [Device]
    DEVICE_IPV6_ADDRESS = parser.get('Device', 'ipv6_address')
    DEVICE_PORT = parser.getint('Device', 'port')
    DEVICE_WIF = parser.get('Device', 'wif')

    # [Location]
    DEVICE_GEOHASH = parser.get('Location', 'geohash', fallback=None)

    # [Service]
    PRICE_SATS_PER_READING = parser.getint('Service', 'price_sats')
    DATA_CAPTURE_INTERVAL_SECONDS = parser.getint('Service', 'data_capture_interval_seconds')
    ADVERTISEMENT_INTERVAL_SECONDS = parser.getint('Service', 'advertisement_interval_seconds')
    BSV_FEE_SATOSHIS_PER_BYTE = parser.getfloat('Service', 'bsv_fee_rate_sats_per_byte')

    # [Token]
    TOKEN_EXPIRY_SECONDS = parser.getint('Token', 'expiry_seconds')
    TOKEN_MAX_USAGE = parser.getint('Token', 'max_usage')

except (configparser.NoSectionError, configparser.NoOptionError) as e:
    logger.critical(f"CRITICAL: Missing required setting in 'config.ini'. Error: {e}")
    sys.exit(1)


# --- API ENDPOINTS (Not user-configurable, but defined here for consistency) ---
BITAILS_API_BASE_URL = "https://api.bitails.io"
BATCH_MAX_SIZE = 1000

# --- PROTOCOL CONSTANTS (These are not user-configurable) ---
DATA_TYPE_TEMP_HUMID: int = 0x0001
SENSORA_PREFIX: bytes = b'SENSORA_PROOF'
SENSORA_VERSION: int = 0x01
SENSORA_DEVICE_ID_TYPE_IPV6_FULL: int = 0x01
ADVERTISEMENT_PREFIX: bytes = b'SENSORA_ADV'

# --- DATABASE (Not user-configurable) ---
DATABASE_NAME = os.path.join(PROJECT_ROOT, "sensora_device.db")
