#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
from typing import List, Dict, Tuple
from eth_utils import event_abi_to_log_topic

from dotenv import load_dotenv
import requests
from tables import Enum
from web3 import Web3
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

load_dotenv()

SHARE_MANAGERS = {
    "mbhBTC": {
        "address": "0x43f084bdBC99409c637319dD7c544D565165A162",
        "start_block": 24245295,
        "mezo_share_manager": "0xE2232789D4cF5bb1ffaDA1a105Cc59B18d639318"
    },
    "mbhcbBTC": {
        "address": "0x171b8E43bB751A558b2b1f3C814d3c96D36cCf2B",
        "start_block": 24245362,
        "mezo_share_manager":  "0x8FB0EB4BB6CA5cf3883E83734BD5bD77a87CC20E"
    },
    "msvUSD": {
        "address": "0xe4741d6901C77Da80FAEeD7E2fE10c8b348Bcc84",
        "start_block": 23970894,
        "mezo_share_manager": "0xc5834dc9EDe2b1d6aE7e52150e95Ccfd12df0999"
    },
}


RPC_URL = os.environ["RPC_URL"]
ROOT_PATH = Path("data/")
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
BATCH_SIZE = 1000
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

BATCH_BLOCKS = 50000

ITOKENIZED_SHARE_MANAGER_ABI = [
    {
        "anonymous": False,
        "inputs": [
        {
            "indexed": True,
            "internalType": "address",
            "name": "from",
            "type": "address"
        },
        {
            "indexed": True,
            "internalType": "address",
            "name": "to",
            "type": "address"
        },
        {
            "indexed": False,
            "internalType": "uint256",
            "name": "value",
            "type": "uint256"
        }
        ],
        "name": "Transfer",
        "type": "event"
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "sharesOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "totalShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "activeShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allocatedShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


TRANSFER_EVENT_ABI = ITOKENIZED_SHARE_MANAGER_ABI[0]

# Multicall3 aggregate3 ABI
MULTICALL3_ABI = [
    {
        "name": "aggregate3",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]

class AcctType(str, Enum):
    EOA = "EOA"
    EIP7702 = "EIP7702" # still EOA but with code, see https://eips.ethereum.org/EIPS/eip-7702
    CONTRACT = "CONTRACT"


EIP7702_PREFIX = "0xef0100"  # 3 bytes
EIP7702_CODE_LEN_HEX = 2 + 2 * (3 + 20)  # "0x" + 23 bytes as hex chars = 48

def classify_by_code_hex(code_hex: str) -> Tuple[AcctType]:
    """
    code_hex: like '0x' or '0x6080...' or '0xef0100<20-byte-addr>'
    returns: (type, impl_address_if_7702_else_None)
    """
    if not code_hex or code_hex == "0x":
        return AcctType.EOA

    # exact EIP-7702 designator: 0xef0100 + 20 bytes (address)
    if code_hex.startswith(EIP7702_PREFIX) and len(code_hex) == EIP7702_CODE_LEN_HEX:
        return AcctType.EIP7702

    return AcctType.CONTRACT


def holder_code_hint(code_hex: str) -> str:
    acct_type = classify_by_code_hex(code_hex)
    if acct_type == AcctType.EOA:
        return "0x"
    if acct_type == AcctType.EIP7702:
        delegation = "0x" + code_hex[-40:]
        return to_checksum_address(delegation)
    # first 4 bytes of deployed runtime code
    return code_hex[:10] if len(code_hex) >= 10 else code_hex

def collect_unique_accounts(w3, contract_address, symbol, start_block, end_block):

    topic0 = event_abi_to_log_topic(TRANSFER_EVENT_ABI)

    unique_accounts = set()

    current = start_block

    if end_block == "latest":
        end_block = w3.eth.block_number

    while current <= end_block:

        to_block = min(current + BATCH_BLOCKS - 1, end_block)


        logs = w3.eth.get_logs({
            "address": Web3.to_checksum_address(contract_address),
            "fromBlock": current,
            "toBlock": to_block,
            "topics": [topic0]
        })
        print(f"Scanning blocks {current} → {to_block} logs found: {len(logs)}")

        for log in logs:
            account = ("0x" + log["topics"][2].hex()[-40:]).lower()
            if account != ZERO_ADDRESS:
                unique_accounts.add(account)

        current = to_block + 1
    
    accounts = sorted(list(unique_accounts))
    accounts = [to_checksum_address(a) for a in accounts]
    return accounts


def write_holders_file(symbol: str, contract: str, holders: Dict[str, Dict[str, str]]) -> Path:
    holders_path = ROOT_PATH.joinpath(symbol, "holders.json")
    holders_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract": contract,
        "unique_accounts_count": len(holders),
        "holders": holders,
    }
    holders_path.write_text(json.dumps(payload, indent=2))
    return holders_path


def normalize_holders_doc(symbol: str, holders_doc: Dict) -> Tuple[Dict, bool]:
    changed = False
    out = dict(holders_doc)
    proofs_path = ROOT_PATH.joinpath(symbol, "proofs.json")
    proofs_claims: Dict[str, Dict] = {}
    if proofs_path.exists():
        proofs = json.loads(proofs_path.read_text())
        proofs_claims = proofs.get("claims", {})

    if "holders" in holders_doc and isinstance(holders_doc["holders"], dict):
        normalized_holders: Dict[str, Dict[str, str]] = {}
        for holder, entry in holders_doc["holders"].items():
            if isinstance(entry, dict):
                shares = entry.get("shares")
                recipient = entry.get("recipient", holder)
                acct_type = entry.get("type")
                code_hint = entry.get("code")
            elif isinstance(entry, list) and len(entry) >= 2:
                shares = entry[0]
                recipient = entry[1]
                acct_type = None
                code_hint = None
                changed = True
            else:
                continue

            if acct_type is None and holder in proofs_claims:
                proof_claim = proofs_claims[holder]
                acct_type = proof_claim.get("type")

            normalized_entry = {
                "shares": str(shares),
                "recipient": recipient,
            }
            if acct_type is not None:
                normalized_entry["type"] = str(acct_type)
            if code_hint is not None:
                normalized_entry["code"] = str(code_hint)

            if entry != normalized_entry:
                changed = True
            normalized_holders[holder] = normalized_entry

        if out.get("holders") != normalized_holders:
            changed = True

        missing_code_holders = [h for h, e in normalized_holders.items() if "code" not in e]
        if missing_code_holders:
            code_map = batch_get_code(missing_code_holders)
            for holder in missing_code_holders:
                checksum_holder = to_checksum_address(holder)
                normalized_holders[holder]["code"] = holder_code_hint(code_map[checksum_holder])
            changed = True

        out["holders"] = normalized_holders
        out["unique_accounts_count"] = len(normalized_holders)
        return out, changed

    if "accounts" in holders_doc and isinstance(holders_doc["accounts"], list):
        if not proofs_path.exists():
            return out, changed

        normalized_holders: Dict[str, Dict[str, str]] = {}
        for holder in holders_doc["accounts"]:
            claim = proofs_claims.get(holder)
            if not claim:
                continue
            normalized_holders[holder] = {
                "shares": str(claim["amount"]),
                "recipient": claim.get("recipient", holder),
                "type": str(claim["type"]),
            }

        if normalized_holders:
            code_map = batch_get_code(list(normalized_holders.keys()))
            for holder in normalized_holders:
                checksum_holder = to_checksum_address(holder)
                normalized_holders[holder]["code"] = holder_code_hint(code_map[checksum_holder])

        out["contract"] = out.get("contract") or proofs.get("rewardToken")
        out["unique_accounts_count"] = len(normalized_holders)
        out["holders"] = normalized_holders
        out.pop("accounts", None)
        changed = True

    return out, changed


def read_holders_file(symbol: str) -> Dict:
    holders_path = ROOT_PATH.joinpath(symbol, "holders.json")
    if not holders_path.exists():
        raise SystemExit(f"holders file missing: {holders_path}")
    holders_doc = json.loads(holders_path.read_text())
    normalized_doc, changed = normalize_holders_doc(symbol, holders_doc)
    if changed:
        holders_path.write_text(json.dumps(normalized_doc, indent=2) + "\n")
        print(f"normalized holders format: {holders_path}")
    return normalized_doc

def urd_leaf(account: str, reward: str, claimable: int) -> bytes:
    # leaf = keccak256( abi.encodePacked( keccak256( abi.encode(account, reward, claimable) ) ) )
    inner = keccak(abi_encode(["address", "address", "uint256"], [account, reward, claimable]))
    return keccak(inner)  # keccak of 32 bytes = keccak256(abi.encodePacked(inner))


def hash_pair(a: bytes, b: bytes) -> bytes:
    # OZ MerkleProof sorted pair hash
    return keccak(a + b) if a < b else keccak(b + a)


def build_layers(leaves: List[bytes]) -> List[List[bytes]]:
    if not leaves:
        raise ValueError("no leaves")
    layers = [leaves]
    level = leaves
    while len(level) > 1:
        nxt: List[bytes] = []
        i = 0
        while i < len(level):
            if i + 1 == len(level):
                nxt.append(level[i])  # odd carry
            else:
                nxt.append(hash_pair(level[i], level[i + 1]))
            i += 2
        layers.append(nxt)
        level = nxt
    return layers


def proof_for_index(layers: List[List[bytes]], index: int) -> List[bytes]:
    proof: List[bytes] = []
    idx = index
    for level in range(len(layers) - 1):
        layer = layers[level]
        sibling = idx ^ 1
        if sibling < len(layer):
            proof.append(layer[sibling])
        idx >>= 1
    return proof


# ========= Onchain calls =========

def fetch_total_shares(w3: Web3, SHARE_MANAGER: str) -> int:
    mgr = w3.eth.contract(address=SHARE_MANAGER, abi=ITOKENIZED_SHARE_MANAGER_ABI)
    active_shares = mgr.functions.activeShares().call()
    total_shares = mgr.functions.totalShares().call()
    if active_shares != total_shares:
        print(f"WARNING: activeShares {active_shares} != totalShares {total_shares}")
    return active_shares

def get_symbol(w3: Web3, SHARE_MANAGER: str) -> str:
    mgr = w3.eth.contract(address=SHARE_MANAGER, abi=ITOKENIZED_SHARE_MANAGER_ABI)
    return mgr.functions.symbol().call()


def fetch_shares_multicall3(
    w3: Web3,
    MULTICALL3: str,
    SHARE_MANAGER: str,
    accounts: List[str],
    batch_size: int,
) -> List[int]:
    mc = w3.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)
    mgr = w3.eth.contract(address=SHARE_MANAGER, abi=ITOKENIZED_SHARE_MANAGER_ABI)

    out = [0] * len(accounts)

    for base in range(0, len(accounts), batch_size):
        part = accounts[base: base + batch_size]
        calls = []
        for a in part:
            calldata = mgr.functions.sharesOf(a)._encode_transaction_data()
            calls.append((SHARE_MANAGER, False, calldata))

        res = mc.functions.aggregate3(calls).call()

        for i, (ok, ret) in enumerate(res):
            if not ok:
                raise RuntimeError(f"multicall sharesOf failed for {part[i]}")
            # uint256 return is last 32 bytes
            if len(ret) < 32:
                raise RuntimeError(f"bad returnData for {part[i]}: {ret.hex()}")
            out[base + i] = int.from_bytes(ret[-32:], "big")

    return out

def batch_get_code(addrs, block="latest", timeout=60):
    addrs = [Web3.to_checksum_address(a) for a in addrs]

    payload = []
    for i, a in enumerate(addrs):
        payload.append({
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_getCode",
            "params": [a, block],
        })

    r = requests.post(RPC_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    resp = r.json()

    # responses may be out-of-order -> map by id
    by_id = {item["id"]: item for item in resp}

    out = {}
    for i, a in enumerate(addrs):
        item = by_id[i]
        if "error" in item:
            raise RuntimeError(f"RPC error for {a}: {item['error']}")
        out[a] = item["result"]  # hex string, "0x" for EOA
    return out


def generate_holders_for_share_manager(w3: Web3, share_manager: Dict) -> None:
    share_manager_address = to_checksum_address(share_manager["address"])
    symbol = get_symbol(w3, share_manager_address)

    print(f"\n=== fetch holders: {symbol} ({share_manager_address}) ===")

    accounts = collect_unique_accounts(
        w3,
        share_manager_address,
        symbol,
        share_manager["start_block"],
        "latest"
    )
    accounts = [to_checksum_address(a) for a in accounts]

    amounts = fetch_shares_multicall3(w3, MULTICALL3, share_manager_address, accounts, BATCH_SIZE)
    codes = batch_get_code(accounts)

    holders: Dict[str, Dict[str, str]] = {}
    for acct, amt in zip(accounts, amounts):
        if amt == 0:
            continue
        holders[acct] = {
            "shares": str(amt),
            "recipient": acct,
            "type": str(classify_by_code_hex(codes[acct])),
            "code": holder_code_hint(codes[acct]),
        }

    holders_path = write_holders_file(symbol, share_manager_address, holders)
    total_shares = sum(int(v["shares"]) for v in holders.values())
    onchain_shares = fetch_total_shares(w3, share_manager_address)
    if total_shares != onchain_shares:
        print(f"WARNING: sum of shares {total_shares} != onchain totalShares {onchain_shares}")

    print(f"holders with non-zero shares: {len(holders)}")
    print(f"sum(shares): {total_shares}")
    print(f"onchain totalShares(): {onchain_shares}")
    print(f"wrote: {holders_path}")


def build_claims_from_holders(w3: Web3, holders_doc: Dict, symbol: str) -> Dict:
    reward_token = to_checksum_address(holders_doc["contract"])

    if "holders" in holders_doc:
        # Preferred format: holder -> {shares, recipient}
        # Backward compatible with holder -> [shares, recipient].
        items = sorted(holders_doc["holders"].items(), key=lambda kv: kv[0].lower())
        accounts: List[str] = []
        amounts: List[int] = []
        recipients: List[str] = []

        for holder, entry in items:
            account = to_checksum_address(holder)

            if isinstance(entry, dict):
                shares_raw = entry.get("shares")
                recipient_raw = entry.get("recipient", holder)
            elif isinstance(entry, list) and len(entry) >= 2:
                shares_raw = entry[0]
                recipient_raw = entry[1]
            else:
                raise SystemExit(
                    f"invalid holder entry format for {symbol}, holder={holder}: {entry}"
                )

            if shares_raw is None:
                raise SystemExit(
                    f"missing shares for {symbol}, holder={holder}"
                )

            accounts.append(account)
            amounts.append(int(shares_raw))
            recipients.append(to_checksum_address(recipient_raw))
    elif "accounts" in holders_doc:
        # Backward compatibility: old holders file format
        accounts = [to_checksum_address(a) for a in sorted(holders_doc["accounts"], key=str.lower)]
        amounts = fetch_shares_multicall3(w3, MULTICALL3, reward_token, accounts, BATCH_SIZE)
        recipients = accounts
    else:
        raise SystemExit(f"invalid holders format for {symbol}: expected `holders` or `accounts`")

    total_shares = sum(amounts)

    leaves = [urd_leaf(a, SHARE_MANAGERS[symbol]["mezo_share_manager"], amt) for a, amt in zip(accounts, amounts)]
    layers = build_layers(leaves)
    root = layers[-1][0]

    codes = batch_get_code(accounts)

    claims: Dict[str, Dict[str, object]] = {}
    for i, (acct, amt, recipient) in enumerate(zip(accounts, amounts, recipients)):
        if amt == 0:
            continue
        proof = proof_for_index(layers, i)
        claims[acct] = {
            "amount": str(amt),
            "proof": ["0x" + p.hex() for p in proof],
            "type": classify_by_code_hex(codes[acct]),
            "recipient": recipient,
        }

    return {
        "rewardToken": SHARE_MANAGERS[symbol]["mezo_share_manager"],
        "totalShares": str(total_shares),
        "root": "0x" + root.hex(),
        "claims": claims,
    }


def generate_proofs_for_share_manager(w3: Web3, share_manager: Dict) -> None:
    share_manager_address = to_checksum_address(share_manager["address"])
    symbol = get_symbol(w3, share_manager_address)

    print(f"\n=== generate proofs: {symbol} ({share_manager_address}) ===")

    holders_doc = read_holders_file(symbol)
    out = build_claims_from_holders(w3, holders_doc, symbol)

    PROOFS_PATH = ROOT_PATH.joinpath(symbol, "proofs.json")
    PROOFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROOFS_PATH.write_text(json.dumps(out, indent=2))
    print(f"shareManager/rewardToken: {share_manager_address}")
    print(f"root: {out['root']}")
    print(f"wrote: {PROOFS_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build holders and Merkle proofs for share managers")
    parser.add_argument(
        "action",
        choices=["holders", "proofs"],
        help="Action to perform",
    )
    parser.add_argument(
        "manager",
        choices=sorted(SHARE_MANAGERS.keys()) + ["all"],
        help="Share manager symbol key, or `all`",
    )
    return parser.parse_args()

def main() -> None:
    args = parse_args()

    if not RPC_URL:
        raise SystemExit("RPC_URL missing (set it in env or .env)")

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 120}))
    if not w3.is_connected():
        raise SystemExit("web3: not connected")

    managers = SHARE_MANAGERS.values() if args.manager == "all" else [SHARE_MANAGERS[args.manager]]

    for share_manager in managers:
        if args.action == "holders":
            generate_holders_for_share_manager(w3, share_manager)
        if args.action == "proofs":
            generate_proofs_for_share_manager(w3, share_manager)


if __name__ == "__main__":
    main()