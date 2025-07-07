# sensora_agent/src/routes.py

import time
import json
import logging
import sqlite3
from flask import Flask, request, jsonify, abort, render_template
from werkzeug.exceptions import HTTPException

# Import from our refactored modules
from . import config
from . import database
from . import bsv_utils
from . import reputation

logger = logging.getLogger(__name__)

# --- Custom Exception and Error Handlers ---

class PaymentRequiredError(HTTPException):
    code = 402
    description = 'Payment for this resource has not been detected.'

def register_error_handlers(app):
    """Registers custom and standard HTTP error handlers for the Flask app."""
    @app.errorhandler(PaymentRequiredError)
    def handle_payment_required(error: PaymentRequiredError):
        response = jsonify({"error": "Payment Required", "message": error.description, "status_code": error.code})
        response.status_code = error.code
        return response

    @app.errorhandler(409)
    def conflict_error_handler(error: HTTPException):
        desc = getattr(error, 'description', "A conflict occurred with the current state of the resource.")
        response = jsonify({"error": "Conflict", "message": desc, "status_code": 409})
        response.status_code = 409
        return response

    @app.errorhandler(404)
    def not_found_error_handler(error: HTTPException):
        desc = getattr(error, 'description', "The requested resource was not found.")
        response = jsonify({"error": "Not Found", "message": desc, "status_code": 404})
        response.status_code = 404
        return response

    @app.errorhandler(400)
    def bad_request_error_handler(error: HTTPException):
        desc = getattr(error, 'description', "The request was malformed.")
        response = jsonify({"error": "Bad Request", "message": desc, "status_code": 400})
        response.status_code = 400
        return response

    @app.errorhandler(500)
    def internal_server_error_handler(error: HTTPException):
        desc = getattr(error, 'description', "An internal server error occurred.")
        logger.exception(f"SERVER ERROR 500: Unhandled exception caught by error handler: {error}")
        response = jsonify({"error": "Internal Server Error", "message": desc, "status_code": 500})
        response.status_code = 500
        return response

# --- Route Definitions ---

