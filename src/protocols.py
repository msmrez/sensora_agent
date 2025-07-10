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
    Creates the new, richer 'SENSORA_ADV' advertisement OP_RETURN payload.
    """
    logger.info("--- Attempting to create rich advertisement payload ---")
    try:
        # --- 1. Assemble Core Fields ---
        device_id_type_byte = struct.pack('>B', config.SENSORA_DEVICE_ID_TYPE_IPV6_FULL)
        device_id_bytes = get_device_id_bytes(config.DEVICE_IPV6_ADDRESS)
        port_bytes = struct.pack('>H', config.DEVICE_PORT)

        geohash_bytes = config.DEVICE_GEOHASH.encode('ascii') if config.DEVICE_GEOHASH else b''
        geohash_len_byte = struct.pack('>B', len(geohash_bytes))

        # --- 2. Build Services JSON from Config ---
        services_to_advertise = []
        logger.debug(f"Scanning config sections for services: {config.cfg.sections()}")

        for section in config.cfg.sections():
            if section.startswith('service.'):
                logger.debug(f"Processing service section: [{section}]")
                service_config = config.cfg[section]

                if service_config.getboolean('enabled', False):
                    # Read simple strings from the config
                    units_str = service_config.get('units', '')
                    
                    # Construct the dictionary for this service
                    service_json = {
                        "type": service_config.getint('data_type_code'),
                        "name": service_config.get('name', 'Unnamed Service'),
                        "desc": service_config.get('description', ''),
                        # Split the simple comma-separated string into a list
                        "units": [unit.strip() for unit in units_str.split(',') if unit.strip()],
                        "price": service_config.getint('price_sats')
                    }
                    services_to_advertise.append(service_json)
        
        logger.info(f"Successfully constructed services list: {services_to_advertise}")

        # --- 3. Serialize to JSON and Check Size ---
        services_payload_str = json.dumps(services_to_advertise, separators=(',', ':'))
        services_payload_bytes = services_payload_str.encode('utf-8')

        if len(services_payload_bytes) > 500: # A reasonable limit for OP_RETURN
            logger.error(f"Services JSON payload is too large ({len(services_payload_bytes)} bytes). Aborting advertisement.")
            return None

        # --- 4. Assemble Final Payload ---
        timestamp_bytes = struct.pack('>Q', int(time.time()))

        op_return_parts = [
            config.SENSORA_ADV_PREFIX, # Use the new prefix name from config
            device_id_type_byte,
            device_id_bytes,
            port_bytes,
            geohash_len_byte,
            geohash_bytes,
            services_payload_bytes, # The JSON blob
            timestamp_bytes
        ]
        
        final_payload = b''.join(op_return_parts)
        logger.info(f"Successfully created advertisement payload ({len(final_payload)} bytes).")
        return final_payload

    except Exception as e:
        # This will catch any error (e.g., config parsing, JSON error) and log the full traceback.
        logger.exception(f"CRITICAL ERROR in create_advertisement_op_return_payload: {e}")
        return None