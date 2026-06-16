"""
strategies.py — Four intraday trading strategy classes.

Module 1: ORBStrategy          — Opening Range Breakout
Module 2: ReversalStrategy     — Fading the Open
Module 3: GapAndGoStrategy     — Gap and Go
Module 4: VWAPReversionStrategy — VWAP Mean Reversion
"""

from collections import namedtuple
import logging

log = logging.getLogger(__name__)

# ─── Signal namedtuple ────────────────────────────────────────────────────────

Signal = namedtuple(
    "Signal",
    ["strategy", "direction", "symbol", "entry_price", "tp_price", "sl_stop", "sl_limit"],
)


# ─── Module 1: Opening Range Breakout ────────────────────────────────────────

class ORBStrategy:
    """
    Opening Range Breakout (9:30–9:45 AM ET).

    The strategy tracks the high/low during the ORB window, freezes them at
    9:45, then fires:
      - LONG  when price crosses above orb_high + tick_size
      - INVERSE when price crosses below orb_low - tick_size (buys mapped inverse ETF)

    Each trigger fires at most once per symbol per day.
    """

    ALLOC_PCT: float = 0.02    # 2% of account
    TP_PCT:    float = 0.010   # +1.0%
    SL_PCT:    float = 0.005   # -0.5%

    def __init__(self):
        # Per-symbol state dicts
        self._open:             dict = {}   # symbol -> open price at 9:30
        self._high:             dict = {}   # symbol -> running high during window
        self._low:              dict = {}   # symbol -> running low during window
        self._established:      dict = {}   # symbol -> bool (window data received)
        self._frozen:           dict = {}   # symbol -> bool (freeze called)
        self._triggered_long:   dict = {}   # symbol -> bool
        self._triggered_inv:    dict = {}   # symbol -> bool

    # ------------------------------------------------------------------
    # Called every tick during 9:30–9:45 window
    # ------------------------------------------------------------------

    def update(self, symbol: str, price: float, in_window: bool) -> None:
        """Update high/low tracking while inside the ORB window."""
        if not in_window:
            return
        if symbol not in self._open:
            self._open[symbol]  = price
            self._high[symbol]  = price
            self._low[symbol]   = price
            self._established[symbol] = True
        else:
            if price > self._high[symbol]:
                self._high[symbol] = price
            if price < self._low[symbol]:
                self._low[symbol] = price

    # ------------------------------------------------------------------
    # Called once at 9:45
    # ------------------------------------------------------------------

    def freeze(self, symbol: str) -> None:
        """Lock the ORB boundaries; no further updates allowed."""
        if self._established.get(symbol):
            self._frozen[symbol] = True
            log.debug(
                "ORB frozen %s  open=%.4f  high=%.4f  low=%.4f",
                symbol,
                self._open.get(symbol, 0),
                self._high.get(symbol, 0),
                self._low.get(symbol, 0),
            )

    # ------------------------------------------------------------------
    # Called after freeze to check for breakout signals
    # ------------------------------------------------------------------

    def check_signal(
        self,
        symbol: str,
        price: float,
        inverse_map: dict,
        tick_size: float = 0.01,
    ) -> "Signal | None":
        """
        Check for an ORB breakout signal after the window is frozen.

        Parameters
        ----------
        symbol     : The underlying ticker being evaluated.
        price      : Current market price.
        inverse_map: INVERSE_ETF_MAP dict {underlying -> inverse_etf}.
        tick_size  : Minimum clearance above/below boundary to confirm break.

        Returns
        -------
        Signal or None
        """
        if not self._frozen.get(symbol):
            return None
        if symbol not in self._high:
            return None

        orb_high = self._high[symbol]
        orb_low  = self._low[symbol]

        # ── Long breakout ────────────────────────────────────────────
        if not self._triggered_long.get(symbol) and price > orb_high + tick_size:
            self._triggered_long[symbol] = True
            entry = round(price, 4)
            tp    = round(entry * (1 + self.TP_PCT), 4)
            sl_stop  = round(entry * (1 - self.SL_PCT), 4)
            sl_limit = round(sl_stop * 0.995, 4)
            log.info("ORB LONG signal  %s  entry=%.4f  tp=%.4f  sl=%.4f", symbol, entry, tp, sl_stop)
            return Signal(
                strategy="ORB",
                direction="LONG",
                symbol=symbol,
                entry_price=entry,
                tp_price=tp,
                sl_stop=sl_stop,
                sl_limit=sl_limit,
            )

        # ── Inverse breakout (breakdown) ─────────────────────────────
        if (
            not self._triggered_inv.get(symbol)
            and price < orb_low - tick_size
            and symbol in inverse_map
        ):
            self._triggered_inv[symbol] = True
            inv_symbol = inverse_map[symbol]
            # Entry for the inverse ETF is the current price of the *inverse*,
            # not the underlying.  The caller must re-price entry after fetching
            # the inverse ETF quote.  We embed inv_symbol in the Signal.symbol.
            entry    = round(price, 4)   # placeholder; caller re-prices
            tp       = round(entry * (1 + self.TP_PCT), 4)
            sl_stop  = round(entry * (1 - self.SL_PCT), 4)
            sl_limit = round(sl_stop * 0.995, 4)
            log.info(
                "ORB INVERSE signal  %s -> %s  entry=%.4f  tp=%.4f  sl=%.4f",
                symbol, inv_symbol, entry, tp, sl_stop,
            )
            return Signal(
                strategy="ORB",
                direction="INVERSE",
                symbol=inv_symbol,
                entry_price=entry,
                tp_price=tp,
                sl_stop=sl_stop,
                sl_limit=sl_limit,
            )

        return None

    def get_bracket(self, entry_price: float) -> tuple:
        """
        Returns (tp_price, sl_stop_price, sl_limit_price) for a given entry.
        """
        tp       = round(entry_price * (1 + self.TP_PCT), 4)
        sl_stop  = round(entry_price * (1 - self.SL_PCT), 4)
        sl_limit = round(sl_stop * 0.995, 4)
        return (tp, sl_stop, sl_limit)


