"""
gui_app.py — MAI Trading Bot desktop GUI
Mitchell Attempted Investing

Entry point:  python gui_app.py
"""

import asyncio
import os
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import pytz
import customtkinter as ctk

import trade_db
from auth import has_credentials, load_credentials, save_credentials, clear_credentials

# ── App-wide theme ────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

MARKET_TZ = pytz.timezone("America/New_York")

# ── Colour palette ────────────────────────────────────────────────────────────
C_GREEN  = "#2ecc71"
C_RED    = "#e74c3c"
C_YELLOW = "#f39c12"
C_MUTED  = "#7f8c8d"
C_WHITE  = "#ecf0f1"
C_BG     = "#1a1a2e"
C_PANEL  = "#16213e"


# ─── Login Window ─────────────────────────────────────────────────────────────

class LoginWindow(ctk.CTkToplevel):
    """Shown when no saved credentials exist."""

    def __init__(self, parent: "MAIApp"):
        super().__init__(parent)
        self.parent  = parent
        self.title("MAI — Login")
        self.geometry("420x480")
        self.resizable(False, False)
        self.grab_set()
        self._build()

    def _build(self) -> None:
        ctk.CTkLabel(
            self, text="Mitchell Attempted Investing",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(pady=(30, 4))
        ctk.CTkLabel(
            self, text="MAI Trading Bot", font=ctk.CTkFont(size=13),
            text_color=C_MUTED,
        ).pack(pady=(0, 24))

        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.pack(padx=30, fill="x")

        def _row(label: str) -> ctk.CTkEntry:
            ctk.CTkLabel(frame, text=label, anchor="w").pack(
                padx=16, pady=(14, 2), fill="x"
            )
            e = ctk.CTkEntry(frame, height=36)
            e.pack(padx=16, fill="x")
            return e

        self._user_e    = _row("Robinhood Email")
        self._pass_e    = _row("Password")
        self._pass_e.configure(show="●")
        self._acct_e    = _row("Account Number")

        self._save_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(frame, text="Remember credentials",
                        variable=self._save_var).pack(padx=16, pady=(14, 16))

        self._status = ctk.CTkLabel(self, text="", text_color=C_RED)
        self._status.pack(pady=(12, 0))

        ctk.CTkButton(
            self, text="Connect to Robinhood", height=40,
            command=self._on_login,
        ).pack(padx=30, pady=16, fill="x")

    def _on_login(self) -> None:
        username = self._user_e.get().strip()
        password = self._pass_e.get()
        account  = self._acct_e.get().strip()

        if not username or not password or not account:
            self._status.configure(text="All fields are required.")
            return

        self._status.configure(text="Connecting…", text_color=C_YELLOW)
        self.update()

        try:
            import robin_stocks.robinhood as rs
            rs.login(username, password, expiresIn=86400, store_session=True)
        except Exception as exc:
            self._status.configure(text=f"Login failed: {exc}", text_color=C_RED)
            return

        if self._save_var.get():
            save_credentials(username, password, account)

        # Inject into env so trading_bot can read them
        os.environ["RH_USERNAME"]       = username
        os.environ["RH_PASSWORD"]       = password
        os.environ["RH_ACCOUNT_NUMBER"] = account

        self.destroy()
        self.parent._init_main_ui()


# ─── Sidebar ──────────────────────────────────────────────────────────────────

class SidebarFrame(ctk.CTkFrame):
    def __init__(self, parent: "MAIApp"):
        super().__init__(parent, width=210, corner_radius=0)
        self.parent = parent
        self.grid_propagate(False)
        self._build()

    def _build(self) -> None:
        self.grid_rowconfigure(10, weight=1)

        # Logo / title
        ctk.CTkLabel(
            self, text="MAI", font=ctk.CTkFont(size=28, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(24, 0), sticky="w")
        ctk.CTkLabel(
            self, text="Mitchell Attempted\nInvesting",
            font=ctk.CTkFont(size=11), text_color=C_MUTED, justify="left",
        ).grid(row=1, column=0, padx=20, pady=(2, 20), sticky="w")

        ctk.CTkLabel(self, text="Portfolio", text_color=C_MUTED,
                     font=ctk.CTkFont(size=11)).grid(
            row=2, column=0, padx=20, sticky="w")
        self.lbl_portfolio = ctk.CTkLabel(
            self, text="$—", font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_portfolio.grid(row=3, column=0, padx=20, sticky="w")

        ctk.CTkLabel(self, text="Today's P&L", text_color=C_MUTED,
                     font=ctk.CTkFont(size=11)).grid(
            row=4, column=0, padx=20, pady=(14, 0), sticky="w")
        self.lbl_pnl = ctk.CTkLabel(
            self, text="—", font=ctk.CTkFont(size=16, weight="bold"))
        self.lbl_pnl.grid(row=5, column=0, padx=20, sticky="w")

        ctk.CTkLabel(self, text="Status", text_color=C_MUTED,
                     font=ctk.CTkFont(size=11)).grid(
            row=6, column=0, padx=20, pady=(18, 0), sticky="w")
        self.lbl_status = ctk.CTkLabel(
            self, text="● Idle", font=ctk.CTkFont(size=13),
            text_color=C_MUTED)
        self.lbl_status.grid(row=7, column=0, padx=20, sticky="w")
        self.lbl_time = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11), text_color=C_MUTED)
        self.lbl_time.grid(row=8, column=0, padx=20, pady=(2, 0), sticky="w")

        # Spacer
        ctk.CTkFrame(self, height=1, fg_color=C_MUTED).grid(
            row=9, column=0, padx=20, pady=20, sticky="ew")

        self.btn_start = ctk.CTkButton(
            self, text="▶  Start Bot", fg_color="#27ae60", hover_color="#1e8449",
            command=self.parent.start_bot)
        self.btn_start.grid(row=11, column=0, padx=16, pady=(0, 8), sticky="ew")

        self.btn_stop = ctk.CTkButton(
            self, text="■  Stop Bot", fg_color="#c0392b", hover_color="#922b21",
            command=self.parent.stop_bot, state="disabled")
        self.btn_stop.grid(row=12, column=0, padx=16, pady=(0, 24), sticky="ew")

    def set_running(self, running: bool) -> None:
        if running:
            self.lbl_status.configure(text="● Running", text_color=C_GREEN)
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        else:
            self.lbl_status.configure(text="● Idle", text_color=C_MUTED)
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

    def update_clock(self) -> None:
        now = datetime.now(MARKET_TZ).strftime("%I:%M:%S %p ET")
        self.lbl_time.configure(text=now)


# ─── Dashboard Tab ────────────────────────────────────────────────────────────

class DashboardTab:
    def __init__(self, tab_frame: ctk.CTkFrame):
        self._build(tab_frame)

    def _build(self, f: ctk.CTkFrame) -> None:
        f.grid_columnconfigure((0, 1), weight=1)
        f.grid_rowconfigure(1, weight=1)

        # ── Top stats row ────────────────────────────────────────────
        stats_frame = ctk.CTkFrame(f, corner_radius=10)
        stats_frame.grid(row=0, column=0, columnspan=2, padx=12, pady=12,
                         sticky="ew")
        stats_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        def _stat(parent, label, row=0, col=0):
            ctk.CTkLabel(parent, text=label, text_color=C_MUTED,
                         font=ctk.CTkFont(size=11)).grid(
                row=row * 2, column=col, padx=20, pady=(14, 0), sticky="w")
            lbl = ctk.CTkLabel(parent, text="—",
                               font=ctk.CTkFont(size=18, weight="bold"))
            lbl.grid(row=row * 2 + 1, column=col, padx=20, pady=(0, 14), sticky="w")
            return lbl

        self.lbl_trades_today = _stat(stats_frame, "Trades Today",   col=0)
        self.lbl_trades_total = _stat(stats_frame, "Trades Total",   col=1)
        self.lbl_val_today    = _stat(stats_frame, "Volume Today",   col=2)
        self.lbl_val_total    = _stat(stats_frame, "Volume Total",   col=3)

        # ── Log area ─────────────────────────────────────────────────
        log_frame = ctk.CTkFrame(f, corner_radius=10)
        log_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 12),
                       sticky="nsew")
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_frame, text="Live Log",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, padx=14, pady=(10, 4), sticky="w")

        self.log_box = ctk.CTkTextbox(log_frame, font=ctk.CTkFont(
            family="Courier", size=11), state="disabled", wrap="word")
        self.log_box.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")

    def append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def update_stats(self, stats: dict) -> None:
        self.lbl_trades_today.configure(text=str(stats["trades_today"]))
        self.lbl_trades_total.configure(text=str(stats["total_trades"]))
        self.lbl_val_today.configure(
            text=f"${stats['value_today']:,.2f}")
        self.lbl_val_total.configure(
            text=f"${stats['value_total']:,.2f}")


