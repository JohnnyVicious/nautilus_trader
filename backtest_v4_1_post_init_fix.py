#!/usr/bin/env python3
"""
Backtest WaveTrend V4.1 - Post Initialization Gating Fix
Test years 2022, 2023, 2024 individually to verify fix doesn't change behavior.
"""

from decimal import Decimal
import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.engine import BacktestEngineConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from nautilus_trader.examples.strategies.wavetrend_mtf_v4_1 import (
    WaveTrendMultiTimeframeV4_1,
    WaveTrendMultiTimeframeV4_1Config,
)

USDT = TestInstrumentProvider.btcusdt_binance().quote_currency


def run_backtest_year(year: int) -> dict:
    """Run backtest for a specific year."""
    print(f"\n{'='*80}")
    print(f"BACKTESTING YEAR {year}")
    print(f"{'='*80}\n")

    # Load catalog
    catalog = ParquetDataCatalog("/home/johan/.nautilus/data/catalog")

    # Define instrument - use test provider (catalog may not have it indexed)
    instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")

    # Try to load from catalog first, fall back to test provider
    try:
        instruments = catalog.instruments(instrument_ids=[instrument_id])
        if instruments:
            instrument = instruments[0]
        else:
            # Use test provider if not in catalog
            from nautilus_trader.test_kit.providers import TestInstrumentProvider
            instrument = TestInstrumentProvider.btcusdt_binance_perp()
    except Exception:
        # Fall back to test provider
        from nautilus_trader.test_kit.providers import TestInstrumentProvider
        instrument = TestInstrumentProvider.btcusdt_binance_perp()

    # Configure engine
    config = BacktestEngineConfig(
        trader_id="BACKTESTER-001",
        logging_level="INFO",
    )

    engine = BacktestEngine(config=config)

    # Add venue
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(10_000, USDT)],
    )

    # Add instrument
    engine.add_instrument(instrument)

    # Define time range
    start = pd.Timestamp(f"{year}-01-01", tz="UTC")
    end = pd.Timestamp(f"{year}-12-31 23:59:59", tz="UTC")

    # Load data for all required timeframes
    print(f"Loading bars for {year}...")

    # 5m bars (primary signal)
    bars_5m = catalog.bars(
        instrument_ids=[instrument_id],
        bar_type=f"BTCUSDT-PERP.BINANCE-5-MINUTE-LAST-EXTERNAL",
        start=dt_to_unix_nanos(start),
        end=dt_to_unix_nanos(end),
    )
    engine.add_data(bars_5m)
    print(f"  Loaded {len(bars_5m)} 5m bars")

    # 1h bars (alignment)
    bars_1h = catalog.bars(
        instrument_ids=[instrument_id],
        bar_type=f"BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        start=dt_to_unix_nanos(start),
        end=dt_to_unix_nanos(end),
    )
    engine.add_data(bars_1h)
    print(f"  Loaded {len(bars_1h)} 1h bars")

    # 4h bars (alignment)
    bars_4h = catalog.bars(
        instrument_ids=[instrument_id],
        bar_type=f"BTCUSDT-PERP.BINANCE-4-HOUR-LAST-EXTERNAL",
        start=dt_to_unix_nanos(start),
        end=dt_to_unix_nanos(end),
    )
    engine.add_data(bars_4h)
    print(f"  Loaded {len(bars_4h)} 4h bars")

    # Configure strategy (V4.1 parameters from production config)
    strategy_config = WaveTrendMultiTimeframeV4_1Config(
        instrument_id=instrument_id,
        trade_size=Decimal("0.01"),
        # WaveTrend parameters (V4.1 optimized)
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
        # V4.1 Volatility filter
        use_volatility_filter=True,
        atr_recent_bars=576,
        atr_baseline_bars=8640,
        high_vol_threshold=1.5,
        elevated_vol_threshold=1.1,
        low_vol_threshold=0.9,
    )

    strategy = WaveTrendMultiTimeframeV4_1(config=strategy_config)
    engine.add_strategy(strategy)

    # Run backtest
    print(f"\nRunning backtest for {year}...")
    engine.run()

    # Extract results
    account = engine.trader.generate_account_report(Venue("BINANCE"))

    # Get position stats
    positions = engine.trader.position_snapshots()
    total_positions = len(positions)

    winners = sum(1 for p in positions if p.realized_pnl.as_double() > 0)
    losers = sum(1 for p in positions if p.realized_pnl.as_double() < 0)
    win_rate = (winners / total_positions * 100) if total_positions > 0 else 0

    total_pnl = sum(p.realized_pnl.as_double() for p in positions)

    # Calculate position hold times
    hold_times = []
    for p in positions:
        if p.ts_closed and p.ts_opened:
            hold_nanos = p.ts_closed - p.ts_opened
            hold_days = hold_nanos / 1_000_000_000 / 86400
            hold_times.append(hold_days)

    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
    max_hold = max(hold_times) if hold_times else 0

    results = {
        "year": year,
        "total_pnl": total_pnl,
        "total_positions": total_positions,
        "winners": winners,
        "losers": losers,
        "win_rate": win_rate,
        "avg_hold_days": avg_hold,
        "max_hold_days": max_hold,
    }

    # Print summary
    print(f"\n{'='*80}")
    print(f"YEAR {year} RESULTS")
    print(f"{'='*80}")
    print(f"Total P&L: {total_pnl:.2f} USDT ({total_pnl/100:.2f}%)")
    print(f"Total Positions: {total_positions}")
    print(f"Winners: {winners} | Losers: {losers}")
    print(f"Win Rate: {win_rate:.1f}%")
    print(f"Avg Hold Time: {avg_hold:.1f} days")
    print(f"Max Hold Time: {max_hold:.1f} days")
    print(f"{'='*80}\n")

    engine.dispose()

    return results


