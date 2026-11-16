"""Tailles, vitesses et durées lisibles, dans la langue de l'interface (« 1,2 Go », « 1.2 GB »)."""

from __future__ import annotations

from .i18n import decimal, tr


def size(n: float) -> str:
    units = (tr("o"), tr("Ko"), tr("Mo"), tr("Go"), tr("To"))
    n = float(max(0, n))
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000
        i += 1
    if i == 0:
        return f"{int(n)} {units[0]}"
    return f"{decimal(n, 1 if n < 100 else 0)} {units[i]}"


def speed(n: float) -> str:
    return tr("{0}/s").format(size(n))


def duration(seconds: int) -> str:
    """« 45 s », « 12 min », « 2 h 05 », « 3 j 4 h »."""
    s = int(max(0, seconds))
    if s < 60:
        return tr("{0} s").format(s)
    m = s // 60
    if m < 60:
        return tr("{0} min").format(m)
    h, m = divmod(m, 60)
    if h < 24:
        return tr("{0} h {1:02d}").format(h, m)
    d, h = divmod(h, 24)
    return tr("{0} j {1} h").format(d, h)


def percent(fraction: float) -> str:
    return f"{int(fraction * 100)} %"
