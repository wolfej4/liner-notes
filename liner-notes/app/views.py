"""Jinja2 environment shared by pages and emails."""
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config

env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                  autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)


def hours(ms) -> str:
    h = (ms or 0) / 3_600_000
    if h >= 100:
        return f"{round(h):,} hours"
    if h >= 1:
        text = f"{h:.1f}".rstrip("0").rstrip(".")
        return f"{text} hour" if text == "1" else f"{text} hours"
    m = round((ms or 0) / 60000)
    return f"{m} minute" if m == 1 else f"{m} minutes"


def when(ts) -> str:
    if not ts:
        return "never"
    d = datetime.fromtimestamp(ts, config.tz())
    return f"{d:%b} {d.day}, {d.year}, {d:%I:%M %p}".replace(" 0", " ")


def day(ts) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts, config.tz())
    return f"{d:%b} {d.day}, {d.year}"


env.filters.update(hours=hours, when=when, day=day, num=lambda n: f"{n or 0:,}")
