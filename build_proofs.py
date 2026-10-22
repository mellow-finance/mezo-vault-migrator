#!/usr/bin/env python3
import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import List, Dict, Tuple
from eth_utils import event_abi_to_log_topic

from dotenv import load_dotenv
import requests
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError
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
        "eth_urd": "0xCf4cBB7994e75B7F8e7F7A52e880860bEaA7eE72",  # DeployReturnURDs.RETURN_URD_MBHBTC
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
        "eth_urd": "0x0746C70Ec99248Ccc1d6e29010e03C9fa5503653",  # DeployReturnURDs.RETURN_URD_MBHCBBTC
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
MEZO_EXPLORER_API = "https://api.explorer.mezo.org/api/v2"

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

def view_fn(name: str, inputs: Tuple[str, ...] = (), outputs: Tuple[str, ...] = ("uint256",)) -> Dict:
    """ABI entry for a view function with plain (non-tuple) argument and return types."""
    return {
        "name": name,
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": f"arg{i}", "type": t} for i, t in enumerate(inputs)],
        "outputs": [{"name": "", "type": t} for t in outputs],
    }


# flexible-vaults (lib/flexible-vaults/src): only the views the mezo snapshot and readiness check need.
IVAULT_ABI = [
    view_fn("shareManager", outputs=("address",)),
    view_fn("oracle", outputs=("address",)),
    view_fn("feeManager", outputs=("address",)),
    view_fn("riskManager", outputs=("address",)),
    view_fn("getAssetCount"),
    view_fn("assetAt", ("uint256",), ("address",)),
    view_fn("getQueueCount", ("address",)),
    view_fn("queueAt", ("address", "uint256"), ("address",)),
    view_fn("isDepositQueue", ("address",), ("bool",)),
    view_fn("isPausedQueue", ("address",), ("bool",)),
    view_fn("subvaults"),
    view_fn("subvaultAt", ("uint256",), ("address",)),
    view_fn("getRoleMembers", ("bytes32",), ("address[]",)),
    view_fn("SET_HOOK_ROLE", outputs=("bytes32",)),
    view_fn("CREATE_QUEUE_ROLE", outputs=("bytes32",)),
    view_fn("SET_QUEUE_STATUS_ROLE", outputs=("bytes32",)),
]

ISHARE_MANAGER_EXTRA_ABI = [
    view_fn("balanceOf", ("address",)),
    view_fn("SET_FLAGS_ROLE", outputs=("bytes32",)),
    {
        "name": "flags",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "hasMintPause", "type": "bool"},
                    {"name": "hasBurnPause", "type": "bool"},
                    {"name": "hasTransferPause", "type": "bool"},
                    {"name": "hasWhitelist", "type": "bool"},
                    {"name": "hasTransferWhitelist", "type": "bool"},
                    {"name": "globalLockup", "type": "uint32"},
                ],
            }
        ],
    },
]

IDEPOSIT_QUEUE_ABI = [
    view_fn("unclaimedRequests"),
]

REDEEM_REQUEST_TUPLE = "(uint256,uint256,bool,uint256)"  # IRedeemQueue.Request {timestamp, shares, isClaimable, assets}

IREDEEM_QUEUE_ABI = [
    view_fn("getState", outputs=("uint256", "uint256", "uint256", "uint256")),
    {
        "name": "requestsOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "offset", "type": "uint256"},
            {"name": "limit", "type": "uint256"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "shares", "type": "uint256"},
                    {"name": "isClaimable", "type": "bool"},
                    {"name": "assets", "type": "uint256"},
                ],
            }
        ],
    },
]

IORACLE_EXTRA_ABI = [
    view_fn("supportedAssets"),
    view_fn("supportedAssetAt", ("uint256",), ("address",)),
    view_fn("SUBMIT_REPORTS_ROLE", outputs=("bytes32",)),
    view_fn("ACCEPT_REPORT_ROLE", outputs=("bytes32",)),
    {
        "name": "securityParams",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "maxAbsoluteDeviation", "type": "uint224"},
                    {"name": "suspiciousAbsoluteDeviation", "type": "uint224"},
                    {"name": "maxRelativeDeviationD18", "type": "uint64"},
                    {"name": "suspiciousRelativeDeviationD18", "type": "uint64"},
                    {"name": "timeout", "type": "uint32"},
                    {"name": "depositInterval", "type": "uint32"},
                    {"name": "redeemInterval", "type": "uint32"},
                ],
            }
        ],
    },
]

IFEE_MANAGER_ABI = [
    view_fn("feeRecipient", outputs=("address",)),
    view_fn("depositFeeD6", outputs=("uint24",)),
    view_fn("redeemFeeD6", outputs=("uint24",)),
    view_fn("performanceFeeD6", outputs=("uint24",)),
    view_fn("protocolFeeD6", outputs=("uint24",)),
    view_fn("timestamps", ("address",)),
    view_fn("minPriceD18", ("address",)),
    view_fn("baseAsset", ("address",), ("address",)),
    view_fn("calculateFee", ("address", "address", "uint256", "uint256")),
]

RISK_STATE_OUTPUT = [
    {
        "name": "",
        "type": "tuple",
        "components": [{"name": "balance", "type": "int256"}, {"name": "limit", "type": "int256"}],
    }
]

IRISK_MANAGER_ABI = [
    {"name": "vaultState", "type": "function", "stateMutability": "view", "inputs": [], "outputs": RISK_STATE_OUTPUT},
    {
        "name": "subvaultState",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "subvault", "type": "address"}],
        "outputs": RISK_STATE_OUTPUT,
    },
    view_fn("pendingAssets", ("address",), ("int256",)),
]

REDEEM_REQUESTED_TOPIC = keccak(text="RedeemRequested(address,uint256,uint256)")
REDEEM_REQUESTS_LIMIT = 1000  # requestsOf page size; one request per (account, block timestamp)
DEFAULT_ADMIN_ROLE = b"\x00" * 32

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

class AcctType:
    # plain strings, written as-is into the JSON files
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
    extra: Dict = None,
) -> Path:
    holders_path = ROOT_PATH.joinpath(symbol, filename)
    holders_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract": contract,
        **(extra or {}),
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


