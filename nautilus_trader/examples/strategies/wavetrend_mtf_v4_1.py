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

import math
from decimal import Decimal

from nautilus_trader.config import PositiveFloat
from nautilus_trader.config import PositiveInt
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators.averages import ExponentialMovingAverage
from nautilus_trader.indicators.volatility import AverageTrueRange
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import BarAggregation
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.enums import TriggerType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import StopMarketOrder
from nautilus_trader.trading.strategy import Strategy


class WaveTrendMultiTimeframeV4_1Config(StrategyConfig, frozen=True, kw_only=True):
    """
    Configuration for WaveTrend Multi-Timeframe strategy V4.1 (Volatility Filtered).

    V4.1 Improvement over V3:
    - Volatility regime detection (Recent ATR vs Baseline ATR)
    - Blocks trades in HIGH or ELEVATED volatility (chop risk)
    - Only trades in NORMAL or LOW volatility (optimal conditions)
    - Simpler than V4 Adaptive: Uses volatility as FILTER, not sizing

    V3 Features (All Retained):
    1. ATR minimum filter: Ensures sufficient volatility
    2. Range filter: Avoids stuck/choppy markets
    3. Multi-timeframe alignment (3/3)
    4. Wider stops (ATR 4.5x)
    5. Higher profit target (4.0%)
    6. Tighter trailing (1.0%)
    7. 4h trend filter

    Expected Result:
    - Fewer trades than V3 (80-120 vs 141)
    - Higher win rate (only optimal conditions)
    - Better returns than V3's +2.02%

    Notes
    -----
    Volatility Regime Classification:
    - HIGH (>1.5x baseline): Chop accelerating → BLOCK
    - ELEVATED (1.1-1.5x): Chop continuing → BLOCK
    - NORMAL (0.9-1.1x): Normal conditions → ALLOW
    - LOW (<0.9x): Chop ending → ALLOW

    """

    instrument_id: InstrumentId
    trade_size: Decimal

    # WaveTrend parameters per timeframe
    wt_5m_channel_length: PositiveInt = 10
    wt_5m_average_length: PositiveInt = 21
    wt_1h_channel_length: PositiveInt = 9
    wt_1h_average_length: PositiveInt = 18
    wt_4h_channel_length: PositiveInt = 8
    wt_4h_average_length: PositiveInt = 15

    # Alignment rule (V2: requires 3/3 by default)
    min_aligned_timeframes: PositiveInt = 3

    # Trailing stop parameters (V2: improved values)
    atr_period: PositiveInt = 14
    atr_multiplier: PositiveFloat = 4.5  # V2: Wider stops (was 3.0)
    profit_threshold_pct: PositiveFloat = 4.0  # V2: Higher profit target (was 2.0)
    percentage_trail: PositiveFloat = 1.0  # V2: Tighter trailing (was 1.5)

    # Trend filter (V2: new feature)
    use_trend_filter: bool = True
    trend_filter_threshold: PositiveFloat = 20.0  # WT1 above/below this = strong trend

    # V3: Regime filters (avoid choppy markets)
    use_atr_min_filter: bool = True
    atr_min_multiplier: PositiveFloat = 0.5  # Minimum ATR as % of price
    use_range_filter: bool = True
    range_lookback: PositiveInt = 100  # Bars to look back for high/low range check

    # V4.1: Volatility regime detection (BLOCKS HIGH/ELEVATED volatility)
    use_volatility_filter: bool = True
    atr_recent_bars: PositiveInt = 576  # 48 hours at 5m
    atr_baseline_bars: PositiveInt = 8640  # 30 days at 5m
    high_vol_threshold: PositiveFloat = 1.5  # Recent/Baseline > 1.5 = HIGH
    elevated_vol_threshold: PositiveFloat = 1.1  # Recent/Baseline > 1.1 = ELEVATED
    low_vol_threshold: PositiveFloat = 0.9  # Recent/Baseline < 0.9 = LOW

    # Order management
    order_id_tag: str = "WT_MTF_V4_1"


