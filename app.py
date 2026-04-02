import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

load_dotenv()

import database
import bot_poller
import telegram as tg
import routing


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IMAGE_PATH = BASE_DIR / "image.png"
UPLOADS_DIR = Path("uploads")
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    database.ensure_superadmin(
        os.getenv("SUPER_ADMIN_EMAIL", ""),
        os.getenv("SUPER_ADMIN_PASSWORD", ""),
    )
    UPLOADS_DIR.mkdir(exist_ok=True)
    bot_poller.start()
    yield
    bot_poller.stop()


app = FastAPI(title="Ratecon Extractor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


# ── Rate limiter ──────────────────────────────────────────────────────────────

_attempts: dict[str, dict] = {}  # {ip: {count, blocked_until}}
MAX_ATTEMPTS = 5
BLOCK_SECS = 3 * 60  # 3 minutes


def _check_rate_limit(ip: str) -> int:
    """Returns seconds remaining if blocked, else 0."""
    entry = _attempts.get(ip, {})
    remaining = entry.get("blocked_until", 0) - time.time()
    return int(remaining) if remaining > 0 else 0


def _record_failure(ip: str):
    entry = _attempts.setdefault(ip, {"count": 0, "blocked_until": 0})
    entry["count"] += 1
    if entry["count"] >= MAX_ATTEMPTS:
        entry["blocked_until"] = time.time() + BLOCK_SECS
        entry["count"] = 0


def _clear_attempts(ip: str):
    _attempts.pop(ip, None)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def require_dispatcher(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = authorization.removeprefix("Bearer ").strip()
    dispatcher = database.get_dispatcher_by_token(token)
    if not dispatcher:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return dispatcher


def require_admin(authorization: str | None) -> dict:
    dispatcher = require_dispatcher(authorization)
    if dispatcher.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return dispatcher


def require_superadmin(authorization: str | None) -> dict:
    dispatcher = require_dispatcher(authorization)
    if dispatcher.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return dispatcher


def require_company_admin(authorization: str | None) -> dict:
    dispatcher = require_dispatcher(authorization)
    if dispatcher.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Company admin access required")
    return dispatcher


def get_company_id(dispatcher: dict) -> int:
    """Returns company_id or raises 403 if not set (e.g. superadmin without company)."""
    company_id = dispatcher.get("company_id")
    if not company_id:
        raise HTTPException(status_code=403, detail="This action requires a company account")
    return company_id


# ── ELD driver location helper ────────────────────────────────────────────────

def _get_driver_location(eld_driver_id: str, dispatcher_id: int) -> dict | None:
    """Get driver location, handling provider-prefixed IDs."""
    from eld import get_client
    configs = database.get_eld_configs(dispatcher_id)
    if not configs:
        return None
    # Parse prefix if present
    if ':' in eld_driver_id:
        provider, raw_id = eld_driver_id.split(':', 1)
        cfg = next((c for c in configs if c['provider'] == provider), None)
        if cfg:
            client = get_client(cfg["provider"], cfg["api_key"], cfg.get("company"), cfg.get("provider_token"))
            return client.get_driver_location(raw_id)
    # No prefix — try all configs (backward compat)
    for cfg in configs:
        try:
            client = get_client(cfg["provider"], cfg["api_key"], cfg.get("company"), cfg.get("provider_token"))
            loc = client.get_driver_location(eld_driver_id)
            if loc:
                return loc
        except Exception:
            continue
    return None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    return FileResponse("templates/landing.html")


@app.get("/app", response_class=HTMLResponse)
async def index():
    return FileResponse("templates/index.html")


@app.get("/setup", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
async def setup():
    return FileResponse("templates/setup.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    return FileResponse("templates/admin.html")


@app.get("/image.png")
async def logo_image():
    if IMAGE_PATH.exists():
        return FileResponse(str(IMAGE_PATH))
    fallback = STATIC_DIR / "image.png"
    if fallback.exists():
        return FileResponse(str(fallback))
    raise HTTPException(status_code=404, detail="Logo image not found")


# ── Auth API ──────────────────────────────────────────────────────────────────

class RegisterBody(BaseModel):
    name: str
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


class CreateUserBody(BaseModel):
    name: str
    email: str
    password: str


@app.post("/api/register")
async def api_register(body: RegisterBody):
    name = body.name.strip()
    email = body.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if database.get_dispatcher_by_email(email):
        raise HTTPException(status_code=409, detail="This email is already registered")
    dispatcher = database.create_dispatcher(name, email, body.password)
    return {"token": dispatcher["token"], "name": dispatcher["name"], "role": dispatcher["role"]}


@app.post("/api/login")
async def api_login(request: Request, body: LoginBody):
    ip = request.client.host or "unknown"
    secs = _check_rate_limit(ip)
    if secs:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {secs // 60 + 1} minutes."
        )

    email = body.email.strip().lower()
    dispatcher = database.get_dispatcher_by_email(email)
    if not dispatcher or not dispatcher.get("password_hash"):
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not database.verify_password(body.password, dispatcher["password_hash"]):
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    _clear_attempts(ip)
    return {"token": dispatcher["token"], "name": dispatcher["name"], "role": dispatcher["role"]}


@app.get("/api/me")
async def api_me(authorization: str | None = Header(default=None)):
    d = require_dispatcher(authorization)
    return {"id": d["id"], "name": d["name"], "role": d.get("role", "user"), "email": d.get("email", "")}


@app.post("/api/change-password")
async def api_change_password(
    body: ChangePasswordBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    if not dispatcher.get("password_hash"):
        raise HTTPException(status_code=400, detail="No password set")
    if not database.verify_password(body.current_password, dispatcher["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    database.update_password(dispatcher["id"], body.new_password)
    return {"ok": True}


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/dispatchers")
async def api_admin_list(authorization: str | None = Header(default=None)):
    me = require_admin(authorization)
    if me.get("role") == "superadmin":
        return database.get_all_dispatchers()
    return database.get_company_dispatchers(get_company_id(me))


class AdminUpdateBody(BaseModel):
    role: str | None = None
    is_active: bool | None = None


@app.patch("/api/admin/dispatchers/{target_id}")
async def api_admin_update(
    target_id: int,
    body: AdminUpdateBody,
    authorization: str | None = Header(default=None),
):
    me = require_admin(authorization)
    if target_id == me["id"]:
        raise HTTPException(status_code=400, detail="Cannot modify your own account")
    if me.get("role") != "superadmin":
        if not database.get_dispatcher_in_company(target_id, get_company_id(me)):
            raise HTTPException(status_code=404, detail="Dispatcher not found")
    fields = {}
    if body.role is not None:
        me_role = me.get("role")
        allowed_roles = ("admin", "user", "superadmin") if me_role == "superadmin" else ("admin", "user")
        if body.role not in allowed_roles:
            raise HTTPException(status_code=400, detail="Invalid role")
        fields["role"] = body.role
    if body.is_active is not None:
        fields["is_active"] = 1 if body.is_active else 0
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    database.update_dispatcher(target_id, **fields)
    return {"ok": True}


@app.delete("/api/admin/dispatchers/{target_id}")
async def api_admin_delete(
    target_id: int,
    authorization: str | None = Header(default=None),
):
    me = require_admin(authorization)
    if target_id == me["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if me.get("role") != "superadmin":
        if not database.get_dispatcher_in_company(target_id, get_company_id(me)):
            raise HTTPException(status_code=404, detail="Dispatcher not found")
    ok = database.delete_dispatcher(target_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Dispatcher not found")
    return {"ok": True}


class AdminResetPasswordBody(BaseModel):
    new_password: str


@app.post("/api/admin/dispatchers/{target_id}/reset-password")
async def api_admin_reset_password(
    target_id: int,
    body: AdminResetPasswordBody,
    authorization: str | None = Header(default=None),
):
    me = require_admin(authorization)
    if target_id == me["id"]:
        raise HTTPException(status_code=400, detail="Use /api/change-password to change your own password")
    if me.get("role") != "superadmin":
        if not database.get_dispatcher_in_company(target_id, get_company_id(me)):
            raise HTTPException(status_code=404, detail="Dispatcher not found")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    ok = database.update_password(target_id, body.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail="Dispatcher not found")
    return {"ok": True}


@app.get("/api/superadmin/stats")
async def api_superadmin_stats(authorization: str | None = Header(default=None)):
    require_superadmin(authorization)
    return database.get_admin_stats()


# ── Companies API (superadmin only) ───────────────────────────────────────────

class CompanyBody(BaseModel):
    name: str


@app.get("/api/superadmin/companies")
async def api_list_companies(authorization: str | None = Header(default=None)):
    require_superadmin(authorization)
    return database.get_all_companies()


@app.post("/api/superadmin/companies")
async def api_create_company(body: CompanyBody, authorization: str | None = Header(default=None)):
    require_superadmin(authorization)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name is required")
    return database.create_company(name)


@app.patch("/api/superadmin/companies/{company_id}")
async def api_update_company(
    company_id: int,
    body: CompanyBody,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name is required")
    ok = database.update_company(company_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"ok": True}


@app.delete("/api/superadmin/companies/{company_id}")
async def api_delete_company(
    company_id: int,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    ok = database.delete_company(company_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"ok": True}


class AssignAdminBody(BaseModel):
    name: str
    email: str
    password: str


@app.post("/api/superadmin/companies/{company_id}/assign-admin")
async def api_assign_admin(
    company_id: int,
    body: AssignAdminBody,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    if not database.get_company(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    name = body.name.strip()
    email = body.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if database.get_dispatcher_by_email(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    return database.create_company_user(company_id, name, email, body.password, role="admin")


@app.get("/api/superadmin/companies/{company_id}/users")
async def api_superadmin_company_users(
    company_id: int,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    if not database.get_company(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return [u for u in database.get_company_dispatchers(company_id) if u["role"] == "user"]


@app.post("/api/superadmin/companies/{company_id}/users")
async def api_superadmin_create_user(
    company_id: int,
    body: CreateUserBody,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    if not database.get_company(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    name = body.name.strip()
    email = body.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if database.get_dispatcher_by_email(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    return database.create_company_user(company_id, name, email, body.password, role="user")


@app.patch("/api/superadmin/users/{target_id}")
async def api_superadmin_update_user(
    target_id: int,
    body: AdminUpdateBody,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    fields = {}
    if body.is_active is not None:
        fields["is_active"] = 1 if body.is_active else 0
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    database.update_dispatcher(target_id, **fields)
    return {"ok": True}


@app.post("/api/superadmin/users/{target_id}/reset-password")
async def api_superadmin_reset_user_pw(
    target_id: int,
    body: AdminResetPasswordBody,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    ok = database.update_password(target_id, body.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@app.delete("/api/superadmin/users/{target_id}")
async def api_superadmin_delete_user(
    target_id: int,
    authorization: str | None = Header(default=None),
):
    require_superadmin(authorization)
    ok = database.delete_dispatcher(target_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


# ── Company Users API (company_admin only) ────────────────────────────────────

@app.get("/api/company/users")
async def api_company_users(authorization: str | None = Header(default=None)):
    me = require_company_admin(authorization)
    company_id = me.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="Not assigned to a company")
    return database.get_company_dispatchers(company_id)


class CreateUserBody(BaseModel):
    name: str
    email: str
    password: str


@app.post("/api/company/users")
async def api_create_company_user(
    body: CreateUserBody,
    authorization: str | None = Header(default=None),
):
    me = require_company_admin(authorization)
    company_id = me.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="Not assigned to a company")
    name = body.name.strip()
    email = body.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if database.get_dispatcher_by_email(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    return database.create_company_user(company_id, name, email, body.password, role="user")


@app.patch("/api/company/users/{target_id}")
async def api_update_company_user(
    target_id: int,
    body: AdminUpdateBody,
    authorization: str | None = Header(default=None),
):
    me = require_company_admin(authorization)
    company_id = me.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="Not assigned to a company")
    if target_id == me["id"]:
        raise HTTPException(status_code=400, detail="Cannot modify your own account")
    if not database.get_dispatcher_in_company(target_id, company_id):
        raise HTTPException(status_code=404, detail="User not found in your company")
    fields = {}
    if body.is_active is not None:
        fields["is_active"] = 1 if body.is_active else 0
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    database.update_dispatcher(target_id, **fields)
    return {"ok": True}


@app.post("/api/company/users/{target_id}/reset-password")
async def api_company_reset_password(
    target_id: int,
    body: AdminResetPasswordBody,
    authorization: str | None = Header(default=None),
):
    me = require_company_admin(authorization)
    company_id = me.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="Not assigned to a company")
    if not database.get_dispatcher_in_company(target_id, company_id):
        raise HTTPException(status_code=404, detail="User not found in your company")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    database.update_password(target_id, body.new_password)
    return {"ok": True}


@app.delete("/api/company/users/{target_id}")
async def api_delete_company_user(
    target_id: int,
    authorization: str | None = Header(default=None),
):
    me = require_company_admin(authorization)
    company_id = me.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="Not assigned to a company")
    if target_id == me["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not database.get_dispatcher_in_company(target_id, company_id):
        raise HTTPException(status_code=404, detail="User not found in your company")
    database.delete_dispatcher(target_id)
    return {"ok": True}


# ── Groups API ────────────────────────────────────────────────────────────────

@app.get("/api/groups")
async def api_groups(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    return database.get_groups(get_company_id(dispatcher))


class RenameBody(BaseModel):
    name: str


@app.patch("/api/groups/{group_id}")
async def api_rename_group(
    group_id: int,
    body: RenameBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    ok = database.rename_group(group_id, get_company_id(dispatcher), name)
    if not ok:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


@app.delete("/api/groups/{group_id}")
async def api_delete_group(
    group_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    ok = database.delete_group(group_id, get_company_id(dispatcher))
    if not ok:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


# ── Send API ──────────────────────────────────────────────────────────────────

class SendBody(BaseModel):
    group_id: int
    message: str
    load_number: str | None = None
    origin_state: str | None = None
    destination_state: str | None = None
    total_rate_usd: str | None = None
    miles: str | None = None
    pickup_address: str | None = None
    pickup_date: str | None = None
    delivery_address: str | None = None
    delivery_date: str | None = None
    stops_json: str | None = None


@app.post("/api/send")
async def api_send(
    body: SendBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not configured")
    group = database.get_group(body.group_id, get_company_id(dispatcher))
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    try:
        tg.send_message(group["chat_id"], body.message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram error: {e}")

    if any([body.load_number, body.pickup_address, body.origin_state, body.destination_state]):
        database.create_load(dispatcher["id"], {
            "group_id": body.group_id,
            "load_number": body.load_number,
            "origin_state": body.origin_state,
            "destination_state": body.destination_state,
            "total_rate_usd": body.total_rate_usd,
            "miles": body.miles,
            "pickup_address": body.pickup_address,
            "pickup_date": body.pickup_date,
            "delivery_address": body.delivery_address,
            "delivery_date": body.delivery_date,
            "stops_json": body.stops_json,
        })

    return {"ok": True}


# ── Bot info ──────────────────────────────────────────────────────────────────

@app.get("/api/bot-info")
async def api_bot_info():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return {"username": None}
    try:
        info = tg.get_me()
        return {"username": info.get("username")}
    except Exception:
        return {"username": None}


# ── ELD API ───────────────────────────────────────────────────────────────────

class EldConfigBody(BaseModel):
    provider: str
    api_key: str
    company: str | None = None
    provider_token: str | None = None


@app.get("/api/eld/config")
async def api_eld_get_config(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    configs = database.get_eld_configs(dispatcher["id"])
    return {
        "configs": [
            {
                "provider": c["provider"],
                "api_key": "••••" + c["api_key"][-4:] if len(c["api_key"]) >= 4 else "••••",
                "company": c.get("company"),
                "provider_token": "••••" if c.get("provider_token") else None,
            }
            for c in configs
        ]
    }


@app.post("/api/eld/config")
async def api_eld_save_config(
    body: EldConfigBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    if body.provider not in ("samsara", "motive", "zippyeld", "evoeld"):
        raise HTTPException(status_code=400, detail="Invalid provider. Use: samsara, motive, zippyeld, evoeld")
    if not body.api_key.strip():
        raise HTTPException(status_code=400, detail="API key is required")
    company = body.company.strip() if body.company else None
    provider_token = body.provider_token.strip() if body.provider_token else None
    database.save_eld_config(dispatcher["id"], body.provider, body.api_key.strip(), company, provider_token)
    return {"ok": True}


@app.delete("/api/eld/config")
async def api_eld_delete_config(provider: str | None = None, authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    database.delete_eld_config(dispatcher["id"], provider)
    return {"ok": True}


@app.get("/api/eld/drivers")
async def api_eld_drivers(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    configs = database.get_eld_configs(dispatcher["id"])
    if not configs:
        raise HTTPException(status_code=400, detail="ELD not configured. Add API key in Settings.")
    from eld import get_client
    all_drivers = []
    for cfg in configs:
        try:
            client = get_client(cfg["provider"], cfg["api_key"], cfg.get("company"), cfg.get("provider_token"))
            drivers = client.get_drivers()
            for d in drivers:
                all_drivers.append({
                    "id": f"{cfg['provider']}:{d['id']}",
                    "name": d["name"],
                    "provider": cfg["provider"],
                })
        except Exception:
            continue
    return {"drivers": all_drivers}


@app.get("/api/settings/google-maps")
async def api_gmaps_get(authorization: str | None = Header(default=None)):
    require_superadmin(authorization)
    key = database.get_global_setting("google_maps_key")
    return {"configured": bool(key), "masked": f"...{key[-4:]}" if key else None}


class GoogleMapsBody(BaseModel):
    api_key: str


@app.post("/api/settings/google-maps")
async def api_gmaps_save(body: GoogleMapsBody, authorization: str | None = Header(default=None)):
    require_superadmin(authorization)
    database.set_global_setting("google_maps_key", body.api_key.strip())
    return {"ok": True}


@app.delete("/api/settings/google-maps")
async def api_gmaps_delete(authorization: str | None = Header(default=None)):
    require_superadmin(authorization)
    database.set_global_setting("google_maps_key", "")
    return {"ok": True}


@app.get("/api/trucks")
async def api_trucks(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    configs = database.get_eld_configs(dispatcher["id"])
    if not configs:
        raise HTTPException(status_code=400, detail="ELD not configured. Add API key in Settings.")
    from eld import get_client
    all_trucks = []
    for cfg in configs:
        try:
            client = get_client(cfg["provider"], cfg["api_key"], cfg.get("company"), cfg.get("provider_token"))
            trucks = client.get_trucks()
            for t in trucks:
                t["provider"] = cfg["provider"]
            all_trucks.extend(trucks)
        except Exception:
            continue
    return {"trucks": all_trucks}


class AssignDriverBody(BaseModel):
    eld_driver_id: str | None = None
    eld_driver_name: str | None = None


@app.patch("/api/groups/{group_id}/driver")
async def api_assign_driver(
    group_id: int,
    body: AssignDriverBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    ok = database.set_group_driver(group_id, get_company_id(dispatcher), body.eld_driver_id, body.eld_driver_name)
    if not ok:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


@app.get("/api/eta")
async def api_eta(
    group_id: int,
    address: str,
    buffer: float = 0,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    company_id = get_company_id(dispatcher)
    group = database.get_group(group_id, company_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if not group.get("eld_driver_id"):
        raise HTTPException(status_code=400, detail="No driver assigned to this group")

    if not database.get_eld_configs(dispatcher["id"]):
        raise HTTPException(status_code=400, detail="ELD not configured")

    try:
        import routing
        origin = _get_driver_location(group["eld_driver_id"], dispatcher["id"])
        if not origin:
            raise HTTPException(status_code=404, detail="Driver location not available")
        gmaps_key = database.get_global_setting("google_maps_key") or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        destination = routing.geocode(address, gmaps_key)
        if not destination:
            raise HTTPException(status_code=400, detail=f"Could not geocode address: {address}")
        eta = routing.calculate_eta(origin, destination, buffer_hours=buffer, api_key=gmaps_key)
        return eta
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Loads API ─────────────────────────────────────────────────────────────────

class CreateLoadBody(BaseModel):
    group_id: int | None = None
    load_number: str | None = None
    origin_state: str | None = None
    destination_state: str | None = None
    total_rate_usd: str | None = None
    miles: str | None = None
    pickup_address: str | None = None
    pickup_date: str | None = None
    delivery_address: str | None = None
    delivery_date: str | None = None
    stops_json: str | None = None


@app.post("/api/loads")
async def api_create_load(
    body: CreateLoadBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.create_load(dispatcher["id"], body.model_dump())
    if not load:
        raise HTTPException(status_code=500, detail="Failed to create load")
    return load


@app.get("/api/loads")
async def api_get_loads(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    return {"loads": database.get_loads(get_company_id(dispatcher))}


class UpdateLoadStatusBody(BaseModel):
    status: str


@app.patch("/api/loads/{load_id}/status")
async def api_update_load_status(
    load_id: int,
    body: UpdateLoadStatusBody,
    authorization: str | None = Header(default=None),
):
    import json as _json
    dispatcher = require_dispatcher(authorization)
    if body.status not in ("upcoming", "dispatched", "delivered"):
        raise HTTPException(status_code=400, detail="Invalid status")

    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")

    new_stop_index = None
    if body.status == "dispatched":
        # Move current_stop_index to first delivery stop
        stops = _json.loads(load.get("stops_json") or "[]")
        first_delivery = next((i for i, s in enumerate(stops) if s.get("type") == "delivery"), None)
        if first_delivery is not None:
            new_stop_index = first_delivery
    elif body.status == "upcoming":
        new_stop_index = 0

    ok = database.update_load_status(load_id, get_company_id(dispatcher), body.status, new_stop_index)
    if not ok:
        raise HTTPException(status_code=404, detail="Load not found")
    return {"ok": True}


@app.post("/api/loads/{load_id}/next-stop")
async def api_load_next_stop(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    import json as _json
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    stops = _json.loads(load.get("stops_json") or "[]")
    current = load.get("current_stop_index", 0)
    if current + 1 >= len(stops):
        raise HTTPException(status_code=400, detail="Already at last stop")
    database.update_load_stop_index(load_id, get_company_id(dispatcher), current + 1)
    return {"ok": True, "current_stop_index": current + 1, "stop": stops[current + 1]}


@app.delete("/api/loads/{load_id}")
async def api_delete_load(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    ok = database.delete_load(load_id, get_company_id(dispatcher))
    if not ok:
        raise HTTPException(status_code=404, detail="Load not found")
    return {"ok": True}


@app.post("/api/loads/{load_id}/eta")
async def api_load_eta(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    import json as _json
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    if not load.get("driver_eld_id"):
        raise HTTPException(status_code=400, detail="No ELD driver assigned to this group")

    if not database.get_eld_configs(dispatcher["id"]):
        raise HTTPException(status_code=400, detail="ELD not configured")

    # Determine next stop address from stops_json
    stops = _json.loads(load.get("stops_json") or "[]")
    current_idx = load.get("current_stop_index", 0)

    if stops and current_idx < len(stops):
        next_address = stops[current_idx].get("address")
    else:
        # Fallback to simple fields
        next_address = (
            load.get("pickup_address") if load["status"] == "upcoming"
            else load.get("delivery_address")
        )

    if not next_address:
        raise HTTPException(status_code=400, detail="No address available for current stop")

    try:
        origin = _get_driver_location(load["driver_eld_id"], dispatcher["id"])
        if not origin:
            raise HTTPException(status_code=404, detail="Driver location not available")
        gmaps_key = database.get_global_setting("google_maps_key") or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        destination = routing.geocode(next_address, gmaps_key)
        if not destination:
            raise HTTPException(status_code=400, detail=f"Could not geocode: {next_address}")
        eta = routing.calculate_eta(origin, destination, api_key=gmaps_key)
        database.update_load_eta(load_id, eta["eta_utc"], eta["distance_miles"])
        return {"ok": True, "eta_utc": eta["eta_utc"], "eta_display": eta["eta_display"], "distance_miles": eta["distance_miles"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/loads/{load_id}/send-status")
async def api_load_send_status(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    if not load.get("group_id"):
        raise HTTPException(status_code=400, detail="No group assigned to this load")
    if not load.get("driver_eld_id"):
        raise HTTPException(status_code=400, detail="No ELD driver assigned")
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not configured")

    if not database.get_eld_configs(dispatcher["id"]):
        raise HTTPException(status_code=400, detail="ELD not configured")

    group = database.get_group(load["group_id"], get_company_id(dispatcher))
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    try:
        location = _get_driver_location(load["driver_eld_id"], dispatcher["id"])
        if not location:
            raise HTTPException(status_code=404, detail="Driver location not available")

        lat, lon = location["lat"], location["lon"]
        speed_mph = location.get("speed_mph") or 0

        # Reverse geocode current position
        current_addr = routing.reverse_geocode(lat, lon) or f"{lat:.4f}, {lon:.4f}"

        # Miles remaining to next stop
        next_address = (
            load.get("pickup_address") if load["status"] == "upcoming"
            else load.get("delivery_address")
        )
        miles_left = "N/A"
        heading = ""

        if next_address:
            gmaps_key = database.get_global_setting("google_maps_key") or os.environ.get("GOOGLE_MAPS_API_KEY", "")
            destination = routing.geocode(next_address, gmaps_key)
            if destination:
                route = routing.get_route({"lat": lat, "lon": lon}, destination, gmaps_key)
                miles_left = f"{round(route['distance_meters'] / 1609.34, 1)} mi"

        # Parse heading from next stop address: "STREET, CITY, STATE, ZIP"
        addr_parts = [p.strip() for p in (next_address or "").split(",")]
        if len(addr_parts) >= 3:
            heading = f"{addr_parts[-3]}, {addr_parts[-2]}"
        elif len(addr_parts) == 2:
            heading = ", ".join(addr_parts[:2])

        status_line = "🟢rolling" if speed_mph > 3 else "🔴stopped"
        load_id_display = load.get("load_number") or load["id"]

        message = (
            f"Load Id: {load_id_display}\n\n"
            f"Current location: {current_addr}\n\n"
            f"Miles left: {miles_left}\n\n"
            f"Heading ➤ {heading}\n\n"
            f"Status: {status_line}"
        )

        tg.send_message(group["chat_id"], message)
        return {"ok": True, "message": message}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Extract ───────────────────────────────────────────────────────────────────

@app.post("/extract")
async def extract_ratecon(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {suffix}. Allowed: {', '.join(SUPPORTED_EXTENSIONS)}"
        )
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 20MB limit")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        from extractor.llm_extractor import extract
        ratecon = extract(tmp_path)
        return {"status": "ok", "data": ratecon.model_dump()}
    except ValidationError as e:
        errors = [
            f"{' > '.join(str(x) for x in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        return {"status": "validation_error", "errors": errors}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


# ── Company Info API ──────────────────────────────────────────────────────────

class CompanyInfoBody(BaseModel):
    company_name:  str | None = None
    address:       str | None = None
    phone:         str | None = None
    email:         str | None = None
    mc_number:     str | None = None
    dot_number:    str | None = None
    bank_name:     str | None = None
    bank_account:  str | None = None
    bank_routing:  str | None = None
    payment_terms: str | None = "Net 30"


@app.get("/api/company")
async def api_get_company(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    info = database.get_company_info(dispatcher["id"])
    return info or {}


@app.post("/api/company")
async def api_save_company(
    body: CompanyInfoBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    database.save_company_info(dispatcher["id"], body.model_dump())
    return {"ok": True}


# ── Invoice API ───────────────────────────────────────────────────────────────

@app.get("/api/loads/{load_id}/invoice")
async def api_invoice(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    company = database.get_company_info(dispatcher["id"]) or {}
    invoice_number = f"INV-{load_id:04d}"
    from invoice import generate_invoice
    pdf_bytes = generate_invoice(load, company, invoice_number)
    filename = f"invoice-{invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Analytics API ─────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def api_analytics(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    company_id = get_company_id(dispatcher)
    company_filter = "dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)"
    with database.get_conn() as conn:
        total_rev = conn.execute(
            f"SELECT SUM(CAST(total_rate_usd AS REAL)) FROM loads WHERE {company_filter} AND status = 'delivered'",
            (company_id,),
        ).fetchone()[0] or 0

        total_miles = conn.execute(
            f"SELECT SUM(CAST(miles AS REAL)) FROM loads WHERE {company_filter} AND status = 'delivered'",
            (company_id,),
        ).fetchone()[0] or 0

        counts_rows = conn.execute(
            f"SELECT status, COUNT(*) as cnt FROM loads WHERE {company_filter} GROUP BY status",
            (company_id,),
        ).fetchall()
        counts = {r["status"]: r["cnt"] for r in counts_rows}

        monthly_rows = conn.execute(
            f"""SELECT strftime('%Y-%m', created_at) as month,
                      SUM(CAST(total_rate_usd AS REAL)) as revenue,
                      COUNT(*) as loads
               FROM loads WHERE {company_filter} AND status = 'delivered'
               GROUP BY month ORDER BY month ASC LIMIT 6""",
            (company_id,),
        ).fetchall()
        monthly = [dict(r) for r in monthly_rows]

    return {
        "total_revenue": round(total_rev, 2),
        "total_miles": round(total_miles, 2),
        "load_counts": counts,
        "monthly": monthly,
        "total_loads": sum(counts.values()),
    }


# ── Map API ───────────────────────────────────────────────────────────────────

@app.get("/api/map/locations")
async def api_map_locations(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    configs = database.get_eld_configs(dispatcher["id"])
    if not configs:
        return {"locations": []}

    groups = database.get_groups(get_company_id(dispatcher))
    groups_with_driver = [g for g in groups if g.get("eld_driver_id")]
    if not groups_with_driver:
        return {"locations": []}

    locations = []
    for group in groups_with_driver:
        try:
            loc = _get_driver_location(group["eld_driver_id"], dispatcher["id"])
            if not loc:
                continue
            locations.append({
                "group_id": group["id"],
                "driver_name": group.get("name") or "Unknown",
                "lat": loc["lat"],
                "lon": loc["lon"],
                "speed_mph": loc.get("speed_mph") or 0,
            })
        except Exception:
            continue
    return {"locations": locations}


# ── File storage API ──────────────────────────────────────────────────────────

LOAD_FILE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}


@app.post("/api/loads/{load_id}/file")
async def api_upload_load_file(
    load_id: int,
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in LOAD_FILE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 20MB limit")
    UPLOADS_DIR.mkdir(exist_ok=True)
    safe_stem = Path(file.filename).stem[:40].replace("/", "_").replace("..", "_")
    filename = f"{load_id}_{safe_stem}{suffix}"
    file_path = UPLOADS_DIR / filename
    file_path.write_bytes(contents)
    database.update_load_file(load_id, str(file_path))
    return {"ok": True, "file_path": str(file_path)}


@app.get("/api/loads/{load_id}/file")
async def api_get_load_file(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    fp = load.get("file_path")
    if not fp or not Path(fp).exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(fp, filename=Path(fp).name)


# ── POD API ───────────────────────────────────────────────────────────────────

POD_DIR = Path("uploads/pods")


@app.post("/api/loads/{load_id}/pod")
async def api_upload_pod(
    load_id: int,
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in LOAD_FILE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 20MB limit")
    POD_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = Path(file.filename).stem[:40].replace("/", "_").replace("..", "_")
    import time as _time
    filename = f"{load_id}_{int(_time.time())}_{safe_stem}{suffix}"
    file_path = POD_DIR / filename
    file_path.write_bytes(contents)
    pod = database.add_load_pod(load_id, str(file_path), file.filename)
    return {"ok": True, "pod": pod}


@app.get("/api/loads/{load_id}/pods")
async def api_get_pods(
    load_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    return {"pods": database.get_load_pods(load_id)}


@app.get("/api/loads/{load_id}/pod/{pod_id}")
async def api_download_pod(
    load_id: int,
    pod_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    pods = database.get_load_pods(load_id)
    pod = next((p for p in pods if p["id"] == pod_id), None)
    if not pod or not Path(pod["file_path"]).exists():
        raise HTTPException(status_code=404, detail="POD file not found")
    return FileResponse(pod["file_path"], filename=pod["filename"])


@app.delete("/api/loads/{load_id}/pod/{pod_id}")
async def api_delete_pod(
    load_id: int,
    pod_id: int,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    file_path = database.delete_load_pod(pod_id, load_id)
    if file_path is None:
        raise HTTPException(status_code=404, detail="POD not found")
    try:
        Path(file_path).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


# ── Auto-send API ─────────────────────────────────────────────────────────────

class AutoSendBody(BaseModel):
    hours: int


@app.patch("/api/loads/{load_id}/auto-send")
async def api_load_auto_send(
    load_id: int,
    body: AutoSendBody,
    authorization: str | None = Header(default=None),
):
    dispatcher = require_dispatcher(authorization)
    load = database.get_load(load_id, get_company_id(dispatcher))
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    if body.hours < 0 or body.hours > 24:
        raise HTTPException(status_code=400, detail="Hours must be 0–24")
    database.update_load_auto_send(load_id, get_company_id(dispatcher), body.hours)
    return {"ok": True}


# ── Alerts API ────────────────────────────────────────────────────────────────

@app.get("/api/alerts")
async def api_alerts(authorization: str | None = Header(default=None)):
    from datetime import datetime, timezone
    dispatcher = require_dispatcher(authorization)
    loads = database.get_loads(get_company_id(dispatcher))
    now = datetime.now(timezone.utc)
    alerts = []
    for load in loads:
        if load.get("eta_utc") and load.get("status") == "dispatched":
            try:
                eta_str = load["eta_utc"]
                if not eta_str.endswith("Z") and "+" not in eta_str:
                    eta_str += "+00:00"
                eta = datetime.fromisoformat(eta_str.replace("Z", "+00:00"))
                diff = (eta - now).total_seconds() / 60
                if 0 <= diff <= 60:
                    alerts.append({
                        "load_id": load["id"],
                        "load_number": load.get("load_number") or str(load["id"]),
                        "driver_name": load.get("driver_name") or "",
                        "eta_utc": load["eta_utc"],
                        "minutes_remaining": round(diff),
                    })
            except Exception:
                continue
    return {"alerts": alerts}


# ── Pay Tiers & Earnings API ──────────────────────────────────────────────────

class PayTierBody(BaseModel):
    dispatcher_id: int | None = None
    min_gross: float
    max_gross: float | None = None
    min_rpm: float = 0
    percentage: float


@app.get("/api/pay-tiers")
async def api_get_pay_tiers(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    company_id = get_company_id(dispatcher)
    return database.get_pay_tiers(company_id)


@app.post("/api/pay-tiers")
async def api_create_pay_tier(
    body: PayTierBody,
    authorization: str | None = Header(default=None),
):
    me = require_company_admin(authorization)
    company_id = get_company_id(me)
    if body.percentage <= 0 or body.percentage > 100:
        raise HTTPException(status_code=400, detail="Percentage must be between 0 and 100")
    if body.dispatcher_id and not database.get_dispatcher_in_company(body.dispatcher_id, company_id):
        raise HTTPException(status_code=404, detail="Worker not found in your company")
    tier = database.create_pay_tier(
        company_id, body.dispatcher_id,
        body.min_gross, body.max_gross, body.min_rpm, body.percentage,
    )
    return tier


@app.delete("/api/pay-tiers/{tier_id}")
async def api_delete_pay_tier(
    tier_id: int,
    authorization: str | None = Header(default=None),
):
    me = require_company_admin(authorization)
    ok = database.delete_pay_tier(tier_id, get_company_id(me))
    if not ok:
        raise HTTPException(status_code=404, detail="Tier not found")
    return {"ok": True}


@app.get("/api/earnings")
async def api_earnings(authorization: str | None = Header(default=None)):
    dispatcher = require_dispatcher(authorization)
    company_id = get_company_id(dispatcher)
    is_admin = dispatcher.get("role") in ("admin", "superadmin")
    worker_id = None if is_admin else dispatcher["id"]
    loads = database.get_earnings(company_id, worker_id)
    return {"earnings": loads}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
