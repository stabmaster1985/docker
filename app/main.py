"""Container-Dashboard: FastAPI-App mit serverseitig gerendertem HTML und htmx."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, fmt
from .action_log import ActionLog
from .config import Settings, load_settings
from .docker_service import ACTIONS, DashboardError, DockerService, group_containers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dashboard")

BASE_DIR = Path(__file__).resolve().parent

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class NotAuthenticated(Exception):
    pass


def create_app(settings: Settings | None = None, service: DockerService | None = None) -> FastAPI:
    settings = settings or load_settings()
    service = service or DockerService(settings)
    action_log = ActionLog(Path(settings.data_dir) / "aktionen.json", settings.action_log_max)
    limiter = auth.LoginLimiter(settings.login_max_attempts, settings.login_lock_seconds)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.service = service
    app.state.action_log = action_log

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="dashboard_session",
        max_age=settings.session_max_age,
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
    templates.env.globals.update(ACTIONS=ACTIONS, refresh_seconds=settings.refresh_seconds)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, _exc: NotAuthenticated):
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    def require_user(request: Request) -> str:
        user = auth.current_user(request)
        if not user:
            raise NotAuthenticated()
        return user

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("user", auth.current_user(request))
        ctx.setdefault("csrf", auth.csrf_token(request))
        return templates.TemplateResponse(request, name, ctx)

    def list_context() -> dict:
        try:
            infos = service.list_containers()
            fehler = None
        except DashboardError as exc:
            infos, fehler = [], str(exc)
        auffaellig = [c for c in infos if c.auffaellig]
        return {
            "gruppen": group_containers(infos),
            "auffaellig": auffaellig,
            "gesamt": len(infos),
            "laufend": sum(1 for c in infos if c.status == "running"),
            "fehler": fehler,
            "stand": datetime.now().astimezone().strftime("%H:%M:%S"),
        }

    # ------------------------------------------------------------- Anmeldung
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if auth.current_user(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", fehler=None)

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, username: str = Form(""), password: str = Form(""),
              csrf: str = Form("")):
        ip = auth.client_ip(request)
        if not auth.csrf_valid(request, csrf):
            return render(request, "login.html", fehler="Sitzung abgelaufen, bitte erneut versuchen.")
        if (rest := limiter.locked_for(ip)):
            return render(request, "login.html",
                          fehler=f"Zu viele Fehlversuche. Bitte in {rest} Sekunden erneut versuchen.")
        if not auth.check_credentials(settings, username, password):
            limiter.fail(ip)
            log.warning("Fehlgeschlagene Anmeldung für „%s“ von %s", username, ip)
            return render(request, "login.html", fehler="Benutzername oder Passwort falsch.")
        limiter.reset(ip)
        request.session.clear()  # keine Sitzungsdaten aus der Zeit vor dem Login übernehmen
        request.session["user"] = settings.username
        auth.csrf_token(request)
        log.info("Anmeldung „%s“ von %s", settings.username, ip)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        if auth.csrf_valid(request, csrf):
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
    @app.post("/container/{container_id}/{action}", response_class=HTMLResponse)
    def aktion(request: Request, container_id: str, action: str):
        user = require_user(request)
        if not auth.csrf_valid(request, request.headers.get("X-CSRF-Token")):
            return Response("Ungültiges CSRF-Token.", status_code=403)

        try:
            name, meldung = service.run_action(container_id, action)
            ok = True
        except DashboardError as exc:
            name, meldung, ok = container_id[:12], str(exc), False
        if action in ACTIONS:
            action_log.add(user, action, name, ok, meldung)
        log.info("Aktion %s auf %s durch %s: %s", action, name, user, meldung)

        toast = {"ok": ok, "text": meldung}
        if request.headers.get("HX-Target") == "detail-kopf":
            try:
                c = service.get(container_id)
            except DashboardError as exc:
                antwort = render(request, "_toast.html", toast={"ok": False, "text": str(exc)})
                antwort.headers["HX-Reswap"] = "none"
                return antwort
            return render(request, "_detail_kopf.html", c=c, toast=toast)
        return render(request, "_liste.html", toast=toast, **list_context())

    # ----------------------------------------------------------- Detail/Logs
    @app.get("/container/{container_id}", response_class=HTMLResponse)
    def detail(request: Request, container_id: str):
        require_user(request)
        try:
            c = service.get(container_id)
            zeilen = service.logs(container_id, settings.log_tail)
            fehler = None
        except DashboardError as exc:
            return render(request, "fehler.html", fehler=str(exc))
        return render(request, "detail.html", c=c, zeilen=zeilen, fehler=fehler,
                      tail=settings.log_tail)

    @app.get("/container/{container_id}/logs", response_class=HTMLResponse)
    def logs(request: Request, container_id: str):
        require_user(request)
        try:
            zeilen, fehler = service.logs(container_id, settings.log_tail), None
        except DashboardError as exc:
            zeilen, fehler = [], str(exc)
        return render(request, "_logs.html", zeilen=zeilen, fehler=fehler, tail=settings.log_tail)

    @app.get("/aktionen", response_class=HTMLResponse)
    def aktionen(request: Request):
        require_user(request)
        return render(request, "aktionen.html", eintraege=action_log.entries(limit=200))

    # --------------------------------------------------------------- Health
    @app.get("/healthz")
    def healthz():
        # Lebenszeichen der App; ob Docker erreichbar ist, steht nur informativ dabei,
        # damit ein kurzer Proxy-Ausfall das Dashboard nicht als „unhealthy“ markiert.
        return JSONResponse({"status": "ok", "docker": service.ping()})

    return app
