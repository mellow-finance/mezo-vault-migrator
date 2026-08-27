#!/usr/bin/env python3
import argparse
import json
import os
import time
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
        "mezo_share_manager": "0xE2232789D4cF5bb1ffaDA1a105Cc59B18d639318",
        "mezo_start_block": 7042471,
        # mezo -> ethereum return leg
        "mezo_vault": "0x807D4778abA870e4222904f5b528F68B350cE0E0",
        "mezo_price_asset": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",  # native BTC
        "eth_asset": "0x18084fbA666a33d37592fA2633fD49a74DD93a88",  # TBTC 18 decimals
    },
    "mbhcbBTC": {
        "address": "0x171b8E43bB751A558b2b1f3C814d3c96D36cCf2B",
        "start_block": 24245362,
        "mezo_share_manager":  "0x8FB0EB4BB6CA5cf3883E83734BD5bD77a87CC20E",
        "mezo_start_block": 7042471,
        # mezo -> ethereum return leg
        "mezo_vault": "0x06ED1E2167AA7FBf2476c5A2D220Bf702559Dcf8",
        "mezo_price_asset": "0x6a7cd8e1384d49F502b4a4Ce9ac9EB320835c5D7",  # mcbBTC
        "eth_asset": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",  # cbBTC, 8 decimals
    },
    "msvUSD": {
        "address": "0xe4741d6901C77Da80FAEeD7E2fE10c8b348Bcc84",
        "start_block": 23970894,
        "mezo_share_manager": "0xc5834dc9EDe2b1d6aE7e52150e95Ccfd12df0999",
        "mezo_start_block": 7042471
    },
}


RPC_URL = os.environ["RPC_URL"]
ROOT_PATH = Path("data/")
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"  # same address on ethereum and mezo
BATCH_SIZE = 1000
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

BATCH_BLOCKS = 50000

# Mezo side. Only the vaults listed here have a share manager worth scanning on mezo.
MEZO_RPC_URL = os.environ.get("MEZO_RPC", "")
MEZO_CHAIN_ID = 31612
MEZO_BATCH_BLOCKS = 10000  # mezo rpc caps eth_getLogs at 10k blocks per request
MEZO_MANAGERS = ["mbhBTC", "mbhcbBTC"]

# Return leg: mezo holders claim plain assets from a URD on ethereum.
RETURN_MANAGERS = ["mbhBTC", "mbhcbBTC"]
NATIVE_ASSET = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"  # has no decimals(), treated as 18
NATIVE_ASSET_DECIMALS = 18

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAIN_ID = 1
ETHERSCAN_SLEEP = 0.25  # free tier allows 5 requests/second

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

# Vault side of the return leg: shares are priced by the vault oracle's last report.
ISHARE_MODULE_ABI = [
    {
        "name": "oracle",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    }
]

IORACLE_ABI = [
    {
        "name": "getReport",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "priceD18", "type": "uint224"},
                    {"name": "timestamp", "type": "uint32"},
                    {"name": "isSuspicious", "type": "bool"},
                ],
            }
        ],
    }
]

IERC20_METADATA_ABI = [
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]

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

def collect_unique_accounts(w3, contract_address, symbol, start_block, end_block, batch_blocks=BATCH_BLOCKS):

    topic0 = event_abi_to_log_topic(TRANSFER_EVENT_ABI)

    unique_accounts = set()

    current = start_block

    if end_block == "latest":
        end_block = w3.eth.block_number

    while current <= end_block:

        to_block = min(current + batch_blocks - 1, end_block)


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


def write_holders_file(
    symbol: str,
    contract: str,
    holders: Dict[str, Dict[str, str]],
    filename: str = "holders.json",
) -> Path:
    holders_path = ROOT_PATH.joinpath(symbol, filename)
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

def batch_get_code(addrs, block="latest", timeout=60, rpc_url=None, chunk_size=100):
    # chunked: a single batch of a few hundred eth_getCode calls times out on the mezo rpc
    rpc_url = rpc_url or RPC_URL
    addrs = [Web3.to_checksum_address(a) for a in addrs]

    out = {}
    for base in range(0, len(addrs), chunk_size):
        part = addrs[base: base + chunk_size]

        payload = []
        for i, a in enumerate(part):
            payload.append({
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_getCode",
                "params": [a, block],
            })

        r = requests.post(rpc_url, json=payload, timeout=timeout)
        r.raise_for_status()
        resp = r.json()

        # responses may be out-of-order -> map by id
        by_id = {item["id"]: item for item in resp}

        for i, a in enumerate(part):
            item = by_id[i]
            if "error" in item:
                raise RuntimeError(f"RPC error for {a}: {item['error']}")
            out[a] = item["result"]  # hex string, "0x" for EOA
    return out


