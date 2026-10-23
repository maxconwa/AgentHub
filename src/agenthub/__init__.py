"""agenthub: a secure coordination hub for Claude Code agents."""

__version__ = "0.3.9.0"


def main() -> None:
    from agenthub.cli import app

    app()
