# Sensōra experiment data (Run 1 and Run 2)

Data behind the controlled experiment in the paper *Sensōra: Letting IoT Devices Sell Their Own Verifiable Data on BSV* (Meysam Rezaei, DeBlock 2026). Code: https://github.com/msmrez/sensora_agent. The measured versions are commit `8d91094` (v1, the baseline) and `f063262` (v2).

## Contents
| Path | What it is |
|---|---|
| `run1/baseline_v1.db`, `run1/v2.db` | Run 1 (30–31 Oct 2026, fee 0.01 sat/byte, below the miner minimum; not used for the comparison) |
| `run1/audit/` | Run 1 audits (Table 4) |
| `run2/baseline_v1.db` | Run 2, v1 arm: 252 saved readings, first 24 h |
| `run2/v2_snapshot_24h.db` | Run 2, v2 arm, snapshot taken just after 24 h (used for the 24 h audit) |
| `run2/v2_final.db` | Run 2, v2 arm, final database (4.3 days, 1,226 readings) |
| `run2/buyer_rounds.csv` | One row per scripted buyer round (68 rounds) |
| `run2/audit_original/` | Audits as run during the experiment (1 and 4 Nov 2026) |
| `run2/audit_reaudit_20261108/` | The same audits re-run on 8 Nov 2026 with the updated audit tool (adds per-reading latency and windows payments by claim time); all on-chain counts are identical |
| `tools/` | `audit.py` and `chain.py` (independent on-chain audit), `buyer.py` (scripted buyer), `per_reading_latency_check.py` (offline recomputation of per-reading latency and reading gaps) |

Run 2 started on 31 Oct 2026 at 07:47:30 UTC (unix 1793432850). Cutoffs: 24 h = 1793519250, 48 h = 1793605650, full run = 1793802722.

## Reproducing an audit
The audit looks up every transaction recorded in a database in two public indexers (JungleBus and Bitails); it does not use the agent's own counters. Python 3.10+, no extra packages:

    python tools/audit.py run2/v2_final.db out.csv --since 1793432850 --cutoff 1793605650 --workers 3

The summary (`out.summary.json`) gives mined/seen/missing per kind, latency per transaction and per reading, the agent-versus-chain matrix and the share of readings with a mined proof.

## Anonymisation
- The access tokens issued to the test buyer were replaced with `[removed]<id>` (79 tokens). Nothing else was changed; the audit reads identical input from these files and from the originals.
- No private keys are included. Every text value was scanned for strings shaped like a private key (WIF); none was found.
- Addresses, transactions and payment memos are public on the BSV blockchain in any case.
- Fingerprints of the original (non-anonymised) Run 2 databases, kept by the author:
- `baseline.db`: 5d6e4cdbf2a33931b2c3330065641211cacbf0493fae9dadc86287777b4f6eb7
- `v2_final.db`: 90843e0887fcc181da7ddb31bfd029ddcfbdeed5a58f4db6caba723811b6d663

The sensor values are simulated (uniform random temperature and humidity), and all purchases were made by the author's own buyer script.

## Licence
- Data (databases, CSV, JSON and log files): Creative Commons Attribution 4.0 International (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/. Please cite the paper.
- Code (`tools/`): MIT licence, as for the rest of the repository (see `LICENSE` at the repository root).

Copyright (c) 2026 Meysam Rezaei.