def batch_get_tx_count(addrs, block="latest", timeout=60, rpc_url=None, chunk_size=100):
    """Nonce per address, i.e. how many transactions it has sent on that chain."""
    rpc_url = rpc_url or RPC_URL
    addrs = [Web3.to_checksum_address(a) for a in addrs]

    out = {}
    for base in range(0, len(addrs), chunk_size):
        part = addrs[base: base + chunk_size]

        payload = []
        for i, a in enumerate(part):
            payload.append({
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_getTransactionCount",
                "params": [a, block],
            })

        r = requests.post(rpc_url, json=payload, timeout=timeout)
        r.raise_for_status()
        resp = r.json()

        by_id = {item["id"]: item for item in resp}

        for i, a in enumerate(part):
            item = by_id[i]
            if "error" in item:
                raise RuntimeError(f"RPC error for {a}: {item['error']}")
            out[a] = int(item["result"], 16)
    return out


def collect_holders(
    w3: Web3,
    manager_address: str,
    symbol: str,
    start_block: int,
    rpc_url: str,
    batch_blocks: int,
) -> Dict[str, Dict[str, str]]:
    """Scan Transfer logs, read sharesOf for every account seen, keep the non-zero ones."""
    accounts = collect_unique_accounts(
        w3,
        manager_address,
        symbol,
        start_block,
        "latest",
        batch_blocks,
    )
    accounts = [to_checksum_address(a) for a in accounts]

    amounts = fetch_shares_multicall3(w3, MULTICALL3, manager_address, accounts, BATCH_SIZE)
    codes = batch_get_code(accounts, rpc_url=rpc_url)

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
    return holders


def generate_holders_for_share_manager(w3: Web3, share_manager: Dict) -> None:
    share_manager_address = to_checksum_address(share_manager["address"])
    symbol = get_symbol(w3, share_manager_address)

    print(f"\n=== fetch holders: {symbol} ({share_manager_address}) ===")

    holders = collect_holders(
        w3,
        share_manager_address,
        symbol,
        share_manager["start_block"],
        RPC_URL,
        BATCH_BLOCKS,
    )

    holders_path = write_holders_file(symbol, share_manager_address, holders)
    total_shares = sum(int(v["shares"]) for v in holders.values())
    onchain_shares = fetch_total_shares(w3, share_manager_address)
    if total_shares != onchain_shares:
        print(f"WARNING: sum of shares {total_shares} != onchain totalShares {onchain_shares}")

    print(f"holders with non-zero shares: {len(holders)}")
    print(f"sum(shares): {total_shares}")
    print(f"onchain totalShares(): {onchain_shares}")
    print(f"wrote: {holders_path}")


# ========= Mezo side =========

def connect_mezo() -> Web3:
    if not MEZO_RPC_URL:
        raise SystemExit("MEZO_RPC missing (set it in env or .env)")

    w3 = Web3(Web3.HTTPProvider(MEZO_RPC_URL, request_kwargs={"timeout": 120}))
    if not w3.is_connected():
        raise SystemExit("web3: not connected to mezo")

    chain_id = w3.eth.chain_id
    if chain_id != MEZO_CHAIN_ID:
        raise SystemExit(f"MEZO_RPC points at chain {chain_id}, expected {MEZO_CHAIN_ID}")
    return w3


