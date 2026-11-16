"""Pure helpers for the GUI (no tkinter), so they can be unit-tested anywhere."""
from __future__ import annotations

from fractions import Fraction
from typing import Optional

MIN_FACTOR, MAX_FACTOR = Fraction(11, 10), Fraction(16)


def _clean(text: str) -> str:
    return text.strip().lower().replace(",", ".").replace("×", "").replace("x", "").strip()


def parse_factor_loose(text: str) -> Optional[Fraction]:
    """Live parsing while typing: any positive number, or None."""
    try:
        f = Fraction(_clean(text))
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def parse_factor(text: str) -> Fraction:
    """Strict parsing for Start.  Accepts 2, 1.5, 3/2, '2x'."""
    f = parse_factor_loose(text)
    if f is None:
        raise ValueError("Enter a valid number, for example 2 or 1.5")
    if not (MIN_FACTOR <= f <= MAX_FACTOR):
        raise ValueError(f"The slow-down factor must be between "
                         f"{float(MIN_FACTOR):g} and {float(MAX_FACTOR):g}")
    return f


def fmt(x: Fraction | float, digits: int = 3) -> str:
    s = f"{float(x):.{digits}f}".rstrip("0").rstrip(".")
    return s or "0"


def fps_to_factor(src_fps: Fraction, target_text: str) -> Optional[Fraction]:
    """Target output FPS typed by the user -> slow-down factor."""
    t = parse_factor_loose(target_text)       # same number grammar
    if t is None or not src_fps:
        return None
    return (src_fps / t).limit_denominator(100)


def out_suffix(src_fps: Optional[Fraction], factor: Fraction) -> str:
    if not src_fps:
        return f"_x{fmt(factor, 2)}"
    return f"_{fmt(src_fps / factor, 2)}fps"
