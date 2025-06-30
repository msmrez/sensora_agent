# sensora_agent/src/reputation.py

import time
import logging
from . import database

logger = logging.getLogger(__name__)

def calculate_reputation_score() -> dict:
    """
    Calculates and returns a dictionary of reputation metrics for the sensor.
    """
    metrics = database.get_all_reputation_metrics()
    
    successful_adv = metrics.get('successful_adv_count', 0)
    failed_adv = metrics.get('failed_adv_count', 0)
    total_adv = successful_adv + failed_adv
    
    successful_stamps = metrics.get('successful_stamp_count', 0)
    failed_stamps = metrics.get('failed_stamp_count', 0)
    total_stamps = successful_stamps + failed_stamps
    
    last_adv_ts = metrics.get('last_adv_timestamp', 0)
    last_stamp_ts = metrics.get('last_stamp_timestamp', 0)
    
    # Calculate Uptime Percentage (based on advertisement success)
    uptime_percentage = 0
    if total_adv > 0:
        uptime_percentage = round((successful_adv / total_adv) * 100, 2)
        
    # Calculate Stamping Success Rate
    stamp_success_rate = 0
    if total_stamps > 0:
        stamp_success_rate = round((successful_stamps / total_stamps) * 100, 2)
        
    # Determine last overall activity
    last_activity_timestamp = max(last_adv_ts, last_stamp_ts)
    seconds_since_last_activity = int(time.time()) - last_activity_timestamp if last_activity_timestamp > 0 else -1

    return {
        'uptime_percentage': uptime_percentage,
        'stamp_success_rate': stamp_success_rate,
        'last_activity_timestamp': last_activity_timestamp,
        'seconds_since_last_activity': seconds_since_last_activity,
        'total_stamps_recorded': total_stamps,
        'total_advertisements_sent': total_adv
    }