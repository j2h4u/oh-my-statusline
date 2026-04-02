#!/usr/bin/env python3
"""Claude Code statusline + TUI theme editor — unified single file.

Statusline mode (default): Reads JSON from stdin (Claude Code statusline protocol),
renders N lines via a slot system. Each slot is either a built-in provider
(limits, git) or an external shell command.

Theme editor mode (--theme flag or symlinked as *theme*):
TUI for editing theme colors, attributes, and settings.

Config: ~/.config/omcc-statusline/config.json

Git status indicators:
  *  dirty (unstaged changes)   — yellow dim
  +  staged changes             — green dim
  ?  untracked files            — gray
  ↑  ahead of remote            — cyan
  ↓  behind remote              — purple
"""

import hashlib
import json
import math
import os
import re
import select
import shutil
import signal
import sqlite3
import sys
import subprocess
import tempfile
import termios
import threading
import time
import tty
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import NamedTuple
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- constants ---------------------------------------------------------------

# Display
PARENT_DIR_MAX_LEN = 15
BRANCH_LABEL = "⑂"
PR_DOT = "⁕"

# Demo/example data
DEMO_DIR_NAME = "my-project/"
DEMO_BRANCH = "feature/wonderful-new-feature"
DEMO_BRANCH_MAIN = DEMO_BRANCH
DEMO_BRANCH_FEATURE = "feat/auth"
DEMO_BRANCH_DEV = "develop"
DEMO_PARENT_DIR = "workspace/"
DEMO_CURRENT_DIR = "my-project/"

# Paths
CONFIG_DIR = Path.home() / ".config" / "omcc-statusline"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = Path("/tmp") / "omcc-statusline"
CACHE_DB = CACHE_DIR / "cache.db"

# Cache TTLs (seconds)
API_CACHE_TTL = 360      # 6 min — CI, PR, limits (anything that hits an API)
GIT_CACHE_TTL = 5        # 5 sec — local git status (changes frequently)
GH_CHECK_TTL = 1800      # 30 min — gh CLI availability (rarely changes)

# Context window normalization.
# Autocompact fires at CLAUDE_AUTOCOMPACT_PCT_OVERRIDE% *used* (default 95%).
# That maps to (100 - pct)% remaining = dead zone we'll never reach.
# Usable range = everything above that remaining threshold.
CTX_IRRITATE_USER_ABOVE = 70.0  # normalized used% above which the ctx block blinks
def _ctx_autocompact_remaining() -> float:
    """Return the remaining% at which autocompact fires (default: 5.0)."""
    raw = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "")
    try:
        pct_used = float(raw)
        if 1.0 <= pct_used <= 100.0:
            return 100.0 - pct_used
    except (ValueError, TypeError):
        pass
    return 5.0  # default: autocompact at 95% used → 5% remaining

# Timeouts (seconds)
TIMEOUT_SUBPROCESS = 5
TIMEOUT_GIT = 3
TIMEOUT_GH_API = 15
GH_PR_FETCH_LIMIT = 20

# Error/slot
SLOT_TIMEOUT = 120
SLOT_CACHE_TTL = 60
ERROR_COOLDOWN_DEFAULT = 30   # generic error backoff (seconds)
VIBES_MAX_DATA_AGE = 600      # 10 min — don't show vibes from older data

# Limits provider
LIMITS_CACHE_TTL = API_CACHE_TTL
LIMITS_HTTP_TIMEOUT = 5
LIMITS_COOLDOWN_MIN = 300       # 5 min — minimum backoff on 429
LIMITS_COOLDOWN_MAX = 3600      # 1 hour — maximum backoff cap
LIMITS_API_URL = "https://api.anthropic.com/api/oauth/usage"
LIMITS_CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
LIMITS_BAR_WIDTH = 5
LIMITS_WINDOW_SECONDS = 7 * 24 * 3600
WORK_DAYS = 5
LIMITS_PACE_BUDGET_HOURS = WORK_DAYS * 24
LIMITS_COUNTDOWN_THRESHOLD = 50
LIMITS_PACE_MIN_EXPECTED = 1

PACE_SCALE = [
    (-20, "depresso"),
    ( -5, "salty"),
    (  5, "chill"),
    ( 20, "hyped"),
    (float("inf"), "based"),
]

RAMP_PACE_GOOD = ((0.45, 0.06, 195), (0.65, 0.10, 195))  # dark→mid teal
RAMP_PACE_BAD  = ((0.45, 0.08, 50),  (0.60, 0.13, 25))   # muted → warm orange
PACE_COLOR_MAX_DELTA = 40

RAMP_PRESETS = {
    "aurora":    [(0, 44), (35, 33), (70, 127), (100, 160)],
    "traffic":   [(0, 35), (50, 185), (100, 160)],
    "twilight":  [(0, 33), (50, 92), (100, 124)],
    "ember":     [(0, 37), (50, 143), (100, 131)],
    "spectrum":  [(0, 35), (25, 44), (50, 33), (75, 127), (100, 160)],
    "heatmap":   [(0, 33), (25, 44), (50, 40), (75, 184), (100, 160)],
}

# INDICATOR_CONFIG — built from SETTINGS_DEFS after it's defined (see below)

_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
_VBAR_EIGHTHS = " ▁▂▃▄▅▆▇█"

DEFAULT_SLOTS = [[{"provider": "path"}, {"provider": "git"}, {"provider": "limits"}, {"provider": "vibes"}]]

# --- ANSI helpers ------------------------------------------------------------

ESC = "\033"
CSI = f"{ESC}["

def fg256(n: int) -> str: return f"{CSI}38;5;{n}m"
def bg256(n: int) -> str: return f"{CSI}48;5;{n}m"
def fg_rgb(r: int, g: int, b: int) -> str: return f"{CSI}38;2;{r};{g};{b}m"


