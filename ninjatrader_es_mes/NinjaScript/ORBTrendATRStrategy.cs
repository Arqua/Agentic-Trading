#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

// ORBTrendATRStrategy.cs  (v2)
//
// Opening-Range Breakout, EMA trend filter, ATR volatility filter and
// ATR-scaled risk management for CME E-mini / Micro E-mini S&P 500 futures
// (ES / MES). Designed for the 5-minute chart of the front-month contract.
//
// v2 changes
// ----------
//  * RTH-only enforcement: an explicit time gate refuses to trade or manage
//    outside 09:30-16:00 ET even if the chart uses a 24/7 (ETH) session
//    template. Use an RTH session template anyway so the EMA/ATR filters
//    see only regular-session bars.
//  * Dual-instrument: the strategy loads BOTH ES and MES data (it derives
//    the sibling contract of whichever one the chart carries — e.g. on an
//    "MES 12-25" chart it adds "ES 12-25"). Signals are always computed on
//    the ES series (the deep, price-discovering contract); orders execute
//    on MES while account buying power is at or below BuyingPowerSwitchUsd
//    ($20,000 default) and on ES above it.
//  * Per-order fees: EsFeePerSideUsd / MesFeePerSideUsd hold the all-in
//    per-contract, per-side cost (broker commission + CME exchange &
//    clearing + NFA regulatory — defaults: ES $1.29+$1.40+$0.02 = $2.71,
//    MES $0.35+$0.37+$0.02 = $0.74, NinjaTrader free-plan pricing). Fees
//    are folded into position sizing, the daily loss limit, and a
//    viability guard that skips trades whose 1R gross profit would not
//    cover at least 10x the round-turn fee.
//  * Catastrophic stop cap: the protective stop is never placed farther
//    away than one tick above the price at which a full stop-out
//    (including fees) would consume BpStopCapPct (5%) of current buying
//    power. If the ATR stop is already tighter it stands; if even the
//    capped stop would be tighter than 2 ticks, the trade is skipped.
//
// Core rules (unchanged from v1)
// ------------------------------
//  1. Opening range: high/low of the first OrbMinutes minutes of RTH
//     (default 15 -> 09:30-09:45 ET), computed on the ES series.
//  2. Trend filter: EMA(TrendPeriod) on the ES series — longs only above,
//     shorts only below.
//  3. Volatility filter: ATR(AtrPeriod) within [MinAtrTicks, MaxAtrTicks].
//  4. Entry: one attempt per side per day once price closes through the
//     range boundary +/- BufferTicks, until MaxEntryTime (11:30 ET).
//  5. Initial stop: max(AtrStopMult * ATR, half the opening range), then
//     capped by the 5%-of-buying-power rule above.
//  6. Target: stop distance * RewardRiskRatio (2R). At +1R, ScaleOutPct
//     scales out, stop moves to breakeven, remainder trails by
//     AtrTrailMult * ATR.
//  7. Session control: no entries after MaxEntryTime; flatten at
//     FlattenTime (15:55 ET). No overnight exposure.
//  8. Sizing: contracts = floor(RiskPerTradeUsd / (stopPts * PointValue +
//     roundTurnFee)), capped at MaxContracts. PointValue comes from the
//     executing instrument ($50/pt ES, $5/pt MES).
//
// This file follows NinjaTrader 8 conventions but has not been compiled
// against the actual NinjaTrader assemblies in this environment — review
// the NinjaScript Editor's compiler output on import.

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
        [Range(0.1, 25)]
        [Display(Name = "Catastrophic stop cap (% of buying power)", GroupName = "3. Risk / Exit", Order = 5)]
        public double BpStopCapPct { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100000)]
        [Display(Name = "Risk per trade (USD)", GroupName = "4. Position Sizing", Order = 1)]
        public double RiskPerTradeUsd { get; set; }

        [NinjaScriptProperty]
        [Range(1, 50)]
        [Display(Name = "Max contracts", GroupName = "4. Position Sizing", Order = 2)]
        public int MaxContracts { get; set; }

        [NinjaScriptProperty]
        [Range(0, 1000000)]
        [Display(Name = "Trade MES at/below this buying power (USD)", GroupName = "4. Position Sizing", Order = 3)]
        public double BuyingPowerSwitchUsd { get; set; }

        [NinjaScriptProperty]
        [Range(0, 1000000)]
        [Display(Name = "Fallback buying power (USD, backtest)", GroupName = "4. Position Sizing", Order = 4)]
        public double FallbackBuyingPowerUsd { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "ES all-in fee per side (USD)", GroupName = "5. Fees", Order = 1)]
        public double EsFeePerSideUsd { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "MES all-in fee per side (USD)", GroupName = "5. Fees", Order = 2)]
        public double MesFeePerSideUsd { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100000)]
        [Display(Name = "Daily loss limit (USD, 0=off)", GroupName = "6. Session Guards", Order = 1)]
        public double DailyLossLimitUsd { get; set; }

        [NinjaScriptProperty]
        [Range(1, 10)]
        [Display(Name = "Max trades per day", GroupName = "6. Session Guards", Order = 2)]
        public int MaxTradesPerDay { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Max entry time (HHmm, ET)", GroupName = "6. Session Guards", Order = 3)]
        public int MaxEntryTimeHHmm { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Flatten time (HHmm, ET)", GroupName = "6. Session Guards", Order = 4)]
        public int FlattenTimeHHmm { get; set; }

        // ── Indicators / internal state ─────────────────────────────────

        private EMA trendEma;
        private ATR atr;

        private int esIdx = -1;    // BarsInProgress index of the ES series (signals)
        private int mesIdx = -1;   // BarsInProgress index of the MES series
        private int activeExecIdx = -1;  // series the open position lives on

        private double orbHigh;
        private double orbLow;
        private bool orbEstablished;
        private bool orbFrozen;
        private bool longTriggeredToday;
        private bool shortTriggeredToday;

        private DateTime currentSessionDate = DateTime.MinValue;
        private double sessionStartRealizedPnl;
        private double feesPaidToday;
        private int tradesToday;
        private bool halted;

        private double entryFillPrice;
        private bool scaledOut;

        // Captured when the entry order is submitted, consumed on fill.
        private double pendingStopDist;
        private int pendingExecIdx = -1;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Opening Range Breakout + EMA trend filter + ATR risk " +
                               "management. ES signals; executes MES below $20k buying " +
                               "power, ES above. Fee-aware sizing and a 5%-of-buying-" +
                               "power catastrophic stop cap.";
                Name = "ORBTrendATRStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.UniqueEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsFillLimitOnTouch = false;
                BarsRequiredToTrade = 30;
                IsInstantiatedOnEachOptimizationIteration = false;

                OrbMinutes = 15;
                BufferTicks = 2;
                TrendPeriod = 50;
                AtrPeriod = 14;
                MinAtrTicks = 12;
                MaxAtrTicks = 160;
                AtrStopMult = 1.5;
                AtrTrailMult = 1.25;
                RewardRiskRatio = 2.0;
                ScaleOutPct = 50;
                BpStopCapPct = 5.0;
                RiskPerTradeUsd = 500;
                MaxContracts = 5;
                BuyingPowerSwitchUsd = 20000;
                FallbackBuyingPowerUsd = 10000;
                // All-in per-side fees: broker commission (NinjaTrader free
                // plan) + CME exchange & clearing + NFA $0.02. Replace with
                // your broker's actual schedule.
                EsFeePerSideUsd = 2.71;   // 1.29 + 1.40 + 0.02
                MesFeePerSideUsd = 0.74;  // 0.35 + 0.37 + 0.02
                DailyLossLimitUsd = 1200;
                MaxTradesPerDay = 2;
                MaxEntryTimeHHmm = 1130;
                FlattenTimeHHmm = 1555;
            }
            else if (State == State.Configure)
            {
                // Load the sibling contract so both ES and MES data are
                // available: on an "ES 12-25" chart this adds "MES 12-25",
                // and vice versa. Signals always come from the ES series;
                // execution is routed by buying power.
                string root = Instrument != null ? Instrument.MasterInstrument.Name : "";
                string sibling = root == "ES" ? "MES" : "ES";
                string expiry = "";
                if (Instrument != null)
                {
                    string[] parts = Instrument.FullName.Split(' ');
                    if (parts.Length > 1)
                        expiry = " " + parts[1];
                }
                AddDataSeries(sibling + expiry, Data.BarsPeriodType.Minute, 5);
            }
            else if (State == State.DataLoaded)
            {
                for (int i = 0; i < BarsArray.Length; i++)
                {
                    string name = BarsArray[i].Instrument.MasterInstrument.Name;
                    if (name == "ES") esIdx = i;
                    else if (name == "MES") mesIdx = i;
                }

                if (esIdx < 0 || mesIdx < 0)
                {
                    Log("ORBTrendATRStrategy: needs both ES and MES series (apply to an " +
                        "ES or MES chart; the sibling contract loads automatically). " +
                        "Disabling.", NinjaTrader.Cbi.LogLevel.Error);
                    SetState(State.Finalized);
                    return;
                }

                trendEma = EMA(Closes[esIdx], TrendPeriod);
                atr = ATR(Closes[esIdx], AtrPeriod);
            }
        }

        protected override void OnBarUpdate()
        {
            // All decisions run off the ES (signal) series; the MES series
            // exists for order routing and shares the same 5-min timeline.
            if (BarsInProgress != esIdx)
                return;

            if (CurrentBars[esIdx] < BarsRequiredToTrade || CurrentBars[mesIdx] < 1)
                return;

            DateTime barTime = Times[esIdx][0];
            TimeSpan tod = barTime.TimeOfDay;
            TimeSpan sessionOpen = new TimeSpan(9, 30, 0);
            TimeSpan sessionClose = new TimeSpan(16, 0, 0);

            // ── Hard RTH gate: never trade or manage on an extended-hours
            //    bar, even on a 24/7 chart template ─────────────────────
            if (tod < sessionOpen || tod >= sessionClose)
                return;

            DateTime sessionDate = barTime.Date;
            if (sessionDate != currentSessionDate)
            {
                currentSessionDate = sessionDate;
                orbHigh = double.MinValue;
                orbLow = double.MaxValue;
                orbEstablished = false;
                orbFrozen = false;
                longTriggeredToday = false;
                shortTriggeredToday = false;
                tradesToday = 0;
                feesPaidToday = 0;
                halted = false;
                scaledOut = false;
                sessionStartRealizedPnl = SystemPerformance.AllTrades.TradesPerformance.NetProfit;
            }

            TimeSpan orbEnd = sessionOpen + TimeSpan.FromMinutes(OrbMinutes);
            TimeSpan maxEntry = HHmmToTimeSpan(MaxEntryTimeHHmm);
            TimeSpan flatten = HHmmToTimeSpan(FlattenTimeHHmm);

            // ── Daily loss limit (fee-inclusive) ─────────────────────────
            // NetProfit excludes the fees this strategy models (assumes no
            // commission template is set), so subtract feesPaidToday here.
            if (!halted && DailyLossLimitUsd > 0)
            {
                double dayPnl = SystemPerformance.AllTrades.TradesPerformance.NetProfit
                                 - sessionStartRealizedPnl
                                 - feesPaidToday
                                 + GetOpenPositionPnl();
                if (dayPnl <= -Math.Abs(DailyLossLimitUsd))
                {
                    halted = true;
                    Log(string.Format("Daily loss limit hit ({0:C} incl. fees); no further entries today.", dayPnl),
                        NinjaTrader.Cbi.LogLevel.Warning);
                    CancelAllWorkingOrders();
                }
            }

            // ── Flatten window ───────────────────────────────────────────
            if (tod >= flatten)
            {
                FlattenEverything("Session flatten time reached");
                return;
            }

            // ── Build the opening range (ES series) ──────────────────────
            if (tod >= sessionOpen && tod < orbEnd)
            {
                orbHigh = Math.Max(orbHigh, Highs[esIdx][0]);
                orbLow = Math.Min(orbLow, Lows[esIdx][0]);
                orbEstablished = true;
                return;
            }

            if (!orbEstablished)
                return;

            if (!orbFrozen && tod >= orbEnd)
            {
                orbFrozen = true;
                Log(string.Format("ORB frozen: high={0} low={1} range={2:0.00}pts",
                    orbHigh, orbLow, orbHigh - orbLow), NinjaTrader.Cbi.LogLevel.Information);
            }

            // ── Manage an existing position ──────────────────────────────
            if (activeExecIdx >= 0 && Positions[activeExecIdx].MarketPosition != MarketPosition.Flat)
            {
                ManageOpenPosition();
                return;
            }

            if (halted || tradesToday >= MaxTradesPerDay)
                return;
            if (tod >= maxEntry)
                return;

            double atrVal = atr[0];
            double atrTicks = atrVal / TickSize;
            if (atrTicks < MinAtrTicks || atrTicks > MaxAtrTicks)
                return;

            double price = Closes[esIdx][0];
            double emaVal = trendEma[0];
            double buffer = BufferTicks * TickSize;

            // ── Long breakout: trend-aligned only ───────────────────────
            if (!longTriggeredToday && price > emaVal && Highs[esIdx][0] >= orbHigh + buffer)
            {
                TrySubmitEntry(true, atrVal);
                return;
            }

            // ── Short breakdown: trend-aligned only ─────────────────────
            if (!shortTriggeredToday && price < emaVal && Lows[esIdx][0] <= orbLow - buffer)
            {
                TrySubmitEntry(false, atrVal);
            }
        }

        // ── Entry submission: routing + fee-aware sizing + 5% BP cap ─────

        private void TrySubmitEntry(bool isLong, double atrVal)
        {
            double bp = GetBuyingPower();

            // Route: MES while buying power is at/below the threshold.
            int execIdx = bp <= BuyingPowerSwitchUsd ? mesIdx : esIdx;
            double pv = BarsArray[execIdx].Instrument.MasterInstrument.PointValue;
            double feeSide = execIdx == mesIdx ? MesFeePerSideUsd : EsFeePerSideUsd;
            double rtFee = 2.0 * feeSide;

            double stopDist = Math.Max(AtrStopMult * atrVal, MinStopFloor());

            // Size from the risk budget net of round-turn fees.
            double perContractRisk = stopDist * pv + rtFee;
            if (perContractRisk <= 0)
                return;
            int qty = (int)Math.Floor(RiskPerTradeUsd / perContractRisk);
            qty = Math.Max(1, Math.Min(qty, MaxContracts));

            // Catastrophic cap: stop must sit one tick above the price at
            // which a full stop-out (incl. fees) consumes BpStopCapPct of
            // buying power.
            double maxLoss = (BpStopCapPct / 100.0) * bp;
            double capDist = (maxLoss - rtFee * qty) / (pv * qty) - TickSize;
            if (capDist < stopDist)
            {
                if (capDist < 2 * TickSize)
                {
                    Log(string.Format(
                        "Entry skipped: 5%% BP cap leaves no stop room (bp={0:C}, capDist={1:0.00}pts)",
                        bp, capDist), NinjaTrader.Cbi.LogLevel.Warning);
                    if (isLong) longTriggeredToday = true; else shortTriggeredToday = true;
                    return;
                }
                stopDist = capDist;
            }

            // Fee viability: 1R gross must cover >= 10x the round-turn fee.
            if (stopDist * pv < 10 * rtFee)
            {
                Log("Entry skipped: expected 1R profit does not clear 10x round-turn fees.",
                    NinjaTrader.Cbi.LogLevel.Warning);
                if (isLong) longTriggeredToday = true; else shortTriggeredToday = true;
                return;
            }

            pendingStopDist = stopDist;
            pendingExecIdx = execIdx;

            if (isLong)
            {
                longTriggeredToday = true;
                EnterLong(execIdx, qty, "ORB_Long");
            }
            else
            {
                shortTriggeredToday = true;
                EnterShort(execIdx, qty, "ORB_Short");
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
            if (!isEntry || pendingExecIdx < 0)
                return;

            Position pos = Positions[pendingExecIdx];
            if (pos.MarketPosition == MarketPosition.Flat)
                return;

            activeExecIdx = pendingExecIdx;
            entryFillPrice = price;
            scaledOut = false;
            tradesToday++;

            // Reserve the full round-turn fee (entry side + eventual exit
            // side, every contract) now, so the daily loss limit sees it
            // immediately. Exit legs must NOT add their side again.
            double feeSide = activeExecIdx == mesIdx ? MesFeePerSideUsd : EsFeePerSideUsd;
            feesPaidToday += 2.0 * feeSide * pos.Quantity;

            double stopDist = pendingStopDist;
            double target = stopDist * RewardRiskRatio;

            if (pos.MarketPosition == MarketPosition.Long)
            {
                SetStopLoss("ORB_Long", CalculationMode.Price, entryFillPrice - stopDist, false);
                SetProfitTarget("ORB_Long", CalculationMode.Price, entryFillPrice + target);
            }
            else
            {
                SetStopLoss("ORB_Short", CalculationMode.Price, entryFillPrice + stopDist, false);
                SetProfitTarget("ORB_Short", CalculationMode.Price, entryFillPrice - target);
            }
        }

        // ── Position management: scale-out at 1R + breakeven + trail ────

        private void ManageOpenPosition()
        {
            Position pos = Positions[activeExecIdx];
            double refPrice = Closes[esIdx][0];  // ES and MES track within a tick
            double atrVal = atr[0];
            double oneR = pendingStopDist;
            double trailDist = AtrTrailMult * atrVal;

            if (pos.MarketPosition == MarketPosition.Long)
            {
                double gain = refPrice - entryFillPrice;
                if (!scaledOut && gain >= oneR && ScaleOutPct > 0)
                {
                    int scaleQty = (int)Math.Round(pos.Quantity * (ScaleOutPct / 100.0));
                    if (scaleQty > 0 && scaleQty < pos.Quantity)
                    {
                        ExitLong(activeExecIdx, scaleQty, "ORB_ScaleOut", "ORB_Long");
                        scaledOut = true;
                        SetStopLoss("ORB_Long", CalculationMode.Price, entryFillPrice, false);
                    }
                }
                if (scaledOut)
                {
                    double newStop = refPrice - trailDist;
                    if (newStop > entryFillPrice)
                        SetStopLoss("ORB_Long", CalculationMode.Price, newStop, false);
                }
            }
            else if (pos.MarketPosition == MarketPosition.Short)
            {
                double gain = entryFillPrice - refPrice;
                if (!scaledOut && gain >= oneR && ScaleOutPct > 0)
                {
                    int scaleQty = (int)Math.Round(pos.Quantity * (ScaleOutPct / 100.0));
                    if (scaleQty > 0 && scaleQty < pos.Quantity)
                    {
                        ExitShort(activeExecIdx, scaleQty, "ORB_ScaleOut", "ORB_Short");
                        scaledOut = true;
                        SetStopLoss("ORB_Short", CalculationMode.Price, entryFillPrice, false);
                    }
                }
                if (scaledOut)
                {
                    double newStop = refPrice + trailDist;
                    if (newStop < entryFillPrice)
                        SetStopLoss("ORB_Short", CalculationMode.Price, newStop, false);
                }
            }
        }

        // ── Helpers ──────────────────────────────────────────────────────

        /// <summary>
        /// Live/sim: real-time buying power from the account. Historical
        /// backtest (Strategy Analyzer): the account object reports no
        /// meaningful buying power, so fall back to FallbackBuyingPowerUsd
        /// plus realized strategy P&L — which also lets the Analyzer
        /// exercise the MES->ES switchover as simulated equity grows.
        /// </summary>
        private double GetBuyingPower()
        {
            if (State == State.Realtime && Account != null)
            {
                try
                {
                    double bp = Account.Get(AccountItem.BuyingPower, Currency.UsDollar);
                    if (bp > 0)
                        return bp;
                }
                catch (Exception) { /* fall through to the simulated value */ }
            }
            return FallbackBuyingPowerUsd
                    + SystemPerformance.AllTrades.TradesPerformance.NetProfit;
        }

        /// <summary>
        /// Never let the ATR-derived stop be tighter than half the opening
        /// range — the range itself is natural support/resistance and a
        /// too-tight stop gets clipped by routine noise.
        /// </summary>
        private double MinStopFloor()
        {
            double rangeWidth = orbHigh - orbLow;
            return Math.Max(rangeWidth * 0.5, 2 * TickSize);
        }

        private double GetOpenPositionPnl()
        {
            if (activeExecIdx < 0)
                return 0.0;
            Position pos = Positions[activeExecIdx];
            if (pos.MarketPosition == MarketPosition.Flat || CurrentBars[activeExecIdx] < 1)
                return 0.0;
            return pos.GetUnrealizedProfitLoss(PerformanceUnit.Currency, Closes[activeExecIdx][0]);
        }

        private void FlattenEverything(string reason)
        {
            if (activeExecIdx < 0)
                return;
            Position pos = Positions[activeExecIdx];
            if (pos.MarketPosition == MarketPosition.Flat)
                return;
            Log("Flatten: " + reason, NinjaTrader.Cbi.LogLevel.Information);
            if (pos.MarketPosition == MarketPosition.Long)
                ExitLong(activeExecIdx, pos.Quantity, "Flatten", "ORB_Long");
            else
                ExitShort(activeExecIdx, pos.Quantity, "Flatten", "ORB_Short");
        }

        private void CancelAllWorkingOrders()
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
