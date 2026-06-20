"""``python -m src.evals`` entry point."""

import logging

from src.evals.reporting import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