def generate_mezo_holders(mezo_w3: Web3, symbol: str, share_manager: Dict) -> None:
    manager_address = to_checksum_address(share_manager["mezo_share_manager"])

    onchain_symbol = get_symbol(mezo_w3, manager_address)
    if onchain_symbol != symbol:
        raise SystemExit(
            f"mezo share manager {manager_address} reports symbol {onchain_symbol}, expected {symbol}"
        )

    print(f"\n=== fetch mezo holders: {symbol} ({manager_address}) ===")

    holders = collect_holders(
        mezo_w3,
        manager_address,
        symbol,
        share_manager["mezo_start_block"],
        MEZO_RPC_URL,
        MEZO_BATCH_BLOCKS,
    )

    holders_path = write_holders_file(symbol, manager_address, holders, filename="mezo_holders.json")

    mgr = mezo_w3.eth.contract(address=manager_address, abi=ITOKENIZED_SHARE_MANAGER_ABI)
    total_shares = sum(int(v["shares"]) for v in holders.values())
    active_shares = mgr.functions.activeShares().call()
    onchain_total = mgr.functions.totalShares().call()

    print(f"mezo holders with non-zero shares: {len(holders)}")
    print(f"sum(shares): {total_shares}")
    print(f"onchain activeShares(): {active_shares}")
    print(f"onchain totalShares(): {onchain_total}")
    # activeShares != totalShares is normal on mezo (allocated/queued shares), so warn only.
    if total_shares != active_shares:
        print(f"WARNING: sum of shares {total_shares} != onchain activeShares {active_shares}")
    print(f"wrote: {holders_path}")


def etherscan_tx_counts(addresses: List[str], timeout: int = 30) -> Dict[str, int]:
    """
    Number of transactions sent (invoked) by each address on ethereum mainnet, i.e. its nonce.
    Note: contracts never sign transactions, so a contract's nonce only counts its CREATEs.
    """
    if not ETHERSCAN_API_KEY:
        raise SystemExit("ETHERSCAN_API_KEY missing (set it in env or .env)")

    out: Dict[str, int] = {}
    for i, address in enumerate(addresses, 1):
        params = {
            "chainid": ETHERSCAN_CHAIN_ID,
            "module": "proxy",
            "action": "eth_getTransactionCount",
            "address": address,
            "tag": "latest",
            "apikey": ETHERSCAN_API_KEY,
        }
        r = requests.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        result = body.get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            raise RuntimeError(f"etherscan eth_getTransactionCount failed for {address}: {body}")
        out[address] = int(result, 16)

        if i % 10 == 0 or i == len(addresses):
            print(f"etherscan tx counts: {i}/{len(addresses)}")
        time.sleep(ETHERSCAN_SLEEP)

    return out


def read_holders_map(symbol: str, filename: str) -> Dict[str, Dict[str, str]]:
    """Plain read of a holders-shaped file — no normalization, no RPC."""
    path = ROOT_PATH.joinpath(symbol, filename)
    if not path.exists():
        raise SystemExit(f"missing {path} — run the matching holders action first")
    doc = json.loads(path.read_text())
    holders = doc.get("holders")
    if not isinstance(holders, dict):
        raise SystemExit(f"invalid holders format in {path}: expected a `holders` object")
    return holders


def diff_mezo_holders(mezo_w3: Web3, symbol: str, share_manager: Dict) -> None:
    eth_holders = read_holders_map(symbol, "holders.json")
    mezo_holders = read_holders_map(symbol, "mezo_holders.json")

    print(f"\n=== mezo-only holders: {symbol} ===")

    eth_by_key = {a.lower(): e for a, e in eth_holders.items()}

    mezo_only: Dict[str, Dict[str, str]] = {}
    common_count = 0
    for account, entry in mezo_holders.items():
        if account.lower() in eth_by_key:
            common_count += 1
            continue
        mezo_only[account] = {
            "shares": entry["shares"],
            "type": entry.get("type"),
            "code": entry.get("code"),
        }

    # did these addresses ever send a transaction on ethereum, even though they never held shares there?
    tx_counts = etherscan_tx_counts(sorted(mezo_only, key=str.lower))
    for account, entry in mezo_only.items():
        entry["eth_tx_count"] = tx_counts[account]
        entry["eth_has_sent_tx"] = tx_counts[account] > 0

    mezo_only_shares = sum(int(v["shares"]) for v in mezo_only.values())
    mezo_total_shares = sum(int(v["shares"]) for v in mezo_holders.values())
    active_on_eth = sum(1 for v in mezo_only.values() if v["eth_has_sent_tx"])

    out = {
        "symbol": symbol,
        "ethereum_share_manager": to_checksum_address(share_manager["address"]),
        "mezo_share_manager": to_checksum_address(share_manager["mezo_share_manager"]),
        "ethereum_holders_count": len(eth_holders),
        "mezo_holders_count": len(mezo_holders),
        "common_count": common_count,
        "mezo_only_count": len(mezo_only),
        "mezo_only_shares": str(mezo_only_shares),
        "mezo_only_with_ethereum_tx_count": active_on_eth,
        "mezo_only": dict(sorted(mezo_only.items(), key=lambda kv: kv[0].lower())),
    }

    out_path = ROOT_PATH.joinpath(symbol, "mezo_only.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")

    pct = (100.0 * mezo_only_shares / mezo_total_shares) if mezo_total_shares else 0.0
    print(f"ethereum holders: {len(eth_holders)}")
    print(f"mezo holders: {len(mezo_holders)}")
    print(f"present on both chains: {common_count}")
    print(f"mezo-only (never held on ethereum): {len(mezo_only)}")
    print(f"mezo-only shares: {mezo_only_shares} ({pct:.4f}% of mezo holder shares)")
    print(f"mezo-only that have sent transactions on ethereum: {active_on_eth}/{len(mezo_only)}")
    print(f"wrote: {out_path}")

    report_untouched_holders(symbol, share_manager, eth_by_key, mezo_holders, mezo_total_shares)