# ─── Module 2: Reversal Strategy (Fading the Open) ───────────────────────────

class ReversalStrategy:
    """
    Fade extreme opening moves using RSI as confirmation.

    Fires:
      - LONG    when price surged >2.5% AND RSI < 25 (oversold — snap-back)
      - INVERSE when price dropped >2.5% AND RSI > 75 (overbought — fade)
                buys the mapped inverse ETF if available

    Each signal fires at most once per symbol per day.
    """

    ALLOC_PCT: float = 0.02    # 2% of account
    TP_PCT:    float = 0.0075  # +0.75%
    SL_PCT:    float = 0.004   # -0.4%

    def __init__(self):
        self._open_price: dict = {}   # symbol -> open price
        self._triggered:  dict = {}   # symbol -> bool

    def set_open(self, symbol: str, open_price: float) -> None:
        """Register the open price for a symbol (called at 9:30)."""
        self._open_price[symbol] = open_price
        self._triggered[symbol]  = False

    def check_signal(
        self,
        symbol: str,
        current_price: float,
        rsi: float,
        inverse_map: dict,
    ) -> "Signal | None":
        """
        Evaluate reversal conditions for a symbol.

        Parameters
        ----------
        symbol        : Ticker to evaluate.
        current_price : Latest trade price.
        rsi           : Current RSI (from calculate_rsi).
        inverse_map   : INVERSE_ETF_MAP dict.

        Returns
        -------
        Signal or None
        """
        if self._triggered.get(symbol):
            return None

        open_price = self._open_price.get(symbol)
        if open_price is None or open_price <= 0:
            return None

        move_pct = abs(current_price - open_price) / open_price
        if move_pct <= 0.025:
            return None

        # ── RSI < 25 — price fell hard; expect bounce; go LONG ──────
        if rsi < 25:
            self._triggered[symbol] = True
            entry    = round(current_price, 4)
            tp       = round(entry * (1 + self.TP_PCT), 4)
            sl_stop  = round(entry * (1 - self.SL_PCT), 4)
            sl_limit = round(sl_stop * 0.996, 4)
            log.info(
                "REVERSAL LONG  %s  rsi=%.1f  entry=%.4f  tp=%.4f  sl=%.4f",
                symbol, rsi, entry, tp, sl_stop,
            )
            return Signal(
                strategy="REVERSAL",
                direction="LONG",
                symbol=symbol,
                entry_price=entry,
                tp_price=tp,
                sl_stop=sl_stop,
                sl_limit=sl_limit,
            )

        # ── RSI > 75 — price surged; fade via inverse ETF ────────────
        if rsi > 75 and symbol in inverse_map:
            self._triggered[symbol] = True
            inv_symbol = inverse_map[symbol]
            entry    = round(current_price, 4)  # caller reprices on inv symbol
            tp       = round(entry * (1 + self.TP_PCT), 4)
            sl_stop  = round(entry * (1 - self.SL_PCT), 4)
            sl_limit = round(sl_stop * 0.996, 4)
            log.info(
                "REVERSAL INVERSE  %s -> %s  rsi=%.1f  entry=%.4f  tp=%.4f  sl=%.4f",
                symbol, inv_symbol, rsi, entry, tp, sl_stop,
            )
            return Signal(
                strategy="REVERSAL",
                direction="INVERSE",
                symbol=inv_symbol,
                entry_price=entry,
                tp_price=tp,
                sl_stop=sl_stop,
                sl_limit=sl_limit,
            )

        return None

    def get_bracket(self, entry_price: float) -> tuple:
        tp       = round(entry_price * (1 + self.TP_PCT), 4)
        sl_stop  = round(entry_price * (1 - self.SL_PCT), 4)
        sl_limit = round(sl_stop * 0.996, 4)
        return (tp, sl_stop, sl_limit)


