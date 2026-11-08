# sensora_agent/src/rawtx.py
#
# Minimal raw-transaction reader. Bitails' /tx/<txid> endpoint no longer returns outputs
# ("partialOutputs": true), so payments are verified from the raw transaction instead.

import base64
import hashlib
import logging
import requests

logger = logging.getLogger(__name__)

BITAILS_RAW_URL = "https://api.bitails.io/download/tx/{txid}/hex"
JUNGLEBUS_TX_URL = "https://junglebus.gorillapool.io/v1/transaction/get/{txid}"


def txid_of(raw_hex: str) -> str:
    return hashlib.sha256(hashlib.sha256(bytes.fromhex(raw_hex)).digest()).digest()[::-1].hex()


def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = b[i]
    if v < 0xFD:
        return v, i + 1
    n = {0xFD: 2, 0xFE: 4, 0xFF: 8}[v]
    return int.from_bytes(b[i + 1:i + 1 + n], "little"), i + 1 + n


def op_return_data(script: bytes) -> bytes | None:
    """First data push after OP_FALSE OP_RETURN (or OP_RETURN); None if not an OP_RETURN script."""
    if script[:2] == b"\x00\x6a":
        s = script[2:]
    elif script[:1] == b"\x6a":
        s = script[1:]
    else:
        return None
    if not s:
        return b""
    op = s[0]
    if op <= 0x4B:
        return s[1:1 + op]
    if op == 0x4C:
        return s[2:2 + s[1]]
    if op == 0x4D:
        return s[3:3 + int.from_bytes(s[1:3], "little")]
    if op == 0x4E:
        return s[5:5 + int.from_bytes(s[1:5], "little")]
    return None


def parse_outputs(raw: bytes) -> list[tuple[int, bytes]]:
    """[(satoshis, locking_script)] for every output."""
    i = 4
    n, i = _varint(raw, i)
    for _ in range(n):
        i += 36
        length, i = _varint(raw, i)
        i += length + 4
    n, i = _varint(raw, i)
    outputs = []
    for _ in range(n):
        sats = int.from_bytes(raw[i:i + 8], "little")
        i += 8
        length, i = _varint(raw, i)
        outputs.append((sats, raw[i:i + length]))
        i += length
    return outputs


_raw_cache: dict[str, bytes] = {}   # a raw tx never changes for its txid (checked by hash), so caching is safe
_RAW_CACHE_MAX = 256


def fetch_raw(txid: str) -> bytes | None:
    """Raw transaction bytes (cached), else from Bitails, falling back to JungleBus. None if neither has it."""
    if txid in _raw_cache:
        return _raw_cache[txid]
    raw = _fetch_raw_uncached(txid)
    if raw is not None:
        if len(_raw_cache) >= _RAW_CACHE_MAX:
            _raw_cache.pop(next(iter(_raw_cache)))
        _raw_cache[txid] = raw
    return raw


def _fetch_raw_uncached(txid: str) -> bytes | None:
    try:
        r = requests.get(BITAILS_RAW_URL.format(txid=txid), timeout=15)
        if r.status_code == 200 and r.text.strip():
            raw = bytes.fromhex(r.text.strip())
            if txid_of(raw.hex()) == txid:
                return raw
    except Exception as e:
        logger.warning(f"[RAWTX] Bitails raw fetch failed for {txid[:12]}: {e}")
    try:
        r = requests.get(JUNGLEBUS_TX_URL.format(txid=txid), timeout=15, headers={"User-Agent": "sensora-agent"})
        if r.status_code == 200:
            raw = base64.b64decode(r.json()["transaction"])
            if txid_of(raw.hex()) == txid:
                return raw
    except Exception as e:
        logger.warning(f"[RAWTX] JungleBus raw fetch failed for {txid[:12]}: {e}")
    return None


def pays_with_memo(raw: bytes, script_hex: str, memo: bytes, exact_sats: int | None = None,
                   min_sats: int | None = None) -> tuple[bool, str]:
    """True if the tx carries exactly `memo` in an OP_RETURN and pays our script the required amount."""
    outputs = parse_outputs(raw)
    if not any(op_return_data(script) == memo for _, script in outputs):
        return False, "OP_RETURN mismatch."
    ours = [sats for sats, script in outputs if script.hex() == script_hex]
    if exact_sats is not None and exact_sats not in ours:
        return False, "Amount mismatch."
    if min_sats is not None and not any(s >= min_sats for s in ours):
        return False, "Amount mismatch."
    return True, "Payment verified."


def _pushes(script: bytes) -> list[bytes]:
    """Every data push in a script (non-push opcodes are skipped)."""
    out, i = [], 0
    while i < len(script):
        op = script[i]
        if 0x01 <= op <= 0x4B:
            n, start = op, i + 1
        elif op == 0x4C:
            n, start = script[i + 1], i + 2
        elif op == 0x4D:
            n, start = int.from_bytes(script[i + 1:i + 3], "little"), i + 3
        elif op == 0x4E:
            n, start = int.from_bytes(script[i + 1:i + 5], "little"), i + 5
        else:
            i += 1
            continue
        out.append(script[start:start + n])
        i = start + n
    return out


def payer_addresses(raw: bytes) -> set[str]:
    """
    P2PKH addresses that signed the inputs of a transaction: the public key is the last push of each
    unlocking script (<sig> <pubkey>). Inputs of any other type contribute nothing.
    """
    from bsvlib import PublicKey
    i = 4
    n, i = _varint(raw, i)
    addresses = set()
    for _ in range(n):
        i += 36
        length, i = _varint(raw, i)
        pushes = _pushes(raw[i:i + length])
        i += length + 4
        if len(pushes) == 2 and len(pushes[1]) in (33, 65):
            try:
                addresses.add(PublicKey(pushes[1].hex()).address())
            except Exception:
                pass
    return addresses
