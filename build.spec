# build.spec — PyInstaller spec for MAI Trading Bot
# Build: pyinstaller build.spec

import os
import sys

# Locate customtkinter data files so the dark-theme assets are bundled
try:
    import customtkinter
    CTK_PATH = os.path.dirname(customtkinter.__file__)
    ctk_datas = [(CTK_PATH, "customtkinter")]
except ImportError:
    ctk_datas = []

block_cipher = None

a = Analysis(
    ["gui_app.py"],
    pathex=["."],
    binaries=[],
    datas=ctk_datas + [
        ("watchlist.py",   "."),
        ("indicators.py",  "."),
        ("strategies.py",  "."),
        ("execution.py",   "."),
        ("risk_manager.py", "."),
        ("logger.py",      "."),
        ("auth.py",        "."),
        ("trade_db.py",    "."),
        ("sentiment.py",   "."),
        ("trading_bot.py", "."),
    ],
    hiddenimports=[
        "customtkinter",
        "PIL",
        "PIL._tkinter_finder",
        "keyring",
        "keyring.backends",
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "vaderSentiment",
        "vaderSentiment.vaderSentiment",
        "feedparser",
        "requests",
        "pytz",
        "numpy",
        "robin_stocks",
        "robin_stocks.robinhood",
        "aiohttp",
        "dotenv",
        "sqlite3",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MAI_Trading_Bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/mai_icon.ico",  # uncomment and add your icon file
)