def report_untouched_holders(
    symbol: str,
    share_manager: Dict,
    eth_by_key: Dict[str, Dict[str, str]],
    mezo_holders: Dict[str, Dict[str, str]],
    mezo_total_shares: int,
) -> None:
    """
    Holders that migrated and then sat still: identical share balance on both chains
    (nothing moved on mezo) and zero transactions ever sent from the address on mezo.
    """
    print(f"\n=== untouched migrated holders: {symbol} ===")

    same_shares: Dict[str, Dict[str, str]] = {}
    for account, entry in mezo_holders.items():
        eth_entry = eth_by_key.get(account.lower())
        if eth_entry is None:
            continue
        if int(eth_entry["shares"]) != int(entry["shares"]):
            continue
        same_shares[account] = entry

    mezo_tx_counts = batch_get_tx_count(sorted(same_shares, key=str.lower), rpc_url=MEZO_RPC_URL)

    untouched: Dict[str, Dict[str, str]] = {}
    for account, entry in same_shares.items():
        if mezo_tx_counts[account] != 0:
            continue
        untouched[account] = {
            "shares": entry["shares"],
            "type": entry.get("type"),
            "code": entry.get("code"),
            "mezo_tx_count": 0,
        }

    untouched_shares = sum(int(v["shares"]) for v in untouched.values())

    out = {
        "symbol": symbol,
        "ethereum_share_manager": to_checksum_address(share_manager["address"]),
        "mezo_share_manager": to_checksum_address(share_manager["mezo_share_manager"]),
        "same_shares_count": len(same_shares),
        "same_shares_with_mezo_tx_count": len(same_shares) - len(untouched),
        "untouched_count": len(untouched),
        "untouched_shares": str(untouched_shares),
        "untouched": dict(sorted(untouched.items(), key=lambda kv: kv[0].lower())),
    }

    out_path = ROOT_PATH.joinpath(symbol, "mezo_untouched.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")

    pct = (100.0 * untouched_shares / mezo_total_shares) if mezo_total_shares else 0.0
    print(f"same shares on both chains: {len(same_shares)}")
    print(f"  of those, never sent a tx on mezo: {len(untouched)}")
    print(f"  of those, active on mezo: {len(same_shares) - len(untouched)}")
    print(f"untouched shares: {untouched_shares} ({pct:.4f}% of mezo holder shares)")
    print(f"wrote: {out_path}")


# ========= Mezo -> Ethereum return leg =========
#
# The forward leg (holders/proofs) migrates shares: ethereum holders claim shares on mezo.
# The return leg pays out plain assets instead: assets are transferred straight to a URD on
# ethereum and mezo holders claim ERC20 there, so the merkle leaves carry an asset amount and
# the reward token is the payout ERC20 rather than a share manager.

def get_decimals(w3: Web3, token: str) -> int:
    if to_checksum_address(token) == to_checksum_address(NATIVE_ASSET):
        return NATIVE_ASSET_DECIMALS
    erc20 = w3.eth.contract(address=to_checksum_address(token), abi=IERC20_METADATA_ABI)
    return int(erc20.functions.decimals().call())


