"""Fallback entry point: ``python -m account_warmer`` (same as the ``warmer`` console script)."""
if __name__ == "__main__":
    from .main import app

    app()
