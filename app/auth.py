"""Anmeldung, Sitzung, CSRF-Schutz und Bremse gegen Passwort-Raten."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict

from fastapi import Request

from .config import Settings


def _digest(wert: str) -> bytes:
    return hashlib.sha256(wert.encode("utf-8")).digest()


def check_credentials(settings: Settings, user: str, password: str) -> bool:
    # Beide Vergleiche immer ausführen und in konstanter Zeit, damit die
    # Antwortzeit nichts über Benutzername oder Passwort verrät.
    user_ok = hmac.compare_digest(_digest(user), _digest(settings.username))
    pass_ok = hmac.compare_digest(_digest(password), _digest(settings.password))
    return user_ok and pass_ok


class LoginLimiter:
    """Sperrt eine IP nach zu vielen Fehlversuchen für eine Weile."""

    def __init__(self, max_attempts: int, lock_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.lock_seconds = lock_seconds
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        versuche = [t for t in self._fails.get(ip, []) if now - t < self.lock_seconds]
        if versuche:
            self._fails[ip] = versuche
        else:
            self._fails.pop(ip, None)
        return versuche

    def locked_for(self, ip: str, now: float | None = None) -> int:
        """Restliche Sperrzeit in Sekunden, 0 wenn nicht gesperrt."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            versuche = self._recent(ip, now)
            if len(versuche) < self.max_attempts:
                return 0
            return int(self.lock_seconds - (now - versuche[0])) + 1

    def fail(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._recent(ip, now)
            self._fails[ip].append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def current_user(request: Request) -> str | None:
    return request.session.get("user")


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def csrf_valid(request: Request, sent: str | None) -> bool:
    erwartet = request.session.get("csrf")
    return bool(erwartet and sent) and hmac.compare_digest(erwartet, sent)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"
