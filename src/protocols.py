# sensora_agent/src/protocols.py

import struct
import ipaddress
import time
import hashlib
from . import config # Import settings from our config file
import uuid
import secrets
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

        # Define services offered by this device from config
        services = [
            {"data_type": config.DATA_TYPE_TEMP_HUMID, "price_sats": config.PRICE_SATS_PER_READING},
            # To add more services, you would extend the config and this list
        ]
        num_services_byte = struct.pack('>B', len(services))
        
        service_bytes_list = []
        for service in services:
            service_bytes_list.append(struct.pack('>H', service["data_type"]))
            service_bytes_list.append(struct.pack('>I', service["price_sats"]))
        
        all_service_bytes = b''.join(service_bytes_list)
        timestamp_bytes = struct.pack('>Q', int(time.time()))

        op_return_parts = [
            config.ADVERTISEMENT_PREFIX,
            device_id_type_byte,
            device_id_bytes,
            port_bytes,
            geohash_len_byte,
            geohash_bytes, 
            num_services_byte,
            all_service_bytes,
            timestamp_bytes
        ]
        return b''.join(op_return_parts)
    except Exception as e:
        # Using logger here would require passing it in, for now, let's keep it clean
        # The caller function will log the error.
        print(f"ERROR: Could not create advertisement payload: {e}")
        return None