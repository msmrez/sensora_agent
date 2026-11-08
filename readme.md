# Sensōra Agent - An Autonomous IoT Data Provider

This repository contains the complete source code for the **Sensōra Agent**, a Python application designed to run on any Linux-based server or IoT device (like a Raspberry Pi). It empowers any connected sensor or data source to become an autonomous economic agent on the BSV blockchain.

The agent periodically captures data, anchors a cryptographic proof of that data on-chain, and sells access to the raw data for BSV micropayments. It is a core component of the decentralized **Sensōra IoT Data Marketplace**.

### The Sensōra Ecosystem

The Agent is the data-producing component in a larger P2P network. It works in conjunction with the [Sensōra Indexer & Registry](https://github.com/msmrez/sensora_indexer) and can be tested with the [Sensōra Client](https://github.com/msmrez/sensora_client).

```
┌─────────────────┐   1. Advertise Service   ┌────────────────┐   4. Discover Agent   ┌──────────────┐
│  Sensōra Agent  │  ─────────────────────>  │                │  <───────────────────  │              │
│ (This Project)  │                          │ BSV Blockchain │                        │ Sensōra      │
│ on IoT Device   │   2. Stamp Data Proof    │                │   5. Purchase Data     │ Client       │
│                 │  <─────────────────────  │ (Ledger)       │  ───────────────────>  │              │
└─────────────────┘   3. Listen for Ads      └────────────────┘   6. Verify Proof      └──────────────┘
       ^            <─────────────────────                          ^
       │                                  │                         │
       │ 7. Query for Agents              │ 3a. Index results       │
       │                                  │                         │
       └───────────────────────────────────[ Sensōra Indexer ]───────┘
```

## Core Features

- **Autonomous Operation:** Runs as a background service, requiring no manual intervention.
- **On-Chain Data Stamping:** Creates immutable, timestamped proofs of data integrity on the BSV blockchain using the `SENSORA_PROOF` protocol.
- **Rich On-Chain Advertisement:** Broadcasts its services with human-readable names, descriptions, and units using the flexible `SENSORA_ADV` protocol.
- **Direct Monetization:** Sells both single data points and historical batches for BSV micropayments via a built-in API.
- **Secure Data Access:** Implements a token-based access system to ensure only verified payers can retrieve data.
- **Persistent State:** Uses a local SQLite database (`sensora_device.db`) to track all readings, payments, and access tokens, ensuring resilience against restarts.
- **Easy Deployment:** Can be run directly from source or as a simple Docker container.

## On-Chain Data Protocols

The agent **creates** two distinct `OP_RETURN` protocols on the BSV blockchain.

### 1. Advertisement Protocol (`SENSORA_ADV`)

Broadcast periodically to announce the sensor's services. This version uses a flexible JSON payload to describe services.

- **Prefix:** `SENSORA_ADV` (ASCII)
- **Payload Structure:** `[Prefix (11B)][DevIDType (1B)][DevID (16B)][Port (2B)][GeoLen (1B)][Geohash (Var)][ServicesJSON (Var)][Timestamp (8B)]`

The `ServicesJSON` is a UTF-8 encoded JSON string representing a list of service objects, for example:

```json
[
  {
    "type": 1,
    "name": "Temperature/Humidity",
    "desc": "Measures ambient temperature and relative humidity.",
    "units": ["°C", "%RH"],
    "price": 50
  }
]
```

### 2. Data Proof Protocol (`SENSORA_PROOF`)

Broadcast for every new data point to provide an immutable proof of integrity.

- **Prefix:** `SENSORA_PROOF` (ASCII)
- **Payload Structure:**

| Field          | Size (Bytes) | Data Type               | Description                   |
| :------------- | :----------- | :---------------------- | :---------------------------- |
| Prefix         | **13**       | ASCII                   | `SENSORA_PROOF`               |
| Version        | 1            | Unsigned Byte           | Protocol version (`0x01`)     |
| Device ID Type | 1            | Unsigned Byte           | `0x01` for Full IPv6          |
| Device ID      | 16           | Bytes                   | The agent's IPv6 address      |
| Reading ID     | 8            | Unsigned Long Long (BE) | Unix timestamp of the reading |
| Data Type      | 2            | Unsigned Short (BE)     | Code for this reading's type  |
| Data Hash      | 32           | Bytes                   | SHA-256 hash of the data      |

## Getting Started

We recommend running the agent using Docker for the easiest and most reliable setup. Instructions for running from source are also provided.

### Docker Quick Start (Recommended)

#### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed on your system.
- A public, static IPv6 address on your host machine.

#### Instructions

1.  **Clone the Repository:**

    ```bash
    git clone git@github.com:msmrez/sensora_agent.git
    cd sensora_agent
    ```

2.  **Create Your Configuration File:**
    The agent is configured using a standard `config.ini` file. Copy the example to create your own.

    ```bash
    cp config.ini.example config.ini
    ```

3.  **Edit `config.ini`:**
    Open the `config.ini` file in a text editor and fill in your details. This is the **only file you need to edit**.

    - `wif`: Your funded BSV private key.
    - `ipv6_address`: The public IPv6 address of the machine you are on.
    - `port`: The public port for the agent's API (e.g., `8083`).
    - `geohash`: (Optional) Your location's geohash.
    - Configure your desired service(s) under the `[service.*]` sections.

4.  **Run the Agent:**
    Start the agent in detached (background) mode using Docker Compose.
    ```bash
    docker compose up --build -d
    ```
    _(The `--build` flag is only needed the first time or when you change the code)._

The agent is now running. To view live logs, use `docker compose logs -f`. To stop it, use `docker compose down`.

---

### Running from Source (for Development)

1.  **Clone and Set Up Environment:**

    ```bash
    git clone git@github.com:msmrez/sensora_agent.git
    cd sensora_agent
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

2.  **Configure:**
    Copy and edit `config.ini` as described in the Docker instructions.

3.  **Run:**
    ```bash
    python app.py
    ```
    The agent will start, and you can access its dashboard at `http://localhost:YOUR_PORT`.

## API Documentation (Swagger)

Interactive API documentation is available at **`/apidocs`** when the agent is running (e.g. `http://localhost:8083/apidocs`). It lists all endpoints, request/response schemas, and payment flows for single readings and batch purchases.

## API Endpoints

The agent serves an API for direct data monetization.

#### Single Readings

- `GET /`: Web dashboard showing agent status, recent readings, and tokens.
- `GET /price[?reading_id=...]`: Payment info for the latest (or the given) reading with a mined proof, including its `commitment`. Pay with OP_RETURN `SENSORA_PAY:<reading_id>:<commitment>`.
- `POST /claim_reading/<reading_id>`: Claims a single reading after payment.
  - **Body:** `{"payment_txid": "...", "signature": "..."}`
- `GET /fetch_data/<reading_id>?token=...`: Fetches the raw data for a single reading.

#### Batch Purchases

- `POST /batch/price`: Gets a price quote for a historical batch of data.
  - **Body:** `{"start_timestamp": 1678880000, "end_timestamp": 1678886400}`
- `POST /batch/claim`: Claims an entire batch after payment.
  - **Body:** `{"batch_id": "...", "payment_txid": "...", "signature": "..."}`

Both claim endpoints require `signature`: a Bitcoin Signed Message (base64) of `SENSORA_CLAIM:<reading_id or batch_id>:<payment_txid>`, made with a key that signed one of the payment's inputs (P2PKH). The payment txid is public, so without it anyone watching the chain could claim the buyer's token.
- `GET /batch/fetch/<batch_id>?token=...`: Fetches the complete JSON array of readings for the batch.