def _rainbow(text: str, hue_start: float = 0.0, hue_range: float = 1.0) -> str:
    """Colorize text with a perceptually uniform rainbow using OKLCH.

    All letters share the same perceived brightness regardless of hue.
    Per-hue max chroma is computed via binary search against sRGB gamut
    boundary (CSS Color Level 4 approach), so every hue gets maximum
    vibrancy without clipping.

    hue_start: starting hue [0.0, 1.0) — maps to full 360° wheel
    hue_range: fraction of color wheel to span across the text
    """
    _L = 0.65
    _CHROMA_FILL = 0.95  # fraction of max chroma per hue

    def _oklch_to_lin(h: float, C: float) -> tuple[float, float, float]:
        h_rad = h % 1.0 * 2 * math.pi
        a = C * math.cos(h_rad)
        b = C * math.sin(h_rad)
        l_ = _L + 0.3963377774 * a + 0.2158037573 * b
        m_ = _L - 0.1055613458 * a - 0.0638541728 * b
        s_ = _L - 0.0894841775 * a - 1.2914855480 * b
        l = l_ ** 3
        m = m_ ** 3
        s = s_ ** 3
        return (+4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    def _max_chroma(h: float) -> float:
        lo, hi = 0.0, 0.4
        for _ in range(20):
            mid = (lo + hi) * 0.5
            r, g, b = _oklch_to_lin(h, mid)
            if -1e-6 <= r <= 1 + 1e-6 and -1e-6 <= g <= 1 + 1e-6 and -1e-6 <= b <= 1 + 1e-6:
                lo = mid
            else:
                hi = mid
        return lo

    def _gamma(c: float) -> int:
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return int(c * 255 + 0.5)

    chars = list(text)
    n = max(len(chars) - 1, 1)
    out = []
    for i, ch in enumerate(chars):
        h = hue_start + (i / n) * hue_range
        if ch == " ":
            out.append(" ")
            continue
        C = _max_chroma(h) * _CHROMA_FILL
        r, g, b = _oklch_to_lin(h, C)
        out.append(f"{fg_rgb(_gamma(r), _gamma(g), _gamma(b))}{ch}")
    return "".join(out)

RESET         = f"{CSI}0m"
BOLD          = f"{CSI}1m"
DIM           = f"{CSI}2m"
ITALIC        = f"{CSI}3m"
UNDERLINE     = f"{CSI}4m"
UL_DOUBLE     = f"{CSI}21m"
UL_CURLY      = f"{CSI}4:3m"
UL_DOTTED     = f"{CSI}4:4m"
UL_DASHED     = f"{CSI}4:5m"
REVERSE       = f"{CSI}7m"
BLINK         = f"{CSI}5m"
STRIKE        = f"{CSI}9m"
OVERLINE      = f"{CSI}53m"

HIDE_CURSOR   = f"{CSI}?25l"
SHOW_CURSOR   = f"{CSI}?25h"
CLEAR_SCREEN  = f"{CSI}2J{CSI}H"
CLEAR_LINE    = f"{CSI}2K"


class T:
    """Semantic theme tokens — the only colors render code should reference."""
    dir_parent     = fg256(239)
    dir_name       = fg256(243)
    branch_sign    = fg256(66)
    branch_name    = fg256(66)
    git_dirty      = DIM + fg256(3)
    git_staged     = DIM + fg256(2)
    git_untracked  = fg256(3)
    git_ahead      = fg256(6)
    git_behind     = fg256(5)
    ok             = fg256(22)
    warn           = fg256(94)
    wait           = fg256(27)
    none           = fg256(237)
    notif          = fg256(6)
    sep            = fg256(241)
    err            = fg256(88)
    lim_time       = fg256(239)
    lim_bar_bg     = bg256(236)
    R              = RESET


ATTRS_AVAILABLE = [
    ("none",       "",        "Clear all attributes"),
    ("dim",        DIM,       "Dim/faint text"),
    ("bold",       BOLD,      "Bold text"),
    ("italic",     ITALIC,    "Italic text"),
    ("underline",  UNDERLINE, "Single underline"),
    ("ul_double",  UL_DOUBLE, "Double underline"),
    ("ul_curly",   UL_CURLY,  "Curly underline"),
    ("ul_dotted",  UL_DOTTED, "Dotted underline"),
    ("ul_dashed",  UL_DASHED, "Dashed underline"),
    ("blink",      BLINK,     "Blinking text"),
    ("strike",     STRIKE,    "Strikethrough"),
    ("overline",   OVERLINE,  "Overline"),
    ("reverse",    REVERSE,   "Swap FG/BG"),
]

ATTR_SGR = {name: sgr for name, sgr, _ in ATTRS_AVAILABLE}

# SEP_GIT, SEP_LIMITS, SEP_EXTRA — built from SETTINGS_DEFS below

# --- theme editor data model ------------------------------------------------

_ALL_PROPS = frozenset({"fg", "bg", "attrs"})


@dataclass
class ElementDef:
    key: str
    label: str
    desc: str
    sample: str
    group: str
    props: frozenset[str] = field(default_factory=lambda: _ALL_PROPS)
    gap: str = ""  # preview gap before element: "" | " " | "  " | "sep" | "git_sep"


ELEMENTS = [
    ElementDef("dir_parent",    "Parent dir",     "Muted parent directory in path",     DEMO_PARENT_DIR,   "dir"),
    ElementDef("dir_name",      "Current dir",    "Current working directory name",     DEMO_CURRENT_DIR,  "dir"),
    ElementDef("sep",           "Separator",      "Section separator",                  "|",        "ui"),
    ElementDef("branch_sign",   "Branch sign",    "Git branch indicator symbol",        "⑂",               "git"),
    ElementDef("branch_name",   "Branch name",    "Current git branch name",            DEMO_BRANCH,       "git"),
    ElementDef("git_dirty",     "Dirty",          "Unstaged changes indicator",         "*",        "git"),
    ElementDef("git_staged",    "Staged",         "Staged changes indicator",           "+",        "git"),
    ElementDef("git_untracked", "Untracked",      "Untracked files indicator",          "?",        "git"),
    ElementDef("git_ahead",     "Ahead",          "Commits ahead of remote",            "↑",        "git"),
    ElementDef("git_behind",    "Behind",         "Commits behind remote",              "↓",        "git"),
    ElementDef("ok",            "OK",             "Success: CI pass, PR approved",      "CI",       "ci",  gap="git_sep"),
    ElementDef("err",           "Error",          "Failure: CI fail, PR rejected",      "CI",       "ci",  gap=" "),
    ElementDef("wait",          "Wait",           "CI pending / PR dot blue",           "CI",       "ci",  gap=" "),
    ElementDef("none",          "None",           "CI/PR not configured (dim)",         "CI",       "ci",  gap=" "),
    ElementDef("notif",         "Notifications",  "Unread notification count",          "💬3",      "pr",  gap=" "),
    ElementDef("warn",          "Warning",        "Stale data / retry countdown",       "WARN",     "ui",  gap="  "),
    ElementDef("lim_time",      "Lim time",       "Reset countdown",                    "4h26m",    "lim", gap="sep"),
    ElementDef("lim_bar_bg",    "Bar bg",         "Progress bar background (fg = ramp)", "▁▂▃",      "lim", frozenset({"bg"})),
]

# Runtime check: T theme tokens must match ELEMENTS definitions
_element_keys = frozenset(e.key for e in ELEMENTS)


@dataclass
class ThemeEntry:
    fg: int | None = None
    bg: int | None = None
    attrs: list[str] = field(default_factory=list)

    def copy(self) -> "ThemeEntry":
        return ThemeEntry(fg=self.fg, bg=self.bg, attrs=list(self.attrs))

    @classmethod
    def from_dict(cls, d: dict) -> "ThemeEntry":
        return cls(fg=d.get("fg"), bg=d.get("bg"), attrs=d.get("attrs", []))

    def to_dict(self) -> dict:
        """Serialize to config dict, omitting None/empty fields."""
        d: dict = {}
        if self.fg is not None:
            d["fg"] = self.fg
        if self.bg is not None:
            d["bg"] = self.bg
        if self.attrs:
            d["attrs"] = self.attrs
        return d


class PrStatus(NamedTuple):
    """Structured PR status data: dots grouped by CI state + unread count."""
    dots_red: list[str]      # FAILURE/ERROR PR dots (may contain OSC8 links)
    dots_pending: list[str]  # PENDING/EXPECTED PR dots
    dots_green: list[str]    # SUCCESS PR dots
    dots_gray: list[str]     # UNKNOWN state PR dots
    unread_count: int        # Unread notification count


DEFAULTS: dict[str, ThemeEntry] = {
    "dir_parent":     ThemeEntry(fg=239),
    "dir_name":       ThemeEntry(fg=243),
    "branch_sign":    ThemeEntry(fg=66),
    "branch_name":    ThemeEntry(fg=66),
    "git_dirty":      ThemeEntry(fg=3, attrs=["dim"]),
    "git_staged":     ThemeEntry(fg=2, attrs=["dim"]),
    "git_untracked":  ThemeEntry(fg=3),
    "git_ahead":      ThemeEntry(fg=6),
    "git_behind":     ThemeEntry(fg=5),
    "ok":             ThemeEntry(fg=22),
    "warn":           ThemeEntry(fg=94),
    "wait":           ThemeEntry(fg=27),
    "none":           ThemeEntry(fg=237),
    "notif":          ThemeEntry(fg=6),
    "sep":            ThemeEntry(fg=241),
    "err":            ThemeEntry(fg=88),
    "lim_time":       ThemeEntry(fg=239),
    "lim_bar_bg":     ThemeEntry(bg=236),
}

assert frozenset(DEFAULTS.keys()) == _element_keys, "DEFAULTS and ELEMENTS out of sync"
del _element_keys

# Build T from DEFAULTS (single source of truth for colors)
for _k, _d in DEFAULTS.items():
    _parts = [ATTR_SGR[a] for a in _d.attrs if a in ATTR_SGR]
    if _d.fg is not None:
        _parts.append(fg256(_d.fg))
    if _d.bg is not None:
        _parts.append(bg256(_d.bg))
    setattr(T, _k, "".join(_parts))
del _k, _d, _parts

RAMP_NAMES = ["aurora", "traffic", "twilight", "ember", "spectrum", "heatmap"]
DISPLAY_MODES = ["number", "vertical", "horizontal"]
SEP_OPTIONS = ["·", "•", "│", "─", "⋮", "|", "║", "┃", "❘", ""]
_SEP_DISPLAY = {"": "∅"}
_SEP_KEYS = frozenset(("separator", "git_separator", "limits_separator"))


def _sep_display_label(sdef_key: str, val: str) -> str:
    """Map separator value to display label (empty string -> null symbol)."""
    return _SEP_DISPLAY.get(val, val) if sdef_key in _SEP_KEYS else val


@dataclass
class SettingDef:
    key: str
    label: str
    options: list[str]
    default: str


SETTINGS_DEFS = [
    SettingDef("5h_ramp",           "5h ramp",          RAMP_NAMES,          "spectrum"),
    SettingDef("7d_ramp",           "7d ramp",          RAMP_NAMES,          "spectrum"),
    SettingDef("ctx_ramp",          "ctx ramp",         RAMP_NAMES,          "aurora"),
    SettingDef("5h_display",        "5h display",       DISPLAY_MODES,       "vertical"),
    SettingDef("7d_display",        "7d display",       DISPLAY_MODES,       "vertical"),
    SettingDef("ctx_display",       "ctx display",      DISPLAY_MODES,       "vertical"),
    SettingDef("separator",         "Sep providers",    SEP_OPTIONS, "⋮"),
    SettingDef("git_separator",     "Sep git",          SEP_OPTIONS, "·"),
    SettingDef("limits_separator",  "Sep limits",       SEP_OPTIONS, ""),
]

# Build SEP_* from SETTINGS_DEFS (single source of truth for separators)
_SETTINGS_DEFAULTS = {s.key: s.default for s in SETTINGS_DEFS}

def _sep_ansi(char: str) -> str:
    """Build separator ANSI string: ' sep_colorCHARreset ' or just ' '."""
    return f" {T.sep}{char}{T.R} " if char else " "


def _load_separator(settings: dict, key: str) -> str:
    """Load a separator from settings dict, falling back to _SETTINGS_DEFAULTS."""
    val = settings.get(key)
    return _sep_ansi(val) if isinstance(val, str) else _sep_ansi(_SETTINGS_DEFAULTS[key])


def _get_setting(settings_dict: dict, key: str) -> str:
    """Return setting value with auto-fallback to _SETTINGS_DEFAULTS."""
    return settings_dict.get(key, _SETTINGS_DEFAULTS[key])


SEP_EXTRA = _sep_ansi(_SETTINGS_DEFAULTS["separator"])
SEP_GIT = _sep_ansi(_SETTINGS_DEFAULTS["git_separator"])
SEP_LIMITS = _sep_ansi(_SETTINGS_DEFAULTS["limits_separator"])

# Build INDICATOR_CONFIG from SETTINGS_DEFS (single source of truth for ramp/display)
INDICATOR_CONFIG = {
    prefix: {
        "ramp": RAMP_PRESETS[_SETTINGS_DEFAULTS[f"{prefix}_ramp"]],
        "display": _SETTINGS_DEFAULTS[f"{prefix}_display"],
    }
    for prefix in ("5h", "7d", "ctx")
}

# --- config validation -------------------------------------------------------

_VALID_THEME_TOKENS = frozenset(e.key for e in ELEMENTS)
_VALID_SETTINGS_KEYS = frozenset(s.key for s in SETTINGS_DEFS)
_VALID_TOP_KEYS = frozenset({"slots", "settings", "theme"})
_VALID_SLOT_KEYS = frozenset({"provider", "command", "ttl", "enabled", "cwd_sensitive"})
_VALID_ATTRS = frozenset(name for name, _, _ in ATTRS_AVAILABLE)


def _validate_theme_token(token: str, val: object, errors: list[str]) -> None:
    """Validate a single theme token entry."""
    if token not in _VALID_THEME_TOKENS:
        errors.append(f"theme: unknown token '{token}'")
        return
    if not isinstance(val, dict):
        errors.append(f"theme.{token}: must be an object")
        return
    for fld in val:
        if fld not in ("fg", "bg", "attrs"):
            errors.append(f"theme.{token}: unknown field '{fld}'")
    fg = val.get("fg")
    if fg is not None and (not isinstance(fg, int) or not 0 <= fg <= 255):
        errors.append(f"theme.{token}.fg: must be 0-255, got {fg!r}")
    bg = val.get("bg")
    if bg is not None and (not isinstance(bg, int) or not 0 <= bg <= 255):
        errors.append(f"theme.{token}.bg: must be 0-255, got {bg!r}")
    attrs = val.get("attrs")
    if attrs is not None:
        if not isinstance(attrs, list):
            errors.append(f"theme.{token}.attrs: must be a list")
        else:
            for a in attrs:
                if a not in _VALID_ATTRS:
                    errors.append(f"theme.{token}.attrs: unknown attr '{a}'")


def _validate_slot_show(show: object, slot: dict, has_provider: bool, prefix: str, errors: list[str]) -> None:
    """Validate a slot's 'show' field."""
    if not isinstance(show, list):
        errors.append(f"{prefix}.show: must be an array")
    elif has_provider:
        prov = slot["provider"]
        valid_sections = PROVIDER_SECTIONS.get(prov)
        if valid_sections is None:
            errors.append(f"{prefix}.show: provider '{prov}' has no sections")
        else:
            for s in show:
                if not isinstance(s, str):
                    errors.append(f"{prefix}.show: values must be strings")
                elif s not in valid_sections:
                    errors.append(
                        f"{prefix}.show: unknown section '{s}', "
                        f"valid: [{', '.join(valid_sections)}]"
                    )


def _validate_config(config: dict) -> list[str]:
    """Validate hierarchical config, return list of error strings."""
    errors: list[str] = []

    for key in config:
        if key not in _VALID_TOP_KEYS:
            hint = ' (theme tokens go inside "theme")' if key in _VALID_THEME_TOKENS else ""
            errors.append(f"unknown top-level key: '{key}'{hint}")

    theme = config.get("theme")
    if theme is not None:
        if not isinstance(theme, dict):
            errors.append("theme: must be an object")
        else:
            for token, val in theme.items():
                _validate_theme_token(token, val, errors)

    settings = config.get("settings")
    if settings is not None:
        if not isinstance(settings, dict):
            errors.append("settings: must be an object")
        else:
            ramp_names = set(RAMP_PRESETS.keys())
            display_modes = set(DISPLAY_MODES)
            for key, val in settings.items():
                if key not in _VALID_SETTINGS_KEYS:
                    errors.append(f"settings: unknown key '{key}'")
                elif key in ("5h_ramp", "7d_ramp", "ctx_ramp"):
                    if val not in ramp_names:
                        errors.append(
                            f"settings.{key}: must be one of "
                            f"[{', '.join(sorted(ramp_names))}], got {val!r}"
                        )
                elif key in ("5h_display", "7d_display", "ctx_display"):
                    if val not in display_modes:
                        errors.append(
                            f"settings.{key}: must be one of "
                            f"[number, vertical, horizontal], got {val!r}"
                        )
                elif key in ("separator", "git_separator", "limits_separator"):
                    if not isinstance(val, str):
                        errors.append(f"settings.{key}: must be a string")

    slots = config.get("slots")
    if slots is not None:
        if not isinstance(slots, list):
            errors.append("slots: must be an array")
        else:
            provider_names = set(PROVIDERS.keys())
            for i, item in enumerate(slots):
                items = item if isinstance(item, list) else [item]
                for j, slot in enumerate(items):
                    prefix = f"slots[{i}][{j}]" if isinstance(item, list) else f"slots[{i}]"
                    if not isinstance(slot, dict):
                        errors.append(f"{prefix}: must be an object")
                        continue
                    for fld in slot:
                        if fld not in _VALID_SLOT_KEYS:
                            errors.append(f"{prefix}: unknown field '{fld}'")
                    has_provider = "provider" in slot
                    has_command = "command" in slot
                    if not has_provider and not has_command:
                        errors.append(f"{prefix}: must have 'provider' or 'command'")
                    elif has_provider and has_command:
                        errors.append(f"{prefix}: cannot have both 'provider' and 'command'")
                    if has_provider and slot["provider"] not in provider_names:
                        errors.append(
                            f"{prefix}: unknown provider '{slot['provider']}', "
                            f"valid: [{', '.join(sorted(provider_names))}]"
                        )
                    if has_command and not isinstance(slot["command"], str):
                        errors.append(f"{prefix}.command: must be a string")
                    ttl = slot.get("ttl")
                    if ttl is not None and not isinstance(ttl, (int, float)):
                        errors.append(f"{prefix}.ttl: must be a number")
                    enabled = slot.get("enabled")
                    if enabled is not None and not isinstance(enabled, bool):
                        if isinstance(enabled, list):
                            _validate_slot_show(enabled, slot, has_provider, prefix, errors)
                        else:
                            errors.append(f"{prefix}.enabled: must be a boolean or array")

    return errors


# --- config loading ----------------------------------------------------------

def _build_ansi(entry: dict) -> str:
    """Build ANSI escape string from a theme config entry dict."""
    parts: list[str] = []
    for attr in entry.get("attrs", []):
        sgr = ATTR_SGR.get(attr)
        if sgr:
            parts.append(sgr)
    fg = entry.get("fg")
    if fg is not None:
        parts.append(fg256(fg))
    bg = entry.get("bg")
    if bg is not None:
        parts.append(bg256(bg))
    return "".join(parts)


def build_style(entry: "ThemeEntry", extra: str = "") -> str:
    """Build ANSI escape string from a ThemeEntry (delegates to _build_ansi)."""
    d: dict = {}
    if entry.attrs:
        d["attrs"] = entry.attrs
    if entry.fg is not None:
        d["fg"] = entry.fg
    if entry.bg is not None:
        d["bg"] = entry.bg
    result = _build_ansi(d)
    if extra:
        result += extra
    return result


def _load_theme_config() -> list[dict]:
    """Load theme overrides from config file into T class. Return slots config."""
    global SEP_GIT, SEP_LIMITS, SEP_EXTRA

    if not CONFIG_FILE.exists():
        return list(DEFAULT_SLOTS)
    config = _load_json_file(CONFIG_FILE, fatal=True)

    errors = _validate_config(config)
    if errors:
        msg = "  ".join(f"config: {e}" for e in errors)
        print(f"\033[31m{msg}  Fix: {CONFIG_FILE}\033[0m")
        return list(DEFAULT_SLOTS)

    # apply theme token overrides
    theme = config.get("theme", {})
    for key, entry in theme.items():
        if isinstance(entry, dict) and hasattr(T, key):
            setattr(T, key, _build_ansi(entry))

    # read settings
    settings = config.get("settings", {})

    for prefix in ("5h", "7d", "ctx"):
        name = settings.get(f"{prefix}_ramp")
        if name and name in RAMP_PRESETS:
            INDICATOR_CONFIG[prefix]["ramp"] = RAMP_PRESETS[name]
        mode = settings.get(f"{prefix}_display")
        if mode in ("number", "vertical", "horizontal"):
            INDICATOR_CONFIG[prefix]["display"] = mode

    SEP_EXTRA = _load_separator(settings, "separator")
    SEP_GIT = _load_separator(settings, "git_separator")
    SEP_LIMITS = _load_separator(settings, "limits_separator")

    return config.get("slots", list(DEFAULT_SLOTS))


# --- helpers -----------------------------------------------------------------

def run(cmd: list[str], *, cwd: str | None = None, timeout: float = TIMEOUT_SUBPROCESS) -> str | None:
    """Run a subprocess, return stdout or None on any failure."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        if r.returncode == 0:
            return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass  # command unavailable or timed out — caller gets None
    return None


def osc8_link(url: str, text: str) -> str:
    """OSC 8 terminal hyperlink."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


_DB_LOCK = threading.Lock()
_CON: sqlite3.Connection | None = None
_DB_ERROR: str | None = None


def _db() -> sqlite3.Connection:
    """Return singleton cache DB connection (lazy init, WAL mode)."""
    global _CON, _DB_ERROR
    if _CON is None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CON = sqlite3.connect(str(CACHE_DB), timeout=2, check_same_thread=False)
            _CON.execute("PRAGMA journal_mode=WAL")
            _CON.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(key TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}',"
                " updated_at REAL NOT NULL DEFAULT 0,"
                " cooldown_until REAL NOT NULL DEFAULT 0)"
            )
        except (sqlite3.Error, OSError) as exc:
            _DB_ERROR = str(exc)
            raise
    return _CON