# ─── Module 3: Gap and Go ─────────────────────────────────────────────────────

class GapAndGoStrategy:
    """
    Gap and Go: buy continuation of a large opening gap if:
      - Gap >= 3% at open
      - Price broke above the ORB high
      - Gap was NOT filled during the ORB window (no print at or below prev_close)

    State per symbol:
      prev_close, open_price, orb_high, orb_low,
      gap_pct, gap_filled, established, triggered
    """

    ALLOC_PCT: float = 0.05   # 5% of account
    TP_PCT:    float = 0.10   # +10%
    SL_PCT:    float = 0.05   # -5%

    MIN_GAP_PCT: float = 0.03  # 3% gap threshold

    def __init__(self):
        self._prev_close:   dict = {}
        self._open_price:   dict = {}
        self._orb_high:     dict = {}
        self._orb_low:      dict = {}
        self._gap_pct:      dict = {}
        self._gap_filled:   dict = {}
        self._established:  dict = {}
        self._frozen:       dict = {}
        self._triggered:    dict = {}

    # ------------------------------------------------------------------

    def qualify(self, symbol: str, prev_close: float, open_price: float) -> None:
        """
        Register a symbol for Gap-and-Go if the gap is large enough.
        Called once at 9:30 AM.
        """
        if prev_close <= 0:
            return
        gap = (open_price - prev_close) / prev_close
        if abs(gap) < self.MIN_GAP_PCT:
            return

        self._prev_close[symbol]   = prev_close
        self._open_price[symbol]   = open_price
        self._orb_high[symbol]     = open_price
        self._orb_low[symbol]      = open_price
        self._gap_pct[symbol]      = gap
        self._gap_filled[symbol]   = False
        self._established[symbol]  = True
        self._frozen[symbol]       = False
        self._triggered[symbol]    = False
        log.debug("GAP&GO qualify  %s  prev_close=%.4f  open=%.4f  gap=%.2f%%",
                  symbol, prev_close, open_price, gap * 100)

    def update(self, symbol: str, price: float) -> None:
        """
        Track high/low and check for gap fill during 9:30–9:45.
        """
        if not self._established.get(symbol):
            return
        if self._frozen.get(symbol):
            return

        if price > self._orb_high[symbol]:
            self._orb_high[symbol] = price
        if price < self._orb_low[symbol]:
            self._orb_low[symbol] = price

        # Gap fill: price revisited prev_close level
        if price <= self._prev_close[symbol]:
            self._gap_filled[symbol] = True

    def freeze(self, symbol: str) -> None:
        """Lock boundaries at 9:45."""
        if self._established.get(symbol):
            self._frozen[symbol] = True
            log.debug(
                "GAP&GO frozen  %s  orb_high=%.4f  gap_filled=%s",
                symbol,
                self._orb_high.get(symbol, 0),
                self._gap_filled.get(symbol, False),
            )

    def check_signal(
        self,
        symbol: str,
        price: float,
        tick_size: float = 0.01,
    ) -> "Signal | None":
        """
        After freeze: fire LONG if price breaks above ORB high with gap intact.
        """
        if not self._frozen.get(symbol):
            return None
        if self._triggered.get(symbol):
            return None
        if self._gap_filled.get(symbol):
            return None

        orb_high = self._orb_high.get(symbol)
        if orb_high is None:
            return None

        if price > orb_high + tick_size:
            self._triggered[symbol] = True
            entry    = round(price, 4)
            tp       = round(entry * (1 + self.TP_PCT), 4)
            sl_stop  = round(entry * (1 - self.SL_PCT), 4)
            sl_limit = round(sl_stop * 0.995, 4)
            log.info(
                "GAP&GO LONG  %s  entry=%.4f  tp=%.4f  sl=%.4f  gap=%.2f%%",
                symbol, entry, tp, sl_stop, self._gap_pct.get(symbol, 0) * 100,
            )
            return Signal(
                strategy="GAP_AND_GO",
                direction="LONG",
                symbol=symbol,
                entry_price=entry,
                tp_price=tp,
                sl_stop=sl_stop,
                sl_limit=sl_limit,
            )

        return None

    def get_active_symbols(self) -> list:
        """Return the list of symbols registered for Gap-and-Go tracking."""
        return [s for s, est in self._established.items() if est]

    def get_bracket(self, entry_price: float) -> tuple:
        tp       = round(entry_price * (1 + self.TP_PCT), 4)
        sl_stop  = round(entry_price * (1 - self.SL_PCT), 4)
        sl_limit = round(sl_stop * 0.995, 4)
        return (tp, sl_stop, sl_limit)


