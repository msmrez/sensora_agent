# Sensōra - A Peer-to-Peer IoT Data Marketplace

Sensōra is a proof-of-concept project demonstrating a trustless, peer-to-peer (P2P) marketplace for Internet of Things (IoT) data, built on the unique capabilities of the **BSV blockchain**, **IPv6**, and direct device-to-consumer micropayments.

The core vision is to empower every IoT sensor to become an autonomous economic agent, able to directly sell its data with verifiable integrity, without relying on centralized intermediary platforms.

**This repository contains the working MVP code for:**
1.  A Python-based **Sensor Agent** that simulates a sensor, anchors data proofs on-chain, serves data for a price, and verifies payments.
2.  A Python-based **Consumer Client** that discovers (manually for now), pays for, and retrieves data from the Sensor Agent.

## The Sensōra Flow

The project demonstrates a complete, end-to-end data transaction cycle:

1.  **On-Chain Advertisement (Discovery):** The Sensor Agent periodically broadcasts an "advertisement" transaction on the BSV blockchain. This transaction contains an `OP_RETURN` payload that announces the sensor's services, making it discoverable by any indexer on the network.
2.  **On-Chain Data Proof (Stamping):** For each new reading, the Sensor Agent hashes the data and broadcasts a "data stamp" transaction. This creates an immutable, timestamped proof of the data's existence and integrity on the BSV ledger.
3.  **Payment:** A Data Consumer discovers the sensor and its price. To purchase a specific reading, the consumer constructs and broadcasts a BSV micropayment transaction directly to the sensor's address. This payment transaction includes a reference to the specific `reading_id` being purchased.
4.  **Secure Data Access:** The consumer then "claims" the data from the sensor by presenting the `txid` of their valid payment. The sensor agent verifies this payment on-chain.
5.  **Token-based Delivery:** Upon successful verification, the sensor issues a short-lived, single-use access token. The consumer uses this token to download the raw sensor data directly from the sensor's IPv6 address.
6.  **Verification:** The consumer can cryptographically verify the integrity of the received data by hashing it and comparing the result to the corresponding data proof previously stamped on the BSV blockchain.

 
<!-- You can create and add a diagram here. I've used a placeholder link. -->

## Key Technologies

*   **BSV Blockchain:** Used for its unparalleled scalability for data transactions, enabling ultra-low-cost micropayments and on-chain data anchoring (proofs and advertisements).
*   **IPv6:** Provides a globally unique, routable address for every sensor, enabling true P2P communication without the need for centralized servers or NAT traversal workarounds.
*   **Python:** The agent and client are built in Python 3, using `bsvlib` for BSV interactions and `Flask` for the device's API server.
*   **SQLite:** Used for persistent storage of sensor readings and access tokens on the device.

## On-Chain Data Protocols

Sensōra defines two distinct `OP_RETURN` protocols for its on-chain interactions.

### 1. Sensor Advertisement Protocol (`SNSRADV1`)

This transaction is broadcast periodically by the sensor to announce its existence and services. This allows indexers (like `bitails.io`) to build a searchable registry of available sensors.