def cache_get(key: str) -> tuple[str | None, float, float]:
    """Return (data, updated_at, cooldown_until) for a cache key."""
    with _DB_LOCK:
        try:
            con = _db()
            row = con.execute(
                "SELECT data, updated_at, cooldown_until FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return row[0], row[1], row[2]
        except sqlite3.Error:
            pass  # cache miss — return defaults below
    return None, 0.0, 0.0




def cache_get_raw(key: str) -> str | None:
    """Return just the data string for a cache key (first element of cache_get tuple)."""
    raw, _, _ = cache_get(key)
    return raw


def _rainbow_next_phase(step: float = 1 / 14) -> float:
    """Read rainbow phase from cache, advance by step, persist, return new value."""
    with _DB_LOCK:
        try:
            con = _db()
            con.execute(
                "INSERT OR IGNORE INTO cache (key, data, updated_at, cooldown_until) "
                "VALUES ('rainbow_phase', '0.0', 0, 0)"
            )
            row = con.execute("SELECT data FROM cache WHERE key = 'rainbow_phase'").fetchone()
            phase = float(row[0]) if row else 0.0
            phase = (phase + step) % 1.0
            con.execute("UPDATE cache SET data = ? WHERE key = 'rainbow_phase'", (str(phase),))
            con.commit()
            return phase
        except (sqlite3.Error, ValueError, TypeError):
            return 0.0  # DB or parse error — use default phase


def _safe_json_loads(raw: str, default=None):
    """Parse JSON string, returning default on failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _load_json_file(path: Path, *, fatal: bool = False) -> dict | None:
    """Read and parse a JSON file. If fatal=True, print error and exit(1). Otherwise return None on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        if fatal:
            print(f"config: failed to parse JSON: {exc}", file=sys.stderr)
            print(f"Fix: {path}", file=sys.stderr)
            sys.exit(1)
        return None


def _try_claim_refresh(key: str, ttl: int) -> bool:
    """Atomically check staleness + cooldown and claim refresh if needed.

    Returns True if this caller won the claim (should fire bg refresh).
    Returns False if cache is fresh, cooldown is active, or another
    process claimed first (race-free via single UPDATE with WHERE).
    """
    with _DB_LOCK:
        try:
            con = _db()
            now = time.time()
            con.execute(
                "INSERT OR IGNORE INTO cache (key, data, updated_at, cooldown_until) "
                "VALUES (?, '{}', 0, 0)", (key,))
            cur = con.execute(
                "UPDATE cache SET cooldown_until = ? "
                "WHERE key = ? AND (? - updated_at) >= ? AND cooldown_until <= ?",
                (now + ERROR_COOLDOWN_DEFAULT, key, now, ttl, now))
            con.commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            if _CON is not None:
                try:
                    _CON.rollback()
                except sqlite3.Error:
                    pass
            return False


def _cached_json(key: str, ttl: int, refresh: "callable") -> dict | None:
    """Return parsed JSON from cache, trigger background refresh if stale."""
    if _try_claim_refresh(key, ttl):
        refresh()
    raw = cache_get_raw(key)
    return _safe_json_loads(raw) if raw else None


def read_remote_url(cwd: str) -> str | None:
    """Read origin remote URL from .git/config — no subprocess."""
    git_dir = Path(cwd) / ".git"
    try:
        if git_dir.is_file():
            text = git_dir.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir: "):
                git_dir = Path(text[8:])
                if not git_dir.is_absolute():
                    git_dir = (Path(cwd) / git_dir).resolve()
        config = (git_dir / "config").read_text(encoding="utf-8")
    except OSError:
        return None

    in_origin = False
    for line in config.splitlines():
        s = line.strip()
        if s == '[remote "origin"]':
            in_origin = True
        elif s.startswith("["):
            in_origin = False
        elif in_origin and s.startswith("url = "):
            return s[6:]
    return None


# --- color ramp --------------------------------------------------------------

def _rgb_cube(r: int, g: int, b: int) -> int:
    """Convert RGB cube coords (0-5 each) to 256-color index."""
    return 16 + 36 * r + 6 * g + b


def _ramp_lerp(t: float, c_lo: int, c_hi: int) -> int:
    """Interpolate between two 256-color RGB cube indices. Returns color index."""
    t = max(0.0, min(1.0, t))
    lr, lg, lb = (c_lo - 16) // 36, ((c_lo - 16) % 36) // 6, (c_lo - 16) % 6
    hr, hg, hb = (c_hi - 16) // 36, ((c_hi - 16) % 36) // 6, (c_hi - 16) % 6
    r = max(0, min(5, round(lr + t * (hr - lr))))
    g = max(0, min(5, round(lg + t * (hg - lg))))
    b = max(0, min(5, round(lb + t * (hb - lb))))
    return _rgb_cube(r, g, b)


def _ramp(t: float, endpoints: tuple[int, int]) -> str:
    """Interpolate between two 256-color indices, return ANSI fg escape."""
    return fg256(_ramp_lerp(t, *endpoints))


def _multi_ramp_color(pct: float, waypoints: list[tuple[float, int]]) -> int:
    """Piecewise-linear color ramp across multiple waypoints, returns color index."""
    if pct <= waypoints[0][0]:
        return waypoints[0][1]
    if pct >= waypoints[-1][0]:
        return waypoints[-1][1]
    for i in range(len(waypoints) - 1):
        p0, c0 = waypoints[i]
        p1, c1 = waypoints[i + 1]
        if pct <= p1:
            t = (pct - p0) / (p1 - p0) if p1 > p0 else 0.0
            return _ramp_lerp(t, c0, c1)
    return waypoints[-1][1]


def _multi_ramp(pct: float, waypoints: list[tuple[float, int]]) -> str:
    """Piecewise-linear color ramp, returns ANSI fg escape."""
    return fg256(_multi_ramp_color(pct, waypoints))


def _srgb_gamma(x: float) -> float:
    """Linear → sRGB gamma correction."""
    return x * 12.92 if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055


def _oklch_to_rgb(L: float, C: float, h_deg: float) -> tuple[int, int, int]:
    """OKLCH → sRGB (clamped to 0-255). Pure math, no dependencies."""
    h_rad = math.radians(h_deg)
    a = C * math.cos(h_rad)
    b = C * math.sin(h_rad)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3
    r_lin = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g_lin = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b_lin = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return tuple(max(0, min(255, round(_srgb_gamma(c) * 255))) for c in (r_lin, g_lin, b_lin))


def _oklch_ramp(t: float, start: tuple, end: tuple, *, fixed_L: bool = False) -> str:
    """Interpolate in OKLCH, return fg_rgb escape.

    start/end: (L, C, H) tuples
    fixed_L=True: keep L constant at start[0] (uniform brightness for limit bars)
    fixed_L=False: interpolate L too (brightness ramps with hue for pace)
    """
    t = max(0.0, min(1.0, t))
    L0, C0, H0 = start
    L1, C1, H1 = end
    L = L0 if fixed_L else L0 + t * (L1 - L0)
    C = C0 + t * (C1 - C0)
    # Shortest-path hue interpolation
    dh = (H1 - H0 + 180) % 360 - 180
    H = (H0 + t * dh) % 360
    r, g, b = _oklch_to_rgb(L, C, H)
    return fg_rgb(r, g, b)


def _pace_delta_color(delta: float) -> str:
    """Pace delta color: log-scaled OKLCH teal (under budget) or orange (over budget)."""
    magnitude = min(abs(delta), PACE_COLOR_MAX_DELTA)
    t = math.log1p(magnitude) / math.log1p(PACE_COLOR_MAX_DELTA)
    ramp = RAMP_PACE_GOOD if delta >= 0 else RAMP_PACE_BAD
    return _oklch_ramp(t, ramp[0], ramp[1], fixed_L=False)


def _fmt_surplus(days: float) -> str:
    """Format surplus days with adaptive precision up to 8 decimal places.

    |days| >= 1        → integer:   +2d, -1d
    |days| >= 0.1      → 1 decimal: +0.5d
    |days| >= 0.01     → 2 decimals: +0.05d
    ...
    |days| >= 1e-8     → 8 decimals: +0.00000001d
    0                  → 0d
    """
    a = abs(days)
    if a == 0:
        return "0d"
    if a >= 1:
        return f"{round(days):+d}d"
    for decimals in range(1, 9):
        if a >= 10 ** -decimals:
            formatted = f"{days:+.{decimals}f}".rstrip("0").rstrip(".")
            return f"{formatted}d"
    return f"{days:+.8f}d"


# --- bar rendering -----------------------------------------------------------

def _resolve_bar_bg(bar_bg: str | None) -> str:
    """Return explicit bar_bg or fall back to T.lim_bar_bg."""
    return bar_bg if bar_bg is not None else T.lim_bar_bg


def _bar(pct: float, width: int = LIMITS_BAR_WIDTH, *, ramp: list, bar_bg: str | None = None) -> str:
    """Progress bar on dark bg, colored by ramp. bar_bg defaults to T.lim_bar_bg."""
    bg = _resolve_bar_bg(bar_bg)
    clamped = max(0.0, min(100.0, pct))
    total = round(clamped / 100 * width * 8)
    total = max(1 if clamped > 0 else 0, min(width * 8, total))
    full = total // 8
    frac = total % 8
    empty = width - full - (1 if frac else 0)
    color = _multi_ramp(clamped, ramp)
    filled = f"{bg}{color}{'█' * full}{_BAR_EIGHTHS[frac] if frac else ''}{T.R}"
    bg_empty = f"{bg}{' ' * empty}{T.R}" if empty else ""
    return f"{filled}{bg_empty}"


def _vbar(pct: float, *, ramp: list, bar_bg: str | None = None) -> str:
    """Single-character vertical progress bar (bottom→up), colored.

    At high fill (idx 6-7) the background switches to bright white so the
    thin remaining gap is clearly distinguishable from a full bar.
    """
    clamped = max(0.0, min(100.0, pct))
    idx = round(clamped / 100 * 8)
    idx = max(1 if clamped > 0 else 0, min(8, idx))
    bg = bg256(250) if idx in (6, 7) else _resolve_bar_bg(bar_bg)
    color = _multi_ramp(clamped, ramp)
    return f"{bg}{color}{_VBAR_EIGHTHS[idx]}{T.R}"


def _render_indicator(pct: float, ramp: list, display: str, *, bar_bg: str | None = None) -> str:
    """Render percentage as horizontal bar, vertical bar, or number."""
    if display == "horizontal":
        return _bar(pct, ramp=ramp, bar_bg=bar_bg)
    if display == "number":
        return f"{_multi_ramp(pct, ramp)}{pct:.0f}%{T.R}"
    return _vbar(pct, ramp=ramp, bar_bg=bar_bg)


def _render_indicator_for_prefix(pct: float, prefix: str) -> str:
    """Render indicator using INDICATOR_CONFIG for the given prefix (5h/7d/ctx)."""
    cfg = INDICATOR_CONFIG[prefix]
    return _render_indicator(pct, cfg["ramp"], cfg["display"])


def _format_limit_window_for_prefix(utilization: float, resets_at: str, prefix: str) -> str:
    """Format one limit window using INDICATOR_CONFIG for the given prefix."""
    cfg = INDICATOR_CONFIG[prefix]
    return _format_limit_window(utilization, resets_at, prefix,
                                ramp=cfg["ramp"], display=cfg["display"])


# --- gh availability ---------------------------------------------------------

def _refresh_gh_available_subprocess() -> None:
    """Fire-and-forget background refresh of gh availability."""
    _bg_refresh(
        imports="import shutil, subprocess",
        payload=r"""
    if shutil.which("gh") is None:
        _w('{"status": "no-gh"}')
        sys.exit(0)
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True, timeout=5,
    )
    _w('{"status": "ok"}' if result.returncode == 0 else '{"status": "no-auth"}')
""",
        cache_key="gh_available",
    )


def check_gh_available() -> str:
    """Return 'ok', 'no-gh', 'no-auth', or 'unknown'. Never blocks on network."""
    cache = _cached_json("gh_available", GH_CHECK_TTL, _refresh_gh_available_subprocess)
    if not cache:
        return "unknown"
    return cache.get("status", "unknown")


# --- directory name ----------------------------------------------------------

def get_dir_name(current_dir: str) -> str:
    """Return 'parent/current/' with parent truncated."""
    p = Path(current_dir)
    current = p.name or str(p)
    parent = p.parent.name
    if parent and parent != current:
        if len(parent) > PARENT_DIR_MAX_LEN:
            parent = parent[: PARENT_DIR_MAX_LEN - 1] + "…"
        return f"{T.dir_parent}{parent}/{T.R}{T.dir_name}{current}/{T.R}"
    return f"{T.dir_name}{current}/{T.R}"


# --- git info ----------------------------------------------------------------

def _refresh_git_cache_subprocess(cwd: str, git_key: str) -> None:
    """Fire-and-forget background refresh of git status cache."""
    _bg_refresh(
        imports="import json, subprocess, re",
        payload=r"""
    CWD = sys.argv[3]
    TIMEOUT = """ + str(TIMEOUT_GIT) + r"""
    out = subprocess.run(
        ["git", "-C", CWD, "--no-optional-locks", "status", "--porcelain=v1", "--branch"],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if out.returncode != 0:
        _w('{"branch": ""}')
        sys.exit(0)
    lines = out.stdout.split("\n")
    branch = ""
    ahead = ""
    behind = ""
    dirty = staged = untracked = False
    if lines and lines[0].startswith("## "):
        header = lines[0]
        bp = header[3:]
        if "..." in bp:
            branch = bp.split("...")[0]
        elif " " in bp:
            branch = bp.split(" ")[0]
        else:
            branch = bp
        if branch in ("HEAD", "No"):
            branch = ""
        m = re.search(r"ahead (\d+)", header)
        if m:
            ahead = m.group(1)
        m = re.search(r"behind (\d+)", header)
        if m:
            behind = m.group(1)
    for line in lines[1:]:
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x in "MADRC":
            staged = True
        if y in "MD":
            dirty = True
        if x == "?" and y == "?":
            untracked = True
    _w(json.dumps({"branch": branch, "dirty": dirty, "staged": staged,
                    "untracked": untracked, "ahead": ahead, "behind": behind}))
""",
        cache_key=git_key,
        extra_argv=(cwd,),
    )


def get_git_info(cwd: str) -> tuple[str, str]:
    """Return (branch, status_indicators) from cache, trigger bg refresh if stale."""
    git_key = f"git:{cwd}"

    def _refresh():
        _refresh_git_cache_subprocess(cwd, git_key)

    data = _cached_json(git_key, GIT_CACHE_TTL, _refresh)
    if not data or not data.get("branch"):
        return "", ""

    branch = data["branch"]
    parts: list[str] = []
    if data.get("dirty"):
        parts.append(f"{T.git_dirty}*{T.R}")
    if data.get("staged"):
        parts.append(f"{T.git_staged}+{T.R}")
    if data.get("untracked"):
        parts.append(f"{T.git_untracked}?{T.R}")
    if data.get("ahead"):
        parts.append(f"{T.git_ahead}↑{T.R}")
    if data.get("behind"):
        parts.append(f"{T.git_behind}↓{T.R}")

    return branch, "".join(parts)


# --- background refresh ------------------------------------------------------

_BG_SCRIPT = r"""
import os, sys, sqlite3, time
from pathlib import Path
__IMPORTS__
DB = Path(sys.argv[1])
KEY = sys.argv[2]
DB.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(str(DB), timeout=5)
con.execute("PRAGMA journal_mode=WAL")
con.execute(
    "CREATE TABLE IF NOT EXISTS cache "
    "(key TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}',"
    " updated_at REAL NOT NULL DEFAULT 0,"
    " cooldown_until REAL NOT NULL DEFAULT 0)")
con.commit()
def _w(data):
    con.execute(
        "INSERT OR REPLACE INTO cache (key, data, updated_at, cooldown_until)"
        " VALUES (?, ?, ?, 0)", (KEY, data, time.time()))
    con.commit()
def _cooldown(seconds=0):
    cd = time.time() + (seconds if seconds > 0 else """ + str(ERROR_COOLDOWN_DEFAULT) + r""")
    con.execute(
        "INSERT OR REPLACE INTO cache (key, data, updated_at, cooldown_until)"
        " VALUES (?, COALESCE((SELECT data FROM cache WHERE key = ?), '{}'),"
        " COALESCE((SELECT updated_at FROM cache WHERE key = ?), 0), ?)",
        (KEY, KEY, KEY, cd))
    con.commit()
try:
__PAYLOAD__
except Exception:  # broad: bg script must not crash — set cooldown instead
    _cooldown()
finally:
    try:
        con.commit()
    except Exception:
        pass  # best-effort final commit before exit
    con.close()
"""


def _bg_refresh(*, imports: str, payload: str, cache_key: str,
                extra_argv: tuple = (), stdin_data: str | None = None) -> None:
    """Fire-and-forget background subprocess with SQLite-based locking."""
    script = _BG_SCRIPT.replace("__IMPORTS__", imports).replace("__PAYLOAD__", payload)
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(CACHE_DB), cache_key, *extra_argv],
        start_new_session=True,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if stdin_data is not None:
        try:
            proc.stdin.write(stdin_data.encode())
            proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass


