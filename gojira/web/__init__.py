"""Gojira web front door: FastAPI + Jinja2 + htmx over the service layer.

`create_app(session_factory, clients)` is a factory so tests can inject an
in-memory DB and fake clients. Provisioning runs in a background thread (see
service.start_provision); the app-detail page htmx-polls /apps/{id}/status until
the run reaches a terminal state.
"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from gojira import models, service, webhooks
from gojira.web import metrics, tracing
from gojira.config import Config, load_config
from gojira.opinions import WORKLOAD_CLASSES
from gojira.service import ProvisionClients
from gojira.web.auth import identity_from
from gojira.sizing import agent_deploy
from gojira.sizing import profile as sizing_profile
from gojira.sizing import service as sizing_service
from gojira.web import auth

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# k8s object-name charset (RFC 1123 label): these values flow unsanitized into
# Jinja2-rendered YAML and a GSA id (finding #3) — auto-merge removes the
# human-review backstop that used to catch a stray char before it got that
# far, so the API boundary has to enforce it instead.
DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def create_app(
    session_factory: sessionmaker[Session],
    clients: ProvisionClients | None = None,
    config: Config | None = None,
) -> FastAPI:
    app = FastAPI(title="Gojira")
    metrics.install(app)
    tracing.install(app)
    app.state.session_factory = session_factory
    app.state.config = config or Config()
    app.state.clients = clients or ProvisionClients.real(
        config=app.state.config, session_factory=session_factory
    )
    auth.install(app, app.state.config.auth)

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

    @app.get("/healthz")
    def healthz():
        """Probe target — identity-exempt (finding #17: kubelet has no IAP)."""
        return {"ok": True}

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
            {
                "clusters": service.list_clusters(session),
                "agents": sizing_service.agent_tokens_by_cluster(session),
                "error": error,
            },
        )

    @app.post("/clusters/{cluster_id}/deploy-agent")
    def deploy_agent(cluster_id: int, request: Request, session: Session = Depends(get_session)):
        cluster = session.get(models.Cluster, cluster_id)
        if cluster is None:
            return RedirectResponse("/clusters?error=no such cluster", status_code=303)
        clients = request.app.state.clients
        try:
            msg = agent_deploy.deploy_agent(
                session,
                cluster,
                gcp=clients.gcp,
                kubectl=clients.kubectl_for(cluster),
                sizing=request.app.state.config.sizing,
            )
            log.info("%s", msg)
        except Exception as exc:
            log.warning("agent deploy to %s failed: %s", cluster.name, exc)
            return RedirectResponse(f"/clusters?error=Agent deploy failed: {exc}", status_code=303)
        return RedirectResponse("/clusters", status_code=303)

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
            return RedirectResponse(f"/clusters?error=Registration failed: {exc}", status_code=303)
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
        if not service.repo_can_be_deployed(request.app.state.clients.github, repo):
            msg = f"{repo} {service.NOT_DEPLOYABLE}"
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
    def app_detail(
        app_id: int,
        request: Request,
        session: Session = Depends(get_session),
        error: str = "",
        notice: str = "",
        history: int = 0,
    ):
        view = service.app_status(session, app_id)
        deploys, history_error = [], ""
        if history:  # fetched only on request — a GitHub call per page view is rude
            try:
                deploys = service.deploy_history(
                    session, app_id, request.app.state.clients.github
                )
            except Exception as exc:
                log.warning("deploy history failed for app %s: %s", app_id, exc)
                history_error = "couldn't read the deploy history from GitHub"
        return TEMPLATES.TemplateResponse(
            request, "app_detail.html",
            {"v": view, "error": error, "notice": notice, "show_history": bool(history),
             "deploys": deploys, "history_error": history_error},
        )

    @app.post("/apps/{app_id}/rollback")
    def rollback(
        app_id: int,
        request: Request,
        sha: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            msg = service.rollback_app(
                session, app_id, sha=sha,
                actor=identity_from(request),
                clients=request.app.state.clients,
            )
        except Exception as exc:
            log.warning("rollback failed for app %s: %s", app_id, exc)
            return RedirectResponse(
                f"/apps/{app_id}?history=1&error={quote(str(exc)[:300])}", status_code=303
            )
        return RedirectResponse(
            f"/apps/{app_id}?history=1&notice={quote(msg)}", status_code=303
        )

    @app.post("/apps/{app_id}/revert-pr")
    def revert_pr(
        app_id: int,
        request: Request,
        sha: str = Form(""),
        session: Session = Depends(get_session),
    ):
        # The durable half of rollback: git catches up to the cluster through
        # review. The rollback banner is the only caller today.
        try:
            msg = service.open_revert_pr(
                session, app_id, sha=sha,
                github=request.app.state.clients.github,
                actor=identity_from(request),
            )
        except Exception as exc:
            log.warning("revert PR failed for app %s: %s", app_id, exc)
            return RedirectResponse(
                f"/apps/{app_id}?error={quote(str(exc)[:300])}", status_code=303
            )
        return RedirectResponse(f"/apps/{app_id}?notice={quote(msg)}", status_code=303)

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
        teardown_identity: str | None = Form(None),
    ):
        clients = request.app.state.clients
        service.delete_app(
            session,
            app_id,
            clients.kubectl_for,
            clients.gcp,
            delete_namespaces=bool(delete_namespaces),
            teardown_identity=bool(teardown_identity),
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/apps/{app_id}/redeploy")
    def redeploy(app_id: int, request: Request, session: Session = Depends(get_session)):
        view = service.app_status(session, app_id)
        try:
            service.redeploy_app(session, app_id, request.app.state.clients.github)
        except Exception as exc:
            log.warning("redeploy failed for app %s: %s", app_id, exc)
            msg = quote(f"Redeploy failed: {str(exc)[:300]}")
            return RedirectResponse(f"/apps/{app_id}?error={msg}", status_code=303)
        # send them to the Actions run the dispatch just triggered
        return RedirectResponse(
            f"https://github.com/{view.app.repo_full_name}/actions", status_code=303
        )

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

    @app.post("/apps/{app_id}/sizing/{env}/open-pr")
    def open_sizing_pr(
        app_id: int, env: str, request: Request, session: Session = Depends(get_session)
    ):
        try:
            url = sizing_service.open_pr_for(
                session, app_id, env, request.app.state.clients.github
            )
            log.info("sizing PR for app %s/%s: %s", app_id, env, url or "nothing to do")
        except Exception as exc:
            log.warning("sizing PR failed for app %s/%s: %s", app_id, env, exc)
        return RedirectResponse(f"/apps/{app_id}", status_code=303)

    class EnvIn(BaseModel):
        name: str
        cluster: str = ""  # blank: the install's cluster for this environment name
        namespace: str = ""

        @field_validator("namespace")
        @classmethod
        def _namespace_charset(cls, v: str) -> str:
            # Blank is a legitimate sentinel, not a raw value that ever
            # reaches k8s: api_connect derives the real namespace with
            # `env.namespace or body.name`, and body.name is itself
            # DNS-1123-validated below. So only a non-empty value needs to
            # satisfy the charset here — requiring non-blank would break the
            # "derive it from the app name" convenience the UI relies on.
            if v and not DNS1123_LABEL.fullmatch(v):
                raise ValueError(f"namespace {v!r} is not a valid DNS-1123 label")
            return v

    class ConnectIn(BaseModel):
        name: str = Field(min_length=1, max_length=63, pattern=DNS1123_LABEL.pattern)
        repo_full_name: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
        workload_class: str
        environments: list[EnvIn] = Field(min_length=1)
        auto_merge_setup_pr: bool = False

        @field_validator("environments")
        @classmethod
        def _unique_env_names(cls, envs: list[EnvIn]) -> list[EnvIn]:
            names = [e.name for e in envs]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate environment names: {names}")
            return envs

        @field_validator("repo_full_name")
        @classmethod
        def _repo_full_name_no_traversal(cls, v: str) -> str:
            # The charset pattern above allows '.' and '-', which are legal
            # GitHub owner/repo characters but also let a segment spell '.',
            # '..', or a leading '-' (which some tools parse as a flag) —
            # reject those specifically rather than narrowing the charset
            # itself.
            owner, repo = v.split("/", 1)
            for segment in (owner, repo):
                if segment in (".", "..") or segment.startswith("-"):
                    raise ValueError(f"repo_full_name segment {segment!r} is not allowed")
            return v

    @app.post("/api/v1/apps")
    def api_connect(body: ConnectIn, request: Request, session: Session = Depends(get_session)):
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        if body.workload_class not in WORKLOAD_CLASSES:
            return JSONResponse(
                {"error": f"unknown workload class {body.workload_class!r}"}, status_code=422
            )
        if service.app_by_repo(session, body.repo_full_name):
            return JSONResponse({"error": "repo already connected"}, status_code=409)
        if not service.repo_can_be_deployed(request.app.state.clients.github, body.repo_full_name):
            return JSONResponse({"error": f"repo {service.NOT_DEPLOYABLE}"}, status_code=422)
        env_map: dict[str, tuple[int, str]] = {}
        defaults = request.app.state.config.defaults.environment_clusters
        for env in body.environments:
            cluster_name = env.cluster or defaults.get(env.name, "")
            if not cluster_name:
                return JSONResponse(
                    {"error": f"no cluster for environment {env.name!r}: name one, or set "
                              f"defaults.environment_clusters on this install"},
                    status_code=422,
                )
            cluster = session.query(models.Cluster).filter_by(name=cluster_name).one_or_none()
            if cluster is None:
                return JSONResponse(
                    {"error": f"unknown cluster {cluster_name!r}"}, status_code=404
                )
            env_map[env.name] = (cluster.id, env.namespace or body.name)
        try:
            installation_id = request.app.state.clients.github.installation_id_for(
                body.repo_full_name
            )
        except Exception as exc:
            log.warning("could not resolve installation for %s: %s", body.repo_full_name, exc)
            installation_id = 0
        try:
            app_obj = service.connect_app(
                session,
                name=body.name,
                repo_full_name=body.repo_full_name,
                workload_class=body.workload_class,
                env_map=env_map,
                installation_id=installation_id,
            )
            app_obj.auto_merge_setup_pr = body.auto_merge_setup_pr
            session.commit()
        except IntegrityError:
            session.rollback()
            return JSONResponse({"error": "repo already connected"}, status_code=409)
        # Captured before start_provision: the background thread mutates this
        # same row in its own session, so reading app_obj.status afterward can
        # race it and report a status the caller never actually got back from
        # THIS request (e.g. already "active" instead of the just-connected
        # "pending").
        status = app_obj.status
        service.start_provision(
            request.app.state.session_factory,
            app_obj.id,
            clients=request.app.state.clients,
            config=request.app.state.config,
        )
        return JSONResponse({"id": app_obj.id, "status": status}, status_code=201)

    @app.get("/api/v1/apps")
    def api_list_apps(request: Request, session: Session = Depends(get_session)):
        # What is connected, and where it runs. scripts/uninstall.sh walks
        # this to remove every app while Gojira still knows what it created.
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        return JSONResponse(
            {
                "apps": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "repo_full_name": a.repo_full_name,
                        "workload_class": a.workload_class,
                        "status": a.status,
                        "environments": [
                            {"name": e.name, "cluster": e.cluster.name, "namespace": e.namespace}
                            for e in a.environments
                        ],
                    }
                    for a in sorted(service.list_apps(session), key=lambda a: a.id)
                ]
            }
        )

    @app.get("/api/v1/clusters")
    def api_list_clusters(request: Request, session: Session = Depends(get_session)):
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        return JSONResponse(
            {
                "clusters": [
                    {
                        "name": c.name,
                        "gcp_project": c.gcp_project,
                        "location": c.location,
                        "gke_cluster": c.gke_cluster,
                        "ar_repo": c.ar_repo,
                    }
                    for c in service.list_clusters(session)
                ]
            }
        )

    @app.get("/api/v1/apps/{app_id}")
    def api_app_status(app_id: int, request: Request, session: Session = Depends(get_session)):
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        app_obj = session.get(models.App, app_id)
        if app_obj is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "id": app_obj.id,
                "name": app_obj.name,
                "status": app_obj.status,
                "setup_pr_url": app_obj.setup_pr_url,
                "auto_merge_setup_pr": app_obj.auto_merge_setup_pr,
                "steps": [
                    {"name": s.step_name, "status": s.status, "error": s.error}
                    for s in app_obj.steps
                ],
            }
        )

    @app.get("/api/v1/apps/{app_id}/usage")
    def api_app_usage(
        app_id: int,
        request: Request,
        session: Session = Depends(get_session),
        env: str | None = None,
        since_days: int | None = None,
    ):
        # The read twin of POST /api/v1/usage, for the argus parity window:
        # a second evidence producer diffs its summaries against the ledger
        # and against the recommender's call. Platform bearer gate (same as
        # the other onboarding routes) — the cluster tokens are write
        # credentials scoped to one cluster, and this reads across them.
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        if since_days is None:
            since_days = request.app.state.config.sizing.history_days
        if not 0 < since_days <= sizing_profile.MAX_WINDOW_DAYS:
            return JSONResponse(
                {"error": f"since_days must be 1..{sizing_profile.MAX_WINDOW_DAYS}"},
                status_code=422,
            )
        body = sizing_service.usage_history(session, app_id, env=env, since_days=since_days)
        if body is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(body)

    @app.post("/api/v1/apps/{app_id}/provision")
    def api_provision(app_id: int, request: Request, session: Session = Depends(get_session)):
        # The automation-tier twin of the UI's Provision-again button: that
        # form route sits behind the IAP-identity gate, which a
        # server-to-server caller can never satisfy — so programmatic
        # clients re-kick runs here, under the same bearer gate as the rest
        # of /api/v1.
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        app_obj = session.get(models.App, app_id)
        if app_obj is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        service.start_provision(
            request.app.state.session_factory,
            app_id,
            clients=request.app.state.clients,
            config=request.app.state.config,
        )
        return JSONResponse({"id": app_id, "status": "provisioning"}, status_code=202)

    @app.post("/api/v1/apps/{app_id}/sync")
    def api_sync(app_id: int, request: Request, session: Session = Depends(get_session)):
        # The automation-tier twin of the UI's "check PR merge" button. A
        # caller that parked an app on a person (kobo's connected-repo
        # pipeline, 2026-09-29) needs a way to ask "has the setup PR merged?"
        # that does not depend on the webhook being wired — on installs
        # without it, GET /api/v1/apps/{id} reads the DB and would say
        # awaiting_pr forever. Cheap: one GitHub read, no provisioning.
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        app_obj = session.get(models.App, app_id)
        if app_obj is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        changed = service.sync_app(session, app_id, request.app.state.clients.github)
        session.refresh(app_obj)
        return JSONResponse({"id": app_id, "status": app_obj.status, "changed": bool(changed)})

    class DeleteAppIn(BaseModel):
        delete_namespaces: bool = True
        teardown_identity: bool = True

    @app.delete("/api/v1/apps/{app_id}")
    def api_delete_app(
        app_id: int,
        request: Request,
        session: Session = Depends(get_session),
        body: DeleteAppIn | None = None,
    ):
        # The automation-tier twin of the UI's delete form: same bearer gate
        # as the rest of /api/v1, same teardown underneath (identity removal
        # opt-out via teardown_identity, image deletion coupled to
        # delete_namespaces — see delete_app's own docstring). Plain def (not
        # async) — the teardown below runs real gcloud/kubectl subprocesses
        # (15-60s realistic), and an async-def handler would run that inline
        # on the event loop, stalling every other request for the duration.
        # A plain-def handler gets Starlette's threadpool for free, same as
        # /apps/{app_id}/delete's identical teardown and api_connect's own
        # plain-def + Pydantic-body handler above.
        if err := auth.api_token_error(request, request.app.state.config.auth):
            return err
        delete_namespaces = body.delete_namespaces if body is not None else True
        teardown_identity = body.teardown_identity if body is not None else True
        clients = request.app.state.clients
        summary = service.delete_app(
            session, app_id, clients.kubectl_for, clients.gcp,
            delete_namespaces=delete_namespaces,
            teardown_identity=teardown_identity,
        )
        if summary is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(summary, status_code=202)

    @app.post("/api/v1/usage")
    async def ingest_usage(request: Request, session: Session = Depends(get_session)):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "bearer token required"}, status_code=401)
        try:
            profile = sizing_profile.UsageProfile.model_validate(await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid profile: {exc}"}, status_code=422)
        try:
            row = sizing_service.ingest_usage(
                session, profile, auth.removeprefix("Bearer "),
                config=request.app.state.config.sizing,
            )
        except sizing_service.IngestError as exc:
            return JSONResponse({"error": exc.detail}, status_code=exc.status)
        return JSONResponse({"ok": True, "profile_id": row.id}, status_code=201)

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
