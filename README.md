# Agentic-Trading

Claude AI-powered trading bot for Robinhood — executes a momentum + large-mover intraday strategy on an agentic-enabled account.

---

## Strategy overview

### Entry window
New positions are only opened during the **first 15 minutes** after market open (9:30–9:45 AM ET). The bot still monitors existing open orders after that window closes.

### Momentum trades
| Parameter | Value |
|-----------|-------|
| Trigger | Stock up ≥ 1% vs. previous close |
| Fee guard | Skip if bid-ask spread > 0.25% (spread would eat profit) |
| Position size | 10% of available buying power per trade |
| Max concurrent | 3 positions |
| Exit | Limit sell at **entry + 0.5%** (GTC) |

### Large-mover trades
| Parameter | Value |
|-----------|-------|
| Trigger | Stock up ≥ 3% vs. previous close |
| Position size | **5% of start-of-day account value** |
| Max concurrent | 1 position per day |
| Take-profit | Limit sell at **entry + 10%** (GTC) |
| Stop-loss | Stop-limit sell at **entry − 5%** (GTC) |

> **Fractional-share note:** Robinhood does not support stop orders on fractional-share positions. If the 5% allocation results in a fractional quantity, the bot places the limit sell (take-profit) but logs a warning about the stop-loss. Monitor those positions in the Robinhood app or set a price alert.

### Risk management
- Portfolio value is checked **every 15 minutes**
- If the day's total loss reaches **≥ 15% of the start-of-day value**, no new entries are opened for the rest of the session
- Existing limit and stop-limit orders remain active even after the halt

### Scan universe
Each cycle fetches:
- Robinhood's built-in top-movers list
- S&P 500 up-movers

---

## Setup

### 1. Prerequisites
- Python 3.10+
- A Robinhood account with **Agentic trading enabled** (account `••••0467`)

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure credentials
```bash
cp .env.example .env
# Edit .env with your Robinhood username, password, and account number
```

The `.env` file is git-ignored. **Never commit credentials.**

### 4. First login (MFA)
On the first run, Robinhood may prompt for an MFA code. After that the session is cached locally (`~/.tokens/robinhood.pickle` by default) and subsequent runs log in silently.

### 5. Run the bot
```bash
python trading_bot.py
```

The bot runs in a continuous loop (15-minute intervals). Use a process manager like `tmux`, `screen`, or `systemd` to keep it alive:

```bash
# Example with tmux
tmux new-session -d -s trading 'python trading_bot.py'
tmux attach -t trading
```

---

## Tuning parameters

All strategy parameters are constants at the top of `trading_bot.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `TRADING_WINDOW_MINUTES` | 15 | Minutes after open to enter new positions |
| `UPDATE_INTERVAL_SECONDS` | 900 | Loop frequency (15 min) |
| `MOMENTUM_GAIN_THRESHOLD` | 0.01 | Min gain to consider a momentum entry |
| `MOMENTUM_SELL_TARGET` | 0.005 | Limit sell at entry + this % |
| `MAX_SPREAD_PCT` | 0.0025 | Max allowed bid-ask spread (fee guard) |
| `MOMENTUM_ALLOC_PCT` | 0.10 | Fraction of buying power per momentum trade |
| `MAX_MOMENTUM_POSITIONS` | 3 | Max concurrent momentum trades |
| `LARGE_MOVER_THRESHOLD` | 0.03 | Min gain to classify as large mover |
| `LARGE_MOVER_ALLOC_PCT` | 0.05 | Fraction of account value per large-mover trade |
| `LARGE_MOVER_TAKE_PROFIT` | 0.10 | Take-profit target (+10%) |
| `LARGE_MOVER_STOP_PCT` | 0.05 | Stop-loss trigger (−5%) |
| `DAILY_LOSS_LIMIT_PCT` | 0.15 | Halt entries if daily drawdown ≥ this |

---

## Fee considerations

Robinhood charges no commission on stock trades. The only costs are:
- **Regulatory fees** (SEC + FINRA): fractions of a cent per trade — negligible
- **Bid-ask spread**: the real cost. The `MAX_SPREAD_PCT = 0.25%` guard ensures the spread does not consume the 0.5% momentum profit target

With a $100 account, the 0.5% momentum target yields ~$0.05–0.10 per trade after spread. Large-mover trades (10% target) are more forgiving.

---

## Logs

A rolling log is written to `trading_bot.log` alongside console output. Each cycle logs the portfolio value, scan results, and every order placed.
