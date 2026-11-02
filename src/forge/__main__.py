"""Allow ``python -m forge`` to run the Forge CLI."""

from forge.cli import app

if __name__ == "__main__":
    app(prog_name="forge")