# ─── Trades Tab ───────────────────────────────────────────────────────────────

_TRADE_COLS = ["Time (ET)", "Symbol", "Strategy", "Side", "Qty", "Price", "Alloc", "P&L"]
_COL_W      = [130,         70,        110,         55,     70,    80,      70,      80]


class TradesTab:
    def __init__(self, tab_frame: ctk.CTkFrame):
        self._rows: list = []
        self._build(tab_frame)

    def _build(self, f: ctk.CTkFrame) -> None:
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(f, corner_radius=0, height=32)
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        for i, (col, w) in enumerate(zip(_TRADE_COLS, _COL_W)):
            ctk.CTkLabel(hdr, text=col, width=w,
                         font=ctk.CTkFont(size=11, weight="bold"),
                         anchor="w").grid(row=0, column=i, padx=4, pady=6)

        # Scrollable rows
        self.scroll = ctk.CTkScrollableFrame(f, corner_radius=10)
        self.scroll.grid(row=1, column=0, padx=12, pady=8, sticky="nsew")

    def refresh(self, trades: list) -> None:
        # Clear existing widgets
        for widget in self.scroll.winfo_children():
            widget.destroy()
        self._rows = []

        for i, t in enumerate(trades[:200]):
            bg = "#1e2d4a" if i % 2 == 0 else "#16213e"
            row_frame = ctk.CTkFrame(self.scroll, fg_color=bg,
                                     corner_radius=4, height=28)
            row_frame.pack(fill="x", pady=1)

            ts = t.get("timestamp", "")[:19].replace("T", " ")
            try:
                dt  = datetime.fromisoformat(ts)
                et  = dt.astimezone(MARKET_TZ).strftime("%m/%d %I:%M %p")
            except Exception:
                et = ts

            pnl = t.get("pnl")
            pnl_str   = f"${pnl:+.2f}" if pnl is not None else "—"
            pnl_color = C_GREEN if (pnl or 0) >= 0 else C_RED

            values = [
                et,
                t.get("symbol", ""),
                t.get("strategy", ""),
                t.get("side", ""),
                f"{t.get('quantity', 0):.4f}",
                f"${t.get('price', 0):.2f}",
                f"${t.get('alloc_usd', 0):.2f}",
                pnl_str,
            ]
            colors = [C_WHITE] * 7 + [pnl_color]

            for j, (val, col, w) in enumerate(zip(values, colors, _COL_W)):
                ctk.CTkLabel(row_frame, text=val, width=w, text_color=col,
                             font=ctk.CTkFont(size=11), anchor="w").grid(
                    row=0, column=j, padx=4, pady=4)


