# sensor_agent/src/routes.py

import time
import json
import logging
import sqlite3
from flask import request, jsonify, abort, render_template
from werkzeug.exceptions import HTTPException
import hashlib
from . import config, database, bsv_utils, reputation

logger = logging.getLogger(__name__)
logger.info("routes.py module is being loaded by Python...")
class PaymentRequiredError(HTTPException):
    code = 402
    description = 'Payment for this resource has not been detected.'

def register_all_routes(app, device_state):
    """Registers all Flask error handlers and routes with the application."""
    logger.info("register_all_routes() function is being CALLED.") # Also add this one

    # --- Error Handlers (Defined inside the registration function) ---
    @app.errorhandler(PaymentRequiredError)
    def handle_payment_required(error):
        return jsonify({"error": "Payment Required", "message": error.description}), 402

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({"error": "Not Found", "message": "The requested resource was not found."}), 404

    @app.errorhandler(500)
    def server_error(error):
        logger.exception("An internal server error occurred")
        return jsonify({"error": "Internal Server Error"}), 500
    
    @app.route("/")
    def dashboard():
        logger.info("--- Rendering Agent Dashboard ---")
        try:
            readings = database.get_latest_readings()
            logger.info(f"Readings data from DB: {readings}")
            recent_batches = database.get_latest_claimed_batches(limit=10)

            tokens = database.get_latest_tokens()
            logger.info(f"Tokens data from DB: {tokens}")
            
            last_adv_txid = database.get_key_value('last_adv_txid') or "N/A"
            logger.info(f"Last Adv TXID: {last_adv_txid}")

            reputation_data = reputation.calculate_reputation_score()
            logger.info(f"Reputation data: {reputation_data}")

            chart_data = { "labels": [], "temp_data": [], "humid_data": [] }
            # This loop needs to be more robust
            for reading in reversed(readings):
                # Check if the 'sensor_values' key exists and is a tuple/list of 2
                if 'sensor_values' in reading and isinstance(reading['sensor_values'], (list, tuple)) and len(reading['sensor_values']) == 2:
                    chart_data["labels"].append(time.strftime('%H:%M:%S', time.localtime(reading["reading_id"])))
                    chart_data["temp_data"].append(reading["sensor_values"][0])
                    chart_data["humid_data"].append(reading["sensor_values"][1])
            chart_data_json = json.dumps(chart_data)
            logger.info(f"Chart JSON data: {chart_data_json}")

        except Exception as e:
            logger.exception("CRITICAL ERROR gathering data for dashboard!")
            # Return a simple error page instead of a blank one
            return "<h1>Error</h1><p>An internal error occurred while generating the dashboard. Please check server logs.</p>", 500
        
        return render_template(
            "index.html", 
            device_ipv6=config.DEVICE_IPV6_ADDRESS,
            device_payment_address=device_state.address,
            price_sats=config.PRICE_SATS_PER_READING,
            readings=readings,
            tokens=tokens,
            recent_batches=recent_batches,
            last_adv_txid=last_adv_txid,
            reputation_data=reputation_data,
            chart_data=json.dumps(chart_data),
            DATA_TYPE_TEMP_HUMID=config.DATA_TYPE_TEMP_HUMID,
            DATA_CAPTURE_INTERVAL_SECONDS=config.DATA_CAPTURE_INTERVAL_SECONDS,
            auto_refresh_interval=30,
            current_year=time.strftime('%Y'),
            time=time
        )

    @app.route("/price")
    def get_price_route():
        latest_id = database.get_latest_reading_id() or int(time.time())
        return jsonify({
            "current_reading_id": str(latest_id), 
            "price_sats": config.PRICE_SATS_PER_READING,
            "payment_address": device_state.address,
            "payment_instructions": f"Send {config.PRICE_SATS_PER_READING} sats to {device_state.address}. OP_RETURN: 'SENSORA_PAY:{latest_id}'."
        })

    @app.route("/claim_reading/<int:reading_id>", methods=['POST'])
    def claim_reading_route(reading_id: int):
        data = request.get_json();
        if not data or "payment_txid" not in data:
            abort(400, "Missing 'payment_txid' in JSON body.")
        
        consumer_payment_txid = data['payment_txid']
        if not database.reading_exists(reading_id):
            abort(404, f"Reading ID {reading_id} not found.")
        
        payment_verified, _ = bsv_utils.check_payment_on_chain(reading_id, device_state.address, device_state.locking_script_hex, consumer_payment_txid)
        if not payment_verified:
            raise PaymentRequiredError("Payment verification failed.")

        access_token = bsv_utils.generate_access_token()
        expiry = int(time.time()) + config.TOKEN_EXPIRY_SECONDS
        
        try:
            database.save_access_token(reading_id, consumer_payment_txid, access_token, expiry, config.TOKEN_MAX_USAGE)
            database.update_reading_as_paid(reading_id, consumer_payment_txid)
            return jsonify({"message": "Access token generated.", "access_token": access_token, "data_endpoint": f"/fetch_data/{reading_id}", "expires_at": expiry}), 201
        except sqlite3.IntegrityError:
            abort(409, "Token for this payment may have already been claimed.")
        except Exception:
            logger.exception("Server error while issuing token."); abort(500)

    @app.route("/fetch_data/<int:reading_id>", methods=['GET'])
    def fetch_data_route(reading_id: int):
        logger.info("fetch_data_route function is being CALLED.") # Also add this one

        token = request.args.get("token")
        if not token: abort(401, "Access token is missing.")

        token_details = database.get_token_details(reading_id, token)
        if not token_details: abort(403, "Invalid or mismatched token.")
        if int(time.time()) >= token_details["expiry_timestamp"]: abort(403, "Token expired.")
        if token_details["usage_count"] >= token_details["max_usage"]: abort(403, "Token usage limit reached.")
        
        reading_data = database.get_reading(reading_id)
        if not reading_data: 
            abort(404, "Data not found (internal inconsistency).")
        
        database.increment_token_usage(token_details["token_id"])

        return jsonify({
            "reading_id": reading_data["reading_id"], "timestamp": reading_data["timestamp"],
            "sensor_values": (reading_data["value_temp"], reading_data["value_humid"]),
            "data_type_code": reading_data["data_type_code"],
            "onchain_proof_txid": reading_data["onchain_proof_txid"]
        })

    @app.route('/status', methods=['GET'])
    def get_agent_status():
        reputation_data = reputation.calculate_reputation_score()
        return jsonify({
            "status": "online",
            "timestamp_utc": int(time.time()),
            "reputation": reputation_data
        })


    @app.route("/batch/fetch/<string:batch_id>", methods=['GET'])
    def batch_fetch_route(batch_id: str):
        logger.info(f"--- ENTERING batch_fetch_route for batch_id: {batch_id[:8]}... ---")
        
        token = request.args.get("token")
        if not token: 
            logger.warning("FETCH FAIL: No token provided.")
            abort(401, "Access token is missing.")

        logger.info("Verifying token...")
        token_details = database.get_token_details(batch_id, token)
        if not token_details: 
            logger.error("FETCH FAIL: Token details not found in database. Invalid token.")
            abort(403, "Invalid or mismatched token for this batch.")
        
        logger.info("Token verified. Now checking cache for batch details...")
        batch_details = database.get_batch_details_for_fetch(batch_id)
        if not batch_details:
            logger.error(f"FETCH FAIL: get_cached_batch_details returned None for batch_id {batch_id[:8]}...")
            abort(404, "Could not find details for this batch ID (internal inconsistency).")

        logger.info(f"Batch details found. Fetching readings from {batch_details['start_timestamp']} to {batch_details['end_timestamp']}...")
        start_ts = batch_details['start_timestamp']
        end_ts = batch_details['end_timestamp']
        readings = database.get_readings_in_range(start_ts, end_ts)

        if not readings:
            logger.error("FETCH FAIL: get_readings_in_range returned an empty list.")
            abort(404, "No data found for the given time range (internal inconsistency).")
        
        logger.info(f"Successfully fetched {len(readings)} readings. Incrementing token usage.")
        database.increment_token_usage(token_details["token_id"])

        logger.info("--- SUCCESS: Returning batch data. ---")
        return jsonify(readings)
    @app.route("/batch/price", methods=['POST'])
    def batch_price_route():
        data = request.get_json()
        if not data or "start_timestamp" not in data or "end_timestamp" not in data:
            abort(400, "Request must include 'start_timestamp' and 'end_timestamp'.")

        start_ts = data['start_timestamp']
        end_ts = data['end_timestamp']

        # 1. Count the number of readings in the requested range.
        num_readings = database.count_readings_in_range(start_ts, end_ts)

        if num_readings == 0:
            return jsonify({"message": "No readings found in the specified time range."}), 404

        # 2. --- THIS IS THE NEW LIMIT CHECK ---
        max_size = config.BATCH_MAX_SIZE 
        if max_size > 0 and num_readings > max_size:
            # The request is too large. Reject it with a helpful error message.
            return jsonify({
                "error": "Batch size limit exceeded",
                "message": f"Your request for {num_readings} readings exceeds the agent's limit of {max_size}. Please request a smaller time range.",
                "limit": max_size,
                "requested": num_readings
            }), 413 # 413 Payload Too Large is a fitting HTTP status code

        # 3. If the check passes, proceed with price calculation.
        total_price = num_readings * config.PRICE_SATS_PER_READING
        
        # Generate a unique ID for this specific batch request.
        batch_id = hashlib.sha256(f"{start_ts}:{end_ts}:{device_state.address}".encode()).hexdigest()
        
        try:
            # --- THIS IS THE CORRECTED LINE ---
            database.cache_batch_details(batch_id, num_readings, total_price, start_ts, end_ts)
            # --- END OF CORRECTION ---
            
            logger.info(f"Cached price quote for batch_id {batch_id[:8]}...: {num_readings} readings for {total_price} sats.")
        except Exception:
            logger.exception("Failed to cache batch details. Cannot provide a quote.")
            abort(500, "Internal error occurred while preparing the batch quote.")
        return jsonify({
            "batch_id": batch_id,
            "num_readings": num_readings,
            "total_price_sats": total_price,
            "payment_address": device_state.address,
            "payment_instructions": f"Send {total_price} sats to {device_state.address}. OP_RETURN: 'SENSORA_PAY:{batch_id}'."
        })
        
    @app.route("/batch/claim", methods=['POST'])
    def batch_claim_route():
        logger.info("batch_claim_route function is being CALLED.") # Also add this one

        """
        Verifies a payment for a pre-quoted batch and issues an access token.
        """
        data = request.get_json()
        if not data or "batch_id" not in data or "payment_txid" not in data:
            abort(400, "Request must include 'batch_id' and 'payment_txid'.")

        batch_id = data['batch_id']
        consumer_payment_txid = data['payment_txid']

        # 1. Retrieve the original quote details from our cache/DB.
        # This prevents the client from trying to claim a batch that wasn't quoted.
        batch_details = database.get_cached_batch_details(batch_id)
        if not batch_details:
            abort(404, f"Batch ID '{batch_id}' not found or has expired. Please request a new price quote.")

        expected_price = batch_details['total_price_sats']

        # 2. Verify the payment on-chain.
        # This util function checks if the txid paid the correct amount to the right address
        # and has the correct OP_RETURN payload.
        op_return_payload = f"SENSORA_PAY:{batch_id}"
        payment_verified, _ = bsv_utils.check_batch_payment_on_chain(
            op_return_payload,
            device_state.address, 
            device_state.locking_script_hex, 
            consumer_payment_txid,
            expected_price
        )
    
        
        if not payment_verified:
            # We use a custom error class here from your existing code
            raise PaymentRequiredError("Batch payment verification failed. Ensure the amount and OP_RETURN are correct.")

        # 3. If payment is verified, generate and save a single access token for the batch.
        access_token = bsv_utils.generate_access_token()
        expiry = int(time.time()) + config.TOKEN_EXPIRY_SECONDS
        
        try:
            # Note: We are now associating the token with the batch_id.
            # Your save_access_token function may need a slight adjustment if it
            # assumes the first argument is always an integer reading_id.
            database.save_access_token(batch_id, consumer_payment_txid, access_token, expiry, config.TOKEN_MAX_USAGE)
            
            # We can also mark the cached batch as "claimed"
            database.mark_batch_as_claimed(batch_id)

            return jsonify({
                "message": "Access token for batch generated.", 
                "access_token": access_token, 
                "data_endpoint": f"/batch/fetch/{batch_id}", 
                "expires_at": expiry
            }), 201
        except sqlite3.IntegrityError:
            abort(409, "Token for this batch payment may have already been claimed.")
        except Exception:
            logger.exception("Server error while issuing batch token.")
            abort(500)



        