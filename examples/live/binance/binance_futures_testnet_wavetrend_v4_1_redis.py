#!/usr/bin/env python3
"""
WaveTrend V4.1 Live Trading - Redis Persistence Configuration

This example shows how to run WaveTrend V4.1 with Redis persistence for:
- Position state across restarts
- Order state across restarts
- Account balances
- Event streams

Redis persistence is CRITICAL for SPOT trading (no position API) and
RECOMMENDED for FUTURES trading (enhanced reliability).

Setup:
1. Start Redis: docker run -d -p 6379:6379 redis
   OR use NautilusTrader's docker-compose: make start-services
2. Set environment variables:
   export BINANCE_FUTURES_TESTNET_API_KEY=your_key
   export BINANCE_FUTURES_TESTNET_API_SECRET=your_secret
3. Run: python binance_futures_testnet_wavetrend_v4_1_redis.py
"""

from decimal import Decimal

from nautilus_trader.adapters.binance import BINANCE
from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance import BinanceDataClientConfig
from nautilus_trader.adapters.binance import BinanceExecClientConfig
from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance import BinanceLiveExecClientFactory
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.common.config import MessageBusConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import WaveTrendMultiTimeframeV4_1
from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import WaveTrendMultiTimeframeV4_1Config
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId


# ============================================================================
# REDIS CONFIGURATION
# ============================================================================

# Redis database configuration
redis_config = DatabaseConfig(
    type="redis",
    host="localhost",  # Change if Redis is on a different host
    port=6379,
    username=None,  # Set if Redis requires authentication
    password=None,  # Set if Redis requires authentication
    timeout=20,
)

# Cache configuration (positions, orders, account state)
cache_config = CacheConfig(
    database=redis_config,
    encoding="msgpack",  # Faster than JSON
    timestamps_as_iso8601=True,
    buffer_interval_ms=100,  # Write to Redis every 100ms
    flush_on_start=False,  # CRITICAL: Don't clear Redis on restart
    persist_account_events=True,  # Track all account changes
)

# Message bus configuration (event streams for analysis/monitoring)
message_bus_config = MessageBusConfig(
    database=redis_config,
    encoding="msgpack",
    timestamps_as_iso8601=True,
    buffer_interval_ms=100,
    autotrim_mins=60,  # Auto-trim streams older than 1 hour
    use_trader_prefix=True,
    use_trader_id=True,
    use_instance_id=False,
    streams_prefix="wavetrend",  # Prefix for stream keys
    stream_per_topic=True,  # Separate Redis stream per event topic
)


# ============================================================================
# TRADING NODE CONFIGURATION
# ============================================================================

config_node = TradingNodeConfig(
    trader_id=TraderId("WAVETREND-TESTNET-001"),
    logging=LoggingConfig(
        log_level="INFO",
        use_pyo3=True,  # Use Rust logging (faster)
    ),
    # ✅ Redis-backed cache for restart persistence
    cache=cache_config,
    # ✅ Redis-backed message bus for event streaming
    message_bus=message_bus_config,
    # Execution engine with reconciliation + snapshots
    exec_engine=LiveExecEngineConfig(
        reconciliation=True,
        reconciliation_lookback_mins=1440,  # 24 hours
        snapshot_orders=True,  # Snapshot open orders to Redis
        snapshot_positions=True,  # Snapshot positions to Redis
        snapshot_positions_interval_secs=5.0,  # Snapshot every 5 seconds
    ),
    # Binance Futures testnet data client
    data_clients={
        BINANCE: BinanceDataClientConfig(
            api_key=None,  # Reads BINANCE_FUTURES_TESTNET_API_KEY from env
            api_secret=None,  # Reads BINANCE_FUTURES_TESTNET_API_SECRET from env
            account_type=BinanceAccountType.USDT_FUTURES,
            testnet=True,  # CRITICAL: Use testnet endpoints
            instrument_provider=InstrumentProviderConfig(load_all=True),
        ),
    },
    # Binance Futures testnet execution client
    exec_clients={
        BINANCE: BinanceExecClientConfig(
            api_key=None,  # Reads from env
            api_secret=None,  # Reads from env
            account_type=BinanceAccountType.USDT_FUTURES,
            testnet=True,  # CRITICAL: Use testnet endpoints
            max_retries=3,
        ),
    },
)


# ============================================================================
# STRATEGY CONFIGURATION
# ============================================================================

# WaveTrend V4.1 with RELAXED defaults (home run model)
strategy_config = WaveTrendMultiTimeframeV4_1Config(
    instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
    trade_size=Decimal("0.001"),  # Small size for testnet
    # Uses new relaxed defaults:
    # - min_aligned_timeframes=2 (2/3 alignment)
    # - atr_multiplier=3.0 (tighter stops)
    # - profit_threshold_pct=2.5 (earlier trailing)
    # - use_atr_min_filter=False (no ATR minimum filter)
)


# ============================================================================
# RUN
# ============================================================================

if __name__ == "__main__":
    # Build trading node
    node = TradingNode(config=config_node)

    # Add strategy
    strategy = WaveTrendMultiTimeframeV4_1(config=strategy_config)
    node.trader.add_strategy(strategy)

    # Register client factories
    node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
    node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
    node.build()

    # Run (Ctrl+C to stop gracefully)
    try:
        node.run()
    finally:
        node.dispose()
