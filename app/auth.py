"""Anmeldung, Sitzung, CSRF-Schutz und Bremse gegen Passwort-Raten."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict
from collections.abc import Callable

from fastapi import Request


class LoginLimiter:
    """Sperrt eine IP nach zu vielen Fehlversuchen für eine Weile."""

    def __init__(self, max_attempts: Callable[[], int], lock_seconds: Callable[[], int]) -> None:
        self._max_attempts = max_attempts
        self._lock_seconds = lock_seconds
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    @property
    def max_attempts(self) -> int:
        return self._max_attempts()

    @property
    def lock_seconds(self) -> int:
        return self._lock_seconds()

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


def current_user(request: Request, max_age: int | None = None) -> str | None:
    """Angemeldeter Benutzer; None, wenn die Sitzung abgelaufen ist."""
    user = request.session.get("user")
    if user and max_age is not None:
        seit = time.time() - float(request.session.get("login_at", 0))
        if seit > max_age:
            request.session.clear()
            return None
    return user


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
