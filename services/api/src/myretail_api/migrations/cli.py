from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from alembic import command
from alembic.config import Config


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run controlled MyRetail state migrations.")
    parser.add_argument("action", choices=("upgrade", "current", "downgrade"))
    parser.add_argument("revision", nargs="?")
    args = parser.parse_args(arguments)

    configuration = Config()
    configuration.set_main_option("script_location", "myretail_api:migrations")

    try:
        if args.action == "upgrade":
            command.upgrade(configuration, args.revision or "head")
        elif args.action == "downgrade":
            command.downgrade(configuration, args.revision or "-1")
        else:
            if args.revision is not None:
                parser.error("current does not accept a revision")
            command.current(configuration, verbose=False)
    except Exception as exc:
        print(
            f"MyRetail state migration failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