def multicall_raw(w3: Web3, calls: List[Tuple[str, bytes]], batch_size: int = BATCH_SIZE) -> List[bytes]:
    """aggregate3 over (target, calldata) pairs, every call must succeed; returns raw returnData."""
    mc = w3.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)
    out: List[bytes] = []
    for base in range(0, len(calls), batch_size):
        part = calls[base: base + batch_size]
        res = mc.functions.aggregate3([(t, False, data) for t, data in part]).call()
        for i, (ok, ret) in enumerate(res):
            if not ok:
                raise RuntimeError(f"multicall failed for call #{base + i} to {part[i][0]}")
            out.append(ret)
    return out


def multicall_uint256(w3: Web3, calls: List[Tuple[str, bytes]], batch_size: int = BATCH_SIZE) -> List[int]:
    out = []
    for i, ret in enumerate(multicall_raw(w3, calls, batch_size)):
        # uint256 return is last 32 bytes
        if len(ret) < 32:
            raise RuntimeError(f"bad returnData for call #{i} to {calls[i][0]}: {ret.hex()}")
        out.append(int.from_bytes(ret[-32:], "big"))
    return out


def fetch_shares_multicall3(
    w3: Web3,
    MULTICALL3: str,
    SHARE_MANAGER: str,
    accounts: List[str],
    batch_size: int,
) -> List[int]:
    mgr = w3.eth.contract(address=SHARE_MANAGER, abi=ITOKENIZED_SHARE_MANAGER_ABI)
    calls = [(SHARE_MANAGER, mgr.functions.sharesOf(a)._encode_transaction_data()) for a in accounts]
    return multicall_uint256(w3, calls, batch_size)

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


def topic_address(topic) -> str:
    return to_checksum_address(bytes(topic)[-20:])


def share_manager_contract(w3: Web3, address: str):
    return w3.eth.contract(address=address, abi=ITOKENIZED_SHARE_MANAGER_ABI + ISHARE_MANAGER_EXTRA_ABI)


def queue_kind(w3: Web3, queue: str, is_deposit: bool) -> str:
    """deposit/redeem for the request-based queues; sync and signature queues settle instantly and keep no requests."""
    abi, probe, kind = (IDEPOSIT_QUEUE_ABI, "unclaimedRequests", "deposit") if is_deposit else (
        IREDEEM_QUEUE_ABI, "getState", "redeem"
    )
    try:
        getattr(w3.eth.contract(address=queue, abi=abi).functions, probe)().call()
        return kind
    except (ContractLogicError, BadFunctionCallOutput):
        return f"instant-{kind}"


def read_mezo_vault(mezo_w3: Web3, share_manager: Dict) -> Dict:
    """Vault wiring: share manager, oracle, fee/risk managers, every queue and every subvault."""
    vault_address = to_checksum_address(share_manager["mezo_vault"])
    vault = mezo_w3.eth.contract(address=vault_address, abi=IVAULT_ABI).functions

    manager_address = to_checksum_address(vault.shareManager().call())
    if manager_address != to_checksum_address(share_manager["mezo_share_manager"]):
        raise SystemExit(
            f"vault {vault_address} uses share manager {manager_address}, config says {share_manager['mezo_share_manager']}"
        )

    queues = []
    for i in range(vault.getAssetCount().call()):
        asset = to_checksum_address(vault.assetAt(i).call())
        for j in range(vault.getQueueCount(asset).call()):
            queue = to_checksum_address(vault.queueAt(asset, j).call())
            queues.append({
                "address": queue,
                "asset": asset,
                "kind": queue_kind(mezo_w3, queue, vault.isDepositQueue(queue).call()),
                "paused": vault.isPausedQueue(queue).call(),
            })

    return {
        "vault": vault_address,
        "share_manager": manager_address,
        "oracle": to_checksum_address(vault.oracle().call()),
        "fee_manager": to_checksum_address(vault.feeManager().call()),
        "risk_manager": to_checksum_address(vault.riskManager().call()),
        "queues": queues,
        "subvaults": [to_checksum_address(vault.subvaultAt(i).call()) for i in range(vault.subvaults().call())],
    }


def queues_of_kind(vault: Dict, kind: str) -> List[str]:
    return [q["address"] for q in vault["queues"] if q["kind"] == kind]


def get_logs_with_retry(w3: Web3, params: Dict, attempts: int = 5):
    # a full mezo scan is ~550 requests; the public rpc sporadically answers 403/429/5xx
    for attempt in range(1, attempts + 1):
        try:
            return w3.eth.get_logs(params)
        except requests.RequestException as e:
            if attempt == attempts:
                raise
            print(f"get_logs {params['fromBlock']}..{params['toBlock']} failed ({type(e).__name__}), retry {attempt}")
            time.sleep(2 * attempt)


def scan_mezo_accounts(mezo_w3: Web3, vault: Dict, start_block: int):
    """
    One pass over the share manager and redeem queue logs, same request count as a share-manager-only scan:
    - Transfer `to` on the share manager: everyone who ever held active shares;
    - RedeemRequested on redeem queues: accounts whose shares got locked in the share manager.
    """
    manager = vault["share_manager"]
    redeem_queues = set(queues_of_kind(vault, "redeem"))
    transfer_topic = event_abi_to_log_topic(TRANSFER_EVENT_ABI)

    recipients, requesters = set(), set()

    current = start_block
    end_block = mezo_w3.eth.block_number
    while current <= end_block:
        to_block = min(current + MEZO_BATCH_BLOCKS - 1, end_block)

        logs = get_logs_with_retry(mezo_w3, {
            "address": [manager, *sorted(redeem_queues)],
            "fromBlock": current,
            "toBlock": to_block,
            "topics": [["0x" + t.hex() for t in (transfer_topic, REDEEM_REQUESTED_TOPIC)]],
        })
        print(f"Scanning blocks {current} → {to_block} logs found: {len(logs)}")

        for log in logs:
            emitter = to_checksum_address(log["address"])
            topic0 = bytes(log["topics"][0])
            if emitter == manager and topic0 == transfer_topic:
                recipients.add(topic_address(log["topics"][2]))
            elif emitter in redeem_queues and topic0 == REDEEM_REQUESTED_TOPIC:
                requesters.add(topic_address(log["topics"][1]))

        current = to_block + 1

    recipients.discard(to_checksum_address(ZERO_ADDRESS))
    return recipients, requesters


