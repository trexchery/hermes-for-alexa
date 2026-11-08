"""Store a secret read with getpass:  python -m deploy.set_secret cf_api_token"""
from __future__ import annotations

import getpass
import sys

from deploy import secrets


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m deploy.set_secret <name>   (e.g. cf_api_token)")
        return 2
    value = getpass.getpass(f"Value for {argv[0]} (hidden): ").strip()
    if not value:
        print("empty value, nothing stored")
        return 1
    secrets.put(argv[0], value)
    print(f"stored {argv[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
