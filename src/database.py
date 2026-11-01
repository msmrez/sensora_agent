# sensora_agent/src/database.py

import sqlite3
import hashlib
import json
import traceback
import logging
import time
from . import config # Import settings from our config file

logger = logging.getLogger(__name__)

def _add_column_if_missing(cursor, table: str, column: str, definition: str):
    """Lets a v1 database be opened by v2 without losing data."""
    existing = [row[1] for row in cursor.execute(f"PRAGMA table_info({table})")]
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    try:
        # v2: four threads write (stamper, advertiser, tracker, API); WAL and a longer timeout avoid
        # "database is locked" errors turning an accepted broadcast into a recorded failure.
        conn = sqlite3.connect(config.DATABASE_NAME, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.Error as e:
        logger.exception(f"DATABASE_ERROR: Could not connect to database '{config.DATABASE_NAME}': {e}")
        return None

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    conn = None 
    try:
        conn = get_db_connection()
        if not conn:
            raise sqlite3.Error("Failed to get DB connection for initialization.")
        
        cursor = conn.cursor()
        logger.info("[DB_INIT] Attempting to create table sensor_readings...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings (
                reading_id INTEGER PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                value_temp REAL,
                value_humid REAL,
                data_type_code INTEGER NOT NULL,
                op_return_payload_hex TEXT NOT NULL,
                onchain_proof_txid TEXT NOT NULL UNIQUE,
                paid INTEGER DEFAULT 0,
                consumer_payment_txid TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        logger.info("[DB_INIT] CREATE TABLE sensor_readings statement executed.")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_onchain_proof_txid ON sensor_readings (onchain_proof_txid);")
        logger.info("[DB_INIT] CREATE INDEX idx_onchain_proof_txid statement executed.")

        logger.info("[DB_INIT] Attempting to create table access_tokens...")
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS access_tokens (
        token_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reading_id TEXT NOT NULL,
        payment_txid TEXT NOT NULL UNIQUE, 
        access_token TEXT NOT NULL UNIQUE,
        expiry_timestamp INTEGER NOT NULL,
        usage_count INTEGER DEFAULT 0,
        max_usage INTEGER DEFAULT 1, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_reading_payment ON access_tokens (reading_id, payment_txid);")
        logger.info("[DB_INIT] CREATE TABLE access_tokens statement executed.")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_access_token ON access_tokens (access_token);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_reading_id ON access_tokens (reading_id);")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_reading_payment ON access_tokens (reading_id, payment_txid);")
        logger.info("[DB_INIT] CREATE INDEX for access_tokens statements executed.")
        
        logger.info("[DB_INIT] Attempting to create table key_value_store...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS key_value_store (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        logger.info("[DB_INIT] CREATE TABLE key_value_store statement executed.")
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS batch_quotes (
        batch_id TEXT PRIMARY KEY,
        num_readings INTEGER NOT NULL,
        total_price_sats INTEGER NOT NULL,
        start_timestamp INTEGER NOT NULL,
        end_timestamp INTEGER NOT NULL,
        created_timestamp INTEGER NOT NULL, -- The column exists here
        is_claimed INTEGER DEFAULT 0,
        consumer_payment_txid TEXT 
    );
''')

    # Add an index for faster cleanup of old quotes
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_batch_quotes_created ON batch_quotes (created_timestamp);
        ''')

        # --- v2: every broadcast is logged and tracked until it is mined or dropped ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tx_log (
                txid TEXT PRIMARY KEY,
                kind TEXT NOT NULL,          -- 'proof' or 'adv'
                ref TEXT,                    -- reading_id for proofs
                raw_hex TEXT NOT NULL,
                fee INTEGER,
                size INTEGER,
                inputs TEXT,                 -- JSON list of [txid, vout]
                created_at INTEGER NOT NULL,
                http_status INTEGER,
                broadcast_body TEXT,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending | seen | mined | dropped | rejected
                first_seen_at INTEGER,
                mined_at INTEGER,
                block_height INTEGER,
                block_time INTEGER,
                attempts INTEGER NOT NULL DEFAULT 1,
                last_attempt_at INTEGER,
                last_error TEXT,
                replaced_by TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tx_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                txid TEXT NOT NULL,
                at INTEGER NOT NULL,
                accepted INTEGER,            -- 1 accepted, 0 rejected, NULL unknown
                http_status INTEGER,
                body TEXT
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_log_status ON tx_log (status);")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS spent_outpoints (
                txid TEXT NOT NULL,
                vout INTEGER NOT NULL,
                PRIMARY KEY (txid, vout)
            )
        ''')
        _add_column_if_missing(cursor, "sensor_readings", "proof_status", "TEXT DEFAULT 'pending'")
        _add_column_if_missing(cursor, "sensor_readings", "proof_block_height", "INTEGER")
        _add_column_if_missing(cursor, "sensor_readings", "proof_mined_at", "INTEGER")
        _add_column_if_missing(cursor, "batch_quotes", "commitment", "TEXT")
        _add_column_if_missing(cursor, "batch_quotes", "reading_ids", "TEXT")
        _add_column_if_missing(cursor, "tx_log", "last_attempt_at", "INTEGER")
        conn.commit()
        logger.info(f"[DB_INIT] Database '{config.DATABASE_NAME}' initialized/checked successfully.")
    except Exception as e:
        logger.exception(f"[DB_INIT] CRITICAL ERROR during database initialization: {e}")
        raise
    finally:
        if conn:
            conn.close()




# --- Query and Update Functions ---

def save_reading(reading_id, timestamp, value_tuple, data_type, op_return_payload_hex, onchain_proof_txid):
    """Saves a new sensor reading to the database."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        temp_val, humid_val = None, None
        if data_type == config.DATA_TYPE_TEMP_HUMID and value_tuple and len(value_tuple) == 2:
            temp_val, humid_val = value_tuple
        
        cursor.execute('''
            INSERT INTO sensor_readings 
            (reading_id, timestamp, value_temp, value_humid, data_type_code, 
             op_return_payload_hex, onchain_proof_txid, paid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (reading_id, timestamp, temp_val, humid_val, data_type, 
              op_return_payload_hex, onchain_proof_txid, 0)) 
        conn.commit()
        logger.info(f"Reading {reading_id} saved to database.")
    except sqlite3.IntegrityError as ie: 
        logger.warning(f"DB IntegrityError (likely duplicate onchain_proof_txid '{onchain_proof_txid}'): {ie}.")
    except Exception as e:
        logger.exception(f"Error saving reading {reading_id} to database: {e}")
    finally:
        if conn:
            conn.close()

def get_reading(reading_id: int):
    """Fetches a single reading by its ID."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sensor_readings WHERE reading_id = ?", (reading_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.exception(f"Error fetching reading {reading_id} from database: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_latest_claimed_batches(limit: int = 10) -> list[dict]:
    """Fetches the most recently claimed batches from the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            batch_id, num_readings, total_price_sats, 
            start_timestamp, end_timestamp, 
            consumer_payment_txid AS payment_txid,
            created_timestamp  -- Select the timestamp for ordering and display
        FROM batch_quotes
        WHERE is_claimed = 1
        ORDER BY created_timestamp DESC
        LIMIT ?
    """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def reading_exists(reading_id: int) -> bool:
    """Checks if a reading exists in the database."""
    # This is a more efficient way than fetching the whole row
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM sensor_readings WHERE reading_id = ? LIMIT 1", (reading_id,))
        return cursor.fetchone() is not None
    except Exception as e:
        logger.exception(f"Error checking existence for reading {reading_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_latest_reading_id() -> int | None:
    """Gets the most recent reading_id from the database."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(reading_id) FROM sensor_readings")
        result = cursor.fetchone()
        return result[0] if result and result[0] is not None else None
    except Exception as e:
        logger.exception(f"Error fetching latest reading_id from DB: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_latest_mined_reading_id() -> int | None:
    """v2: only readings with a mined proof are offered for single purchase."""
    conn = get_db_connection()
    row = conn.execute("SELECT MAX(reading_id) FROM sensor_readings WHERE proof_status = 'mined'").fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None

def update_reading_as_paid(reading_id: int, payment_txid: str):
    """Marks a reading as paid in the database."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE sensor_readings SET paid = 1, consumer_payment_txid = ? 
            WHERE reading_id = ? AND paid = 0
        ''', (payment_txid, reading_id))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Updated reading {reading_id} to PAID status in database.")
        else:
            logger.info(f"Reading {reading_id} was already marked as paid or did not exist.")
    except Exception as e:
        logger.exception(f"Error updating payment status for reading {reading_id}: {e}")
    finally:
        if conn:
            conn.close()

def save_access_token(reading_id, payment_txid, access_token, expiry_timestamp, max_usage):
    """
    Saves a new access token to the database.
    The 'reading_id' can be an integer (for single readings) or a string (for batches).
    The database column is TEXT to accommodate both.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # This SQL statement is correct.
        sql = '''
            INSERT INTO access_tokens 
                (reading_id, payment_txid, access_token, expiry_timestamp, max_usage) 
            VALUES 
                (?, ?, ?, ?, ?)
        '''
        
        # This tuple of parameters now correctly matches the function signature.
        params = (
            str(reading_id),
            payment_txid,
            access_token,
            expiry_timestamp,
            max_usage
        )

        cursor.execute(sql, params)
        conn.commit()
        
        logger.info(f"Access token for reading/batch {str(reading_id)[:12]}... saved to database.")

    except Exception as e:
        logger.exception(f"Error saving access token for reading/batch {str(reading_id)}: {e}")
        # Re-raise the exception so the calling route knows something went wrong.
        raise
    finally:
        if conn:
            conn.close()

# In sensora_agent/src/database.py

def mark_batch_as_claimed(batch_id: str, payment_txid: str):
    """Marks a batch quote as claimed and records the payment TXID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        logger.info(f"Attempting to mark batch {batch_id[:8]} as claimed with TXID {payment_txid[:12]}...")
        
        # Double-check table and column names here
        cursor.execute(
            "UPDATE batch_quotes SET is_claimed = 1, consumer_payment_txid = ? WHERE batch_id = ?", 
            (payment_txid, batch_id)
        )
        
        conn.commit()
        
        # Add a verification log. rows_affected should be 1.
        rows_affected = cursor.rowcount
        if rows_affected > 0:
            logger.info(f"Successfully marked batch {batch_id[:8]} as claimed. Rows affected: {rows_affected}")
        else:
            logger.error(f"FAILED to mark batch {batch_id[:8]} as claimed. The UPDATE query did not affect any rows.")

    except Exception as e:
        # This will now print the exact SQL error to your log.
        logger.exception(f"CRITICAL ERROR in mark_batch_as_claimed for batch_id {batch_id[:8]}: {e}")
    finally:
        if conn:
            conn.close()

def get_token_details(reading_id, token):
    """Fetches token details for validation."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM access_tokens WHERE reading_id = ? AND access_token = ?", (reading_id, token))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.exception(f"Error fetching token details for reading {reading_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def increment_token_usage(token_id):
    """Increments the usage count of a token."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE access_tokens SET usage_count = usage_count + 1 WHERE token_id = ?", (token_id,))
        conn.commit()
    except Exception as e:
        logger.exception(f"Error incrementing usage for token_id {token_id}: {e}")
    finally:
        if conn:
            conn.close()
            
def set_key_value(key: str, value: str):
    """Inserts or replaces a key-value pair."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("INSERT OR REPLACE INTO key_value_store (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        logger.info(f"Set key '{key}' in key_value_store.")
    except Exception as e:
        logger.exception(f"Error setting key '{key}' in key_value_store: {e}")
    finally:
        if conn:
            conn.close()

def get_key_value(key: str) -> str | None:
    """Gets a value by its key."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM key_value_store WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row['value'] if row else None
    except Exception as e:
        logger.exception(f"Error getting key '{key}' from key_value_store: {e}")
        return None
    finally:
        if conn:
            conn.close()

# Add these two functions to your src/database.py file

def get_latest_readings(limit: int = 20) -> list:
    """Fetches the most recent sensor readings."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sensor_readings ORDER BY reading_id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        # Reconstruct sensor_values tuple
        readings = []
        for row in rows:
            reading_dict = dict(row)
            if row["value_temp"] is not None and row["value_humid"] is not None:
                reading_dict["sensor_values"] = (row["value_temp"], row["value_humid"])
            else:
                reading_dict["sensor_values"] = None
            readings.append(reading_dict)
        return readings
    except Exception as e:
        logger.exception(f"Error fetching latest readings: {e}")
        return []
    finally:
        if conn: conn.close()
        
def get_latest_tokens(limit: int = 10) -> list:
    """Fetches the most recently created access tokens."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM access_tokens ORDER BY token_id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.exception(f"Error fetching latest tokens: {e}")
        return []
    finally:
        if conn: conn.close()

def increment_key_value(key: str, amount: int = 1):
    """Atomically increments a numeric value for a given key in the key-value store."""
    conn = None
    try:
        conn = get_db_connection()
        # INSERT OR IGNORE ensures the key exists with a default value of 0 if it's the first time.
        conn.execute("INSERT OR IGNORE INTO key_value_store (key, value) VALUES (?, '0')", (key,))
        # Then, perform the atomic update.
        conn.execute("UPDATE key_value_store SET value = CAST(value AS INTEGER) + ? WHERE key = ?", (amount, key))
        conn.commit()
    except Exception as e:
        logger.exception(f"Error incrementing key '{key}' in key_value_store: {e}")
    finally:
        if conn:
            conn.close()

def get_all_reputation_metrics() -> dict:
    """Fetches all keys related to reputation metrics from the key-value store."""
    keys_to_fetch = (
        'last_adv_timestamp', 'successful_adv_count', 'failed_adv_count',
        'last_stamp_timestamp', 'successful_stamp_count', 'failed_stamp_count'
    )
    conn = None
    try:
        conn = get_db_connection()
        # Create a format string for the IN clause
        placeholders = ', '.join('?' for _ in keys_to_fetch)
        query = f"SELECT key, value FROM key_value_store WHERE key IN ({placeholders})"
        cursor = conn.execute(query, keys_to_fetch)
        # Convert all values to int, defaulting to 0 if conversion fails
        metrics = {}
        for row in cursor.fetchall():
            try:
                metrics[row['key']] = int(row['value'])
            except (ValueError, TypeError):
                metrics[row['key']] = 0
        return metrics
    except Exception as e:
        logger.exception(f"Error getting all reputation metrics: {e}")
        return {} # Return empty dict on error
    finally:
        if conn:
            conn.close()


def count_readings_in_range(start_ts: int, end_ts: int) -> int:
    """Counts how many readings exist within a given timestamp range."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM sensor_readings WHERE timestamp >= ? AND timestamp <= ?",
        (start_ts, end_ts)
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_provable_readings_in_range(start_ts: int, end_ts: int) -> list[dict]:
    """v2: only readings whose proof is mined can be sold, so the buyer can always verify them."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM sensor_readings WHERE timestamp >= ? AND timestamp <= ? AND proof_status = 'mined' "
        "ORDER BY timestamp ASC",
        (start_ts, end_ts)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_readings_by_ids(reading_ids: list[int]) -> list[dict]:
    conn = get_db_connection()
    placeholders = ', '.join('?' for _ in reading_ids)
    rows = conn.execute(
        f"SELECT * FROM sensor_readings WHERE reading_id IN ({placeholders}) ORDER BY timestamp ASC",
        reading_ids
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def cache_batch_details(batch_id: str, num_readings: int, total_price_sats: int, start_ts: int, end_ts: int,
                        commitment: str | None = None, reading_ids: list[int] | None = None):
    """Saves a batch quote to the database with an expiry."""
    conn = get_db_connection()
    cursor = conn.cursor()
    created_ts = int(time.time())
    cursor.execute(
        """
        INSERT OR REPLACE INTO batch_quotes
        (batch_id, num_readings, total_price_sats, start_timestamp, end_timestamp, created_timestamp,
         commitment, reading_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, num_readings, total_price_sats, start_ts, end_ts, created_ts,
         commitment, json.dumps(reading_ids) if reading_ids is not None else None)
    )
    conn.commit()
    conn.close()

# --- v2: transaction log and spent-outpoint memory ---

def add_spent_outpoints(outpoints: list[tuple[str, int]]):
    conn = get_db_connection()
    conn.executemany("INSERT OR IGNORE INTO spent_outpoints (txid, vout) VALUES (?, ?)", outpoints)
    conn.commit()
    conn.close()

def remove_spent_outpoints(outpoints: list[tuple[str, int]]):
    conn = get_db_connection()
    conn.executemany("DELETE FROM spent_outpoints WHERE txid = ? AND vout = ?", outpoints)
    conn.commit()
    conn.close()

def get_live_txs() -> list[dict]:
    """Transactions that are not dropped or rejected (pending, seen or mined)."""
    conn = get_db_connection()
    rows = conn.execute("SELECT txid, raw_hex, inputs, status FROM tx_log WHERE status IN ('pending', 'seen', 'mined')").fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_spent_outpoints() -> list[tuple[str, int]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT txid, vout FROM spent_outpoints").fetchall()
    conn.close()
    return [(row["txid"], row["vout"]) for row in rows]

def log_attempt(txid: str, accepted, http_status, body: str):
    """Append-only history of every broadcast and rebroadcast response."""
    conn = get_db_connection()
    conn.execute("INSERT INTO tx_attempts (txid, at, accepted, http_status, body) VALUES (?, ?, ?, ?, ?)",
                 (txid, int(time.time()), None if accepted is None else int(bool(accepted)), http_status, (body or "")[:2000]))
    conn.commit()
    conn.close()

def log_tx(kind: str, ref: str | None, attempt: dict):
    """
    Records a broadcast. Accepted and unknown outcomes start as 'pending' and are resolved by the tracker;
    definite rejections are kept as 'rejected' so the audit can count them.
    """
    for earlier in attempt.get("previous_attempts", []):
        log_tx(kind, ref, {**earlier, "previous_attempts": []})
    now = int(time.time())
    status = "rejected" if attempt["accepted"] is False else "pending"
    conn = get_db_connection()
    conn.execute(
        """INSERT OR REPLACE INTO tx_log
           (txid, kind, ref, raw_hex, fee, size, inputs, created_at, http_status, broadcast_body, status,
            last_attempt_at, last_error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (attempt["txid"], kind, ref, attempt["raw_hex"], attempt.get("fee"), attempt.get("size"),
         json.dumps(attempt.get("inputs", [])), now, attempt.get("http_status"),
         (attempt.get("body") or "")[:2000], status, now,
         None if attempt["accepted"] else (attempt.get("body") or "")[:500])
    )
    conn.commit()
    conn.close()
    log_attempt(attempt["txid"], attempt["accepted"], attempt.get("http_status"), attempt.get("body"))

def get_descendant_txids(txid: str) -> set[str]:
    """Open or past txs that (transitively) spend outputs of txid, using the recorded inputs."""
    conn = get_db_connection()
    rows = conn.execute("SELECT txid, inputs FROM tx_log WHERE status IN ('pending', 'seen')").fetchall()
    conn.close()
    spends = {row["txid"]: {i[0] for i in json.loads(row["inputs"] or "[]")} for row in rows}
    found, frontier = set(), {txid}
    while frontier:
        nxt = {t for t, parents in spends.items() if parents & frontier and t not in found}
        found |= nxt
        frontier = nxt
    return found

def get_readings_needing_restamp(limit: int = 3) -> list[dict]:
    """Readings whose proof was dropped or never accepted, and that have no open transaction."""
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT r.* FROM sensor_readings r
           WHERE r.proof_status IN ('dropped', 'unstamped')
             AND NOT EXISTS (SELECT 1 FROM tx_log t WHERE t.ref = CAST(r.reading_id AS TEXT) AND t.kind = 'proof'
                             AND t.status IN ('pending', 'seen', 'mined'))
           ORDER BY r.timestamp LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_open_txs() -> list[dict]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM tx_log WHERE status IN ('pending', 'seen') ORDER BY created_at").fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_tx(txid: str, **fields):
    if not fields:
        return
    conn = get_db_connection()
    assignments = ', '.join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE tx_log SET {assignments} WHERE txid = ?", (*fields.values(), txid))
    conn.commit()
    conn.close()

def update_reading_proof(reading_id: int, **fields):
    if not fields:
        return
    conn = get_db_connection()
    assignments = ', '.join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE sensor_readings SET {assignments} WHERE reading_id = ?", (*fields.values(), reading_id))
    conn.commit()
    conn.close()

def get_cached_batch_details(batch_id: str) -> dict | None:
    """Retrieves cached details for a given batch ID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # Optional: Add a check to ensure the quote hasn't expired (e.g., older than 15 mins)
    # quote_expiry_threshold = int(time.time()) - (15 * 60)
    cursor.execute(
        "SELECT * FROM batch_quotes WHERE batch_id = ? AND is_claimed = 0", # AND created_timestamp > ?
        (batch_id,) #, quote_expiry_threshold)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_readings_in_range(start_ts: int, end_ts: int) -> list[dict]:
    """Fetches all reading data within a given timestamp range."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM sensor_readings WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
        (start_ts, end_ts)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_batch_details_for_fetch(batch_id: str) -> dict | None:
    """
    Retrieves details for a given batch ID, regardless of its claimed status.
    This is used by the /fetch endpoint after a token has been verified.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM batch_quotes WHERE batch_id = ?",
        (batch_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None