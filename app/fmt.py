"""Formatierungshelfer (deutsche Zeit- und Größenangaben)."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_docker_time(wert: str | None) -> datetime | None:
    """Docker liefert RFC3339 mit Nanosekunden, z. B. 2026-10-29T12:00:00.123456789Z."""
    if not wert or wert.startswith("0001-01-01"):
        return None
    wert = wert.strip()
    if wert.endswith("Z"):
        wert = wert[:-1] + "+00:00"
    # Bruchteile auf Mikrosekunden kürzen, das kann fromisoformat verarbeiten.
    if "." in wert:
        kopf, rest = wert.split(".", 1)
        ziffern = ""
        for i, zeichen in enumerate(rest):
            if not zeichen.isdigit():
                rest = rest[i:]
                break
            ziffern += zeichen
        else:
            rest = ""
        wert = f"{kopf}.{ziffern[:6].ljust(6, '0')}{rest}"
    try:
        dt = datetime.fromisoformat(wert)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dauer(sekunden: float | None) -> str:
    if sekunden is None:
        return "–"
    s = max(0, int(sekunden))
    tage, s = divmod(s, 86400)
    std, s = divmod(s, 3600)
    minuten, s = divmod(s, 60)
    if tage:
        return f"{tage} Tg. {std} Std."
    if std:
        return f"{std} Std. {minuten} Min."
    if minuten:
        return f"{minuten} Min."
    return f"{s} Sek."


def seit(dt: datetime | None, jetzt: datetime | None = None) -> float | None:
    if dt is None:
        return None
    jetzt = jetzt or datetime.now(timezone.utc)
    return (jetzt - dt).total_seconds()


def alter(dt: datetime | None, jetzt: datetime | None = None) -> str:
    """Grobe Altersangabe, z. B. „vor 3 Monaten“."""
    sek = seit(dt, jetzt)
    if sek is None:
        return "–"
    tage = int(sek // 86400)
    if tage < 1:
        return "heute"
    if tage == 1:
        return "gestern"
    if tage < 14:
        return f"vor {tage} Tagen"
    if tage < 60:
        return f"vor {tage // 7} Wochen"
    if tage < 730:
        return f"vor {tage // 30} Monaten"
    return f"vor {tage // 365} Jahren"


def bytes_text(n: int | float | None) -> str:
    if n is None:
        return "–"
    wert = float(n)
    for einheit in ("B", "KiB", "MiB", "GiB"):
        if wert < 1024:
            return f"{wert:.0f} {einheit}" if einheit in ("B", "KiB") else f"{wert:.1f} {einheit}".replace(".", ",")
        wert /= 1024
    return f"{wert:.1f} TiB".replace(".", ",")


def prozent(wert: float | None) -> str:
    if wert is None:
        return "–"
    return f"{wert:.1f} %".replace(".", ",")


def zeitstempel(iso: str | None) -> str:
    """ISO-Zeit (UTC) in lokale Anzeige „29.10.2026 14:02:03“."""
    dt = parse_docker_time(iso) if iso else None
    if dt is None:
        return iso or "–"
    return dt.astimezone().strftime("%d.%m.%Y %H:%M:%S")