def pending_redeem_shares(mezo_w3: Web3, redeem_queues: List[str], requesters: set) -> Dict[str, int]:
    """Shares each requester has locked in the share manager that no oracle report has priced yet."""
    accounts = sorted(requesters, key=str.lower)
    out: Dict[str, int] = {}
    for queue in redeem_queues:
        q = mezo_w3.eth.contract(address=queue, abi=IREDEEM_QUEUE_ABI)
        calls = [
            (queue, q.functions.requestsOf(a, 0, REDEEM_REQUESTS_LIMIT)._encode_transaction_data()) for a in accounts
        ]
        for account, ret in zip(accounts, multicall_raw(mezo_w3, calls)):
            (requests,) = abi_decode([f"{REDEEM_REQUEST_TUPLE}[]"], ret)
            if len(requests) == REDEEM_REQUESTS_LIMIT:
                raise SystemExit(f"{account} has >= {REDEEM_REQUESTS_LIMIT} requests in {queue}, raise the limit")
            for _timestamp, shares, is_claimable, assets in requests:
                # priced requests carry assets; unpriced ones are still plain locked shares
                if shares and not is_claimable and assets == 0:
                    out[account] = out.get(account, 0) + shares
    return out


def queue_asset_balance(w3: Web3, queue: str, asset: str) -> int:
    if to_checksum_address(asset) == to_checksum_address(NATIVE_ASSET):
        return w3.eth.get_balance(queue)
    erc20 = w3.eth.contract(address=to_checksum_address(asset), abi=ISHARE_MANAGER_EXTRA_ABI)
    return erc20.functions.balanceOf(queue).call()


def generate_mezo_holders(mezo_w3: Web3, symbol: str, share_manager: Dict) -> None:
    """
    Active shares only (balanceOf). Left out, both settled on mezo:
    - claimable deposit shares (allocatedShares): depositors claim them on mezo;
    - shares locked by pending redeems (the share manager's own balance): redeemed on mezo.
    """
    manager_address = to_checksum_address(share_manager["mezo_share_manager"])
    mgr = share_manager_contract(mezo_w3, manager_address)

    onchain_symbol = mgr.functions.symbol().call()
    if onchain_symbol != symbol:
        raise SystemExit(
            f"mezo share manager {manager_address} reports symbol {onchain_symbol}, expected {symbol}"
        )

    print(f"\n=== fetch mezo holders: {symbol} ({manager_address}) ===")

    vault = read_mezo_vault(mezo_w3, share_manager)
    redeem_queues = queues_of_kind(vault, "redeem")

    recipients, requesters = scan_mezo_accounts(mezo_w3, vault, share_manager["mezo_start_block"])

    accounts = sorted(recipients, key=str.lower)
    calls = [(manager_address, mgr.functions.balanceOf(a)._encode_transaction_data()) for a in accounts]
    balances = {a: amt for a, amt in zip(accounts, multicall_uint256(mezo_w3, calls)) if amt}

    # Shares sitting on the vault, a subvault or a queue would have no owner on ethereum.
    protocol = {vault["vault"], *vault["subvaults"], *(q["address"] for q in vault["queues"])}
    stray = {a: amt for a, amt in balances.items() if a in protocol}
    if stray:
        raise SystemExit(f"protocol addresses hold {symbol} shares: {stray}")
    locked = balances.pop(manager_address, 0)

    codes = batch_get_code(list(balances), rpc_url=MEZO_RPC_URL)
    holders: Dict[str, Dict[str, str]] = {}
    for acct, amt in balances.items():
        holders[acct] = {
            "shares": str(amt),
            "recipient": acct,
            "type": str(classify_by_code_hex(codes[acct])),
            "code": holder_code_hint(codes[acct]),
        }

    # Pending redeems: locked in the share manager, not priced yet. Not paid on ethereum, recorded for the team.
    total_pending = 0
    for queue in redeem_queues:
        q = mezo_w3.eth.contract(address=queue, abi=IREDEEM_QUEUE_ABI)
        batch_iterator, batches, total_demand_assets, total_pending_shares = q.functions.getState().call()
        if batch_iterator < batches or total_demand_assets:
            raise SystemExit(
                f"redeem queue {queue} has priced but unfunded batches ({batch_iterator}/{batches}, "
                f"demand {total_demand_assets}): those shares are burned but their assets are still in the vault"
            )
        total_pending += total_pending_shares
    excluded = pending_redeem_shares(mezo_w3, redeem_queues, requesters)
    excluded_total = sum(excluded.values())
    if not excluded_total == total_pending == locked:
        raise SystemExit(
            f"locked shares mismatch: requesters {excluded_total}, totalPendingShares {total_pending}, "
            f"share manager balance {locked}"
        )

    # Completeness: activeShares is the sum of every balance, so the snapshot plus the locked shares must match it.
    active_shares = mgr.functions.activeShares().call()
    total_shares = mgr.functions.totalShares().call()
    allocated = mgr.functions.allocatedShares().call()
    snapshot_shares = sum(balances.values())
    if snapshot_shares + locked != active_shares:
        raise SystemExit(
            f"snapshot {snapshot_shares} + locked {locked} != activeShares {active_shares}: "
            f"holders are missing, or shares moved during the scan (rerun)"
        )

    holders_path = write_holders_file(
        symbol,
        manager_address,
        holders,
        filename="mezo_holders.json",
        extra={
            "totalShares": str(total_shares),
            "activeShares": str(active_shares),
            "snapshotShares": str(snapshot_shares),
            "excludedAllocatedShares": str(allocated),
            "excludedPendingRedeem": {
                "total": str(locked),
                "requesters": {a: str(s) for a, s in sorted(excluded.items(), key=lambda kv: kv[0].lower())},
            },
        },
    )

    print(f"mezo holders with non-zero active shares: {len(holders)}")
    print(f"sum(shares): {snapshot_shares}")
    print(f"onchain activeShares(): {active_shares}  totalShares(): {total_shares}")
    print(f"excluded pending-redeem shares (locked in share manager): {locked} from {len(excluded)} requesters")
    print(f"excluded claimable deposit shares (allocatedShares): {allocated}")
    print(f"invariant ok: activeShares == snapshot + locked")
    print(f"wrote: {holders_path}")