# ─── Sentiment Tab ────────────────────────────────────────────────────────────

class SentimentTab:
    def __init__(self, tab_frame: ctk.CTkFrame):
        self._bars: dict = {}
        self._build(tab_frame)

    def _build(self, f: ctk.CTkFrame) -> None:
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(f, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        ctk.CTkLabel(top, text="Market Sentiment — Reddit WSB + News",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
            side="left", padx=14, pady=8)
        self.lbl_updated = ctk.CTkLabel(top, text="", text_color=C_MUTED,
                                         font=ctk.CTkFont(size=11))
        self.lbl_updated.pack(side="right", padx=14)

        self.scroll = ctk.CTkScrollableFrame(f, corner_radius=10)
        self.scroll.grid(row=1, column=0, padx=12, pady=8, sticky="nsew")
        self.scroll.grid_columnconfigure(1, weight=1)

    def refresh(self, sentiment: dict) -> None:
        for w in self.scroll.winfo_children():
            w.destroy()
        self._bars = {}

        sorted_items = sorted(sentiment.items(), key=lambda x: -x[1])
        for i, (sym, boost) in enumerate(sorted_items):
            ctk.CTkLabel(self.scroll, text=sym, width=70,
                         font=ctk.CTkFont(size=12, weight="bold"),
                         anchor="w").grid(row=i, column=0, padx=(8, 4), pady=3)

            # Bar: 0.5 (bear) → 1.0 (neutral) → 2.0 (bull)
            pct   = (boost - 0.5) / 1.5   # map [0.5,2.0] → [0,1]
            color = C_GREEN if boost >= 1.0 else C_RED
            label = "Bullish" if boost > 1.1 else ("Bearish" if boost < 0.9 else "Neutral")
            bar_frame = ctk.CTkFrame(self.scroll, height=20, corner_radius=4,
                                     fg_color="#2c3e50")
            bar_frame.grid(row=i, column=1, padx=(0, 8), pady=3, sticky="ew")
            bar_frame.grid_columnconfigure(0, weight=1)
            bar_inner = ctk.CTkFrame(bar_frame, height=20, corner_radius=4,
                                     fg_color=color,
                                     width=max(4, int(pct * 200)))
            bar_inner.place(relx=0, rely=0, relheight=1)

            ctk.CTkLabel(self.scroll, text=f"{label}  ({boost:.2f}×)",
                         font=ctk.CTkFont(size=11), text_color=color,
                         width=130, anchor="w").grid(
                row=i, column=2, padx=(4, 8), pady=3)

        self.lbl_updated.configure(
            text=f"Updated {datetime.now(MARKET_TZ).strftime('%I:%M %p ET')}")


# ─── Settings Tab ─────────────────────────────────────────────────────────────

class SettingsTab:
    def __init__(self, tab_frame: ctk.CTkFrame, app: "MAIApp"):
        self.app = app
        self._build(tab_frame)

    def _build(self, f: ctk.CTkFrame) -> None:
        frame = ctk.CTkFrame(f, corner_radius=12)
        frame.pack(padx=30, pady=30, fill="x")

        ctk.CTkLabel(frame, text="Account Settings",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            padx=20, pady=(20, 12), anchor="w")

        u, _, a = load_credentials()
        ctk.CTkLabel(frame, text=f"Email:   {u or '—'}",
                     font=ctk.CTkFont(family="Courier")).pack(padx=20, anchor="w")
        ctk.CTkLabel(frame, text=f"Account: {a or '—'}",
                     font=ctk.CTkFont(family="Courier")).pack(padx=20, pady=(4, 20),
                                                               anchor="w")

        ctk.CTkButton(
            frame, text="Change credentials",
            command=self._change_credentials,
        ).pack(padx=20, pady=(0, 8), anchor="w")

        ctk.CTkButton(
            frame, text="Clear saved credentials", fg_color=C_RED,
            hover_color="#922b21", command=self._clear_credentials,
        ).pack(padx=20, pady=(0, 20), anchor="w")

        ctk.CTkFrame(f, height=1, fg_color=C_MUTED).pack(
            padx=30, fill="x", pady=8)

        db_frame = ctk.CTkFrame(f, corner_radius=12)
        db_frame.pack(padx=30, pady=8, fill="x")
        ctk.CTkLabel(db_frame, text="Trade Database",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            padx=20, pady=(20, 8), anchor="w")
        ctk.CTkLabel(db_frame,
                     text=f"Location: {trade_db.DB_PATH}",
                     font=ctk.CTkFont(family="Courier", size=11),
                     text_color=C_MUTED).pack(padx=20, anchor="w")
        ctk.CTkButton(
            db_frame, text="Clear trade history", fg_color=C_YELLOW,
            hover_color="#d68910", text_color="#000",
            command=self._clear_trades,
        ).pack(padx=20, pady=(8, 20), anchor="w")

    def _change_credentials(self) -> None:
        self.app.show_login(reinit=True)

    def _clear_credentials(self) -> None:
        clear_credentials()
        ctk.CTkLabel(self.app, text="Credentials cleared. Restart to log in again.",
                     text_color=C_YELLOW).pack()

    def _clear_trades(self) -> None:
        import sqlite3
        with sqlite3.connect(trade_db.DB_PATH) as conn:
            conn.execute("DELETE FROM trades")
            conn.commit()


# ─── Main App ─────────────────────────────────────────────────────────────────

class MAIApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("MAI Trading Bot — Mitchell Attempted Investing")
        self.geometry("1200x720")
        self.minsize(900, 580)

        self._bot_thread: Optional[threading.Thread] = None
        self._sentinel_thread: Optional[threading.Thread] = None
        self._last_log_pos = 0
        self._sentiment_cache: dict = {}
        self._sentiment_thread: Optional[threading.Thread] = None

        trade_db.init_db()

        if not has_credentials():
            self.show_login(reinit=False)
        else:
            u, p, a = load_credentials()
            os.environ.setdefault("RH_USERNAME", u or "")
            os.environ.setdefault("RH_PASSWORD", p or "")
            os.environ.setdefault("RH_ACCOUNT_NUMBER", a or "")
            self._init_main_ui()

    # ── Login flow ────────────────────────────────────────────────────

    def show_login(self, reinit: bool = False) -> None:
        self._login_win = LoginWindow(self)
        if reinit:
            self._login_win.protocol("WM_DELETE_WINDOW", lambda: None)

    # ── UI construction ───────────────────────────────────────────────

    def _init_main_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = SidebarFrame(self)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")

        self.tabs.add("Dashboard")
        self.tabs.add("Trades")
        self.tabs.add("Sentiment")
        self.tabs.add("Settings")

        self.dashboard = DashboardTab(self.tabs.tab("Dashboard"))
        self.trades_tab = TradesTab(self.tabs.tab("Trades"))
        self.sentiment_tab = SentimentTab(self.tabs.tab("Sentiment"))
        self.settings_tab = SettingsTab(self.tabs.tab("Settings"), self)

        self._refresh()

    # ── Bot control ───────────────────────────────────────────────────

    def start_bot(self) -> None:
        if self._bot_thread and self._bot_thread.is_alive():
            return

        import trading_bot
        trading_bot.BOT_STOP.clear()

        def _run():
            asyncio.run(trading_bot.main())

        self._bot_thread = threading.Thread(target=_run, daemon=True, name="BotThread")
        self._bot_thread.start()
        self.sidebar.set_running(True)
        self.dashboard.append_log(
            f"[{datetime.now(MARKET_TZ).strftime('%H:%M:%S')}] Bot started.")

        # Start sentiment refresh in background
        self._start_sentiment_refresh()

    def stop_bot(self) -> None:
        import trading_bot
        trading_bot.BOT_STOP.set()
        self.sidebar.set_running(False)
        self.dashboard.append_log(
            f"[{datetime.now(MARKET_TZ).strftime('%H:%M:%S')}] Stop requested.")

    def _start_sentiment_refresh(self) -> None:
        def _worker():
            from sentiment import SentimentEngine
            from watchlist import WATCHLIST
            eng = SentimentEngine()
            while self._bot_thread and self._bot_thread.is_alive():
                result = eng.refresh(WATCHLIST)
                self._sentiment_cache = result
                time.sleep(900)

        self._sentiment_thread = threading.Thread(
            target=_worker, daemon=True, name="SentimentThread")
        self._sentiment_thread.start()

    # ── Periodic UI refresh (every 2 s) ──────────────────────────────

    def _refresh(self) -> None:
        try:
            self._refresh_clock()
            self._refresh_stats()
            self._refresh_trades()
            self._refresh_portfolio()
            self._refresh_log()
            self._refresh_sentiment()
        except Exception:
            pass
        self.after(2000, self._refresh)

    def _refresh_clock(self) -> None:
        self.sidebar.update_clock()

    def _refresh_stats(self) -> None:
        try:
            stats = trade_db.get_stats()
            self.dashboard.update_stats(stats)

            pnl = stats["pnl_today"]
            color = C_GREEN if pnl >= 0 else C_RED
            self.sidebar.lbl_pnl.configure(
                text=f"${pnl:+.2f}", text_color=color)
        except Exception:
            pass

    def _refresh_trades(self) -> None:
        try:
            trades = trade_db.get_recent_trades(200)
            self.trades_tab.refresh(trades)
        except Exception:
            pass

    def _refresh_portfolio(self) -> None:
        try:
            import trading_bot
            val = trading_bot.BOT_STATE.get("portfolio", 0.0)
            if val:
                self.sidebar.lbl_portfolio.configure(text=f"${val:,.2f}")
        except Exception:
            pass

    def _refresh_log(self) -> None:
        try:
            log_path = "execution_log.txt"
            if not os.path.exists(log_path):
                return
            with open(log_path, "r") as fh:
                fh.seek(self._last_log_pos)
                new_lines = fh.read()
                self._last_log_pos = fh.tell()
            if new_lines.strip():
                for line in new_lines.strip().splitlines()[-20:]:
                    self.dashboard.append_log(line)
        except Exception:
            pass

    def _refresh_sentiment(self) -> None:
        if self._sentiment_cache:
            self.sentiment_tab.refresh(self._sentiment_cache)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = MAIApp()
    app.mainloop()


if __name__ == "__main__":
    main()
