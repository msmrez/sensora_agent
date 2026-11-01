import logging
import json
import struct
import time
import hashlib
import ipaddress
from . import config

logger = logging.getLogger(__name__)

# --- Helper Functions ---
def get_device_id_bytes(ipv6_string: str) -> bytes:
    return ipaddress.IPv6Address(ipv6_string).packed

def format_float_for_hash(value: float, precision: int = 1) -> str:
    return f"{value:.{precision}f}"

def reading_data_hash(reading_timestamp_int: int, sensor_values_tuple) -> bytes:
    """The hash stamped in SENSORA_PROOF. A buyer recomputes it from the delivered values."""
    formatted_values = [format_float_for_hash(v) for v in sensor_values_tuple]
    data_to_hash_str = f"{reading_timestamp_int}:" + ":".join(formatted_values)
    return hashlib.sha256(data_to_hash_str.encode('utf-8')).digest()

def data_hash_from_proof_payload(payload: bytes) -> bytes:
    """The data hash is the last 32 bytes of a SENSORA_PROOF payload."""
    return payload[-32:]

def batch_commitment(data_hashes: list[bytes]) -> str:
    """v2: binds a batch payment to the exact proofs being bought (hashes in timestamp order)."""
    return hashlib.sha256(b''.join(data_hashes)).hexdigest()

# --- Payload Creators ---

def create_sensora_proof_payload(reading_timestamp_int: int, data_type_code: int, sensor_values_tuple: tuple) -> bytes:
    """Creates the 'SENSORA_PROOF' data stamping OP_RETURN payload."""
    device_id_bytes = get_device_id_bytes(config.DEVICE_IPV6_ADDRESS)
    data_hash = reading_data_hash(reading_timestamp_int, sensor_values_tuple)
    op_return_parts = [
        config.SENSORA_PROOF_PREFIX, 
        struct.pack('>B', 1), # Version
        struct.pack('>B', config.SENSORA_DEVICE_ID_TYPE_IPV6_FULL),
        device_id_bytes, 
        struct.pack('>Q', reading_timestamp_int), 
        struct.pack('>H', data_type_code), 
        data_hash
    ]
    return b''.join(op_return_parts)

def create_advertisement_op_return_payload() -> bytes | None:
    """Creates the new, richer 'SENSORA_ADV' advertisement OP_RETURN payload."""
    try:
        device_id_type_byte = struct.pack('>B', config.SENSORA_DEVICE_ID_TYPE_IPV6_FULL)
        device_id_bytes = get_device_id_bytes(config.DEVICE_IPV6_ADDRESS)
        port_bytes = struct.pack('>H', config.DEVICE_PORT)
        geohash_bytes = config.DEVICE_GEOHASH.encode('ascii') if config.DEVICE_GEOHASH else b''
        geohash_len_byte = struct.pack('>B', len(geohash_bytes))

        services_to_advertise = []
        # Uses config.cfg to access the parser object.
        for section in config.cfg.sections():
            if section.startswith('service.'):
                service_config = config.cfg[section]
                if service_config.getboolean('enabled', False):
                    units_str = service_config.get('units', '')
                    service_json = {
                        "type": service_config.getint('data_type_code'),
                        "name": service_config.get('name'),
                        "desc": service_config.get('description'),
                        "units": [unit.strip() for unit in units_str.split(',') if unit.strip()],
                        "price": service_config.getint('price_sats')
                    }
                    services_to_advertise.append(service_json)
        
        services_payload_str = json.dumps(services_to_advertise, separators=(',', ':'))
        services_payload_bytes = services_payload_str.encode('utf-8')

        timestamp_bytes = struct.pack('>Q', int(time.time()))
        op_return_parts = [
            config.SENSORA_ADV_PREFIX, device_id_type_byte, device_id_bytes,
            port_bytes, geohash_len_byte, geohash_bytes,
            services_payload_bytes, timestamp_bytes
        ]
        return b''.join(op_return_parts)
    except Exception as e:
        logger.exception(f"CRITICAL ERROR in create_advertisement_op_return_payload: {e}")
        return None