def read_mezo_price(mezo_w3: Web3, share_manager: Dict) -> Dict:
    """Last oracle report for the asset that prices this vault's shares on mezo."""
    vault = to_checksum_address(share_manager["mezo_vault"])
    price_asset = to_checksum_address(share_manager["mezo_price_asset"])

    module = mezo_w3.eth.contract(address=vault, abi=ISHARE_MODULE_ABI)
    oracle_address = to_checksum_address(module.functions.oracle().call())
    oracle = mezo_w3.eth.contract(address=oracle_address, abi=IORACLE_ABI)
    price_d18, timestamp, is_suspicious = oracle.functions.getReport(price_asset).call()

    if price_d18 == 0:
        raise SystemExit(f"oracle {oracle_address} has no price for asset {price_asset}")
    if is_suspicious:
        print(f"WARNING: oracle report for {price_asset} is flagged suspicious")

    return {
        "vault": vault,
        "oracle": oracle_address,
        "asset": price_asset,
        "asset_decimals": get_decimals(mezo_w3, price_asset),
        "price_d18": int(price_d18),
        "timestamp": int(timestamp),
        "is_suspicious": bool(is_suspicious),
        "block": mezo_w3.eth.block_number,
    }


def shares_to_assets(shares: int, price_d18: int, asset_decimals: int, reward_decimals: int) -> int:
    """
    Invariant from IOracle: shares = assets * priceD18 / 1e18, so assets = shares * 1e18 / priceD18.
    Every step floors, so rounding can only ever lose fractions in the funder's favour.
    """
    assets = (shares * 10 ** 18) // price_d18  # in asset_decimals
    if reward_decimals >= asset_decimals:
        return assets * 10 ** (reward_decimals - asset_decimals)
    return assets // 10 ** (asset_decimals - reward_decimals)


def build_return_claims(w3: Web3, symbol: str, share_manager: Dict, price: Dict, reward_decimals: int) -> Dict:
    reward_token = to_checksum_address(share_manager["eth_asset"])
    mezo_holders = read_holders_map(symbol, "mezo_holders.json")

    items = sorted(mezo_holders.items(), key=lambda kv: kv[0].lower())

    accounts: List[str] = []
    shares: List[int] = []
    amounts: List[int] = []
    dropped: Dict[str, str] = {}

    for holder, entry in items:
        account = to_checksum_address(holder)
        holder_shares = int(entry["shares"])
        amount = shares_to_assets(holder_shares, price["price_d18"], price["asset_decimals"], reward_decimals)
        if amount == 0:
            # would be an unclaimable leaf: keep it out of the tree entirely
            dropped[account] = str(holder_shares)
            continue
        accounts.append(account)
        shares.append(holder_shares)
        amounts.append(amount)

    if not accounts:
        raise SystemExit(f"no claimable accounts for {symbol} after conversion")

    total_shares = sum(int(v["shares"]) for v in mezo_holders.values())
    total_assets = sum(amounts)
    total_converted = shares_to_assets(total_shares, price["price_d18"], price["asset_decimals"], reward_decimals)
    if total_assets > total_converted:
        raise SystemExit(f"per-holder assets {total_assets} exceed converted total {total_converted}")

    leaves = [urd_leaf(a, reward_token, amt) for a, amt in zip(accounts, amounts)]
    layers = build_layers(leaves)
    root = layers[-1][0]

    codes = batch_get_code(accounts, rpc_url=RPC_URL)

    claims: Dict[str, Dict[str, object]] = {}
    for i, (account, holder_shares, amount) in enumerate(zip(accounts, shares, amounts)):
        claims[account] = {
            "shares": str(holder_shares),
            "amount": str(amount),
            "proof": ["0x" + p.hex() for p in proof_for_index(layers, i)],
            "type": classify_by_code_hex(codes[account]),
            "recipient": account,
        }

    return {
        "direction": "mezo->ethereum",
        "rewardToken": reward_token,
        "rewardTokenDecimals": reward_decimals,
        "mezoShareManager": to_checksum_address(share_manager["mezo_share_manager"]),
        "mezoVault": price["vault"],
        "mezoOracle": price["oracle"],
        "priceAsset": price["asset"],
        "priceD18": str(price["price_d18"]),
        "priceTimestamp": price["timestamp"],
        "priceSuspicious": price["is_suspicious"],
        "priceMezoBlock": price["block"],
        "mezoAssetDecimals": price["asset_decimals"],
        "totalShares": str(total_shares),
        "totalAssets": str(total_assets),
        "roundingRemainder": str(total_converted - total_assets),
        "droppedZeroAmount": dropped,
        "root": "0x" + root.hex(),
        "claims": claims,
    }


