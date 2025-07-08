# Sensōra Agent - An Autonomous IoT Data Provider

This repository contains the complete source code for the **Sensōra Agent**, a Python application designed to run on IoT devices (like a Raspberry Pi or any Linux server). It empowers any connected sensor to become an autonomous economic agent on the BSV blockchain.

The agent periodically captures sensor data, anchors a cryptographic proof of that data on-chain, and sells access to the raw data for BSV micropayments. It is a core component of the decentralized **Sensōra IoT Data Marketplace**.

### The Sensōra Ecosystem

The Agent is the data-producing component in a larger P2P network.

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

This agent works in conjunction with the [Sensōra Indexer & Registry](https://github.com/msmrez/sensora_indexer), which discovers and lists active sensors, and can be tested with the [Sensōra Client](https://github.com/msmrez/sensora_client).

## Core Features

- **Autonomous Operation:** Runs as a background service, requiring no manual intervention.
- **On-Chain Data Stamping:** Creates immutable, timestamped proofs of sensor data on the BSV blockchain using the `SENSORA` protocol.
- **On-Chain Service Advertisement:** Periodically broadcasts its services (data types, price, location) on-chain using the `SENSORA-ADV1` protocol.
- **Direct Monetization:** Sells data directly to consumers for BSV micropayments via a built-in API.
- **Secure Data Access:** Implements a token-based access system to ensure only verified payers can retrieve data.
- **Persistent State:** Uses a local SQLite database (`sensora_device.db`) to track all readings, payments, and access tokens, ensuring resilience against restarts.

## On-Chain Data Protocols

The agent **creates** two distinct `OP_RETURN` protocols on the BSV blockchain.

### 1. Advertisement Protocol (`SENSORA-ADV1`)

Broadcast periodically to announce the sensor's services to the network.

- **Prefix:** `SENSORA-ADV1` (ASCII)
- **Payload Structure:**

| Field           | Size (Bytes) | Data Type               | Description                         |
| :-------------- | :----------- | :---------------------- | :---------------------------------- |
| Prefix          | **12**       | ASCII                   | `SENSORA-ADV1`                      |
| Device ID Type  | 1            | Unsigned Byte           | `0x01` for Full IPv6                |
| Device ID       | 16           | Bytes                   | The sensor's IPv6 address           |
| Port            | 2            | Unsigned Short (BE)     | API listener port                   |
| Geohash Length  | 1            | Unsigned Byte           | Length of the Geohash string        |
| Geohash         | (variable)   | ASCII                   | Sensor's location                   |
| # of Services   | 1            | Unsigned Byte           | Number of data types offered        |
| Service 1 Type  | 2            | Unsigned Short (BE)     | Code for the service                |
| Service 1 Price | 4            | Unsigned Int (BE)       | Price in satoshis                   |
| ...             | ...          | ...                     | _(Repeat for more services)_        |
| Timestamp       | 8            | Unsigned Long Long (BE) | Unix timestamp of the advertisement |

### 2. Data Stamp Protocol (`SENSORA`)

Broadcast for every new sensor reading to provide an immutable proof of integrity.

- **Prefix:** `SENSORA` (ASCII)
- **Payload Structure:**

| Field          | Size (Bytes) | Data Type               | Description                   |
| :------------- | :----------- | :---------------------- | :---------------------------- |
| Prefix         | **7**        | ASCII                   | `SENSORA`                     |
| Version        | 1            | Unsigned Byte           | Protocol version (`0x01`)     |
| Device ID Type | 1            | Unsigned Byte           | `0x01` for Full IPv6          |
| Device ID      | 16           | Bytes                   | The sensor's IPv6 address     |
| Reading ID     | 8            | Unsigned Long Long (BE) | Unix timestamp of the reading |
| Data Type      | 2            | Unsigned Short (BE)     | Code for this reading's type  |
| Data Hash      | 32           | Bytes                   | SHA-256 hash of the data      |

## Getting Started

### Prerequisites

- A Linux-based device (e.g., Raspberry Pi, cloud VM) with Python 3.10+.
- A globally routable IPv6 address for the device.
- A funded BSV wallet.

### Installation & Configuration

1.  **Clone the Repository:**

    ```bash
    git clone https://github.com/msmrez/sensora_agent.git
    cd sensora_agent
    ```

2.  **Set up Virtual Environment:**

    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install Dependencies:**

    ```bash
    pip install -r requirements.txt
    # Note: If requirements.txt doesn't exist, create it after installing
    # dependencies manually (pip install Flask requests bsvlib)
    # using the command: pip freeze > requirements.txt
    ```

4.  **Configure the Agent:**
    - Copy the example configuration file: `cp config.ini.example config.ini`
    - **Edit `config.ini`** with your details:
      - `ipv6_address`: The public IPv6 address of your device.
      - `port`: The port for the API server (e.g., `8080`).
      - `wif`: The BSV Wallet Import Format (WIF) private key. **This wallet must be funded** with BSV to pay for transaction fees.
      - `geohash`: An optional geohash string for your sensor's location.

### Running the Agent

With your virtual environment activated, run the main application from the project root directory. The application is typically named `app.py` or `sensor_agent.py`.

```bash
# Example:
python app.py
```

The agent will initialize its database, start its background threads for stamping and advertising, and launch the Flask web server. You can monitor its activity via the console logs and access its dashboard in a web browser at `http://[YOUR_DEVICE_IPV6]:YOUR_PORT/`.

## API Endpoints

The agent serves a simple API for monetization:

- `GET /`: A web dashboard showing sensor status.
- `GET /price`: Provides payment information for the latest data reading.
- `POST /claim_reading/<reading_id>`: Allows a consumer to claim data after making a payment. Requires a JSON body: `{"payment_txid": "..."}`.
- `GET /fetch_data/<reading_id>?token=...`: Serves the raw data if a valid access token is provided.

## Future Work

- **Hardware Integration:** Running the agent on low-cost hardware like a Raspberry Pi with real sensors.
- **Advanced Encryption:** Offering optional end-to-end encryption for sensitive data.
- **Batching/Merkle Proofs:** Reducing on-chain costs for high-frequency sensors.