# --- PR status ---------------------------------------------------------------

def _refresh_pr_cache_subprocess() -> None:
    """Fire-and-forget background refresh of PR cache."""
    _bg_refresh(
        imports="import json, subprocess, time",
        payload=r"""
    TIMEOUT = """ + str(TIMEOUT_GH_API) + r"""
    gql = subprocess.run(
        ["gh", "api", "graphql", "-f", "query=" + '''
        query {
            search(query: "is:open is:pr author:@me", type: ISSUE, first: """ + str(GH_PR_FETCH_LIMIT) + r""") {
                nodes {
                    ... on PullRequest {
                        number
                        repository { nameWithOwner }
                        url
                        headRefName
                        commits(last: 1) {
                            nodes {
                                commit {
                                    statusCheckRollup {
                                        state
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        '''.strip()],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if gql.returncode != 0:
        _cooldown()
        sys.exit(0)
    prs = json.loads(gql.stdout)
    unread = 0
    try:
        notif = subprocess.run(
            ["gh", "api", "notifications"], capture_output=True, text=True, timeout=TIMEOUT,
        )
        if notif.returncode == 0:
            for n in json.loads(notif.stdout):
                if (n.get("subject", {}).get("type") in ("PullRequest", "Issue")
                        and n.get("unread")
                        and n.get("reason") in {"comment", "mention", "author", "review_requested", "assign"}):
                    unread += 1
    except Exception:
        pass  # gh notifications fetch failed — skip unread count
    _w(json.dumps({"prs": prs, "unread_count": unread, "updated_at": int(time.time())}))
""",
        cache_key="pr",
    )


def get_pr_status() -> "PrStatus | None":
    """Return structured PR status data from cache, trigger refresh if stale."""
    gh = check_gh_available()
    if gh != "ok":
        return None

    cache = _cached_json("pr", API_CACHE_TTL, _refresh_pr_cache_subprocess)
    if not cache:
        return None

    nodes = cache.get("prs", {}).get("data", {}).get("search", {}).get("nodes", [])
    if not nodes:
        return None

    dots_red: list[str] = []
    dots_pending: list[str] = []
    dots_green: list[str] = []
    dots_gray: list[str] = []

    for pr in nodes:
        url = pr.get("url", "")
        commits = pr.get("commits", {}).get("nodes", [])
        state = "UNKNOWN"
        if commits:
            rollup = commits[0].get("commit", {}).get("statusCheckRollup")
            if rollup:
                state = rollup.get("state", "UNKNOWN")

        dot = osc8_link(url, PR_DOT) if url else PR_DOT
        if state in ("FAILURE", "ERROR"):
            dots_red.append(dot)
        elif state in ("PENDING", "EXPECTED"):
            dots_pending.append(dot)
        elif state == "SUCCESS":
            dots_green.append(dot)
        else:
            dots_gray.append(dot)

    unread = cache.get("unread_count", 0)
    return PrStatus(dots_red, dots_pending, dots_green, dots_gray, unread)


def _format_pr_dots(status: "PrStatus", include_pr: bool, include_notif: bool) -> str:
    """Format PrStatus into a colored ANSI string based on requested sections."""
    parts: list[str] = []

    if include_pr:
        if status.dots_red:
            parts.append(f"{T.err}{''.join(status.dots_red)}{T.R}")
        if status.dots_pending:
            parts.append(f"{T.wait}{''.join(status.dots_pending)}{T.R}")
        if status.dots_green:
            parts.append(f"{T.ok}{''.join(status.dots_green)}{T.R}")
        if status.dots_gray:
            parts.append(f"{T.none}{''.join(status.dots_gray)}{T.R}")

    if include_notif and status.unread_count > 0:
        sep = " " if parts else ""
        notif_text = osc8_link("https://github.com/notifications", f"\U0001f4ac{status.unread_count}")
        parts.append(f"{sep}{T.notif}{notif_text}{T.R}")

    return "".join(parts)


# --- CI status ---------------------------------------------------------------

def _ci_from_pr_cache(branch: str, actions_url: str = "") -> str | None:
    """Try to get CI status from PR cache if branch matches an open PR."""
    raw = cache_get_raw("pr")
    cache = _safe_json_loads(raw) if raw else None
    if not cache:
        return None

    for pr in cache.get("prs", {}).get("data", {}).get("search", {}).get("nodes", []):
        if pr.get("headRefName") != branch:
            continue
        commits = pr.get("commits", {}).get("nodes", [])
        if not commits:
            return _format_ci_label("pending", actions_url)
        rollup = commits[0].get("commit", {}).get("statusCheckRollup")
        if not rollup:
            return _format_ci_label("pending", actions_url)
        state = rollup.get("state", "UNKNOWN")
        mapping = {
            "SUCCESS": "success",
            "FAILURE": "failure",
            "ERROR": "failure",
            "PENDING": "pending",
            "EXPECTED": "pending",
        }
        return _format_ci_label(mapping.get(state, ""), actions_url)
    return None


