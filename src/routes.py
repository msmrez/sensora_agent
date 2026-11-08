# sensora_agent/src/routes.py

import time
import json
import logging
import sqlite3
from flask import request, jsonify, abort, render_template
from werkzeug.exceptions import HTTPException
import hashlib
import secrets
from . import config, database, bsv_utils, reputation, protocols

logger = logging.getLogger(__name__)


class PaymentRequiredError(HTTPException):
    code = 402
    description = 'Payment for this resource has not been detected.'

def register_all_routes(app, device_state):
    """Registers all Flask error handlers and routes with the application."""
    # Error handlers
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
        context = {}
        try:
            context['readings'] = database.get_latest_readings()
            context['tokens'] = database.get_latest_tokens()

            context['batches'] = database.get_latest_claimed_batches(limit=10)
            context['last_adv_txid'] = database.get_key_value('last_adv_txid') or "N/A"
            context['reputation_data'] = reputation.calculate_reputation_score()

            context['device_ipv6'] = config.DEVICE_IPV6_ADDRESS
            context['device_payment_address'] = device_state.address
            context['price_sats'] = config.PRICE_SATS_PER_READING
            context['DATA_CAPTURE_INTERVAL_SECONDS'] = config.DATA_CAPTURE_INTERVAL_SECONDS
            context['auto_refresh_interval'] = 30
            context['current_year'] = time.strftime('%Y')
            context['time'] = time

            return render_template("index.html", **context)

        except Exception as e:
            logger.exception("--- UNHANDLED EXCEPTION IN DASHBOARD ROUTE ---")
            return "<h1>Internal Server Error</h1><p>An error occurred while building the dashboard. Please check the server logs for the full traceback.</p>", 500

    def single_commitment(reading: dict) -> str:
        """The commitment for one reading: the batch rule applied to a batch of one proof."""
        return protocols.batch_commitment(
            [protocols.data_hash_from_proof_payload(bytes.fromhex(reading["op_return_payload_hex"]))])

    @app.route("/price")
    def get_price_route():
        requested = request.args.get("reading_id")
        if requested is not None:
            if not requested.isdigit():
                abort(400, "'reading_id' must be an integer.")
            reading = database.get_reading(int(requested))
            if not reading or reading.get("proof_status") != "mined":
                return jsonify({"error": "Not Found", "message": f"Reading {requested} has no mined proof."}), 404
        else:
            latest_id = database.get_latest_mined_reading_id()
            if latest_id is None:
                return jsonify({"error": "Not Found", "message": "No reading with a mined proof is available yet."}), 404
            reading = database.get_reading(latest_id)
        reading_id = reading["reading_id"]
        commitment = single_commitment(reading)
        return jsonify({
            "current_reading_id": str(reading_id),
            "commitment": commitment,
            "price_sats": config.PRICE_SATS_PER_READING,
            "payment_address": device_state.address,
            "payment_instructions": f"Send {config.PRICE_SATS_PER_READING} sats to {device_state.address}. OP_RETURN: 'SENSORA_PAY:{reading_id}:{commitment}'."
        })

    @app.route("/claim_reading/<int:reading_id>", methods=['POST'])
    def claim_reading_route(reading_id: int):
        data = request.get_json();
        if not data or "payment_txid" not in data:
            abort(400, "Missing 'payment_txid' in JSON body.")
        
        consumer_payment_txid = data['payment_txid']
        reading = database.get_reading(reading_id)
        if not reading:
            abort(404, f"Reading ID {reading_id} not found.")
        if reading.get("proof_status") != "mined":
            abort(409, f"Reading {reading_id} does not have a mined proof yet.")
        
        payment_verified, _ = bsv_utils.check_payment_on_chain(reading_id, device_state.address, device_state.locking_script_hex,
                                                               consumer_payment_txid, commitment=single_commitment(reading))
        if not payment_verified:
            raise PaymentRequiredError("Payment verification failed.")
        if not bsv_utils.payment_confirmed_enough(consumer_payment_txid, config.PRICE_SATS_PER_READING):
            return jsonify({"error": "Payment Required", "reason": "unconfirmed",
                            "message": f"Payments above {config.MAX_UNCONFIRMED_SATS} sats must be mined before data is released. Retry later."}), 402
        if not data.get("signature"):
            abort(400, "Missing 'signature': sign " + bsv_utils.claim_message(reading_id, consumer_payment_txid) + " with a key that funded the payment.")
        if not bsv_utils.claim_signed_by_payer(consumer_payment_txid, reading_id, data["signature"]):
            abort(403, "Claim signature does not match any key that signed the payment.")

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
        token = request.args.get("token")
        if not token:
            abort(401, "Access token is missing.")

        token_details = database.get_token_details(batch_id, token)
        if not token_details:
            abort(403, "Invalid or mismatched token for this batch.")
        # Same limits as /fetch_data: before this check a batch token never expired and had no usage cap.
        if int(time.time()) >= token_details["expiry_timestamp"]: abort(403, "Token expired.")
        if token_details["usage_count"] >= token_details["max_usage"]: abort(403, "Token usage limit reached.")

        batch_details = database.get_batch_details_for_fetch(batch_id)
        if not batch_details:
            abort(404, "Could not find details for this batch ID (internal inconsistency).")

        if batch_details.get('reading_ids'):
            # v2: deliver exactly the readings the payment committed to, with their proof payloads.
            readings = database.get_readings_by_ids(json.loads(batch_details['reading_ids']))
        else:
            readings = database.get_readings_in_range(batch_details['start_timestamp'], batch_details['end_timestamp'])

        if not readings:
            abort(404, "No data found for the given time range (internal inconsistency).")

        database.increment_token_usage(token_details["token_id"])
        if batch_details.get('commitment'):
            return jsonify({"batch_id": batch_id, "commitment": batch_details['commitment'], "readings": readings})
        return jsonify(readings)
    @app.route("/batch/price", methods=['POST'])
    def batch_price_route():
        data = request.get_json()
        if not data or "start_timestamp" not in data or "end_timestamp" not in data:
            abort(400, "Request must include 'start_timestamp' and 'end_timestamp'.")

        start_ts = data['start_timestamp']
        end_ts = data['end_timestamp']
        # bool is a subclass of int in Python, so check the exact type.
        if type(start_ts) is not int or type(end_ts) is not int or start_ts > end_ts:
            abort(400, "'start_timestamp' and 'end_timestamp' must be integers (unix seconds) with start <= end.")

        # 1. v2: only readings with a mined proof are offered, so every one can be verified by the buyer.
        readings = database.get_provable_readings_in_range(start_ts, end_ts)
        num_readings = len(readings)

        if num_readings == 0:
            return jsonify({"message": "No readings with a mined proof in the specified time range."}), 404

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
        
        # The commitment binds the payment to the exact on-chain proofs being sold.
        commitment = protocols.batch_commitment(
            [protocols.data_hash_from_proof_payload(bytes.fromhex(r["op_return_payload_hex"])) for r in readings])
        # A nonce keeps every quote distinct, so re-quoting a window never overwrites a claimed batch.
        batch_id = hashlib.sha256(f"{start_ts}:{end_ts}:{device_state.address}:{commitment}:{secrets.token_hex(8)}".encode()).hexdigest()

        try:
            database.cache_batch_details(batch_id, num_readings, total_price, start_ts, end_ts,
                                         commitment, [r["reading_id"] for r in readings])
            logger.info(f"Cached price quote for batch_id {batch_id[:8]}...: {num_readings} readings for {total_price} sats.")
        except Exception:
            logger.exception("Failed to cache batch details. Cannot provide a quote.")
            abort(500, "Internal error occurred while preparing the batch quote.")
        return jsonify({
            "batch_id": batch_id,
            "commitment": commitment,
            "num_readings": num_readings,
            "total_price_sats": total_price,
            "payment_address": device_state.address,
            "payment_instructions": f"Send {total_price} sats to {device_state.address}. OP_RETURN: 'SENSORA_PAY:{batch_id}:{commitment}'."
        })
    @app.route("/batch/claim", methods=['POST'])
    def batch_claim_route():
        """Verifies a payment for a pre-quoted batch and issues an access token."""
        data = request.get_json()
        if not data or "batch_id" not in data or "payment_txid" not in data:
            abort(400, "Request must include 'batch_id' and 'payment_txid'.")

        batch_id = data['batch_id']
        consumer_payment_txid = data['payment_txid']

        batch_details = database.get_cached_batch_details(batch_id)
        if not batch_details:
            abort(404, f"Batch ID '{batch_id}' not found or has expired. Please request a new price quote.")

        expected_price = batch_details['total_price_sats']
        op_return_payload = f"SENSORA_PAY:{batch_id}"
        if batch_details.get('commitment'):
            op_return_payload += f":{batch_details['commitment']}"

        payment_verified, _ = bsv_utils.check_batch_payment_on_chain(
            op_return_payload,
            device_state.address, 
            device_state.locking_script_hex, 
            consumer_payment_txid,
            expected_price
        )
        
        if not payment_verified:
            raise PaymentRequiredError("Batch payment verification failed.")
        if not bsv_utils.payment_confirmed_enough(consumer_payment_txid, expected_price):
            return jsonify({"error": "Payment Required", "reason": "unconfirmed",
                            "message": f"Payments above {config.MAX_UNCONFIRMED_SATS} sats must be mined before data is released. Retry later."}), 402
        if not data.get("signature"):
            abort(400, "Missing 'signature': sign " + bsv_utils.claim_message(batch_id, consumer_payment_txid) + " with a key that funded the payment.")
        if not bsv_utils.claim_signed_by_payer(consumer_payment_txid, batch_id, data["signature"]):
            abort(403, "Claim signature does not match any key that signed the payment.")

        access_token = bsv_utils.generate_access_token()
        expiry = int(time.time()) + config.TOKEN_EXPIRY_SECONDS
        
        try:
            # One transaction: of two concurrent claims for the same batch, only one gets a token.
            if not database.claim_batch_with_token(batch_id, consumer_payment_txid, access_token, expiry,
                                                    config.TOKEN_MAX_USAGE):
                abort(409, "This batch has already been claimed.")

            return jsonify({
                "message": "Access token for batch generated.", 
                "access_token": access_token, 
                "data_endpoint": f"/batch/fetch/{batch_id}", 
                "expires_at": expiry
            }), 201
        except sqlite3.IntegrityError:
            abort(409, "Token for this batch payment may have already been claimed.")
        except HTTPException:
            raise
        except Exception:
            logger.exception("Server error while issuing batch token.")
            abort(500)

    @app.route("/debug/batches", methods=['GET'])
    def debug_batches_route():
        """Returns the latest claimed batches (same data as dashboard)."""
        try:
            return jsonify(database.get_latest_claimed_batches(limit=10))
        except Exception as e:
            logger.exception("Error in get_latest_claimed_batches: %s", e)
            return jsonify({"error": str(e)}), 500