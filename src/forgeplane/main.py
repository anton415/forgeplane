"""Module entry point for running Forgeplane as a Python module."""

# Import the Typer application object that defines all CLI commands.
from forgeplane.cli import app

# This block runs only when this file is executed directly, for example:
# python -m forgeplane.main
if __name__ == "__main__":
    # Calling app() hands control to Typer, which parses CLI arguments and
    # dispatches to the matching command from forgeplane.cli.
    app()