def main():
    """Run backtests for 2022, 2023, and 2024."""
    print("\n" + "="*80)
    print("WaveTrend V4.1 - Post Initialization Gating Fix Verification")
    print("Testing years 2022, 2023, 2024 individually")
    print("="*80)

    years = [2022, 2023, 2024]
    all_results = []

    for year in years:
        results = run_backtest_year(year)
        all_results.append(results)

    # Print comparison table
    print("\n" + "="*80)
    print("SUMMARY - ALL YEARS")
    print("="*80)
    print(f"{'Year':<8} {'P&L (USDT)':<15} {'Positions':<12} {'Win Rate':<12} {'Max Hold (days)':<15}")
    print("-" * 80)

    total_pnl = 0
    total_positions = 0
    total_winners = 0

    for r in all_results:
        print(
            f"{r['year']:<8} "
            f"{r['total_pnl']:<15.2f} "
            f"{r['total_positions']:<12} "
            f"{r['win_rate']:<12.1f}% "
            f"{r['max_hold_days']:<15.1f}"
        )
        total_pnl += r['total_pnl']
        total_positions += r['total_positions']
        total_winners += r['winners']

    overall_win_rate = (total_winners / total_positions * 100) if total_positions > 0 else 0

    print("-" * 80)
    print(
        f"{'TOTAL':<8} "
        f"{total_pnl:<15.2f} "
        f"{total_positions:<12} "
        f"{overall_win_rate:<12.1f}% "
    )
    print("="*80)

    # Verify no catastrophic holds
    max_hold_all = max(r['max_hold_days'] for r in all_results)
    if max_hold_all > 365:
        print(f"\n⚠️  WARNING: Found position held for {max_hold_all:.0f} days (>1 year)!")
    else:
        print(f"\n✅ PASS: No positions held longer than 1 year (max: {max_hold_all:.1f} days)")

    print("\n" + "="*80)
    print("VERIFICATION COMPLETE")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
