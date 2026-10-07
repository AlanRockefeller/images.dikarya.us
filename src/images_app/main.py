"""The FastAPI application (served by uvicorn as images_app.main:app)."""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from images_app.config import PACKAGE_ROOT, settings
from images_app.routes import account, admin, public, ranking
from images_app.security import CSRF_COOKIE, new_csrf_token
from images_app.web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("images_app")

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


@app.middleware("http")
async def site_middleware(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if host in ALIAS_HOSTS:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{settings.base_url}{request.url.path}{query}", status_code=301)

    cookie_token = request.cookies.get(CSRF_COOKIE)
    request.state.csrf_token = cookie_token if cookie_token and len(cookie_token) <= 64 else new_csrf_token()
    response: Response = await call_next(request)
    if request.state.csrf_token != cookie_token:
        response.set_cookie(
            CSRF_COOKIE, request.state.csrf_token, httponly=True, samesite="lax",
            secure=settings.production, path="/", max_age=60 * 60 * 24 * 365,
        )
    headers = response.headers
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
    if _wants_json(request):
        return JSONResponse({"error": "Invalid request."}, status_code=422)
    return render(request, "error.html", status_code=422, title="Invalid request",
                  message="Some of the submitted values were not valid.")


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    if _wants_json(request):
        return JSONResponse({"error": "Server error."}, status_code=500)
    if not hasattr(request.state, "csrf_token"):
        request.state.csrf_token = new_csrf_token()
    return render(request, "error.html", status_code=500, title="Something went wrong",
                  message="The problem has been logged. Please try again shortly.")
