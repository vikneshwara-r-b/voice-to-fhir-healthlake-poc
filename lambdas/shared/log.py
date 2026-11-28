"""Structured logging. Log ids, statuses, codes and counts only: never transcript or entity text."""

import json


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, default=str, sort_keys=True))
