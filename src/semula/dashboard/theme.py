"""The Material 3 tokens from ``docs/design/tokens.json`` mapped onto indah's token names.

indah's theme is a fixed list of roles (``bg``, ``surface``, ``primary``, ``ok``, ``warn``,
``err`` ...), so the design's surface tiers and custom roles are folded in: panels take the
lowest container tier in light and the low tier in dark, ``ok`` is the primary green, and the
logo red (``record``) is indah's ``err``. A test keeps these values equal to the token file.
"""

from __future__ import annotations

from typing import Any

LIGHT = {
    "bg": "#F6FAF7",
    "surface": "#FFFFFF",
    "surface-2": "#E8F0EB",
    "surface-3": "#E1EAE5",
    "text": "#2A3531",
    "muted": "#57615D",
    "outline": "#727D78",
    "outline-soft": "#A9B4AF",
    "primary": "#256A59",
    "on-primary": "#E4FFF4",
    "primary-container": "#ACF0DB",
    "on-primary-container": "#135C4C",
    "secondary": "#4C645C",
    "on-secondary": "#E4FFF4",
    "secondary-container": "#CDE9DE",
    "on-secondary-container": "#3E574F",
    "ok": "#256A59",
    "warn": "#825500",
    "err": "#C8271F",
}
DARK = {
    "bg": "#0A0F0D",
    "surface": "#0E1512",
    "surface-2": "#141B19",
    "surface-3": "#19211E",
    "text": "#DDE8E2",
    "muted": "#A3AEA8",
    "outline": "#6D7873",
    "outline-soft": "#404A46",
    "primary": "#9ED1C0",
    "on-primary": "#15483C",
    "primary-container": "#2A5B4E",
    "on-primary-container": "#BAEEDD",
    "secondary": "#B2CCC2",
    "on-secondary": "#2D453D",
    "secondary-container": "#284039",
    "on-secondary-container": "#ABC5BB",
    "ok": "#9ED1C0",
    "warn": "#FFB952",
    "err": "#FF7767",
}
# Where each indah token comes from in docs/design/tokens.json (light, dark), for the test.
SOURCES: dict[str, tuple[str, str]] = {
    "bg": ("surface", "surface"),
    "surface": ("surface-container-lowest", "surface-container-low"),
    "surface-2": ("surface-container", "surface-container"),
    "surface-3": ("surface-container-high", "surface-container-high"),
    "text": ("on-surface", "on-surface"),
    "muted": ("on-surface-variant", "on-surface-variant"),
    "outline": ("outline", "outline"),
    "outline-soft": ("outline-variant", "outline-variant"),
    "primary": ("primary", "primary"),
    "on-primary": ("on-primary", "on-primary"),
    "primary-container": ("primary-container", "primary-container"),
    "on-primary-container": ("on-primary-container", "on-primary-container"),
    "secondary": ("secondary", "secondary"),
    "on-secondary": ("on-secondary", "on-secondary"),
    "secondary-container": ("secondary-container", "secondary-container"),
    "on-secondary-container": ("on-secondary-container", "on-secondary-container"),
    "ok": ("primary", "primary"),
    "warn": ("warn", "warn"),
    "err": ("record", "record"),
}


def build_theme() -> Any:
    import indah

    return indah.Theme(light=LIGHT, dark=DARK, radius="12px")
