from __future__ import annotations

import asyncio
import time
import unicodedata

from artemis.toolchain import find_adb

# Percentage coordinates (x%, y%) on a standard Android portrait QWERTY layout.
# Matches RTA's ``relative_positions`` for 1..0, q..p, a..l, z..m and adds
# control/navigation keys (shift, backspace, symbols, comma, space, dot, enter).
DEFAULT_KEYBOARD_RELATIVE_POSITIONS: dict[str, tuple[float, float]] = {
    # Row 0: numbers (top row on 5-row keyboard or top row of ?123 layout)
    "1": (7.6, 69.5),
    "2": (16.9, 69.5),
    "3": (26.3, 69.5),
    "4": (35.6, 69.5),
    "5": (45.1, 69.5),
    "6": (54.5, 69.5),
    "7": (64.0, 69.5),
    "8": (73.4, 69.5),
    "9": (82.9, 69.5),
    "0": (92.3, 69.5),
    # Row 1: q..p
    "q": (7.6, 74.9),
    "w": (16.9, 74.9),
    "e": (26.3, 74.9),
    "r": (35.6, 74.9),
    "t": (45.1, 74.9),
    "y": (54.5, 74.9),
    "u": (64.0, 74.9),
    "i": (73.4, 74.9),
    "o": (82.9, 74.9),
    "p": (92.3, 74.9),
    # Row 2: a..l
    "a": (12.3, 80.5),
    "s": (21.7, 80.5),
    "d": (31.0, 80.5),
    "f": (40.4, 80.5),
    "g": (49.8, 80.5),
    "h": (59.3, 80.5),
    "j": (68.7, 80.5),
    "k": (78.1, 80.5),
    "l": (87.6, 80.5),
    # Row 3: shift, z..m, backspace
    "shift": (9.0, 86.0),
    "z": (21.7, 86.0),
    "x": (31.0, 86.0),
    "c": (40.4, 86.0),
    "v": (49.8, 86.0),
    "b": (59.3, 86.0),
    "n": (68.7, 86.0),
    "m": (78.1, 86.0),
    "backspace": (91.0, 86.0),
    # Row 4: ?123, comma, space, dot, enter/search
    "symbols": (9.0, 91.5),
    ",": (21.7, 91.5),
    "space": (50.0, 91.5),
    ".": (78.1, 91.5),
    "enter": (91.0, 91.5),
}

# Common symbols accessible on the ?123 Gboard/AOSP layout mapped to the
# corresponding primary-layout key slot.
SYMBOL_KEY_SLOTS: dict[str, str] = {
    "@": "a",
    "#": "s",
    "$": "d",
    "_": "f",
    "&": "g",
    "-": "h",
    "+": "j",
    "(": "k",
    ")": "l",
    "/": "p",
    "*": "z",
    '"': "x",
    "'": "c",
    ":": "v",
    ";": "b",
    "!": "n",
    "?": "m",
}

DIRECT_PUNCTUATION: dict[str, str] = {
    " ": "space",
    "\n": "enter",
    ",": ",",
    ".": ".",
}


def strip_accents(text: str) -> str:
    """Removes combining diacritics (e.g. 'ç' -> 'c', 'ã' -> 'a') for QWERTY typing."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def key_to_pixels(
    key: str,
    width: int,
    height: int,
    positions: dict[str, tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """Converts a logical key name into pixel coordinates ``(px, py)``."""
    table = positions or DEFAULT_KEYBOARD_RELATIVE_POSITIONS
    if key not in table:
        raise KeyError(f"Key '{key}' is not defined in keyboard layout.")
    x_pct, y_pct = table[key]
    px = round((x_pct / 100.0) * max(1, width - 1), 2)
    py = round((y_pct / 100.0) * max(1, height - 1), 2)
    return px, py


def text_to_key_sequence(text: str) -> list[str]:
    """Translates a string into an ordered sequence of virtual keyboard key names.

    Batches consecutive digits/symbols that live on the ``?123`` layer so the
    robot only toggles ``symbols`` once per contiguous numeric/symbol run.
    """
    clean = strip_accents(text)
    keys: list[str] = []
    in_symbols_layer = False

    for ch in clean:
        if ch in DIRECT_PUNCTUATION:
            if in_symbols_layer:
                keys.append("symbols")
                in_symbols_layer = False
            keys.append(DIRECT_PUNCTUATION[ch])
            continue

        if ch.isdigit():
            if not in_symbols_layer:
                keys.append("symbols")
                in_symbols_layer = True
            keys.append(ch)
            continue

        if ch in SYMBOL_KEY_SLOTS:
            if not in_symbols_layer:
                keys.append("symbols")
                in_symbols_layer = True
            keys.append(SYMBOL_KEY_SLOTS[ch])
            continue

        lower = ch.lower()
        if lower in DEFAULT_KEYBOARD_RELATIVE_POSITIONS and lower.isalpha():
            if in_symbols_layer:
                keys.append("symbols")
                in_symbols_layer = False
            if ch.isupper():
                keys.append("shift")
            keys.append(lower)

    if in_symbols_layer:
        keys.append("symbols")

    return keys


async def is_keyboard_visible(device_id: str | None) -> bool:
    """Checks via read-only ADB ``dumpsys input_method`` whether the IME is shown."""
    adb_bin = find_adb() or "adb"
    cmd = [adb_bin]
    if device_id and device_id != "default":
        cmd.extend(["-s", str(device_id)])
    cmd.extend(["shell", "dumpsys", "input_method"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, TimeoutError):
        return False

    output = stdout.decode("utf-8", errors="replace")
    return "mInputShown=true" in output or "InputShown=true" in output


async def wait_for_keyboard_visible(
    device_id: str | None,
    timeout_s: float = 1.2,
    poll_interval_s: float = 0.15,
) -> bool:
    """Waits briefly for the soft keyboard to appear after tapping an input field."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if await is_keyboard_visible(device_id):
            return True
        await asyncio.sleep(poll_interval_s)
    return False
