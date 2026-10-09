"""The FastAPI application (served by uvicorn as images_app.main:app)."""

from __future__ import annotations

import logging
import re
import secrets
import time
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from images_app import logs, threats
from images_app.config import PACKAGE_ROOT, settings
from images_app.routes import account, admin, public, ranking
from images_app.security import CSRF_COOKIE, new_csrf_token
from images_app.db import SessionLocal
from images_app.web import current_user, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("images_app")
logs.configure()

ALIAS_HOSTS = {"image.dikarya.us"}
IMAGE_HOSTS = (
    "https://inaturalist-open-data.s3.amazonaws.com https://static.inaturalist.org "
    "https://mushroomobserver.org https://*.mushroomobserver.org"
)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
    f"img-src 'self' data: {IMAGE_HOSTS}; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


class CachedStaticFiles(StaticFiles):
    """Static files are requested with a content-hash query string, so cache them for a year."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200 and b"v=" in scope.get("query_string", b""):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app = FastAPI(title="Dikarya Images", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", CachedStaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
for module in (public, account, ranking, admin):
    app.include_router(module.router)


ATTACK_TOOLS = re.compile(
    r"sqlmap|nikto|acunetix|netsparker|burp|havij|w3af|wpscan|dirbuster|gobuster|feroxbuster|ffuf|wfuzz"
    r"|hydra|commix|xsstrike|arachni|skipfish|nessus|openvas|nmap scripting engine",
    re.IGNORECASE,
)
SCANNER_AGENTS = re.compile(
    r"zgrab|masscan|nuclei|censys|shodan|expanse|internet-measurement|leakix|netcraft|l9explore|odin\.io",
    re.IGNORECASE,
)


def _known_route(scope) -> bool:
    return any(route.matches(scope)[0] != Match.NONE for route in app.router.routes)


def _identify(request: Request) -> None:
    if not hasattr(request.state, "user"):
        with SessionLocal() as db:
            current_user(request, db)


def _classify(request: Request) -> None:
    """Log hostile-looking requests before they are handled.

    Attack payloads or attack tools aimed at one of this site's real pages are
    "targeted"; probes for software we don't run are background "scanner" noise.
    """
    known = _known_route(request.scope)
    params = [item for pair in request.query_params.multi_items() for item in pair]
    found = threats.find_payload(
        request.url.path, *params, request.headers.get("user-agent"), request.headers.get("referer"),
        *request.cookies.values(),
    )
    agent = request.headers.get("user-agent", "")
    if found or ATTACK_TOOLS.search(agent):
        _identify(request)  # so attacks by signed-in members are attributed to them
    if found:
        stream = logs.attack if known else logs.scanner
        logs.event(stream, f"payload.{found[0]}", request, logging.WARNING,
                   query=request.url.query or None, sample=found[1])
        request.state.classified = True
    elif not known and threats.is_scanner_path(request.url.path):
        logs.event(logs.scanner, "probe", request, query=request.url.query or None)
        request.state.classified = True
    elif ATTACK_TOOLS.search(agent):
        stream = logs.attack if known else logs.scanner
        logs.event(stream, "attack_tool", request, logging.WARNING, query=request.url.query or None)
        request.state.classified = True
    elif SCANNER_AGENTS.search(agent):
        logs.event(logs.scanner, "scanner_agent", request)
        request.state.classified = True


@app.middleware("http")
async def site_middleware(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if host in ALIAS_HOSTS:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{settings.base_url}{request.url.path}{query}", status_code=301)

    started = time.monotonic()
    request.state.request_id = secrets.token_hex(6)
    request.state.classified = False
    cookie_token = request.cookies.get(CSRF_COOKIE)
    request.state.csrf_token = cookie_token if cookie_token and len(cookie_token) <= 64 else new_csrf_token()
    # Everything an IP does for a while after an attack goes to the attack log too.
    watched = logs.is_watched(logs.client_ip(request))
    _classify(request)

    response: Response = await call_next(request)

    status = response.status_code
    if watched:
        logs.event(logs.attack, "watched_ip.request", request, status=status,
                   query=request.url.query or None, ms=round(1000 * (time.monotonic() - started)))
    if status == 404 and not request.state.classified:
        referer = request.headers.get("referer", "")
        if urlsplit(referer).netloc in (request.url.netloc, settings.canonical_host):
            logs.event(logs.activity, "broken_link", request, logging.WARNING, referer=referer)
        else:
            logs.event(logs.scanner, "not_found", request, query=request.url.query or None)
    elif status == 405 and not request.state.classified:
        logs.event(logs.scanner, "method_not_allowed", request)

    if request.state.csrf_token != cookie_token:
        response.set_cookie(
            CSRF_COOKIE, request.state.csrf_token, httponly=True, samesite="lax",
            secure=settings.production, path="/", max_age=60 * 60 * 24 * 365,
        )
    headers = response.headers
    headers["X-Request-ID"] = request.state.request_id
    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if "text/html" in headers.get("content-type", ""):
        headers.setdefault("Cache-Control", "no-store" if "set-cookie" in headers else "no-cache")
    return response


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        # After logging in, return to the page (a form's target can't be revisited with GET).
        if request.method == "GET":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        else:
            target = urlsplit(request.headers.get("referer", "")).path or "/"
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    if not hasattr(request.state, "csrf_token"):
        request.state.csrf_token = new_csrf_token()
    titles = {404: "Page not found", 403: "Not allowed", 429: "Slow down"}
    return render(
        request, "error.html", status_code=exc.status_code,
        title=titles.get(exc.status_code, "Something went wrong"), message=exc.detail,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    problems = "; ".join(f"{'.'.join(map(str, e.get('loc', ())))}: {e.get('msg')}" for e in exc.errors())
    if _wants_json(request):
        # Our own pages never send malformed API calls; something else is poking at the API.
        logs.event(logs.attack, "api.malformed_request", request, logging.WARNING, problems=problems)
        return JSONResponse({"error": "Invalid request."}, status_code=422)
    logs.event(logs.activity, "form.invalid", request, logging.WARNING, problems=problems)
    return render(request, "error.html", status_code=422, title="Invalid request",
                  message="Some of the submitted values were not valid.")


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s (request %s)", request.method, request.url.path,
                  getattr(request.state, "request_id", "-"))
    logs.event(logs.activity, "server.error", request, logging.ERROR, error=type(exc).__name__)
    if _wants_json(request):
        return JSONResponse({"error": "Server error."}, status_code=500)
    if not hasattr(request.state, "csrf_token"):
        request.state.csrf_token = new_csrf_token()
    return render(request, "error.html", status_code=500, title="Something went wrong",
                  message="The problem has been logged. Please try again shortly.")