def generate_return_proofs(w3: Web3, mezo_w3: Web3, symbol: str, share_manager: Dict) -> None:
    if not share_manager.get("eth_asset"):
        raise SystemExit(
            f"eth_asset not set for {symbol} — fill in the payout ERC20 in SHARE_MANAGERS first"
        )

    reward_token = to_checksum_address(share_manager["eth_asset"])
    print(f"\n=== return proofs (mezo -> ethereum): {symbol} ===")

    price = read_mezo_price(mezo_w3, share_manager)
    reward_decimals = get_decimals(w3, reward_token)

    out = build_return_claims(w3, symbol, share_manager, price, reward_decimals)

    total_from_claims = sum(int(c["amount"]) for c in out["claims"].values())
    if int(out["totalAssets"]) != total_from_claims:
        raise SystemExit(
            f"totalAssets mismatch: top-level={out['totalAssets']} != sum(claims.amount)={total_from_claims}"
        )

    out_path = ROOT_PATH.joinpath(symbol, "mezo_proofs.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")

    print(f"reward token: {reward_token} ({reward_decimals} decimals)")
    print(f"price asset: {price['asset']} ({price['asset_decimals']} decimals)")
    print(f"priceD18: {price['price_d18']} (report ts {price['timestamp']}, mezo block {price['block']})")
    print(f"claims: {len(out['claims'])} (dropped as zero after conversion: {len(out['droppedZeroAmount'])})")
    print(f"totalShares: {out['totalShares']}")
    print(f"totalAssets: {out['totalAssets']} (verified == sum of claims; fund the URD with this)")
    print(f"rounding remainder kept by funder: {out['roundingRemainder']}")
    print(f"root: {out['root']}")
    print(f"wrote: {out_path}")


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

    total_from_claims = sum(int(c["amount"]) for c in out["claims"].values())
    if int(out["totalShares"]) != total_from_claims:
        raise SystemExit(
            f"totalShares mismatch: top-level={out['totalShares']} != sum(claims.amount)={total_from_claims}"
        )

    PROOFS_PATH = ROOT_PATH.joinpath(symbol, "proofs.json")
    PROOFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROOFS_PATH.write_text(json.dumps(out, indent=2))
    print(f"shareManager/rewardToken: {share_manager_address}")
    print(f"root: {out['root']}")
    print(f"totalShares: {out['totalShares']} (verified == sum of claims)")
    print(f"wrote: {PROOFS_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build holders and Merkle proofs for share managers")
    parser.add_argument(
        "action",
        choices=["holders", "proofs", "mezo-holders", "mezo-diff", "return-proofs"],
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

    mezo_action = args.action in ("mezo-holders", "mezo-diff")

    if mezo_action:
        if args.manager == "all":
            symbols = list(MEZO_MANAGERS)
        elif args.manager in MEZO_MANAGERS:
            symbols = [args.manager]
        else:
            raise SystemExit(
                f"{args.action} is limited to {', '.join(MEZO_MANAGERS)} — got {args.manager}"
            )

        mezo_w3 = connect_mezo()
        for symbol in symbols:
            share_manager = SHARE_MANAGERS[symbol]
            if args.action == "mezo-holders":
                generate_mezo_holders(mezo_w3, symbol, share_manager)
            if args.action == "mezo-diff":
                diff_mezo_holders(mezo_w3, symbol, share_manager)
        return

    if args.action == "return-proofs":
        if args.manager == "all":
            symbols = list(RETURN_MANAGERS)
        elif args.manager in RETURN_MANAGERS:
            symbols = [args.manager]
        else:
            raise SystemExit(
                f"{args.action} is limited to {', '.join(RETURN_MANAGERS)} — got {args.manager}"
            )

        if not RPC_URL:
            raise SystemExit("RPC_URL missing (set it in env or .env)")

        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 120}))
        if not w3.is_connected():
            raise SystemExit("web3: not connected")

        mezo_w3 = connect_mezo()
        for symbol in symbols:
            generate_return_proofs(w3, mezo_w3, symbol, SHARE_MANAGERS[symbol])
        return

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