# ========= Mezo readiness check =========

def explorer_token_balances(address: str) -> List[Tuple[str, str, float]]:
    """Non-zero token balances from the mezo explorer."""
    r = requests.get(f"{MEZO_EXPLORER_API}/addresses/{address}/token-balances", timeout=30)
    r.raise_for_status()
    out = []
    for item in r.json():
        token = item["token"]
        value = int(item["value"] or 0)
        if not value:
            continue
        decimals = int(token.get("decimals") or 0)
        out.append((token.get("symbol") or "?", token.get("address") or token.get("address_hash"), value / 10**decimals))
    return out


def print_positions(mezo_w3: Web3, vault: Dict) -> None:
    risk = mezo_w3.eth.contract(address=vault["risk_manager"], abi=IRISK_MANAGER_ABI)
    balance, limit = risk.functions.vaultState().call()
    print(f"  riskManager.vaultState: balance {balance} limit {limit}")

    labelled = [("vault", vault["vault"])]
    for sv in vault["subvaults"]:
        sv_balance, sv_limit = risk.functions.subvaultState(sv).call()
        print(f"  riskManager.subvaultState({sv}): balance {sv_balance} limit {sv_limit}")
        labelled.append(("subvault", sv))
    labelled += [(f"{q['kind']} queue", q["address"]) for q in vault["queues"]]

    for label, address in labelled:
        native = mezo_w3.eth.get_balance(address)
        try:
            tokens = explorer_token_balances(address)
        except (requests.RequestException, ValueError, KeyError) as e:
            tokens = [("explorer unavailable", str(e), 0.0)]
        if not native and not tokens:
            print(f"  {label} {address}: empty")
            continue
        print(f"  {label} {address}: native {native / 10**NATIVE_ASSET_DECIMALS:.8f}")
        for symbol, token, amount in tokens:
            print(f"      {symbol:14} {amount:.8f}  {token}")


def check_snapshot_freshness(mezo_w3: Web3, symbol: str, share_manager: Dict, mgr, oracle) -> List[str]:
    """
    Compares data/<symbol>/mezo_*.json with the latest state. With transfers and queues paused, holder balances can
    only move through a report (fees, redeem burn) or a new redeem lock, and both show up in these totals.
    """
    changes = []
    holders_path = ROOT_PATH.joinpath(symbol, "mezo_holders.json")
    if holders_path.exists():
        doc = json.loads(holders_path.read_text())
        if "activeShares" not in doc:
            return ["mezo_holders.json is in the old format: regenerate it"]
        now = {
            "activeShares": mgr.functions.activeShares().call(),
            "excludedPendingRedeem": mgr.functions.balanceOf(mgr.address).call(),
        }
        then = {"activeShares": int(doc["activeShares"]), "excludedPendingRedeem": int(doc["excludedPendingRedeem"]["total"])}
        changes += [f"{k} {then[k]} -> {now[k]}" for k in now if now[k] != then[k]]

    proofs_path = ROOT_PATH.joinpath(symbol, "mezo_proofs.json")
    if proofs_path.exists() and holders_path.exists():
        proofs = json.loads(proofs_path.read_text())
        # amounts come from the URD balance, not the price: the proofs only have to sit on this snapshot
        if proofs.get("totalShares") != doc.get("snapshotShares"):
            changes.append(f"mezo_proofs.json totalShares {proofs.get('totalShares')} != snapshot {doc.get('snapshotShares')}")
        if proofs.get("amountSource") != "urd_balance":
            print(f"  mezo_proofs.json is a TEST tree ({proofs.get('amountSource')}): rebuild it once the URD is funded")

    print(f"  mezo_holders.json / mezo_proofs.json: {'match the latest state' if not changes else 'STALE'}")
    for change in changes:
        print(f"    {change}")
    return changes


