"""Create a dashboard account:  docker compose run --rm web python -m gi_trace.web.create_user

Asks for the username (an email address is fine), the role and the password. The
password is typed hidden, twice, and only its scrypt hash is stored. Where typing
cannot be hidden (some IDE consoles, piped input) it stops instead of showing it.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import warnings

from .._docker import require_container
from .users import DEFAULT_DB, ROLES, UserError, UserStore


def run(db, ask=input, ask_hidden=getpass.getpass, log=print) -> int:
    try:
        username = ask("Username (an email address is fine): ")
        role = ask(f"Role ({', '.join(ROLES)}): ").strip().lower()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = ask_hidden("Password (hidden): ")
            repeat = ask_hidden("Repeat the password: ")
        if repeat != password:
            log("ERROR: the passwords do not match. Nothing was created.")
            return 1
        name = UserStore(db).create_user(username, role, password)
    except UserError as e:
        log(f"ERROR: {e}. Nothing was created.")
        return 1
    except getpass.GetPassWarning:
        log("\nERROR: this console cannot hide the password, so it was not asked for. "
            "Use an interactive `docker compose run --rm web python -m gi_trace.web.create_user`. "
            "Nothing was created.")
        return 1
    except (EOFError, KeyboardInterrupt):
        log("\nCancelled. Nothing was created.")
        return 1
    log(f"Created {role} account {name}.")
    if str(db) == str(DEFAULT_DB):
        log("Commit accounts/users.db and push so other clones get this account.")
    return 0


def main(argv=None) -> int:
    require_container()
    ap = argparse.ArgumentParser(description="Create a dashboard account.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"accounts database (default: {DEFAULT_DB})")
    return run(ap.parse_args(argv).db)


if __name__ == "__main__":
    sys.exit(main())
