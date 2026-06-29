import json as _json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import telegram
import database
from database import link_group

logger = logging.getLogger(__name__)


def _to_datetime(val) -> datetime | None:
    """Convert a value (datetime or string) to a timezone-aware datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    s = str(val)
    if not s.endswith("Z") and "+" not in s:
        s += "+00:00"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

_offset = 0
_stop_event = threading.Event()


def _load_offset() -> None:
    """Restore the Telegram update offset across restarts so a redeploy/crash
    doesn't re-process (and double-apply) commands like /delivered or /arrived."""
    global _offset
    try:
        val = database.get_global_setting("bot_offset")
        if val:
            _offset = int(val)
    except Exception as e:
        logger.warning("Could not load bot offset: %s", e)


def _save_offset() -> None:
    try:
        database.set_global_setting("bot_offset", str(_offset))
    except Exception as e:
        logger.warning("Could not save bot offset: %s", e)


# ── Status message helper ──────────────────────────────────────────────────────

def _get_driver_location_for_load(load: dict) -> dict | None:
    """Get driver location for a load using multi-provider ELD configs."""
    from eld import get_client
    eld_driver_id = load.get("driver_eld_id")
    if not eld_driver_id:
        return None
    dispatcher_id = load.get("dispatcher_id")
    if not dispatcher_id:
        return None
    configs = database.get_eld_configs(dispatcher_id)
    if not configs:
        return None
    # Parse prefix if present
    if ':' in eld_driver_id:
        provider, raw_id = eld_driver_id.split(':', 1)
        cfg = next((c for c in configs if c['provider'] == provider), None)
        if cfg:
            try:
                client = get_client(cfg["provider"], cfg["api_key"], cfg.get("company"), cfg.get("provider_token"))
                return client.get_driver_location(raw_id)
            except Exception:
                return None
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


def _build_status_message(load: dict) -> str | None:
    """Builds a status update message by querying the ELD. Returns None on failure."""
    try:
        import routing
        location = _get_driver_location_for_load(load)
        if not location:
            return None

        lat, lon = location["lat"], location["lon"]
        speed_mph = location.get("speed_mph") or 0
        current_addr = routing.reverse_geocode(lat, lon) or f"{lat:.4f}, {lon:.4f}"

        stops = _json.loads(load.get("stops_json") or "[]")
        idx = load.get("current_stop_index", 0)
        next_address = (
            stops[idx].get("address") if stops and idx < len(stops)
            else load.get("delivery_address") or load.get("pickup_address")
        )

        miles_left = "N/A"
        gmaps_key = database.get_global_setting("google_maps_key") or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if next_address:
            destination = routing.geocode(next_address, gmaps_key)
            if destination:
                route = routing.get_route({"lat": lat, "lon": lon}, destination, gmaps_key)
                miles_left = f"{round(route['distance_meters'] / 1609.34, 1)} mi"

        addr_parts = [p.strip() for p in (next_address or "").split(",")]
        if len(addr_parts) >= 3:
            heading = f"{addr_parts[-3]}, {addr_parts[-2]}"
        elif len(addr_parts) == 2:
            heading = ", ".join(addr_parts[:2])
        else:
            heading = next_address or "—"

        status_line = "🟢rolling" if speed_mph > 3 else "🔴stopped"
        load_id_display = load.get("load_number") or load["id"]

        # Use company template if available
        import collections as _collections
        _DEFAULT_TPL = (
            "Load Id: {load_id}\n\n"
            "Current location: {current_location}\n\n"
            "Miles left: {miles_left}\n\n"
            "Heading ➤ {heading}\n\n"
            "Status: {status}"
        )
        company_id = load.get("company_id")
        tpl = None
        if company_id:
            tpl = database.get_global_setting(f"status_template_{company_id}")
        if not tpl:
            # Also check without company_id prefix as a fallback
            tpl = database.get_global_setting("status_template") or _DEFAULT_TPL
        variables = {
            "load_id": load_id_display,
            "current_location": current_addr,
            "miles_left": miles_left,
            "heading": heading,
            "status": status_line,
            "speed": f"{speed_mph:.0f} mph" if speed_mph else "0 mph",
            "lat": f"{lat:.4f}",
            "lon": f"{lon:.4f}",
            "driver_name": load.get("driver_name") or "",
            "pickup_address": load.get("pickup_address") or "",
            "delivery_address": load.get("delivery_address") or "",
        }
        try:
            return tpl.format_map(_collections.defaultdict(lambda: "N/A", variables))
        except Exception:
            return _DEFAULT_TPL.format_map(_collections.defaultdict(lambda: "N/A", variables))
    except Exception as e:
        logger.warning("_build_status_message error: %s", e)
        return None


