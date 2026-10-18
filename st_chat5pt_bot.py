"""WSGI entry point for the Heroku web process."""

from stbot.web import create_app

__all__ = ["create_app"]
