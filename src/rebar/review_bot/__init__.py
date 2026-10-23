"""Gerrit webhook receiver package.
The FastAPI app calls rebar's review kernel as a library and posts Gerrit
votes. Importing this package does not import FastAPI. Only review_bot.app
requires the reviewbot extra."""
