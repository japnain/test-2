"""Configuration loading from .env and config.yaml."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ScanningConfig(BaseModel):
    poll_interval_sec: float = 1.0
    market_refresh_sec: float = 120.0
    max_markets: int = 5000
    use_websocket: bool = True
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_reconnect_delay_sec: float = 2.0


class ArbitrageConfig(BaseModel):
    min_profit_bps: float = 30.0
    min_profit_usd: float = 0.10
    fee_estimate_pct: float = 0.02
    safety_margin_pct: float = 0.005
    strategies: list[str] = Field(default_factory=lambda: ["multi_outcome", "binary"])


class ExecutionConfig(BaseModel):
    order_type: str = "FOK"
    max_order_usd: float = 100.0
    max_slippage_bps: float = 10.0
    verify_before_execute: bool = True
    parallel_execution: bool = False
    execution_timeout_sec: float = 10.0
    gas_cost_per_leg_usd: float = 0.01
    max_exit_slippage_pct: float = 0.05


class RiskConfig(BaseModel):
    max_position_usd: float = 500.0
    max_open_positions: int = 10
    max_daily_loss_usd: float = 50.0
    cooldown_after_fail_sec: float = 30.0
    kill_switch: bool = False


class LoggingConfig(BaseModel):
    level: str = "INFO"
    trade_log: str = "logs/trades.jsonl"


class Config(BaseModel):
    mode: str = "dry_run"
    chain_id: int = 137
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    scanning: ScanningConfig = Field(default_factory=ScanningConfig)
    arbitrage: ArbitrageConfig = Field(default_factory=ArbitrageConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Secrets (loaded from .env)
    private_key: str = ""
    proxy_url: str | None = None

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_dry_run(self) -> bool:
        return self.mode == "dry_run"


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> Config:
    """Load config from YAML file and .env secrets."""
    # Load .env
    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(env_file)

    # Load YAML
    yaml_data = {}
    yaml_file = Path(config_path)
    if yaml_file.exists():
        with open(yaml_file) as f:
            yaml_data = yaml.safe_load(f) or {}

    # Build config
    config = Config(**yaml_data)

    # Inject secrets from environment
    config.private_key = os.getenv("PRIVATE_KEY", "")
    config.proxy_url = os.getenv("PROXY_URL")

    return config
