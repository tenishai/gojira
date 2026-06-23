"""Gojira web front door: FastAPI + Jinja2 + htmx over the service layer.

`create_app(session_factory, clients)` is a factory so tests can inject an
in-memory DB and fake clients. Provisioning runs in a background thread (see
service.start_provision); the app-detail page htmx-polls /apps/{id}/status until
the run reaches a terminal state.
"""

import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from gojira import service, webhooks
from gojira.config import Config, load_config
from gojira.opinions import WORKLOAD_CLASSES
from gojira.service import ProvisionClients

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(
    session_factory: sessionmaker[Session],
    clients: ProvisionClients | None = None,
    config: Config | None = None,
) -> FastAPI:
    app = FastAPI(title="Gojira")
    app.state.session_factory = session_factory
    app.state.config = config or Config()
    app.state.clients = clients or ProvisionClients.real(
        config=app.state.config, session_factory=session_factory
    )

    if app.state.config.drift.enabled:
        @app.on_event("startup")
        def _start_drift_scanner():
            _, stop = service.start_drift_scanner(
                session_factory, app.state.clients, app.state.config
            )
            app.state.drift_stop = stop
            log.info("drift scanner started (every %ss)", app.state.config.drift.interval_seconds)

        @app.on_event("shutdown")
        def _stop_drift_scanner():
            stop = getattr(app.state, "drift_stop", None)
            if stop is not None:
                stop.set()

    def get_session():
        with session_factory() as s:
            yield s

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(get_session)):
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"apps": service.list_apps(session)}
        )

    @app.get("/clusters", response_class=HTMLResponse)
    def clusters(request: Request, session: Session = Depends(get_session), error: str = ""):
        return TEMPLATES.TemplateResponse(
            request,
            "clusters.html",
            {"clusters": service.list_clusters(session), "error": error},
        )

    @app.post("/clusters")
    def add_cluster(
        request: Request,
        session: Session = Depends(get_session),
        name: str = Form(...),
        gcp_project: str = Form(...),
        location: str = Form(...),
        gke_cluster: str = Form(...),
        ar_repo: str = Form(""),
    ):
        try:
            service.register_cluster(
                session,
                name=name,
                gcp_project=gcp_project,
                location=location,
                gke_cluster=gke_cluster,
                ar_repo=ar_repo or None,
                gcp=request.app.state.clients.gcp,
            )
        except Exception as exc:  # connectivity check or DB error → show it
            log.warning("cluster registration failed: %s", exc)
            return RedirectResponse(f"/clusters?error={exc}", status_code=303)
        return RedirectResponse("/clusters", status_code=303)

    @app.get("/connect", response_class=HTMLResponse)
    def connect_form(request: Request, session: Session = Depends(get_session), error: str = ""):
        return TEMPLATES.TemplateResponse(
            request,
            "connect.html",
            {
                "clusters": service.list_clusters(session),
                "repos": service.list_repos(request.app.state.clients.github),
                "classes": list(WORKLOAD_CLASSES),  # canonical order: web first
                "env_names": request.app.state.config.defaults.environments,
                "error": error,
            },
        )

    @app.post("/connect")
    async def connect(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        # repo: dropdown choice, overridden by a manual entry if the operator typed one
        repo = (form.get("repo_manual") or "").strip() or (form.get("repo_select") or "").strip()
        if not repo:
            return RedirectResponse("/connect?error=pick+a+repo", status_code=303)
        existing = service.app_by_repo(session, repo)
        if existing:  # already connected — take them to it rather than 500 on the unique constraint
            return RedirectResponse(f"/apps/{existing.id}", status_code=303)
        if not service.repo_has_dockerfile(request.app.state.clients.github, repo):
            msg = f"{repo} has no Dockerfile — Gojira needs a containerizable app at the repo root"
            return RedirectResponse(f"/connect?error={msg}", status_code=303)
        name = (form.get("name") or "").strip()
        if not name:
            return RedirectResponse("/connect?error=app+name+is+required", status_code=303)
        env_names = request.app.state.config.defaults.environments
        env_map: dict[str, tuple[int, str]] = {}
        for env in env_names:
            cluster_id = form.get(f"cluster_{env}")
            if not cluster_id:
                continue  # no cluster picked → skip this environment
            # blank namespace defaults to the app name, so a picked cluster is
            # never silently dropped just because the namespace was left empty
            namespace = (form.get(f"namespace_{env}") or "").strip() or name
            env_map[env] = (int(cluster_id), namespace)
        if not env_map:
            return RedirectResponse("/connect?error=map+at+least+one+environment", status_code=303)
        # Resolve the App installation now so it's recorded on the app; harmless
        # 0 under the gh-CLI backend. A transient lookup failure shouldn't block.
        try:
            installation_id = request.app.state.clients.github.installation_id_for(repo)
        except Exception as exc:
            log.warning("could not resolve installation for %s: %s", repo, exc)
            installation_id = 0
        app_obj = service.connect_app(
            session,
            name=name,
            repo_full_name=repo,
            workload_class=form["workload_class"],
            env_map=env_map,
            installation_id=installation_id,
        )
        service.start_provision(
            request.app.state.session_factory,
            app_obj.id,
            clients=request.app.state.clients,
            config=request.app.state.config,
        )
        return RedirectResponse(f"/apps/{app_obj.id}", status_code=303)

    @app.get("/apps/{app_id}", response_class=HTMLResponse)
    def app_detail(app_id: int, request: Request, session: Session = Depends(get_session)):
        view = service.app_status(session, app_id)
        return TEMPLATES.TemplateResponse(request, "app_detail.html", {"v": view})

    @app.get("/apps/{app_id}/status", response_class=HTMLResponse)
    def app_status_partial(
        app_id: int, request: Request, session: Session = Depends(get_session)
    ):
        view = service.app_status(session, app_id)
        return TEMPLATES.TemplateResponse(request, "_status.html", {"v": view})

    @app.post("/apps/{app_id}/delete")
    def delete_app_route(
        app_id: int,
        request: Request,
        session: Session = Depends(get_session),
        delete_namespaces: str | None = Form(None),
    ):
        service.delete_app(
            session,
            app_id,
            request.app.state.clients.kubectl_for,
            delete_namespaces=bool(delete_namespaces),
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/apps/{app_id}/sync")
    def sync(app_id: int, request: Request, session: Session = Depends(get_session)):
        service.sync_app(session, app_id, request.app.state.clients.github)
        return RedirectResponse(f"/apps/{app_id}", status_code=303)

    @app.post("/apps/{app_id}/scan-drift")
    def scan_drift(app_id: int, request: Request, session: Session = Depends(get_session)):
        service.scan_app_drift(session, app_id, request.app.state.clients)
        return RedirectResponse(f"/apps/{app_id}", status_code=303)

    @app.post("/apps/{app_id}/promote")
    def promote(
        app_id: int,
        request: Request,
        session: Session = Depends(get_session),
        version: str = Form(...),
    ):
        view = service.app_status(session, app_id)
        repo = view.app.repo_full_name
        try:
            service.cut_release(request.app.state.clients.github, repo, version)
        except Exception as exc:
            log.warning("promote failed for %s: %s", repo, exc)
            return RedirectResponse(f"/apps/{app_id}", status_code=303)
        # send them to the Actions run that the tag just triggered
        return RedirectResponse(f"https://github.com/{repo}/actions", status_code=303)

    @app.post("/apps/{app_id}/provision")
    def reprovision(app_id: int, request: Request):
        service.start_provision(
            request.app.state.session_factory,
            app_id,
            clients=request.app.state.clients,
            config=request.app.state.config,
        )
        return RedirectResponse(f"/apps/{app_id}", status_code=303)

    @app.post("/webhook/github")
    async def github_webhook(request: Request, session: Session = Depends(get_session)):
        body = await request.body()
        secret = request.app.state.config.github.webhook_secret
        if not webhooks.verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid payload"}, status_code=400)
        event_type = request.headers.get("X-GitHub-Event", "")
        result = webhooks.handle_event(session, event_type, payload)
        log.info("webhook %s: %s", event_type, result or "ignored")
        return JSONResponse({"ok": True, "result": result}, status_code=202)

    return app


def build_default_app() -> FastAPI:
    """Wire a real app from config: file/Postgres DB + real clients."""
    from gojira.db import init_db, make_engine, make_session_factory

    config = load_config()
    engine = make_engine(config)
    init_db(engine)
    return create_app(make_session_factory(engine), config=config)
