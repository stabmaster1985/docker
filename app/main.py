"""Container-Dashboard: FastAPI-App mit serverseitig gerendertem HTML und htmx."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, fmt
from .action_log import ActionLog
from .config import Settings, load_settings
from .docker_service import (ACTIONS, LOG_ACTIONS, DashboardError, DockerService,
                             group_containers)
from .image_updates import ImageUpdateChecker
from .store import FELDER, PASSWORT_MIN, Store
from .updater import UpdaterBridge, running_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dashboard")

BASE_DIR = Path(__file__).resolve().parent

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
# Das Cookie selbst lebt lange; die eigentliche Sitzungsdauer prüft current_user()
# anhand der Einstellung, damit sie ohne Neustart änderbar ist.
COOKIE_MAX_AGE = 31 * 86400


class NotAuthenticated(Exception):
    pass


def create_app(settings: Settings | None = None, *, client=None, hostname: str | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings)
    service = DockerService(store, client=client, hostname=hostname)
    data_dir = Path(settings.data_dir)
    action_log = ActionLog(data_dir / "aktionen.json", lambda: store.s.action_log_max)
    limiter = auth.LoginLimiter(lambda: store.s.login_max_attempts, lambda: store.s.login_lock_seconds)
    bridge = UpdaterBridge(data_dir)
    checker = ImageUpdateChecker(store, service, data_dir / "image-updates.json")
    version = running_version()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background:
            checker.start()
        yield
        checker.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.service = service
    app.state.action_log = action_log
    app.state.bridge = bridge
    app.state.checker = checker
    app.state.hinweise = 0

    app.add_middleware(
        SessionMiddleware,
        secret_key=store.session_secret(),
        session_cookie="dashboard_session",
        max_age=COOKIE_MAX_AGE,
        same_site="strict",
        https_only=settings.cookie_secure,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters.update(
        bytes=fmt.bytes_text, prozent=fmt.prozent, zeitstempel=fmt.zeitstempel,
    )
    templates.env.globals.update(ACTIONS=ACTIONS, LOG_ACTIONS=LOG_ACTIONS, version=version,
                                 PASSWORT_MIN=PASSWORT_MIN)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, _exc: NotAuthenticated):
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    def current_user(request: Request) -> str | None:
        return auth.current_user(request, store.s.session_max_age)

    def require_user(request: Request) -> str:
        user = current_user(request)
        if not user:
            raise NotAuthenticated()
        return user

    async def require_post(request: Request) -> tuple[str, dict[str, str]]:
        """Angemeldet + gültiges CSRF-Token (Header von htmx oder Formularfeld)."""
        user = require_user(request)
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        token = request.headers.get("X-CSRF-Token") or form.get("csrf")
        if not auth.csrf_valid(request, token):
            raise CsrfError()
        return user, form

    class CsrfError(Exception):
        pass

    @app.exception_handler(CsrfError)
    async def _csrf(_request: Request, _exc: CsrfError):
        return Response("Ungültiges CSRF-Token – bitte Seite neu laden.", status_code=403)

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("user", current_user(request))
        ctx.setdefault("csrf", auth.csrf_token(request))
        ctx.setdefault("s", store.s)
        ctx.setdefault("hinweise", app.state.hinweise)
        ctx.setdefault("toasts", [])
        return templates.TemplateResponse(request, name, ctx)

    def updater_status() -> dict:
        return bridge.status()

    def finished_jobs_toasts() -> list[dict]:
        """Fertige Container-Updates melden: Hinweis, Aktionslog, Update-Stand neu prüfen."""
        toasts = []
        for job in bridge.new_finished():
            ok = job.get("phase") == "fertig"
            meldung = job.get("meldung") or ""
            action_log.add(job.get("benutzer") or "Update-Dienst", "update",
                           job.get("name") or job.get("container", "")[:12], ok, meldung)
            toasts.append({"ok": ok, "text": meldung})
            if job.get("ref"):
                checker.check_in_background(job["ref"])
        return toasts

    def decorate(infos) -> None:
        pending = bridge.pending_containers()
        for c in infos:
            c.update = checker.status(c.image_ref, c.image_id)
            c.update_pending = c.id in pending

    def list_context() -> dict:
        try:
            infos = service.list_containers()
            fehler = None
        except DashboardError as exc:
            infos, fehler = [], str(exc)
        decorate(infos)
        status = updater_status()
        update_count = sum(1 for c in infos if c.update.hat_update)
        app.state.hinweise = update_count + (1 if status.get("phase") == "bereit" else 0)
        return {
            "gruppen": group_containers(infos),
            "auffaellig": [c for c in infos if c.auffaellig],
            "gesamt": len(infos),
            "laufend": sum(1 for c in infos if c.status == "running"),
            "update_count": update_count,
            "fehler": fehler,
            "stand": datetime.now().astimezone().strftime("%H:%M:%S"),
            "updater_ok": status.get("verfuegbar", False),
            "toasts": finished_jobs_toasts(),
            "hinweise": app.state.hinweise,
        }

    # ------------------------------------------------------------- Anmeldung
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", fehler=None)

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
        ip = auth.client_ip(request)
        if not auth.csrf_valid(request, str(form.get("csrf", ""))):
            return render(request, "login.html", fehler="Sitzung abgelaufen, bitte erneut versuchen.")
        if (rest := limiter.locked_for(ip)):
            return render(request, "login.html",
                          fehler=f"Zu viele Fehlversuche. Bitte in {rest} Sekunden erneut versuchen.")
        if not store.check_login(username, password):
            limiter.fail(ip)
            log.warning("Fehlgeschlagene Anmeldung für „%s“ von %s", username, ip)
            return render(request, "login.html", fehler="Benutzername oder Passwort falsch.")
        limiter.reset(ip)
        request.session.clear()  # keine Sitzungsdaten aus der Zeit vor dem Login übernehmen
        request.session["user"] = store.username
        request.session["login_at"] = time.time()
        auth.csrf_token(request)
        log.info("Anmeldung „%s“ von %s", store.username, ip)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        if auth.csrf_valid(request, str(form.get("csrf", ""))):
            request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------ Übersicht
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        require_user(request)
        return render(request, "index.html", **list_context())

    @app.get("/partials/liste", response_class=HTMLResponse)
    def liste(request: Request):
        require_user(request)
        return render(request, "_liste.html", **list_context())

    # -------------------------------------------------------------- Aktionen
    def request_container_update(user: str, container_id: str) -> tuple[bool, str]:
        try:
            c = service.get(container_id, with_stats=False)
        except DashboardError as exc:
            return False, str(exc)
        if c.protected:
            return False, f"„{c.name}“ ist geschützt ({c.protected_reason})."
        if not c.can_update:
            return False, f"„{c.name}“ gehört zu keinem Compose-Projekt und kann nicht aktualisiert werden."
        if not updater_status().get("verfuegbar"):
            return False, "Der Update-Dienst auf dem Server ist nicht eingerichtet (siehe „Updates“)."
        if c.id in bridge.pending_containers():
            return False, f"Für „{c.name}“ läuft bereits ein Update."
        bridge.request("container", user, container=c.id, name=c.name, ref=c.image_ref)
        action_log.add(user, "update-angefordert", c.name, True, "an den Update-Dienst übergeben")
        return True, f"Update für „{c.name}“ läuft im Hintergrund – das Ergebnis erscheint als Hinweis."

    @app.post("/container/{container_id}/{action}", response_class=HTMLResponse)
    async def aktion(request: Request, container_id: str, action: str):
        user, _ = await require_post(request)
        if action == "update":
            ok, meldung = request_container_update(user, container_id)
        else:
            try:
                name, meldung = service.run_action(container_id, action)
                ok = True
            except DashboardError as exc:
                name, meldung, ok = container_id[:12], str(exc), False
            if action in ACTIONS:
                action_log.add(user, action, name, ok, meldung)
            log.info("Aktion %s auf %s durch %s: %s", action, name, user, meldung)

        toast = {"ok": ok, "text": meldung}
        ziel = request.headers.get("HX-Target")
        if ziel == "detail-kopf":
            try:
                c = service.get(container_id)
            except DashboardError as exc:
                antwort = render(request, "_toast.html", toast={"ok": False, "text": str(exc)})
                antwort.headers["HX-Reswap"] = "none"
                return antwort
            decorate([c])
            return render(request, "_detail_kopf.html", c=c, toast=toast,
                          updater_ok=updater_status().get("verfuegbar", False))
        if ziel == "container-updates":
            return render(request, "_container_updates.html", toast=toast, **updates_context())
        return render(request, "_liste.html", toast=toast, **list_context())

    # ----------------------------------------------------------- Detail/Logs
    @app.get("/container/{container_id}", response_class=HTMLResponse)
    def detail(request: Request, container_id: str):
        require_user(request)
        try:
            c = service.get(container_id)
            zeilen = service.logs(container_id, store.s.log_tail)
        except DashboardError as exc:
            return render(request, "fehler.html", fehler=str(exc))
        decorate([c])
        return render(request, "detail.html", c=c, zeilen=zeilen, fehler=None, tail=store.s.log_tail,
                      updater_ok=updater_status().get("verfuegbar", False))

    @app.get("/container/{container_id}/logs", response_class=HTMLResponse)
    def logs(request: Request, container_id: str):
        require_user(request)
        try:
            zeilen, fehler = service.logs(container_id, store.s.log_tail), None
        except DashboardError as exc:
            zeilen, fehler = [], str(exc)
        return render(request, "_logs.html", zeilen=zeilen, fehler=fehler, tail=store.s.log_tail)

    # --------------------------------------------------------------- Updates
    def version_context() -> dict:
        status = updater_status()
        laufend = version.get("commit") or status.get("aktuell") or ""
        return {"status": status, "laufend": laufend}

    def updates_context() -> dict:
        try:
            infos = service.list_containers(with_stats=False)
            fehler = None
        except DashboardError as exc:
            infos, fehler = [], str(exc)
        decorate(infos)
        reihenfolge = {"update": 0, "neu": 1, "fehler": 2, "offen": 3, "aktuell": 4, "lokal": 5, "fest": 6}
        infos.sort(key=lambda c: (reihenfolge.get(c.update.code, 9), c.project, c.name))
        jobs = bridge.jobs(limit=15)
        return {
            "container": infos,
            "fehler": fehler,
            "jobs": jobs,
            "pruefung_laeuft": checker.laeuft,
            "letzte_pruefung": checker.letzte_pruefung,
            "updater_ok": updater_status().get("verfuegbar", False),
            "beschaeftigt": checker.laeuft or any(j.get("phase") not in ("fertig", "fehler") for j in jobs)
                            or bridge.open_requests() > 0,
            "toasts": finished_jobs_toasts(),
        }

    @app.get("/updates", response_class=HTMLResponse)
    def updates(request: Request):
        require_user(request)
        return render(request, "updates.html", **version_context(), **updates_context())

    @app.get("/partials/version", response_class=HTMLResponse)
    def partial_version(request: Request):
        require_user(request)
        return render(request, "_version.html", **version_context())

    @app.get("/partials/container-updates", response_class=HTMLResponse)
    def partial_container_updates(request: Request):
        require_user(request)
        return render(request, "_container_updates.html", **updates_context())

    @app.post("/updates/dashboard/{schritt}", response_class=HTMLResponse)
    async def dashboard_update(request: Request, schritt: str):
        user, _ = await require_post(request)
        status = updater_status()
        if not status.get("verfuegbar"):
            toast = {"ok": False, "text": "Der Update-Dienst ist nicht eingerichtet."}
        elif status.get("beschaeftigt") or status.get("auftrag_offen"):
            toast = {"ok": False, "text": "Der Update-Dienst ist gerade beschäftigt."}
        elif schritt == "pruefen":
            bridge.request("vorbereiten", user)
            toast = {"ok": True, "text": "Suche nach einer neuen Version …"}
        elif schritt == "umschalten" and status.get("phase") == "bereit":
            bridge.request("umschalten", user)
            action_log.add(user, "dashboard-update", "Dashboard", True,
                           f"Umschalten auf {str(status.get('bereit', ''))[:7]} angefordert")
            toast = {"ok": True, "text": "Neue Version wird gestartet – die Seite lädt gleich neu."}
        else:
            toast = {"ok": False, "text": "Es ist keine neue Version vorbereitet."}
        return render(request, "_version.html", toast=toast, **version_context())

    @app.post("/updates/images/pruefen", response_class=HTMLResponse)
    async def images_pruefen(request: Request):
        user, _ = await require_post(request)
        if checker.laeuft:
            toast = {"ok": False, "text": "Die Prüfung läuft bereits."}
        else:
            checker.check_in_background()
            action_log.add(user, "update-pruefung", "alle Images", True, "Prüfung gestartet")
            toast = {"ok": True, "text": "Prüfung gestartet – das dauert je nach Anzahl der Images etwas."}
        ctx = updates_context()
        ctx["beschaeftigt"] = True
        return render(request, "_container_updates.html", toast=toast, **ctx)

    # ---------------------------------------------------------- Einstellungen
    def settings_context(**extra) -> dict:
        try:
            infos = service.list_containers(with_stats=False, include_hidden=True)
            fehler = None
        except DashboardError as exc:
            infos, fehler = [], str(exc)
        infos.sort(key=lambda c: (c.project or "~", c.name))
        gruppen: dict[str, list] = {}
        for feld in FELDER:
            gruppen.setdefault(feld.gruppe, []).append(feld)
        return {"store": store, "feldgruppen": gruppen, "container": infos, "fehler": fehler,
                "formfehler": [], **extra}

    @app.get("/einstellungen", response_class=HTMLResponse)
    def einstellungen(request: Request):
        require_user(request)
        return render(request, "einstellungen.html", **settings_context())

    @app.post("/einstellungen/allgemein", response_class=HTMLResponse)
    async def einstellungen_speichern(request: Request):
        user, form = await require_post(request)
        fehler = store.update(form)
        if fehler:
            toast = {"ok": False, "text": "Nicht gespeichert – bitte Eingaben prüfen."}
        else:
            toast = {"ok": True, "text": "Einstellungen gespeichert."}
            action_log.add(user, "einstellungen", "–", True, "Einstellungen geändert")
        return render(request, "_einstellungen_allgemein.html", toast=toast,
                      **settings_context(formfehler=fehler))

    @app.post("/einstellungen/zuruecksetzen", response_class=HTMLResponse)
    async def einstellungen_zuruecksetzen(request: Request):
        user, _ = await require_post(request)
        store.reset()
        action_log.add(user, "einstellungen", "–", True, "auf Werte der .env zurückgesetzt")
        return render(request, "_einstellungen_allgemein.html",
                      toast={"ok": True, "text": "Auf die Werte aus der .env zurückgesetzt."},
                      **settings_context())

    @app.post("/einstellungen/container", response_class=HTMLResponse)
    async def einstellungen_container(request: Request):
        user, form = await require_post(request)
        name = form.get("name", "").strip()
        if not name:
            return Response("Name fehlt.", status_code=400)
        hidden = form.get("ausgeblendet") == "on"
        protected = form.get("geschuetzt") == "on"
        store.set_container(name, hidden, protected)
        teile = [("ausgeblendet" if hidden else "sichtbar"), ("geschützt" if protected else "nicht geschützt")]
        action_log.add(user, "container-einstellung", name, True, ", ".join(teile))
        antwort = render(request, "_toast.html", toast={"ok": True, "text": f"„{name}“: {', '.join(teile)}."})
        antwort.headers["HX-Reswap"] = "none"
        return antwort

    @app.post("/einstellungen/zugang", response_class=HTMLResponse)
    async def einstellungen_zugang(request: Request):
        user, form = await require_post(request)
        fehler = store.change_access(form.get("alt", ""), form.get("benutzer", ""),
                                     form.get("neu", ""), form.get("neu2", ""))
        if fehler:
            toast = {"ok": False, "text": fehler}
        else:
            request.session["user"] = store.username
            action_log.add(store.username, "zugang", "–", True, f"Zugangsdaten geändert (vorher „{user}“)")
            toast = {"ok": True, "text": "Zugangsdaten geändert."}
        return render(request, "_einstellungen_zugang.html", toast=toast, store=store,
                      user=store.username)

    @app.post("/einstellungen/aktionslog-leeren", response_class=HTMLResponse)
    async def aktionslog_leeren(request: Request):
        user, _ = await require_post(request)
        action_log.clear()
        action_log.add(user, "aktionslog", "–", True, "Aktionslog geleert")
        antwort = render(request, "_toast.html", toast={"ok": True, "text": "Aktionslog geleert."})
        antwort.headers["HX-Reswap"] = "none"
        return antwort

    # -------------------------------------------------------------- Aktionslog
    @app.get("/aktionen", response_class=HTMLResponse)
    def aktionen(request: Request):
        require_user(request)
        return render(request, "aktionen.html", eintraege=action_log.entries(limit=200))

    # --------------------------------------------------------------- Health
    @app.get("/healthz")
    def healthz():
        # Lebenszeichen der App; ob Docker erreichbar ist, steht nur informativ dabei,
        # damit ein kurzer Proxy-Ausfall das Dashboard nicht als „unhealthy“ markiert.
        return JSONResponse({"status": "ok", "docker": service.ping(),
                             "version": (version.get("commit") or "")[:7]})

    return app
