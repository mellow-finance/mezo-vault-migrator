#!/usr/bin/env python3
"""
Collect Mint events and calculate time-weighted average of cumulative shares.

Mint(address indexed account, uint256 shares)
topic0: 0x0f6798a560793a54c3bcfe86a93cde1e73087d944c0ea20544137d4121396885
"""

import json
import os
import sys
from decimal import Decimal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timezone

from dotenv import load_dotenv
from web3 import Web3

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

RPC_URL = os.environ["RPC_URL"]

FEE_PERFORMANCE = 0.1  # 10% fee on performance
FEE_PROTOCOL = 0.01  # 1% protocol fee on total shares
ONE_YEAR = 3600 * 24 * 365 # seconds in a year, for annualizing fees if needed

SHARE_MANAGERS = {
    "mbhBTC": {
        "address": "0x43f084bdBC99409c637319dD7c544D565165A162",
        "fee_recipient": "0xb7b2ee53731Fc80080ED2906431e08452BC58786",
        "start_block": 24245295,
        "price_delta": 0.139284798705/100, # delta price of base asset since inception
    },
    "mbhcbBTC": {
        "address": "0x171b8E43bB751A558b2b1f3C814d3c96D36cCf2B",
        "fee_recipient": "0xb7b2ee53731Fc80080ED2906431e08452BC58786",
        "start_block": 24245362,
        "price_delta": 0.172389112893/100, # delta price of base asset since inception
    },
}

TOPIC0 = "0x0f6798a560793a54c3bcfe86a93cde1e73087d944c0ea20544137d4121396885"
CHUNK_SIZE = 50000

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    print("Failed to connect to RPC", file=sys.stderr)
    sys.exit(1)