def _parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Parse owner/repo from SSH or HTTPS git remote URL."""
    m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    return (m.group(1), m.group(2)) if m else None


def _refresh_ci_cache_subprocess(owner: str, repo: str, branch: str, ci_key: str) -> None:
    """Fire-and-forget background refresh of CI cache."""
    _bg_refresh(
        imports="import json, subprocess",
        payload=r"""
    TIMEOUT = """ + str(TIMEOUT_GH_API) + r"""
    out = subprocess.run(
        ["gh", "api", f"repos/{sys.argv[3]}/{sys.argv[4]}/commits/{sys.argv[5]}/check-runs",
         "--jq", ".check_runs"],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if out.returncode != 0:
        _cooldown()
        sys.exit(0)
    runs = json.loads(out.stdout) if out.stdout.strip() else []
    if not runs:
        _w(json.dumps({"conclusion": "none"}))
        sys.exit(0)
    conclusions = [r.get("conclusion") for r in runs]
    statuses = [r.get("status") for r in runs]
    if any(c in ("failure", "timed_out", "cancelled", "action_required") for c in conclusions):
        result = "failure"
    elif all(c == "success" for c in conclusions if c is not None) and all(s == "completed" for s in statuses):
        result = "success"
    elif any(s in ("queued", "in_progress") for s in statuses):
        result = "pending"
    else:
        result = "unknown"
    _w(json.dumps({"conclusion": result}))
""",
        cache_key=ci_key,
        extra_argv=(owner, repo, branch),
    )


def get_ci_status(cwd: str, branch: str) -> str:
    """Return CI status label for the current branch."""
    if not branch:
        return ""

    gh = check_gh_available()
    if gh != "ok":
        return ""

    remote_url = read_remote_url(cwd)
    if not remote_url:
        return ""

    parsed = _parse_owner_repo(remote_url)
    if not parsed:
        return ""
    owner, repo = parsed
    actions_url = f"https://github.com/{owner}/{repo}/actions"

    from_pr = _ci_from_pr_cache(branch, actions_url)
    if from_pr is not None:
        return from_pr

    ci_key = f"ci:{owner}_{repo}_{branch}"

    def _refresh():
        _refresh_ci_cache_subprocess(owner, repo, branch, ci_key)

    cache = _cached_json(ci_key, API_CACHE_TTL, _refresh)
    if not cache:
        return ""
    actions_url = f"https://github.com/{owner}/{repo}/actions"
    return _format_ci_label(cache.get("conclusion"), actions_url)


def _format_ci_label(conclusion: str | None, actions_url: str = "") -> str:
    """Format CI conclusion as a colored 'CI' label with optional OSC 8 link."""
    ci_text = "CI"
    if actions_url:
        ci_text = osc8_link(actions_url, ci_text)
    labels = {
        "success": f"{T.ok}{ci_text}{T.R}",
        "failure": f"{T.err}{ci_text}{T.R}",
        "pending": f"{T.wait}{ci_text}{T.R}",
        "none":    f"{T.none}{ci_text}{T.R}",
    }
    return labels.get(conclusion, f"{T.none}{ci_text}{T.R}")


# --- limits provider ---------------------------------------------------------

def _read_oauth_token() -> str | None:
    """Read OAuth access token from Claude credentials file."""
    data = _load_json_file(LIMITS_CREDS_FILE)
    if data is None:
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    return oauth.get("accessToken")


def _parse_iso_utc(raw: str) -> float | None:
    """Parse ISO-8601 timestamp to epoch seconds (stdlib only)."""
    try:
        s = re.sub(r'\.\d+', '', raw)
        s = s.replace("+00:00", "+0000").replace("Z", "+0000")
        if "+" in s[10:]:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
        else:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _format_duration(minutes: int) -> str:
    """Format minutes into compact duration: 4h26m, 3d 2h, 23m, now."""
    if minutes <= 0:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    if hours < 24:
        return f"{hours}h{mins:02d}m" if mins else f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d{rem_hours}h" if rem_hours else f"{days}d"


def _7d_pace_label(utilization: float, resets_at: str) -> str:
    """Return pace label for 7d window assuming WORK_DAYS budget.

    After WORK_DAYS have elapsed, pace metrics are meaningless — show weekend mode instead.
    """
    reset_epoch = _parse_iso_utc(resets_at)
    if reset_epoch is None:
        return ""
    hours_elapsed = max(0.0, (time.time() - (reset_epoch - LIMITS_WINDOW_SECONDS)) / 3600)
    days_elapsed = hours_elapsed / 24
    if days_elapsed >= WORK_DAYS:
        return f"{_rainbow('no pace police', hue_start=_rainbow_next_phase())}{T.R}"
    expected = min(hours_elapsed / LIMITS_PACE_BUDGET_HOURS * 100.0, 100.0)
    if expected < LIMITS_PACE_MIN_EXPECTED:
        return ""
    delta = expected - utilization
    dc = _pace_delta_color(delta)
    label = next(name for threshold, name in PACE_SCALE if delta <= threshold)
    surplus_str = ""
    if utilization > 0 and days_elapsed >= 0.5:
        daily_rate = utilization / days_elapsed
        surplus_str = f" {_fmt_surplus((100 / daily_rate) - WORK_DAYS)}"
    return f"{dc}{label} {delta:+.0f}%{surplus_str}{T.R}"


def _bar_last_step_threshold(display: str, width: int = LIMITS_BAR_WIDTH) -> float:
    """Return pct threshold above which the bar is stuck on its last visual step.

    vbar: 8 steps  → threshold = 7.5/8 * 100 = 93.75%
    hbar: width*8 steps → threshold = (N-0.5)/N * 100
    """
    n = width * 8 if display == "horizontal" else 8
    return (n - 0.5) / n * 100


def _format_limit_window(utilization: float, resets_at: str, label: str,
                         *, ramp: list, display: str) -> str:
    """Format one limit window: '5h▅' or '5h [█96% 2d4h]' (compact)."""
    pct = max(0.0, min(100.0, utilization))
    indicator = _render_indicator(pct, ramp, display)

    pct_str = ""
    if pct >= _bar_last_step_threshold(display):
        pct_str = f"{pct:.0f}%"

    time_str = ""
    if pct >= LIMITS_COUNTDOWN_THRESHOLD:
        reset_epoch = _parse_iso_utc(resets_at)
        if reset_epoch is not None:
            remaining_min = max(0, int((reset_epoch - time.time()) / 60))
            time_str = _format_duration(remaining_min)

    extras = " ".join(filter(None, [pct_str, time_str]))
    lc = T.dir_parent  # label color — used for brackets too
    if extras:
        inner = f"{indicator}{T.lim_time}{extras}{T.R}"
        return f"{lc}{label}{T.R} {lc}[{T.R}{inner}{lc}]{T.R}"
    return f"{lc}{label}{T.R} {indicator}"


def _refresh_limits_cache_subprocess() -> None:
    """Fire-and-forget background refresh of limits cache."""
    token = _read_oauth_token()
    if not token:
        return
    _bg_refresh(
        imports="import json\nfrom urllib.request import Request, urlopen\nfrom urllib.error import HTTPError",
        payload=r"""
    BACKOFF_MIN = """ + str(LIMITS_COOLDOWN_MIN) + r"""
    BACKOFF_MAX = """ + str(LIMITS_COOLDOWN_MAX) + r"""
    req = Request(sys.argv[3])
    req.add_header("Authorization", f"Bearer {sys.argv[4]}")
    req.add_header("anthropic-beta", "oauth-2025-04-20")
    try:
        resp = urlopen(req, timeout=""" + str(LIMITS_HTTP_TIMEOUT) + r""")
    except HTTPError as e:
        if e.code == 429:
            retry = int(e.headers.get("Retry-After", 0))
            if retry > 0:
                _cooldown(retry)
            else:
                # Exponential backoff via fail counter stored in cache data
                row = con.execute(
                    "SELECT data FROM cache WHERE key = ?", (KEY,)
                ).fetchone()
                fails = 0
                if row and row[0]:
                    try:
                        fails = json.loads(row[0]).get("_429_fails", 0)
                    except Exception:
                        pass
                fails += 1
                backoff = min(BACKOFF_MAX, BACKOFF_MIN * (2 ** (fails - 1)))
                # Preserve fail counter in cache data (without updating updated_at)
                con.execute(
                    "UPDATE cache SET data = ? WHERE key = ?",
                    (json.dumps({"_429_fails": fails}), KEY))
                _cooldown(backoff)
            sys.exit(0)
        raise
    _w(json.dumps(json.loads(resp.read())))
""",
        cache_key="limits",
        extra_argv=(LIMITS_API_URL, token),
    )


def _build_limits_bars(data: dict, sections: set[str]) -> list[str]:
    """Build limit bars from cached data."""
    bars: list[str] = []
    five = data.get("five_hour", {})
    seven = data.get("seven_day", {})
    now = time.time()
    r5 = _parse_iso_utc(five.get("resets_at", ""))
    r7 = _parse_iso_utc(seven.get("resets_at", ""))
    stale5 = r5 is not None and now > r5
    stale7 = r7 is not None and now > r7
    u5 = five.get("utilization", 0)
    u7 = seven.get("utilization", 0)

    # When 7d is maxed and not stale, only show 7d (5h is irrelevant)
    if u7 >= 100 and not stale7:
        if "7d" in sections:
            bars.append(_format_limit_window_for_prefix(u7, seven.get("resets_at", ""), "7d"))
        return bars

    if "5h" in sections:
        if stale5:
            bars.append(f"{T.dir_parent}5h{T.R} {T.warn}stale{T.R}")
        else:
            bars.append(_format_limit_window_for_prefix(u5, five.get("resets_at", ""), "5h"))

    if "7d" in sections:
        if stale7:
            bars.append(f"{T.dir_parent}7d{T.R} {T.warn}stale{T.R}")
        else:
            bars.append(_format_limit_window_for_prefix(u7, seven.get("resets_at", ""), "7d"))

    return bars


def provider_limits(input_json: str, cwd: str, show: list[str] | None = None) -> str:
    """Built-in provider: API usage limits (5h/7d windows) + context window."""
    if show is not None and not show:
        return ""
    sections = set(show) if show else {"5h", "7d", "ctx"}
    bars: list[str] = []

    if "5h" in sections or "7d" in sections:
        data = _cached_json("limits", LIMITS_CACHE_TTL, _refresh_limits_cache_subprocess)
        has_data = data and "five_hour" in data
        if has_data:
            bars.extend(_build_limits_bars(data, sections))
        else:
            # Show retry countdown only for long cooldowns (429 backoff),
            # not for the brief 30s claim-cooldown during normal bg refresh
            _, _, cooldown_until = cache_get("limits")
            remaining = cooldown_until - time.time() if cooldown_until else 0
            if remaining > ERROR_COOLDOWN_DEFAULT:
                eta = _format_duration(int(remaining / 60)) if remaining >= 60 else f"{int(remaining)}s"
                bars.append(f"{T.warn}retry in {eta}{T.R}")

    if "ctx" in sections:
        try:
            inp = json.loads(input_json)
            remaining = inp.get("context_window", {}).get("remaining_percentage")
            if remaining is not None:
                # Normalize: treat autocompact threshold as the "full" mark.
                # usable_remaining = (remaining - dead_zone) / usable_range * 100
                # used = 100 - usable_remaining  (clamped to [0, 100])
                dead_zone = _ctx_autocompact_remaining()
                usable_range = 100.0 - dead_zone
                usable_remaining = (remaining - dead_zone) / usable_range * 100.0
                used = max(0.0, min(100.0, 100.0 - usable_remaining))
                ctx_bar = _render_indicator_for_prefix(used, "ctx")
                if used >= CTX_IRRITATE_USER_ABOVE:
                    bars.append(f"{T.dir_parent}ctx{T.R} {ctx_bar} {BLINK}☠{RESET} ")
                else:
                    bars.append(f"{T.dir_parent}ctx{T.R} {ctx_bar}")
            else:
                bars.append(f"{T.dir_parent}ctx{T.R} {DIM}N/A{T.R}")
        except (json.JSONDecodeError, KeyError, TypeError):
            bars.append(f"{T.dir_parent}ctx{T.R} {DIM}N/A{T.R}")

    return SEP_LIMITS.join(bars)


def provider_vibes(_input_json: str, _cwd: str, *, show=None) -> str:
    """Built-in provider: 7d pace label (vibes indicator)."""
    data = _cached_json("limits", LIMITS_CACHE_TTL, _refresh_limits_cache_subprocess)
    if not data or "seven_day" not in data:
        return ""
    _, updated_at, _ = cache_get("limits")
    if not updated_at or (time.time() - updated_at) > VIBES_MAX_DATA_AGE:
        return ""
    seven = data["seven_day"]
    r7 = _parse_iso_utc(seven.get("resets_at", ""))
    if r7 is not None and time.time() > r7:
        return ""
    return _7d_pace_label(seven.get("utilization", 0), seven.get("resets_at", ""))


# --- slot providers ----------------------------------------------------------

def provider_path(_input_json: str, cwd: str, *, show=None) -> str:
    """Built-in provider: directory name (parent/current/)."""
    return get_dir_name(cwd)


def provider_git(input_json: str, cwd: str, show: list[str] | None = None) -> str:
    """Built-in provider: git branch + status + CI + PR."""
    if show is not None and not show:
        return ""
    sections = set(show) if show else {"branch", "ci", "pr", "notif"}

    branch, git_status = "", ""
    if "branch" in sections or "ci" in sections:
        branch, git_status = get_git_info(cwd)

    pr_data = None
    if "pr" in sections or "notif" in sections:
        pr_data = get_pr_status()

    ci_label = ""
    if branch and "ci" in sections:
        ci_label = get_ci_status(cwd, branch)

    line = ""
    if "branch" in sections and branch:
        line = f"{T.branch_sign}{BRANCH_LABEL}{T.R}{T.branch_name}{branch}{T.R}"
        if git_status:
            line += git_status
    if ci_label:
        line += f"{SEP_GIT}{ci_label}" if line else ci_label
    if pr_data:
        include_pr = "pr" in sections
        include_notif = "notif" in sections
        pr_status = _format_pr_dots(pr_data, include_pr, include_notif)
        if pr_status:
            line += f"{SEP_GIT}{pr_status}" if line else pr_status
    return line


PROVIDERS: dict[str, callable] = {
    "path": provider_path,
    "git": provider_git,
    "limits": provider_limits,
    "vibes": provider_vibes,
}

PROVIDER_SECTIONS: dict[str, tuple[str, ...]] = {
    "git": ("branch", "ci", "pr", "notif"),
    "limits": ("5h", "7d", "ctx"),
}


# --- external slot executor --------------------------------------------------

def _refresh_external_slot_subprocess(command: str, input_json: str,
                                      cache_key: str) -> None:
    """Fire-and-forget background refresh of an external slot cache."""
    _bg_refresh(
        imports="import subprocess",
        payload=r"""
    env = {**os.environ, "FORCE_COLOR": "1"}
    r = subprocess.run(
        sys.argv[3], shell=True, input=sys.stdin.read(),
        capture_output=True, text=True, timeout=""" + str(SLOT_TIMEOUT) + r""", env=env,
    )
    if r.returncode == 0 and r.stdout.strip():
        _w(r.stdout.strip())
    elif r.returncode != 0:
        _cooldown()
""",
        cache_key=cache_key,
        extra_argv=(command,),
        stdin_data=input_json,
    )


def _check_command_available(command: str) -> str | None:
    """Return None if command's executable is available, or a placeholder string."""
    parts = command.split()
    if not parts:
        return None
    exe = parts[0]
    exe_path = Path(exe)
    if exe_path.is_absolute():
        found = exe_path.is_file() and os.access(exe_path, os.X_OK)
    else:
        found = shutil.which(exe) is not None
    if found:
        return None
    # Prefer basename of first arg (script) over the interpreter itself
    label = Path(parts[1]).name if len(parts) > 1 else exe_path.name
    return f"{T.warn}[{label}: not found]{T.R}"


def run_external_slot(command: str, input_json: str, ttl: int, cwd_sensitive: bool = False) -> str:
    """Return external slot output from cache, trigger bg refresh if stale."""
    expanded = str(Path(command).expanduser())
    placeholder = _check_command_available(expanded)
    if placeholder is not None:
        return placeholder
    if cwd_sensitive:
        try:
            _cdir = json.loads(input_json).get("workspace", {}).get("current_dir", "")
        except Exception:
            _cdir = ""
        slot_key = f"slot:{hashlib.md5((expanded + _cdir).encode()).hexdigest()}"
    else:
        slot_key = f"slot:{hashlib.md5(expanded.encode()).hexdigest()}"

    if _try_claim_refresh(slot_key, ttl):
        _refresh_external_slot_subprocess(expanded, input_json, slot_key)

    raw = cache_get_raw(slot_key)
    stripped = raw.strip() if raw else ""
    return "" if stripped == "{}" else stripped


# --- slot orchestrator -------------------------------------------------------

def _build_slot_grid(slots: list) -> tuple[list[list[dict]], list[tuple[int, int, dict]]]:
    """Parse slot config into grid layout. Returns (lines, all_widgets)."""
    lines: list[list[dict]] = []
    for slot in slots:
        if isinstance(slot, list):
            enabled = [s for s in slot if s.get("enabled", True)]
            if enabled:
                lines.append(enabled)
        elif slot.get("enabled", True):
            lines.append([slot])

    all_widgets: list[tuple[int, int, dict]] = []
    for li, widgets in enumerate(lines):
        for wi, w in enumerate(widgets):
            all_widgets.append((li, wi, w))

    return lines, all_widgets


_CACHE_FREE_PROVIDERS = frozenset({"path"})


def execute_slots(slots: list, input_json: str, cwd: str) -> list[str]:
    """Execute all slots in parallel, return ordered list of non-empty lines."""
    lines, all_widgets = _build_slot_grid(slots)

    db_err = _DB_ERROR
    db_err_shown = False

    def _run_slot(slot: dict) -> str:
        nonlocal db_err_shown
        provider = slot.get("provider")
        if provider:
            if db_err and provider not in _CACHE_FREE_PROVIDERS:
                if not db_err_shown:
                    db_err_shown = True
                    return f"{T.err}{CACHE_DIR} inaccessible, please delete{T.R}"
                return ""
            func = PROVIDERS.get(provider)
            if func:
                enabled = slot.get("enabled")
                show = enabled if isinstance(enabled, list) else None
                return func(input_json, cwd, show=show)
            return ""
        command = slot.get("command")
        if command:
            ttl = slot.get("ttl", SLOT_CACHE_TTL)
            cwd_sensitive = slot.get("cwd_sensitive", False)
            return run_external_slot(command, input_json, ttl, cwd_sensitive)
        return ""

    grid: list[list[str]] = [[""] * len(ws) for ws in lines]
    with ThreadPoolExecutor(max_workers=min(max(len(all_widgets), 1), 16)) as pool:
        futures = {pool.submit(_run_slot, w): (li, wi)
                   for li, wi, w in all_widgets}
        for future in as_completed(futures):
            li, wi = futures[future]
            try:
                grid[li][wi] = future.result()
            except Exception:
                grid[li][wi] = ""  # slot failed — render as empty

    result: list[str] = []
    for parts in grid:
        joined = SEP_EXTRA.join(p for p in parts if p)
        if joined:
            result.append(joined)
    return result


def render(lines: list[str]) -> str:
    """Join slot output lines."""
    return "\n".join(lines)


# --- theme editor config I/O ------------------------------------------------

def _load_validated_config() -> dict:
    """Read config.json, validate, exit(1) on errors. Return parsed dict."""
    if not CONFIG_FILE.exists():
        return {}
    config = _load_json_file(CONFIG_FILE, fatal=True)

    errors = _validate_config(config)
    if errors:
        msg = "  ".join(f"config: {e}" for e in errors)
        print(f"\033[31m{msg}  Fix: {CONFIG_FILE}\033[0m")
        return {}

    return config


def _theme_from_config(config: dict) -> dict[str, ThemeEntry]:
    """Extract theme entries from validated config."""
    theme = {k: v.copy() for k, v in DEFAULTS.items()}
    for key, val in config.get("theme", {}).items():
        if key in theme and isinstance(val, dict):
            theme[key] = ThemeEntry.from_dict(val)
    return theme


def _settings_from_config(config: dict) -> dict[str, str]:
    """Extract settings from validated config."""
    settings = {s.key: s.default for s in SETTINGS_DEFS}
    for s in SETTINGS_DEFS:
        val = config.get("settings", {}).get(s.key)
        if isinstance(val, str) and val in s.options:
            settings[s.key] = val
    return settings


def save_theme(theme: dict[str, ThemeEntry],
               settings: dict[str, str] | None = None) -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_json_file(CONFIG_FILE) or {} if CONFIG_FILE.exists() else {}

    data: dict = {}

    if "slots" in existing:
        data["slots"] = existing["slots"]

    settings_out: dict = {}
    if settings:
        for s in SETTINGS_DEFS:
            val = settings.get(s.key, s.default)
            if val != s.default:
                settings_out[s.key] = val
    if settings_out:
        data["settings"] = settings_out

    theme_out = {key: entry.to_dict() for key, entry in theme.items()}
    data["theme"] = theme_out

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=".theme.", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_path, str(CONFIG_FILE))
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return str(CONFIG_FILE)