# ── Update processor ──────────────────────────────────────────────────────────

def _process_updates(updates: list[dict]):
    global _offset
    for update in updates:
        _offset = update["update_id"] + 1
        # Persist the advanced offset BEFORE handling each update. If the process
        # crashes mid-batch, Telegram won't re-deliver (and so won't re-apply) an
        # already-acknowledged non-idempotent command like /arrived or /delivered.
        # Worst case a single in-flight update is skipped — far safer than
        # double-applying it (e.g. /arrived advancing the stop index twice).
        _save_offset()
        # Isolate each update: one malformed message must not abort the batch.
        try:
            _handle_update(update)
        except Exception as e:
            logger.warning("Error handling update %s: %s", update.get("update_id"), e)


# Commands that either mutate a load or pull live driver GPS — restricted to the
# telegram user who originally redeemed the group's link code (L4). Chatter like
# /help and /link is unaffected.
_GUARDED_COMMANDS = {"/status", "/eta", "/arrived", "/delivered", "/pickup"}


def _is_authorized_sender(message: dict, chat_id) -> bool:
    """Return True if the message sender may run a guarded command in this chat.

    When a group's link is redeemed we record the redeemer's telegram user id
    (see the /link handler). Only that user may run guarded commands afterwards.
    Legacy groups linked before this id was recorded have no stored value — we
    allow them for backward compatibility but log a warning so it's observable.
    Storage read errors fail open to avoid breaking legitimate drivers."""
    try:
        stored = database.get_global_setting(f"tg_group_user_{chat_id}")
    except Exception as e:
        logger.warning("Could not read authorized telegram user for chat %s: %s", chat_id, e)
        return True
    if not stored:
        logger.warning(
            "No authorized telegram user recorded for chat %s; allowing command (legacy group)",
            chat_id,
        )
        return True
    from_id = (message.get("from") or {}).get("id")
    if from_id is not None and str(from_id) == str(stored):
        return True
    logger.warning(
        "Unauthorized telegram user %s attempted a guarded command in chat %s (authorized=%s)",
        from_id, chat_id, stored,
    )
    return False