def save_tvl_plot(symbol, series, start_ts, final_ts, twa):
    bar_times = []
    bar_values = []
    bar_widths = []
    seg_prev_ts = start_ts
    seg_prev_value = 0
    for ts, value in series:
        if ts > seg_prev_ts:
            mid = (seg_prev_ts + ts) / 2
            bar_times.append(datetime.fromtimestamp(mid, tz=timezone.utc))
            bar_values.append(seg_prev_value / 1e18)
            bar_widths.append((ts - seg_prev_ts) / 86400)
        seg_prev_ts = ts
        seg_prev_value = value
    if final_ts > seg_prev_ts:
        mid = (seg_prev_ts + final_ts) / 2
        bar_times.append(datetime.fromtimestamp(mid, tz=timezone.utc))
        bar_values.append(seg_prev_value / 1e18)
        bar_widths.append((final_ts - seg_prev_ts) / 86400)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.bar(bar_times, bar_values, width=bar_widths, align="center", edgecolor="none")
    ax.axhline(float(twa) / 1e18, color="red", linewidth=1.5, label=f"TWA = {float(twa) / 1e18:.6f}")
    ax.legend()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("TVL (shares, 18-dec)")
    ax.set_title(f"{symbol} — TVL over time")
    out_path = os.path.join(os.path.dirname(__file__), "data", symbol, "tvl.png")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def process_share_manager(symbol, latest_block):
    cfg = SHARE_MANAGERS[symbol]
    contract = cfg["address"]
    start_block = cfg["start_block"]
    price_delta = cfg["price_delta"]

    print(f"\n{'='*60}")
    print(f"Processing {symbol} (contract {contract}, start block {start_block})")
    print(f"Fetching Mint events from block {start_block} to {latest_block}...")

    # Collect all logs in chunks
    logs = []
    for from_block in range(start_block, latest_block + 1, CHUNK_SIZE):
        to_block = min(from_block + CHUNK_SIZE - 1, latest_block)
        chunk = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": Web3.to_checksum_address(contract),
            "topics": [TOPIC0],
        })
        logs.extend(chunk)
        print(f"  blocks {from_block}-{to_block}: {len(chunk)} events")

    print(f"\nTotal Mint events: {len(logs)}")

    if not logs:
        print("No events found.")
        return

    # Decode logs: topic1 = account (indexed), data = shares (uint256)
    events = []
    for log in logs:
        shares = int(log["data"].hex(), 16)
        block_number = log["blockNumber"]
        events.append((block_number, shares))

    # Sort by block number
    events.sort(key=lambda x: x[0])

    # Fetch block timestamps for unique blocks
    unique_blocks = sorted(set(e[0] for e in events))
    print(f"\nFetching timestamps for {len(unique_blocks)} unique blocks...")
    block_timestamps = {}
    for bn in unique_blocks:
        block_timestamps[bn] = w3.eth.get_block(bn)["timestamp"]

    # Build (timestamp, cumulative_shares) series
    # For each event, sum(shares) = running total of all shares minted so far
    running_total = 0
    series = []  # list of (timestamp, running_total_after_event)
    for block_number, shares in events:
        running_total += shares
        ts = block_timestamps[block_number]
        series.append((ts, running_total))

    # Time-weighted average of sum(shares):
    # TWA = integral(sum(shares) dt) / total_time
    # Use step function: value holds from its timestamp until next event
    # Final value holds until the latest block timestamp
    final_ts = w3.eth.get_block('latest')["timestamp"]
    # Also get timestamp of start block for reference
    start_ts = w3.eth.get_block(start_block)["timestamp"]

    print(f"\nStart block timestamp: {start_ts}")
    print(f"End block timestamp:     {final_ts}")
    print(f"Total duration:          {final_ts - start_ts} seconds")

    # Compute TWA over the range [start_ts, final_ts]
    # Before any event, sum(shares) = 0
    total_time = final_ts - start_ts
    if total_time == 0:
        print("All events in same block, cannot compute TWA.")
        return

    weighted_sum = Decimal(0)
    prev_ts = start_ts
    prev_value = Decimal(0)

    for ts, value in series:
        if ts > prev_ts:
            weighted_sum += prev_value * Decimal(ts - prev_ts)
        prev_ts = ts
        prev_value = Decimal(value)

    # Final segment: from last event to end block
    if final_ts > prev_ts:
        weighted_sum += prev_value * Decimal(final_ts - prev_ts)

    twa = weighted_sum / Decimal(total_time)
    tvl = prev_value
    years = total_time / ONE_YEAR

    save_tvl_plot(symbol, series, start_ts, final_ts, twa)

    #print(f"\nPer-event breakdown:")
    #print(f"  {'Block':>10}  {'Timestamp':>12}  {'Shares':>30}  {'Cumulative':>30}")
    #for i, ((block_number, shares), (ts, cum)) in enumerate(zip(events, series)):
    #    account = "0x" + logs[i]["topics"][1].hex()[-40:]
    #    print(f"  {block_number:>10}  {ts:>12}  {shares:>30}  {cum:>30}  {account}")
    fee_integral = Decimal(0)
    _prev_ts, _prev_val = start_ts, Decimal(0)
    for ts, value in series:
        if ts > _prev_ts:
            fee_integral += _prev_val * Decimal(ts - _prev_ts)
        _prev_ts, _prev_val = ts, Decimal(value)
    if final_ts > _prev_ts:
        fee_integral += _prev_val * Decimal(final_ts - _prev_ts)

    protocol_fee = float(fee_integral) * FEE_PROTOCOL / ONE_YEAR
    performance_fee = float(tvl) * price_delta * FEE_PERFORMANCE
    fee_total = protocol_fee + performance_fee

    # Update holders.json with fee_recipient entry
    holders_path = os.path.join(os.path.dirname(__file__), "data", symbol, "holders.json")
    if os.path.exists(holders_path):
        with open(holders_path) as f:
            holders_data = json.load(f)
        fee_recipient = cfg["fee_recipient"]
        is_new = fee_recipient not in holders_data["holders"]
        holders_data["holders"][fee_recipient] = {
            "shares": str(int(round(fee_total))),
            "recipient": fee_recipient,
            "type": "CONTRACT",
            "code": "0x60a08060",
        }
        if is_new:
            holders_data["unique_accounts_count"] += 1
        with open(holders_path, "w") as f:
            json.dump(holders_data, f, indent=2)
        print(f"\nUpdated {holders_path} — fee_recipient {fee_recipient} {'added' if is_new else 'updated'} with shares={int(round(fee_total))}")

    print(f"\nResults for {symbol}:")
    print(f"  Total shares minted (sum):               {running_total}")
    print(f"  Total shares minted (18-dec):            {running_total / 1e18:.6f}")
    print(f"  Time-weighted avg sum(shares):           {twa:.0f}")
    print(f"  Time-weighted avg sum(shares) 18d:       {float(twa) / 1e18:.6f}")
    print(f"  Period of time in years:                 {years:.6f}")
    print(f"  Total fair performance fees(shares):     {performance_fee:.0f}")
    print(f"  Total fair performance fees(shares 18d): {(performance_fee / 1e18):.6f}")
    print(f"  Total fair protocol fees(shares):        {protocol_fee:.0f}")
    print(f"  Total fair protocol fees(shares 18d):    {(protocol_fee / 1e18):.6f}")


symbols = sys.argv[1:]
if symbols:
    unknown = [s for s in symbols if s not in SHARE_MANAGERS]
    if unknown:
        print(f"Unknown symbol(s): {', '.join(unknown)}. Choose from: {', '.join(SHARE_MANAGERS)}", file=sys.stderr)
        sys.exit(1)
else:
    symbols = list(SHARE_MANAGERS)

latest_block = w3.eth.block_number
print(f"Connected. Latest block: {latest_block}")

for symbol in symbols:
    process_share_manager(symbol, latest_block)