*   **Prefix:** `SNSRADV1` (ASCII)
*   **Example TXID:** [`50e1adefee49aa48cde515211b765ed755ce22a51d17c6cd85f766501cfa10d8`](https://bitails.io/tx/50e1adefee49aa48cde515211b765ed755ce22a51d17c6cd85f766501cfa10d8)
*   **Payload Structure:**

| Field | Size (Bytes) | Data Type | Description | Example Value |
|---|---|---|---|---|
| Prefix | 8 | ASCII | `SNSRADV1` | `534e535241445631` |
| Device ID Type | 1 | Unsigned Byte | `0x01` for Full IPv6 | `01` |
| Device ID | 16 | Bytes | The sensor's IPv6 address | `2a01:4f8:1c1b:83c3::1` |
| Port | 2 | Unsigned Short (BE) | API listener port | `8080` |
| Geohash Length| 1 | Unsigned Byte | Length of the Geohash string | `8` |
| Geohash | (variable) | ASCII | Sensor's location | `u4pruydq` |
| # of Services | 1 | Unsigned Byte | Number of data types offered | `1` |
| Data Type 1 | 2 | Unsigned Short (BE) | Code for the service (`0x0001` = Temp/Humid) | `0001` |
| Price 1 (Sats)| 4 | Unsigned Int (BE) | Price in satoshis for this service | `150` |
| ... | ... | ... | *(Repeat for more services)* | |
| Timestamp | 8 | Unsigned Long Long (BE) | Unix timestamp of the advertisement | (e.g. `1751139792`) |

### 2. Data Stamp Protocol (`SNSR`)

This transaction is broadcast for every new sensor reading to provide an immutable proof of integrity.

*   **Prefix:** `SNSR` (ASCII)
*   **Payload Structure:**

| Field | Size (Bytes) | Data Type | Description |
|---|---|---|---|
| Prefix | 4 | ASCII | `SNSR` |
| Version | 1 | Unsigned Byte | Protocol version (`0x01`) |
| Device ID Type| 1 | Unsigned Byte | `0x01` for Full IPv6 |
| Device ID | 16 | Bytes | The sensor's IPv6 address |
| Reading ID | 8 | Unsigned Long Long (BE) | Unix timestamp of the reading |
| Data Type | 2 | Unsigned Short (BE) | Code for this specific reading's type |
| Data Hash | 32 | Bytes | SHA-256 hash of `timestamp:value1:value2...` |

## Getting Started

### Prerequisites
*   Python 3.10+
*   An IPv6-capable server (e.g., a cloud VM or a local machine with IPv6)
*   A funded BSV wallet to provide WIFs for the sensor agent and consumer client.

### Sensor Agent Setup
1.  Clone this repository.
2.  Set up a Python virtual environment: `python3 -m venv venv && source venv/bin/activate`
3.  Install dependencies: `pip install -r requirements.txt` (You'll need to create this file with `bsvlib`, `requests`, `Flask`).
4.  Edit `sensor_agent.py`:
    *   Set `DEVICE_WIF` to a WIF that you control.
    *   Ensure `DEVICE_IPV6_ADDRESS` matches your server's IPv6.
    *   Adjust `DEVICE_GEOHASH` and other settings as needed.
5.  Fund the `DEVICE_PAYMENT_ADDRESS` (which is derived from the WIF) with a small amount of BSV to cover transaction fees.
6.  Run the agent: `python sensor_agent.py`
7.  Access the web dashboard in your browser at `http://[YOUR_SERVER_IPV6]:8080/`.

### Consumer Client Usage
1.  The `client.py` script demonstrates how to purchase data.
2.  Ensure your consumer wallet has funds.
3.  Run the client via command line:
    ```bash
    python client.py <SENSOR_IPV6> <PORT> <READING_ID> <YOUR_CONSUMER_WIF>
    ```
    *   `<READING_ID>` can be obtained from the sensor agent's web dashboard or its `/price` API endpoint.

## Future Work
This MVP is the foundation for a much larger vision. Future steps include:
-   **Automated Discovery:** Enhancing `bitails.io` (or another indexer) to parse `SNSRADV1` transactions and provide a queryable sensor registry API.
-   **Hardware Integration:** Running the agent on low-cost hardware like a Raspberry Pi with real sensors.
-   **Advanced Encryption:** Offering optional end-to-end encryption for sensitive data using public keys provided by the consumer.
-   **Batching/Merkle Proofs:** Reducing on-chain costs for high-frequency sensors by publishing a Merkle root of many data hashes.
-   **User-Friendly Interfaces:** Building more advanced web and command-line tools for both sensor owners and data consumers.

---
*This project is in active development. We welcome feedback and ideas.*