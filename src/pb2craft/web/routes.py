"""FastAPI routes for the web UI."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from pb2craft import __version__
from pb2craft.api.craft import CraftAuthError, CraftClient, CraftError
from pb2craft.api.pocketbook import PocketBookClient, PocketBookError, Shop
from pb2craft.app import AppContainer, NotConfiguredError
from pb2craft.credentials import CraftConfig

log = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


ContainerDep = Annotated[AppContainer, Depends(get_container)]


# --------------------------------------------------------------------------- #
# Template helpers                                                             #
# --------------------------------------------------------------------------- #


def _base_context(container: AppContainer, active: str, **extra: Any) -> dict[str, Any]:
    return {
        "version": __version__,
        "port": container.settings.web_port,
        "config_dir": str(container.config_dir),
        "active": active,
        "flash": None,
        **extra,
    }


def _render(
    request: Request,
    template: str,
    container: AppContainer,
    *,
    active: str,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    ctx = _base_context(container, active=active, **context)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=ctx,
        status_code=status_code,
    )


# --------------------------------------------------------------------------- #
# Health                                                                       #
# --------------------------------------------------------------------------- #


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


# --------------------------------------------------------------------------- #
# Dashboard                                                                    #
# --------------------------------------------------------------------------- #


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, container: ContainerDep) -> HTMLResponse:
    pb_creds = container.credentials.load()
    craft_cfg = container.credentials.load_craft()
    ready, not_ready_reason = container.is_ready_to_sync()
    summary = container.state.summary()

    return _render(
        request,
        "index.html",
        container,
        active="index",
        pb_connected=pb_creds is not None,
        pb_shop=pb_creds.shop_alias if pb_creds else None,
        craft_connected=craft_cfg is not None,
        craft_folder=craft_cfg.folder_name if craft_cfg else None,
        ready=ready,
        not_ready_reason=not_ready_reason,
        is_syncing=container.is_syncing,
        summary=summary,
        last_sync_at=_format_local(summary.get("last_sync_at")),
    )


# --------------------------------------------------------------------------- #
# Sync trigger                                                                 #
# --------------------------------------------------------------------------- #


@router.post("/sync")
async def trigger_sync(
    container: ContainerDep,
    force: str | None = Form(default=None),
) -> Response:
    try:
        await container.run_sync(force=bool(force))
    except NotConfiguredError:
        # The route still completes; flash is rendered by /
        pass
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- #
# Login — PocketBook                                                           #
# --------------------------------------------------------------------------- #


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, container: ContainerDep) -> HTMLResponse:
    return _render_login(request, container)


def _render_login(
    request: Request,
    container: AppContainer,
    *,
    pb_email: str | None = None,
    pb_shops: list[Shop] | None = None,
    pb_error: str | None = None,
    craft_error: str | None = None,
    craft_ok: str | None = None,
    craft_settings_ok: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    pb_creds = container.credentials.load()
    craft_cfg = container.credentials.load_craft()
    return _render(
        request,
        "login.html",
        container,
        active="login",
        status_code=status_code,
        pb_status="connected" if pb_creds else "missing",
        pb_shop=pb_creds.shop_alias if pb_creds else None,
        pb_email=pb_email,
        pb_shops=pb_shops,
        pb_error=pb_error,
        craft_status="connected" if craft_cfg else "missing",
        craft_cfg=craft_cfg,
        craft_error=craft_error,
        craft_ok=craft_ok,
        craft_settings_ok=craft_settings_ok,
    )


@router.post("/login/pocketbook/discover", response_class=HTMLResponse)
async def login_pocketbook_discover(
    request: Request,
    container: ContainerDep,
    email: str = Form(...),
) -> HTMLResponse:
    pb = container.build_pocketbook_client()
    try:
        shops = await pb.get_shops(email)
    except PocketBookError as e:
        log.warning("Shop discovery failed: %s", e)
        return _render_login(request, container, pb_error=str(e), status_code=400)
    finally:
        await pb.aclose()

    if not shops:
        return _render_login(
            request,
            container,
            pb_email=email,
            pb_error="No shops returned for that email.",
            status_code=400,
        )
    return _render_login(request, container, pb_email=email, pb_shops=shops)


@router.post("/login/pocketbook/complete", response_class=HTMLResponse)
async def login_pocketbook_complete(
    request: Request,
    container: ContainerDep,
    email: str = Form(...),
    password: str = Form(...),
    shop_alias: str = Form(...),
) -> Response:
    pb = container.build_pocketbook_client()
    try:
        shops = await pb.get_shops(email)
        shop = next((s for s in shops if s.alias == shop_alias), None)
        if shop is None:
            return _render_login(
                request,
                container,
                pb_email=email,
                pb_shops=shops,
                pb_error=f"Shop {shop_alias!r} not found.",
                status_code=400,
            )
        await pb.login(email=email, password=password, shop=shop)
    except PocketBookError as e:
        log.warning("Login failed: %s", e)
        return _render_login(
            request,
            container,
            pb_email=email,
            pb_shops=[shop] if 'shop' in locals() and shop else None,
            pb_error=f"Sign-in failed: {e}",
            status_code=400,
        )
    finally:
        await pb.aclose()

    return RedirectResponse("/login", status_code=303)


@router.post("/logout/pocketbook")
async def logout_pocketbook(container: ContainerDep) -> Response:
    container.credentials.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- #
# Login — Craft                                                                #
# --------------------------------------------------------------------------- #


def _decorations_from_form(deco_focus: str | None, deco_block: str | None) -> list[str]:
    """Convert form-checkbox state to the canonical decoration list."""
    decos: list[str] = []
    if deco_focus:
        decos.append("quote")
    if deco_block:
        decos.append("callout")
    return decos


@router.post("/login/craft", response_class=HTMLResponse)
async def login_craft(
    request: Request,
    container: ContainerDep,
    api_url: str = Form(...),
    api_token: str = Form(...),
    folder_name: str = Form("PocketBook Imports"),
    deco_focus: str | None = Form(default=None),
    deco_block: str | None = Form(default=None),
    tag_author: str | None = Form(default=None),
    tag_publisher: str | None = Form(default=None),
) -> Response:
    # Test connection: list folders. Then persist on success.
    client = CraftClient(api_url=api_url, token=api_token)
    try:
        await client.list_folders()
    except CraftAuthError:
        return _render_login(
            request,
            container,
            craft_error="Authentication failed — check the API token.",
            status_code=400,
        )
    except CraftError as e:
        return _render_login(
            request,
            container,
            craft_error=f"Connection failed: {e}",
            status_code=400,
        )
    finally:
        await client.aclose()

    container.credentials.save_craft(
        CraftConfig(
            api_url=api_url,
            api_token=api_token,
            folder_name=folder_name,
            quote_decorations=_decorations_from_form(deco_focus, deco_block),
            add_author_tag=bool(tag_author),
            add_publisher_tag=bool(tag_publisher),
        )
    )
    # New folder picks up via SyncService on next run; clear any stale folder_id.
    container.state.set_folder_id(None)
    return RedirectResponse("/login", status_code=303)


@router.post("/settings/craft-display", response_class=HTMLResponse)
async def update_craft_display(
    request: Request,
    container: ContainerDep,
    deco_focus: str | None = Form(default=None),
    deco_block: str | None = Form(default=None),
    tag_author: str | None = Form(default=None),
    tag_publisher: str | None = Form(default=None),
) -> Response:
    """Update display settings (decorations + tag toggles) on an existing connection."""
    cfg = container.credentials.load_craft()
    if cfg is None:
        return RedirectResponse("/login", status_code=303)

    updated = cfg.model_copy(
        update={
            "quote_decorations": _decorations_from_form(deco_focus, deco_block),
            "add_author_tag": bool(tag_author),
            "add_publisher_tag": bool(tag_publisher),
        }
    )
    container.credentials.save_craft(updated)
    return _render_login(
        request,
        container,
        craft_settings_ok="Display settings updated.",
    )


@router.post("/logout/craft")
async def logout_craft(container: ContainerDep) -> Response:
    container.credentials.clear_craft()
    container.state.set_folder_id(None)
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- #
# Logs                                                                         #
# --------------------------------------------------------------------------- #


@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request, container: ContainerDep) -> HTMLResponse:
    runs = [_decorate_run(r) for r in container.run_log]
    return _render(request, "logs.html", container, active="logs", runs=runs)


# --------------------------------------------------------------------------- #
# Settings                                                                     #
# --------------------------------------------------------------------------- #


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, container: ContainerDep) -> HTMLResponse:
    return _render(
        request,
        "settings.html",
        container,
        active="settings",
        sync_interval_minutes=container.current_sync_interval_minutes(),
        settings_saved=None,
    )


@router.post("/settings", response_class=HTMLResponse)
async def update_settings(
    request: Request,
    container: ContainerDep,
    sync_interval_minutes: int = Form(...),
) -> HTMLResponse:
    container.update_sync_interval(sync_interval_minutes)
    return _render(
        request,
        "settings.html",
        container,
        active="settings",
        sync_interval_minutes=container.current_sync_interval_minutes(),
        settings_saved="Saved.",
    )


def _decorate_run(r: Any) -> dict[str, Any]:
    duration = (r.finished_at - r.started_at).total_seconds()
    return {
        "started_at_display": _format_local(r.started_at.isoformat()) or "—",
        "duration_display": f"{duration:.1f}s",
        "success": r.success,
        "books": r.books,
        "highlights": r.highlights,
        "skipped": r.skipped,
        "errors": r.errors,
        "error_message": r.error_message,
    }


def _format_local(iso_string: str | None) -> str | None:
    """Convert an ISO-8601 UTC timestamp to the container's local timezone.

    The container TZ comes from the ``TZ`` env var (set in compose / Unraid
    template). If unset, ``astimezone()`` falls back to UTC.
    """
    if not iso_string:
        return None
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return iso_string
    return dt.astimezone().strftime("%b %d, %Y at %I:%M %p %Z").strip()


# Hush unused-import warnings for symbols kept for typing/clarity at module level.
_ = PocketBookClient, timezone
