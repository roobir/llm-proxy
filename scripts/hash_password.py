#!/usr/bin/env python3
"""Prints a bcrypt hash for DASHBOARD_PASSWORD_HASH. Prompts interactively
so the plaintext password never ends up in shell history."""
import getpass

import bcrypt

if __name__ == "__main__":
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        raise SystemExit("passwords did not match")
    print(bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())
