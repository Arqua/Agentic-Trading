"""
watchlist.py — 100-symbol liquid equity/ETF universe for the trading bot.

Groups:
  mega-cap tech        (8)
  large-cap tech      (14)
  financials          (12)
  healthcare          (12)
  energy               (5)
  consumer             (8)
  industrials          (6)
  high-beta/growth     (9)
  broad ETFs           (5)
  sector ETFs          (8)
  commodity ETFs       (4)
  bond ETFs            (2)
  inverse ETFs         (7)
  ─────────────────────────
  Total               100
"""

# ── Mega-cap tech (8) ────────────────────────────────────────────────────────
MEGA_CAP_TECH = [
    "AAPL",   # Apple
    "MSFT",   # Microsoft
    "NVDA",   # NVIDIA
    "GOOGL",  # Alphabet Class A
    "AMZN",   # Amazon
    "META",   # Meta Platforms
    "TSLA",   # Tesla
    "AVGO",   # Broadcom
]

# ── Large-cap tech (14) ──────────────────────────────────────────────────────
LARGE_CAP_TECH = [
    "AMD",    # Advanced Micro Devices
    "INTC",   # Intel
    "QCOM",   # Qualcomm
    "TXN",    # Texas Instruments
    "MU",     # Micron Technology
    "AMAT",   # Applied Materials
    "LRCX",   # Lam Research
    "KLAC",   # KLA Corporation
    "ADBE",   # Adobe
    "CRM",    # Salesforce
    "NOW",    # ServiceNow
    "SNOW",   # Snowflake
    "PANW",   # Palo Alto Networks
    "CRWD",   # CrowdStrike
]

# ── Financials (12) ──────────────────────────────────────────────────────────
FINANCIALS = [
    "JPM",    # JPMorgan Chase
    "BAC",    # Bank of America
    "WFC",    # Wells Fargo
    "GS",     # Goldman Sachs
    "MS",     # Morgan Stanley
    "BLK",    # BlackRock
    "C",      # Citigroup
    "AXP",    # American Express
    "V",      # Visa
    "MA",     # Mastercard
    "SCHW",   # Charles Schwab
    "COF",    # Capital One
]

# ── Healthcare (12) ──────────────────────────────────────────────────────────
HEALTHCARE = [
    "UNH",    # UnitedHealth Group
    "JNJ",    # Johnson & Johnson
    "LLY",    # Eli Lilly
    "PFE",    # Pfizer
    "ABBV",   # AbbVie
    "MRK",    # Merck
    "TMO",    # Thermo Fisher Scientific
    "ABT",    # Abbott Laboratories
    "DHR",    # Danaher
    "ISRG",   # Intuitive Surgical
    "VRTX",   # Vertex Pharmaceuticals
    "REGN",   # Regeneron
]

# ── Energy (5) ───────────────────────────────────────────────────────────────
ENERGY = [
    "XOM",    # ExxonMobil
    "CVX",    # Chevron
    "COP",    # ConocoPhillips
    "SLB",    # SLB (Schlumberger)
    "EOG",    # EOG Resources
]

# ── Consumer (8) ─────────────────────────────────────────────────────────────
# AMZN is already in MEGA_CAP_TECH; it is included here for group documentation
# but the dedup in _build_watchlist() ensures it is only counted once.
CONSUMER = [
    "WMT",    # Walmart
    "COST",   # Costco
    "HD",     # Home Depot
    "MCD",    # McDonald's
    "NKE",    # Nike
    "SBUX",   # Starbucks
    "TGT",    # Target
    "LOW",    # Lowe's
]

# ── Industrials (6) ──────────────────────────────────────────────────────────
INDUSTRIALS = [
    "CAT",    # Caterpillar
    "DE",     # Deere & Company
    "HON",    # Honeywell
    "UPS",    # United Parcel Service
    "BA",     # Boeing
    "GE",     # GE Aerospace
]

