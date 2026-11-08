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
    
    # Per reading, from the readings table: the counters counted a reading that failed and was then
    # restamped as one failure plus one success, and missed failed restamps. 'dropped' and 'unstamped' are
    # transient while the restamp sweep works; 'pending' readings are not counted until resolved.
    outcomes = database.get_stamp_outcomes()
    successful_stamps = outcomes['mined']
    total_stamps = outcomes['mined'] + outcomes['failed']
    
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
        'total_stamps_recorded': outcomes['readings'],
        'total_advertisements_sent': total_adv
    }