def _handle_update(update: dict):
    message = update.get("message") or update.get("my_chat_member")
    if not message:
        return
    text = (message.get("text") or "").strip()
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not text.startswith("/"):
        return
    parts = text.split()
    # Normalize: strip a trailing @botname and lowercase, then match exactly so
    # "/deliveredxyz" no longer fires "/delivered" and "/status@MyBot" works.
    cmd = parts[0].split("@", 1)[0].lower()

    # L4: restrict load-mutating / GPS-pulling commands to the driver who linked
    # the group, not any chat member. (No-op for legacy groups with no stored id.)
    if cmd in _GUARDED_COMMANDS and not _is_authorized_sender(message, chat_id):
        _reply(message, "⛔ Only the driver who linked this group can use that command.")
        return

    # /link <code> — single-use code, NOT the dispatcher's API token
    if cmd == "/link":
        if len(parts) < 2:
            _reply(message, "Usage: /link <code>\n\nGet a code in the app: Settings → Telegram → Link a group.")
            return
        code = parts[1].strip()
        # Old flow pasted the API token (UUID) into chat — refuse it.
        if len(code) >= 32 and "-" in code:
            _reply(message, "That looks like an old token. For your security, open the app → Settings → Telegram and use the short link code instead.")
            return
        dispatcher = database.consume_link_code(code)
        if not dispatcher:
            _reply(message, "Invalid or expired code. Generate a fresh one in the app (Settings → Telegram).")
            return
        chat_title = chat.get("title") or chat.get("username") or str(chat_id)
        try:
            link_group(dispatcher["id"], chat_id, chat_title)
            # L4: remember who redeemed the link so only they can run guarded
            # commands later. Best-effort — a storage hiccup must not block linking.
            from_id = (message.get("from") or {}).get("id")
            if from_id is not None:
                try:
                    database.set_global_setting(f"tg_group_user_{chat_id}", str(from_id))
                except Exception as e:
                    logger.warning("Could not record authorized telegram user for chat %s: %s", chat_id, e)
            _reply(message, f"✅ Group '{chat_title}' linked to {dispatcher['name']}!")
        except Exception as e:
            logger.error("link_group error: %s", e)
            _reply(message, "Something went wrong. Please try again.")

    # /help
    elif cmd == "/help":
        _reply(message, (
            "🚛 *SimpleTrucking Bot Commands*\n\n"
            "/status — Send current location & status update\n"
            "/eta — Show ETA for current stop\n"
            "/arrived — Mark current stop as complete\n"
            "/delivered — Mark load as delivered\n"
            "/pickup — Mark upcoming load as dispatched\n"
            "/help — Show this message"
        ))

    # /status
    elif cmd == "/status":
        load = database.get_active_load_by_chat_id(chat_id)
        if not load:
            _reply(message, "No active dispatched load found for this group.")
            return
        if not load.get("driver_eld_id"):
            _reply(message, "No ELD driver assigned to this load.")
            return
        if not database.get_eld_configs(load["dispatcher_id"]):
            _reply(message, "ELD not configured for your dispatcher.")
            return
        msg = _build_status_message(load)
        _reply(message, msg if msg else "Could not retrieve driver location.")

    # /eta
    elif cmd == "/eta":
        load = database.get_active_load_by_chat_id(chat_id)
        if not load:
            _reply(message, "No active dispatched load found for this group.")
            return
        if not load.get("eta_utc"):
            _reply(message, "No ETA calculated yet. Ask your dispatcher to refresh ETA.")
            return
        try:
            eta_dt = _to_datetime(load["eta_utc"])
            local_str = eta_dt.strftime("%b %d %I:%M %p UTC")
            mi = f" ({load['eta_miles']} mi)" if load.get("eta_miles") else ""
            load_id_display = load.get("load_number") or load["id"]
            _reply(message, f"📍 Load {load_id_display} ETA: {local_str}{mi}")
        except Exception as e:
            logger.warning("ETA reply error: %s", e)
            _reply(message, f"ETA: {load['eta_utc']}")

    # /arrived
    elif cmd == "/arrived":
        load = database.get_active_load_by_chat_id(chat_id)
        if not load:
            _reply(message, "No active dispatched load found for this group.")
            return
        stops = _json.loads(load.get("stops_json") or "[]")
        idx = load.get("current_stop_index", 0)
        if not stops:
            _reply(message, "No stops configured for this load.")
            return
        if idx + 1 >= len(stops):
            _reply(message, "✅ You're at the last stop! Use /delivered to complete the load.")
            return
        new_idx = idx + 1
        database.update_load_stop_index(load["id"], load["company_id"], new_idx)
        next_stop = stops[new_idx]
        next_city = f"{next_stop.get('city', '')}, {next_stop.get('state', '')}".strip(", ")
        next_type = next_stop.get("type", "stop").capitalize()
        load_id_display = load.get("load_number") or load["id"]
        msg = (
            f"✅ Arrived at stop {idx + 1}/{len(stops)}\n\n"
            f"Load {load_id_display}: Moved to next {next_type}\n"
            f"📍 {next_city or next_stop.get('address', '—')}"
        )
        if new_idx + 1 >= len(stops):
            msg += "\n\n⚠️ This is the last stop — use /delivered when complete."
        _reply(message, msg)

    # /delivered
    elif cmd == "/delivered":
        load = database.get_active_load_by_chat_id(chat_id)
        if not load:
            _reply(message, "No active dispatched load found for this group.")
            return
        database.update_load_status(load["id"], load["company_id"], "delivered")
        load_id_display = load.get("load_number") or load["id"]
        _reply(message, f"✅ Load {load_id_display} marked as *delivered*! Great job! 🎉")

    # /pickup
    elif cmd == "/pickup":
        load = database.get_upcoming_load_by_chat_id(chat_id)
        if not load:
            _reply(message, "No upcoming load found for this group.")
            return
        stops = _json.loads(load.get("stops_json") or "[]")
        first_delivery = next((i for i, s in enumerate(stops) if s.get("type") == "delivery"), None)
        new_idx = first_delivery if first_delivery is not None else 0
        database.update_load_status(load["id"], load["company_id"], "dispatched", new_idx)
        load_id_display = load.get("load_number") or load["id"]
        _reply(message, f"🚛 Load {load_id_display} marked as *dispatched*! Safe travels!")