# ── High-beta / growth (9) ───────────────────────────────────────────────────
HIGH_BETA_GROWTH = [
    "SHOP",   # Shopify
    "MELI",   # MercadoLibre
    "DKNG",   # DraftKings
    "RBLX",   # Roblox
    "COIN",   # Coinbase
    "PLTR",   # Palantir
    "RIVN",   # Rivian
    "LCID",   # Lucid Group
    "SOFI",   # SoFi Technologies
]

# ── Broad ETFs (5) ───────────────────────────────────────────────────────────
BROAD_ETFS = [
    "SPY",    # S&P 500
    "QQQ",    # Nasdaq-100
    "IWM",    # Russell 2000
    "DIA",    # Dow Jones
    "VTI",    # Total US Market
]

# ── Sector ETFs (8) ──────────────────────────────────────────────────────────
SECTOR_ETFS = [
    "XLF",    # Financials
    "XLK",    # Technology
    "XLE",    # Energy
    "XLV",    # Healthcare
    "XLI",    # Industrials
    "XLY",    # Consumer Discretionary
    "XLP",    # Consumer Staples
    "XLU",    # Utilities
]

# ── Commodity ETFs (4) ───────────────────────────────────────────────────────
COMMODITY_ETFS = [
    "GLD",    # Gold
    "SLV",    # Silver
    "USO",    # Crude Oil
    "UNG",    # Natural Gas
]

# ── Bond ETFs (2) ────────────────────────────────────────────────────────────
BOND_ETFS = [
    "TLT",    # 20+ Year Treasury
    "HYG",    # High-Yield Corporate Bond
]

# ── Inverse ETFs (7) ─────────────────────────────────────────────────────────
INVERSE_ETFS = [
    "SPXS",   # 3x inverse S&P 500
    "SQQQ",   # 3x inverse Nasdaq-100
    "TZA",    # 3x inverse Russell 2000
    "SDOW",   # 3x inverse Dow Jones
    "FAZ",    # 3x inverse Financials
    "SOXS",   # 3x inverse Semiconductors
    "ERY",    # 2x inverse Energy
]

# ─── Master watchlist ────────────────────────────────────────────────────────
# Ordered, deduplicated list preserving group order.
# Group counts (unique contributions):
#   mega-cap tech  8  + large-cap tech  14 + financials 12 + healthcare 12
#   + energy 5 + consumer 8 + industrials 6 + high-beta/growth 9
#   + broad ETFs 5 + sector ETFs 8 + commodity ETFs 4 + bond ETFs 2
#   + inverse ETFs 7 = 100

def _build_watchlist() -> list:
    seen: set = set()
    result: list = []
    for sym in (
        MEGA_CAP_TECH
        + LARGE_CAP_TECH
        + FINANCIALS
        + HEALTHCARE
        + ENERGY
        + CONSUMER
        + INDUSTRIALS
        + HIGH_BETA_GROWTH
        + BROAD_ETFS
        + SECTOR_ETFS
        + COMMODITY_ETFS
        + BOND_ETFS
        + INVERSE_ETFS
    ):
        if sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result


WATCHLIST: list = _build_watchlist()

assert len(WATCHLIST) == 100, (
    f"Watchlist length is {len(WATCHLIST)}, expected 100. "
    f"Symbols: {WATCHLIST}"
)

# ─── Inverse ETF mappings ────────────────────────────────────────────────────

# Maps an underlying ticker to the inverse ETF to buy when it breaks down.
INVERSE_ETF_MAP: dict = {
    "SPY":  "SPXS",
    "QQQ":  "SQQQ",
    "IWM":  "TZA",
    "DIA":  "SDOW",
    "VTI":  "SPXS",
    "XLF":  "FAZ",
    "XLK":  "SOXS",
    "XLE":  "ERY",
    "XLV":  "RXD",
}

# Symbols that ARE inverse ETFs — never buy an inverse of an inverse.
INVERSE_ETF_SYMBOLS: set = set(INVERSE_ETFS) | {"RXD"}
