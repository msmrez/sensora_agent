# sensora_agent/src/protocols.py

import struct
import ipaddress
import time
import hashlib
from . import config # Import settings from our config file
import uuid
import secrets
import logging
import json
logger = logging.getLogger(__name__)
# --- OP_RETURN HELPER FUNCTIONS ---

def generate_access_token() -> str:
    """Generates a secure, unique access token."""
    return str(uuid.uuid4()) + "-" + secrets.token_hex(16)

def get_device_id_bytes(ipv6_string: str) -> bytes:
    """Converts an IPv6 string to its 16-byte representation."""
    return ipaddress.IPv6Address(ipv6_string).packed

def format_float_for_hash(value: float, precision: int = 1) -> str:
    """Formats a float to a string with fixed precision for consistent hashing."""
    return f"{value:.{precision}f}"

# --- PROTOCOL PAYLOAD CREATORS ---

def create_sensora_op_return_payload(reading_timestamp_int: int, data_type_code: int, sensor_values_tuple: tuple) -> bytes:
    """
    Creates the 'SENSORA_PROOF' data stamping OP_RETURN payload.
    """
    device_id_bytes = get_device_id_bytes(config.DEVICE_IPV6_ADDRESS)
    
    formatted_values = []
    if data_type_code == config.DATA_TYPE_TEMP_HUMID:
        # Ensure tuple has expected number of values
        if len(sensor_values_tuple) != 2:
            raise ValueError("sensor_values_tuple must have 2 values for Temp/Humid data type.")
        formatted_values.append(format_float_for_hash(sensor_values_tuple[0]))
        formatted_values.append(format_float_for_hash(sensor_values_tuple[1]))
    else:
        raise ValueError(f"Unsupported data_type_code for hashing: {data_type_code}")
        
    # The canonical string to be hashed
    data_to_hash_str = f"{reading_timestamp_int}:" + ":".join(formatted_values)
    data_hash = hashlib.sha256(data_to_hash_str.encode('utf-8')).digest()

    op_return_parts = [
        config.SENSORA_PREFIX, 
        struct.pack('>B', config.SENSORA_VERSION), 
        struct.pack('>B', config.SENSORA_DEVICE_ID_TYPE_IPV6_FULL),
        device_id_bytes, 
        struct.pack('>Q', reading_timestamp_int), 
        struct.pack('>H', data_type_code), 
        data_hash
    ]
    return b''.join(op_return_parts)


def create_advertisement_op_return_payload() -> bytes | None:
    """
    Creates the 'SENSORA_ADV1' advertisement OP_RETURN payload.
    """
    try:
        device_id_type_byte = struct.pack('>B', config.SENSORA_DEVICE_ID_TYPE_IPV6_FULL)
        device_id_bytes = get_device_id_bytes(config.DEVICE_IPV6_ADDRESS)
        port_bytes = struct.pack('>H', config.DEVICE_PORT)

        # Handle Geohash
        geohash_bytes = b''
        if config.DEVICE_GEOHASH and isinstance(config.DEVICE_GEOHASH, str):
            geohash_bytes = config.DEVICE_GEOHASH.encode('ascii')
            # Enforce max length
            if len(geohash_bytes) > 12: 
                geohash_bytes = geohash_bytes[:12]
        geohash_len_byte = struct.pack('>B', len(geohash_bytes))
        services_to_advertise = []

        services_to_advertise = []
        for section in config.cfg.sections():
            if section.startswith('service.'):
                service_config = config.cfg[section]
                if service_config.getboolean('enabled', False):
                
                    # Read simple strings from the config
                    units_str = service_config.get('units', '')
                    
                    service_json = {
                        "type": service_config.getint('data_type_code'),
                        "name": service_config.get('name', 'Unnamed Service'),
                        "desc": service_config.get('description', ''),
                        # Split the simple string into a list for the JSON
                        "units": [unit.strip() for unit in units_str.split(',')],
                        "price": service_config.getint('price_sats')
                    }
                    services_to_advertise.append(service_json)

        services_payload_str = json.dumps(services_to_advertise, separators=(',', ':'))
        services_payload_bytes = services_payload_str.encode('utf-8')


        # To keep the OP_RETURN manageable, we should check the size
        if len(services_payload_bytes) > 500: # A reasonable limit
            logger.error("Services JSON payload is too large for OP_RETURN.")
            # Fallback to advertising no services
            services_payload_bytes = b'[]'

        timestamp_bytes = struct.pack('>Q', int(time.time()))

        op_return_parts = [
            config.ADVERTISEMENT_PREFIX,
            device_id_type_byte,
            device_id_bytes,
            port_bytes,
            geohash_len_byte,
            geohash_bytes,
            # Instead of #services, price, etc., we just push the JSON payload
            services_payload_bytes,
            timestamp_bytes
        ]
        return b''.join(op_return_parts)
    except Exception as e:
        logger.exception(f"ERROR: Could not create advertisement payload: {e}")
        return None