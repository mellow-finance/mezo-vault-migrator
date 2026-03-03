#!/usr/bin/env python3
import json
import os
from pathlib import Path
from typing import List, Dict, Tuple
from eth_utils import event_abi_to_log_topic

from dotenv import load_dotenv
from web3 import Web3
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

load_dotenv()

mbhBTC = {
    "address": "0x43f084bdBC99409c637319dD7c544D565165A162",
    "start_block": 24245295
}

mbhcbBTC =  {
    "address": "0x171b8E43bB751A558b2b1f3C814d3c96D36cCf2B",
    "start_block": 24245362
}

RPC_URL = os.environ["RPC_URL"]
ROOT_PATH = Path("data/")
SHARE_MANAGER = mbhcbBTC
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
BATCH_SIZE = 1000

STEP = 50000

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


def collect_unique_accounts(w3, contract_address, symbol, start_block, end_block):

    topic0 = event_abi_to_log_topic(TRANSFER_EVENT_ABI)

    unique_accounts = set()

    current = start_block

    if end_block == "latest":
        end_block = w3.eth.block_number

    while current <= end_block:

        to_block = min(current + STEP - 1, end_block)


        logs = w3.eth.get_logs({
            "address": Web3.to_checksum_address(contract_address),
            "fromBlock": current,
            "toBlock": to_block,
            "topics": [topic0]
        })
        print(f"Scanning blocks {current} → {to_block} logs found: {len(logs)}")

        for log in logs:
            #account = ("0x" + log["topics"][2].hex()[-40:]).lower() #Web3.to_checksum_address("0x" + log["topics"][2].hex()[-40:])
            unique_accounts.add(("0x" + log["topics"][2].hex()[-40:]).lower())

        current = to_block + 1
    
    accounts = sorted(list(unique_accounts))
    accounts = [to_checksum_address(a) for a in accounts]
    result = {
        "contract": SHARE_MANAGER,
        "unique_accounts_count": len(accounts),
        "accounts": accounts
    }

    HOLDERS_PATH = ROOT_PATH.joinpath(symbol, "holders.json")
    HOLDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HOLDERS_PATH, "w") as f:
        json.dump(result, f, indent=2)

    return accounts

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
    return mgr.functions.totalShares().call()

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


def main() -> None:

    if not RPC_URL:
        raise SystemExit("RPC_URL missing (set it in env or .env)")

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 120}))
    if not w3.is_connected():
        raise SystemExit("web3: not connected")
    
    share_manager_address = to_checksum_address(SHARE_MANAGER["address"])
    symbol = get_symbol(w3, share_manager_address)

    accounts = collect_unique_accounts(
        w3,
        share_manager_address,
        symbol,
        SHARE_MANAGER["start_block"],
        "latest"
    )
    accounts = [to_checksum_address(a) for a in accounts]

    total_shares = fetch_total_shares(w3, share_manager_address)
    amounts = fetch_shares_multicall3(w3, MULTICALL3, share_manager_address, accounts, BATCH_SIZE)

    # Same invariant as your script: totalShares == sum(sharesOf)
    s = sum(amounts)
    if s != total_shares:
        raise SystemExit(f"share total mismatch: sum(sharesOf)={s} totalShares()={total_shares}")

    # In your solidity you use SHARE_MANAGER as "rewardToken" in leaf + output
    reward_token = share_manager_address

    leaves = [urd_leaf(a, reward_token, amt) for a, amt in zip(accounts, amounts)]
    layers = build_layers(leaves)
    root = layers[-1][0]

    # proofs
    claims: Dict[str, Dict[str, object]] = {}
    for i, (acct, amt) in enumerate(zip(accounts, amounts)):
        if amt == 0:
            continue
        proof = proof_for_index(layers, i)
        claims[acct] = {
            "amount": str(amt),
            "proof": ["0x" + p.hex() for p in proof],
        }

    out = {
        "rewardToken": reward_token,
        "totalShares": str(total_shares),
        "root": "0x" + root.hex(),
        "claims": claims,
    }
    PROOFS_PATH = ROOT_PATH.joinpath(symbol, "proofs.json")
    PROOFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROOFS_PATH.write_text(json.dumps(out, indent=2))
    print(f"shareManager/rewardToken: {share_manager_address}")
    print(f"root: 0x{root.hex()}")
    print(f"wrote: {PROOFS_PATH}")


if __name__ == "__main__":
    main()