class WaveTrendState:
    """
    Holds WaveTrend indicator state for one timeframe.

    Parameters
    ----------
    channel_length : int
        The period for channel EMAs (ESA and D calculation).
    average_length : int
        The period for averaging the Channel Index (CI) to produce WT1.

    Attributes
    ----------
    wt1 : float
        Primary WaveTrend line (EMA of CI).
    wt2 : float
        Signal line (SMA of WT1 over 4 periods).

    """

    def __init__(self, channel_length: int, average_length: int) -> None:
        self.channel_length = channel_length
        self.average_length = average_length

        # WaveTrend calculation components
        self.esa_ema = ExponentialMovingAverage(channel_length)
        self.d_ema = ExponentialMovingAverage(channel_length)
        self.wt1_ema = ExponentialMovingAverage(average_length)
        self.wt1_values: list[float] = []  # Store for SMA(WT1, 4)

        # Current values
        self.wt1: float = 0.0
        self.wt2: float = 0.0
        self.prev_wt1: float = 0.0
        self.prev_wt2: float = 0.0

    def update(self, bar: Bar) -> None:
        """
        Update WaveTrend with new bar using LazyBear formula.

        Parameters
        ----------
        bar : Bar
            The bar containing OHLC data to update the indicator.

        Notes
        -----
        The WaveTrend is calculated as:
        1. HLC3 = (High + Low + Close) / 3
        2. ESA = EMA(HLC3, channel_length)
        3. D = EMA(abs(HLC3 - ESA), channel_length)
        4. CI = (HLC3 - ESA) / (0.015 * D)
        5. WT1 = EMA(CI, average_length)
        6. WT2 = SMA(WT1, 4)

        """
        # Calculate HLC3 (typical price)
        hlc3 = (bar.high.as_double() + bar.low.as_double() + bar.close.as_double()) / 3.0

        # ESA = EMA(HLC3, channel_length)
        self.esa_ema.update_raw(hlc3)
        if not self.esa_ema.initialized:
            return
        esa = self.esa_ema.value

        # D = EMA(abs(HLC3 - ESA), channel_length)
        d_input = abs(hlc3 - esa)
        self.d_ema.update_raw(d_input)
        if not self.d_ema.initialized:
            return
        d = self.d_ema.value

        # CI = (HLC3 - ESA) / (0.015 * D)
        if d == 0:
            ci = 0.0
        else:
            ci = (hlc3 - esa) / (0.015 * d)

        # WT1 = EMA(CI, average_length)
        self.wt1_ema.update_raw(ci)
        if not self.wt1_ema.initialized:
            return

        # Store previous values
        self.prev_wt1 = self.wt1
        self.prev_wt2 = self.wt2

        # Update WT1
        self.wt1 = self.wt1_ema.value

        # WT2 = SMA(WT1, 4)
        self.wt1_values.append(self.wt1)
        if len(self.wt1_values) > 4:
            self.wt1_values.pop(0)

        if len(self.wt1_values) == 4:
            self.wt2 = sum(self.wt1_values) / 4.0

    @property
    def initialized(self) -> bool:
        """
        Check if WaveTrend is ready.

        Returns
        -------
        bool
            True if the indicator has been initialized with sufficient data.

        """
        return len(self.wt1_values) == 4

    def is_bullish(self) -> bool:
        """
        Check if WT1 > WT2 (bullish).

        Returns
        -------
        bool
            True if WT1 is above WT2 (bullish condition).

        """
        return self.wt1 > self.wt2

    def is_bearish(self) -> bool:
        """
        Check if WT1 < WT2 (bearish).

        Returns
        -------
        bool
            True if WT1 is below WT2 (bearish condition).

        """
        return self.wt1 < self.wt2

    def bullish_cross(self) -> bool:
        """
        Check if WT1 just crossed above WT2.

        Returns
        -------
        bool
            True if WT1 crossed above WT2 on the most recent update.

        """
        return self.prev_wt1 <= self.prev_wt2 and self.wt1 > self.wt2

    def bearish_cross(self) -> bool:
        """
        Check if WT1 just crossed below WT2.

        Returns
        -------
        bool
            True if WT1 crossed below WT2 on the most recent update.

        """
        return self.prev_wt1 >= self.prev_wt2 and self.wt1 < self.wt2


