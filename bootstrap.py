import os
import sys
import json
import logging
import threading
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] - %(message)s")
LOG = logging.getLogger("bootstrap")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.env"
TEMPLATES_DIR = str(ROOT / "Backend" / "fastapi" / "templates")

DB_NAME = "dbFyvio"
BOOTSTRAP_COLLECTION = "bootstrap_config"
BOOTSTRAP_ID = "credentials"
CRED_KEYS = ("api_id", "api_hash", "bot_token", "user_session_string", "owner_id", "port")


def _db_uris(raw: str) -> list:
    return [u.strip() for u in (raw or "").split(",") if u.strip()]


def is_configured() -> bool:
    load_dotenv(CONFIG_PATH)
    api_id = (os.getenv("API_ID") or "").strip()
    api_hash = (os.getenv("API_HASH") or "").strip()
    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    owner_id = (os.getenv("OWNER_ID") or "").strip()
    database = _db_uris(os.getenv("DATABASE", ""))
    return all([
        api_id.isdigit() and int(api_id) > 0,
        api_hash,
        ":" in bot_token,
        owner_id.isdigit() and int(owner_id) > 0,
        len(database) >= 2,
    ])


def _mongo_ping(uris: list):
    try:
        from pymongo import MongoClient
    except Exception as e:
        LOG.warning(f"pymongo unavailable, skipping DB validation: {e}")
        return None
    for i, uri in enumerate(uris):
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=6000)
            client.admin.command("ping")
            client.close()
        except Exception as e:
            return f"Could not connect to MongoDB URI #{i + 1}: {e}"
    return None


def _save_config_to_db(uris: list, values: dict):
    try:
        from pymongo import MongoClient
        client = MongoClient(uris[0], serverSelectionTimeoutMS=6000)
        doc = {"_id": BOOTSTRAP_ID, "database": values["database"]}
        for k in CRED_KEYS:
            doc[k] = values.get(k, "")
        client[DB_NAME][BOOTSTRAP_COLLECTION].replace_one({"_id": BOOTSTRAP_ID}, doc, upsert=True)
        client.close()
        LOG.info("Saved bootstrap configuration to database (persists across restarts).")
    except Exception as e:
        LOG.warning(f"Could not persist config to database: {e}")


def _load_config_from_db(uris: list):
    try:
        from pymongo import MongoClient
        client = MongoClient(uris[0], serverSelectionTimeoutMS=6000)
        doc = client[DB_NAME][BOOTSTRAP_COLLECTION].find_one({"_id": BOOTSTRAP_ID})
        client.close()
        return doc
    except Exception as e:
        LOG.warning(f"Could not read config from database: {e}")
        return None


def _telegram_check(bot_token: str):
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            return "Telegram rejected the bot token."
        return None
    except HTTPError:
        return "Bot token was rejected by Telegram (invalid or revoked token)."
    except (URLError, TimeoutError, Exception):
        return None


def _validate(form: dict):
    errors = []
    api_id = (form.get("api_id") or "").strip()
    api_hash = (form.get("api_hash") or "").strip()
    bot_token = (form.get("bot_token") or "").strip()
    owner_id = (form.get("owner_id") or "").strip()
    database = (form.get("database") or "").strip()
    port = (form.get("port") or "8000").strip()
    user_session_string = (form.get("user_session_string") or "").strip()

    if not api_id.isdigit():
        errors.append("API_ID must be a number (from my.telegram.org).")
    if not api_hash:
        errors.append("API_HASH is required.")
    if ":" not in bot_token:
        errors.append("BOT_TOKEN looks invalid (get it from @BotFather).")
    if not owner_id.isdigit():
        errors.append("OWNER_ID must be your numeric Telegram ID.")
    if len(_db_uris(database)) < 2:
        errors.append("DATABASE needs at least 2 MongoDB URIs (1 tracking + 1 storage), comma-separated.")
    if not port.isdigit():
        errors.append("PORT must be a number.")

    values = {
        "api_id": api_id, "api_hash": api_hash, "bot_token": bot_token,
        "owner_id": owner_id, "database": database, "port": port or "8000",
        "user_session_string": user_session_string,
    }
    return values, errors