def register_routes(app, device_state):
    """
    Registers all Flask routes with the application.
    Passes in `device_state` to access shared state like keys and addresses.
    """

    @app.route("/", methods=['GET']) 
    def dashboard():
        readings = database.get_latest_readings()
        tokens = database.get_latest_tokens()
        last_adv_txid = database.get_key_value('last_adv_txid') or "N/A"
        
        # --- NEW: Get reputation data ---
        reputation_data = reputation.calculate_reputation_score()
        
        # Prepare chart data
        chart_data = { "labels": [], "temp_data": [], "humid_data": [] }
        for reading in reversed(readings):
            if reading.get("paid") and reading.get("sensor_values"):
                chart_data["labels"].append(time.strftime('%H:%M:%S', time.localtime(reading["reading_id"])))
                chart_data["temp_data"].append(reading["value_temp"])
                chart_data["humid_data"].append(reading["value_humid"])

        return render_template(
            "index.html", 
            device_ipv6=config.DEVICE_IPV6_ADDRESS,
            device_payment_address=device_state.address,
            price_sats=config.PRICE_SATS_PER_READING,
            readings=readings,
            tokens=tokens,
            last_adv_txid=last_adv_txid,
            reputation_data=reputation_data,
            chart_data=json.dumps(chart_data),
            DATA_TYPE_TEMP_HUMID=config.DATA_TYPE_TEMP_HUMID,
            DATA_CAPTURE_INTERVAL_SECONDS=config.DATA_CAPTURE_INTERVAL_SECONDS,
            auto_refresh_interval=30,
            current_year=time.strftime('%Y'),
            time=time
        )

    @app.route("/price", methods=['GET'])
    def get_price_route():
        latest_id = database.get_latest_reading_id() or int(time.time())
        return jsonify({
            "current_reading_id": str(latest_id), 
            "price_sats": config.PRICE_SATS_PER_READING,
            "payment_address": device_state.address,
            "payment_instructions": f"Send {config.PRICE_SATS_PER_READING} sats to {device_state.address}. OP_RETURN: 'SENSORA_PAY:{latest_id}'."
        })
    @app.route("/status", methods=['GET'])
    def get_status_route():
        """Returns the current status and reputation of the sensor."""
        reputation_data = reputation.calculate_reputation_score()
        
        status_payload = {
            "device_id": config.DEVICE_IPV6_ADDRESS,
            "bsv_address": device_state.address,
            "status": "online",
            "timestamp_utc": int(time.time()),
            "reputation": reputation_data
        }
        return jsonify(status_payload)
    @app.route("/claim_reading/<int:reading_id>", methods=['POST'])
    def claim_reading_route(reading_id: int):
        data = request.get_json()
        if not data or "payment_txid" not in data:
            abort(400, "Missing 'payment_txid' in JSON body.")
        
        consumer_payment_txid = data["payment_txid"]
        logger.info(f"[CLAIM_TOKEN] Attempt to claim reading {reading_id} with payment_txid {consumer_payment_txid[:10]}...")

        if not database.reading_exists(reading_id):
            abort(404, f"Reading ID {reading_id} not found.")

        payment_verified, _ = bsv_utils.check_payment_on_chain(
            reading_id, device_state.address, 
            device_state.locking_script_hex, specific_txid_to_check=consumer_payment_txid
        )

        if not payment_verified:
            # --- START CORRECTION ---
            # Instead of abort(402, ...), raise the custom exception
            raise PaymentRequiredError("Payment verification failed for the specified reading ID and payment TXID.")
            # --- END CORRECTION ---

        access_token = bsv_utils.generate_access_token()
        expiry = int(time.time()) + config.TOKEN_EXPIRY_SECONDS
        
        try:
            database.save_access_token(reading_id, consumer_payment_txid, access_token, expiry, config.TOKEN_MAX_USAGE)
            database.update_reading_as_paid(reading_id, consumer_payment_txid)
            return jsonify({
                "message": "Access token generated successfully.", "access_token": access_token,
                "data_endpoint": f"/fetch_data/{reading_id}", "expires_at": expiry
            }), 201
        except sqlite3.IntegrityError:
            abort(409, "Token for this payment has already been claimed.")
        except Exception:
            logger.exception("Server error while issuing token.")
            abort(500, "Server error issuing token.")

    @app.route("/fetch_data/<int:reading_id>", methods=['GET'])
    def fetch_data_route(reading_id: int):
        token = request.args.get("token")
        if not token: abort(401, "Access token is missing.")

        token_details = database.get_token_details(reading_id, token)
        if not token_details: abort(403, "Invalid or mismatched token.")
        
        if int(time.time()) >= token_details["expiry_timestamp"]: abort(403, "Token expired.")
        if token_details["usage_count"] >= token_details["max_usage"]: abort(403, "Token usage limit reached.")
        
        reading_data = database.get_reading(reading_id)
        if not reading_data: abort(404, "Data not found (internal inconsistency).")
        
        database.increment_token_usage(token_details["token_id"])

        return jsonify({
            "reading_id": reading_data["reading_id"], "timestamp": reading_data["timestamp"],
            "sensor_values": (reading_data["value_temp"], reading_data["value_humid"]),
            "data_type_code": reading_data["data_type_code"],
            "onchain_proof_txid": reading_data["onchain_proof_txid"]
        })


@app.route('/status', methods=['GET'])
def get_agent_status():
    """Returns the agent's current operational status and reputation metrics."""
    # This uses the reputation module we already built for the agent's own dashboard
    reputation_data = reputation.calculate_reputation_score()
    
    status_payload = {
        "status": "online",
        "timestamp_utc": int(time.time()),
        "reputation": reputation_data
    }
    return jsonify(status_payload)