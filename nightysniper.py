# Sniper
#
# Watches Discord messages for gift, promotion, and invite links, then posts
# each new link to a webhook. The Nighty account is always one source. Extra
# accounts are added in the Sniper tab and connect on their own gateway,
# the same way VC Manager keeps managed tokens.
#
# Sniper does not redeem codes. A client that can see the webhook channel
# still has to claim them.
#
# Setup:
#   1. Leave nightysniper.py in the Nighty scripts folder.
#   2. Open the Sniper tab.
#   3. Paste a Discord webhook, choose what to watch, and turn Sniper on.
#   4. Optional: add other account tokens. They stay in json/sniper_tokens.json.
#   5. Invite links are appended to scriptData/invites.txt.

@nightyScript(
    name="Sniper",
    author="Mime | @n0_.q3, Neoarz",
    description="Forward gift, promotion, and invite links from this account and extra tokens.",
    usage="Open the Sniper tab. Add a webhook, then any extra account tokens."
)
def sniper_script():
    """
    SNIPER

    The Sniper tab stores the webhook and watch settings. Extra account
    tokens are saved in json/sniper_tokens.json and are not printed.
    """

    import asyncio
    import json
    import os
    import random
    import re
    import threading
    import time
    import uuid
    from datetime import datetime, timezone
    from pathlib import Path
    from urllib.parse import urlparse

    import aiohttp

    SCRIPT = "Sniper"
    SCRIPT_VERSION = "1.0.0"
    PROJECT_URL = "https://github.com/xmimiez/NightySniper"
    VERSION_SOURCE = "https://raw.githubusercontent.com/xmimiez/NightySniper/main/nightysniper.py"
    RUNTIME_KEY = "_sniper_runtime"
    DATA_DIR = Path(getScriptsPath()) / "json"
    SCRIPT_DATA = Path(getScriptsPath()) / "scriptData"
    TOKENS_PATH = DATA_DIR / "sniper_tokens.json"
    INVITES_PATH = SCRIPT_DATA / "invites.txt"
    WEBHOOK_RE = re.compile(
        r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/(\d+)/[\w-]+/?$",
        re.IGNORECASE,
    )
    MAX_CANDIDATES = 16
    DEDUPE_TTL = 24 * 60 * 60
    DEDUPE_LIMIT = 100000
    QUEUE_LIMIT = 256
    BUILD_NUMBER = 582977

    previous = getattr(bot, RUNTIME_KEY, None)
    runtime = {
        "tasks": set(),
        "listeners": [],
        "clients": {},
        "closed": False,
        "settings": {},
        "account_stats": {},
    }
    setattr(bot, RUNTIME_KEY, runtime)

    if isinstance(previous, dict):
        previous["closed"] = True
        for task in list(previous.get("tasks", set())):
            try:
                if task and not task.done():
                    task.cancel()
            except Exception:
                pass
        for event_name, handler in previous.get("listeners", []):
            try:
                bot.remove_listener(handler, event_name)
            except Exception:
                pass

    file_lock = threading.Lock()
    dedupe_lock = threading.Lock()
    dedupe_entries = {}
    gateway_start_lock = None
    gateway_last_start = 0.0
    http_session = None
    notify_queue = asyncio.Queue(QUEUE_LIMIT)

    def log(message):
        print(f"[{SCRIPT}] {message}", type_="ERROR")

    def create_tracked_task(coroutine):
        try:
            task = bot.loop.create_task(coroutine)
        except Exception as error:
            try:
                coroutine.close()
            except Exception:
                pass
            log(f"Task error: {error}")
            return None
        runtime["tasks"].add(task)
        task.add_done_callback(runtime["tasks"].discard)
        return task

    async def close_previous_clients():
        if not isinstance(previous, dict):
            return
        clients = previous.get("clients", {})
        if isinstance(clients, dict):
            clients = list(clients.values())
        for client in list(clients):
            try:
                await client.close()
            except Exception as error:
                log(f"Could not close a previous account: {type(error).__name__}")
        session = previous.get("http")
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                pass

    create_tracked_task(close_previous_clients())

    def load_tokens():
        try:
            if TOKENS_PATH.exists():
                with TOKENS_PATH.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
                    raise ValueError("Token data must contain a tokens list")
                return data
        except Exception as error:
            log(f"Could not read {TOKENS_PATH.name}: {type(error).__name__}")
            return {"tokens": [], "_load_error": f"Could not read {TOKENS_PATH.name}."}
        return {"tokens": []}

    def save_tokens(data):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            temporary = TOKENS_PATH.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"tokens": data.get("tokens", [])}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, TOKENS_PATH)
            return True
        except Exception as error:
            log(f"Could not save tokens: {type(error).__name__}")
            return False

    def clean_token(value):
        token = str(value or "").strip().strip('"').strip("'").strip()
        if not token or any(character.isspace() for character in token):
            return ""
        return token

    def webhook_id_from_url(url):
        match = WEBHOOK_RE.match(str(url or "").strip())
        return match.group(1) if match else ""

    def read_settings():
        config = getConfigData()
        webhook = str(config.get("sniper_webhook_url", "") or "").strip()
        presence = str(config.get("sniper_presence", "invisible") or "invisible").lower()
        if presence not in {"invisible", "online", "idle", "dnd"}:
            presence = "invisible"
        username = str(config.get("sniper_webhook_username", "") or "").strip()
        if username and not 1 <= len(username) <= 80:
            username = username[:80]
        avatar = str(config.get("sniper_webhook_avatar", "") or "").strip()
        if avatar and not avatar.lower().startswith("https://"):
            avatar = ""
        kinds = {
            "gift": bool(config.get("sniper_watch_gifts", True)),
            "promotion": bool(config.get("sniper_watch_promos", True)),
            "invite": bool(config.get("sniper_watch_invites", True)),
        }
        return {
            "enabled": bool(config.get("sniper_enabled", False)),
            "webhook": webhook,
            "webhook_id": webhook_id_from_url(webhook),
            "username": username,
            "avatar": avatar,
            "presence": presence,
            "dedupe": bool(config.get("sniper_dedupe", True)),
            "kinds": kinds,
        }

    def store_settings(updates):
        current = read_settings()
        if "enabled" in updates:
            updateConfigData("sniper_enabled", bool(updates["enabled"]))
        if "webhook" in updates:
            updateConfigData("sniper_webhook_url", str(updates["webhook"] or "").strip())
        if "username" in updates:
            updateConfigData("sniper_webhook_username", str(updates["username"] or "").strip()[:80])
        if "avatar" in updates:
            updateConfigData("sniper_webhook_avatar", str(updates["avatar"] or "").strip())
        if "presence" in updates:
            updateConfigData("sniper_presence", str(updates["presence"] or "invisible"))
        if "dedupe" in updates:
            updateConfigData("sniper_dedupe", bool(updates["dedupe"]))
        if "gifts" in updates:
            updateConfigData("sniper_watch_gifts", bool(updates["gifts"]))
        if "promos" in updates:
            updateConfigData("sniper_watch_promos", bool(updates["promos"]))
        if "invites" in updates:
            updateConfigData("sniper_watch_invites", bool(updates["invites"]))
        fresh = read_settings()
        runtime["settings"].clear()
        runtime["settings"].update(fresh)
        return current, fresh

    runtime["settings"].update(read_settings())

    def alnum(value):
        return ("a" <= value <= "z") or ("A" <= value <= "Z") or ("0" <= value <= "9")

    def unreserved(value):
        return alnum(value) or value in "-._~"

    def lower_ascii(value):
        if "A" <= value <= "Z":
            return chr(ord(value) + 32)
        return value

    def equal_fold_at(value, start, target):
        if len(value) - start < len(target):
            return False
        for index, expected in enumerate(target):
            if lower_ascii(value[start + index]) != expected:
                return False
        return True

    SIGNATURES = (
        ("gift", "discord.gift/", 16, 24, alnum),
        ("gift", "discord.com/gift/", 16, 24, alnum),
        ("gift", "discord.com/gifts/", 16, 24, alnum),
        ("gift", "canary.discord.com/gifts/", 16, 24, alnum),
        ("gift", "ptb.discord.com/gifts/", 16, 24, alnum),
        ("gift", "discordapp.com/gifts/", 16, 24, alnum),
        ("gift", "canary.discordapp.com/gifts/", 16, 24, alnum),
        ("gift", "ptb.discordapp.com/gifts/", 16, 24, alnum),
        ("promotion", "promos.discord.gg/", 4, 128, unreserved),
        ("promotion", "discord.com/billing/promotions/", 4, 128, unreserved),
        ("invite", "discord.gg/", 1, 64, unreserved),
        ("invite", "discord.com/invite/", 1, 64, unreserved),
        ("invite", "discord.com/invites/", 1, 64, unreserved),
    )

    def link_boundary(content, start):
        if start == 0:
            return True
        previous_char = content[start - 1]
        return not alnum(previous_char) and previous_char not in ".-_/@:"

    def candidates_in(content):
        found = []
        seen = set()
        start = 0
        length = len(content)
        while start < length and len(found) < MAX_CANDIDATES:
            if not link_boundary(content, start):
                start += 1
                continue
            host = start
            if equal_fold_at(content, start, "https://"):
                host += len("https://")
            elif equal_fold_at(content, start, "http://"):
                host += len("http://")
            matched = False
            for kind, prefix, minimum, maximum, allowed in SIGNATURES:
                if not equal_fold_at(content, host, prefix):
                    continue
                code_start = host + len(prefix)
                end = code_start
                while end < length and allowed(content[end]):
                    end += 1
                code = content[code_start:end].rstrip(".~")
                if minimum <= len(code) <= maximum:
                    key = (kind, code)
                    if key not in seen:
                        seen.add(key)
                        found.append(key)
                start = end
                matched = True
                break
            if not matched:
                start += 1
        return found

    def candidate_link(kind, code):
        if kind == "gift":
            return "https://discord.gift/" + code
        if kind == "promotion":
            return "https://promos.discord.gg/" + code
        if kind == "invite":
            return "https://discord.gg/" + code
        return ""

    def reserve_candidate(kind, code):
        settings = runtime["settings"]
        if not settings.get("dedupe", True):
            return True
        now = time.time()
        key = (kind, code)
        with dedupe_lock:
            expired = [item for item, stamp in dedupe_entries.items() if stamp <= now]
            for item in expired[:32]:
                dedupe_entries.pop(item, None)
            stamp = dedupe_entries.get(key)
            if stamp and stamp > now:
                return False
            if len(dedupe_entries) >= DEDUPE_LIMIT:
                return False
            dedupe_entries[key] = now + DEDUPE_TTL
            return True

    def release_candidate(kind, code):
        with dedupe_lock:
            dedupe_entries.pop((kind, code), None)

    def log_invite(code):
        link = candidate_link("invite", code)
        if not link:
            return
        try:
            SCRIPT_DATA.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with file_lock:
                with INVITES_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(f"{stamp} {link}\n")
        except Exception as error:
            log(f"Could not write invites.txt: {type(error).__name__}")

    def account_bucket(source):
        source = str(source or "primary")
        bucket = runtime["account_stats"].get(source)
        if bucket is None:
            bucket = {
                "messages": 0,
                "candidates": 0,
                "forwarded": 0,
                "duplicates": 0,
                "dropped": 0,
            }
            runtime["account_stats"][source] = bucket
        return bucket

    def admit_text(content, webhook_id="", source="primary"):
        if runtime["closed"]:
            return
        settings = runtime["settings"]
        if not settings.get("enabled"):
            return
        bucket = account_bucket(source)
        bucket["messages"] += 1
        destination = settings.get("webhook_id") or ""
        if destination and str(webhook_id or "") == destination:
            return
        for kind, code in candidates_in(str(content or "")):
            if not settings["kinds"].get(kind, False):
                continue
            bucket["candidates"] += 1
            if not reserve_candidate(kind, code):
                bucket["duplicates"] += 1
                continue
            if kind == "invite":
                log_invite(code)
            try:
                notify_queue.put_nowait((kind, code, source))
            except asyncio.QueueFull:
                release_candidate(kind, code)
                bucket["dropped"] += 1

    async def ensure_http():
        nonlocal http_session
        if runtime["closed"]:
            return None
        if http_session is None or http_session.closed:
            http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
            )
            runtime["http"] = http_session
        return http_session

    async def notify_worker():
        while not runtime["closed"]:
            try:
                kind, code, source = await notify_queue.get()
            except asyncio.CancelledError:
                return
            try:
                delivered = await post_candidate(kind, code)
                if delivered:
                    account_bucket(source)["forwarded"] += 1
                else:
                    release_candidate(kind, code)
            except asyncio.CancelledError:
                release_candidate(kind, code)
                return
            except Exception as error:
                release_candidate(kind, code)
                log(f"Webhook worker failed: {type(error).__name__}")
            finally:
                notify_queue.task_done()
            push_ui()

    async def post_candidate(kind, code):
        settings = runtime["settings"]
        url = settings.get("webhook") or ""
        if not webhook_id_from_url(url):
            return False
        link = candidate_link(kind, code)
        if not link:
            return False
        payload = {
            "content": link,
            "allowed_mentions": {"parse": []},
        }
        if settings.get("username"):
            payload["username"] = settings["username"]
        if settings.get("avatar"):
            payload["avatar_url"] = settings["avatar"]
        session = await ensure_http()
        if session is None:
            return False
        for attempt in range(3):
            if runtime["closed"]:
                return False
            try:
                async with session.post(url, json=payload, allow_redirects=False) as response:
                    if 200 <= response.status < 300:
                        return True
                    if response.status == 429 and attempt < 2:
                        try:
                            body = await response.json(content_type=None)
                            delay = float(body.get("retry_after", 1))
                        except Exception:
                            delay = 1
                        await asyncio.sleep(min(max(delay, 0.2), 10))
                        continue
                    return False
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return False
        return False

    def normalise_gateway_base(value):
        raw = str(value or "").strip()
        if not raw:
            return "wss://gateway.discord.gg"
        try:
            parsed = urlparse(raw if "://" in raw else f"wss://{raw}")
            hostname = str(parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme.lower() != "wss" or not (
                hostname == "gateway.discord.gg" or hostname.endswith(".discord.gg")
            ):
                return "wss://gateway.discord.gg"
            port = f":{parsed.port}" if parsed.port else ""
            path = str(parsed.path or "").rstrip("/")
            return f"wss://{hostname}{port}{path}"
        except Exception:
            return "wss://gateway.discord.gg"

    async def wait_for_gateway_slot():
        nonlocal gateway_start_lock, gateway_last_start
        if gateway_start_lock is None:
            gateway_start_lock = asyncio.Lock()
        await gateway_start_lock.acquire()
        delay = 5.25 - (time.monotonic() - gateway_last_start)
        if delay > 0:
            await asyncio.sleep(delay)
        gateway_last_start = time.monotonic()
        return gateway_start_lock

    class SniperGateway:
        def __init__(self, token, name, record_id):
            self.token = token
            self.name = name or "Token"
            self.record_id = str(record_id)
            self.ws = None
            self.session_id = None
            self.sequence = None
            self.resume_gateway_url = None
            self.user_id = ""
            self.user_name = ""
            self.status = "Idle"
            self.last_error = ""
            self.listener_task = None
            self.heartbeat_task = None
            self.reconnect_task = None
            self.sub_task = None
            self._closing = False
            self._starting = False
            self._terminal = False
            self._generation = 0
            self._heartbeat_ok = True
            self._resume_on_hello = False
            self._reconnects = 0
            self.guilds = {}
            self.selected_guild = ""
            self.subscribed = set()

        def snapshot(self):
            label = self.user_name or self.name
            if self.user_id:
                label = f"{label} ({self.user_id})"
            return {
                "id": self.record_id,
                "name": self.name,
                "user": label,
                "status": self.status,
                "error": self.last_error,
                "enabled": True,
            }

        def _set_status(self, status, error=""):
            self.status = str(status or "Idle")
            self.last_error = str(error or "")
            if self.last_error:
                log(f"{self.name}: {self.last_error}")
            push_ui()

        def _alive(self):
            return bool(
                self.ws
                and not self.ws.closed
                and self.listener_task
                and not self.listener_task.done()
            )

        async def close(self):
            self._closing = True
            self._terminal = True
            self.status = "Disconnected"
            for task in (self.reconnect_task, self.sub_task, self.heartbeat_task, self.listener_task):
                if task and not task.done():
                    task.cancel()
            if self.ws and not self.ws.closed:
                try:
                    await self.ws.close()
                except Exception:
                    pass
            self.ws = None

        def _clear_session(self):
            self.session_id = None
            self.sequence = None
            self.resume_gateway_url = None
            self._resume_on_hello = False
            self.guilds = {}
            self.selected_guild = ""
            self.subscribed = set()

        def schedule_reconnect(self, delay):
            if self._closing or self._terminal or runtime["closed"]:
                return
            if self.reconnect_task and not self.reconnect_task.done():
                return

            async def reconnect():
                wait = max(float(delay or 0), 0)
                while not self._closing and not self._terminal and not runtime["closed"]:
                    if wait:
                        await asyncio.sleep(wait)
                    if await self.start():
                        return
                    self._reconnects += 1
                    wait = min(5 * (2 ** min(self._reconnects, 5)), 300)

            self.reconnect_task = create_tracked_task(reconnect())

        async def start(self):
            if self._terminal or self._closing or runtime["closed"]:
                return False
            if not runtime["settings"].get("enabled"):
                self._set_status("Paused", "Sniper is off.")
                return False
            if self._alive():
                return True
            if self._starting:
                for _ in range(100):
                    await asyncio.sleep(0.1)
                    if self._alive() or not self._starting:
                        break
                return self._alive()
            self._starting = True
            start_lock = None
            try:
                self._set_status("Connecting")
                session = await ensure_http()
                if session is None:
                    return False
                self._resume_on_hello = bool(self.session_id and self.sequence is not None)
                start_lock = await wait_for_gateway_slot()
                bases = [normalise_gateway_base(self.resume_gateway_url)]
                if bases[0] != "wss://gateway.discord.gg":
                    bases.append("wss://gateway.discord.gg")
                self.ws = None
                last_error = None
                for base in bases:
                    gateway_url = f"{base.rstrip('/')}/?v=10&encoding=json"
                    try:
                        self.ws = await asyncio.wait_for(
                            session.ws_connect(gateway_url, heartbeat=None, autoclose=True, autoping=True),
                            timeout=20,
                        )
                        break
                    except Exception as error:
                        last_error = error
                if self.ws is None:
                    raise last_error or ConnectionError("Gateway connection failed")
                self._generation += 1
                generation = self._generation
                socket = self.ws
                self.listener_task = create_tracked_task(self._loop(socket, generation))
                if self.listener_task is None:
                    self._set_status("Gateway failed", "Nighty could not start this account.")
                    return False
                self._reconnects = 0
                return True
            except Exception as error:
                log(f"Gateway connect failed for {self.name}: {type(error).__name__}")
                self._set_status("Gateway failed", "Could not reach Discord. Check the connection, then try again.")
                self.ws = None
                self.schedule_reconnect(15)
                return False
            finally:
                if start_lock and start_lock.locked():
                    start_lock.release()
                self._starting = False

        async def _identify(self, socket):
            if self._resume_on_hello and self.session_id and self.sequence is not None:
                await socket.send_json({
                    "op": 6,
                    "d": {
                        "token": self.token,
                        "session_id": self.session_id,
                        "seq": self.sequence,
                    },
                })
                return
            presence = runtime["settings"].get("presence", "invisible")
            await socket.send_json({
                "op": 2,
                "d": {
                    "token": self.token,
                    "capabilities": 16381,
                    "properties": {
                        "os": "Windows",
                        "browser": "Chrome",
                        "device": "",
                        "system_locale": "en-US",
                        "browser_user_agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/116.0.0.0 Safari/537.36"
                        ),
                        "browser_version": "116.0.0.0",
                        "os_version": "10",
                        "release_channel": "stable",
                        "client_build_number": BUILD_NUMBER,
                    },
                    "presence": {
                        "status": presence,
                        "since": 0,
                        "activities": [],
                        "afk": False,
                    },
                    "compress": False,
                    "client_state": {
                        "guild_versions": {},
                        "highest_last_message_id": "0",
                        "read_state_version": 0,
                        "user_guild_settings_version": -1,
                        "user_settings_version": -1,
                        "private_channels_version": "0",
                        "api_code_version": 0,
                    },
                },
            })

        def _subscription_targets(self):
            return list(self.guilds.keys())

        def _queue_subscriptions(self, guild_ids):
            pending = [guild_id for guild_id in guild_ids if guild_id and guild_id not in self.subscribed]
            if not pending:
                return
            if self.sub_task and not self.sub_task.done():
                self.subscribed.difference_update(pending)
            generation = self._generation

            async def send_subscriptions():
                for guild_id in pending:
                    if self._closing or self._generation != generation or not self.ws or self.ws.closed:
                        return
                    if guild_id in self.subscribed:
                        continue
                    self.subscribed.add(guild_id)
                    try:
                        await self.ws.send_json({
                            "op": 14,
                            "d": {
                                "guild_id": guild_id,
                                "typing": True,
                                "threads": True,
                                "activities": True,
                            },
                        })
                    except Exception:
                        self.subscribed.discard(guild_id)
                        return
                    await asyncio.sleep(0.35)

            self.sub_task = create_tracked_task(send_subscriptions())

        def _remember_guild(self, guild):
            guild_id = str((guild or {}).get("id") or "")
            if not guild_id:
                return ""
            current = self.guilds.get(guild_id, {})
            members = guild.get("member_count", current.get("member_count", 0))
            try:
                members = int(members or 0)
            except (TypeError, ValueError):
                members = 0
            self.guilds[guild_id] = {"member_count": members}
            return guild_id

        async def _heartbeat(self, socket, generation, interval):
            try:
                interval_seconds = max(float(interval) / 1000.0, 1.0)
                await asyncio.sleep(random.random() * interval_seconds)
                while self.ws is socket and self._generation == generation and not socket.closed:
                    if not self._heartbeat_ok:
                        await socket.close()
                        return
                    self._heartbeat_ok = False
                    await socket.send_json({"op": 1, "d": self.sequence})
                    await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                return
            except Exception:
                return

        def _close_meaning(self, code):
            if code == 4004:
                return "Discord rejected this token. Remove it and add a fresh one.", False
            if code == 4014:
                return "Discord disallowed this gateway session.", False
            if code == 4008:
                return "Discord rate-limited this account. Sniper will wait before retrying.", True
            return "The gateway closed. Sniper will reconnect.", True

        async def _loop(self, socket, generation):
            should_retry = True
            retry_delay = 5
            try:
                async for message in socket:
                    if runtime["closed"] or self._closing or self._generation != generation:
                        should_retry = False
                        return
                    if message.type != aiohttp.WSMsgType.TEXT:
                        if message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                            break
                        continue
                    try:
                        data = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get("s") is not None:
                        self.sequence = data.get("s")
                    opcode = data.get("op")
                    event_name = data.get("t")
                    payload = data.get("d") if isinstance(data.get("d"), dict) else {}
                    if opcode == 10:
                        self._heartbeat_ok = True
                        if self.heartbeat_task and not self.heartbeat_task.done():
                            self.heartbeat_task.cancel()
                        self.heartbeat_task = create_tracked_task(
                            self._heartbeat(socket, generation, payload.get("heartbeat_interval", 41250))
                        )
                        await self._identify(socket)
                    elif opcode == 11:
                        self._heartbeat_ok = True
                    elif opcode == 1:
                        await socket.send_json({"op": 1, "d": self.sequence})
                    elif opcode == 7:
                        should_retry = True
                        break
                    elif opcode == 9:
                        if not data.get("d"):
                            self._clear_session()
                        should_retry = True
                        break
                    elif event_name == "READY":
                        self.session_id = payload.get("session_id")
                        resume_url = str(payload.get("resume_gateway_url") or "").strip()
                        if resume_url:
                            self.resume_gateway_url = resume_url
                        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
                        self.user_id = str(user.get("id") or "")
                        self.user_name = str(user.get("global_name") or user.get("username") or self.name)
                        self.guilds = {}
                        self.subscribed = set()
                        for guild in payload.get("guilds") or []:
                            if isinstance(guild, dict):
                                self._remember_guild(guild)
                        self._queue_subscriptions(self._subscription_targets())
                        self._set_status("Ready")
                    elif event_name == "RESUMED":
                        self.subscribed = set()
                        self._queue_subscriptions(self._subscription_targets())
                        self._set_status("Ready")
                    elif event_name == "GUILD_CREATE" and isinstance(data.get("d"), dict):
                        guild_id = self._remember_guild(data.get("d"))
                        if guild_id:
                            self._queue_subscriptions([guild_id])
                    elif event_name == "GUILD_DELETE" and isinstance(data.get("d"), dict):
                        guild = data.get("d") or {}
                        guild_id = str(guild.get("id") or "")
                        if guild_id and not guild.get("unavailable"):
                            self.guilds.pop(guild_id, None)
                            self.subscribed.discard(guild_id)
                    elif event_name == "MESSAGE_CREATE" and isinstance(data.get("d"), dict):
                        body = data.get("d") or {}
                        admit_text(body.get("content") or "", body.get("webhook_id") or "", self.record_id)
            except asyncio.CancelledError:
                should_retry = False
                return
            except Exception as error:
                log(f"Gateway loop stopped for {self.name}: {type(error).__name__}")
            finally:
                if self.ws is socket:
                    self.ws = None
                if self.heartbeat_task and not self.heartbeat_task.done():
                    self.heartbeat_task.cancel()
                if should_retry and not self._closing and not self._terminal and not runtime["closed"]:
                    close_code = None
                    try:
                        close_code = int(getattr(socket, "close_code", None))
                    except (TypeError, ValueError):
                        close_code = None
                    reason, retry = self._close_meaning(close_code)
                    if not retry:
                        self._terminal = True
                        self._set_status("Token rejected", reason)
                    else:
                        if close_code == 4008:
                            retry_delay = 300
                        self._set_status("Reconnecting", reason)
                        self.schedule_reconnect(retry_delay)

    def public_rows():
        rows = []
        settings = runtime["settings"]
        primary = "Watching" if settings.get("enabled") else "Paused"
        rows.append({
            "id": "primary",
            "name": "This Nighty account",
            "user": str(getattr(getattr(bot, "user", None), "name", "") or "Current session"),
            "status": primary,
            "error": "",
            "enabled": True,
            "locked": True,
        })
        saved = load_tokens()
        clients = runtime["clients"]
        for item in saved.get("tokens", []):
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("id") or "")
            client = clients.get(record_id)
            if client:
                row = client.snapshot()
            else:
                row = {
                    "id": record_id,
                    "name": str(item.get("name") or "Token"),
                    "user": str(item.get("name") or "Token"),
                    "status": "Disconnected" if item.get("enabled", True) else "Off",
                    "error": "",
                    "enabled": bool(item.get("enabled", True)),
                }
            row["enabled"] = bool(item.get("enabled", True))
            row["locked"] = False
            rows.append(row)
        return rows

    def table_rows():
        rows = []
        for row in public_rows():
            detail = row.get("user") or ""
            if row.get("error"):
                detail = f"{detail} — {row['error']}".strip(" —")
            counts = account_bucket(row.get("id"))
            detail = (
                f"{detail} · Messages {counts['messages']}. Links {counts['candidates']}. "
                f"Sent {counts['forwarded']}. Repeats {counts['duplicates']}. Dropped {counts['dropped']}."
            ).strip(" ·")
            status = row.get("status") or ""
            if row.get("locked"):
                color = "green" if status == "Watching" else "gray"
            elif status == "Ready":
                color = "green"
            elif row.get("error"):
                color = "red"
            else:
                color = "gray"
            rows.append({
                "id": row["id"],
                "cells": [
                    {"text": row.get("name") or "Account", "subtext": detail},
                    {"text": status, "color": color},
                ],
            })
        return rows

    def push_ui():
        if runtime["closed"]:
            return
        try:
            if ui_refs.get("table") is not None:
                ui_refs["table"].rows = table_rows()
        except Exception:
            pass

    async def sync_gateways():
        if runtime["closed"]:
            return
        settings = runtime["settings"]
        saved = load_tokens()
        wanted = {}
        for item in saved.get("tokens", []):
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("id") or "")
            token = clean_token(item.get("token"))
            if record_id and token and item.get("enabled", True) and settings.get("enabled"):
                wanted[record_id] = (token, str(item.get("name") or "Token"))
        for record_id, client in list(runtime["clients"].items()):
            if record_id not in wanted:
                runtime["clients"].pop(record_id, None)
                await client.close()
        for record_id, (token, name) in wanted.items():
            client = runtime["clients"].get(record_id)
            if client is None:
                client = SniperGateway(token, name, record_id)
                runtime["clients"][record_id] = client
            if not client._alive() and not client._terminal:
                await client.start()
        push_ui()

    def restart_gateways():
        async def restart():
            for client in list(runtime["clients"].values()):
                client._clear_session()
                await client.close()
                client._closing = False
                client._terminal = False
            runtime["clients"].clear()
            await sync_gateways()
        create_tracked_task(restart())

    ui_refs = {}
    typed = {}

    def remember_input(key):
        def store(value):
            if isinstance(value, dict):
                value = value.get("value", "")
            typed[key] = str(value or "")
        return store

    def field_text(key, element):
        if key in typed:
            return typed[key].strip()
        return str(getattr(element, "value", "") or "").strip()

    def selected_id(selected_items):
        if isinstance(selected_items, dict):
            selected_items = selected_items.get("selected_items") or selected_items.get("items") or []
        if isinstance(selected_items, str):
            return selected_items
        if isinstance(selected_items, (list, tuple)) and selected_items:
            first = selected_items[0]
            if isinstance(first, dict):
                return str(first.get("id") or "")
            return str(first or "")
        return ""

    settings = runtime["settings"]
    tab = Tab(name="Sniper", icon="nitro", gap=4, title="Sniper")
    container = tab.create_container(type="rows", width="full", height="auto", gap=4)

    settings_card = container.create_card(width="full", height="auto", gap=3)
    settings_card.create_ui_element(UI.Text, content="Sniper", size="lg", weight="bold")
    settings_card.create_ui_element(
        UI.Text,
        content=f"Sniper v{SCRIPT_VERSION} posts links to the webhook. It does not redeem them.",
        size="sm",
    )
    fallback_notices = {}
    notice_text = settings_card.create_ui_element(UI.Text, content="", visible=False)
    notice_button = settings_card.create_ui_element(UI.Button, label="Later", visible=False)
    pending_notices = set()

    def refresh_update_notice():
        if fallback_notices:
            notice_text.content = next(iter(fallback_notices.values()))
        notice_text.visible = notice_button.visible = bool(fallback_notices)

    def dismiss_update_notice():
        if not fallback_notices:
            return
        token = next(iter(fallback_notices))
        pending_notices.discard(token)
        del fallback_notices[token]
        refresh_update_notice()

    notice_button.onClick = dismiss_update_notice

    def nightysniper_notice_rendered(token):
        if token in pending_notices:
            pending_notices.discard(token)
            fallback_notices.pop(token, None)
            refresh_update_notice()

    def show_update_notice(latest):
        if runtime["closed"]:
            return
        title = "Nighty Sniper update available"
        message = (
            f"Installed: v{SCRIPT_VERSION}. Available: v{latest}. "
            "Replace nightysniper.py and reload it in Nighty."
        )
        token = uuid.uuid4().hex
        pending_notices.add(token)
        payload = json.dumps([title, message, PROJECT_URL, token])
        try:
            evaluate_js("""(([title, message, url, token]) => {
                const dialog = document.createElement('dialog');
                dialog.setAttribute('aria-label', title);
                dialog.style.cssText = 'position:fixed;inset:0;margin:auto;width:min(90vw,28rem);height:fit-content;box-sizing:border-box;padding:22px;border:1px solid #53586b;border-radius:12px;background:#191b23;color:#eee;z-index:10000';
                const heading = document.createElement('strong');
                heading.textContent = title;
                const body = document.createElement('p');
                body.textContent = message;
                const dismiss = document.createElement('button');
                dismiss.textContent = 'Later';
                dismiss.onclick = () => dialog.remove();
                const link = document.createElement('a');
                link.textContent = 'Open repository';
                link.href = url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.style.cssText = 'margin-left:16px;color:#aacaff';
                dialog.append(heading, body, dismiss, link);
                document.body.append(dialog);
                if (typeof dialog.showModal === 'function') dialog.showModal();
                else dialog.setAttribute('open', '');
                window.pywebview?.api?.customCallback('nightysniper_notice_rendered', token);
            })(""" + payload + ");")
        except Exception:
            pass

        async def check_delivery():
            await asyncio.sleep(3)
            if token in pending_notices and not runtime["closed"]:
                fallback_notices[token] = f"{title}: {message} {PROJECT_URL}"
                refresh_update_notice()

        create_tracked_task(check_delivery())

    async def check_update():
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            headers = {"User-Agent": f"NightySniper/{SCRIPT_VERSION}"}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(VERSION_SOURCE) as response:
                    if response.status != 200:
                        return
                    # The version declaration lives in the script header. Never execute remote code.
                    size = 0
                    async for line in response.content:
                        size += len(line)
                        if size > 16384 or len(line) > 4096:
                            return
                        match = re.fullmatch(rb'\s*SCRIPT_VERSION\s*=\s*"(\d{1,6}\.\d{1,6}\.\d{1,6})"\s*', line)
                        if match:
                            latest = match[1].decode("ascii")
                            if tuple(map(int, latest.split("."))) > tuple(map(int, SCRIPT_VERSION.split("."))):
                                show_update_notice(latest)
                            return
        except (OSError, asyncio.TimeoutError, ValueError, aiohttp.ClientError):
            return

    enabled_toggle = settings_card.create_ui_element(
        UI.Toggle,
        label="Sniper on",
        checked=bool(settings.get("enabled")),
    )
    webhook_input = settings_card.create_ui_element(
        UI.Input,
        label="Webhook",
        placeholder="https://discord.com/api/webhooks/...",
        value=settings.get("webhook", ""),
        full_width=True,
        show_clear_button=True,
    )
    username_input = settings_card.create_ui_element(
        UI.Input,
        label="Webhook name",
        placeholder="Optional",
        value=settings.get("username", ""),
        full_width=True,
    )
    avatar_input = settings_card.create_ui_element(
        UI.Input,
        label="Webhook avatar URL",
        placeholder="https://...",
        value=settings.get("avatar", ""),
        full_width=True,
    )
    presence_select = settings_card.create_ui_element(
        UI.Select,
        label="Extra account presence",
        items=[
            {"id": "invisible", "title": "Invisible"},
            {"id": "online", "title": "Online"},
            {"id": "idle", "title": "Idle"},
            {"id": "dnd", "title": "Do Not Disturb"},
        ],
        selected_items=[settings.get("presence", "invisible")],
        mode="single",
        full_width=True,
    )
    dedupe_toggle = settings_card.create_ui_element(
        UI.Toggle,
        label="Skip repeats for 24 hours",
        checked=bool(settings.get("dedupe", True)),
    )
    gifts_toggle = settings_card.create_ui_element(
        UI.Toggle,
        label="Gifts",
        checked=bool(settings["kinds"].get("gift", True)),
    )
    promos_toggle = settings_card.create_ui_element(
        UI.Toggle,
        label="Promotions",
        checked=bool(settings["kinds"].get("promotion", True)),
    )
    invites_toggle = settings_card.create_ui_element(
        UI.Toggle,
        label="Invites",
        checked=bool(settings["kinds"].get("invite", True)),
    )
    save_button = settings_card.create_ui_element(
        UI.Button,
        label="Save",
        variant="solid",
        color="primary",
    )

    webhook_input.onInput = remember_input("webhook")
    username_input.onInput = remember_input("username")
    avatar_input.onInput = remember_input("avatar")

    def handle_save():
        save_button.loading = True
        try:
            webhook = field_text("webhook", webhook_input)
            avatar = field_text("avatar", avatar_input)
            username = field_text("username", username_input)
            if webhook and not webhook_id_from_url(webhook):
                webhook_input.invalid = True
                webhook_input.error_message = "Use a full Discord webhook URL."
                tab.toast("Webhook invalid", "Use a full Discord webhook URL.", "ERROR")
                return
            webhook_input.invalid = False
            webhook_input.error_message = ""
            if avatar and not avatar.lower().startswith("https://"):
                avatar_input.invalid = True
                avatar_input.error_message = "The avatar must be an HTTPS image URL."
                tab.toast("Avatar invalid", "The avatar must be an HTTPS image URL.", "ERROR")
                return
            avatar_input.invalid = False
            avatar_input.error_message = ""
            presence = selected_id(getattr(presence_select, "selected_items", None)) or settings.get("presence", "invisible")
            before, after = store_settings({
                "enabled": bool(enabled_toggle.checked),
                "dedupe": bool(dedupe_toggle.checked),
                "gifts": bool(gifts_toggle.checked),
                "promos": bool(promos_toggle.checked),
                "invites": bool(invites_toggle.checked),
                "webhook": webhook,
                "username": username,
                "avatar": avatar,
                "presence": presence,
            })
            if not after["webhook_id"] and after["enabled"]:
                updateConfigData("sniper_enabled", False)
                runtime["settings"]["enabled"] = False
                enabled_toggle.checked = False
                tab.toast("Webhook required", "Add a webhook before turning Sniper on.", "ERROR")
            else:
                tab.toast("Sniper saved", "Settings are stored.", "SUCCESS")
            session_changed = (
                before.get("presence") != runtime["settings"].get("presence")
                or before.get("enabled") != runtime["settings"].get("enabled")
            )
            if session_changed:
                restart_gateways()
            else:
                create_tracked_task(sync_gateways())
            push_ui()
        finally:
            save_button.loading = False

    save_button.onClick = handle_save
    enabled_toggle.onChange = lambda checked: handle_save()
    dedupe_toggle.onChange = lambda checked: handle_save()
    gifts_toggle.onChange = lambda checked: handle_save()
    promos_toggle.onChange = lambda checked: handle_save()
    invites_toggle.onChange = lambda checked: handle_save()
    presence_select.onChange = lambda selected: handle_save()

    token_card = container.create_card(width="full", height="auto", gap=3)
    token_card.create_ui_element(UI.Text, content="Extra accounts", size="lg", weight="bold")
    token_card.create_ui_element(
        UI.Text,
        content="Add other tokens here. Sniper connects them when Sniper is on.",
        size="sm",
    )
    token_input = token_card.create_ui_element(
        UI.Input,
        label="Account token",
        placeholder="Paste a Discord token",
        value="",
        full_width=True,
        show_clear_button=True,
    )
    name_input = token_card.create_ui_element(
        UI.Input,
        label="Label",
        placeholder="Alt 1",
        value="",
        full_width=True,
    )
    add_button = token_card.create_ui_element(
        UI.Button,
        label="Add account",
        variant="solid",
        color="primary",
    )
    token_input.onInput = remember_input("token")
    name_input.onInput = remember_input("name")

    def handle_add():
        add_button.loading = True
        try:
            token = clean_token(field_text("token", token_input))
            if not token:
                token_input.invalid = True
                token_input.error_message = "Paste the account token only, without spaces or quotes."
                tab.toast("Token missing", "Paste the account token only, without spaces or quotes.", "ERROR")
                return
            token_input.invalid = False
            token_input.error_message = ""
            saved = load_tokens()
            if saved.get("_load_error"):
                tab.toast("Token file unreadable", saved["_load_error"], "ERROR")
                return
            if any(clean_token(item.get("token")) == token for item in saved.get("tokens", []) if isinstance(item, dict)):
                tab.toast("Account already added", "That token is already in the list.", "ERROR")
                return
            name = field_text("name", name_input) or f"Token {len(saved.get('tokens', [])) + 1}"
            saved["tokens"].append({
                "id": str(uuid.uuid4()),
                "token": token,
                "name": name[:80],
                "enabled": True,
            })
            if not save_tokens(saved):
                tab.toast("Could not save", f"Nighty could not write {TOKENS_PATH.name}.", "ERROR")
                return
            typed.pop("token", None)
            typed.pop("name", None)
            token_input.value = ""
            name_input.value = ""
            tab.toast("Account added", f"{name} is saved.", "SUCCESS")
            create_tracked_task(sync_gateways())
            push_ui()
        finally:
            add_button.loading = False

    add_button.onClick = handle_add

    account_card = container.create_card(width="full", height="auto", gap=3)
    account_card.create_ui_element(UI.Text, content="Accounts", size="lg", weight="bold")
    ui_refs["table"] = account_card.create_ui_element(
        UI.Table,
        columns=[
            {"type": "text", "label": "Account"},
            {"type": "tag", "label": "Status"},
        ],
        rows=table_rows(),
        search=False,
        items_per_page=8,
        selectable=True,
    )
    selected_row = {"id": ""}

    def remember_selection(row_ids):
        selected_row["id"] = selected_id(row_ids)

    ui_refs["table"].onSelectionChange = remember_selection

    account_actions = account_card.create_group(type="columns", gap=2, full_width=True)
    toggle_button = account_actions.create_ui_element(
        UI.Button,
        label="Connect / Disconnect",
        variant="bordered",
        color="primary",
    )
    remove_button = account_actions.create_ui_element(
        UI.Button,
        label="Remove",
        variant="solid",
        color="danger",
    )

    def selected_record():
        record_id = selected_row["id"]
        if not record_id or record_id == "primary":
            tab.toast("Pick an extra account", "Select a saved token, not this Nighty account.", "ERROR")
            return ""
        return record_id

    def handle_toggle_selected():
        record_id = selected_record()
        if not record_id:
            return
        with file_lock:
            saved = load_tokens()
            if saved.get("_load_error"):
                tab.toast("Token file unreadable", saved["_load_error"], "ERROR")
                return
            tokens = [item for item in saved.get("tokens", []) if isinstance(item, dict)]
            target = next((item for item in tokens if str(item.get("id") or "") == record_id), None)
            if target is None:
                push_ui()
                return
            target["enabled"] = not bool(target.get("enabled", True))
            saved["tokens"] = tokens
            if not save_tokens(saved):
                tab.toast("Could not update", "The token file could not be updated.", "ERROR")
                return
        create_tracked_task(sync_gateways())

    def handle_remove_selected():
        record_id = selected_record()
        if not record_id:
            return
        removed = None
        with file_lock:
            saved = load_tokens()
            if saved.get("_load_error"):
                tab.toast("Token file unreadable", saved["_load_error"], "ERROR")
                return
            tokens = [item for item in saved.get("tokens", []) if isinstance(item, dict)]
            removed = next((item for item in tokens if str(item.get("id") or "") == record_id), None)
            if removed is None:
                push_ui()
                return
            saved["tokens"] = [item for item in tokens if str(item.get("id") or "") != record_id]
            if not save_tokens(saved):
                tab.toast("Could not remove", "The token file could not be updated.", "ERROR")
                return
        selected_row["id"] = ""
        tab.toast("Account removed", str((removed or {}).get("name") or "Account"), "SUCCESS")
        client = runtime["clients"].pop(record_id, None)
        if client:
            async def drop_client():
                await client.close()
                push_ui()
            create_tracked_task(drop_client())
        else:
            create_tracked_task(sync_gateways())

    toggle_button.onClick = handle_toggle_selected
    remove_button.onClick = handle_remove_selected

    async def handle_message(message):
        if runtime["closed"]:
            return
        content = getattr(message, "content", "") or ""
        webhook_id = getattr(message, "webhook_id", None)
        admit_text(content, "" if webhook_id is None else str(webhook_id), "primary")

    bot.add_listener(handle_message, "on_message")
    runtime["listeners"].append(("on_message", handle_message))

    async def refresh_account_stats():
        while not runtime["closed"]:
            await asyncio.sleep(2)
            push_ui()

    create_tracked_task(notify_worker())
    create_tracked_task(sync_gateways())
    create_tracked_task(refresh_account_stats())
    create_tracked_task(check_update())
    tab.render()
    try:
        callback_store.register(nightysniper_notice_rendered)
    except Exception as error:
        log(f"Could not register the update notice: {type(error).__name__}")


sniper_script()