def _reply(message: dict, text: str):
    chat_id = message.get("chat", {}).get("id")
    if chat_id:
        try:
            telegram.send_message(chat_id, text)
        except Exception as e:
            logger.warning("Failed to send reply: %s", e)


# ── Polling loop ──────────────────────────────────────────────────────────────

def _poll_loop():
    global _offset
    while not _stop_event.is_set():
        try:
            if not os.environ.get("TELEGRAM_BOT_TOKEN"):
                time.sleep(5)
                continue
            updates = telegram.get_updates(_offset)
            if updates:
                _process_updates(updates)
        except Exception as e:
            logger.warning("Polling error: %s", e)
            time.sleep(3)  # back off only on error; long-poll already blocks



# ── Auto-send background thread ───────────────────────────────────────────────

def _auto_send_loop():
    while not _stop_event.is_set():
        try:
            if not os.environ.get("TELEGRAM_BOT_TOKEN"):
                time.sleep(60)
                continue
            loads = database.get_loads_for_auto_send()
            now = datetime.now(timezone.utc)
            for load in loads:
                try:
                    hours = load.get("auto_send_hours") or 0
                    if not hours:
                        continue
                    last_raw = load.get("last_auto_send")
                    if last_raw:
                        last_dt = _to_datetime(last_raw)
                        elapsed_h = (now - last_dt).total_seconds() / 3600
                        if elapsed_h < hours:
                            continue

                    # The dispatcher's full (multi-provider) ELD config is resolved
                    # inside _build_status_message; here we only gate on the columns
                    # get_loads_for_auto_send already fetched, to skip loads with no
                    # ELD config without building a now-unused dict.
                    if not load.get("eld_provider") or not load.get("eld_api_key"):
                        continue

                    chat_id = load.get("group_chat_id")
                    if not chat_id:
                        continue

                    msg = _build_status_message(load)
                    if msg:
                        telegram.send_message(chat_id, msg)
                        database.update_last_auto_send(load["id"])
                except Exception as e:
                    logger.warning("Auto-send error for load %s: %s", load.get("id"), e)
        except Exception as e:
            logger.warning("Auto-send loop error: %s", e)
        time.sleep(60)  # every 60 seconds


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def start():
    _stop_event.clear()
    _load_offset()
    threading.Thread(target=_poll_loop, daemon=True, name="bot-poller").start()
    threading.Thread(target=_auto_send_loop, daemon=True, name="auto-send").start()
    logger.info("Bot poller started (with auto-send), offset=%s", _offset)


def stop():
    _stop_event.set()