def check_mezo_vault(mezo_w3: Web3, symbol: str, share_manager: Dict) -> Tuple[List[str], List[str]]:
    """Prints the vault state that decides whether a snapshot is final; returns (blockers, warnings)."""
    blockers: List[str] = []
    warnings: List[str] = []

    block = mezo_w3.eth.get_block("latest")
    print(f"\n=== mezo readiness: {symbol} at block {block['number']} ===")
    vault = read_mezo_vault(mezo_w3, share_manager)
    vault_contract = mezo_w3.eth.contract(address=vault["vault"], abi=IVAULT_ABI)
    mgr = share_manager_contract(mezo_w3, vault["share_manager"])
    oracle = mezo_w3.eth.contract(address=vault["oracle"], abi=IORACLE_ABI + IORACLE_EXTRA_ABI)
    fees = mezo_w3.eth.contract(address=vault["fee_manager"], abi=IFEE_MANAGER_ABI)
    risk = mezo_w3.eth.contract(address=vault["risk_manager"], abi=IRISK_MANAGER_ABI)

    print(f"vault {vault['vault']}  share manager {vault['share_manager']}  oracle {vault['oracle']}")

    # --- share manager ---
    flags = mgr.functions.flags().call()
    total_shares = mgr.functions.totalShares().call()
    active_shares = mgr.functions.activeShares().call()
    allocated = mgr.functions.allocatedShares().call()
    locked = mgr.functions.balanceOf(vault["share_manager"]).call()
    print("\n[share manager]")
    print(
        f"  flags: mintPause={flags[0]} burnPause={flags[1]} transferPause={flags[2]} "
        f"whitelist={flags[3]} transferWhitelist={flags[4]} globalLockup={flags[5]}"
    )
    print(f"  totalShares {total_shares}  activeShares {active_shares}  allocatedShares {allocated}")
    print(f"  locked in share manager (pending redeems): {locked}")
    if not flags[2]:
        warnings.append("share transfers are not paused: holder balances can move between each other")

    # --- queues ---
    print("\n[queues]")
    for q in vault["queues"]:
        balance = queue_asset_balance(mezo_w3, q["address"], q["asset"])
        line = f"  {q['kind']:15} {q['address']} asset {q['asset']} paused={q['paused']} asset balance {balance}"
        if not q["paused"]:
            blockers.append(f"{q['kind']} queue {q['address']} is not paused")
        if q["kind"] == "deposit":
            unclaimed = mezo_w3.eth.contract(address=q["address"], abi=IDEPOSIT_QUEUE_ABI).functions.unclaimedRequests().call()
            line += f" unclaimedRequests {unclaimed}"
            if balance:
                blockers.append(f"deposit queue {q['address']} holds {balance} of unprocessed deposits")
        elif q["kind"] == "redeem":
            state = mezo_w3.eth.contract(address=q["address"], abi=IREDEEM_QUEUE_ABI).functions.getState().call()
            batch_iterator, batches, total_demand_assets, total_pending_shares = state
            line += (
                f" batches {batch_iterator}/{batches} demandAssets {total_demand_assets} "
                f"pendingShares {total_pending_shares}"
            )
            if batch_iterator < batches or total_demand_assets:
                blockers.append(f"redeem queue {q['address']} has priced but unfunded batches")
            if total_pending_shares:
                warnings.append(
                    f"redeem queue {q['address']}: {total_pending_shares} shares pending redeem, locked in the "
                    f"share manager; excluded from the payout, the next report burns them"
                )
            if balance:
                warnings.append(
                    f"redeem queue {q['address']} holds {balance} of handled but unclaimed redemptions (claimed on mezo)"
                )
        print(line)
    for asset in sorted({q["asset"] for q in vault["queues"]}):
        pending_assets = risk.functions.pendingAssets(asset).call()
        if pending_assets:
            blockers.append(f"riskManager.pendingAssets({asset}) = {pending_assets}")
    if allocated:
        warnings.append(f"{allocated} claimable deposit shares are excluded from the payout (claimed on mezo)")

    # --- oracle ---
    print("\n[oracle]")
    params = oracle.functions.securityParams().call()
    print(f"  timeout {params[4]}s  depositInterval {params[5]}s  redeemInterval {params[6]}s")
    reports = {}
    for i in range(oracle.functions.supportedAssets().call()):
        asset = to_checksum_address(oracle.functions.supportedAssetAt(i).call())
        price_d18, timestamp, is_suspicious = oracle.functions.getReport(asset).call()
        reports[asset] = (price_d18, timestamp, is_suspicious)
        age_days = (block["timestamp"] - timestamp) / 86400 if timestamp else float("nan")
        print(f"  {asset}: priceD18 {price_d18}  ts {timestamp} ({age_days:.1f} days ago)  suspicious={is_suspicious}")

    # --- fees ---
    print("\n[fees]")
    base_asset = to_checksum_address(fees.functions.baseAsset(vault["vault"]).call())
    recipient = to_checksum_address(fees.functions.feeRecipient().call())
    print(
        f"  recipient {recipient}  deposit {fees.functions.depositFeeD6().call()}  "
        f"redeem {fees.functions.redeemFeeD6().call()}  performance {fees.functions.performanceFeeD6().call()}  "
        f"protocol {fees.functions.protocolFeeD6().call()} (D6)"
    )
    print(f"  base asset {base_asset}  last fee update {fees.functions.timestamps(vault['vault']).call()}")
    if base_asset in reports:
        fee_shares = fees.functions.calculateFee(
            vault["vault"], base_asset, reports[base_asset][0], total_shares - mgr.functions.balanceOf(recipient).call()
        ).call()
        print(f"  a report now at the last price would mint {fee_shares} fee shares to {recipient}")
        if fee_shares:
            warnings.append(f"next base-asset report mints {fee_shares} fee shares: snapshot only after the last report")

    # --- roles that can still move shares ---
    print("\n[roles]")
    roles = {
        "DEFAULT_ADMIN_ROLE": DEFAULT_ADMIN_ROLE,
        "SET_QUEUE_STATUS_ROLE": vault_contract.functions.SET_QUEUE_STATUS_ROLE().call(),
        "CREATE_QUEUE_ROLE": vault_contract.functions.CREATE_QUEUE_ROLE().call(),
        "SET_HOOK_ROLE": vault_contract.functions.SET_HOOK_ROLE().call(),
        "SET_FLAGS_ROLE": mgr.functions.SET_FLAGS_ROLE().call(),
        "SUBMIT_REPORTS_ROLE": oracle.functions.SUBMIT_REPORTS_ROLE().call(),
        "ACCEPT_REPORT_ROLE": oracle.functions.ACCEPT_REPORT_ROLE().call(),
    }
    for name, role in roles.items():
        members = vault_contract.functions.getRoleMembers(role).call()
        print(f"  {name}: {', '.join(members) or '-'}")
        if name == "SUBMIT_REPORTS_ROLE" and members:
            warnings.append(f"reports can still be submitted by {', '.join(members)}: stop them before the final snapshot")

    # --- positions (reference) ---
    print("\n[positions]")
    if base_asset in reports and reports[base_asset][0]:
        decimals = get_decimals(mezo_w3, base_asset)
        implied = total_shares * 10**18 // reports[base_asset][0]
        print(f"  implied NAV: totalShares / price = {implied / 10**decimals:.8f} of {base_asset}")
    print_positions(mezo_w3, vault)

    # --- snapshot freshness ---
    print("\n[snapshot]")
    changes = check_snapshot_freshness(mezo_w3, symbol, share_manager, mgr, oracle)
    if changes:
        blockers.append(f"snapshot is stale: {'; '.join(changes)}")

    print(f"\n[verdict] {symbol}: {'READY' if not blockers else 'NOT READY'}")
    for b in blockers:
        print(f"  BLOCKER: {b}")
    for w in warnings:
        print(f"  WARNING: {w}")
    return blockers, warnings


# ========= Contract holders on ethereum =========
#
# The URD pays a holder's own address on ethereum. For a contract holder that only works if the same people
# can operate that address there, i.e. it is a Safe and the signers it has on mezo today can reach the quorum of:
# - the Safe already deployed at that address on ethereum, or
# - the Safe that replaying its mezo creation (same factory, singleton, initializer, salt nonce) deploys on
#   ethereum at the same address: that one starts with the owners and threshold of the original initializer,
#   whatever the owners became on mezo afterwards.

ISAFE_ABI = [
    view_fn("VERSION", outputs=("string",)),
    view_fn("getOwners", outputs=("address[]",)),
    view_fn("getThreshold"),
]

