"""troupe — a team of AI agents that builds software with you."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as cli_main

    cli_main()