# --- theme editor TUI helpers -----------------------------------------------

def _render_ramp_strip(ramp_name: str, width: int = 20) -> str:
    """Render a horizontal color strip showing the ramp gradient."""
    waypoints = RAMP_PRESETS.get(ramp_name)
    if not waypoints:
        return ""
    bg = T.lim_bar_bg
    parts: list[str] = []
    for i in range(width):
        pct = i / (width - 1) * 100
        c = _multi_ramp_color(pct, waypoints)
        parts.append(f"{bg}{fg256(c)}█{RESET}")
    return "".join(parts)


def _render_demo_number(pct: float, ramp_name: str, width: int = 0) -> str:
    """Render a colored percentage number for demo. Pad to width if given."""
    waypoints = RAMP_PRESETS.get(ramp_name, RAMP_PRESETS[_SETTINGS_DEFAULTS["5h_ramp"]])
    c = _multi_ramp_color(pct, waypoints)
    text = f"{pct:.0f}%"
    if width:
        text = text.rjust(width)
    return f"{fg256(c)}{text}{RESET}"


# --- theme editor: Editor class ----------------------------------------------

class Editor:
    def __init__(self):
        config = _load_validated_config()
        self.theme = _theme_from_config(config)
        self.settings = _settings_from_config(config)
        self._saved_theme = {k: v.copy() for k, v in self.theme.items()}
        self._saved_settings = dict(self.settings)
        self.cursor = 0
        self.clipboard: ThemeEntry | None = None
        self.mode = "nav"
        self.color_cursor = 0
        self.attr_cursor = 0
        self.settings_cursor = 0
        self._anim_pct = 0.0
        self._anim_ascending = True
        self.msg = ""
        self.running = True

    # --- dirty tracking ---

    def _mark_saved(self):
        self._saved_theme = {k: v.copy() for k, v in self.theme.items()}
        self._saved_settings = dict(self.settings)

    def _has_changes(self) -> bool:
        if self.settings != self._saved_settings:
            return True
        for key in self.theme:
            cur, saved = self.theme[key], self._saved_theme[key]
            if cur.fg != saved.fg or cur.bg != saved.bg or cur.attrs != saved.attrs:
                return True
        return False

    def _diff_lines(self) -> list[str]:
        lines: list[str] = []
        for elem in ELEMENTS:
            cur, saved = self.theme[elem.key], self._saved_theme[elem.key]
            diffs: list[str] = []
            if cur.fg != saved.fg:
                diffs.append(f"fg {saved.fg}→{cur.fg}")
            if cur.bg != saved.bg:
                diffs.append(f"bg {saved.bg}→{cur.bg}")
            if cur.attrs != saved.attrs:
                old_a = ",".join(saved.attrs) or "none"
                new_a = ",".join(cur.attrs) or "none"
                diffs.append(f"attrs {old_a}→{new_a}")
            if diffs:
                lines.append(f"  {elem.label} ({elem.key}): {', '.join(diffs)}")
        for sdef in SETTINGS_DEFS:
            cur_val = self.settings.get(sdef.key, sdef.default)
            saved_val = self._saved_settings.get(sdef.key, sdef.default)
            if cur_val != saved_val:
                cur_display = _sep_display_label(sdef.key, cur_val)
                saved_display = _sep_display_label(sdef.key, saved_val)
                lines.append(f"  {sdef.label}: {saved_display}→{cur_display}")
        return lines

    @staticmethod
    def _config_path_display() -> str:
        if CONFIG_FILE.is_relative_to(Path.home()):
            return f"~/{CONFIG_FILE.relative_to(Path.home())}"
        return str(CONFIG_FILE)

    # --- preview rendering ---

    def _styled(self, key: str, text: str) -> str:
        entry = self.theme[key]
        if key == ELEMENTS[self.cursor].key:
            if self.mode in ("fg", "bg"):
                entry = entry.copy()
                if self.mode == "fg":
                    entry.fg = self.color_cursor if self.color_cursor >= 0 else None
                else:
                    entry.bg = self.color_cursor if self.color_cursor >= 0 else None
            elif self.mode == "attr":
                attr_name = ATTRS_AVAILABLE[self.attr_cursor][0]
                entry = entry.copy()
                if attr_name == "none":
                    entry.attrs.clear()
                elif attr_name not in entry.attrs:
                    entry.attrs.append(attr_name)
                else:
                    entry.attrs.remove(attr_name)
        style = build_style(entry)
        return f"{style}{text}{RESET}"

    @staticmethod
    def _visual_len(text: str) -> int:
        n = 0
        for ch in text:
            if unicodedata.east_asian_width(ch) in ("W", "F"):
                n += 2
            else:
                n += 1
        return n

    def _build_preview_line(self, segments: list[tuple[str | None, str]], highlight_key: str) -> tuple[list[str], list[str]]:
        """Build styled preview line and caret indicator from segment list."""
        parts: list[str] = []
        carets: list[str] = []
        for key, text in segments:
            vlen = self._visual_len(text)
            if key is not None:
                parts.append(self._styled(key, text))
                carets.extend(["^" if key == highlight_key else " "] * vlen)
            else:
                parts.append(text)
                carets.extend([" "] * vlen)
        return parts, carets

    def render_preview(self) -> tuple[str, str]:
        cur = ELEMENTS[self.cursor].key
        sep_char = self.settings["separator"]
        git_sep_char = self.settings["git_separator"]

        def _sep_segment(kind: str) -> tuple[str | None, str]:
            ch = sep_char if kind == "sep" else git_sep_char
            return ("sep", f" {ch} ") if ch else (None, " ")

        # Build segments from ELEMENTS order — single source of truth
        segments: list[tuple[str | None, str]] = []
        prev_group = None
        for elem in ELEMENTS:
            # Insert gap before element
            if elem.gap in ("sep", "git_sep"):
                segments.append(_sep_segment(elem.gap))
            elif elem.gap:
                segments.append((None, elem.gap))
            # Lim group content rendered by _append_limits_demo
            if elem.group == "lim":
                continue
            # After CI group ends, inject PR dots block
            if prev_group == "ci" and elem.group != "ci":
                segments.append(_sep_segment("git_sep"))
                for dot_key, dot_text in [("ok", "⁕⁕⁕"), ("err", "⁕"), ("wait", "⁕⁕"), ("none", "⁕")]:
                    segments.append((dot_key, dot_text))
            prev_group = elem.group
            # Sep element renders as the configured separator character
            if elem.key == "sep":
                segments.append(_sep_segment("sep"))
            # warn element: sandwich between OK and ERR semantic labels
            elif elem.key == "warn":
                segments.append(("ok", "OK"))
                segments.append((None, " "))
                segments.append(("err", "ERR"))
                segments.append((None, " "))
                segments.append((elem.key, elem.sample))
            else:
                segments.append((elem.key, elem.sample))

        preview_parts, caret_chars = self._build_preview_line(segments, cur)
        self._append_limits_demo(preview_parts, caret_chars, cur)

        preview = "".join(preview_parts)
        carets = f"{DIM}{''.join(caret_chars)}{RESET}"
        return preview, carets

    def _themed_bar_bg(self) -> str:
        """Resolve bar background ANSI from current theme (with live preview)."""
        entry = self.theme.get("lim_bar_bg")
        if entry is None:
            return T.lim_bar_bg
        bg_val = entry.bg
        if ELEMENTS[self.cursor].key == "lim_bar_bg" and self.mode == "bg":
            bg_val = self.color_cursor if self.color_cursor >= 0 else None
        return bg256(bg_val) if bg_val is not None else T.lim_bar_bg

    def _append_limits_demo(self, parts: list[str], carets: list[str], cur: str):
        bar_bg = self._themed_bar_bg()

        anim = self._is_anim_active()
        p = self._anim_pct
        demos = [
            ("5h", p if anim else 30, None if anim else "4h26m"),
            ("7d", p if anim else 55, None),
            ("ctx", p if anim else 40, None),
        ]
        lim_sep = _get_setting(self.settings, "limits_separator")
        lim_sep_text = f" {lim_sep} " if lim_sep else " "
        for i, (label, pct, time_text) in enumerate(demos):
            if i > 0:
                parts.append(lim_sep_text if not lim_sep else self._styled("sep", lim_sep_text))
                carets.extend([" "] * self._visual_len(lim_sep_text))

            lbl = f"{label} "
            parts.append(self._styled("lim_time", lbl))
            carets.extend(["^" if cur == "lim_time" else " "] * self._visual_len(lbl))

            ramp_name = _get_setting(self.settings, f"{label}_ramp")
            display = _get_setting(self.settings, f"{label}_display")
            if display == "number":
                bar_text = _render_demo_number(pct, ramp_name, width=4)
                bar_vlen = 4
            elif display == "horizontal":
                bar_text = _bar(pct, ramp=RAMP_PRESETS[ramp_name], bar_bg=bar_bg)
                bar_vlen = 5
            else:
                bar_text = _vbar(pct, ramp=RAMP_PRESETS[ramp_name], bar_bg=bar_bg)
                bar_vlen = 1
            parts.append(bar_text)
            carets.extend(["^" if cur == "lim_bar_bg" else " "] * bar_vlen)

            if time_text:
                parts.append(self._styled("lim_time", time_text))
                carets.extend(["^" if cur == "lim_time" else " "] * len(time_text))

    # --- legend ---

    def render_legend(self) -> list[str]:
        elem = ELEMENTS[self.cursor]
        entry = self.theme[elem.key]

        parts: list[str] = []
        if "fg" in elem.props:
            fg_s = f"{fg256(entry.fg)}██{RESET} {entry.fg}" if entry.fg is not None else f"{DIM}default{RESET}"
            parts.append(f"FG: {fg_s}")
        if "bg" in elem.props:
            bg_s = f"{bg256(entry.bg)}  {RESET} {entry.bg}" if entry.bg is not None else f"{DIM}default{RESET}"
            parts.append(f"BG: {bg_s}")
        if "attrs" in elem.props:
            attr_s = ", ".join(entry.attrs) if entry.attrs else f"{DIM}none{RESET}"
            parts.append(f"Attrs: {attr_s}")

        return [
            f"{BOLD}{elem.label}{RESET}  {DIM}({elem.key}){RESET}  {DIM}{elem.desc}{RESET}",
            "",
            "   ".join(parts),
        ]

    # --- color picker ---

    def _color_cell(self, n: int, is_bg: bool, sel: int, active: int | None, elem_bg: str) -> str:
        """Render a single color cell with selection and active markers."""
        if is_bg:
            block = f"{bg256(n)}  {RESET}"
        else:
            block = f"{elem_bg}{fg256(n)}[]{RESET}"
        if n == sel:
            return f"{BLINK}{REVERSE}{block}{RESET}"
        if n == active:
            return f"{UNDERLINE}{block}{RESET}"
        return block

    def render_color_grid(self, is_bg: bool) -> list[str]:
        lines: list[str] = []
        sel = self.color_cursor
        entry = self.theme[ELEMENTS[self.cursor].key]
        active = entry.bg if is_bg else entry.fg

        elem_bg = bg256(entry.bg) if entry.bg is not None else ""

        def cell(n: int) -> str:
            return self._color_cell(n, is_bg, sel, active, elem_bg)

        is_default = active is None
        dflt_arrow = f"{BLINK}{REVERSE}▸{RESET}" if sel == -1 else " "
        dflt_mark = f"{BOLD}●{RESET}" if is_default else f"{DIM}○{RESET}"
        lines.append(f"  {dflt_arrow} {dflt_mark} default {DIM}(transparent){RESET}")
        lines.append("")

        lines.append("  " + " ".join(cell(i) for i in range(8)))
        lines.append("  " + " ".join(cell(i) for i in range(8, 16)))
        lines.append("")

        for g in range(6):
            row_cells = []
            for r in range(6):
                for b in range(6):
                    row_cells.append(cell(_rgb_cube(r, g, b)))
                if r < 5:
                    row_cells.append(" ")
            lines.append("  " + "".join(row_cells))
        lines.append("")

        lines.append("  " + "".join(cell(232 + i) for i in range(24)))

        return lines

    # --- attribute picker ---

    def render_attr_picker(self) -> list[str]:
        entry = self.theme[ELEMENTS[self.cursor].key]
        lines: list[str] = []
        color = ""
        if entry.fg is not None:
            color += fg256(entry.fg)
        if entry.bg is not None:
            color += bg256(entry.bg)
        for i, (name, sgr, desc) in enumerate(ATTRS_AVAILABLE):
            arrow = "▸" if i == self.attr_cursor else " "
            if name == "none":
                active_mark = f"{BOLD}●{RESET}" if not entry.attrs else f"{DIM}○{RESET}"
                lines.append(f"  {arrow} {active_mark} {color}{desc}{RESET}")
            else:
                active_mark = f"{BOLD}●{RESET}" if name in entry.attrs else f"{DIM}○{RESET}"
                lines.append(f"  {arrow} {active_mark} {color}{sgr}{desc}{RESET}")
        return lines

    # --- settings panel ---

    def _render_setting_preview(self, sdef: SettingDef, val: str) -> str:
        bar_bg = self._themed_bar_bg()

        if sdef.key in ("5h_ramp", "7d_ramp", "ctx_ramp"):
            prefix = sdef.key.split("_")[0]
            display = _get_setting(self.settings, f"{prefix}_display")
            if display == "number":
                bars = " ".join(_render_demo_number(p, val) for p in range(10, 100, 10))
            elif display == "horizontal":
                bars = " ".join(_bar(p, 3, ramp=RAMP_PRESETS[val], bar_bg=bar_bg) for p in (20, 50, 80))
            else:
                bars = " ".join(_vbar(p, ramp=RAMP_PRESETS[val], bar_bg=bar_bg) for p in range(5, 100, 5))
            return f"  {bars}"
        elif sdef.key in ("5h_display", "7d_display", "ctx_display"):
            prefix = sdef.key.split("_")[0]
            ramp = _get_setting(self.settings, f"{prefix}_ramp")
            if val == "number":
                return f"  {_render_demo_number(60, ramp)}"
            elif val == "horizontal":
                return f"  {_bar(60, 8, ramp=RAMP_PRESETS[ramp], bar_bg=bar_bg)}"
            else:
                bars = " ".join(_vbar(p, ramp=RAMP_PRESETS[ramp], bar_bg=bar_bg) for p in (10, 30, 50, 70, 90))
                return f"  {bars}"
        elif sdef.key in ("separator", "git_separator", "limits_separator"):
            sep_entry = self.theme.get("sep")
            sep_style = build_style(sep_entry) if sep_entry else fg256(8)
            sep_vis = f" {sep_style}{val}{RESET} " if val else " "
            labels = {"separator": ("path", "git", "limits"),
                      "git_separator": ("branch", "CI", "PR"),
                      "limits_separator": ("5h", "7d", "ctx")}
            parts = sep_vis.join(f"{DIM}{l}{RESET}" for l in labels[sdef.key])
            return f"  {parts}"
        return ""

    def render_settings(self) -> list[str]:
        lines: list[str] = []
        for i, sdef in enumerate(SETTINGS_DEFS):
            arrow = "▸" if i == self.settings_cursor else " "
            val = self.settings[sdef.key]
            opt_parts: list[str] = []
            for opt in sdef.options:
                label = _sep_display_label(sdef.key, opt)
                if opt == val:
                    opt_parts.append(f"{BOLD}{REVERSE} {label} {RESET}")
                else:
                    opt_parts.append(f" {DIM}{label}{RESET} ")
            opts_str = " ".join(opt_parts)
            preview = self._render_setting_preview(sdef, val)
            lines.append(f"  {arrow} {sdef.label:20s} {opts_str}{preview}")
        return lines

    # --- full render ---

    def render(self):
        out: list[str] = []
        out.append(CLEAR_SCREEN)
        out.append(HIDE_CURSOR)

        out.append(f"  {BOLD}Claude Code Statusline — Theme Editor{RESET}\r\n\r\n")

        preview, carets = self.render_preview()
        legend = self.render_legend()
        out.append(f"  {preview}\r\n  {carets}\r\n\r\n")
        for line in legend:
            out.append(f"  {line}\r\n")

        if self.mode in ("fg", "bg", "attr"):
            elem = ELEMENTS[self.cursor]
            entry = self.theme[elem.key]
            pad = 0
            fg_vis = (3 + len(str(entry.fg))) if entry.fg is not None else 7
            bg_vis = (3 + len(str(entry.bg))) if entry.bg is not None else 7
            if self.mode == "fg":
                pad = 4
                sel = self.color_cursor
                hint = f"{fg256(sel)}██{RESET} {sel}" if sel >= 0 else f"{DIM}default{RESET}"
            elif self.mode == "bg":
                pad = (4 + fg_vis + 3) if "fg" in elem.props else 0
                pad += 4
                sel = self.color_cursor
                hint = f"{bg256(sel)}  {RESET} {sel}" if sel >= 0 else f"{DIM}default{RESET}"
            else:
                if "fg" in elem.props:
                    pad += 4 + fg_vis + 3
                if "bg" in elem.props:
                    pad += 4 + bg_vis + 3
                pad += 7
                attr_name = ATTRS_AVAILABLE[self.attr_cursor][0]
                tentative = list(entry.attrs)
                if attr_name == "none":
                    tentative.clear()
                elif attr_name not in tentative:
                    tentative.append(attr_name)
                else:
                    tentative.remove(attr_name)
                hint = ", ".join(tentative) if tentative else f"{DIM}none{RESET}"
            out.append(f"  {' ' * pad}{hint}\r\n")

        out.append("\r\n")

        if self.mode == "fg":
            out.append(f"  {BOLD}Pick FG color{RESET}  {DIM}(arrows navigate, Enter select, Esc cancel){RESET}\r\n")
            for line in self.render_color_grid(is_bg=False):
                out.append(f"{line}\r\n")
        elif self.mode == "bg":
            out.append(f"  {BOLD}Pick BG color{RESET}  {DIM}(arrows navigate, Enter select, Esc cancel){RESET}\r\n")
            for line in self.render_color_grid(is_bg=True):
                out.append(f"{line}\r\n")
        elif self.mode == "attr":
            out.append(f"  {BOLD}Toggle attributes{RESET}  {DIM}(↑↓ navigate, Space toggle, Esc done){RESET}\r\n")
            for line in self.render_attr_picker():
                out.append(f"{line}\r\n")
        elif self.mode == "settings":
            out.append(f"  {BOLD}Global Settings{RESET}  {DIM}(↑↓ navigate, ←→ change, Enter apply, Esc cancel){RESET}\r\n\r\n")
            for line in self.render_settings():
                out.append(f"{line}\r\n")
        elif self.mode == "quit":
            out.append(f"  {BOLD}Unsaved changes:{RESET}\r\n\r\n")
            for line in self._diff_lines():
                out.append(f"  {line}\r\n")
            out.append(f"\r\n  {DIM}Save to {self._config_path_display()}?{RESET}\r\n")
            K = f"{RESET}\033[97m"
            D = f"{RESET}"
            out.append(f"\r\n  {K}y{D} save & quit   {K}n{D} discard & quit   {K}q{D}/{K}Esc{D} cancel{RESET}\r\n")

        out.append("\r\n")
        if self.mode == "nav":
            K = f"{RESET}\033[97m"
            D = f"{RESET}"
            X = f"{RESET}{fg256(239)}"
            props = ELEMENTS[self.cursor].props
            has_changes = self._has_changes()
            def _k(key: str, label: str, active: bool) -> str:
                return f"{K}{key}{D} {label}" if active else f"{X}{key} {label}"
            keys: list[str] = [
                f"{D}← → navigate",
                _k("f", "fg", "fg" in props),
                _k("b", "bg", "bg" in props),
                _k("a", "attrs", "attrs" in props),
                f"{K}g{D} settings",
                f"{K}c{D} copy", f"{K}v{D} paste",
                _k("s", "save", has_changes),
                f"{K}r{D} reset", f"{K}q{D} quit",
            ]
            out.append(f"  {'   '.join(keys)}{RESET}\r\n")

        if self.msg:
            out.append(f"\r\n  {self.msg}\r\n")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # --- key handling ---

    def handle_key(self, key: str):
        key = _CYRILLIC_MAP.get(key, key)
        self.msg = ""

        if self.mode == "quit":
            self._handle_quit(key)
        elif self.mode == "nav":
            self._handle_nav(key)
        elif self.mode in ("fg", "bg"):
            self._handle_color(key)
        elif self.mode == "attr":
            self._handle_attr(key)
        elif self.mode == "settings":
            self._handle_settings(key)

    def _handle_quit(self, key: str):
        if key == "y":
            save_theme(self.theme, self.settings)
            self._mark_saved()
            self.running = False
        elif key == "n":
            self.running = False
        elif key in ("q", "\x1b"):
            self.mode = "nav"
            self.msg = ""

    def _handle_nav(self, key: str):
        if key == "q":
            if self._has_changes():
                self.mode = "quit"
            else:
                self.running = False
        elif key == LEFT:
            self.cursor = (self.cursor - 1) % len(ELEMENTS)
        elif key == RIGHT:
            self.cursor = (self.cursor + 1) % len(ELEMENTS)
        elif key == "f":
            elem = ELEMENTS[self.cursor]
            if "fg" not in elem.props:
                return
            self.mode = "fg"
            e = self.theme[elem.key]
            self.color_cursor = e.fg if e.fg is not None else -1
        elif key == "b":
            elem = ELEMENTS[self.cursor]
            if "bg" not in elem.props:
                return
            self.mode = "bg"
            e = self.theme[elem.key]
            self.color_cursor = e.bg if e.bg is not None else -1
        elif key == "a":
            if "attrs" not in ELEMENTS[self.cursor].props:
                return
            self.mode = "attr"
            self.attr_cursor = 0
        elif key == "g":
            self.mode = "settings"
            self.settings_cursor = 0
            self._settings_snapshot = dict(self.settings)
        elif key == "s":
            if not self._has_changes():
                return
            save_theme(self.theme, self.settings)
            self._mark_saved()
            self.msg = f"Saved → {self._config_path_display()}"
        elif key == "r":
            k = ELEMENTS[self.cursor].key
            self.theme[k] = DEFAULTS[k].copy()
            self.msg = f"Reset {k} to default"
        elif key == "R":
            self.theme = {k: v.copy() for k, v in DEFAULTS.items()}
            self.msg = "Reset ALL to defaults"
        elif key == "c":
            e = self.theme[ELEMENTS[self.cursor].key]
            self.clipboard = e.copy()
            self.msg = f"Copied {ELEMENTS[self.cursor].label}"
        elif key == "v":
            if self.clipboard:
                elem = ELEMENTS[self.cursor]
                cur = self.theme[elem.key]
                self.theme[elem.key] = ThemeEntry(
                    fg=self.clipboard.fg if "fg" in elem.props else cur.fg,
                    bg=self.clipboard.bg if "bg" in elem.props else cur.bg,
                    attrs=list(self.clipboard.attrs) if "attrs" in elem.props else list(cur.attrs))
                self.msg = f"Pasted → {elem.label}"
            else:
                self.msg = "Nothing to paste"

    def _handle_color(self, key: str):
        if key == ESC_KEY:
            self.mode = "nav"
        elif self.color_cursor == -1:
            if key == DOWN:
                self.color_cursor = 0
            elif key == ENTER:
                k = ELEMENTS[self.cursor].key
                if self.mode == "fg":
                    self.theme[k].fg = None
                else:
                    self.theme[k].bg = None
                self.mode = "nav"
        else:
            if key == RIGHT:
                self.color_cursor = _grid_move(self.color_cursor, "right")
            elif key == LEFT:
                self.color_cursor = _grid_move(self.color_cursor, "left")
            elif key == DOWN:
                self.color_cursor = _grid_move(self.color_cursor, "down")
            elif key == UP:
                row_i, _ = _COLOR_POS[self.color_cursor]
                if row_i == 0:
                    self.color_cursor = -1
                else:
                    self.color_cursor = _grid_move(self.color_cursor, "up")
            elif key == "d":
                self.color_cursor = -1
            elif key == ENTER:
                k = ELEMENTS[self.cursor].key
                if self.mode == "fg":
                    self.theme[k].fg = self.color_cursor
                else:
                    self.theme[k].bg = self.color_cursor
                self.mode = "nav"

    def _handle_attr(self, key: str):
        if key == ESC_KEY:
            self.mode = "nav"
        elif key == UP:
            self.attr_cursor = (self.attr_cursor - 1) % len(ATTRS_AVAILABLE)
        elif key == DOWN:
            self.attr_cursor = (self.attr_cursor + 1) % len(ATTRS_AVAILABLE)
        elif key == " ":
            name = ATTRS_AVAILABLE[self.attr_cursor][0]
            entry = self.theme[ELEMENTS[self.cursor].key]
            if name == "none":
                entry.attrs.clear()
            elif name in entry.attrs:
                entry.attrs.remove(name)
            else:
                entry.attrs.append(name)

    def _handle_settings(self, key: str):
        if key == "\r":
            self.mode = "nav"
            self.msg = f"{DIM}Settings applied{RESET}"
        elif key == ESC_KEY:
            self.settings = dict(self._settings_snapshot)
            self.mode = "nav"
            self.msg = f"{DIM}Settings reverted{RESET}"
        elif key == UP:
            self.settings_cursor = (self.settings_cursor - 1) % len(SETTINGS_DEFS)
        elif key == DOWN:
            self.settings_cursor = (self.settings_cursor + 1) % len(SETTINGS_DEFS)
        elif key in (LEFT, RIGHT):
            sdef = SETTINGS_DEFS[self.settings_cursor]
            cur_val = self.settings[sdef.key]
            idx = sdef.options.index(cur_val) if cur_val in sdef.options else 0
            if key == RIGHT:
                idx = (idx + 1) % len(sdef.options)
            else:
                idx = (idx - 1) % len(sdef.options)
            self.settings[sdef.key] = sdef.options[idx]

    # --- terminal I/O ---

    def read_key(self) -> str:
        fd = sys.stdin.fileno()
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            if select.select([fd], [], [], 0.1)[0]:
                ch2 = os.read(fd, 1)
                if ch2 == b"[":
                    ch3 = os.read(fd, 1)
                    if ch3.isdigit():
                        buf = ch3
                        while select.select([fd], [], [], 0.02)[0]:
                            c = os.read(fd, 1)
                            buf += c
                            if c.isalpha() or c == b"~":
                                break
                        return f"\x1b[{buf.decode()}"
                    return f"\x1b[{ch3.decode()}"
                if ch2 == b"O":
                    ch3 = os.read(fd, 1)
                    return f"\x1b[{ch3.decode()}"
                return f"\x1b{ch2.decode()}"
            return ESC_KEY
        b0 = ch[0]
        if b0 >= 0xC0:
            need = (2 if b0 < 0xE0 else 3 if b0 < 0xF0 else 4) - 1
            ch += os.read(fd, need)
        return ch.decode()

    # --- animation ---

    _ANIM_STEP = 5.0
    _ANIM_INTERVAL = 0.067

    def _is_anim_active(self) -> bool:
        if self.mode != "settings":
            return False
        sdef = SETTINGS_DEFS[self.settings_cursor]
        return sdef.key.endswith("_ramp") or sdef.key.endswith("_display")

    def _advance_animation(self):
        if self._anim_ascending:
            self._anim_pct += self._ANIM_STEP
            if self._anim_pct >= 100:
                self._anim_pct = 100
                self._anim_ascending = False
        else:
            self._anim_pct -= self._ANIM_STEP
            if self._anim_pct <= 0:
                self._anim_pct = 0
                self._anim_ascending = True

    def _read_key_timeout(self, timeout: float) -> str | None:
        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], timeout)[0]:
            return None
        return self.read_key()

    def run(self):
        old = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin)
            while self.running:
                self.render()
                if self._is_anim_active():
                    key = self._read_key_timeout(self._ANIM_INTERVAL)
                    if key is None:
                        self._advance_animation()
                        continue
                else:
                    key = self.read_key()
                self.handle_key(key)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
            sys.stdout.write(SHOW_CURSOR + CLEAR_SCREEN)
            sys.stdout.flush()


