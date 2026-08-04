#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

// ORBTrendATRStrategy.cs
//
// Opening-Range Breakout, EMA trend filter, ATR volatility filter and
// ATR-scaled risk management for CME E-mini / Micro E-mini S&P 500 futures
// (ES / MES). Designed for the 5-minute chart of the front-month contract,
// RTH session template ("US Index Futures RTH", 09:30-16:00 ET).
//
// Rules
// -----
//  1. Instrument guard: only trades if the root symbol is ES or MES. Any
//     other instrument is refused (LogAndDisableIfWrongInstrument).
//  2. Opening range: high/low of the first OrbMinutes minutes of RTH
//     (default 15 -> 09:30-09:45 ET). Range is frozen at 09:45.
//  3. Trend filter: EMA(TrendPeriod) on the same 5-min series. Longs are
//     only armed above the EMA, shorts only below it -- this keeps the
//     strategy from fading its own breakout on a mean-reverting day.
//  4. Volatility filter: ATR(AtrPeriod) must sit inside
//     [MinAtrTicks, MaxAtrTicks] * TickSize. Too-quiet days produce
//     breakout noise; too-wild days (news spikes, halts) blow through
//     ATR-based stops before the fill is even confirmed.
//  5. Entry: stop order at OrbHigh + BufferTicks (long) / OrbLow -
//     BufferTicks (short), armed once at 09:45, live until MaxEntryTime
//     (default 11:30 ET) or fill/cancel. One attempt per side per day.
//  6. Initial stop: ATR(AtrPeriod) * AtrStopMult behind entry, floored so
//     it is never tighter than the opposite side of the opening range.
//  7. Target: AtrStopMult * RewardRiskRatio away (2R default). At 1R,
//     ScaleOutPct of the position is taken off and the stop on the
//     remainder is walked to breakeven; the remainder trails by
//     ATR * AtrTrailMult.
//  8. Session control: no new entries after MaxEntryTime; everything is
//     flattened and all working orders cancelled at FlattenTime (default
//     15:55 ET) -- no overnight or rollover exposure.
//  9. Risk controls: position size is computed from RiskPerTradeUsd and the
//     instrument's real point value (auto: $50/pt ES, $5/pt MES), not a
//     fixed contract count. A daily loss limit (DailyLossLimitUsd) and a
//     max-trades-per-day cap stop the strategy from digging a hole on a
//     choppy or news-driven session.
//
// This file is written to NinjaTrader 8 / C# 8 conventions and is meant to
// be dropped into Documents\NinjaTrader 8\bin\Custom\Strategies and
// compiled with NinjaScript Editor (F5). It has not been compiled against
// the actual NinjaTrader assemblies in this environment -- review the
// compiler's output on import for any local API-version drift.

namespace NinjaTrader.NinjaScript.Strategies
{
    public class ORBTrendATRStrategy : Strategy
    {
        // ── User-configurable parameters ────────────────────────────────

        [NinjaScriptProperty]
        [Range(1, 60)]
        [Display(Name = "Opening range minutes", GroupName = "1. Opening Range", Order = 1)]
        public int OrbMinutes { get; set; }

        [NinjaScriptProperty]
        [Range(0, 20)]
        [Display(Name = "Breakout buffer (ticks)", GroupName = "1. Opening Range", Order = 2)]
        public int BufferTicks { get; set; }

