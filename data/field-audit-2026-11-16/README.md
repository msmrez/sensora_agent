# Sensōra field deployment: on-chain audit (16 Nov 2026)

Data behind the field-deployment results in the paper (Section 3.5, Section 4 and Table 4): how many of the transactions the field agent recorded between 19 Aug 2025 and 30 Jul 2026 are actually on the BSV blockchain.

## Contents
| File | What it is |
|---|---|
| `dbtxids.json` | Transaction IDs taken from the field agent's database: `proofs` = 41,883 × [reading timestamp (unix), proof txid]; `pays` = 1,265 single-reading payment txids; `batchpays` = 171 × [txid, amount in satoshis, number of readings] (6,351 readings in total) |
| `fieldcheck.py` | The audit script. It draws a fixed-seed sample (seed 20261116): 30 proofs per calendar month (360), all 171 batch payments and 200 random single-reading payments. Each txid is then looked up in JungleBus and, if not found there, in Bitails |
| `fieldcheck.csv` | One row per sampled transaction: kind, month, reading timestamp, txid, status (`mined` / `seen_unmined` / `missing`), block height, block time, source indexer |
| `fieldcheck.out` | The summary printed by the script on 16 Nov 2026: mined share per month, the weighted estimate (44.5%), proof latency, payment results |

## Re-running
Python 3.11+ (uses `datetime.UTC`), no extra packages. From this folder:

    python3 fieldcheck.py

The sample is identical on every run (fixed seed). The statuses come from the public indexers at the time of the run, so a later run can differ if an indexer adds or drops transactions. The only change from the script as run on 16 Nov 2026 is that it now reads `dbtxids.json` from its own folder instead of an absolute path.

## Notes
- The field agent's full database is not published. This folder contains only transaction IDs, reading timestamps and payment amounts, all of which are public on the blockchain.
- The weighted estimate weights each month's mined share by that month's number of readings.
- The sensor values in the field deployment were simulated, and all purchases were made by the author's own test clients.

## Licence
Data (JSON, CSV and the summary): CC BY 4.0, https://creativecommons.org/licenses/by/4.0/. Please cite the paper. Script: MIT, as for the rest of the repository.
