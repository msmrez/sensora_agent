# sensora_agent/src/openapi_spec.py
# OpenAPI 2.0 (Swagger) specification for the Sensōra Agent API.

SWAGGER_TEMPLATE = {
    "swagger": "2.0",
    "info": {
        "title": "Sensōra Agent API",
        "description": (
            "API for the Sensōra IoT data agent. The agent captures sensor data, "
            "anchors proofs on the BSV blockchain, and sells access to single readings "
            "or batch ranges for BSV micropayments. Payment is verified on-chain; "
            "then an access token is issued for fetching data."
        ),
        "version": "1.0.0",
    },
    "host": "",  # Use request host
    "basePath": "/",
    "schemes": ["http", "https"],
    "consumes": ["application/json"],
    "produces": ["application/json", "text/html"],
    "paths": {
        "/": {
            "get": {
                "summary": "Dashboard",
                "description": "Web dashboard with device info, recent readings, tokens, and batch purchases.",
                "responses": {"200": {"description": "HTML dashboard"}},
            }
        },
        "/price": {
            "get": {
                "summary": "Get price for single reading",
                "description": "Returns payment address, price, a reading ID (the latest with a mined proof, or `reading_id`) and its commitment. Pay this amount to the address with OP_RETURN `SENSORA_PAY:{reading_id}:{commitment}`.",
                "parameters": [{"name": "reading_id", "in": "query", "type": "integer", "required": False}],
                "responses": {
                    "200": {
                        "description": "Price and payment info",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "current_reading_id": {"type": "string"},
                                "commitment": {"type": "string", "description": "sha256 of the reading's proof data hash"},
                                "price_sats": {"type": "integer"},
                                "payment_address": {"type": "string"},
                                "payment_instructions": {"type": "string"},
                            },
                        },
                    },
                    "400": {"description": "reading_id is not an integer"},
                    "404": {"description": "No reading with a mined proof yet, or reading_id has none"},
                },
            }
        },
        "/claim_reading/{reading_id}": {
            "post": {
                "summary": "Claim access token for a single reading",
                "description": "After paying on-chain, send the payment TXID to receive an access token for fetching the reading.",
                "parameters": [
                    {"name": "reading_id", "in": "path", "type": "integer", "required": True},
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {
                            "type": "object",
                            "required": ["payment_txid", "signature"],
                            "properties": {"payment_txid": {"type": "string"},
                                           "signature": {"type": "string", "description": "Bitcoin Signed Message (base64) of SENSORA_CLAIM:<id>:<payment_txid>, by a key that signed an input of the payment"}},
                        },
                    },
                ],
                "responses": {
                    "201": {
                        "description": "Token issued",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string"},
                                "access_token": {"type": "string"},
                                "data_endpoint": {"type": "string"},
                                "expires_at": {"type": "integer"},
                            },
                        },
                    },
                    "400": {"description": "Missing payment_txid or signature"},
                    "403": {"description": "Signature is not from a key that funded the payment"},
                    "402": {"description": "Payment not verified on-chain"},
                    "404": {"description": "Reading not found"},
                    "409": {"description": "Token already claimed for this payment"},
                },
            }
        },
        "/fetch_data/{reading_id}": {
            "get": {
                "summary": "Fetch a single reading (requires token)",
                "description": "Returns the raw reading data. Query param `token` must be the access token from claim_reading.",
                "parameters": [
                    {"name": "reading_id", "in": "path", "type": "integer", "required": True},
                    {"name": "token", "in": "query", "type": "string", "required": True},
                ],
                "responses": {
                    "200": {
                        "description": "Reading data",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "reading_id": {"type": "integer"},
                                "timestamp": {"type": "integer"},
                                "sensor_values": {"type": "array", "items": {"type": "number"}},
                                "data_type_code": {"type": "integer"},
                                "onchain_proof_txid": {"type": "string"},
                            },
                        },
                    },
                    "401": {"description": "Token missing"},
                    "403": {"description": "Invalid, expired, or usage limit reached"},
                    "404": {"description": "Reading not found"},
                },
            }
        },
        "/status": {
            "get": {
                "summary": "Agent status",
                "description": "Returns online status and reputation metrics.",
                "responses": {
                    "200": {
                        "description": "Status and reputation",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string", "example": "online"},
                                "timestamp_utc": {"type": "integer"},
                                "reputation": {
                                    "type": "object",
                                    "properties": {
                                        "uptime_percentage": {"type": "number"},
                                        "stamp_success_rate": {"type": "number"},
                                        "total_stamps_recorded": {"type": "integer"},
                                        "total_advertisements_sent": {"type": "integer"},
                                    },
                                },
                            },
                        },
                    }
                },
            }
        },
        "/batch/price": {
            "post": {
                "summary": "Get price for a batch of readings",
                "description": "Request a quote for all readings in a time range. Pay total_price_sats with OP_RETURN `SENSORA_PAY:{batch_id}:{commitment}`.",
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {
                            "type": "object",
                            "required": ["start_timestamp", "end_timestamp"],
                            "properties": {
                                "start_timestamp": {"type": "integer", "description": "Unix timestamp (inclusive)"},
                                "end_timestamp": {"type": "integer", "description": "Unix timestamp (inclusive)"},
                            },
                        },
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Batch quote",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "batch_id": {"type": "string"},
                                "num_readings": {"type": "integer"},
                                "total_price_sats": {"type": "integer"},
                                "payment_address": {"type": "string"},
                                "payment_instructions": {"type": "string"},
                            },
                        },
                    },
                    "400": {"description": "Missing start_timestamp or end_timestamp"},
                    "400": {"description": "Timestamps missing, not integers, or start > end"},
                    "404": {"description": "No readings in range"},
                    "413": {"description": "Batch size exceeds agent limit"},
                },
            }
        },
        "/batch/claim": {
            "post": {
                "summary": "Claim access token for a batch",
                "description": "After paying for a batch quote, send batch_id and payment_txid to receive an access token.",
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {
                            "type": "object",
                            "required": ["batch_id", "payment_txid", "signature"],
                            "properties": {
                                "batch_id": {"type": "string"},
                                "payment_txid": {"type": "string"},
                                "signature": {"type": "string", "description": "Bitcoin Signed Message (base64) of SENSORA_CLAIM:<id>:<payment_txid>, by a key that signed an input of the payment"},
                            },
                        },
                    }
                ],
                "responses": {
                    "201": {
                        "description": "Token issued",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string"},
                                "access_token": {"type": "string"},
                                "data_endpoint": {"type": "string"},
                                "expires_at": {"type": "integer"},
                            },
                        },
                    },
                    "400": {"description": "Missing batch_id, payment_txid or signature"},
                    "403": {"description": "Signature is not from a key that funded the payment"},
                    "402": {"description": "Payment not verified"},
                    "404": {"description": "Batch quote not found or expired"},
                    "409": {"description": "Batch already claimed, or token already claimed for this payment"},
                },
            }
        },
        "/batch/fetch/{batch_id}": {
            "get": {
                "summary": "Fetch batch data (requires token)",
                "description": "Returns all readings in the batch time range. Query param `token` from batch/claim.",
                "parameters": [
                    {"name": "batch_id", "in": "path", "type": "string", "required": True},
                    {"name": "token", "in": "query", "type": "string", "required": True},
                ],
                "responses": {
                    "200": {
                        "description": "Array of reading objects",
                        "schema": {"type": "array", "items": {"type": "object"}},
                    },
                    "401": {"description": "Token missing"},
                    "403": {"description": "Invalid or expired token"},
                    "404": {"description": "Batch not found or no data"},
                },
            }
        },
        "/debug/batches": {
            "get": {
                "summary": "Latest claimed batches (debug)",
                "description": "Returns the same claimed-batch list shown on the dashboard.",
                "responses": {"200": {"description": "List of claimed batch records"}},
            }
        },
    },
}
