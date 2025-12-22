#!/usr/bin/env python3
"""
WaveTrend V4.1 Backtest - RELAXED PARAMETERS

Testing hypothesis: Strict filters are killing trade count and win rate.

Changes from original V4.1:
1. ATR_min filter DISABLED (was blocking 80-90% of trades)
2. Alignment requirement: 2/3 (was 3/3 - too strict, late entries)
3. ATR multiplier: 3.0 (was 4.5 - too wide, long losers)
4. Profit threshold: 2.5% (was 4.0% - trailing activates earlier)

Expected improvements:
- Trade count: 7 → 40-60 per year
- Win rate: 10% → 25-35%
- Max hold time: 31 days → 7-14 days
"""

import sys
from decimal import Decimal
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.engine import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import WaveTrendMultiTimeframeV4_1
from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import WaveTrendMultiTimeframeV4_1Config
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog


# *** CONFIGURE THESE PARAMETERS ***

# Data catalog path
CATALOG_PATH = Path("~/.nautilus/catalog").expanduser()

# Instrument
VENUE = Venue("BINANCE")
SYMBOL = "BTCUSDT-PERP"
instrument_id = InstrumentId.from_str(f"{SYMBOL}.{VENUE}")

# Default backtest period
DEFAULT_START = "2024-01-01"
DEFAULT_END = "2024-12-31"

# Strategy parameters
TRADE_SIZE = Decimal("0.01")


def run_backtest(start_date=None, end_date=None):
    """Run WaveTrend MTF V4.1 with RELAXED parameters."""
    START = start_date or DEFAULT_START
    END = end_date or DEFAULT_END

    print(f"\n{'='*80}")
    print(f"BACKTEST PERIOD: {START} to {END}")
    print(f"CONFIGURATION: RELAXED (ATR_min OFF, 2/3 align, tighter stops)")
    print(f"{'='*80}\n")

    # Load data catalog
    catalog = ParquetDataCatalog(CATALOG_PATH)

    # Configure backtest engine
    config = BacktestEngineConfig(
        trader_id=TraderId("BACKTESTER-001"),
        logging=LoggingConfig(log_level="INFO"),
    )
    engine = BacktestEngine(config=config)

    # Load instrument
    instruments = catalog.instruments(instrument_ids=[str(instrument_id)])
    if not instruments:
        raise ValueError(f"No instrument found for {instrument_id}")

    instrument = instruments[0]

    # Add venue
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, instrument.quote_currency)],
    )

    engine.add_instrument(instrument)

    # Load bar data for all three timeframes
    print(f"Loading bars for {instrument_id}...")

    # Load 5m bars
    bars_5m = catalog.bars(
        bar_types=[f"{instrument_id}-5-MINUTE-LAST-EXTERNAL"],
        instrument_ids=[str(instrument_id)],
        start=START,
        end=END,
    )
    if bars_5m:
        engine.add_data(bars_5m)
        print(f"✓ Loaded {len(bars_5m)} 5m bars")
    else:
        print("⚠ No 5m bars loaded!")

    # Load 1h bars
    bars_1h = catalog.bars(
        bar_types=[f"{instrument_id}-1-HOUR-LAST-EXTERNAL"],
        instrument_ids=[str(instrument_id)],
        start=START,
        end=END,
    )
    if bars_1h:
        engine.add_data(bars_1h)
        print(f"✓ Loaded {len(bars_1h)} 1h bars")
    else:
        print("⚠ No 1h bars loaded!")

    # Load 4h bars
    bars_4h = catalog.bars(
        bar_types=[f"{instrument_id}-4-HOUR-LAST-EXTERNAL"],
        instrument_ids=[str(instrument_id)],
        start=START,
        end=END,
    )
    if bars_4h:
        engine.add_data(bars_4h)
        print(f"✓ Loaded {len(bars_4h)} 4h bars")
    else:
        print("⚠ No 4h bars loaded!")

    # Configure strategy - RELAXED PARAMETERS
    strat_config = WaveTrendMultiTimeframeV4_1Config(
        instrument_id=instrument_id,
        trade_size=TRADE_SIZE,
        # WaveTrend parameters (same as strict)
        wt_5m_channel_length=10,
        wt_5m_average_length=21,
        wt_1h_channel_length=9,
        wt_1h_average_length=18,
        wt_4h_channel_length=8,
        wt_4h_average_length=15,
        min_aligned_timeframes=2,  # ← RELAXED: 2/3 instead of 3/3
        # Trailing stop parameters - TIGHTENED
        atr_period=14,
        atr_multiplier=3.0,  # ← TIGHTENED: 3.0 instead of 4.5
        profit_threshold_pct=2.5,  # ← LOWERED: 2.5% instead of 4.0%
        percentage_trail=1.0,
        # V3 Regime filters
        use_trend_filter=True,
        trend_filter_threshold=20.0,
        use_atr_min_filter=False,  # ← DISABLED: Was blocking 80-90% of trades!
        atr_min_multiplier=0.5,  # (not used when disabled)
        use_range_filter=True,
        range_lookback=100,
        # V4.1: Volatility filter (KEEP - it works)
        use_volatility_filter=True,
        atr_recent_bars=576,  # 48h at 5m
        atr_baseline_bars=8640,  # 30d at 5m
        high_vol_threshold=1.5,
        elevated_vol_threshold=1.1,
        low_vol_threshold=0.9,
    )

    # Add strategy
    strategy = WaveTrendMultiTimeframeV4_1(config=strat_config)
    engine.add_strategy(strategy)

    # Run backtest
    print("\nRunning backtest...")
    engine.run()

    # Print results
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS - RELAXED PARAMETERS")
    print("=" * 80)

    # Account report
    print("\n--- Account Report ---")
    print(engine.trader.generate_account_report(VENUE))

    # Order fills report
    print("\n--- Order Fills Report ---")
    print(engine.trader.generate_order_fills_report())

    # Positions report
    print("\n--- Positions Report ---")
    print(engine.trader.generate_positions_report())

    # Cleanup
    engine.dispose()


if __name__ == "__main__":
    # Parse command line arguments
    start = sys.argv[1] if len(sys.argv) > 1 else None
    end = sys.argv[2] if len(sys.argv) > 2 else None

    run_backtest(start, end)
