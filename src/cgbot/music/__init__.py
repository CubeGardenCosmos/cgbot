"""Soundfont catalog and download management for the music feature."""

from cgbot.music.errors import MusicError
from cgbot.music.soundfont import CatalogEntry, SoundfontInfo, SoundfontStore

__all__ = ["CatalogEntry", "MusicError", "SoundfontInfo", "SoundfontStore"]
