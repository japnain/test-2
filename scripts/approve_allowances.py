"""One-time script to approve token allowances for Polymarket trading.

Run this ONCE before enabling live trading. It approves USDC and Conditional
Tokens to the Polymarket exchange contracts on Polygon.

Usage:
    python scripts/approve_allowances.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ERC20 approve ABI
ERC20_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Polygon addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchange contracts that need approval
EXCHANGE_CONTRACTS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # CTF Exchange
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # NegRisk CTF Exchange
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # NegRisk Exchange
]

MAX_UINT256 = 2**256 - 1


def main():
    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("ERROR: PRIVATE_KEY not set in .env file")
        sys.exit(1)

    try:
        from web3 import Web3
    except ImportError:
        print("ERROR: web3 not installed. Run: pip install web3")
        sys.exit(1)

    # Connect to Polygon
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        print(f"ERROR: Cannot connect to Polygon RPC at {rpc_url}")
        sys.exit(1)

    account = w3.eth.account.from_key(private_key)
    print(f"Wallet: {account.address}")
    print(f"Chain ID: {w3.eth.chain_id}")
    print()

    tokens = [
        ("USDC", USDC_ADDRESS),
        ("Conditional Tokens (ERC1155)", CONDITIONAL_TOKENS),
    ]

    for token_name, token_address in tokens:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_APPROVE_ABI,
        )

        for exchange_address in EXCHANGE_CONTRACTS:
            print(f"Approving {token_name} → {exchange_address[:10]}...")

            try:
                tx = contract.functions.approve(
                    Web3.to_checksum_address(exchange_address),
                    MAX_UINT256,
                ).build_transaction(
                    {
                        "from": account.address,
                        "nonce": w3.eth.get_transaction_count(account.address),
                        "gas": 100000,
                        "gasPrice": w3.eth.gas_price,
                    }
                )

                signed = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

                if receipt["status"] == 1:
                    print(f"  ✓ Approved (tx: {tx_hash.hex()[:16]}...)")
                else:
                    print(f"  ✗ Failed (tx: {tx_hash.hex()[:16]}...)")
            except Exception as e:
                print(f"  ✗ Error: {e}")

    print()
    print("Done! Token allowances are set. You can now enable live trading.")


if __name__ == "__main__":
    main()
