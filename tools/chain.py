"""Minimal, dependency-free helpers to read BSV transactions from public indexers.

Used by the buyer and the audit so that verification does not depend on the agent's own records.
"""
import base64
import binascii
import hashlib
import http.client
import json
import urllib.error
import urllib.request

JUNGLEBUS = "https://junglebus.gorillapool.io/v1/transaction/get/{txid}"
BITAILS_TX = "https://api.bitails.io/tx/{txid}"
BITAILS_RAW = "https://api.bitails.io/download/tx/{txid}/hex"
HEADERS = {"User-Agent": "sensora-tools"}

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _get(url: str, raw: bool = False):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return body.decode().strip() if raw else json.loads(body)


def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = b[i]
    if v < 0xFD:
        return v, i + 1
    n = {0xFD: 2, 0xFE: 4, 0xFF: 8}[v]
    return int.from_bytes(b[i + 1:i + 1 + n], "little"), i + 1 + n


def p2pkh_address(h160: bytes) -> str:
    payload = b"\x00" + h160
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n, s = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + s


def push_data(script: bytes) -> bytes | None:
    """Returns the first data push after OP_FALSE OP_RETURN (or OP_RETURN)."""
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
        n = int.from_bytes(s[1:3], "little")
        return s[3:3 + n]
    if op == 0x4E:
        n = int.from_bytes(s[1:5], "little")
        return s[5:5 + n]
    return None


def parse_tx(raw: bytes) -> dict:
    """Returns inputs [(txid, vout)] and outputs [(sats, address or None, op_return data or None)]."""
    i = 4
    n, i = _varint(raw, i)
    inputs = []
    for _ in range(n):
        inputs.append((raw[i:i + 32][::-1].hex(), int.from_bytes(raw[i + 32:i + 36], "little")))
        i += 36
        length, i = _varint(raw, i)
        i += length + 4
    n, i = _varint(raw, i)
    outputs = []
    for _ in range(n):
        sats = int.from_bytes(raw[i:i + 8], "little")
        i += 8
        length, i = _varint(raw, i)
        script = raw[i:i + length]
        i += length
        if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
            outputs.append((sats, p2pkh_address(script[3:23]), None))
        else:
            outputs.append((sats, None, push_data(script)))
    return {"inputs": inputs, "outputs": outputs}


class IndexerError(Exception):
    """An indexer could not be asked (timeout, 429, 5xx). Never treat this as "transaction not found"."""


def _ask(url: str, raw: bool = False):
    """Returns the response, None for a 404, or raises IndexerError."""
    try:
        return _get(url, raw=raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise IndexerError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        raise IndexerError(f"{type(e).__name__} from {url}") from e


def txid_of(raw: bytes) -> str:
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()


def lookup(txid: str) -> dict:
    """
    Status and contents of a transaction from two independent indexers.
    {"seen": bool, "block_height": int|None, "block_time": int|None, "raw": bytes|None, "source": str|None}
    "seen": False only when both indexers answer 404. Raises IndexerError when that cannot be established.
    """
    out = {"seen": False, "block_height": None, "block_time": None, "raw": None, "source": None}
    errors = []
    try:
        d = _ask(JUNGLEBUS.format(txid=txid))
        if d is not None:
            try:
                raw = base64.b64decode(d["transaction"])
            except (KeyError, TypeError, binascii.Error) as e:
                raise IndexerError(f"malformed JungleBus response for {txid}") from e
            if txid_of(raw) == txid:
                out.update(seen=True, source="junglebus", raw=raw)
                if d.get("block_height"):
                    out.update(block_height=int(d["block_height"]), block_time=int(d.get("block_time") or 0) or None)
                    return out
    except IndexerError as e:
        errors.append(str(e))
    try:
        d = _ask(BITAILS_TX.format(txid=txid))
        if d is not None:
            out.update(seen=True, source=out["source"] or "bitails")
            if d.get("blockheight"):
                out.update(block_height=int(d["blockheight"]), block_time=int(d.get("blocktime") or 0) or None)
            if out["raw"] is None:
                hex_body = _ask(BITAILS_RAW.format(txid=txid), raw=True)
                if hex_body:
                    raw = bytes.fromhex(hex_body)
                    if txid_of(raw) == txid:
                        out["raw"] = raw
    except IndexerError as e:
        errors.append(str(e))
    # A partial answer is not an answer: if an indexer failed and we cannot say "mined" with the raw tx in
    # hand, the caller must treat this as unresolved rather than as "not found" or "unmined".
    if errors and not (out["block_height"] and out["raw"]):
        raise IndexerError("; ".join(errors))
    return out