# --- grid navigation ---------------------------------------------------------

_GRID_ROWS: list[list[int]] = [
    list(range(0, 8)),
    list(range(8, 16)),
]
for _g in range(6):
    _row = []
    for _r in range(6):
        for _b in range(6):
            _row.append(_rgb_cube(_r, _g, _b))
    _GRID_ROWS.append(_row)
_GRID_ROWS.append(list(range(232, 256)))
del _g, _r, _b, _row

_COLOR_POS: dict[int, tuple[int, int]] = {}
for _ri, _row in enumerate(_GRID_ROWS):
    for _ci, _color in enumerate(_row):
        _COLOR_POS[_color] = (_ri, _ci)
del _ri, _row, _ci, _color


def _row_visual_x(row_i: int) -> list[int]:
    n = len(_GRID_ROWS[row_i])
    if n == 8:
        return [c * 3 for c in range(n)]
    elif n == 36:
        return [c * 2 + c // 6 for c in range(n)]
    else:
        return [c * 2 for c in range(n)]


_VISUAL_X: list[list[int]] = [_row_visual_x(i) for i in range(len(_GRID_ROWS))]


def _closest_col(row_i: int, target_x: int) -> int:
    positions = _VISUAL_X[row_i]
    best = 0
    best_dist = abs(positions[0] - target_x)
    for c in range(1, len(positions)):
        dist = abs(positions[c] - target_x)
        if dist < best_dist:
            best = c
            best_dist = dist
    return best


def _grid_move(pos: int, direction: str) -> int:
    row_i, col_i = _COLOR_POS[pos]
    if direction == "left":
        col_i = max(0, col_i - 1)
    elif direction == "right":
        col_i = min(len(_GRID_ROWS[row_i]) - 1, col_i + 1)
    elif direction == "up":
        if row_i > 0:
            cur_x = _VISUAL_X[row_i][col_i]
            row_i -= 1
            col_i = _closest_col(row_i, cur_x)
    elif direction == "down":
        if row_i < len(_GRID_ROWS) - 1:
            cur_x = _VISUAL_X[row_i][col_i]
            row_i += 1
            col_i = _closest_col(row_i, cur_x)
    return _GRID_ROWS[row_i][col_i]


# Key constants
LEFT     = "\x1b[D"
RIGHT    = "\x1b[C"
UP       = "\x1b[A"
DOWN     = "\x1b[B"
ENTER    = "\r"
ESC_KEY  = "\x1b"

# ЙЦУКЕН → QWERTY mapping for Cyrillic keyboard layout
_CYRILLIC_MAP = {
    "й": "q", "а": "f", "и": "b", "ф": "a", "ы": "s",
    "к": "r", "К": "R", "в": "d", "с": "c", "м": "v",
    "п": "g",
}


# --- demo --------------------------------------------------------------------

def demo() -> None:
    """Print demo scenarios for visual testing."""
    D = PR_DOT

    def path_part() -> str:
        return f"{T.dir_name}{DEMO_DIR_NAME}{T.R}"

    def git_line(branch: str, status: str = "", ci: str = "", pr: str = "") -> str:
        if not branch:
            return ""
        line = f"{T.branch_sign}{BRANCH_LABEL}{T.R}{T.branch_name}{branch}{T.R}"
        if status:
            line += status
        if ci:
            line += f"{SEP_GIT}{ci}"
        if pr:
            line += f"{SEP_GIT}{pr}"
        return line

    def limits_bars(u5: float, r5: str, u7: float, r7: str, ctx: int) -> str:
        bars: list[str] = []
        if u7 >= 100:
            bars.append(_format_limit_window_for_prefix(u7, r7, "7d"))
        else:
            bars.append(_format_limit_window_for_prefix(u5, r5, "5h"))
            bars.append(_format_limit_window_for_prefix(u7, r7, "7d"))
        ctx_bar = _render_indicator_for_prefix(ctx, "ctx")
        bars.append(f"{T.dir_parent}ctx{T.R} {ctx_bar}")
        return SEP_LIMITS.join(bars)

    def vibes_label(u7: float, r7: str) -> str:
        return _7d_pace_label(u7, r7)

    def combined(path: str, git: str, limits: str, vibes: str) -> str:
        return SEP_EXTRA.join(p for p in [path, git, limits, vibes] if p)

    now = datetime.now(timezone.utc)
    r5h = (now + timedelta(hours=4, minutes=26)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    r5h_low = (now + timedelta(minutes=23)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    r7d = (now + timedelta(days=4, hours=17)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    r7d_med = (now + timedelta(days=1, hours=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    r7d_crit = (now + timedelta(days=2, hours=4)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    gsd_line = f"⬆ /gsd:update {T.sep}│{T.R} Fixing auth bug {T.sep}│{T.R} █████░░░░░ 52%"

    pp = path_part()

    print("\n=== Demo: limits green — both windows low ===\n")
    print(combined(
        pp,
        git_line(DEMO_BRANCH_MAIN, f"{T.git_staged}+{T.R}", "",
                 f"{T.ok}{D}{D}{D}{T.R}"),
        limits_bars(12, r5h, 35, r7d, 24),
        vibes_label(35, r7d),
    ))

    print("\n=== Demo: limits yellow — 5h warn ===\n")
    print(combined(
        pp,
        git_line(DEMO_BRANCH_FEATURE, f"{T.git_dirty}*{T.R}{T.git_staged}+{T.R}",
                 f"{T.err}CI{T.R}",
                 f"{T.err}{D}{T.R}{T.wait}{D}{D}{T.R}{T.ok}{D}{D}{T.R}{T.none}{D}{T.R} {T.notif}💬2{T.R}"),
        limits_bars(70, r5h, 45, r7d, 65),
        vibes_label(45, r7d),
    ))

    print("\n=== Demo: 5h exhausted (red), 7d for context ===\n")
    print(combined(
        pp,
        git_line(DEMO_BRANCH_FEATURE, f"{T.git_dirty}*{T.R}",
                 f"{T.wait}CI{T.R}",
                 f"{T.wait}{D}{T.R}{T.ok}{D}{D}{T.R}"),
        limits_bars(100, r5h_low, 80, r7d_med, 80),
        vibes_label(80, r7d_med),
    ))

    print("\n=== Demo: 7d exhausted — only 7d shown ===\n")
    print(combined(
        pp,
        git_line(DEMO_BRANCH_DEV, f"{T.git_ahead}↑{T.R}"),
        limits_bars(100, r5h_low, 100, r7d_crit, 45),
        vibes_label(100, r7d_crit),
    ))

    print("\n=== Demo: 2-line with GSD slot ===\n")
    print(render([
        combined(
            pp,
            git_line(DEMO_BRANCH_FEATURE, f"{T.git_dirty}*{T.R}{T.git_staged}+{T.R}",
                     f"{T.err}CI{T.R}",
                     f"{T.err}{D}{T.R}{T.wait}{D}{D}{T.R}{T.ok}{D}{D}{T.R}{T.none}{D}{T.R} {T.notif}💬3{T.R}"),
            limits_bars(25, r5h, 18, r7d, 30),
            vibes_label(18, r7d),
        ),
        gsd_line,
    ]))

    print("\n=== Demo: gh not installed ===\n")
    print(combined(
        pp,
        git_line(DEMO_BRANCH_MAIN, "", "", f"{T.err}gh not installed{T.R}"),
        limits_bars(5, r5h, 10, r7d, 8),
        vibes_label(10, r7d),
    ))

    print("\n=== Color ramp presets ===\n")
    for name, wp in RAMP_PRESETS.items():
        vbars = ""
        for p in range(0, 101, 5):
            vbars += _vbar(p, ramp=wp)
        print(f"  {name:10s} {vbars}")
        print()


# --- install -----------------------------------------------------------------

SETTINGS_FILE = Path.home() / ".claude" / "settings.json"


def install() -> None:
    """Write this script as statusLine command in ~/.claude/settings.json."""
    script_path = str(Path(__file__).resolve())
    command = f"{sys.executable} {script_path}"

    settings: dict = _load_json_file(SETTINGS_FILE) or {} if SETTINGS_FILE.exists() else {}

    old = settings.get("statusLine", {}).get("command", "")
    settings["statusLine"] = {"type": "command", "command": command}

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    if old and old != command:
        print(f"Replaced: {old}")
    print(f"Installed: {command}")
    print(f"Config:    {SETTINGS_FILE}")


# --- main (dispatch) --------------------------------------------------------

def editor_main() -> None:
    """TUI theme editor mode."""
    _load_validated_config()
    if not sys.stdin.isatty():
        print("Error: theme editor requires an interactive terminal", file=sys.stderr)
        sys.exit(1)
    Editor().run()


def statusline_main() -> None:
    """Normal statusline mode: read stdin JSON, execute slots, output lines."""
    slots = _load_theme_config()

    def _stdin_timeout(signum, frame):
        raise TimeoutError
    old_handler = signal.signal(signal.SIGALRM, _stdin_timeout)
    try:
        signal.alarm(1)
        raw = sys.stdin.read()
    except TimeoutError:
        print("FATAL: Timed out reading stdin", file=sys.stderr)
        sys.exit(1)
    except (OSError, IOError, BrokenPipeError) as e:
        print(f"FATAL: Failed to read stdin: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    if not raw.strip():
        print("FATAL: No JSON input received from stdin", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"FATAL: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # From here on: valid JSON confirmed — we're inside Claude Code.
    # Never crash silently; all errors go to stdout in red.
    try:
        current_dir = data.get("workspace", {}).get("current_dir")
        if not current_dir:
            print("\033[31merror: current_dir missing from stdin JSON\033[0m")
            return

        lines = execute_slots(slots, raw, current_dir)
        print(render(lines))
    except Exception as e:
        print(f"\033[31merror: {e}\033[0m")


def _print_help() -> None:
    print(f"""omcc-statusline — Claude Code statusline + theme editor

Usage:
  <stdin JSON> | omcc-statusline.py   Statusline mode (default)
  omcc-statusline.py --theme          TUI theme editor
  omcc-statusline.py --demo           Print demo scenarios
  omcc-statusline.py --install        Register in ~/.claude/settings.json
  omcc-statusline.py --help           Show this help

Theme editor is also activated when invoked via a symlink
containing "theme" in its name (e.g. theme-editor.py).

Config: {CONFIG_FILE}""")


def main() -> None:
    name = Path(sys.argv[0]).stem
    if "theme" in name:
        editor_main()
    elif len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        _print_help()
    elif len(sys.argv) > 1 and sys.argv[1] == "--theme":
        editor_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "--install":
        install()
    elif len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _load_theme_config()
        demo()
    else:
        statusline_main()


if __name__ == "__main__":
    main()