class WaveTrendMultiTimeframeV4_1(Strategy):
    """
    Multi-timeframe WaveTrend strategy V4.1 (Volatility Filtered).

    V4.1 Improvement over V3:
    - Volatility regime detection using Recent ATR / Baseline ATR ratio
    - Blocks trades in HIGH or ELEVATED volatility (chop risk)
    - Only trades in NORMAL or LOW volatility (optimal conditions)

    V3 Features (All Retained):
    - ATR minimum filter, range filter
    - Multi-timeframe alignment (3/3)
    - Wider stops (ATR 4.5x), higher profit target (4.0%)
    - Tighter trailing (1.0%), 4h trend filter

    Volatility Regimes:
    - HIGH (>1.5x): Chop accelerating → BLOCK
    - ELEVATED (1.1-1.5x): Chop continuing → BLOCK
    - NORMAL (0.9-1.1x): Normal → ALLOW
    - LOW (<0.9x): Chop ending → ALLOW

    Expected: Fewer trades than V3, higher win rate, better returns.
    """

    def __init__(self, config: WaveTrendMultiTimeframeV4_1Config) -> None:
        super().__init__(config)

        # Configuration
        self.instrument_id = config.instrument_id
        self.trade_size = config.trade_size

        # WaveTrend states for each timeframe
        self.wt_5m = WaveTrendState(
            config.wt_5m_channel_length,
            config.wt_5m_average_length,
        )
        self.wt_1h = WaveTrendState(
            config.wt_1h_channel_length,
            config.wt_1h_average_length,
        )
        self.wt_4h = WaveTrendState(
            config.wt_4h_channel_length,
            config.wt_4h_average_length,
        )

        # ATR for initial trailing stop
        self.atr = AverageTrueRange(config.atr_period)

        # V3: Price history for range filter
        self.price_history_high: list[float] = []
        self.price_history_low: list[float] = []

        # V4.1: ATR history for volatility regime detection
        self.atr_history: list[float] = []

        # Trailing stop state
        self.entry_price: float | None = None
        self.peak_price: float | None = None
        self.stop_order: StopMarketOrder | None = None
        self.use_percentage_trail: bool = False

        # Configuration values
        self.min_aligned = config.min_aligned_timeframes
        self.atr_multiplier = config.atr_multiplier
        self.profit_threshold = config.profit_threshold_pct / 100.0
        self.percentage_trail = config.percentage_trail / 100.0

        # V2: Trend filter configuration
        self.use_trend_filter = config.use_trend_filter
        self.trend_filter_threshold = config.trend_filter_threshold

        # V3: Regime filter configuration
        self.use_atr_min_filter = config.use_atr_min_filter
        self.atr_min_multiplier = config.atr_min_multiplier
        self.use_range_filter = config.use_range_filter
        self.range_lookback = config.range_lookback

        # V4.1: Volatility filter configuration
        self.use_volatility_filter = config.use_volatility_filter
        self.atr_recent_bars = config.atr_recent_bars
        self.atr_baseline_bars = config.atr_baseline_bars
        self.high_vol_threshold = config.high_vol_threshold
        self.elevated_vol_threshold = config.elevated_vol_threshold
        self.low_vol_threshold = config.low_vol_threshold

    def on_start(self) -> None:
        """
        Actions to be performed on strategy start.

        PR #2 IMPORTANT FIX (Issue #6): Reconnection handling - checks for existing
        positions on startup and handles them conservatively.
        """
        self.log.info(f"Starting {self.__class__.__name__}")

        # Subscribe to 5-minute bars
        bar_type_5m = BarType.from_str(
            f"{self.instrument_id}-5-MINUTE-LAST-EXTERNAL"
        )
        self.subscribe_bars(bar_type_5m)

        # Subscribe to 1-hour bars
        bar_type_1h = BarType.from_str(
            f"{self.instrument_id}-1-HOUR-LAST-EXTERNAL"
        )
        self.subscribe_bars(bar_type_1h)

        # Subscribe to 4-hour bars
        bar_type_4h = BarType.from_str(
            f"{self.instrument_id}-4-HOUR-LAST-EXTERNAL"
        )
        self.subscribe_bars(bar_type_4h)

        self.log.info("Subscribed to 5m, 1h, 4h bars")

        # Initialize state variables
        self.entry_price = None
        self.peak_price = None
        self.stop_order = None
        self.use_percentage_trail = False

        # PR #1 CRITICAL FIX: Add tracking flags for stop order management
        self._pending_stop_cancel = False  # Track if waiting for cancel confirmation
        self._last_stop_price = None  # Track last submitted stop price (spam prevention)

        # PR #2 IMPORTANT FIX (Issue #7): Track pending entry orders to prevent race conditions
        self._pending_entry_order = None  # Track pending entry order

        # PR #2 IMPORTANT FIX (Issue #6): CONSERVATIVE RECONNECTION - Close existing positions
        # This ensures clean restart without inheriting unknown state
        positions = self.cache.positions_open(instrument_id=self.instrument_id)

        if positions:
            position = positions[0]
            self.log.warning(
                f"RECONNECTION: Found existing position - closing it for clean restart "
                f"({position.side} {position.quantity} @ {position.avg_px_open}, "
                f"unrealized P&L: {position.unrealized_pnl(position.last)})"
            )

            # Cancel all open orders first
            open_orders = self.cache.orders_open(
                venue=self.instrument_id.venue,
                instrument_id=self.instrument_id,
            )

            for order in open_orders:
                self.log.info(f"Cancelling existing order: {order.client_order_id}")
                self.cancel_order(order)

            # Close position
            self.close_all_positions(self.instrument_id)

            self.log.info("Closed existing position - starting fresh")
        else:
            self.log.info("Started with no existing positions")

    def on_stop(self) -> None:
        """Actions to be performed on strategy stop."""
        self.log.info(f"Stopping {self.__class__.__name__}")
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Handle bar updates for all timeframes."""
        # Update appropriate WaveTrend based on bar aggregation period
        bar_spec = bar.bar_type.spec

        # Debug: Log first bar of each type
        if not hasattr(self, "_bars_received"):
            self._bars_received = {}

        bar_key = f"{bar_spec.step}-{bar_spec.aggregation}"
        if bar_key not in self._bars_received:
            self._bars_received[bar_key] = True
            self.log.info(f"First bar received: {bar.bar_type} (step={bar_spec.step}, agg={bar_spec.aggregation})")

        if bar_spec.step == 5 and bar_spec.aggregation == BarAggregation.MINUTE:
            self._on_bar_5m(bar)
        elif bar_spec.step == 1 and bar_spec.aggregation == BarAggregation.HOUR:
            self._on_bar_1h(bar)
        elif bar_spec.step == 4 and bar_spec.aggregation == BarAggregation.HOUR:
            self._on_bar_4h(bar)
        else:
            self.log.warning(f"Unhandled bar type: {bar.bar_type} (step={bar_spec.step}, agg={bar_spec.aggregation})")

    def _on_bar_5m(self, bar: Bar) -> None:
        """Handle 5-minute bar updates."""
        # Update ATR
        self.atr.update_raw(
            bar.high.as_double(),
            bar.low.as_double(),
            bar.close.as_double(),
        )

        # V4.1: Track ATR history for volatility regime detection
        if self.atr.initialized:
            self.atr_history.append(self.atr.value)
            # Keep only baseline period (30 days = 8640 bars at 5m)
            if len(self.atr_history) > self.atr_baseline_bars:
                self.atr_history.pop(0)

        # V3: Update price history for range filter
        self.price_history_high.append(bar.high.as_double())
        self.price_history_low.append(bar.low.as_double())

        # Keep only lookback period
        if len(self.price_history_high) > self.range_lookback:
            self.price_history_high.pop(0)
            self.price_history_low.pop(0)

        # Update WaveTrend
        prev_initialized = self.wt_5m.initialized
        self.wt_5m.update(bar)

        # Log when 5m WaveTrend first initializes
        if not prev_initialized and self.wt_5m.initialized:
            self.log.info(f"5m WaveTrend initialized (WT1={self.wt_5m.wt1:.2f}, WT2={self.wt_5m.wt2:.2f})")

        if not self.wt_5m.initialized:
            return

        # Check for entry signals
        self._check_entry_signals(bar)

        # Update trailing stop if in position
        self._update_trailing_stop(bar)

    def _on_bar_1h(self, bar: Bar) -> None:
        """Handle 1-hour bar updates."""
        self.wt_1h.update(bar)

    def _on_bar_4h(self, bar: Bar) -> None:
        """Handle 4-hour bar updates."""
        self.wt_4h.update(bar)

    def _count_aligned_timeframes(self, direction: str) -> int:
        """Count how many timeframes are aligned in the given direction."""
        count = 0

        if direction == "bullish":
            if self.wt_5m.is_bullish():
                count += 1
            if self.wt_1h.initialized and self.wt_1h.is_bullish():
                count += 1
            if self.wt_4h.initialized and self.wt_4h.is_bullish():
                count += 1
        elif direction == "bearish":
            if self.wt_5m.is_bearish():
                count += 1
            if self.wt_1h.initialized and self.wt_1h.is_bearish():
                count += 1
            if self.wt_4h.initialized and self.wt_4h.is_bearish():
                count += 1

        return count

    def _detect_volatility_regime(self) -> str:
        """
        V4.1: Detect current volatility regime.

        Compares recent ATR (48h) vs baseline ATR (30d) to classify
        market volatility state.

        Returns
        -------
        str
            One of: 'HIGH', 'ELEVATED', 'NORMAL', 'LOW'
        """
        # Need sufficient ATR history
        if len(self.atr_history) < self.atr_baseline_bars:
            return "NORMAL"  # Default until enough data

        # Calculate recent ATR average (48h = 576 bars at 5m)
        if len(self.atr_history) >= self.atr_recent_bars:
            atr_recent = sum(self.atr_history[-self.atr_recent_bars:]) / self.atr_recent_bars
        else:
            atr_recent = sum(self.atr_history) / len(self.atr_history)

        # Calculate baseline ATR average (30d = 8640 bars)
        atr_baseline = sum(self.atr_history) / len(self.atr_history)

        # Avoid division by zero
        if atr_baseline == 0:
            return "NORMAL"

        # Calculate volatility ratio
        vol_ratio = atr_recent / atr_baseline

        # Classify regime
        if vol_ratio >= self.high_vol_threshold:
            return "HIGH"  # Chop accelerating
        elif vol_ratio >= self.elevated_vol_threshold:
            return "ELEVATED"  # Chop continuing
        elif vol_ratio <= self.low_vol_threshold:
            return "LOW"  # Chop ending
        else:
            return "NORMAL"

    def _check_entry_signals(self, bar: Bar) -> None:
        """
        Check for entry signals based on WaveTrend crosses and alignment.

        PR #2 IMPORTANT FIX (Issue #7): Checks for pending entry orders to prevent
        race conditions where multiple entry orders could be submitted before first fill.

        PR #2 IMPORTANT FIX (Issue #8): Validates ATR and all indicators before entry
        to ensure we can set stops properly.
        """
        # Don't enter if already in a position
        if self.portfolio.is_flat(self.instrument_id) is False:
            return

        # PR #2 FIX (Issue #7): Don't enter if we already have a pending entry order
        if self._pending_entry_order is not None:
            self.log.debug(
                f"Skipping entry signal - order {self._pending_entry_order.client_order_id} still pending"
            )
            return

        # PR #2 FIX (Issue #8): Validate ATR is ready BEFORE checking entry signals
        # CRITICAL: Must ensure ATR is valid so we can set stops after entry
        if not self.atr.initialized:
            self.log.debug("Skipping entry check - ATR indicator not fully initialized")
            return

        atr = self.atr.value
        if atr is None or atr <= 0 or not math.isfinite(atr):
            self.log.debug(f"Skipping entry check - ATR not valid (ATR={atr})")
            return

        # Validate ATR is reasonable (sanity check for calculation errors)
        current_price = bar.close.as_double()
        if atr > current_price * 10:
            self.log.warning(
                f"ATR suspiciously large ({atr:.2f}) vs price ({current_price:.2f}) - "
                f"skipping entry"
            )
            return

        # PR #2 FIX (Issue #8): Validate essential indicators are initialized
        # CRITICAL: 5m WaveTrend + ATR must be ready (primary signal + stops)
        # RELAXED: 1h/4h WaveTrend can be uninitialized - alignment logic handles gracefully
        if not self.wt_5m.initialized:
            self.log.debug("Skipping entry - 5m WaveTrend not initialized")
            return

        # Log partial initialization status (informational for live trading monitoring)
        if not self.wt_1h.initialized or not self.wt_4h.initialized:
            uninit = []
            if not self.wt_1h.initialized:
                uninit.append("1h")
            if not self.wt_4h.initialized:
                uninit.append("4h")
            self.log.info(
                f"Trading with partial initialization - {', '.join(uninit)} WaveTrend "
                f"not ready yet (alignment logic will handle gracefully)"
            )

        # Check for bullish cross on 5m
        bullish = self.wt_5m.bullish_cross()
        bearish = self.wt_5m.bearish_cross()

        if bullish:
            aligned_count = self._count_aligned_timeframes("bullish")

            # V2: Check trend filter
            trend_ok = True
            trend_status = ""
            if self.use_trend_filter and self.wt_4h.initialized:
                trend_ok = self.wt_4h.wt1 > -self.trend_filter_threshold
                trend_status = f", 4h_trend={'OK' if trend_ok else 'BLOCKED'}(WT1={self.wt_4h.wt1:.1f})"

            # V3: Check range filter (avoid stuck/choppy markets)
            range_ok = True
            range_status = ""
            if self.use_range_filter and len(self.price_history_high) >= self.range_lookback:
                current_high = bar.high.as_double()
                current_low = bar.low.as_double()
                lookback_high = max(self.price_history_high)
                lookback_low = min(self.price_history_low)

                # Check if current price is making new highs or lows (expanding range = trending)
                making_new_high = current_high >= lookback_high * 0.999  # Within 0.1% of high
                making_new_low = current_low <= lookback_low * 1.001  # Within 0.1% of low

                range_ok = making_new_high or making_new_low
                range_status = f", Range={'OK' if range_ok else 'BLOCKED'}(H:{making_new_high},L:{making_new_low})"

            # V3: Check ATR minimum filter
            atr_ok = True
            atr_status = ""
            if self.use_atr_min_filter and self.atr.initialized:
                instrument = self.cache.instrument(self.instrument_id)
                if instrument:
                    current_price = bar.close.as_double()
                    atr_min = current_price * (self.atr_min_multiplier / 100.0)
                    atr_ok = self.atr.value >= atr_min
                    atr_status = f", ATR_min={'OK' if atr_ok else 'BLOCKED'}({self.atr.value:.1f}>={atr_min:.1f})"

            # V4.1: Check volatility regime filter (NEW - BLOCKS HIGH/ELEVATED)
            vol_ok = True
            vol_status = ""
            if self.use_volatility_filter:
                vol_regime = self._detect_volatility_regime()
                vol_ok = vol_regime in ["NORMAL", "LOW"]
                vol_status = f", Vol={vol_regime}({'OK' if vol_ok else 'BLOCKED'})"

            self.log.info(
                f"5m Bullish cross detected! WT1={self.wt_5m.wt1:.2f}, WT2={self.wt_5m.wt2:.2f}, "
                f"Aligned: {aligned_count}/3 (5m:{self.wt_5m.is_bullish()}, "
                f"1h:{self.wt_1h.is_bullish() if self.wt_1h.initialized else 'uninit'}, "
                f"4h:{self.wt_4h.is_bullish() if self.wt_4h.initialized else 'uninit'}){trend_status}{range_status}{atr_status}{vol_status}"
            )

            if aligned_count >= self.min_aligned and trend_ok and range_ok and atr_ok and vol_ok:
                self.log.info("All conditions met - entering LONG")
                self._enter_long()
            elif aligned_count >= self.min_aligned:
                reasons = []
                if not trend_ok:
                    reasons.append("trend filter")
                if not range_ok:
                    reasons.append("stuck in range")
                if not atr_ok:
                    reasons.append("ATR too low")
                if not vol_ok:
                    reasons.append(f"{vol_regime} volatility")
                self.log.info(f"Aligned but blocked by: {', '.join(reasons)}")


        # Check for bearish cross on 5m
        elif bearish:
            aligned_count = self._count_aligned_timeframes("bearish")

            # V2: Check trend filter
            trend_ok = True
            trend_status = ""
            if self.use_trend_filter and self.wt_4h.initialized:
                trend_ok = self.wt_4h.wt1 < self.trend_filter_threshold
                trend_status = f", 4h_trend={'OK' if trend_ok else 'BLOCKED'}(WT1={self.wt_4h.wt1:.1f})"

            # V3: Check range filter (same for both directions)
            range_ok = True
            range_status = ""
            if self.use_range_filter and len(self.price_history_high) >= self.range_lookback:
                current_high = bar.high.as_double()
                current_low = bar.low.as_double()
                lookback_high = max(self.price_history_high)
                lookback_low = min(self.price_history_low)

                # Check if current price is making new highs or lows (expanding range = trending)
                making_new_high = current_high >= lookback_high * 0.999  # Within 0.1% of high
                making_new_low = current_low <= lookback_low * 1.001  # Within 0.1% of low

                range_ok = making_new_high or making_new_low
                range_status = f", Range={'OK' if range_ok else 'BLOCKED'}(H:{making_new_high},L:{making_new_low})"

            # V3: Check ATR minimum filter
            atr_ok = True
            atr_status = ""
            if self.use_atr_min_filter and self.atr.initialized:
                instrument = self.cache.instrument(self.instrument_id)
                if instrument:
                    current_price = bar.close.as_double()
                    atr_min = current_price * (self.atr_min_multiplier / 100.0)
                    atr_ok = self.atr.value >= atr_min
                    atr_status = f", ATR_min={'OK' if atr_ok else 'BLOCKED'}({self.atr.value:.1f}>={atr_min:.1f})"

            # V4.1: Check volatility regime filter (NEW - BLOCKS HIGH/ELEVATED)
            vol_ok = True
            vol_status = ""
            if self.use_volatility_filter:
                vol_regime = self._detect_volatility_regime()
                vol_ok = vol_regime in ["NORMAL", "LOW"]
                vol_status = f", Vol={vol_regime}({'OK' if vol_ok else 'BLOCKED'})"

            self.log.info(
                f"5m Bearish cross detected! WT1={self.wt_5m.wt1:.2f}, WT2={self.wt_5m.wt2:.2f}, "
                f"Aligned: {aligned_count}/3 (5m:{self.wt_5m.is_bearish()}, "
                f"1h:{self.wt_1h.is_bearish() if self.wt_1h.initialized else 'uninit'}, "
                f"4h:{self.wt_4h.is_bearish() if self.wt_4h.initialized else 'uninit'}){trend_status}{range_status}{atr_status}{vol_status}"
            )

            if aligned_count >= self.min_aligned and trend_ok and range_ok and atr_ok and vol_ok:
                self.log.info("All conditions met - entering SHORT")
                self._enter_short()
            elif aligned_count >= self.min_aligned:
                reasons = []
                if not trend_ok:
                    reasons.append("trend filter")
                if not range_ok:
                    reasons.append("stuck in range")
                if not atr_ok:
                    reasons.append("ATR too low")
                if not vol_ok:
                    reasons.append(f"{vol_regime} volatility")
                self.log.info(f"Aligned but blocked by: {', '.join(reasons)}")

    def _enter_long(self) -> None:
        """
        Enter a long position.

        PR #2 IMPORTANT FIX (Issue #7): Tracks pending entry order to prevent duplicates.
        """
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.error(f"Cannot enter LONG - instrument {self.instrument_id} not found in cache")
            return

        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(self.trade_size),
        )

        # PR #2 FIX (Issue #7): Track pending entry order BEFORE submitting
        self._pending_entry_order = order

        self.submit_order(order)
        self.log.info(f"Submitted LONG entry order: {order.client_order_id}")

    def _enter_short(self) -> None:
        """
        Enter a short position.

        PR #2 IMPORTANT FIX (Issue #7): Tracks pending entry order to prevent duplicates.
        """
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.error(f"Cannot enter SHORT - instrument {self.instrument_id} not found in cache")
            return

        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(self.trade_size),
        )

        # PR #2 FIX (Issue #7): Track pending entry order BEFORE submitting
        self._pending_entry_order = order

        self.submit_order(order)
        self.log.info(f"Submitted SHORT entry order: {order.client_order_id}")

    def on_order_filled(self, event) -> None:
        """
        Handle order filled events.

        PR #1 CRITICAL FIX (Issue #2): Detect stop fills vs entry fills.
        PR #2 IMPORTANT FIX (Issue #7): Clear pending entry order flag on fill.

        CRITICAL: Must distinguish between:
        - Entry fills (market orders) → initialize state and set stop
        - Stop fills (stop orders) → clear state and close position
        """
        # Check if this was our stop order filling
        if self.stop_order is not None and event.client_order_id == self.stop_order.client_order_id:
            self.log.info(
                f"Stop order filled: {event.order_side} {event.last_qty} @ {event.last_px} "
                f"(position closed by stop)"
            )

            # PR #1 CRITICAL FIX: Clear all state when stop fills
            self.entry_price = None
            self.peak_price = None
            self.stop_order = None
            self.use_percentage_trail = False
            self._pending_stop_cancel = False
            self._last_stop_price = None
            # PR #2 FIX: Also clear pending entry order (defensive)
            self._pending_entry_order = None

            return  # Don't treat stop fill as new entry

        # PR #2 FIX (Issue #7): Check if this was our pending entry order
        if self._pending_entry_order is not None and event.client_order_id == self._pending_entry_order.client_order_id:
            # Clear pending entry flag - order has filled
            self._pending_entry_order = None

            # Entry order filled - set initial stop
            self.entry_price = event.last_px.as_double()
            self.peak_price = self.entry_price
            self.use_percentage_trail = False

            self.log.info(
                f"Entry filled at {self.entry_price:.2f}, setting ATR-based stop"
            )

            # Set initial ATR-based stop
            self._set_atr_stop(event.order_side)
            return

        # Unknown order fill (shouldn't happen in normal operation)
        self.log.warning(
            f"Received fill for unknown order: {event.client_order_id}"
        )

    def on_order_canceled(self, event) -> None:
        """
        Handle order cancellation confirmation.

        PR #1 CRITICAL FIX (Issue #1): This prevents creating duplicate stops by
        waiting for cancel confirmation before submitting replacement stop.

        PR #2 IMPORTANT FIX (Issue #7): Clear pending entry order flag on cancellation.

        BUGFIX: After cancel completes, recreate the stop based on current mode.
        """
        self.log.info(f"on_order_canceled called for order {event.client_order_id}")

        # PR #2 FIX (Issue #7): Check if this was our pending entry order (user manually cancelled?)
        if self._pending_entry_order is not None and event.client_order_id == self._pending_entry_order.client_order_id:
            self.log.info(
                f"Entry order {self._pending_entry_order.client_order_id} cancelled"
            )
            self._pending_entry_order = None
            return

        # Clear the stop reference if it was our stop that got cancelled
        if self.stop_order is not None and event.client_order_id == self.stop_order.client_order_id:
            self.log.info(
                f"Stop order {self.stop_order.client_order_id} successfully cancelled"
            )
            self.stop_order = None
            self._pending_stop_cancel = False

            # BUGFIX: Recreate the stop after cancellation completes
            # This is critical - without it, position remains unprotected!
            positions = self.cache.positions_open(instrument_id=self.instrument_id)
            if positions:
                position = positions[0]
                if self.use_percentage_trail:
                    # We're in percentage trailing mode - recreate percentage stop
                    self.log.info("Recreating percentage trailing stop after cancel")
                    self._set_percentage_stop(position.side)
                else:
                    # We're in ATR mode - recreate ATR stop
                    self.log.info("Recreating ATR stop after cancel")
                    self._set_atr_stop(position.side)

    def on_order_rejected(self, event) -> None:
        """
        Handle order rejection events.

        PR #1 CRITICAL FIX (Issue #3): If a stop order is rejected, the position
        is UNPROTECTED. We must either retry stop submission or close position.

        PR #2 IMPORTANT FIX (Issue #7): Clear pending entry order flag on rejection.
        """
        # PR #2 FIX (Issue #7): Check if this was our pending entry order
        if self._pending_entry_order is not None and event.client_order_id == self._pending_entry_order.client_order_id:
            self.log.warning(
                f"Entry order rejected: {event.client_order_id} - Reason: {event.reason}"
            )
            # Clear pending flag so we can try again on next signal
            self._pending_entry_order = None
            return

        # Check if this was our stop order being rejected
        if self.stop_order is not None and event.client_order_id == self.stop_order.client_order_id:
            self.log.error(
                f"CRITICAL: Stop order REJECTED - position is UNPROTECTED! "
                f"Reason: {event.reason}"
            )

            # Clear stop reference
            self.stop_order = None
            self._pending_stop_cancel = False
            self._last_stop_price = None

            # Check if position still exists
            positions = self.cache.positions_open(instrument_id=self.instrument_id)
            if positions:
                # Position is open but stop was rejected - CRITICAL situation
                # Strategy: Close position immediately (safest approach)
                self.log.warning(
                    f"Closing position immediately due to stop rejection - "
                    f"cannot trade without stop protection"
                )
                self.close_all_positions(self.instrument_id)

                # Clear state
                self.entry_price = None
                self.peak_price = None
                self.use_percentage_trail = False

            return

        # Unknown order rejection
        self.log.warning(
            f"Order rejected: {event.client_order_id} - Reason: {event.reason}"
        )

    def _set_atr_stop(self, entry_side: OrderSide) -> None:
        """
        Set ATR-based trailing stop.

        PR #1 CRITICAL FIX (Issues #1, #5): Deferred creation + validation.
        """
        # Check position exists
        positions = self.cache.positions_open(instrument_id=self.instrument_id)
        if not positions:
            return
        position = positions[0]

        # Get instrument from cache
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.error(f"Cannot set ATR stop - instrument {self.instrument_id} not found in cache")
            return

        # PR #1 FIX (Issue #1): Cancel existing stop and mark pending
        # CRITICAL: Wait for cancellation before creating new stop
        if self.stop_order is not None:
            self.log.info(
                f"Cancelling existing stop {self.stop_order.client_order_id} before creating new ATR stop"
            )
            self._pending_stop_cancel = True
            self.cancel_order(self.stop_order)
            # DO NOT create new stop here - wait for on_order_cancelled callback
            return

        # Get current price and ATR
        current_price = position.avg_px_open
        atr = self.atr.value

        # PR #1 FIX (Issue #5): Validate ATR is valid
        if atr is None or atr <= 0 or not math.isfinite(atr):
            self.log.error(
                f"Cannot set ATR stop - invalid ATR value: {atr}"
            )
            # Close position if we can't protect it
            self.log.warning("Closing position due to inability to set stop loss")
            self.close_all_positions(self.instrument_id)
            return

        # ATR sanity check (shouldn't be >10x the price)
        if atr > current_price * 10:
            self.log.error(
                f"ATR value suspiciously large ({atr:.2f}) vs price ({current_price:.2f}) - "
                f"possible calculation error"
            )
            self.close_all_positions(self.instrument_id)
            return

        # Calculate stop distance
        stop_distance = atr * self.atr_multiplier

        # Calculate stop price based on position direction
        if entry_side == OrderSide.BUY:
            stop_price = current_price - stop_distance
        else:
            stop_price = current_price + stop_distance

        # PR #1 FIX (Issue #5): Validate stop price is positive
        if stop_price <= 0:
            self.log.error(
                f"Calculated stop price is invalid ({stop_price:.2f}) - "
                f"current={current_price:.2f}, ATR={atr:.2f}, mult={self.atr_multiplier}"
            )
            self.close_all_positions(self.instrument_id)
            return

        # Validate stop price is reasonable distance from current price
        min_distance_pct = 0.001  # 0.1% minimum
        distance_pct = abs(stop_price - current_price) / current_price

        if distance_pct < min_distance_pct:
            self.log.error(
                f"Stop price ({stop_price:.2f}) too close to current price ({current_price:.2f}) - "
                f"distance {distance_pct*100:.3f}% < minimum {min_distance_pct*100:.1f}%"
            )
            self.close_all_positions(self.instrument_id)
            return

        # Validate stop respects position direction
        if entry_side == OrderSide.BUY and stop_price >= current_price:
            self.log.error(
                f"LONG stop price ({stop_price:.2f}) must be BELOW current price ({current_price:.2f})"
            )
            self.close_all_positions(self.instrument_id)
            return

        if entry_side == OrderSide.SELL and stop_price <= current_price:
            self.log.error(
                f"SHORT stop price ({stop_price:.2f}) must be ABOVE current price ({current_price:.2f})"
            )
            self.close_all_positions(self.instrument_id)
            return

        # All validations passed - create stop order
        trigger_price = instrument.make_price(stop_price)

        self.stop_order = self.order_factory.stop_market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY,
            quantity=instrument.make_qty(self.trade_size),
            trigger_price=trigger_price,
            trigger_type=TriggerType.DEFAULT,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(self.stop_order)

        # PR #1 FIX (Issue #4): Track last stop price for spam prevention
        self._last_stop_price = stop_price

        self.log.info(
            f"Set ATR stop: {stop_price:.2f} (ATR={atr:.2f}, mult={self.atr_multiplier}, "
            f"distance={distance_pct*100:.2f}%)"
        )

    def _set_percentage_stop(self, position_side: OrderSide) -> None:
        """
        Set percentage-based trailing stop from peak price.

        PR #1 CRITICAL FIX (Issues #1, #5): Deferred creation + validation.
        """
        # Check position exists
        positions = self.cache.positions_open(instrument_id=self.instrument_id)
        if not positions:
            return
        position = positions[0]

        # Get instrument from cache
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.error(f"Cannot set percentage stop - instrument {self.instrument_id} not found in cache")
            return

        # PR #1 FIX (Issue #1): Cancel existing stop and mark pending
        # CRITICAL: Wait for cancellation before creating new stop
        if self.stop_order is not None:
            self.log.info(
                f"Cancelling existing stop {self.stop_order.client_order_id} before creating new percentage stop"
            )
            self._pending_stop_cancel = True
            self.cancel_order(self.stop_order)
            return

        # PR #1 FIX (Issue #5): Validate peak price
        if self.peak_price is None or self.peak_price <= 0:
            self.log.error(
                f"Cannot set percentage stop - invalid peak price: {self.peak_price}"
            )
            self.close_all_positions(self.instrument_id)
            return

        # Calculate stop price from peak
        trail_distance_pct = self.percentage_trail

        # Validate trail percentage is reasonable
        if trail_distance_pct <= 0 or trail_distance_pct >= 1.0:
            self.log.error(
                f"Invalid trail percentage: {trail_distance_pct*100:.1f}% "
                f"(must be between 0 and 100)"
            )
            self.close_all_positions(self.instrument_id)
            return

        if position_side == OrderSide.BUY:
            stop_price = self.peak_price * (1 - trail_distance_pct)
        else:
            stop_price = self.peak_price * (1 + trail_distance_pct)

        # PR #1 FIX (Issue #5): Validate stop price is positive
        if stop_price <= 0:
            self.log.error(
                f"Calculated percentage stop price is invalid ({stop_price:.2f}) - "
                f"peak={self.peak_price:.2f}, trail={trail_distance_pct*100:.1f}%"
            )
            self.close_all_positions(self.instrument_id)
            return

        # Get current price and check stop direction
        current_price = position.avg_px_open

        if position_side == OrderSide.BUY and stop_price >= current_price:
            self.log.error(
                f"LONG percentage stop ({stop_price:.2f}) cannot be above current price ({current_price:.2f})"
            )
            self.close_all_positions(self.instrument_id)
            return

        if position_side == OrderSide.SELL and stop_price <= current_price:
            self.log.error(
                f"SHORT percentage stop ({stop_price:.2f}) cannot be below current price ({current_price:.2f})"
            )
            self.close_all_positions(self.instrument_id)
            return

        # All validations passed - create stop order
        trigger_price = instrument.make_price(stop_price)

        self.stop_order = self.order_factory.stop_market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL if position_side == OrderSide.BUY else OrderSide.BUY,
            quantity=instrument.make_qty(self.trade_size),
            trigger_price=trigger_price,
            trigger_type=TriggerType.DEFAULT,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(self.stop_order)

        # PR #1 FIX (Issue #4): Track last stop price for spam prevention
        self._last_stop_price = stop_price

        self.log.info(
            f"Set percentage stop: {stop_price:.2f} (peak={self.peak_price:.2f}, "
            f"trail={trail_distance_pct*100:.1f}%, current={current_price:.2f})"
        )

    def _update_trailing_stop(self, bar: Bar) -> None:
        """
        Update trailing stop based on current price and P&L.

        PR #1 CRITICAL FIX (Issue #4): Spam prevention + pending cancel guard.
        """
        # Only update if in a position
        if self.portfolio.is_flat(self.instrument_id):
            return

        if self.entry_price is None:
            return

        # PR #1 FIX: Don't update stops while cancel is pending
        if self._pending_stop_cancel:
            self.log.debug("Skipping stop update - cancel pending")
            return

        # Get current position
        positions = self.cache.positions_open(instrument_id=self.instrument_id)
        if not positions:
            return
        position = positions[0]  # Get the first open position

        current_price = bar.close.as_double()

        # Update peak price
        if position.side == OrderSide.BUY:
            # Long position - track highest price
            if self.peak_price is None or current_price > self.peak_price:
                self.peak_price = current_price
        elif position.side == OrderSide.SELL:
            # Short position - track lowest price
            if self.peak_price is None or current_price < self.peak_price:
                self.peak_price = current_price

        # Calculate unrealized P&L percentage
        if position.side == OrderSide.BUY:
            pnl_pct = (current_price - self.entry_price) / self.entry_price
        else:
            pnl_pct = (self.entry_price - current_price) / self.entry_price

        # Check if we should switch to percentage trail
        if not self.use_percentage_trail and pnl_pct >= self.profit_threshold:
            self.log.info(
                f"Profit threshold reached ({pnl_pct * 100:.2f}%), "
                f"switching to percentage trail"
            )
            self.use_percentage_trail = True
            self._set_percentage_stop(position.side)

        # Update stop based on current mode
        elif self.use_percentage_trail:
            # PR #1 FIX (Issue #4): Only update if stop price actually changed
            # Calculate what the new stop price WOULD be
            trail_distance_pct = self.percentage_trail

            if position.side == OrderSide.BUY:
                new_stop_price = self.peak_price * (1 - trail_distance_pct)
            else:
                new_stop_price = self.peak_price * (1 + trail_distance_pct)

            # CRITICAL: Only update if stop price changed significantly
            # Use small epsilon for float comparison
            EPSILON = 0.01  # 1 cent minimum movement

            if self._last_stop_price is None:
                # No stop set yet - set it
                self._set_percentage_stop(position.side)
            elif abs(new_stop_price - self._last_stop_price) >= EPSILON:
                # Stop price changed significantly - update it
                self.log.info(
                    f"Stop price moved: {self._last_stop_price:.2f} → {new_stop_price:.2f} "
                    f"(peak={self.peak_price:.2f})"
                )
                self._set_percentage_stop(position.side)
            else:
                # Stop price unchanged - skip update (spam prevention)
                self.log.debug(
                    f"Stop price unchanged ({self._last_stop_price:.2f}) - skipping update"
                )

    def on_position_closed(self, position) -> None:
        """
        Handle position closed event.

        BUGFIX: Clear ALL state including tracking flags to ensure complete cleanup.
        This handles edge cases like manual closes, risk engine closes, liquidations.
        """
        self.log.info(f"Position closed: {position}")

        # Reset ALL state (complete cleanup)
        self.entry_price = None
        self.peak_price = None
        self.stop_order = None
        self.use_percentage_trail = False

        # BUGFIX: Also clear tracking flags (PR #1 & #2)
        # Critical: Ensures clean state for next position in all close scenarios
        self._pending_stop_cancel = False
        self._last_stop_price = None
        self._pending_entry_order = None