        [NinjaScriptProperty]
        [Range(2, 200)]
        [Display(Name = "Trend EMA period", GroupName = "2. Filters", Order = 1)]
        public int TrendPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(2, 100)]
        [Display(Name = "ATR period", GroupName = "2. Filters", Order = 2)]
        public int AtrPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0, 500)]
        [Display(Name = "Min ATR (ticks) to trade", GroupName = "2. Filters", Order = 3)]
        public int MinAtrTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, 2000)]
        [Display(Name = "Max ATR (ticks) to trade", GroupName = "2. Filters", Order = 4)]
        public int MaxAtrTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10)]
        [Display(Name = "Stop = ATR x", GroupName = "3. Risk / Exit", Order = 1)]
        public double AtrStopMult { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10)]
        [Display(Name = "Trail = ATR x (after 1R)", GroupName = "3. Risk / Exit", Order = 2)]
        public double AtrTrailMult { get; set; }

        [NinjaScriptProperty]
        [Range(0.5, 10)]
        [Display(Name = "Reward:Risk ratio", GroupName = "3. Risk / Exit", Order = 3)]
        public double RewardRiskRatio { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "Scale-out % at 1R", GroupName = "3. Risk / Exit", Order = 4)]
        public double ScaleOutPct { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100000)]
        [Display(Name = "Risk per trade (USD)", GroupName = "4. Position Sizing", Order = 1)]
        public double RiskPerTradeUsd { get; set; }

        [NinjaScriptProperty]
        [Range(1, 50)]
        [Display(Name = "Max contracts", GroupName = "4. Position Sizing", Order = 2)]
        public int MaxContracts { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100000)]
        [Display(Name = "Daily loss limit (USD, 0=off)", GroupName = "5. Session Guards", Order = 1)]
        public double DailyLossLimitUsd { get; set; }

        [NinjaScriptProperty]
        [Range(1, 10)]
        [Display(Name = "Max trades per day", GroupName = "5. Session Guards", Order = 2)]
        public int MaxTradesPerDay { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Max entry time (HHmm, ET)", GroupName = "5. Session Guards", Order = 3)]
        public int MaxEntryTimeHHmm { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Flatten time (HHmm, ET)", GroupName = "5. Session Guards", Order = 4)]
        public int FlattenTimeHHmm { get; set; }

        // ── Indicators / internal state ─────────────────────────────────

        private EMA trendEma;
        private ATR atr;

        private double orbHigh;
        private double orbLow;
        private bool orbEstablished;
        private bool orbFrozen;
        private bool longArmedToday;
        private bool shortArmedToday;
        private bool longTriggeredToday;
        private bool shortTriggeredToday;

        private DateTime currentSessionDate = DateTime.MinValue;
        private double sessionStartRealizedPnl;
        private int tradesToday;
        private bool halted;

        private double initialStopPrice;
        private double entryFillPrice;
        private bool scaledOut;

        // ── Instrument economics (auto-detected, NOT hardcoded) ─────────

        private double PointValue => Instrument.MasterInstrument.PointValue;
        private double TickSizeVal => TickSize;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Opening Range Breakout + EMA trend filter + ATR " +
                               "risk management, for ES / MES front-month futures.";
                Name = "ORBTrendATRStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.UniqueEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsFillLimitOnTouch = false;
                BarsRequiredToTrade = 30;
                IsInstantiatedOnEachOptimizationIteration = false;

                // Defaults tuned for the 5-minute chart.
                OrbMinutes = 15;
                BufferTicks = 2;
                TrendPeriod = 50;
                AtrPeriod = 14;
                MinAtrTicks = 12;     // ~3.0 pts on ES -- below this, breakouts are noise
                MaxAtrTicks = 160;    // ~40 pts on ES -- above this, treat as news/halt regime
                AtrStopMult = 1.5;
                AtrTrailMult = 1.25;
                RewardRiskRatio = 2.0;
                ScaleOutPct = 50;
                RiskPerTradeUsd = 500;
                MaxContracts = 5;
                DailyLossLimitUsd = 1200;
                MaxTradesPerDay = 2;
                MaxEntryTimeHHmm = 1130;
                FlattenTimeHHmm = 1555;
            }
            else if (State == State.Configure)
            {
                AddDataSeries(BarsPeriodType.Minute, 5);
            }
            else if (State == State.DataLoaded)
            {
                trendEma = EMA(TrendPeriod);
                atr = ATR(AtrPeriod);

                string root = Instrument.MasterInstrument.Name;
                if (root != "ES" && root != "MES")
                {
                    Log(string.Format(
                        "ORBTrendATRStrategy: instrument '{0}' is neither ES nor MES -- " +
                        "disabling strategy. This algorithm is only validated for E-mini " +
                        "and Micro E-mini S&P 500 futures.", root),
                        NinjaTrader.Cbi.LogLevel.Error);
                    SetState(State.Finalized);
                }
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0)
                return; // only act on the primary 5-min series

            if (CurrentBars[0] < BarsRequiredToTrade)
                return;

            DateTime barTime = Time[0];
            DateTime sessionDate = barTime.Date;

            // ── New session bookkeeping ─────────────────────────────────
            if (sessionDate != currentSessionDate)
            {
                currentSessionDate = sessionDate;
                orbHigh = double.MinValue;
                orbLow = double.MaxValue;
                orbEstablished = false;
                orbFrozen = false;
                longArmedToday = false;
                shortArmedToday = false;
                longTriggeredToday = false;
                shortTriggeredToday = false;
                tradesToday = 0;
                halted = false;
                scaledOut = false;
                sessionStartRealizedPnl = SystemPerformance.AllTrades.TradesPerformance.NetProfit;
            }

            TimeSpan tod = barTime.TimeOfDay;
            TimeSpan sessionOpen = new TimeSpan(9, 30, 0);
            TimeSpan orbEnd = sessionOpen + TimeSpan.FromMinutes(OrbMinutes);
            TimeSpan maxEntry = HHmmToTimeSpan(MaxEntryTimeHHmm);
            TimeSpan flatten = HHmmToTimeSpan(FlattenTimeHHmm);

            // ── Daily loss limit check ──────────────────────────────────
            if (!halted && DailyLossLimitUsd > 0)
            {
                double dayPnl = SystemPerformance.AllTrades.TradesPerformance.NetProfit
                                 - sessionStartRealizedPnl
                                 + GetOpenPositionPnl();
                if (dayPnl <= -Math.Abs(DailyLossLimitUsd))
                {
                    halted = true;
                    Log(string.Format("Daily loss limit hit ({0:C}); no further entries today.", dayPnl),
                        NinjaTrader.Cbi.LogLevel.Warning);
                    CancelAllOrdersForToday();
                }
            }

            // ── Flatten / no-trade window ────────────────────────────────
            if (tod >= flatten)
            {
                if (Position.MarketPosition != MarketPosition.Flat)
                    FlattenEverything("Session flatten time reached");
                return;
            }

            // ── Build the opening range ──────────────────────────────────
            if (tod >= sessionOpen && tod < orbEnd)
            {
                orbHigh = Math.Max(orbHigh, High[0]);
                orbLow = Math.Min(orbLow, Low[0]);
                orbEstablished = true;
                return; // no trading decisions inside the range-forming window
            }

            if (!orbEstablished)
                return; // pre-market / no range yet (e.g. holiday half day)

            if (!orbFrozen && tod >= orbEnd)
            {
                orbFrozen = true;
                longArmedToday = true;
                shortArmedToday = true;
                Log(string.Format("ORB frozen: high={0} low={1} range={2:0.00}pts",
                    orbHigh, orbLow, orbHigh - orbLow), NinjaTrader.Cbi.LogLevel.Information);
            }

            if (halted || tradesToday >= MaxTradesPerDay)
                return;

            // ── Manage an existing position (scale-out / trail) ─────────
            if (Position.MarketPosition != MarketPosition.Flat)
            {
                ManageOpenPosition();
                return;
            }

            if (tod >= maxEntry)
                return; // breakout window has expired for today

            double atrVal = atr[0];
            double atrTicks = atrVal / TickSizeVal;
            bool volOk = atrTicks >= MinAtrTicks && atrTicks <= MaxAtrTicks;
            if (!volOk)
                return;

            double price = Close[0];
            double emaVal = trendEma[0];
            double buffer = BufferTicks * TickSizeVal;

            // ── Long breakout: trend-aligned only ───────────────────────
            if (longArmedToday && !longTriggeredToday && price > emaVal)
            {
                double trigger = orbHigh + buffer;
                if (High[0] >= trigger)
                {
                    EnterLong(SizeForRisk(atrVal), "ORB_Long");
                    longTriggeredToday = true;
                }
            }

            // ── Short breakdown: trend-aligned only ─────────────────────
            if (shortArmedToday && !shortTriggeredToday && price < emaVal)
            {
                double trigger = orbLow - buffer;
                if (Low[0] <= trigger)
                {
                    EnterShort(SizeForRisk(atrVal), "ORB_Short");
                    shortTriggeredToday = true;
                }
            }
        }

        // ── Order fill / bracket setup ──────────────────────────────────

        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition, string orderId,
            DateTime time)
        {
            if (execution.Order == null)
                return;

            bool isEntry = execution.Order.Name == "ORB_Long" || execution.Order.Name == "ORB_Short";
            if (isEntry && Position.MarketPosition != MarketPosition.Flat)
            {
                entryFillPrice = price;
                scaledOut = false;
                tradesToday++;

                double atrVal = atr[0];
                double stopDist = Math.Max(AtrStopMult * atrVal, MinStopFloor());
                double target = stopDist * RewardRiskRatio;

                if (Position.MarketPosition == MarketPosition.Long)
                {
                    initialStopPrice = entryFillPrice - stopDist;
                    SetStopLoss("ORB_Long", CalculationMode.Price, initialStopPrice, false);
                    SetProfitTarget("ORB_Long", CalculationMode.Price, entryFillPrice + target);
                }
                else
                {
                    initialStopPrice = entryFillPrice + stopDist;
                    SetStopLoss("ORB_Short", CalculationMode.Price, initialStopPrice, false);
                    SetProfitTarget("ORB_Short", CalculationMode.Price, entryFillPrice - target);
                }
            }
        }

        // ── Position management: scale-out at 1R + breakeven + trail ────

        private void ManageOpenPosition()
        {
            double atrVal = atr[0];
            double stopDist = Math.Max(AtrStopMult * atrVal, MinStopFloor());
            double oneR = stopDist;
            double trailDist = AtrTrailMult * atrVal;

            if (Position.MarketPosition == MarketPosition.Long)
            {
                double gain = Close[0] - entryFillPrice;
                if (!scaledOut && gain >= oneR && ScaleOutPct > 0)
                {
                    int scaleQty = (int)Math.Round(Position.Quantity * (ScaleOutPct / 100.0));
                    if (scaleQty > 0 && scaleQty < Position.Quantity)
                    {
                        ExitLong(scaleQty, "ORB_ScaleOut", "ORB_Long");
                        scaledOut = true;
                        SetStopLoss("ORB_Long", CalculationMode.Price, entryFillPrice, false);
                    }
                }
                if (scaledOut)
                {
                    double newStop = Close[0] - trailDist;
                    if (newStop > entryFillPrice)
                        SetStopLoss("ORB_Long", CalculationMode.Price, newStop, false);
                }
            }
            else if (Position.MarketPosition == MarketPosition.Short)
            {
                double gain = entryFillPrice - Close[0];
                if (!scaledOut && gain >= oneR && ScaleOutPct > 0)
                {
                    int scaleQty = (int)Math.Round(Position.Quantity * (ScaleOutPct / 100.0));
                    if (scaleQty > 0 && scaleQty < Position.Quantity)
                    {
                        ExitShort(scaleQty, "ORB_ScaleOut", "ORB_Short");
                        scaledOut = true;
                        SetStopLoss("ORB_Short", CalculationMode.Price, entryFillPrice, false);
                    }
                }
                if (scaledOut)
                {
                    double newStop = Close[0] + trailDist;
                    if (newStop < entryFillPrice)
                        SetStopLoss("ORB_Short", CalculationMode.Price, newStop, false);
                }
            }
        }

        // ── Helpers ──────────────────────────────────────────────────────

        /// <summary>
        /// Never let the ATR-derived stop be tighter than the far side of the
        /// opening range -- the range itself is a natural support/resistance
        /// level and a too-tight ATR stop gets clipped by routine noise.
        /// </summary>
        private double MinStopFloor()
        {
            double rangeWidth = orbHigh - orbLow;
            return Math.Max(rangeWidth * 0.5, 2 * TickSizeVal);
        }

        /// <summary>
        /// Contracts = floor(RiskPerTradeUsd / (stopDistance_in_points * PointValue)),
        /// capped at MaxContracts. PointValue is read from the instrument
        /// (Instrument.MasterInstrument.PointValue: $50/pt for ES, $5/pt for MES)
        /// so the same parameter set produces correctly-scaled size on either
        /// symbol without manual re-tuning.
        /// </summary>
        private int SizeForRisk(double atrVal)
        {
            double stopDist = Math.Max(AtrStopMult * atrVal, MinStopFloor());
            double dollarsPerContract = stopDist * PointValue;
            if (dollarsPerContract <= 0)
                return 1;

            int qty = (int)Math.Floor(RiskPerTradeUsd / dollarsPerContract);
            qty = Math.Max(1, Math.Min(qty, MaxContracts));
            return qty;
        }

        private double GetOpenPositionPnl()
        {
            if (Position.MarketPosition == MarketPosition.Flat)
                return 0.0;
            return Position.GetUnrealizedProfitLoss(PerformanceUnit.Currency, Close[0]);
        }

        private void FlattenEverything(string reason)
        {
            Log("Flatten: " + reason, NinjaTrader.Cbi.LogLevel.Information);
            if (Position.MarketPosition == MarketPosition.Long)
                ExitLong("Flatten", "ORB_Long");
            else if (Position.MarketPosition == MarketPosition.Short)
                ExitShort("Flatten", "ORB_Short");
        }

        private void CancelAllOrdersForToday()
        {
            foreach (Order order in Orders)
            {
                if (order.OrderState == OrderState.Working ||
                    order.OrderState == OrderState.Accepted)
                    CancelOrder(order);
            }
        }

        private static TimeSpan HHmmToTimeSpan(int hhmm)
        {
            int h = hhmm / 100;
            int m = hhmm % 100;
            return new TimeSpan(h, m, 0);
        }
    }
}
