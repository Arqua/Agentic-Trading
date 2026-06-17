"""Local credential storage using the OS keychain (keyring)."""

import keyring
import keyring.errors

APP_NAME = "MitchellAttemptedInvesting"
_KEYS = ("rh_username", "rh_password", "rh_account")


def save_credentials(username: str, password: str, account_number: str) -> None:
    keyring.set_password(APP_NAME, "rh_username", username)
    keyring.set_password(APP_NAME, "rh_password", password)
    keyring.set_password(APP_NAME, "rh_account", account_number)


def load_credentials() -> tuple:
    """Return (username, password, account_number). Any may be None if not set."""
    username = keyring.get_password(APP_NAME, "rh_username")
    password = keyring.get_password(APP_NAME, "rh_password")
    account  = keyring.get_password(APP_NAME, "rh_account")
    return username, password, account


def has_credentials() -> bool:
    username, _, _ = load_credentials()
    return username is not None


def clear_credentials() -> None:
    for key in _KEYS:
        try:
            keyring.delete_password(APP_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass
