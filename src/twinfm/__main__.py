"""Command line: ``twinfm [LEFT] [RIGHT] [--btty-exec | --no-btty-exec]
[--remote-home PATH] [--config PATH] [--version]``."""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections.abc import Sequence
from typing import TYPE_CHECKING

from twinfm import __version__
from twinfm.backend import parse_location
from twinfm.config import DEFAULT_CONFIG_PATH, load_settings

if TYPE_CHECKING:
    from twinfm.ui.app import TwinFM


class _HelpFormatter(argparse.HelpFormatter):
    """Wraps help text without splitting a flag such as --no-btty-exec at a hyphen."""

    def _split_lines(self, text: str, width: int) -> list[str]:
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="twinfm",
        description="Two-panel file manager for local files and a btty (Beacon IDE) workspace.",
        formatter_class=_HelpFormatter,
    )
    location = "a local directory, or btty:[PATH] for the btty workspace (default: the current directory)"
    parser.add_argument("left", nargs="?", default=".", metavar="LEFT", help=f"left panel: {location}")
    parser.add_argument("right", nargs="?", default=".", metavar="RIGHT", help=f"right panel: {location}")
    parser.add_argument(
        "--btty-exec",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="exec mode, on by default: let btty run shell commands on the Beacon IDE "
        "(--exec --allow-unrestricted) to browse, delete, rename and make folders; "
        "--no-btty-exec turns it off, allowing only single-file upload and download "
        "(overrides [btty] exec in the config file)",
    )
    parser.add_argument(
        "--remote-home",
        metavar="PATH",
        help="absolute remote directory btty: opens at (overrides [btty] home)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=f"config file (default: $TWINFM_CONFIG, else {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--version", action="version", version=f"twinfm {__version__}")
    return parser


def build_app(argv: Sequence[str] | None = None) -> TwinFM:
    """Parse ``argv`` (default: the command line) and build the app without
    running it. A bad flag, config file or location exits with status 2."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(
            args.config, {"btty_exec": args.btty_exec, "remote_home": args.remote_home}
        )
        left = parse_location(args.left, settings)
        right = parse_location(args.right, settings)
    except (ValueError, LookupError) as e:  # ConfigError, bad override, UnknownBackendError
        parser.exit(2, f"twinfm: error: {e}\n")

    from twinfm.ui.app import TwinFM  # Textual loads only when the app runs

    return TwinFM(left, right, settings)


def main(argv: Sequence[str] | None = None) -> int:
    app = build_app(argv)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
