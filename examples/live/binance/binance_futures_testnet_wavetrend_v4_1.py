#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

"""
WaveTrend V4.1 Strategy - Binance Futures Testnet (Paper Trading).

CRITICAL: This is for PAPER TRADING only (testnet with fake money).
DO NOT use in production without completing full verification protocol.

Requirements:
- Binance Futures Testnet account (https://testnet.binancefuture.com/)
- API keys in .env file:
    BINANCE_FUTURES_TESTNET_API_KEY=your_key
    BINANCE_FUTURES_TESTNET_API_SECRET=your_secret

Monitoring:
- Run for minimum 24 hours
- Monitor logs for CRITICAL errors
- Verify stop orders in testnet UI
- Test restart/reconnection scenario
"""

from decimal import Decimal

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.config import BinanceExecClientConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance.factories import BinanceLiveExecClientFactory
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId

from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import (
    WaveTrendMultiTimeframeV4_1,
)
from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import (
    WaveTrendMultiTimeframeV4_1Config,
)


def run_testnet():
    """
    Run WaveTrend V4.1 on Binance Futures Testnet for paper trading verification.

    This script is for TESTING ONLY with fake money on testnet.
    """
    # Configure trading node
    config_node = TradingNodeConfig(
        trader_id=TraderId("TESTER-V4_1"),
        # Logging - VERBOSE for testing
        logging=LoggingConfig(
            log_level="INFO",  # Change to DEBUG for detailed logs
            log_colors=True,
        ),
        # Data client
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                api_key=None,  # Reads from env: BINANCE_FUTURES_TESTNET_API_KEY
                api_secret=None,  # Reads from env: BINANCE_FUTURES_TESTNET_API_SECRET
                account_type=BinanceAccountType.USDT_FUTURES,
                testnet=True,  # CRITICAL - enables testnet endpoints
                instrument_provider=InstrumentProviderConfig(load_all=True),
            ),
        },
        # Execution client
        exec_clients={
            "BINANCE": BinanceExecClientConfig(
                api_key=None,  # Reads from env
                api_secret=None,  # Reads from env
                account_type=BinanceAccountType.USDT_FUTURES,
                testnet=True,  # CRITICAL - enables testnet endpoints
                max_retries=3,
            ),
        },
        # Execution engine with reconciliation
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,  # Check for existing orders/positions on startup
            reconciliation_lookback_mins=1440,  # 24 hours
        ),
    )

    # Build node
    node = TradingNode(config=config_node)

    # Configure strategy
    strat_config = WaveTrendMultiTimeframeV4_1Config(
        instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
        # SMALL position size for testing (0.001 BTC ≈ $50-100)
        trade_size=Decimal("0.001"),
        # WaveTrend parameters (V4.1 defaults from backtests)
        wt_5m_channel_length=10,
        wt_5m_average_length=21,
        wt_1h_channel_length=9,
        wt_1h_average_length=18,
        wt_4h_channel_length=8,
        wt_4h_average_length=15,
        min_aligned_timeframes=3,
        # Trailing stop parameters
        atr_period=14,
        atr_multiplier=4.5,
        profit_threshold_pct=4.0,
        percentage_trail=1.0,
        # V3 Regime filters
        use_trend_filter=True,
        trend_filter_threshold=20.0,
        use_atr_min_filter=True,
        atr_min_multiplier=0.5,
        use_range_filter=True,
        range_lookback=100,
        # V4.1 Volatility filter (blocks HIGH/ELEVATED volatility)
        use_volatility_filter=True,
        atr_recent_bars=576,  # 48h at 5m
        atr_baseline_bars=8640,  # 30d at 5m
        high_vol_threshold=1.5,
        elevated_vol_threshold=1.1,
        low_vol_threshold=0.9,
    )

    # Create strategy
    strategy = WaveTrendMultiTimeframeV4_1(config=strat_config)
    node.trader.add_strategy(strategy)

    # Register client factories
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)
    node.build()

    # Print startup banner
    print("\n" + "=" * 80)
    print("STARTING WAVETREND V4.1 ON BINANCE FUTURES TESTNET")
    print("=" * 80)
    print("\nInstrument: BTCUSDT-PERP")
    print("Position size: 0.001 BTC (small for testing)")
    print("Mode: PAPER TRADING (testnet with fake money)")
    print("\nMonitoring checklist:")
    print("  - Run for minimum 24 hours")
    print("  - Watch for CRITICAL errors in logs")
    print("  - Verify stop orders in testnet UI (https://testnet.binancefuture.com/)")
    print("  - Test restart scenario (Ctrl+C then restart)")
    print("  - Check stop update frequency (<20/day expected)")
    print("\nPress Ctrl+C to stop gracefully\n")
    print("=" * 80 + "\n")

    try:
        node.run()
    except KeyboardInterrupt:
        print("\n\nShutting down gracefully...")
    finally:
        node.dispose()
        print("Strategy stopped.")


if __name__ == "__main__":
    run_testnet()