ISAFE_PROXY_FACTORY_ABI = [
    view_fn("proxyCreationCode", outputs=("bytes",)),
    {
        "name": "createProxyWithNonce",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "singleton", "type": "address"},
            {"name": "initializer", "type": "bytes"},
            {"name": "saltNonce", "type": "uint256"},
        ],
        "outputs": [{"name": "proxy", "type": "address"}],
    },
]

SAFE_PROXY_CREATION_TOPIC = keccak(text="ProxyCreation(address,address)")
CREATE_PROXY_WITH_NONCE_SELECTOR = keccak(text="createProxyWithNonce(address,bytes,uint256)")[:4]
SAFE_SETUP_SELECTOR = keccak(text="setup(address[],uint256,address,bytes,address,address,uint256,address)")[:4]
SAFE_SETUP_TYPES = ["address[]", "uint256", "address", "bytes", "address", "address", "uint256", "address"]

# ethereum_access values that let the holder claim and later move the funds
ETHEREUM_ACCESS_OK = ("safe_deployed", "safe_deployable")


def read_safe(w3: Web3, address: str):
    safe = w3.eth.contract(address=address, abi=ISAFE_ABI).functions
    try:
        return {
            "version": safe.VERSION().call(),
            "owners": [to_checksum_address(o) for o in safe.getOwners().call()],
            "threshold": safe.getThreshold().call(),
        }
    except (ContractLogicError, BadFunctionCallOutput, ValueError):
        return None


def quorum_held(owners: List[str], threshold: int, signers: List[str]) -> bool:
    return len({o.lower() for o in owners} & {s.lower() for s in signers}) >= threshold


def first_code_block(w3: Web3, address: str) -> int:
    """Block in which `address` got its code: binary search over archive state."""
    lo, hi = 0, w3.eth.block_number
    while lo < hi:
        mid = (lo + hi) // 2
        if w3.eth.get_code(address, block_identifier=mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def safe_proxy_address(factory: str, proxy_creation_code: bytes, singleton: str, initializer: bytes, salt_nonce: int) -> str:
    """CREATE2 address of SafeProxyFactory.createProxyWithNonce (1.3.0 and 1.4.1)."""
    salt = keccak(keccak(initializer) + salt_nonce.to_bytes(32, "big"))
    init_code = proxy_creation_code + abi_encode(["address"], [singleton])
    return to_checksum_address(keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + keccak(init_code))[12:])


def safe_creation(mezo_w3: Web3, address: str):
    """The createProxyWithNonce call that deployed the Safe at `address` on mezo, None if it was created otherwise."""
    block = first_code_block(mezo_w3, address)
    logs = mezo_w3.eth.get_logs({"fromBlock": block, "toBlock": block, "topics": ["0x" + SAFE_PROXY_CREATION_TOPIC.hex()]})
    for log in logs:
        topics = [bytes(t) for t in log["topics"]]
        proxy = topics[1][-20:] if len(topics) > 1 else bytes(log["data"])[12:32]  # indexed since 1.4.1
        if to_checksum_address(proxy) != address:
            continue

        factory = to_checksum_address(log["address"])
        proxy_creation_code = mezo_w3.eth.contract(address=factory, abi=ISAFE_PROXY_FACTORY_ABI).functions.proxyCreationCode().call()
        calldata = bytes(mezo_w3.eth.get_transaction(log["transactionHash"])["input"])
        # a direct factory call or one wrapped in a multisend / multicall: the CREATE2 address proves the right one
        start = calldata.find(CREATE_PROXY_WITH_NONCE_SELECTOR)
        while start != -1:
            try:
                singleton, initializer, salt_nonce = abi_decode(["address", "bytes", "uint256"], calldata[start + 4:])
            except DecodingError:
                singleton = None
            if singleton and safe_proxy_address(factory, proxy_creation_code, singleton, initializer, salt_nonce) == address:
                return {
                    "factory": factory,
                    "singleton": to_checksum_address(singleton),
                    "initializer": initializer,
                    "salt_nonce": salt_nonce,
                    "proxy_creation_code": proxy_creation_code,
                    "tx": "0x" + bytes(log["transactionHash"]).hex(),
                }
            start = calldata.find(CREATE_PROXY_WITH_NONCE_SELECTOR, start + 1)
    return None


def replay_safe_creation(eth_w3: Web3, creation: Dict, address: str, state_override: Dict = None):
    """Why the mezo creation cannot be replayed on ethereum to the same address, None if it can."""
    factory = creation["factory"]
    if not eth_w3.eth.get_code(factory):
        return f"factory {factory} is not deployed on ethereum"
    factory_contract = eth_w3.eth.contract(address=factory, abi=ISAFE_PROXY_FACTORY_ABI).functions
    if factory_contract.proxyCreationCode().call() != creation["proxy_creation_code"]:
        return f"factory {factory} has a different proxy creation code on ethereum"
    # the dry run also executes setup(): singleton, fallback handler and setup modules have to exist on ethereum too
    try:
        proxy = factory_contract.createProxyWithNonce(
            creation["singleton"], creation["initializer"], creation["salt_nonce"]
        ).call({"from": ZERO_ADDRESS}, "latest", state_override)
    except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
        return f"createProxyWithNonce reverts on ethereum: {e}"
    if to_checksum_address(proxy) != address:
        return f"createProxyWithNonce on ethereum deploys {proxy}"
    return None


def contract_holder_access(mezo_w3: Web3, eth_w3: Web3, address: str) -> Dict:
    mezo_safe = read_safe(mezo_w3, address)
    if mezo_safe is None:
        return {"status": "not_safe"}
    signers = mezo_safe["owners"]
    out = {"version": mezo_safe["version"], "mezo_owners": signers, "mezo_threshold": mezo_safe["threshold"]}

    if eth_w3.eth.get_code(address):
        eth_safe = read_safe(eth_w3, address)
        if eth_safe is None:
            return {"status": "address_taken", **out}
        out.update(ethereum_owners=eth_safe["owners"], ethereum_threshold=eth_safe["threshold"])
        held = quorum_held(eth_safe["owners"], eth_safe["threshold"], signers)
        return {"status": "safe_deployed" if held else "safe_no_quorum", **out}

    creation = safe_creation(mezo_w3, address)
    if creation is None:
        return {"status": "safe_not_deployable", "reason": "not created by SafeProxyFactory.createProxyWithNonce", **out}
    out.update(factory=creation["factory"], singleton=creation["singleton"], salt_nonce=str(creation["salt_nonce"]),
               creation_tx=creation["tx"])

    if creation["initializer"][:4] != SAFE_SETUP_SELECTOR:
        return {"status": "safe_not_deployable", "reason": "initializer is not Safe.setup", **out}
    initial_owners, initial_threshold = abi_decode(SAFE_SETUP_TYPES, creation["initializer"][4:])[:2]
    initial_owners = [to_checksum_address(o) for o in initial_owners]
    out.update(initial_owners=initial_owners, initial_threshold=initial_threshold)

    reason = replay_safe_creation(eth_w3, creation, address)
    if reason:
        return {"status": "safe_not_deployable", "reason": reason, **out}
    held = quorum_held(initial_owners, initial_threshold, signers)
    return {"status": "safe_deployable" if held else "safe_no_quorum", **out}


