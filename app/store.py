"""Einstellungen aus der Oberfläche (data/einstellungen.json) und Zugangsdaten (data/zugang.json).

Reihenfolge: eingebaute Vorgabe → .env → Oberfläche. Was in der Oberfläche gespeichert
ist, gilt. Wird eine Datei gelöscht, gelten wieder die Werte aus der .env.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Settings
from .jsonfile import read_json, write_json

log = logging.getLogger("dashboard")


@dataclass(frozen=True)
class Feld:
    key: str
    titel: str
    hilfe: str
    typ: type = int
    minimum: int = 0
    maximum: int = 0
    gruppe: str = "Anzeige"


FELDER: tuple[Feld, ...] = (
    Feld("refresh_seconds", "Aktualisierung (Sekunden)",
         "Wie oft sich die Übersicht von selbst neu lädt.", int, 5, 3600),
    Feld("log_tail", "Log-Zeilen", "Anzahl Zeilen in der Detailansicht.", int, 10, 5000),
    Feld("ignore_exit_zero", "Exit-Code 0 nicht als auffällig werten",
         "Sauber beendete Container (z. B. Einmal-Jobs) erscheinen dann nicht im Block „Auffällig“.", bool),
    Feld("restart_loop_threshold", "Restart-Schleife ab … Restarts",
         "So viele Restarts im Zeitfenster gelten als Schleife.", int, 1, 100, "Restart-Schleife"),
    Feld("restart_loop_window", "Zeitfenster (Sekunden)",
         "Zeitraum, in dem die Restarts gezählt werden.", int, 30, 86400, "Restart-Schleife"),
    Feld("update_check", "Images automatisch auf Updates prüfen",
         "Fragt die Registry nach dem aktuellen Stand des Tags. Es wird nichts heruntergeladen.", bool,
         gruppe="Updates"),
    Feld("update_check_hours", "Prüfen alle … Stunden",
         "Seltener prüfen schont die Abruf-Limits von Docker Hub.", int, 1, 168, "Updates"),
    Feld("session_max_age", "Sitzungsdauer (Minuten)",
         "Danach ist eine neue Anmeldung nötig.", int, 5, 43200, "Zugang"),
    Feld("login_max_attempts", "Fehlversuche bis zur Sperre", "Pro IP-Adresse.", int, 1, 100, "Zugang"),
    Feld("login_lock_seconds", "Sperrdauer (Sekunden)", "", int, 10, 86400, "Zugang"),
    Feld("action_log_max", "Aktionslog: maximale Einträge",
         "Ältere Einträge werden verworfen.", int, 10, 100000, "Aktionslog"),
)
FELD = {f.key: f for f in FELDER}

# session_max_age wird in Minuten angezeigt, intern aber in Sekunden gespeichert.
_MINUTEN = {"session_max_age"}

PASSWORT_MIN = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        verfahren, n, r, p, salt, digest = stored.split("$")
        if verfahren != "scrypt":
            return False
        neu = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(neu, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _digest(wert: str) -> bytes:
    return hashlib.sha256(wert.encode("utf-8")).digest()


class Store:
    def __init__(self, base: Settings) -> None:
        self.base = base
        self.dir = Path(base.data_dir)
        self.path = self.dir / "einstellungen.json"
        self.zugang_path = self.dir / "zugang.json"
        self._lock = threading.Lock()
        self._daten = self._load()
        self._settings = self._build()

    # ---------------------------------------------------------- Einstellungen
    def _load(self) -> dict:
        daten = read_json(self.path, {})
        if not isinstance(daten, dict):
            daten = {}
        daten.setdefault("werte", {})
        daten.setdefault("ausgeblendet", [])
        daten.setdefault("geschuetzt", [])
        daten.setdefault("links", {})
        return daten

    def _build(self) -> Settings:
        werte = {}
        for key, wert in self._daten["werte"].items():
            feld = FELD.get(key)
            if feld is None:
                continue
            if feld.typ is bool and isinstance(wert, bool):
                werte[key] = wert
            elif feld.typ is int and isinstance(wert, int) and not isinstance(wert, bool):
                werte[key] = wert
        return replace(self.base, **werte)

    @property
    def s(self) -> Settings:
        return self._settings

    def from_gui(self, key: str) -> bool:
        return key in self._daten["werte"]

    def anzeige_wert(self, key: str):
        wert = getattr(self._settings, key)
        return wert // 60 if key in _MINUTEN else wert

    def env_wert(self, key: str):
        wert = getattr(self.base, key)
        return wert // 60 if key in _MINUTEN else wert

    def update(self, formular: dict[str, str]) -> list[str]:
        """Werte aus dem Formular prüfen und speichern. Gibt Fehlermeldungen zurück."""
        fehler: list[str] = []
        neu: dict = {}
        for feld in FELDER:
            if feld.typ is bool:
                neu[feld.key] = formular.get(feld.key) in ("on", "true", "1")
                continue
            roh = (formular.get(feld.key) or "").strip()
            try:
                zahl = int(roh)
            except ValueError:
                fehler.append(f"{feld.titel}: bitte eine ganze Zahl eingeben.")
                continue
            if not feld.minimum <= zahl <= feld.maximum:
                fehler.append(f"{feld.titel}: erlaubt sind {feld.minimum} bis {feld.maximum}.")
                continue
            neu[feld.key] = zahl * 60 if feld.key in _MINUTEN else zahl
        if fehler:
            return fehler
        with self._lock:
            # Nur speichern, was von der .env abweicht – so greift eine spätere
            # Änderung in der .env weiter für alle nicht angefassten Werte.
            self._daten["werte"] = {k: v for k, v in neu.items() if getattr(self.base, k) != v}
            self._save()
        return []

    def reset(self) -> None:
        with self._lock:
            self._daten["werte"] = {}
            self._save()

    def _save(self) -> None:
        write_json(self.path, self._daten)
        self._settings = self._build()

    # ------------------------------------------------------------ Container
    @property
    def hidden_names(self) -> frozenset[str]:
        return frozenset(self._daten["ausgeblendet"])

    @property
    def protected_names(self) -> frozenset[str]:
        return frozenset(self._daten["geschuetzt"])

    @property
    def links(self) -> dict[str, str]:
        return dict(self._daten["links"])

    def set_container(self, name: str, hidden: bool, protected: bool,
                      link: str | None = None) -> None:
        """link: None = unverändert, "" = entfernen, sonst bereits geprüfte URL."""
        with self._lock:
            for key, an in (("ausgeblendet", hidden), ("geschuetzt", protected)):
                namen = set(self._daten[key])
                namen.add(name) if an else namen.discard(name)
                self._daten[key] = sorted(namen)
            if link is not None:
                if link:
                    self._daten["links"][name] = link
                else:
                    self._daten["links"].pop(name, None)
            self._save()

    # --------------------------------------------------------------- Zugang
    def _zugang(self) -> dict:
        daten = read_json(self.zugang_path, {})
        return daten if isinstance(daten, dict) else {}

    @property
    def username(self) -> str:
        return self._zugang().get("benutzer") or self.base.username

    @property
    def password_from_gui(self) -> bool:
        return bool(self._zugang().get("passwort"))

    def check_login(self, user: str, password: str) -> bool:
        zugang = self._zugang()
        # Benutzername immer in konstanter Zeit vergleichen.
        user_ok = hmac.compare_digest(_digest(user), _digest(zugang.get("benutzer") or self.base.username))
        if zugang.get("passwort"):
            pass_ok = verify_password(password, zugang["passwort"])
        else:
            pass_ok = hmac.compare_digest(_digest(password), _digest(self.base.password))
        return user_ok and pass_ok

    def change_access(self, alt: str, benutzer: str, neu: str, neu2: str) -> str | None:
        """Passwort (und optional Benutzernamen) ändern. Gibt eine Fehlermeldung oder None zurück."""
        benutzer = benutzer.strip()
        if not self.check_login(self.username, alt):
            return "Das aktuelle Passwort stimmt nicht."
        if not benutzer:
            return "Der Benutzername darf nicht leer sein."
        if neu != neu2:
            return "Die beiden neuen Passwörter stimmen nicht überein."
        if len(neu) < PASSWORT_MIN:
            return f"Das neue Passwort muss mindestens {PASSWORT_MIN} Zeichen haben."
        with self._lock:
            write_json(self.zugang_path, {"benutzer": benutzer, "passwort": hash_password(neu)}, mode=0o600)
        return None

    # -------------------------------------------------------- Sitzungsschlüssel
    def session_secret(self) -> str:
        if self.base.session_secret:
            return self.base.session_secret
        pfad = self.dir / "session_secret"
        try:
            wert = pfad.read_text(encoding="ascii").strip()
            if wert:
                return wert
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("Sitzungsschlüssel nicht lesbar: %s", exc)
        wert = secrets.token_urlsafe(48)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            pfad.write_text(wert, encoding="ascii")
            pfad.chmod(0o600)
        except OSError as exc:
            log.warning("Sitzungsschlüssel nicht speicherbar (%s) – Anmeldungen verfallen beim Neustart.", exc)
        return wert