def _write_config(values: dict) -> None:
    lines = [
        f'API_ID="{values["api_id"]}"',
        f'API_HASH="{values["api_hash"]}"',
        f'BOT_TOKEN="{values["bot_token"]}"',
        f'USER_SESSION_STRING="{values["user_session_string"]}"',
        f'OWNER_ID="{values["owner_id"]}"',
        f'DATABASE="{values["database"]}"',
        f'PORT="{values["port"]}"',
    ]
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def _apply_to_env(values: dict) -> None:
    os.environ["API_ID"] = str(values.get("api_id", ""))
    os.environ["API_HASH"] = str(values.get("api_hash", ""))
    os.environ["BOT_TOKEN"] = str(values.get("bot_token", ""))
    os.environ["USER_SESSION_STRING"] = str(values.get("user_session_string", ""))
    os.environ["OWNER_ID"] = str(values.get("owner_id", ""))
    os.environ["DATABASE"] = str(values.get("database", ""))
    os.environ["PORT"] = str(values.get("port", "8000"))


def _launch_backend() -> None:
    LOG.info("Configuration present — launching Telegram-Stremio.")
    os.execv(sys.executable, [sys.executable, "-m", "Backend"])


def _restart_into_backend() -> None:
    LOG.info("Setup saved — starting Telegram-Stremio...")
    try:
        os.execv(sys.executable, [sys.executable, "-m", "Backend"])
    except Exception as e:
        LOG.error(f"Re-exec failed ({e}); exiting for container restart.")
        os._exit(0)


def run_setup_server() -> None:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app = FastAPI(title="Telegram-Stremio Setup")

    def _prefill():
        return {"database": (os.getenv("DATABASE") or "").strip(), "port": (os.getenv("PORT") or "8000").strip()}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("setup.html", {"request": request, "errors": [], "values": _prefill()})

    @app.post("/save", response_class=HTMLResponse)
    async def save(request: Request):
        form = dict(await request.form())
        values, errors = _validate(form)
        if not errors:
            uris = _db_uris(values["database"])
            db_err = _mongo_ping(uris)
            if db_err:
                errors.append(db_err)
            tok_err = _telegram_check(values["bot_token"])
            if tok_err:
                errors.append(tok_err)
        if errors:
            return templates.TemplateResponse(
                "setup.html", {"request": request, "errors": errors, "values": values}, status_code=400
            )
        _write_config(values)
        _save_config_to_db(_db_uris(values["database"]), values)
        _apply_to_env(values)
        threading.Timer(2.0, _restart_into_backend).start()
        return templates.TemplateResponse(
            "setup.html", {"request": request, "errors": [], "values": values, "saved": True}
        )

    @app.get("/{full_path:path}", response_class=HTMLResponse)
    async def catch_all(full_path: str):
        return RedirectResponse(url="/", status_code=302)

    port = int((os.getenv("PORT") or "8000").strip() or "8000")
    LOG.info(f"No configuration detected — starting first-run Setup Wizard on http://0.0.0.0:{port}")
    uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")).run()


def _try_recover_from_db() -> bool:
    uris = _db_uris(os.getenv("DATABASE", ""))
    if len(uris) < 2:
        return False
    doc = _load_config_from_db(uris)
    if not doc:
        return False
    values = {k: str(doc.get(k, "")) for k in CRED_KEYS}
    values["database"] = os.getenv("DATABASE", "")
    if values["api_id"] and values["api_hash"] and ":" in values["bot_token"] and values["owner_id"]:
        LOG.info("Restored saved configuration from database.")
        _apply_to_env(values)
        return True
    return False


def main() -> None:
    if is_configured():
        _launch_backend()
        return
    if _try_recover_from_db():
        _launch_backend()
        return
    run_setup_server()


if __name__ == "__main__":
    main()