def check_contract_holders(mezo_w3: Web3, eth_w3: Web3, symbol: str, cache: Dict) -> None:
    """Adds `ethereum_access` (+ `safe` details) to every CONTRACT holder in mezo_holders.json."""
    path = ROOT_PATH.joinpath(symbol, "mezo_holders.json")
    doc = json.loads(path.read_text())

    print(f"\n=== contract holders on ethereum: {symbol} ===")
    for account, entry in doc["holders"].items():
        if entry.get("type") != AcctType.CONTRACT:
            continue
        if account not in cache:
            cache[account] = contract_holder_access(mezo_w3, eth_w3, account)
        result = dict(cache[account])
        entry["ethereum_access"] = result.pop("status")
        entry["safe"] = result
        mark = "ok" if entry["ethereum_access"] in ETHEREUM_ACCESS_OK else "CHECK"
        print(f"  [{mark}] {account} shares {entry['shares']}: {entry['ethereum_access']} {result.get('reason', '')}")

    path.write_text(json.dumps(doc, indent=2))
    print(f"wrote: {path}")


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
        raise SystemExit(f"oracle report for {price_asset} is flagged suspicious")

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


def allocate_pro_rata(amount: int, shares: Dict[str, int]) -> Dict[str, int]:
    """
    Splits `amount` exactly by `shares`: everyone gets floor(amount * shares / total), then the few units left
    go one each to the largest fractional parts (ties by address), so the allocations add up to `amount`.
    """
    total = sum(shares.values())
    allocation = {a: amount * s // total for a, s in shares.items()}
    leftover = amount - sum(allocation.values())  # < len(shares)
    by_fraction = sorted(shares, key=lambda a: (-(amount * shares[a] % total), a.lower()))
    for account in by_fraction[:leftover]:
        allocation[account] += 1
    return allocation


def read_urd_balance(w3: Web3, share_manager: Dict, reward_token: str):
    """(urd, balance of the payout token) — (None, 0) while the URD is not deployed or not configured."""
    urd = share_manager.get("eth_urd")
    if not urd:
        return None, 0
    urd = to_checksum_address(urd)
    if not w3.eth.get_code(urd):
        raise SystemExit(f"eth_urd {urd} has no code on ethereum")
    token = w3.eth.contract(address=reward_token, abi=ISHARE_MANAGER_EXTRA_ABI)
    return urd, token.functions.balanceOf(urd).call()


def build_return_claims(symbol: str, share_manager: Dict, amount: int, reward_decimals: int) -> Dict:
    reward_token = to_checksum_address(share_manager["eth_asset"])
    mezo_holders = read_holders_map(symbol, "mezo_holders.json")
    shares = {to_checksum_address(a): int(e["shares"]) for a, e in mezo_holders.items()}

    allocation = allocate_pro_rata(amount, shares)
    accounts = sorted((a for a, amt in allocation.items() if amt > 0), key=str.lower)
    # a zero amount would be an unclaimable leaf: keep it out of the tree entirely
    dropped = {a: str(shares[a]) for a in sorted(allocation, key=str.lower) if allocation[a] == 0}
    if not accounts:
        raise SystemExit(f"no claimable accounts for {symbol}")

    leaves = [urd_leaf(a, reward_token, allocation[a]) for a in accounts]
    layers = build_layers(leaves)

    codes = batch_get_code(accounts, rpc_url=RPC_URL)

    # the URD pays the same address on ethereum: a mezo contract with no code there may have no owner
    no_eth_code = sorted(
        (a for a in accounts if mezo_holders[a]["type"] == AcctType.CONTRACT and codes[a] == "0x"),
        key=str.lower,
    )

    claims: Dict[str, Dict[str, object]] = {}
    for i, account in enumerate(accounts):
        claims[account] = {
            "shares": str(shares[account]),
            "amount": str(allocation[account]),
            "proof": ["0x" + p.hex() for p in proof_for_index(layers, i)],
            "type": classify_by_code_hex(codes[account]),
            "recipient": account,
        }
        if mezo_holders[account]["type"] == AcctType.CONTRACT:
            # set by `mezo-contracts`; missing means the check has not been run on this snapshot
            claims[account]["ethereum_access"] = mezo_holders[account].get("ethereum_access", "unchecked")

    return {
        "direction": "mezo->ethereum",
        "leaf": "keccak256(bytes.concat(keccak256(abi.encode(account, rewardToken, claimable))))",
        "rewardToken": reward_token,
        "rewardTokenDecimals": reward_decimals,
        "mezoShareManager": to_checksum_address(share_manager["mezo_share_manager"]),
        "totalShares": str(sum(shares.values())),
        "totalAssets": str(amount),
        "droppedZeroAmount": dropped,
        "mezoContractsWithoutEthCode": no_eth_code,
        "root": "0x" + layers[-1][0].hex(),
        "claims": claims,
    }


def verify_return_proofs(out: Dict, holders: Dict[str, Dict[str, str]]) -> None:
    """Re-derives everything in `out` from the holders snapshot alone: tree, amounts, totals."""
    root = bytes.fromhex(out["root"][2:])
    token = out["rewardToken"]
    amount = int(out["totalAssets"])
    shares = {to_checksum_address(a): int(e["shares"]) for a, e in holders.items()}
    total_shares = sum(shares.values())
    errors = []

    if int(out["totalShares"]) != total_shares:
        errors.append(f"totalShares {out['totalShares']} != snapshot {total_shares}")
    if set(out["claims"]) | set(out["droppedZeroAmount"]) != set(shares) or set(out["claims"]) & set(out["droppedZeroAmount"]):
        errors.append("claims + dropped do not partition the snapshot holders")
    if sum(int(c["amount"]) for c in out["claims"].values()) != amount:
        errors.append("sum of claims != totalAssets")

    for account, claim in out["claims"].items():
        claimable = int(claim["amount"])
        exact_floor = amount * shares[account] // total_shares
        if claimable <= 0 or not exact_floor <= claimable <= exact_floor + 1:
            errors.append(f"{account}: amount {claimable} is not its pro-rata share {exact_floor}(+1)")
        node = urd_leaf(account, token, claimable)
        for sibling in claim["proof"]:
            node = hash_pair(node, bytes.fromhex(sibling[2:]))
        if node != root:
            errors.append(f"{account}: proof does not reach the root")
    for account in out["droppedZeroAmount"]:
        if amount * shares[account] // total_shares != 0:
            errors.append(f"{account}: dropped with a non-zero pro-rata share")

    if errors:
        raise SystemExit("proof verification failed:\n  " + "\n  ".join(errors[:20]))
    print(f"verified: {len(out['claims'])} proofs reach the root, amounts are the exact pro-rata split of {amount}")


def generate_return_proofs(w3: Web3, mezo_w3: Web3, symbol: str, share_manager: Dict, test_amount: str = None) -> None:
    """
    Pays out what the URD actually holds, split by the snapshot shares. Until the URD is funded there is
    nothing to split: the tree is then built for a test amount only (--test-amount in token units, else the
    oracle estimate) and marked `amountSource: test_amount`, which ReturnDeploy refuses to set or claim.
    """
    if not share_manager.get("eth_asset"):
        raise SystemExit(
            f"eth_asset not set for {symbol} — fill in the payout ERC20 in SHARE_MANAGERS first"
        )

    reward_token = to_checksum_address(share_manager["eth_asset"])
    reward_decimals = get_decimals(w3, reward_token)
    print(f"\n=== return proofs (mezo -> ethereum): {symbol} ===")

    # informational: what the snapshot is worth at the vault's own price, next to what the URD actually holds
    price = read_mezo_price(mezo_w3, share_manager)
    holders = read_holders_map(symbol, "mezo_holders.json")
    total_shares = sum(int(e["shares"]) for e in holders.values())
    oracle_estimate = shares_to_assets(total_shares, price["price_d18"], price["asset_decimals"], reward_decimals)

    urd, balance = read_urd_balance(w3, share_manager, reward_token)
    if balance > 0:
        amount, source = balance, "urd_balance"
    else:
        if test_amount is not None:
            amount = int(Decimal(test_amount) * 10**reward_decimals)
        else:
            amount = oracle_estimate
        source = "test_amount"
        print(f"URD {urd or '(not set)'} holds no {reward_token}: skipping the real tree, building a TEST tree")

    out = build_return_claims(symbol, share_manager, amount, reward_decimals)
    out = {
        "amountSource": source,
        "urd": urd,
        **out,
        "oracleEstimate": str(oracle_estimate),
        "mezoVault": price["vault"],
        "mezoOracle": price["oracle"],
        "priceAsset": price["asset"],
        "priceD18": str(price["price_d18"]),
        "priceTimestamp": price["timestamp"],
        "priceMezoBlock": price["block"],
    }
    verify_return_proofs(out, holders)

    out_path = ROOT_PATH.joinpath(symbol, "mezo_proofs.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")

    print(f"reward token: {reward_token} ({reward_decimals} decimals)")
    print(f"amount source: {source}" + (f" (URD {urd})" if urd and source == "urd_balance" else ""))
    print(f"distributed: {amount}  oracle estimate: {oracle_estimate} (priceD18 {price['price_d18']})")
    print(f"claims: {len(out['claims'])} (dropped as zero: {len(out['droppedZeroAmount'])})")
    for account in out["mezoContractsWithoutEthCode"]:
        print(f"WARNING: {account} is a contract on mezo with no code on ethereum, check who controls it there")
    for account, claim in out["claims"].items():
        access = claim.get("ethereum_access")
        if access is not None and access not in ETHEREUM_ACCESS_OK:
            print(f"WARNING: contract holder {account} ethereum_access={access} (run mezo-contracts / check manually)")
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
        choices=["holders", "proofs", "mezo-check", "mezo-holders", "mezo-contracts", "mezo-diff", "return-proofs"],
        help="Action to perform",
    )
    parser.add_argument(
        "manager",
        choices=sorted(SHARE_MANAGERS.keys()) + ["all"],
        help="Share manager symbol key, or `all`",
    )
    parser.add_argument(
        "--test-amount",
        default=None,
        help="return-proofs: payout (in token units) for the test tree built while the URD holds nothing; "
        "defaults to the oracle estimate",
    )
    return parser.parse_args()

def main() -> None:
    args = parse_args()

    mezo_action = args.action in ("mezo-check", "mezo-holders", "mezo-contracts", "mezo-diff")

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
        if args.action == "mezo-contracts":
            if not RPC_URL:
                raise SystemExit("RPC_URL missing (set it in env or .env)")
            eth_w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 120}))
            safes: Dict[str, Dict] = {}  # the same Safe can hold both vaults
        not_ready = []
        for symbol in symbols:
            share_manager = SHARE_MANAGERS[symbol]
            if args.action == "mezo-check":
                blockers, _ = check_mezo_vault(mezo_w3, symbol, share_manager)
                if blockers:
                    not_ready.append(symbol)
            if args.action == "mezo-holders":
                generate_mezo_holders(mezo_w3, symbol, share_manager)
            if args.action == "mezo-contracts":
                check_contract_holders(mezo_w3, eth_w3, symbol, safes)
            if args.action == "mezo-diff":
                diff_mezo_holders(mezo_w3, symbol, share_manager)
        if not_ready:
            raise SystemExit(f"\nNOT READY: {', '.join(not_ready)}")
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
            generate_return_proofs(w3, mezo_w3, symbol, SHARE_MANAGERS[symbol], args.test_amount)
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