"""Heroku Scheduler entry point: refresh cached articles and six-point summaries."""

import logging

from stbot.ingestion import main

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Third-party exception strings can contain credentials or connection URLs.
        logging.getLogger(__name__).error("Article refresh failed (%s)", type(error).__name__)
        raise SystemExit(1) from None