# ─── Module 4: VWAP Mean Reversion ───────────────────────────────────────────

class VWAPReversionStrategy:
    """
    VWAP Mean Reversion: buy when price is 2 standard deviations below VWAP
    and target a return to the VWAP midline.

    Entry uses a limit 0.01% above current price for faster fill.
    Stop-loss is 0.6% below entry.
    Take-profit is the VWAP itself.
    """

    ALLOC_PCT: float = 0.03   # 3% of account
    SL_PCT:    float = 0.006  # -0.6%

    def __init__(self):
        self._vwap:        dict = {}   # symbol -> vwap
        self._upper:       dict = {}   # symbol -> upper band
        self._lower:       dict = {}   # symbol -> lower band
        self._triggered:   dict = {}   # symbol -> bool

    def set_vwap(
        self,
        symbol: str,
        vwap: float,
        upper: float,
        lower: float,
    ) -> None:
        """Register VWAP bands for a symbol (called at 9:45 after bars arrive)."""
        self._vwap[symbol]      = vwap
        self._upper[symbol]     = upper
        self._lower[symbol]     = lower
        self._triggered[symbol] = False
        log.debug("VWAP set  %s  vwap=%.4f  upper=%.4f  lower=%.4f",
                  symbol, vwap, upper, lower)

    def check_signal(self, symbol: str, price: float) -> "Signal | None":
        """
        Fire LONG if price is below the lower VWAP band.

        Entry is placed 0.01% above current price (limit order for fill speed).
        Take-profit = VWAP midline.
        Stop-loss   = 0.6% below entry.
        """
        if self._triggered.get(symbol):
            return None

        lower_band = self._lower.get(symbol)
        vwap       = self._vwap.get(symbol)

        if lower_band is None or vwap is None or lower_band <= 0:
            return None

        if price >= lower_band:
            return None

        self._triggered[symbol] = True
        entry    = round(price * 1.0001, 4)   # limit slightly above for fill
        tp       = round(vwap, 4)             # target the VWAP baseline
        sl_stop  = round(entry * (1 - self.SL_PCT), 4)
        sl_limit = round(sl_stop * 0.994, 4)  # 0.6% below stop for limit
        log.info(
            "VWAP REVERSION LONG  %s  price=%.4f  entry=%.4f  vwap=%.4f  sl=%.4f",
            symbol, price, entry, tp, sl_stop,
        )
        return Signal(
            strategy="VWAP_REVERSION",
            direction="LONG",
            symbol=symbol,
            entry_price=entry,
            tp_price=tp,
            sl_stop=sl_stop,
            sl_limit=sl_limit,
        )

    def get_bracket(self, entry_price: float) -> tuple:
        """
        Note: TP is VWAP-dependent and symbol-specific.
        This generic version uses a 0.6% SL only; TP is set per-signal.
        """
        sl_stop  = round(entry_price * (1 - self.SL_PCT), 4)
        sl_limit = round(sl_stop * 0.994, 4)
        tp       = round(entry_price * 1.006, 4)  # placeholder; overridden by set_vwap
        return (tp, sl_stop, sl_limit)
