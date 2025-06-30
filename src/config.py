# sensora_agent/src/config.py

# --- DEVICE CONFIG ---
DEVICE_IPV6_ADDRESS: str = "2a01:4f8:1c1b:83c3::1" 
DEVICE_PORT: int = 8080 
DEVICE_WIF: str = "KyuXbxKpRugMxJzHhJq4oUYJaxmFBnSLDNtjoccakDC9ZRMZzcS5"
DEVICE_GEOHASH: str = "u4pruydq"

# --- PRICING & TIMING ---
PRICE_SATS_PER_READING: int = 5
DATA_CAPTURE_INTERVAL_SECONDS: int = 300 
ADVERTISEMENT_INTERVAL_SECONDS: int = 12 * 3600 
BSV_FEE_SATOSHIS_PER_BYTE: float = 0.02
TOKEN_EXPIRY_SECONDS = 3600 # 1 hour
TOKEN_MAX_USAGE = 3 # Allow a few downloads per token

# --- PROTOCOL CONSTANTS ---
DATA_TYPE_TEMP_HUMID: int = 0x0001
SENSORA_PREFIX: bytes = b'SNSR'
SENSORA_VERSION: int = 0x01
SENSORA_DEVICE_ID_TYPE_IPV6_FULL: int = 0x01
ADVERTISEMENT_PREFIX: bytes = b'SNSRADV1'

# --- DATABASE ---
DATABASE_NAME = "sensora_device.db" # Assumes DB is in the same root dir as app.py

# --- BITAILS API ---
BITAILS_API_BASE_URL = "https://api.bitails.io"