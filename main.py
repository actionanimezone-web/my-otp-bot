# Last updated: 2026-07-26 — Changes: BEP-20/Binance rate default 1$=125 BDT; Delete All UIDs admin feature
import os
import csv
import re
import aiohttp
import openpyxl
import random
import string
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path=".env"):
        """Minimal .env loader for hosts where python-dotenv is unavailable."""
        path = Path(dotenv_path)
        if not path.exists():
            return False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
        return True

import motor.motor_asyncio
import pyotp
from faker import Faker
import urllib.parse

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.enums import ContentType
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
    TelegramObject
)
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from typing import Callable, Dict, Any, Awaitable
from io import BytesIO
from contextvars import ContextVar


load_dotenv()


# ---------------------------------------------------------------------------
# PORTABLE CONFIGURATION
# ---------------------------------------------------------------------------
# Keep the primary administrator here. Additional administrators can be
# managed from the bot's Admin Panel and are stored in MongoDB.
PRIMARY_ADMIN_ID = 7040496360
PRIMARY_ADMIN_USERNAME = "@Ariyan_Ahamed_Ari"
MAX_ADMINS = 5

# Personal bot credentials. Replace these two values before running.
HARDCODED_BOT_TOKEN = "8938433828:AAEE-5gezl-9KY5g3cEA_rcSf6U6bCralf8"
HARDCODED_MONGO_URI = "PASTE_YOUR_MONGODB_URI_HERE"


@dataclass(frozen=True)
class Admin:
    telegram_id: int
    username: str


def normalize_username(value: str) -> str:
    return value.strip().lower().removeprefix("@")


ADMINS: list[Admin] = [
    Admin(telegram_id=PRIMARY_ADMIN_ID, username=PRIMARY_ADMIN_USERNAME)
]


def admin_ids() -> tuple[int, ...]:
    return tuple(admin.telegram_id for admin in ADMINS)


def get_admin_by_id(telegram_id: int | str) -> Admin | None:
    try:
        candidate = int(telegram_id)
    except (TypeError, ValueError):
        return None
    return next((admin for admin in ADMINS if admin.telegram_id == candidate), None)


def get_admin_by_username(value: str) -> Admin | None:
    candidate = normalize_username(value)
    if not candidate:
        return None
    return next(
        (admin for admin in ADMINS
         if normalize_username(admin.username) == candidate),
        None,
    )


ALL_ADMIN_PERMISSIONS = [
    "stats",
    "export",
    "verify",
    "broadcast",
    "settings",
    "full_control",
]


def refresh_admin_cache(records: list[dict]) -> None:
    """Load MongoDB-managed admins while keeping the primary admin present."""
    primary_username = PRIMARY_ADMIN_USERNAME
    by_id: dict[int, Admin] = {}
    for record in records:
        try:
            admin_id = int(record["tg_id"])
        except (KeyError, TypeError, ValueError):
            continue
        username = str(record.get("username") or f"@{admin_id}")
        if admin_id == PRIMARY_ADMIN_ID:
            primary_username = username
            continue
        by_id[admin_id] = Admin(admin_id, username)
    by_id = {
        PRIMARY_ADMIN_ID: Admin(PRIMARY_ADMIN_ID, primary_username),
        **by_id,
    }
    ADMINS[:] = list(by_id.values())


def update_admin_cache(
    admin_id: int,
    username: str | None = None,
    remove: bool = False,
) -> None:
    ADMINS[:] = [admin for admin in ADMINS if admin.telegram_id != admin_id]
    if not remove:
        ADMINS.append(Admin(admin_id, username or f"@{admin_id}"))


async def get_system_date(task_type=None):
    if task_type:
        specific_date = await get_setting(f'manual_date_override_{task_type}')
        if specific_date:
            return specific_date
            
    manual_date = await get_setting('manual_date_override')
    if manual_date:
        return manual_date
    return (datetime.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d")

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

BOT_TOKEN = HARDCODED_BOT_TOKEN
MONGO_URI = HARDCODED_MONGO_URI
ADMIN_ID = ADMINS[0].telegram_id

CHANNEL_LINK = "https://t.me/Ariyan_Earning_Shop"
CHANNEL_ID_STR = ""
BOT_USERNAME = "facebook_bazaarbot"   # fallback only — runtime fetches real username
BOT_NAME = "Facebook bazar"

# Cache for the actual bot username fetched at runtime
_BOT_USERNAME_CACHE: str | None = None

async def get_bot_username(bot: Bot) -> str:
    """Return the real bot username, fetching from Telegram API once and caching."""
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    me = await bot.me()
    _BOT_USERNAME_CACHE = me.username
    return _BOT_USERNAME_CACHE

if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Replace HARDCODED_BOT_TOKEN in main.py.")
if not MONGO_URI or MONGO_URI == "PASTE_YOUR_MONGODB_URI_HERE":
    raise RuntimeError("Replace HARDCODED_MONGO_URI in main.py.")
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)


CURRENT_OWNER_ID: ContextVar[int | None] = ContextVar(
    "current_owner_id", default=None
)


def current_owner_id() -> int | None:
    return CURRENT_OWNER_ID.get()


def _add_owner_filter(query: dict | None) -> dict:
    scoped_query = dict(query or {})
    owner_id = current_owner_id()
    if owner_id is not None:
        scoped_query.setdefault("owner_id", owner_id)
    return scoped_query


class TenantCollection:
    """Small Motor collection proxy that applies the current admin scope.

    Every request handler gets an owner context from OwnerContextMiddleware.
    This keeps legacy handlers tenant-safe even when they use direct
    ``db.users``/``db.tasks`` calls.
    """

    def __init__(self, collection, scoped: bool = True):
        self._collection = collection
        self._scoped = scoped

    def __getattr__(self, name):
        return getattr(self._collection, name)

    def _query(self, query):
        return _add_owner_filter(query) if self._scoped else dict(query or {})

    def find(self, *args, **kwargs):
        if self._scoped:
            query = args[0] if args else kwargs.pop("filter", None)
            if not args:
                return self._collection.find(_add_owner_filter(query), **kwargs)
            scoped_args = (_add_owner_filter(query), *args[1:])
            if args:
                return self._collection.find(*scoped_args, **kwargs)
        return self._collection.find(*args, **kwargs)

    async def find_one(self, query=None, *args, **kwargs):
        if self._scoped:
            return await self._collection.find_one(self._query(query), *args, **kwargs)
        return await self._collection.find_one(query, *args, **kwargs)

    async def count_documents(self, query, *args, **kwargs):
        return await self._collection.count_documents(
            self._query(query) if self._scoped else query, *args, **kwargs
        )

    async def distinct(self, key, query=None, *args, **kwargs):
        return await self._collection.distinct(
            key,
            self._query(query) if self._scoped else query,
            *args,
            **kwargs,
        )

    async def insert_one(self, document, *args, **kwargs):
        payload = dict(document)
        owner_id = current_owner_id()
        if self._scoped and owner_id is not None:
            payload.setdefault("owner_id", owner_id)
        return await self._collection.insert_one(payload, *args, **kwargs)

    async def update_one(self, query, update, *args, **kwargs):
        return await self._collection.update_one(
            self._query(query) if self._scoped else query,
            update,
            *args,
            **kwargs,
        )

    async def update_many(self, query, update, *args, **kwargs):
        return await self._collection.update_many(
            self._query(query) if self._scoped else query,
            update,
            *args,
            **kwargs,
        )

    async def delete_one(self, query, *args, **kwargs):
        return await self._collection.delete_one(
            self._query(query) if self._scoped else query, *args, **kwargs
        )

    async def delete_many(self, query, *args, **kwargs):
        return await self._collection.delete_many(
            self._query(query) if self._scoped else query, *args, **kwargs
        )

    def aggregate(self, pipeline, *args, **kwargs):
        if self._scoped:
            owner_id = current_owner_id()
            if owner_id is not None:
                pipeline = [{"$match": {"owner_id": owner_id}}, *pipeline]
        return self._collection.aggregate(pipeline, *args, **kwargs)


class TenantDatabase:
    _unscoped_collections = {"admins"}

    def __init__(self, database):
        self._database = database
        self._collections: dict[str, TenantCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            return getattr(self._database, name)
        if name not in self._collections:
            self._collections[name] = TenantCollection(
                self._database[name],
                scoped=name not in self._unscoped_collections,
            )
        return self._collections[name]


db = TenantDatabase(mongo_client["gotaskpay"])
raw_db = mongo_client["gotaskpay"]


async def get_saved_owner_id(telegram_id: int) -> int | None:
    """Return the immutable owner assigned to a user, if any."""
    doc = await raw_db.users.find_one({"tg_id": telegram_id}, {"owner_id": 1})
    owner_id = doc.get("owner_id") if doc else None
    return owner_id if get_admin_by_id(owner_id) else None


class OwnerContextMiddleware(BaseMiddleware):
    """Resolve the immutable admin owner before any handler touches MongoDB."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)

        owner = get_admin_by_id(user.id)
        if owner is None:
            saved_owner_id = await get_saved_owner_id(user.id)
            owner = get_admin_by_id(saved_owner_id) if saved_owner_id else None

        state = data.get("state")
        current_state = await state.get_state() if state else None
        owner_verification_state = OwnerVerifyStates.waiting_for_username.state
        is_start = isinstance(event, Message) and bool(
            event.text and event.text.startswith("/start")
        )

        if owner is None and not is_start and current_state != owner_verification_state:
            prompt = (
                "🔐 প্রথমবার চালু করতে আপনার Admin-এর username দিন।"
            )
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                    await event.message.answer(prompt)
                else:
                    await event.answer(prompt)
            except Exception:
                pass
            return

        token = CURRENT_OWNER_ID.set(owner.telegram_id if owner else None)
        try:
            return await handler(event, data)
        finally:
            CURRENT_OWNER_ID.reset(token)

CHANNEL_ID = None

class AntiSpamMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.0):
        self.limit = limit
        self.users = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user_id = None
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id
            
        if user_id:
            now = asyncio.get_event_loop().time()
            last_time = self.users.get(user_id, 0)
            if now - last_time < self.limit:
                return # Drop request
            self.users[user_id] = now
            
        return await handler(event, data)

import time

class SessionTimeoutMiddleware(BaseMiddleware):
    def __init__(self, timeout_seconds: int = 3600):
        self.timeout_seconds = timeout_seconds
        self.last_activity = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user_id = None
        state = data.get('state')
        
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id
            
        # Ignore for /start and restart_bot
        if isinstance(event, Message) and event.text and event.text.startswith('/start'):
            if user_id: self.last_activity[user_id] = time.time()
            return await handler(event, data)
            
        if isinstance(event, CallbackQuery) and event.data == "restart_bot":
            if user_id: self.last_activity[user_id] = time.time()
            return await handler(event, data)
            
        if user_id:
            now = time.time()
            last_time = self.last_activity.get(user_id)
            
            if last_time and (now - last_time > self.timeout_seconds):
                self.last_activity[user_id] = now
                if state: await state.clear()
                
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [ibtn("Restart", "restart_bot", "5337132498965010628", "primary")],
                ])
                text = "⏳ **Session Expired!**\n\nYou were inactive for 1 hour. Please click the button below to restart the bot."
                
                try:
                    if isinstance(event, Message):
                        await event.answer(text, reply_markup=kb, parse_mode="Markdown")
                    elif isinstance(event, CallbackQuery):
                        await event.message.answer(text, reply_markup=kb, parse_mode="Markdown")
                        await event.answer()
                except:
                    pass
                return # Block request
                
            self.last_activity[user_id] = now
            
        return await handler(event, data)


try:
    if CHANNEL_ID_STR:
        CHANNEL_ID = int(CHANNEL_ID_STR)
except ValueError:
    pass

fake = Faker()
router = Router()
ADMIN_PERMISSIONS = {}
MAIN_MENU_BUTTONS = {
    "Cancel", "Back", "Back to Main Menu", "বাতিল",
    "Admin Panel", "Balance", "ব্যালেন্স", "Leaderboard", "লিডারবোর্ড",
    "Task", "কাজ", "Withdraw", "উত্তোলন",
    "My Referral", "আমার রেফারেল",
    "How to Work", "কিভাবে কাজ করবেন",
    "Change Language", "ভাষা পরিবর্তন",
    "Contact Support", "My History", "Work",
}

# Premium Emoji Constants
E_INFO = "<tg-emoji emoji-id='4956475826762679249'>💬</tg-emoji>"
E_CHECK = "<tg-emoji emoji-id='4956721670690702265'>✅</tg-emoji>"
E_FIRE = "<tg-emoji emoji-id='4956222745814762495'>🔥</tg-emoji>"
E_WARN = "<tg-emoji emoji-id='4956611513369494230'>⚠️</tg-emoji>"
E_CASH = "<tg-emoji emoji-id='4956601935592424315'>💵</tg-emoji>"
E_TARGET = "<tg-emoji emoji-id='4958506272551863292'>📊</tg-emoji>"
E_USER = "<tg-emoji emoji-id='4958479549265347295'>👤</tg-emoji>"
E_LOCK = "<tg-emoji emoji-id='4958926882994127612'>🔐</tg-emoji>"
E_BULL = "<tg-emoji emoji-id='4956475826762679249'>📢</tg-emoji>"
E_DOWN = "<tg-emoji emoji-id='4958479549265347295'>⬇️</tg-emoji>"

# ---------------------------------------------------------
# PLATFORM PREMIUM EMOJI CONSTANTS
# PE_*  = HTML tg-emoji tags  → শুধু message text-এ (parse_mode="HTML")
# PEB_* = Plain Unicode emoji → keyboard button text-এ
# ---------------------------------------------------------

# Message text emojis (HTML format — parse_mode="HTML" দিয়ে কাজ করে)
PE_FACEBOOK    = "<tg-emoji emoji-id='5334807341109908955'>📘</tg-emoji>"
PE_INSTAGRAM   = "<tg-emoji emoji-id='5334868205091459431'>📸</tg-emoji>"
PE_WHATSAPP    = "<tg-emoji emoji-id='5334759662677957452'>💬</tg-emoji>"
PE_TELEGRAM    = "<tg-emoji emoji-id='5337010556253543833'>✈️</tg-emoji>"
PE_TIKTOK      = "<tg-emoji emoji-id='5339213256001102461'>🎵</tg-emoji>"
PE_SNAPCHAT    = "<tg-emoji emoji-id='5359441366554255082'>👻</tg-emoji>"
PE_TWITTER     = "<tg-emoji emoji-id='5215726959056662534'>🐦</tg-emoji>"
PE_YOUTUBE     = "<tg-emoji emoji-id='5336879280578138635'>📺</tg-emoji>"
PE_DISCORD     = "<tg-emoji emoji-id='5116246243646898866'>🎬</tg-emoji>"
PE_REDDIT      = "<tg-emoji emoji-id='4992421103847604984'>👽</tg-emoji>"
PE_LINKEDIN    = "<tg-emoji emoji-id='6224222994265279792'>💼</tg-emoji>"
PE_VIBER       = "<tg-emoji emoji-id='5463060437572528782'>💜</tg-emoji>"
PE_SIGNAL      = "<tg-emoji emoji-id='5293998404404272267'>💬</tg-emoji>"
PE_ZOOM        = "<tg-emoji emoji-id='5881799193219043268'>📹</tg-emoji>"
PE_SLACK       = "<tg-emoji emoji-id='4994972469040251302'>💻</tg-emoji>"
PE_SKYPE       = "<tg-emoji emoji-id='4992613535562334989'>☎️</tg-emoji>"
PE_NETFLIX     = "<tg-emoji emoji-id='6255738712664050133'>🎥</tg-emoji>"
PE_SPOTIFY     = "<tg-emoji emoji-id='5411392711146095115'>🎵</tg-emoji>"
PE_CHATGPT     = "<tg-emoji emoji-id='5296516998996445955'>🤖</tg-emoji>"
PE_GITHUB      = "<tg-emoji emoji-id='5417836094098007862'>🐙</tg-emoji>"
PE_CANVA       = "<tg-emoji emoji-id='5111661409008092227'>🖌️</tg-emoji>"
PE_UBER        = "<tg-emoji emoji-id='5298715455316303708'>🚗</tg-emoji>"
PE_PAYPAL      = "<tg-emoji emoji-id='5776103539872896061'>💵</tg-emoji>"
PE_GOOGLE      = "<tg-emoji emoji-id='5335010201005231986'>🔍</tg-emoji>"
PE_PINTEREST   = "<tg-emoji emoji-id='5346103513120258857'>📌</tg-emoji>"
PE_TWITCH      = "<tg-emoji emoji-id='5233333563306301418'>🎮</tg-emoji>"
PE_BYBIT       = "<tg-emoji emoji-id='5348372939479751825'>📈</tg-emoji>"
PE_BKASH       = "<tg-emoji emoji-id='5348469219761626211'>🏦</tg-emoji>"
PE_NAGAD       = "<tg-emoji emoji-id='6147706485439730700'>🏦</tg-emoji>"
PE_ROCKET      = "<tg-emoji emoji-id='5346042941196507141'>🚀</tg-emoji>"
PE_BINANCE     = "<tg-emoji emoji-id='5348212415077064131'>💱</tg-emoji>"

# Button text emojis (plain Unicode — keyboard button-এ ব্যবহার করুন)
PEB_FACEBOOK   = "📘"
PEB_INSTAGRAM  = "📸"
PEB_WHATSAPP   = "💬"
PEB_TELEGRAM   = "✈️"
PEB_TIKTOK     = "🎵"
PEB_SNAPCHAT   = "👻"
PEB_TWITTER    = "🐦"
PEB_YOUTUBE    = "📺"
PEB_DISCORD    = "🎬"
PEB_REDDIT     = "👽"
PEB_LINKEDIN   = "💼"
PEB_VIBER      = "💜"
PEB_SIGNAL     = "💬"
PEB_ZOOM       = "📹"
PEB_SLACK      = "💻"
PEB_SKYPE      = "☎️"
PEB_NETFLIX    = "🎥"
PEB_SPOTIFY    = "🎵"
PEB_CHATGPT    = "🤖"
PEB_GITHUB     = "🐙"
PEB_CANVA      = "🖌️"
PEB_UBER       = "🚗"
PEB_PAYPAL     = "💵"
PEB_GOOGLE     = "🔍"
PEB_PINTEREST  = "📌"
PEB_TWITCH     = "🎮"
PEB_BYBIT      = "📈"
PEB_BKASH      = "🏦"
PEB_NAGAD      = "🏦"
PEB_ROCKET     = "🚀"
PEB_BINANCE    = "💱"
    

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------
async def init_db():
    default_settings = {
        'task_price': '10',
        'password_mode': 'random', 
        'fixed_password': 'GoTaskPassword123',
        'submit_deadline': '23:59',
        'min_withdraw_amount': '50',
        'withdraw_status': 'on',
        'refer_percentage': '10',
        'tutorial_video_id': '',
        'tutorial_message': 'How to work tutorial:',
        'tutorial_fb': 'Facebook Tutorial',
        'tutorial_insta': 'Instagram Tutorial',
        'channel_id': '',
        'channel_link': 'https://t.me/your_channel',
        'auto_export_time': '20:00',
        'auto_export_time_fb_2fa': '21:00',
        'auto_export_time_fb_cookie': '22:00',
        'late_export_time': '23:55',
        'late_export_status': 'on',
        'fb_2fa_price': '10',
        'fb_cookie_price': '10',
        'binance_rate': '125'
    }
    
    default_start_message = "🥰 স্বাগতম, <b>{name}</b>\\n\\n✍️কাজ শুরু করতে নিচের অপশনগুলো ব্যবহার করুন👌"

    # Create Indexes for performance
    await db.users.create_index('tg_id', unique=True)
    await db.users.create_index('referred_by')
    await db.tasks.create_index([('tg_id', 1), ('status', 1)])
    await db.tasks.create_index('status')
    await db.tasks.create_index('date')
    await db.tasks.create_index('name')
    existing_setting_indexes = await raw_db.settings.list_indexes().to_list(length=None)
    if any(index.get("name") == "key_1" for index in existing_setting_indexes):
        await raw_db.settings.drop_index("key_1")
    await db.withdraws.create_index([('tg_id', 1), ('status', 1)])
    await db.withdraws.create_index('status')
    existing_uid_indexes = await raw_db.deleted_uids.list_indexes().to_list(length=None)
    if any(index.get("name") == "uid_1" for index in existing_uid_indexes):
        await raw_db.deleted_uids.drop_index("uid_1")

    # Migrate the old single-admin dataset into the primary admin's tenant.
    # New records always receive their owner_id from TenantCollection.
    for collection_name in (
        "users",
        "tasks",
        "withdraws",
        "buyers",
        "settings",
        "tutorials",
        "ocr_keys",
        "deleted_uids",
        "net",
    ):
        await raw_db[collection_name].update_many(
            {"owner_id": {"$exists": False}},
            {"$set": {"owner_id": ADMIN_ID}},
        )
        await raw_db[collection_name].create_index("owner_id")

    await raw_db.settings.create_index([("owner_id", 1), ("key", 1)], unique=True)
    await raw_db.deleted_uids.create_index([("owner_id", 1), ("uid", 1)], unique=True)

    for owner_id in admin_ids():
        for key, value in default_settings.items():
            await raw_db.settings.update_one(
                {"owner_id": owner_id, "key": key},
                {"$setOnInsert": {"owner_id": owner_id, "key": key, "value": value}},
                upsert=True,
            )
        await raw_db.settings.update_one(
            {"owner_id": owner_id, "key": "start_message"},
            {
                "$setOnInsert": {
                    "owner_id": owner_id,
                    "key": "start_message",
                    "value": default_start_message,
                }
            },
            upsert=True,
        )
            
    # Load administrators from MongoDB. The primary admin is always present.
    await raw_db.admins.create_index('tg_id', unique=True)
    await raw_db.admins.update_one(
        {'tg_id': PRIMARY_ADMIN_ID},
        {'$set': {'permissions': ALL_ADMIN_PERMISSIONS},
         '$setOnInsert': {'tg_id': PRIMARY_ADMIN_ID,
                          'username': PRIMARY_ADMIN_USERNAME}},
        upsert=True,
    )
    admin_records = await raw_db.admins.find({}).to_list(length=None)
    refresh_admin_cache(admin_records)
    ADMIN_PERMISSIONS.clear()
    for admin_record in admin_records:
        ADMIN_PERMISSIONS[str(admin_record['tg_id'])] = admin_record.get(
            'permissions', []
        )
    ADMIN_PERMISSIONS[str(PRIMARY_ADMIN_ID)] = ALL_ADMIN_PERMISSIONS.copy()
async def initialize_admin_settings(owner_id: int) -> None:
    default_settings = {
        'task_price': '10', 'password_mode': 'random',
        'fixed_password': 'GoTaskPassword123', 'submit_deadline': '23:59',
        'min_withdraw_amount': '50', 'withdraw_status': 'on',
        'refer_percentage': '10', 'tutorial_video_id': '',
        'tutorial_message': 'How to work tutorial:',
        'tutorial_fb': 'Facebook Tutorial',
        'tutorial_insta': 'Instagram Tutorial', 'channel_id': '',
        'channel_link': 'https://t.me/your_channel',
        'auto_export_time': '20:00',
        'auto_export_time_fb_2fa': '21:00',
        'auto_export_time_fb_cookie': '22:00',
        'late_export_time': '23:55', 'late_export_status': 'on',
        'fb_2fa_price': '10', 'fb_cookie_price': '10',
        'binance_rate': '125',
    }
    for key, value in default_settings.items():
        await raw_db.settings.update_one(
            {'owner_id': owner_id, 'key': key},
            {'$setOnInsert': {'owner_id': owner_id, 'key': key, 'value': value}},
            upsert=True,
        )
    await raw_db.settings.update_one(
        {'owner_id': owner_id, 'key': 'start_message'},
        {'$setOnInsert': {
            'owner_id': owner_id,
            'key': 'start_message',
            'value': '🥰 স্বাগতম, <b>{name}</b>\\n\\n✍️কাজ শুরু করতে নিচের অপশনগুলো ব্যবহার করুন👌',
        }},
        upsert=True,
    )


SETTINGS_CACHE = {}

async def get_setting(key):
    owner_id = current_owner_id() or ADMIN_ID
    cache_key = (owner_id, key)
    if cache_key in SETTINGS_CACHE:
        return SETTINGS_CACHE[cache_key]
    doc = await raw_db.settings.find_one({'owner_id': owner_id, 'key': key})
    if doc:
        SETTINGS_CACHE[cache_key] = doc['value']
        return doc['value']
    return None

async def update_setting(key, value):
    owner_id = current_owner_id() or ADMIN_ID
    await raw_db.settings.update_one(
        {'owner_id': owner_id, 'key': key},
        {'$set': {'value': str(value)}},
        upsert=True,
    )
    SETTINGS_CACHE[(owner_id, key)] = str(value)

async def add_user(tg_id, referred_by=None):
    from datetime import datetime
    existing = await db.users.find_one({'tg_id': tg_id})
    if existing: return False
    await db.users.insert_one({
        'tg_id': tg_id,
        'owner_id': current_owner_id(),
        'balance': 0,
        'referred_by': referred_by,
        'join_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'referral_earnings': 0,
        'lang': 'bn'
    })
    return True

async def get_user(tg_id):
    doc = await db.users.find_one({'tg_id': tg_id})
    if doc:
        return (str(doc.get('_id')), doc.get('tg_id'), doc.get('balance', 0), doc.get('referred_by'), doc.get('join_date'), doc.get('referral_earnings', 0))
    return None

async def get_all_users():
    cursor = db.users.find({}, {'tg_id': 1})
    users = []
    async for doc in cursor:
        users.append(doc['tg_id'])
    return users

async def update_balance(tg_id, amount):
    await db.users.update_one({'tg_id': tg_id}, {'$inc': {'balance': amount}})

async def add_task(tg_id, name, password, two_fa_key, two_fa_code, task_type='insta_2fa'):
    date_str = await get_system_date(task_type)
    await db.tasks.insert_one({
        'tg_id': tg_id, 'name': name, 'password': password, 
        'two_fa_key': two_fa_key, 'two_fa_code': two_fa_code, 
        'date': date_str, 'status': 'pending', 'task_type': task_type
    })

async def get_pending_tasks_count(tg_id):
    return await db.tasks.count_documents({'tg_id': tg_id, 'status': 'pending'})

async def get_completed_tasks_count(tg_id):
    return await db.tasks.count_documents({'tg_id': tg_id, 'status': 'accepted'})

async def export_tasks_xlsx(date_str, task_type='insta_2fa'):
    owner_id = current_owner_id() or ADMIN_ID
    if task_type == 'insta_2fa':
        file_path = f'tasks_insta_{date_str}_admin_{owner_id}.xlsx'
        headers = ['Username', 'Password', '2FA Key']
    elif task_type == 'fb_2fa':
        file_path = f'tasks_fb_2fa_{date_str}_admin_{owner_id}.xlsx'
        headers = ['UID', 'Password', '2FA Key']
    else:
        file_path = f'tasks_fb_cookies_{date_str}_admin_{owner_id}.xlsx'
        headers = ['UID', 'Password', 'Cookies']
    
    # Build query - if date_str is 'all', export all dates
    query = {'task_type': task_type}
    if date_str != 'all':
        query['date'] = date_str
        
    cursor = db.tasks.find(query)
    rows = []
    async for doc in cursor:
        rows.append([doc.get('name'), doc.get('password'), doc.get('two_fa_key')])
        
    if not rows:
        return None, 0
        
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(file_path)
    return file_path, len(rows)

async def export_unexported_tasks_xlsx(task_type='insta_2fa'):
    """Export all tasks that haven't been auto-exported yet (exported != true)"""
    now = datetime.utcnow() + timedelta(hours=6)
    date_str = now.strftime("%Y-%m-%d")
    
    if task_type == 'insta_2fa':
        file_path = f'tasks_insta_new_{date_str}.xlsx'
        headers = ['Username', 'Password', '2FA Key']
    elif task_type == 'fb_2fa':
        file_path = f'tasks_fb_2fa_new_{date_str}.xlsx'
        headers = ['UID', 'Password', '2FA Key']
    else:
        file_path = f'tasks_fb_cookies_new_{date_str}.xlsx'
        headers = ['UID', 'Password', 'Cookies']
    
    # Find all tasks that haven't been exported yet
    query = {'task_type': task_type, 'exported': {'$ne': True}}
    
    cursor = db.tasks.find(query)
    rows = []
    task_ids = []
    async for doc in cursor:
        rows.append([doc.get('name'), doc.get('password'), doc.get('two_fa_key')])
        task_ids.append(doc['_id'])
        
    if not rows:
        return None, 0, []
        
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(file_path)
    return file_path, len(rows), task_ids

async def mark_tasks_exported(task_ids):
    """Mark tasks as exported after successful auto export"""
    if task_ids:
        await db.tasks.update_many(
            {'_id': {'$in': task_ids}},
            {'$set': {'exported': True}}
        )


async def verify_and_credit_accounts(valid_usernames_list, bot: Bot):
    accepted_count = 0
    
    try:
        insta_price = float(await get_setting('task_price'))
    except:
        insta_price = 3.15
    try:
        fb_2fa_price = float(await get_setting('fb_2fa_price'))
    except:
        fb_2fa_price = 3.50
    try:
        fb_cookie_price = float(await get_setting('fb_cookie_price'))
    except:
        fb_cookie_price = 4.50
        
    try:
        ref_percent = float(await get_setting('refer_percentage'))
    except:
        ref_percent = 0
        
    user_earnings = {}
    user_approved_accounts = {}
    
    referrer_earnings = {}
    referrer_tasks = {}
    
    for username in valid_usernames_list:
        # নতুন রিপোর্ট হিসেবে — status যাই থাকুক সবাইকে process করবে
        doc = await db.tasks.find_one({'name': username})
        if doc:
            task_type = doc.get('task_type', 'insta_2fa')
            if task_type == 'fb_2fa':
                price = fb_2fa_price
            elif task_type == 'fb_cookie':
                price = fb_cookie_price
            else:
                price = insta_price
                
            ref_reward = price * (ref_percent / 100)
            
            task_id, tg_id = doc['_id'], doc['tg_id']
            prev_status = doc.get('status', 'pending')
            await db.tasks.update_one({'_id': task_id}, {'$set': {'status': 'accepted'}})
            
            # Balance শুধু তখনই যোগ হবে যদি আগে accepted না থাকে (duplicate credit নেই)
            if prev_status != 'accepted':
                await db.users.update_one({'tg_id': tg_id}, {'$inc': {'balance': price}})
                user_earnings[tg_id] = user_earnings.get(tg_id, 0) + price
            
            if tg_id not in user_approved_accounts:
                user_approved_accounts[tg_id] = []
            user_approved_accounts[tg_id].append(username)
            
            # Referral bonus — শুধু প্রথমবার (pending থেকে) accept হলে দেব
            if prev_status == 'pending':
                user = await db.users.find_one({'tg_id': tg_id})
                if user and user.get('referred_by'):
                    ref_id = user['referred_by']
                    refer_income_status = await get_setting('refer_income_status') or 'on'
                    if refer_income_status == 'on' and ref_reward > 0:
                        await db.users.update_one({'tg_id': ref_id}, {'$inc': {'balance': ref_reward, 'referral_earnings': ref_reward}})
                        referrer_earnings[ref_id] = referrer_earnings.get(ref_id, 0) + ref_reward
                        referrer_tasks[ref_id] = referrer_tasks.get(ref_id, 0) + 1
            accepted_count += 1
        
    for u_tg_id, amount in user_earnings.items():
        try:
            accounts = user_approved_accounts[u_tg_id]
            acc_list_str = "\n".join([f"✅ `{acc}`" for acc in accounts])
            kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("আরও কাজ করুন", "start_task_submenu", "5352877703043258544", "success")]])
            
            text = (
                f"🎉 **অভিনন্দন! আপনার কাজগুলো অ্যাপ্রুভ হয়েছে।**\n\n"
                f"📝 **অ্যাপ্রুভড অ্যাকাউন্টসমূহ ({len(accounts)} টি):**\n"
                f"{acc_list_str}\n\n"
                f"💰 **আপনার ব্যালেন্সে যোগ হয়েছে:** `{amount:.2f} TK`"
            )
            await bot.send_message(chat_id=u_tg_id, text=text, reply_markup=kb, parse_mode="Markdown")
        except: pass

    for ref_id, amount in referrer_earnings.items():
        try:
            total_refs = await db.users.count_documents({'referred_by': ref_id})
            referrer_user = await db.users.find_one({'tg_id': ref_id})
            total_ref_earnings = referrer_user.get('referral_earnings', 0) if referrer_user else 0
            
            task_count = referrer_tasks[ref_id]
            
            text = (
                f"🎉 **রেফারেল বোনাস প্রাপ্তি!**\n\n"
                f"আপনার রেফার করা ইউজাররা কাজ সম্পন্ন করেছে! এর ফলে আপনি বোনাস পেয়েছেন:\n"
                f"🔹 **নতুন বোনাস:** `{amount:.2f} TK`\n"
                f"🔹 **কাজ কমপ্লিট করেছে:** `{task_count}` টি\n\n"
                f"📊 **আপনার সর্বমোট রেফারেল স্ট্যাটাস:**\n"
                f"👥 মোট রেফার করেছেন: `{total_refs}` জন\n"
                f"💰 সর্বমোট রেফার ইনকাম: `{total_ref_earnings:.2f} TK`\n\n"
                f"আরও বেশি ইনকাম করতে বন্ধুদের সাথে আপনার লিংক শেয়ার করুন 👇"
            )
            real_username = await get_bot_username(bot)
            await bot.send_message(chat_id=ref_id, text=text, reply_markup=get_share_and_earn_inline(real_username, ref_id), parse_mode="Markdown")
        except: pass
        
    return accepted_count

async def reject_accounts(bad_usernames_list, bot: Bot):
    rejected_count = 0
    user_rejected_accounts = {}
    
    try:
        insta_price = float(await get_setting('task_price'))
    except:
        insta_price = 3.15
    try:
        fb_2fa_price = float(await get_setting('fb_2fa_price'))
    except:
        fb_2fa_price = 3.50
    try:
        fb_cookie_price = float(await get_setting('fb_cookie_price'))
    except:
        fb_cookie_price = 4.50
    
    for username in bad_usernames_list:
        # নতুন রিপোর্ট হিসেবে — status যাই থাকুক সবাইকে reject করবে
        doc = await db.tasks.find_one({'name': username})
        if doc:
            task_type = doc.get('task_type', 'insta_2fa')
            if task_type == 'fb_2fa':
                price = fb_2fa_price
            elif task_type == 'fb_cookie':
                price = fb_cookie_price
            else:
                price = insta_price
                
            task_id, tg_id = doc['_id'], doc['tg_id']
            prev_status = doc.get('status', 'pending')
            await db.tasks.update_one({'_id': task_id}, {'$set': {'status': 'rejected'}})
            
            # যদি আগে accepted ছিল (balance দেওয়া হয়েছিল), তাহলে balance কেটে নাও
            if prev_status == 'accepted':
                await db.users.update_one({'tg_id': tg_id}, {'$inc': {'balance': -price}})
            
            if prev_status != 'rejected':
                if tg_id not in user_rejected_accounts:
                    user_rejected_accounts[tg_id] = []
                user_rejected_accounts[tg_id].append(username)
                
            rejected_count += 1
            
    for u_tg_id, accounts in user_rejected_accounts.items():
        try:
            acc_list_str = "\n".join([f"❌ `{acc}`" for acc in accounts])
            text = (
                f"⚠️ **অ্যাকাউন্ট বাতিল!**\n\n"
                f"আপনার জমাকৃত নিচের অ্যাকাউন্টগুলো চেক করার সময় ভুল/নষ্ট পাওয়া গেছে, তাই এগুলো বাতিল করা হয়েছে:\n\n"
                f"{acc_list_str}"
            )
            await bot.send_message(chat_id=u_tg_id, text=text, parse_mode="Markdown")
        except: pass
            
    return rejected_count

async def reject_all_pending_accounts(bot: Bot):
    rejected_count = 0
    user_rejected_accounts = {}
    
    cursor = db.tasks.find({'status': 'pending'})
    async for doc in cursor:
        task_id, tg_id, username = doc['_id'], doc.get('tg_id'), doc.get('name')
        await db.tasks.update_one({'_id': task_id}, {'$set': {'status': 'rejected'}})
        
        if tg_id and username:
            if tg_id not in user_rejected_accounts:
                user_rejected_accounts[tg_id] = []
            user_rejected_accounts[tg_id].append(username)
            rejected_count += 1
            
    for u_tg_id, accounts in user_rejected_accounts.items():
        try:
            acc_list_str = "\n".join([f"❌ `{acc}`" for acc in accounts])
            text = (
                f"⚠️ **অ্যাকাউন্ট বাতিল!**\n\n"
                f"আপনার জমাকৃত নিচের অ্যাকাউন্টগুলো চেক করার সময় ভুল/নষ্ট পাওয়া গেছে, তাই এগুলো বাতিল করা হয়েছে:\n\n"
                f"{acc_list_str}"
            )
            await bot.send_message(chat_id=u_tg_id, text=text, parse_mode="Markdown")
        except: pass
            
    return rejected_count

async def add_withdraw_request(tg_id, method, details, requested_amount, fee=5):
    receive_amount = requested_amount - fee
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = await db.withdraws.insert_one({
        'tg_id': tg_id, 'method': method, 'details': details, 
        'amount': receive_amount, 'status': 'pending', 'date': date_str,
        'requested_amount': requested_amount, 'fee': fee
    })
    await db.users.update_one({'tg_id': tg_id}, {'$inc': {'balance': -requested_amount}})
    return str(result.inserted_id)

async def get_withdraw_request(req_id):
    from bson.objectid import ObjectId
    try:
        doc = await db.withdraws.find_one({'_id': ObjectId(req_id)})
        if doc:
            return (str(doc['_id']), doc['tg_id'], doc['method'], doc['details'], doc['amount'], doc['status'], doc['date'])
    except: pass
    return None

async def update_withdraw_status(req_id, status):
    from bson.objectid import ObjectId
    try:
        await db.withdraws.update_one({'_id': ObjectId(req_id)}, {'$set': {'status': status}})
    except: pass

async def get_pending_withdraw_amount(tg_id):
    cursor = db.withdraws.find({'tg_id': tg_id, 'status': 'pending'})
    total = 0
    async for doc in cursor:
        total += doc.get('amount', 0)
    return total

async def get_successful_withdraw_amount(tg_id):
    cursor = db.withdraws.find({'tg_id': tg_id, 'status': 'accepted'})
    total = 0
    async for doc in cursor:
        total += doc.get('amount', 0)
    return total

# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------
def generate_hard_password(day):
    symbols = "@#$%"
    random_chars = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice(string.digits),
        random.choice(symbols),
        random.choice(symbols)
    ]
    random.shuffle(random_chars)
    return "GoTask" + "".join(random_chars) + f"%{day}"

def generate_username():
    name = fake.name()
    username_base = name.lower().replace(' ', '_').replace('.', '')
    return f"{username_base}{random.randint(10, 999)}"

def generate_2fa_code(key):
    try:
        clean_key = key.replace(" ", "").upper()
        totp = pyotp.TOTP(clean_key)
        return totp.now()
    except Exception:
        return None

LANG_CACHE = {}

async def get_user_lang(user_id):
    if user_id in LANG_CACHE:
        return LANG_CACHE[user_id]
    user = await db.users.find_one({'tg_id': user_id})
    lang = user.get('lang', 'bn') if user else 'bn'
    LANG_CACHE[user_id] = lang
    return lang

def is_admin(user_id, permission=None):
    admin = get_admin_by_id(user_id)
    if admin is None:
        return False
    if admin.telegram_id == PRIMARY_ADMIN_ID or permission is None:
        return True
    return permission in ADMIN_PERMISSIONS.get(str(user_id), [])


def is_primary_admin(user_id: int | str) -> bool:
    return str(user_id) == str(ADMIN_ID)

MEMBERSHIP_CACHE = {}

async def check_channel_membership(bot: Bot, user_id: int):
    if is_admin(user_id):
        return True
        
    import time
    now = time.time()
    # If checked in the last 5 minutes and was a member, return True
    if user_id in MEMBERSHIP_CACHE and MEMBERSHIP_CACHE[user_id] > now:
        return True
    
    channel_id = await get_setting('channel_id')
    import os
        
    main_joined = True
    if channel_id:
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator', 'Member', 'Administrator', 'Creator']:
                main_joined = False
        except Exception as e:
            print(f"Error checking main channel membership: {e}")
            main_joined = True
            
    payout_id = await get_setting('payout_group_id')
    payout_joined = True
    if payout_id:
        try:
            member = await bot.get_chat_member(chat_id=payout_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator', 'Member', 'Administrator', 'Creator']:
                payout_joined = False
        except Exception as e:
            print(f"Error checking payout group membership: {e}")
            payout_joined = True
            
    result = main_joined and payout_joined
    if result:
        MEMBERSHIP_CACHE[user_id] = now + 300 # cache for 5 minutes if joined
    else:
        MEMBERSHIP_CACHE[user_id] = now + 10 # cache for 10 seconds if not joined (prevents spamming API)
        
    return result

async def check_submit_deadline():
    deadline = await get_setting('submit_deadline')
    if not deadline: return True
    now = datetime.now()
    try:
        deadline_time = datetime.strptime(deadline, "%H:%M").time()
        if now.time() > deadline_time:
            return False
    except ValueError:
        pass
    return True

# ---------------------------------------------------------
# STATES
# ---------------------------------------------------------
class FlowStates(StatesGroup):
    waiting_for_task_platform = State()
    waiting_for_tutorial_platform = State()
    in_fb_menu = State()
    in_insta_menu = State()


class OwnerVerifyStates(StatesGroup):
    waiting_for_username = State()

class TaskStates(StatesGroup):
    waiting_for_2fa_key = State()
    waiting_for_submit = State()
    waiting_for_screenshot = State()

class FBTaskStates(StatesGroup):
    waiting_for_fb_uid_2fa = State()
    waiting_for_fb_uid_cookie = State()
    waiting_for_fb_2fa_key = State()
    waiting_for_fb_cookie = State()
    waiting_for_fb_submit = State()

class AdminManagementStates(StatesGroup):
    waiting_for_admin_id = State()
    waiting_for_edit_admin_id = State()
    waiting_for_edit_admin_username = State()
    waiting_for_admin_username = State()
    waiting_for_admin_permissions = State()
    waiting_for_remove_admin_id = State()

class AdminBuyerStates(StatesGroup):
    waiting_for_buyer_id = State()
    waiting_for_remove_buyer_id = State()
    buyer_id_temp = State()

class AdminUserStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_balance_amount = State()

class AdminExportStates(StatesGroup):
    waiting_for_auto_export_insta = State()
    waiting_for_auto_export_fb_2fa = State()
    waiting_for_auto_export_fb_cookie = State()
    waiting_for_late_export_time = State()

class AdminOCRStates(StatesGroup):
    waiting_for_ocr_key = State()
    waiting_for_remove_ocr_key = State()

class AdminSettingsStates(StatesGroup):
    waiting_for_join_bonus_amount = State()
    waiting_for_refer_percent = State()
    waiting_for_tutorial_video = State()
    waiting_for_tutorial_msg = State()
    waiting_for_channel_id = State()
    waiting_for_channel_link = State()
    waiting_for_export_time = State()
    waiting_for_support_link = State()
    waiting_for_day_reset_time = State()
    waiting_for_manual_date = State()
    waiting_for_payout_channel = State()
    waiting_for_video_link = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast_msg = State()
    waiting_for_txt_file = State()
    waiting_for_withdraw_screenshot = State()
    waiting_for_new_price = State()
    waiting_for_start_msg = State()
    waiting_for_fixed_password = State()
    waiting_for_min_withdraw = State()
    waiting_for_binance_rate = State()

class AdminVerifyStates(StatesGroup):
    waiting_for_input = State()

class AdminDeleteUIDStates(StatesGroup):
    waiting_for_confirm = State()
    waiting_for_uids    = State()

class WithdrawStates(StatesGroup):
    waiting_for_method = State()
    waiting_for_details = State()
    waiting_for_amount = State()

# ---------------------------------------------------------
# KEYBOARDS
# ---------------------------------------------------------

# ─── Reply Keyboard Button Helper ───────────────────────────────────────────
def btn(text: str, emoji_id: str = None, style: str = "primary") -> KeyboardButton:
    """Create a styled Reply Keyboard Button with optional custom emoji."""
    kwargs = {"text": text, "style": style}
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    return KeyboardButton(**kwargs)

# ─── Inline Keyboard Button Helper ──────────────────────────────────────────
def ibtn(text: str, callback: str = None, emoji_id: str = None,
         style: str = "primary", url: str = None) -> InlineKeyboardButton:
    """Create a styled Inline Keyboard Button with optional custom emoji."""
    kwargs = {"text": text, "style": style}
    if callback:
        kwargs["callback_data"] = callback
    if url:
        kwargs["url"] = url
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**kwargs)


def get_main_keyboard(admin_access=False, lang='bn'):
    if lang == 'en':
        kb = [
            [btn("Task",           "5197269100878907942", "success"),
             btn("Balance",        "6267068789146260253", "primary")],
            [btn("Withdraw",       "6278480525841667186", "danger"),
             btn("How to Work",    "5330032308139343696", "primary")],
            [btn("My Referral",    "5420396762189831222", "success"),
             btn("Leaderboard",    "5352877703043258544", "success")],
            [btn("Change Language","5391232134658667509", "primary")],
        ]
    else:
        kb = [
            [btn("কাজ",            "5197269100878907942", "success"),
             btn("ব্যালেন্স",       "6267068789146260253", "primary")],
            [btn("উত্তোলন",        "6278480525841667186", "danger"),
             btn("কিভাবে কাজ করবেন","5330032308139343696", "primary")],
            [btn("আমার রেফারেল",   "5420396762189831222", "success"),
             btn("লিডারবোর্ড",     "5352877703043258544", "success")],
            [btn("ভাষা পরিবর্তন",  "5391232134658667509", "primary")],
        ]
    if admin_access:
        kb.append([btn("Admin Panel", "5420155432272438703", "danger")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    
def get_language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("বাংলা (Bangla)", "set_lang_bn", "5911365056594973179", "success")],
        [ibtn("English",         "set_lang_en", "5913463998522592692", "primary")],
    ])

async def get_platform_keyboard():
    insta_status = await get_setting('task_status_insta_2fa') or 'on'
    fb_2fa_status = await get_setting('task_status_fb_2fa') or 'on'
    fb_cookie_status = await get_setting('task_status_fb_cookie') or 'on'

    has_fb = fb_2fa_status == 'on' or fb_cookie_status == 'on'
    has_insta = insta_status == 'on'

    if not has_fb and not has_insta:
        return None

    kb = []
    if has_fb:
        kb.append([btn("Facebook", "5334807341109908955", "primary")])
    if has_insta:
        kb.append([btn("Instagram", "5334868205091459431", "success")])
    kb.append([btn("বাতিল", "5974083768233760323", "danger")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_fb_tasks_keyboard(fb_2fa_price, fb_cookie_price):
    return ReplyKeyboardMarkup(keyboard=[
        [btn(f"Facebook 2FA ({fb_2fa_price} BDT)",    "5334807341109908955", "primary")],
        [btn(f"Facebook Cookie ({fb_cookie_price} BDT)", "5334807341109908955", "primary")],
        [btn("বাতিল",                           "5974083768233760323", "danger")],
    ], resize_keyboard=True)

def get_insta_tasks_keyboard(insta_price):
    return ReplyKeyboardMarkup(keyboard=[
        [btn(f"Instagram 2FA ({insta_price} BDT)", "5334868205091459431", "primary")],
        [btn("বাতিল",                                              "5974083768233760323", "danger")],
    ], resize_keyboard=True)

def get_admin_tutorial_platform():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Facebook Tutorial",  "set_tut_fb",    "5334807341109908955", "primary")],
        [ibtn("Instagram Tutorial","set_tut_insta", "5334868205091459431", "primary")],
    ])



def get_task_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [btn("Done",   "6267008582294705964", "success")],
        [btn("Cancel", "5974083768233760323", "danger")],
    ], resize_keyboard=True)

def get_submit_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [btn("Complete and Submit", "5463060437572528782", "success")],
        [btn("Cancel",              "5974083768233760323", "danger")],
    ], resize_keyboard=True)

def get_submit_uid_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [btn("Submit UID", "5337132498965010628", "primary")],
        [btn("Cancel",     "5974083768233760323", "danger")],
    ], resize_keyboard=True)

def get_permissions_keyboard(current_perms):
    perms_map = {
        'stats': 'Dashboard / Stats',
        'export': 'Export Data',
        'verify': 'Verify & Withdraw',
        'broadcast': 'Broadcast',
        'settings': 'Settings',
        'full_control': 'Full Control (Primary)'
    }
    buttons = []
    for k, v in perms_map.items():
        check = "✅ " if k in current_perms else ""
        _style = "success" if k in current_perms else "default"
        buttons.append([ibtn(f"{check}{v}", f"toggle_perm_{k}", style=_style)])
    buttons.append([ibtn("Save", "save_admin_perms", "5463060437572528782", "success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_buyer_file_types_keyboard(current_types):
    types_map = {
        'insta_2fa': f'{PEB_INSTAGRAM} Insta 2FA',
        'fb_2fa': f'{PEB_FACEBOOK} FB 2FA',
        'fb_cookie': f'{PEB_FACEBOOK} FB Cookie'
    }
    buttons = []
    for k, v in types_map.items():
        check = "✅ " if k in current_types else "❌ "
        _style = "success" if k in current_types else "danger"
        buttons.append([ibtn(f"{check}{v}", f"toggle_buyer_type_{k}", style=_style)])
    buttons.append([ibtn("Save & Confirm", "save_buyer", "5463060437572528782", "success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_task_submenu_keyboard(insta_price, fb_2fa_price, fb_cookie_price):
    st_insta = await get_setting('task_status_insta_2fa') or 'on'
    st_fb2fa = await get_setting('task_status_fb_2fa') or 'on'
    st_fbc = await get_setting('task_status_fb_cookie') or 'on'

    kb = []
    if st_insta == 'on':
        kb.append([btn("Instagram 2FA",    "5334868205091459431", "success")])
    if st_fb2fa == 'on':
        kb.append([btn("Facebook 2FA",      "5334807341109908955", "primary")])
    if st_fbc == 'on':
        kb.append([btn("Facebook Cookie",   "5334807341109908955", "primary")])

    kb.append([btn("বাতিল",           "5974083768233760323", "danger")])

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [btn("Cancel", "5974083768233760323", "danger")],
    ], resize_keyboard=True)

async def get_force_join_keyboard(bot: Bot = None):
    kb = []
    
    link = await get_setting('channel_link')
    if not link: link = "https://t.me/your_channel"
    kb.append([ibtn("Join Channel", url=link, emoji_id="5337132498965010628", style="primary")])
    
    if bot:
        payout_id = await get_setting('payout_group_id')
        if payout_id:
            try:
                chat = await bot.get_chat(payout_id)
                if chat.username:
                    payout_link = f"https://t.me/{chat.username}"
                else:
                    payout_link = await bot.export_chat_invite_link(payout_id)
                kb.append([ibtn("Payment Proof Group", url=payout_link, emoji_id="5352877703043258544", style="success")])
            except:
                pass
                
    kb.append([ibtn("Joined", "check_joined", "5463060437572528782", "success")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_withdraw_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Withdrawal", "start_withdraw", "5352585194295564660", "danger")],
    ])

async def get_withdraw_methods():
    bkash_status = await get_setting('pm_bkash') or 'on'
    nagad_status = await get_setting('pm_nagad') or 'on'
    rocket_status = await get_setting('pm_rocket') or 'on'
    binance_status = await get_setting('pm_binance') or 'on'
    bep20_status = await get_setting('pm_bep20') or 'on'
    
    methods = []
    row1 = []
    if bkash_status == 'on':
        row1.append(ibtn("bKash | Charge 5 TK",  "wd_bKash",  "5348469219761626211", "success"))
    if nagad_status == 'on':
        row1.append(ibtn("Nagad | Charge 5 TK",  "wd_Nagad",  "6147706485439730700", "success"))
    if row1: methods.append(row1)
        
    row2 = []
    if rocket_status == 'on':
        row2.append(ibtn("Rocket | Charge 5 TK",   "wd_Rocket",  "5346042941196507141", "primary"))
    if binance_status == 'on':
        row2.append(ibtn("Binance | Charge 5 TK", "wd_Binance", "5348212415077064131", "primary"))
    if row2: methods.append(row2)

    if bep20_status == 'on':
        methods.append([ibtn("BEP20 (USDT) | Charge 5 TK", "wd_BEP20", "5348212415077064131", "primary")])

    methods.append([ibtn("Cancel", "cancel_withdraw", "5974083768233760323", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=methods)

def get_admin_panel_keyboard(user_id):
    kb = []
    row1 = []
    if is_admin(user_id, 'export'):
        row1.append(btn("Export Data",       "5463352748751753567", "primary"))
    if is_admin(user_id, 'verify'):
        row1.append(btn("Verify Accounts",   "5463060437572528782", "success"))
    if row1: kb.append(row1)

    # Delete All (bulk UID removal) — full_control admins only
    if is_admin(user_id, 'full_control') or str(user_id) == str(ADMIN_ID):
        kb.append([btn("Delete All (UIDs)", "5974083768233760323", "danger")])

    row2 = []
    if is_admin(user_id, 'broadcast'):
        row2.append(btn("Broadcast",         "5337132498965010628", "primary"))
    if is_admin(user_id, 'settings'):
        row2.append(btn("Settings",          "5420155432272438703", "primary"))
    if row2: kb.append(row2)

    row_lb = []
    if is_admin(user_id, 'stats'):
        row_lb.append(btn("Leaderboard",     "5352877703043258544", "success"))
    if row_lb: kb.append(row_lb)

    row_wd = []
    if is_admin(user_id, 'full_control') or str(user_id) == str(ADMIN_ID):
        row_wd.append(btn("Pending Withdraws","5352585194295564660", "danger"))
    if row_wd: kb.append(row_wd)

    row3 = []
    if is_primary_admin(user_id):
        row3.append(btn("Manage Admins",     "5420396762189831222", "primary"))
        row3.append(btn("Edit Admin",       "5337132498965010628", "primary"))
        row3.append(btn("Manage Buyers",    "5337132498965010628", "primary"))
    if row3: kb.append(row3)

    row4 = []
    if is_admin(user_id, 'full_control'):
        row4.append(btn("Manage Users",      "5337132498965010628", "primary"))
    if row4: kb.append(row4)

    if str(user_id) != str(ADMIN_ID):
        kb.append([btn("Leave Admin",        "5974083768233760323", "danger")])

    kb.append([btn("Back to Main Menu",      "5420145051336485498", "primary")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def get_tasks_toggle_inline():
    st_insta = await get_setting('task_status_insta_2fa') or 'on'
    st_fb2fa = await get_setting('task_status_fb_2fa') or 'on'
    st_fbc = await get_setting('task_status_fb_cookie') or 'on'

    def _toggle_style(s): return "success" if s == 'on' else "danger"
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn(f"Insta 2FA: {' ON' if st_insta == 'on' else ' OFF'}",
              "toggle_tsk_insta_2fa", "5334868205091459431", _toggle_style(st_insta))],
        [ibtn(f"FB 2FA: {' ON' if st_fb2fa == 'on' else ' OFF'}",
              "toggle_tsk_fb_2fa",   "5334807341109908955", _toggle_style(st_fb2fa))],
        [ibtn(f"FB Cookie: {' ON' if st_fbc == 'on' else ' OFF'}",
              "toggle_tsk_fb_cookie","5334807341109908955", _toggle_style(st_fbc))],
    ])

def get_verify_action_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Approve (OK)",              "verify_action_approve", "5463060437572528782", "success")],
        [ibtn("Reject (BAD)",              "verify_action_reject",  "5974083768233760323", "danger")],
        [ibtn("Both (CSV)",               "verify_action_both",    "5267421176841398765", "primary")],
        [ibtn("Approve OK & Auto Reject Rest","verify_action_auto","5337132498965010628", "primary")],
    ])

def get_settings_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [btn("Change Insta Price",         "5267421176841398765", "primary"),
         btn("Change FB 2FA Price",         "5267421176841398765", "primary")],
        [btn("Change FB Cookie Price",      "5267421176841398765", "primary"),
         btn("Change Refer %",              "5337132498965010628", "success")],
        [btn("Set Tutorial",                "5337132498965010628", "primary"),
         btn("Set Channel Link",            "5337132498965010628", "primary")],
        [btn("Set Channel ID",              "5337132498965010628", "primary"),
         btn("Change Start Msg",            "5337132498965010628", "primary")],
        [btn("Toggle Withdraw",             "5352585194295564660", "danger"),
         btn("Change Min Withdraw",         "5352877703043258544", "primary")],
        [btn("Toggle Payment Methods",      "5352585194295564660", "primary"),
         btn("Toggle W/D Force Join",       "5337132498965010628", "primary")],
        [btn("Set Password Mode",           "5267421176841398765", "primary"),
         btn("Change Binance Rate",         "5267421176841398765", "primary")],
        [btn("Auto Export (Insta)",         "5337132498965010628", "primary"),
         btn("Auto Export (FB 2FA)",        "5337132498965010628", "primary")],
        [btn("Auto Export (Cookie)",        "5337132498965010628", "primary"),
         btn("Set Support Link",            "5420145051336485498", "primary")],
        [btn("কাজের ভিডিও",                 "5337132498965010628", "success")],
        [btn("Set Date",                    "5337132498965010628", "primary"),
         btn("Toggle Join Bonus",           "5337132498965010628", "success")],
        [btn("Change Join Bonus",           "5352877703043258544", "primary"),
         btn("Toggle Refer Income",         "5420396762189831222", "primary")],
        [btn("Set Payout Group",            "5337132498965010628", "primary"),
         btn("Toggle Tasks",               "5352877703043258544", "success")],
        [btn("Manage OCR API Keys",         "5420155432272438703", "primary"),
         btn("Toggle Insta Screenshot",     "5337132498965010628", "primary")],
        [btn("Back to Admin Panel",         "5420145051336485498", "primary")],
    ], resize_keyboard=True)

def get_password_mode_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Random (Hard)", "set_pw_random", "5267421176841398765", "primary")],
        [ibtn("Fixed",         "set_pw_fixed",  "5337132498965010628", "success")],
    ])

def get_withdraw_approve_keyboard(req_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Approve", f"wd_approve_{req_id}_{user_id}", "5463060437572528782", "success")],
        [ibtn("Reject",  f"wd_reject_{req_id}_{user_id}",  "5974083768233760323", "danger")],
    ])

def get_share_and_earn_inline(bot_user, tg_id):
    share_msg = "🔥 ঘরে বসেই Income করুন!\n\nএখানে আপনি প্রতিদিন আনলিমিটেড **Instagram 2FA Account** সেল করে দারুণ ইনকাম করতে পারবেন। 💯\n\n👇 নিচের লিংকে ক্লিক করে এখনই জয়েন করুন:"
    encoded_text = urllib.parse.quote(share_msg)
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_user}?start={tg_id}&text={encoded_text}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Share and Earn", url=share_url, emoji_id="5420396762189831222", style="success")],
    ])

def get_broadcast_preview_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Send Broadcast", "send_broadcast",    "5337132498965010628", "success")],
        [ibtn("Cancel",          "cancel_broadcast",  "5974083768233760323", "danger")],
    ])

# ---------------------------------------------------------
# USER HANDLERS
# ---------------------------------------------------------
@router.message(F.forward_from_chat)
async def get_forwarded_chat_id(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(f"Channel/Chat ID: `{message.forward_from_chat.id}`\n\nCopy this and set it as CHANNEL_ID in your .env file.", parse_mode="Markdown")



async def process_join_bonus(user_id: int, bot: Bot):
    status = await get_setting('join_bonus_status') or 'off'
    if status != 'on':
        return
        
    amount = await get_setting('join_bonus_amount')
    if not amount:
        return
    amount = float(amount)
        
    user = await db.users.find_one({'tg_id': user_id})
    if user and not user.get('join_bonus_received'):
        await db.users.update_one({'tg_id': user_id}, {'$inc': {'balance': amount}, '$set': {'join_bonus_received': True}})
        try:
            await bot.send_message(user_id, f"🎉 **Congratulations!**\n\nYou received a Joining Bonus of **{amount} ৳** for joining our channel!", parse_mode="Markdown")
        except:
            pass

@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    pending_ref = data.get('referred_by')
    await state.clear()
    try:
        if get_admin_by_id(message.from_user.id) is None:
            saved_owner_id = await get_saved_owner_id(message.from_user.id)
            if saved_owner_id is None:
                await message.answer(
                    "🔐 প্রথমবার চালু করতে আপনার Admin-এর username দিন।"
                )
                if pending_ref:
                    await state.update_data(referred_by=pending_ref)
                await state.set_state(OwnerVerifyStates.waiting_for_username)
                return

        args = message.text.split()[1] if len(message.text.split()) > 1 else None
        referred_by = int(args) if args and args.isdigit() else pending_ref
        
        if not await check_channel_membership(bot, message.from_user.id):
            if referred_by:
                await state.update_data(referred_by=referred_by)
            return await message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))

        is_new = await add_user(message.from_user.id, referred_by)
        if is_new and referred_by:
            try:
                total_refs = await db.users.count_documents({'referred_by': referred_by})
                        
                share_msg = "🔥 ঘরে বসেই Income করুন!\n\nএখানে আপনি প্রতিদিন আনলিমিটেড **Instagram 2FA Account** সেল করে দারুণ ইনকাম করতে পারবেন। 💯\n\n👇 নিচের লিংকে ক্লিক করে এখনই জয়েন করুন:"
                encoded_text = urllib.parse.quote(share_msg)
                real_username = await get_bot_username(bot)
                url = f"https://t.me/share/url?url=https://t.me/{real_username}?start={referred_by}&text={encoded_text}"
                kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Share More & Earn More", url=url, emoji_id="5420396762189831222", style="success")]])
                
                await bot.send_message(
                    chat_id=referred_by, 
                    text=f"🎉 **নতুন রেফারেল!**\n\n👤 **{message.from_user.first_name}** আপনার রেফারেল লিংক ব্যবহার করে জয়েন করেছেন!\n👥 **মোট রেফারেল:** `{total_refs}` জন", 
                    reply_markup=kb, 
                    parse_mode="Markdown"
                )
            except: pass

        start_msg = await get_setting('start_message')
        full_name = message.from_user.full_name
        final_msg = start_msg.replace('{name}', full_name).replace('\\n', '\n')
        try:
            await message.answer(final_msg, reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)), parse_mode="HTML")
        except Exception:
            await message.answer(final_msg, reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))
        await process_join_bonus(message.from_user.id, bot)
        
        if args == "work":
            await state.clear()
            await state.set_state(FlowStates.waiting_for_task_platform)
            lang = await get_user_lang(message.from_user.id)
            text = f"<tg-emoji emoji-id='4958506272551863292'>📊</tg-emoji> <b>কোন প্লাটফর্মের কাজ করতে চান?</b>" if lang == 'bn' else f"<tg-emoji emoji-id='4958506272551863292'>📊</tg-emoji> <b>Which platform's tasks do you want to do?</b>"
            kb = await get_platform_keyboard()
            if kb:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer("⚠️ বর্তমানে কোনো কাজ এভেইলেবল নেই। (No tasks available right now)", reply_markup=get_main_keyboard(is_admin(message.from_user.id), lang), parse_mode="HTML")
        elif args == "ref":
            total_refs = await db.users.count_documents({'referred_by': message.from_user.id})
            user = await get_user(message.from_user.id)
            ref_earnings = user[5] if user and len(user) > 5 and user[5] is not None else 0
            ref_percent = await get_setting('refer_percentage')
            text = (
                f"<tg-emoji emoji-id='6109486907207455487'>🔥</tg-emoji> <b>Refer & Earn Program</b> <tg-emoji emoji-id='6111500783012810838'>🌟</tg-emoji>\n\n"
                f"Invite your friends and earn <b>{ref_percent}%</b> of their task earnings forever! <tg-emoji emoji-id='6109188248066593101'>💰</tg-emoji>\n\n"
                f"📊 <b>Your Statistics:</b>\n"
                f"<tg-emoji emoji-id='6109572166603247368'>📣</tg-emoji> <b>Total Referrals:</b> <code>{total_refs}</code> friends\n"
                f"<tg-emoji emoji-id='6109394509576015826'>🎁</tg-emoji> <b>Total Earned:</b> <code>{ref_earnings} TK</code>\n\n"
                f"<tg-emoji emoji-id='6109636449378767561'>➡️</tg-emoji> Click the button below to share your link instantly!"
            )
            real_username = await get_bot_username(bot)
            await message.answer(text, reply_markup=get_share_and_earn_inline(real_username, message.from_user.id), parse_mode="HTML")
    except Exception as e:
        import traceback
        await message.answer(f"DEBUG ERROR in /start:\\n{str(e)}\\n{traceback.format_exc()}")


@router.message(OwnerVerifyStates.waiting_for_username)
async def verify_owner_username(message: Message, bot: Bot, state: FSMContext):
    owner = get_admin_by_username(message.text or "")
    if owner is None:
        return await message.answer(
            "❌ Username সঠিক নয়। অনুগ্রহ করে সঠিক Admin-এর username দিন।"
        )

    data = await state.get_data()
    existing_owner_id = await get_saved_owner_id(message.from_user.id)
    if existing_owner_id is not None and existing_owner_id != owner.telegram_id:
        return await message.answer(
            "❌ আপনার account আগে থেকেই অন্য একজন Admin-এর সাথে যুক্ত আছে। "
            "Admin পরিবর্তন করা যাবে না।"
        )
    # Bind the Telegram user exactly once. Future requests use this immutable
    # owner_id and never ask the user to choose an admin again.
    await raw_db.users.update_one(
        {"tg_id": message.from_user.id},
        {
            "$setOnInsert": {
                "owner_id": owner.telegram_id,
                "tg_id": message.from_user.id,
                "balance": 0,
                "referred_by": data.get("referred_by"),
                "join_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "referral_earnings": 0,
                "lang": "bn",
            }
        },
        upsert=True,
    )
    token = CURRENT_OWNER_ID.set(owner.telegram_id)
    try:
        await state.clear()
        if data.get("referred_by"):
            await state.update_data(referred_by=data["referred_by"])
        await cmd_start(message, bot, state)
    finally:
        CURRENT_OWNER_ID.reset(token)


@router.callback_query(F.data == "restart_bot")
async def cb_restart_bot(callback: CallbackQuery, bot: Bot):
    await callback.message.delete()
    
    # Simulate /start behavior without referring to the original message object which is deleted
    if not await check_channel_membership(bot, callback.from_user.id):
        return await callback.message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))

    start_msg = await get_setting('start_message')
    full_name = callback.from_user.full_name
    final_msg = start_msg.replace('{name}', full_name).replace('\\n', '\n')
    try:
        await callback.message.answer(final_msg, reply_markup=get_main_keyboard(is_admin(callback.from_user.id), await get_user_lang(callback.from_user.id)), parse_mode="HTML")
    except Exception:
        await callback.message.answer(final_msg, reply_markup=get_main_keyboard(is_admin(callback.from_user.id), await get_user_lang(callback.from_user.id)))
    await process_join_bonus(callback.from_user.id, bot)

@router.callback_query(F.data == "check_joined")
async def cb_check_joined(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if await check_channel_membership(bot, callback.from_user.id):
        await callback.message.delete()
        
        data = await state.get_data()
        referred_by = data.get('referred_by')
        
        is_new = await add_user(callback.from_user.id, referred_by)
        if is_new and referred_by:
            try:
                total_refs = await db.users.count_documents({'referred_by': referred_by})
                        
                share_msg = "🔥 ঘরে বসেই Income করুন!\n\nএখানে আপনি প্রতিদিন আনলিমিটেড **Instagram 2FA Account** সেল করে দারুণ ইনকাম করতে পারবেন। 💯\n\n👇 নিচের লিংকে ক্লিক করে এখনই জয়েন করুন:"
                encoded_text = urllib.parse.quote(share_msg)
                real_username = await get_bot_username(bot)
                url = f"https://t.me/share/url?url=https://t.me/{real_username}?start={referred_by}&text={encoded_text}"
                kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Share More & Earn More", url=url, emoji_id="5420396762189831222", style="success")]])
                
                await bot.send_message(
                    chat_id=referred_by, 
                    text=f"🎉 **নতুন রেফারেল!**\n\n👤 **{callback.from_user.first_name}** আপনার রেফারেল লিংক ব্যবহার করে জয়েন করেছেন!\n👥 **মোট রেফারেল:** `{total_refs}` জন", 
                    reply_markup=kb, 
                    parse_mode="Markdown"
                )
            except: pass
            
        await state.update_data(referred_by=None)

        start_msg = await get_setting('start_message')
        full_name = callback.from_user.full_name
        final_msg = start_msg.replace('{name}', full_name).replace('\\n', '\n')
        try:
            await callback.message.answer(final_msg, reply_markup=get_main_keyboard(is_admin(callback.from_user.id), await get_user_lang(callback.from_user.id)), parse_mode="HTML")
        except Exception:
            await callback.message.answer(final_msg, reply_markup=get_main_keyboard(is_admin(callback.from_user.id), await get_user_lang(callback.from_user.id)))
        await process_join_bonus(callback.from_user.id, bot)
    else:
        await callback.answer("⚠️ আপনি এখনো চ্যানেলে Join করেননি!", show_alert=True)

@router.message(F.text.in_({"ব্যালেন্স", "Balance"}), StateFilter("*"))
async def cmd_balance(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await check_channel_membership(bot, message.from_user.id):
        return await message.answer("⚠️ You must join our channel first!", reply_markup=await get_force_join_keyboard(bot))

    try:
        user = await get_user(message.from_user.id)
        if not user:
            return await message.answer("⚠️ আপনার অ্যাকাউন্ট পাওয়া যায়নি। অনুগ্রহ করে /start চাপুন।")
            
        insta_pending = await db.tasks.count_documents({'tg_id': message.from_user.id, 'status': 'pending', 'task_type': 'insta_2fa'})
        fb_2fa_pending = await db.tasks.count_documents({'tg_id': message.from_user.id, 'status': 'pending', 'task_type': 'fb_2fa'})
        fb_cookie_pending = await db.tasks.count_documents({'tg_id': message.from_user.id, 'status': 'pending', 'task_type': 'fb_cookie'})
        pending_count = insta_pending + fb_2fa_pending + fb_cookie_pending
        
        pending_withdraw = await get_pending_withdraw_amount(message.from_user.id)
        success_withdraw = await get_successful_withdraw_amount(message.from_user.id)
        
        try:
            insta_price = float(await get_setting('task_price'))
        except:
            insta_price = 3.15
        try:
            fb_2fa_price = float(await get_setting('fb_2fa_price'))
        except:
            fb_2fa_price = 3.50
        try:
            fb_cookie_price = float(await get_setting('fb_cookie_price'))
        except:
            fb_cookie_price = 4.50
            
        processing_amount = (insta_pending * insta_price) + (fb_2fa_pending * fb_2fa_price) + (fb_cookie_pending * fb_cookie_price)
            
        try:
            min_withdraw = float(await get_setting('min_withdraw_amount'))
        except:
            min_withdraw = 50.0
            
        pending_wd_text = f"\n<tg-emoji emoji-id='4956222745814762495'>🔥</tg-emoji> <b>Pending Withdraw:</b> <code>{pending_withdraw} TK</code>" if pending_withdraw > 0 else ""
        success_wd_text = f"\n<tg-emoji emoji-id='4956721670690702265'>✅</tg-emoji> <b>Successful Withdraw:</b> <code>{success_withdraw} TK</code>" if success_withdraw > 0 else ""

        # USDT equivalent of current balance
        try:
            binance_rate = float(await get_setting('binance_rate'))
            if binance_rate < 10:
                binance_rate = 125.0
        except:
            binance_rate = 125.0
        balance_tk   = user[2]
        balance_usdt = balance_tk / binance_rate        # raw USDT value of balance
        # BEP-20 withdrawable USDT (after 0.05$ fee, only if positive)
        wd_usdt_bep20 = max(0.0, balance_usdt - 0.05)
        # Binance Pay withdrawable USD
        wd_usd_binance = balance_usdt

        lang = await get_user_lang(message.from_user.id)
        if lang == 'en':
            text = (
                f"<tg-emoji emoji-id='4958926882994127612'>🏦</tg-emoji> <b>Your Account Balance</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"<tg-emoji emoji-id='4956601935592424315'>💵</tg-emoji> <b>Current Balance:</b> <code>{balance_tk:.2f} TK</code>\n"
                f"<tg-emoji emoji-id='5348212415077064131'>💱</tg-emoji> <b>In USDT (Rate {binance_rate:.0f} TK/$):</b> <code>≈ {balance_usdt:.4f} USDT</code>\n\n"
                f"<tg-emoji emoji-id='4956475826762679249'>💡</tg-emoji> <b>Withdrawable:</b>\n"
                f"  • Binance Pay → <code>{wd_usd_binance:.2f} USD</code>\n"
                f"  • BEP-20 (USDT) → <code>{wd_usdt_bep20:.4f} USDT</code> <i>(after 0.05$ fee)</i>\n\n"
                f"<tg-emoji emoji-id='4958479549265347295'>⚡</tg-emoji> <b>Processing Amount:</b> <code>{processing_amount:.2f} TK</code>\n"
                f"<i>(You have {pending_count} pending tasks)</i>"
                f"{pending_wd_text}"
                f"{success_wd_text}\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"<tg-emoji emoji-id='4956611513369494230'>⚠️</tg-emoji> <b>Min Withdraw:</b> <code>{min_withdraw:.2f} TK</code>\n"
                f"<tg-emoji emoji-id='4956475826762679249'>💡</tg-emoji> <i>Click below to withdraw</i>"
            )
        else:
            text = (
                f"<tg-emoji emoji-id='4958926882994127612'>🏦</tg-emoji> <b>আপনার অ্যাকাউন্ট ব্যালেন্স</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"<tg-emoji emoji-id='4956601935592424315'>💵</tg-emoji> <b>বর্তমান ব্যালেন্স:</b> <code>{balance_tk:.2f} TK</code>\n"
                f"<tg-emoji emoji-id='5348212415077064131'>💱</tg-emoji> <b>USDT তে ({binance_rate:.0f} TK = 1$):</b> <code>≈ {balance_usdt:.4f} USDT</code>\n\n"
                f"<tg-emoji emoji-id='4956475826762679249'>💡</tg-emoji> <b>Withdraw করলে পাবেন:</b>\n"
                f"  • Binance Pay → <code>{wd_usd_binance:.2f} USD</code>\n"
                f"  • BEP-20 (USDT) → <code>{wd_usdt_bep20:.4f} USDT</code> <i>(0.05$ ফি কাটার পর)</i>\n\n"
                f"<tg-emoji emoji-id='4958479549265347295'>⚡</tg-emoji> <b>Processing Amount:</b> <code>{processing_amount:.2f} TK</code>\n"
                f"<i>(আপনার {pending_count} টি কাজ পেন্ডিং আছে)</i>"
                f"{pending_wd_text}"
                f"{success_wd_text}\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"<tg-emoji emoji-id='4956611513369494230'>⚠️</tg-emoji> <b>মিনিমাম উইথড্র:</b> <code>{min_withdraw:.2f} TK</code>\n"
                f"<tg-emoji emoji-id='4956475826762679249'>💡</tg-emoji> <i>টাকা তুলতে নিচের বাটনে ক্লিক করুন</i>"
            )
        await message.answer(text, reply_markup=get_withdraw_inline(), parse_mode="HTML")
    except Exception as e:
        print(f"Balance handler error: {e}")
        await message.answer("⚠️ একটি সমস্যা হয়েছে। দয়া করে আবার চেষ্টা করুন।")


@router.message(F.text.in_({"আমার রেফারেল", "My Referral"}), StateFilter("*"))
async def cmd_refer(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await check_channel_membership(bot, message.from_user.id):
        return await message.answer("⚠️ You must join our channel first!", reply_markup=await get_force_join_keyboard(bot))
    
    total_refs = await db.users.count_documents({'referred_by': message.from_user.id})
    user = await get_user(message.from_user.id)
    ref_earnings = user[5] if len(user) > 5 and user[5] is not None else 0
    ref_percent = await get_setting('refer_percentage')
            
    real_username = await get_bot_username(bot)
    refer_link = f"https://t.me/{real_username}?start={message.from_user.id}"
    text = (
        f"🎁<b>Refer &amp; Earn Program</b>🤑\n\n"
        f"🔗 <b>আপনার রেফার লিংক:</b>⬇️\n"
        f"<code>{refer_link}</code>\n\n"
        f"👤 <b>মোট রেফারেল:</b> {total_refs}\n"
        f"💰 <b>রেফারেল আয়:</b> ৳{ref_earnings:.2f}\n"
        f"💸 <b>কমিশন হার:</b> {ref_percent}%\n\n"
        f"🔔বন্ধুদের invite করুন এবং তাদের প্রতিটি কাজের আয়ের {ref_percent}% চিরকাল পান! 💰"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(F.text.in_({"লিডারবোর্ড", "Leaderboard"}), StateFilter("*"))
async def user_leaderboard(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    lang = await get_user_lang(message.from_user.id)
    if lang == 'en':
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ibtn("Today's Top 10 Workers", "lb_today_workers", "5352877703043258544", "success")],
            [ibtn("Top 10 Referrers",        "lb_top_referrers", "5420396762189831222", "success")],
        ])
        text = "🏆 <b>Leaderboard</b>\n\nWhat do you want to see?"
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ibtn("আজকের টপ ১০ কর্মী", "lb_today_workers", "5352877703043258544", "success")],
            [ibtn("টপ ১০ রেফারার",      "lb_top_referrers", "5420396762189831222", "success")],
        ])
        text = "🏆 <b>লিডারবোর্ড</b>\n\nকী দেখতে চান?"
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "lb_today_workers")
async def lb_today_workers(callback: CallbackQuery, bot: Bot):
    lang = await get_user_lang(callback.from_user.id)
    await callback.answer("⏳ Loading..." if lang == 'en' else "⏳ লোড হচ্ছে...")
    date_str_insta = await get_system_date('insta_2fa')
    date_str_fb    = await get_system_date('fb_2fa')
    date_str_cookie = await get_system_date('fb_cookie')

    pipeline = [
        {'$match': {'$or': [
            {'task_type': 'insta_2fa',  'date': date_str_insta},
            {'task_type': 'fb_2fa',     'date': date_str_fb},
            {'task_type': 'fb_cookie',  'date': date_str_cookie}
        ]}},
        {'$group': {'_id': '$tg_id', 'count': {'$sum': 1}}},
        {'$sort': {'count': -1}},
        {'$limit': 10}
    ]

    workers = []
    async for doc in db.tasks.aggregate(pipeline):
        workers.append(doc)

    if not workers:
        msg = "❌ No tasks submitted today yet." if lang == 'en' else "❌ আজকে এখনো কোনো কাজ জমা হয়নি।"
        return await callback.message.answer(msg)

    medals = ['🥇', '🥈', '🥉']
    lines = []
    for i, w in enumerate(workers):
        tg_id = w['_id']
        count = w['count']
        try:
            chat = await bot.get_chat(tg_id)
            name = (chat.first_name or 'Unknown').replace('<', '&lt;').replace('>', '&gt;')
        except:
            name = 'Unknown'
        rank = medals[i] if i < 3 else f"{i+1}."
        task_label = "tasks" if lang == 'en' else "টি কাজ"
        lines.append(f"{rank} <b>{name}</b> — <code>{count}</code> {task_label}")

    from datetime import datetime, timedelta
    today = (datetime.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d")
    title = "🥇 <b>Today's Top 10 Workers</b>" if lang == 'en' else "🥇 <b>আজকের টপ ১০ কর্মী</b>"
    text = f"{title}\n📅 {today}\n━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines)
    await callback.message.answer(text, parse_mode="HTML")

@router.callback_query(F.data == "lb_top_referrers")
async def lb_top_referrers(callback: CallbackQuery, bot: Bot):
    lang = await get_user_lang(callback.from_user.id)
    await callback.answer("⏳ Loading..." if lang == 'en' else "⏳ লোড হচ্ছে...")

    pipeline = [
        {'$match': {'referred_by': {'$exists': True, '$ne': None}}},
        {'$group': {'_id': '$referred_by', 'count': {'$sum': 1}}},
        {'$sort': {'count': -1}},
        {'$limit': 10}
    ]

    referrers = []
    async for doc in db.users.aggregate(pipeline):
        referrers.append(doc)

    if not referrers:
        msg = "❌ No referrals yet." if lang == 'en' else "❌ এখনো কোনো রেফার হয়নি।"
        return await callback.message.answer(msg)

    medals = ['🥇', '🥈', '🥉']
    lines = []
    for i, r in enumerate(referrers):
        tg_id = r['_id']
        count = r['count']
        try:
            chat = await bot.get_chat(int(tg_id))
            name = (chat.first_name or 'Unknown').replace('<', '&lt;').replace('>', '&gt;')
        except:
            name = 'Unknown'
        rank = medals[i] if i < 3 else f"{i+1}."
        ref_label = "referrals" if lang == 'en' else "জন রেফার"
        lines.append(f"{rank} <b>{name}</b> — <code>{count}</code> {ref_label}")

    title = "🎯 <b>Top 10 Referrers</b>" if lang == 'en' else "🎯 <b>টপ ১০ রেফারার</b>"
    text = f"{title}\n━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines)
    await callback.message.answer(text, parse_mode="HTML")


@router.message(F.text == "Report")
async def cmd_report(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await check_channel_membership(bot, message.from_user.id):
        return await message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))
    pending = await get_pending_tasks_count(message.from_user.id)
    accepted = await get_completed_tasks_count(message.from_user.id)
    text = (
        f"📊 **Work Report (কাজের রিপোর্ট)**\n\n"
        f"⏳ Processing (পেন্ডিং কাজ): {pending}\n"
        f"✅ Completed (অ্যাপ্রুভড কাজ): {accepted}\n\n"
        f"*(বিঃদ্রঃ Failed বা Rejected কাজগুলো এখানে যুক্ত হবে না।)*"
    )
    await message.answer(text, parse_mode="Markdown")


@router.message(F.text.in_({"উত্তোলন", "Withdraw"}), StateFilter("*"))
async def withdraw_menu(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    
    if await get_setting('withdraw_force_join') == 'on':
        if not await check_channel_membership(bot, message.from_user.id):
            return await message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))
    
    user_doc = await db.users.find_one({'tg_id': message.from_user.id})
    if user_doc and user_doc.get('is_banned'):
        return await message.answer("❌ Your account is blocked. You cannot withdraw funds.")
        
    if await get_setting('withdraw_status') == 'off':
        return await message.answer("⚠️ Withdrawals are currently disabled by Admin.")
    user = await get_user(message.from_user.id)
    if not user:
        return await message.answer("⚠️ আপনার অ্যাকাউন্ট পাওয়া যাচ্ছে না। প্রথমে /start চাপুন।")
    min_amount = int(await get_setting('min_withdraw_amount'))
    if user[2] < min_amount:
        return await message.answer(f"⚠️ Minimum withdraw is {min_amount} TK.")
    await message.answer("💳 Select withdrawal method:", reply_markup=await get_withdraw_methods())
    await state.set_state(WithdrawStates.waiting_for_method)

def get_howto_work_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Instagram 2FA কাজের ভিডিও",    "howto_insta_2fa",    "5334868205091459431", "success")],
        [ibtn("Instagram Cookies কাজের ভিডিও","howto_insta_cookie", "5334868205091459431", "success")],
        [ibtn("Facebook 2FA কাজের ভিডিও",      "howto_fb_2fa",       "5334807341109908955", "primary")],
        [ibtn("Facebook Cookies কাজের ভিডিও",  "howto_fb_cookie",    "5334807341109908955", "primary")],
    ])

@router.message(F.text.in_({"কিভাবে কাজ করবেন", "How to Work"}), StateFilter("*"))
async def howto_work_menu(message: Message):
    await message.answer(
        f"{E_INFO} <b>কোন কাজের ভিডিও দেখতে চান? নিচে থেকে বেছে নিন।</b>",
        reply_markup=get_howto_work_inline(),
        parse_mode="HTML"
    )

_HOWTO_NAMES = {
    "howto_insta_2fa":    ("Instagram 2FA",      "video_link_insta_2fa"),
    "howto_insta_cookie": ("Instagram Cookies",  "video_link_insta_cookie"),
    "howto_fb_2fa":       ("Facebook 2FA",       "video_link_fb_2fa"),
    "howto_fb_cookie":    ("Facebook Cookies",   "video_link_fb_cookie"),
}

@router.callback_query(F.data.in_(set(_HOWTO_NAMES.keys())))
async def howto_video_callback(callback: CallbackQuery):
    name, setting_key = _HOWTO_NAMES[callback.data]
    link = await get_setting(setting_key)
    if link:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ibtn("ভিডিও দেখুন", url=link, emoji_id="5337132498965010628", style="success")],
        ])
        await callback.message.answer(
            f"🎥 <b>{name} কাজের ভিডিও দেখে কাজ করুন</b>\n\n{link}",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        await callback.answer("⚠️ এই কাজের ভিডিও এখনো সেট করা হয়নি।", show_alert=True)
    await callback.answer()

@router.message(F.text.in_({"ভাষা পরিবর্তন", "Change Language"}), StateFilter("*"))
async def language_menu(message: Message):
    text = "আপনার ভাষা নির্বাচন করুন:\nSelect your language:"
    await message.answer(text, reply_markup=get_language_keyboard())

@router.callback_query(F.data.startswith("set_lang_"))
async def process_language(callback: CallbackQuery):
    lang = callback.data.split("_")[2]
    await db.users.update_one({'tg_id': callback.from_user.id}, {'$set': {'lang': lang}})
    LANG_CACHE[callback.from_user.id] = lang
    text = "✅ ভাষা সফলভাবে পরিবর্তন করা হয়েছে!" if lang == 'bn' else "✅ Language changed successfully!"
    await callback.message.delete()
    await callback.message.answer(text, reply_markup=get_main_keyboard(is_admin(callback.from_user.id), lang))

# ---------------------------------------------------------
# WITHDRAW HANDLERS

# ---------------------------------------------------------
@router.callback_query(F.data == "start_withdraw")
async def start_withdraw(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    
    if await get_setting('withdraw_force_join') == 'on':
        if not await check_channel_membership(bot, callback.from_user.id):
            return await callback.message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))

    if await get_setting('withdraw_status') == 'off':
        return await callback.answer("⚠️ Withdrawals are currently disabled by Admin.", show_alert=True)
    user = await get_user(callback.from_user.id)
    if not user:
        return await callback.answer("⚠️ User not found. Please restart the bot.", show_alert=True)
    lang = await get_user_lang(callback.from_user.id)
    min_amount = int(await get_setting('min_withdraw_amount'))
    if user[2] < min_amount:
        msg = f"⚠️ Minimum withdraw is {min_amount} TK." if lang == 'en' else f"⚠️ মিনিমাম উইথড্র {min_amount} টাকা।"
        return await callback.answer(msg, show_alert=True)
    msg = f"<tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> <b>Select withdrawal method:</b>" if lang == 'en' else f"<tg-emoji emoji-id='4958926882994127612'>💰</tg-emoji> <b>টাকা তোলার মাধ্যম সিলেক্ট করুন:</b>"
    await callback.message.edit_text(msg, reply_markup=await get_withdraw_methods(), parse_mode="HTML")
    await state.set_state(WithdrawStates.waiting_for_method)

@router.callback_query(F.data == "cancel_withdraw")
async def cb_cancel_withdraw(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("❌ Withdrawal cancelled.", reply_markup=get_main_keyboard(is_admin(callback.from_user.id), await get_user_lang(callback.from_user.id)))

@router.callback_query(WithdrawStates.waiting_for_method, F.data.startswith("wd_"))
async def process_withdraw_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    await state.update_data(method=method)
    await callback.message.delete()
    prompt = (
        "📋 Enter your USDT BEP-20 wallet address:"
        if method == "BEP20"
        else "Enter your Binance Pay ID:"
        if method == "Binance"
        else "Enter account number:"
    )
    await callback.message.answer(f"📝 Selected **{method}**.\n{prompt}", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    await state.set_state(WithdrawStates.waiting_for_details)

@router.message(WithdrawStates.waiting_for_details)
async def process_withdraw_details(message: Message, state: FSMContext):
    if message.text in ["Cancel ❌", "Cancel", "Back 🔙", "Back to Main Menu 🔙", "❌ বাতিল", "বাতিল"]:
        await state.clear()
        lang = await get_user_lang(message.from_user.id)
        msg = "❌ Cancelled." if lang == 'en' else "❌ বাতিল করা হয়েছে।"
        return await message.answer(msg, reply_markup=get_main_keyboard(is_admin(message.from_user.id), lang))
        
    await state.update_data(details=message.text)
    user = await get_user(message.from_user.id)
    balance = user[2] if user and len(user) > 2 else 0.0
    try:
        binance_rate = float(await get_setting('binance_rate'))
        if binance_rate < 10:
            binance_rate = 125.0
    except:
        binance_rate = 125.0
    balance_usdt   = balance / binance_rate
    wd_usdt_bep20  = max(0.0, balance_usdt - 0.05)
    data = await state.get_data()
    method = data.get('method', '')
    if method == 'BEP20':
        usdt_line = f"\n💱 **USDT তে:** `≈ {balance_usdt:.4f} USDT`\n🔖 **Withdraw করলে পাবেন:** `{wd_usdt_bep20:.4f} USDT` _(0.05$ ফি কাটার পর)_"
    elif method == 'Binance':
        usdt_line = f"\n💱 **USD তে:** `≈ {balance_usdt:.2f} USD`"
    else:
        usdt_line = ""
    await message.answer(
        f"💰 Current Balance: **{balance:.2f} TK**{usdt_line}\n\n💵 Enter amount to withdraw (TK):",
        reply_markup=get_cancel_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(WithdrawStates.waiting_for_amount)

@router.message(WithdrawStates.waiting_for_amount)
async def process_withdraw_amount(message: Message, state: FSMContext, bot: Bot):
    if message.text in ["Cancel ❌", "Cancel", "Back 🔙", "Back to Main Menu 🔙", "❌ বাতিল", "বাতিল"]:
        await state.clear()
        lang = await get_user_lang(message.from_user.id)
        msg = "❌ Cancelled." if lang == 'en' else "❌ বাতিল করা হয়েছে।"
        return await message.answer(msg, reply_markup=get_main_keyboard(is_admin(message.from_user.id), lang))

    try:
        amount = float(message.text)
    except ValueError:
        return await message.answer("⚠️ Enter a valid number.")
        
    user = await get_user(message.from_user.id)
    try:
        min_amount = float(await get_setting('min_withdraw_amount'))
    except:
        min_amount = 30.0
        
    if amount < min_amount: return await message.answer(f"⚠️ Min withdraw is {min_amount} TK.")
    if amount > user[2]: return await message.answer(f"⚠️ Not enough balance. You have {user[2]:.2f} TK.")
    
    data = await state.get_data()
    method = data['method']
    
    fee = 0
    if method == "bKash" or method == "Nagad":
        fee = 5
        
    receive_amount = amount - fee
    if receive_amount <= 0: return await message.answer(f"⚠️ Amount is too low. After {fee} TK fee, you receive 0 or less.")
    
    req_id = await add_withdraw_request(message.from_user.id, method, data['details'], amount, fee=fee)
    
    if method == "Binance":
        try:
            binance_rate = float(await get_setting('binance_rate'))
            if binance_rate < 10:
                binance_rate = 125.0
        except:
            binance_rate = 125.0
        receive_text = f"{(amount / binance_rate):.2f} USD (Rate: {binance_rate} TK/$)"
    elif method == "BEP20":
        try:
            binance_rate = float(await get_setting('binance_rate'))
            if binance_rate < 10:
                binance_rate = 125.0
        except:
            binance_rate = 125.0
        usdt_amount = (amount / binance_rate) - 0.05
        if usdt_amount <= 0:
            return await message.answer("⚠️ Amount is too low. After 0.05$ BEP20 fee, you receive 0 or less.")
        receive_text = f"{usdt_amount:.4f} USDT (Rate: {binance_rate} TK/$, Fee: 0.05$)"
    else:
        receive_text = f"{receive_amount} TK"
    
    text = (
        f"✅ **Withdrawal Request Sent!**\n\n"
        f"💰 **Requested:** `{amount} TK`\n"
        f"➖ **Fee:** `{fee} TK`\n"
        f"💵 **You Will Receive:** `{receive_text}`"
    )
    await message.answer(text, parse_mode="Markdown")
    await state.clear()
    
    admin_text = (
        f"🚨 **New Withdrawal Request**\n\n"
        f"👤 User: [{message.from_user.full_name}](tg://user?id={message.from_user.id}) (ID: `{message.from_user.id}`)\n"
        f"💳 Method: {method}\n"
        f"📝 Details: `{data['details']}`\n"
        f"💰 Requested Amount: {amount} TK\n"
        f"💵 Amount to Send: {receive_text} (Fee: {fee})\n"
        f"💵 User's Remaining Balance: {user[2] - amount:.2f} TK"
    )
    try:
        await bot.send_message(chat_id=current_owner_id() or ADMIN_ID, text=admin_text, reply_markup=get_withdraw_approve_keyboard(req_id, message.from_user.id), parse_mode="Markdown")
    except: pass

# ---------------------------------------------------------
# TASK HANDLERS
# ---------------------------------------------------------
async def get_task_data(is_facebook=False, task_type='insta_2fa'):
    pw_mode = await get_setting(f'password_mode_{task_type}')
    if not pw_mode:
        pw_mode = await get_setting('password_mode') or 'random'
        
    date_str = await get_system_date()
    day = int(date_str.split('-')[2])
    
    if pw_mode == 'random':
        password = generate_hard_password(day)
    else:
        password = await get_setting(f'fixed_password_{task_type}')
        if not password:
            password = await get_setting('fixed_password') or 'GoTaskPassword123'
            
    if is_facebook:
        first_name = fake.first_name()
        last_name = fake.last_name()
        return first_name, last_name, password
    return generate_username(), password

@router.message(F.text.in_({"কাজ", "Task"}), StateFilter("*"))
async def task_platform_selection(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FlowStates.waiting_for_task_platform)
    lang = await get_user_lang(message.from_user.id)
    text = f"<tg-emoji emoji-id='4958506272551863292'>📊</tg-emoji> <b>কোন প্লাটফর্মের কাজ করতে চান?</b>" if lang == 'bn' else f"<tg-emoji emoji-id='4958506272551863292'>📊</tg-emoji> <b>Which platform's tasks do you want to do?</b>"
    kb = await get_platform_keyboard()
    if not kb:
        await state.clear()
        return await message.answer("⚠️ দুঃখিত, বর্তমানে কোনো কাজ এভেইলেবল নেই। দয়া করে পরে আবার চেষ্টা করুন।", reply_markup=get_main_keyboard(is_admin(message.from_user.id), lang))
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.message(F.text.contains("Facebook"), FlowStates.waiting_for_task_platform)
async def show_fb_tasks(message: Message, state: FSMContext):
    await state.set_state(FlowStates.in_fb_menu)
    fb_2fa_price = await get_setting('fb_2fa_price')
    fb_cookie_price = await get_setting('fb_cookie_price')
    await message.answer(f"{PE_FACEBOOK} Facebook এর কাজগুলো:", reply_markup=get_fb_tasks_keyboard(fb_2fa_price, fb_cookie_price), parse_mode="HTML")

@router.message(F.text.contains("Instagram"), FlowStates.waiting_for_task_platform)
async def show_insta_tasks(message: Message, state: FSMContext):
    await state.set_state(FlowStates.in_insta_menu)
    insta_price = await get_setting('task_price')
    await message.answer(f"{PE_INSTAGRAM} Instagram এর কাজগুলো:", reply_markup=get_insta_tasks_keyboard(insta_price), parse_mode="HTML")

@router.message(F.text == "কীভাবে কাজ করব")
async def show_how_to_work(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == FlowStates.in_fb_menu.state:
        tut = await get_setting('tutorial_fb')
        if not tut: tut = "No tutorial set for Facebook yet."
        await message.answer(tut)
    elif current_state == FlowStates.in_insta_menu.state:
        tut = await get_setting('tutorial_insta')
        if not tut: tut = "No tutorial set for Instagram yet."
        await message.answer(tut)
    else:
        tut = await get_setting('tutorial_message')
        if not tut: tut = "No tutorial set."
        await message.answer(tut)

@router.message(F.text == "বাতিল", StateFilter("*"))
async def cancel_menus(message: Message, state: FSMContext):
    await state.clear()
    lang = await get_user_lang(message.from_user.id)
    await message.answer("❌ Cancelled.", reply_markup=get_main_keyboard(is_admin(message.from_user.id), lang))


@router.callback_query(F.data == "start_task_submenu")
async def cb_start_task_submenu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    insta_price = await get_setting('task_price')
    fb_2fa_price = await get_setting('fb_2fa_price')
    fb_cookie_price = await get_setting('fb_cookie_price')
    await callback.message.answer("Select a task from the menu below:", reply_markup=await get_task_submenu_keyboard(insta_price, fb_2fa_price, fb_cookie_price))
    await callback.answer()

import re
import asyncio
import time

async def task_timeout_worker(state: FSMContext, start_time: float, bot: Bot, chat_id: int):
    try:
        countdown_started = 0
        while True:
            await asyncio.sleep(10)
            data = await state.get_data()
            if data.get('task_start_time') != start_time:
                break
                
            task_type = data.get('task_type', 'insta_2fa')
            db_key = f'task_status_{task_type}'
            status = await get_setting(db_key) or 'on'
            
            if status == 'off':
                if countdown_started == 0:
                    countdown_started = time.time()
                elif time.time() - countdown_started >= 300:
                    await state.clear()
                    if 'task_msg_id' in data:
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=data['task_msg_id'])
                        except Exception:
                            pass
                    lang = await get_user_lang(chat_id)
                    text = "⚠️ এই টাস্কটি বন্ধ করে দেওয়া হয়েছে এবং আপনার ৫ মিনিট সময় শেষ! (Task expired)" if lang == 'bn' else "⚠️ This task was closed and your 5 minutes expired!"
                    try:
                        await bot.send_message(chat_id, text)
                    except Exception:
                        pass
                    break
            else:
                countdown_started = 0
    except Exception:
        pass

def generate_full_name(username: str) -> str:
    base = re.sub(r'[0-9]+', '', username)
    base = base.replace('_', ' ').replace('.', ' ')
    full_name = base.title().strip()
    if not full_name:
        return 'Random Name'
    return full_name

@router.message(F.text.contains("Instagram 2FA") | F.text.contains("Insta 2FA"), StateFilter("*"))
async def start_task(message: Message, bot: Bot, state: FSMContext):
    if not await check_channel_membership(bot, message.from_user.id):
        return await message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))
    if not await check_submit_deadline():
        return await message.answer("⚠️ আজকের জন্য টাস্ক সাবমিট করার সময় শেষ হয়ে গেছে! দয়া করে আগামীকাল আবার চেষ্টা করুন।")
    
    status = await get_setting('task_status_insta_2fa') or 'on'
    if status == 'off':
        return await message.answer("⚠️ এই টাস্কটি সাময়িকভাবে বন্ধ আছে। (Currently disabled by admin)")

    username, password = await get_task_data()
    full_name = generate_full_name(username)
    start_time = time.time()
    await state.update_data(task_username=username, task_password=password, task_type='insta_2fa', task_start_time=start_time)
    price = await get_setting('task_price')
    
    # Send a small helper message to show the Reply Keyboard (Done / Cancel)
    lang = await get_user_lang(message.from_user.id)
    msg_help = f"{E_DOWN} <b>নিচে থাকা username and password ব্যবহার করে account খুলে Done ✅ এ ক্লিক করুন, এরপর আপনার 2fa key দিন:</b>" if lang == 'bn' else f"{E_DOWN} <b>Use the username and password below to open an account, click Done ✅, then provide your 2fa key:</b>"
    await message.answer(msg_help, reply_markup=get_task_keyboard(), parse_mode="HTML")

    text = (
        f"{E_TARGET} <b>আপনার নতুন কাজ প্রস্তুত!</b>\n\n" if lang == 'bn' else f"{E_TARGET} <b>আপনার নতুন কাজ প্রস্তুত!</b>\n\n"
    )
    text += (
        f"<b>টাস্ক:</b> {PE_INSTAGRAM} Instagram 2FA\n" if lang == 'bn' else f"<b>টাস্ক:</b> {PE_INSTAGRAM} Instagram 2FA\n"
    )
    text += (
        f"{E_CASH} <b>মূল্য:</b> <code>{price} TK</code>\n\n" if lang == 'bn' else f"{E_CASH} <b>মূল্য:</b> <code>{price} TK</code>\n\n"
    )
    text += (
        f"📝 <b>পুরো নাম:</b> <code>{full_name}</code> (ঐচ্ছিক/Optional)\n" if lang == 'bn' else f"📝 <b>পুরো নাম:</b> <code>{full_name}</code> (ঐচ্ছিক/Optional)\n"
    )
    text += (
        f"{E_USER} <b>ইউজারনেম:</b> <code>{username}</code>\n" if lang == 'bn' else f"{E_USER} <b>ইউজারনেম:</b> <code>{username}</code>\n"
    )
    text += (
        f"{E_LOCK} <b>পাসওয়ার্ড:</b> <code>{password}</code>\n\n" if lang == 'bn' else f"{E_LOCK} <b>পাসওয়ার্ড:</b> <code>{password}</code>\n\n"
    )
    text += (
        f"{E_BULL} <i>অ্যাকাউন্ট খোলার পর অবশ্যই Profile Picture অ্যাড করবেন এবং ২-৩ জনকে Follow করবেন!</i>" if lang == 'bn' else f"{E_BULL} <i>অ্যাকাউন্ট খোলার পর অবশ্যই Profile Picture অ্যাড করবেন এবং ২-৩ জনকে Follow করবেন!</i>"
    )
    msg = await message.answer(text, parse_mode="HTML")
    await state.update_data(task_username=username, task_password=password, task_msg_id=msg.message_id)
    asyncio.create_task(task_timeout_worker(state, start_time, bot, message.from_user.id))

@router.message(F.text == "Done")
async def task_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if 'task_username' not in data:
        return await message.answer("⚠️ Start a task first.")
    await message.answer("আপনার কাজ সফলভাবে করে থাকলে দয়া করে 2FA Key দিন।", reply_markup=get_task_keyboard())
    await state.set_state(TaskStates.waiting_for_2fa_key)

@router.message(TaskStates.waiting_for_2fa_key)
async def process_2fa_key(message: Message, state: FSMContext, bot: Bot):
    if message.text in ["Done ✅", "Cancel ❌", "Cancel", "বাতিল", "কাজ 💼", "Balance 💰", "Report 📊", "Admin Panel ⚙️", "Back 🔙"]:
        if message.text in ["Cancel ❌", "Cancel", "বাতিল", "Back 🔙"]:
            data = await state.get_data()
            if 'task_msg_id' in data:
                try:
                    await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
                except Exception:
                    pass
            await state.clear()
            return await message.answer("❌ Task cancelled.", reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))
        return
        
    clean_key = message.text.replace(" ", "").upper()
    if len(clean_key) != 32 or not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in clean_key):
        return await message.answer("⚠️ আপনার 2FA Key-টি সঠিক নয়! Instagram 2FA Key ঠিক ৩২ অক্ষরের Base32 কোড হতে হবে।")
        
    existing = await db.tasks.find_one({'two_fa_key': clean_key})
    if existing:
        return await message.answer("⚠️ Duplicate 2FA Key পাওয়া গেছে! এই Key-টি আগে কেউ সাবমিট করেছে। ফেক কাজ করলে অ্যাকাউন্ট ব্যান করা হবে!")
        
    otp = generate_2fa_code(clean_key)
    if not otp: return await message.answer("⚠️ Invalid 2FA Key. কোড জেনারেট করা সম্ভব হয়নি।")
    
    await state.update_data(two_fa_key=clean_key, two_fa_code=otp)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Refresh Code", "refresh_2fa_code", "5337132498965010628", "success")]])
    await message.answer(f"✅ Key সফলভাবে গ্রহণ করা হয়েছে।\n\n🔐 **2FA Code:** `{otp}`", reply_markup=kb, parse_mode="Markdown")
    
    await message.answer(f"{PE_INSTAGRAM} Instagram-এ এই কোডটি বসিয়ে নিচে থেকে Complete and Submit বাটনে ক্লিক করুন।", reply_markup=get_submit_keyboard(), parse_mode="HTML")
    await state.set_state(TaskStates.waiting_for_submit)

@router.callback_query(F.data == "refresh_2fa_code")
async def cb_refresh_2fa_code(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    clean_key = data.get('two_fa_key')
    if not clean_key:
        return await callback.answer("⚠️ No active task found.", show_alert=True)
    
    otp = generate_2fa_code(clean_key)
    if not otp:
        return await callback.answer("⚠️ Failed to generate code.", show_alert=True)
        
    await state.update_data(two_fa_code=otp)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Refresh Code", "refresh_2fa_code", "5337132498965010628", "success")]])
    new_text = f"✅ Key সফলভাবে গ্রহণ করা হয়েছে।\n\n🔐 **2FA Code:** `{otp}`"
    
    # Only edit if the text actually changed (otherwise Telegram throws an error)
    if callback.message.text and new_text.strip() not in callback.message.text.strip():
        try:
            await callback.message.edit_text(new_text, reply_markup=kb, parse_mode="Markdown")
            await callback.answer("✅ Code Refreshed!")
        except Exception:
            await callback.answer("✅ Code is same, wait a few seconds.", show_alert=False)
    else:
        await callback.answer("⚠️ Wait a few seconds before refreshing again.", show_alert=False)

@router.message(F.text == "Complete and Submit", TaskStates.waiting_for_submit)
async def submit_task(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    username = data.get('task_username')
    
    # Check if user is already banned
    user_doc = await db.users.find_one({'tg_id': message.from_user.id})
    if user_doc and user_doc.get('is_banned', False):
        return await message.answer("❌ Your account is blocked due to fake submissions.")
        
    ss_status = await get_setting('insta_screenshot_status') or 'on'
    
    if ss_status == 'off':
        # Bypass screenshot and submit directly
        task_type = data.get('task_type', 'insta_2fa')
        await add_task(message.from_user.id, data.get('task_username', ''), data.get('task_password', ''), data.get('two_fa_key', ''), data.get('two_fa_code', ''), task_type)
        
        if 'task_msg_id' in data:
            try:
                await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
            except Exception:
                pass
        await state.clear()
        
        pending_count = await get_pending_tasks_count(message.from_user.id)
        accepted_count = await get_completed_tasks_count(message.from_user.id)
        
        text = (
            f"🎉 **Task Submitted Successfully!**\n\n"
            f"📊 **আপনার কাজের রিপোর্ট:**\n"
            f"⏳ **পেন্ডিং কাজ:** `{pending_count}` টি\n"
            f"✅ **অ্যাপ্রুভড কাজ:** `{accepted_count}` টি\n\n"
            f"নতুন কাজ শুরু করতে নিচের বাটনে ক্লিক করুন 👇"
        )
        insta_price = await get_setting('task_price')
        fb_2fa_price = await get_setting('fb_2fa_price')
        fb_cookie_price = await get_setting('fb_cookie_price')
        await message.answer(text, reply_markup=await get_task_submenu_keyboard(insta_price, fb_2fa_price, fb_cookie_price), parse_mode="Markdown")
        return
        
    await message.answer(
        f"📷 <b>স্ক্রিনশট দিন</b>\n\n"
        f"অনুগ্রহ করে আপনার Instagram প্রোফাইলের একটি স্ক্রিনশট দিন, যেখানে <b>{username}</b> ইউজারনেমটি পরিষ্কার দেখা যায়।\n\n"
        f"<i>(ফেক স্ক্রিনশট দিলে অ্যাকাউন্ট ব্লক করা হবে!)</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(TaskStates.waiting_for_screenshot)

@router.message(~F.text.in_(MAIN_MENU_BUTTONS), TaskStates.waiting_for_submit)
async def submit_task_fallback(message: Message, state: FSMContext, bot: Bot):
    return await message.answer("⚠️ অনুগ্রহ করে নিচের 'Complete and Submit 📥' বাটনে ক্লিক করুন।", reply_markup=get_submit_keyboard())

@router.message(F.text & ~F.text.in_(MAIN_MENU_BUTTONS), TaskStates.waiting_for_screenshot)
async def process_screenshot_text_fallback(message: Message, state: FSMContext, bot: Bot):
    await message.answer("⚠️ অনুগ্রহ করে একটি **ছবি (Screenshot)** দিন। আপনি টেক্সট পাঠিয়েছেন।", parse_mode="Markdown")

async def get_valid_ocr_key():
    current_month = datetime.now().strftime("%Y-%m")
    keys = await db.ocr_keys.find({}).to_list(None)
    
    for k in keys:
        if k.get('month') != current_month:
            await db.ocr_keys.update_one({'_id': k['_id']}, {'$set': {'usage': 0, 'month': current_month}})
            k['usage'] = 0
            k['month'] = current_month
            
        if k.get('usage', 0) < 25000:
            return k['key'], k['_id']
            
    return 'helloworld', None

@router.message(F.photo, TaskStates.waiting_for_screenshot)
async def process_screenshot(message: Message, state: FSMContext, bot: Bot):
    processing_msg = await message.answer("🔄 স্ক্রিনশট স্ক্যান করা হচ্ছে, দয়া করে অপেক্ষা করুন...")
    
    try:
        data = await state.get_data()
        expected_username = data.get('task_username')
        
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                image_data = await resp.read()
                
            api_key, key_id = await get_valid_ocr_key()
                
            payload = aiohttp.FormData()
            payload.add_field('apikey', api_key) 
            payload.add_field('file', image_data, filename='screenshot.jpg', content_type='image/jpeg')
            payload.add_field('OCREngine', '2')
            
            async with session.post('https://api.ocr.space/parse/image', data=payload) as ocr_resp:
                ocr_result = await ocr_resp.json()
                if key_id:
                    await db.ocr_keys.update_one({'_id': key_id}, {'$inc': {'usage': 1}})
                
        extracted_text = ""
        if ocr_result and not ocr_result.get('IsErroredOnProcessing'):
            for p in ocr_result.get('ParsedResults', []):
                extracted_text += p.get('ParsedText', '')
                
        extracted_text = extracted_text.lower().replace(" ", "")
        expected_username_clean = expected_username.lower().replace(" ", "")
        
        if expected_username_clean in extracted_text:
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
            
            task_type = data.get('task_type', 'insta_2fa')
            await add_task(message.from_user.id, data['task_username'], data['task_password'], data['two_fa_key'], data['two_fa_code'], task_type)
            
            if 'task_msg_id' in data:
                try:
                    await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
                except Exception:
                    pass
            await state.clear()
            
            await db.users.update_one({'tg_id': message.from_user.id}, {'$set': {'screenshot_strikes': 0}})
            
            pending_count = await get_pending_tasks_count(message.from_user.id)
            accepted_count = await get_completed_tasks_count(message.from_user.id)
            
            text = (
                f"🎉 **Task Submitted Successfully!**\n\n"
                f"📊 **আপনার কাজের রিপোর্ট:**\n"
                f"⏳ **পেন্ডিং কাজ:** `{pending_count}` টি\n"
                f"✅ **অ্যাপ্রুভড কাজ:** `{accepted_count}` টি\n\n"
                f"নতুন কাজ শুরু করতে নিচের বাটনে ক্লিক করুন 👇"
            )
            insta_price = await get_setting('task_price')
            fb_2fa_price = await get_setting('fb_2fa_price')
            fb_cookie_price = await get_setting('fb_cookie_price')
            await message.answer(text, reply_markup=await get_task_submenu_keyboard(insta_price, fb_2fa_price, fb_cookie_price), parse_mode="Markdown")
        else:
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
            
            user_doc = await db.users.find_one({'tg_id': message.from_user.id})
            today_str = await get_system_date()
            
            strikes = 0
            if user_doc:
                last_strike_date = user_doc.get('screenshot_strikes_date', '')
                if last_strike_date == today_str:
                    strikes = user_doc.get('screenshot_strikes', 0)
            
            strikes += 1
            
            if strikes >= 3:
                await db.users.update_one({'tg_id': message.from_user.id}, {'$set': {'is_banned': True, 'screenshot_strikes': strikes, 'screenshot_strikes_date': today_str}})
                await state.clear()
                await message.answer("❌ আপনি আজ বারবার ফেক স্ক্রিনশট দিয়েছেন! আপনার অ্যাকাউন্ট ব্লক করা হয়েছে এবং উইথড্র বন্ধ করে দেওয়া হয়েছে।")
            else:
                await db.users.update_one({'tg_id': message.from_user.id}, {'$set': {'screenshot_strikes': strikes, 'screenshot_strikes_date': today_str}})
                remaining = 3 - strikes
                await message.answer(f"⚠️ আপনার স্ক্রিনশটে `{expected_username}` ইউজারনেমটি পাওয়া যায়নি! এটি আপনার ভুল স্ক্রিনশট।\nআজ আর মাত্র {remaining} বার ভুল করলে আপনার অ্যাকাউন্ট ব্লক করা হবে এবং ব্যালেন্স কেটে নেওয়া হবে (Warning)!\n\nসঠিক স্ক্রিনশট দিন:")
                
    except Exception as e:
        print(f"OCR Error: {e}")
        await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        await message.answer("⚠️ স্ক্রিনশট চেক করতে সমস্যা হয়েছে। দয়া করে আবার স্ক্রিনশট দিন বা কিছুক্ষণ পর চেষ্টা করুন।")

# ---------------------------------------------------------
# FACEBOOK TASK HANDLERS
# ---------------------------------------------------------
@router.message(
    F.text.regexp(r"^(?:Facebook 2FA|Facebook Cookie|FB 2FA|FB Cookie)(?: \([^()\n]*\))?$"),
    StateFilter("*"),
)
async def start_fb_task(message: Message, bot: Bot, state: FSMContext):
    if not await check_channel_membership(bot, message.from_user.id):
        return await message.answer("⚠️ বটটি ব্যবহার করতে হলে প্রথমে আমাদের টেলিগ্রাম চ্যানেলে Join করতে হবে!", reply_markup=await get_force_join_keyboard(bot))
    if not await check_submit_deadline():
        return await message.answer("⚠️ আজকের জন্য টাস্ক সাবমিট করার সময় শেষ হয়ে গেছে! দয়া করে আগামীকাল আবার চেষ্টা করুন।")
    
    is_cookie_task = "Cookie" in message.text
    task_type = 'fb_cookie' if is_cookie_task else 'fb_2fa'
    
    db_key = 'task_status_fb_cookie' if is_cookie_task else 'task_status_fb_2fa'
    status = await get_setting(db_key) or 'on'
    if status == 'off':
        return await message.answer("⚠️ এই টাস্কটি সাময়িকভাবে বন্ধ আছে। (Currently disabled by admin)")
    
    first_name, last_name, password = await get_task_data(is_facebook=True, task_type=task_type)
    start_time = time.time()
    await state.update_data(fb_first_name=first_name, fb_last_name=last_name, fb_password=password, task_type=task_type, task_start_time=start_time)
    
    price = await get_setting('fb_cookie_price') if is_cookie_task else await get_setting('fb_2fa_price')
    task_name = f"{PE_FACEBOOK} Facebook Cookies" if is_cookie_task else f"{PE_FACEBOOK} Facebook 2FA"
    
    lang = await get_user_lang(message.from_user.id)
    msg_help = f"{E_DOWN} <b>নিচে থাকা First name, Last name and password ব্যবহার করে account খুলে Submit UID 🆔 এ ক্লিক করুন:</b>" if lang == 'bn' else f"{E_DOWN} <b>Use the First name, Last name and password below to open an account and click Submit UID 🆔:</b>"
    await message.answer(msg_help, reply_markup=get_submit_uid_keyboard(), parse_mode="HTML")

    text = (
        f"<b>আপনার নতুন কাজ প্রস্তুত!</b>\n\n" if lang == 'bn' else f"<b>আপনার নতুন কাজ প্রস্তুত!</b>\n\n"
    )
    text += (
        f"<b>টাস্ক:</b> {task_name}\n" if lang == 'bn' else f"<b>টাস্ক:</b> {task_name}\n"
    )
    text += (
        f"<b>মূল্য:</b> <code>{price} TK</code>\n\n" if lang == 'bn' else f"<b>মূল্য:</b> <code>{price} TK</code>\n\n"
    )
    text += (
        f"👤 <b>First Name:</b> <code>{first_name}</code>\n"
        f"👤 <b>Last Name:</b> <code>{last_name}</code>\n"
    )
    text += (
        f"🔐 <b>পাসওয়ার্ড:</b> <code>{password}</code>\n\n" if lang == 'bn' else f"🔐 <b>পাসওয়ার্ড:</b> <code>{password}</code>\n\n"
    )
    text += (
        f"<i>অ্যাকাউন্ট খোলার পর Submit UID তে ক্লিক করুন!</i>" if lang == 'bn' else f"<i>অ্যাকাউন্ট খোলার পর Submit UID তে ক্লিক করুন!</i>"
    )
    msg = await message.answer(text, parse_mode="HTML")
    await state.update_data(task_msg_id=msg.message_id)
    
    asyncio.create_task(task_timeout_worker(state, start_time, bot, message.from_user.id))

@router.message(F.text == "Submit UID")
async def ask_for_uid(message: Message, state: FSMContext):
    data = await state.get_data()
    if 'fb_first_name' not in data:
        return await message.answer("⚠️ Start a task first.")
    await message.answer("✅ Send me the UID (User ID):", reply_markup=get_cancel_keyboard())
    
    task_type = data.get('task_type', 'fb_2fa')
    if task_type == 'fb_cookie':
        await state.set_state(FBTaskStates.waiting_for_fb_uid_cookie)
    else:
        await state.set_state(FBTaskStates.waiting_for_fb_uid_2fa)

@router.message(FBTaskStates.waiting_for_fb_uid_2fa)
@router.message(FBTaskStates.waiting_for_fb_uid_cookie)
async def process_fb_uid(message: Message, state: FSMContext, bot: Bot):
    if message.text in ["Cancel ❌", "Cancel", "বাতিল", "কাজ 💼", "Balance 💰", "Report 📊", "Admin Panel ⚙️", "Back 🔙"]:
        if message.text in ["Cancel ❌", "Cancel", "বাতিল", "Back 🔙"]:
            data = await state.get_data()
            if 'task_msg_id' in data:
                try:
                    await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
                except Exception:
                    pass
            await state.clear()
            return await message.answer("❌ Task cancelled.", reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))
        return
        
    uid = message.text.strip()
    if not uid.isdigit() or not uid.startswith("61") or len(uid) != 14:
        return await message.answer("⚠️ দয়া করে সঠিক UID দিন।")
        
    existing_task = await db.tasks.find_one({'name': uid, 'task_type': {'$in': ['fb_2fa', 'fb_cookie']}, 'status': {'$in': ['pending', 'accepted']}})
    if existing_task:
        return await message.answer("⚠️ এই UID টি ইতিমধ্যে সাবমিট করা হয়েছে। দয়া করে অন্য UID দিন:")

    # Blacklist check — UID deleted by admin
    if await db.deleted_uids.find_one({'uid': uid}):
        return await message.answer("⚠️ এই UID টি গ্রহণযোগ্য নয়। দয়া করে অন্য UID দিন:")

    await state.update_data(fb_uid=uid)
    
    current_state = await state.get_state()
    if current_state == FBTaskStates.waiting_for_fb_uid_cookie.state:
        await message.answer("✅ UID Received. Now send me the Cookies data:", reply_markup=get_cancel_keyboard())
        await state.set_state(FBTaskStates.waiting_for_fb_cookie)
    else:
        await message.answer("✅ UID Received. Now send me the 2FA Key:", reply_markup=get_cancel_keyboard())
        await state.set_state(FBTaskStates.waiting_for_fb_2fa_key)

@router.message(FBTaskStates.waiting_for_fb_cookie)
async def process_fb_cookie(message: Message, state: FSMContext, bot: Bot):
    if message.text in ["Cancel ❌", "Cancel", "বাতিল", "কাজ 💼", "Balance 💰", "Report 📊", "Admin Panel ⚙️", "Back 🔙"]:
        if message.text in ["Cancel ❌", "Cancel", "বাতিল", "Back 🔙"]:
            data = await state.get_data()
            if 'task_msg_id' in data:
                try:
                    await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
                except Exception:
                    pass
            await state.clear()
            return await message.answer("❌ Task cancelled.", reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))
        return
        
    cookie_data = message.text.strip()
    if not cookie_data:
        return await message.answer("⚠️ Please enter valid Cookies data.")
        
    await state.update_data(fb_cookie_data=cookie_data)
    await message.answer("✅ Cookies Received. Click below to submit.", reply_markup=get_submit_keyboard())
    await state.set_state(FBTaskStates.waiting_for_fb_submit)

@router.message(FBTaskStates.waiting_for_fb_2fa_key)
async def process_fb_2fa_key(message: Message, state: FSMContext, bot: Bot):
    if message.text in ["Cancel ❌", "Cancel", "বাতিল", "কাজ 💼", "Balance 💰", "Report 📊", "Admin Panel ⚙️", "Back 🔙"]:
        if message.text in ["Cancel ❌", "Cancel", "বাতিল", "Back 🔙"]:
            data = await state.get_data()
            if 'task_msg_id' in data:
                try:
                    await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
                except Exception:
                    pass
            await state.clear()
            return await message.answer("❌ Task cancelled.", reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))
        return
        
    clean_key = message.text.replace(" ", "").upper()
    if len(clean_key) < 16 or not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in clean_key):
        return await message.answer("⚠️ আপনার 2FA Key-টি সঠিক নয়! এটি একটি সঠিক Base32 কোড হতে হবে।")
        
    existing = await db.tasks.find_one({'two_fa_key': clean_key, 'task_type': 'fb_2fa'})
    if existing:
        return await message.answer("⚠️ Duplicate 2FA Key পাওয়া গেছে! এই Key-টি আগে কেউ সাবমিট করেছে। ফেক কাজ করলে অ্যাকাউন্ট ব্যান করা হবে!")
        
    otp = generate_2fa_code(clean_key)
    if not otp: return await message.answer("⚠️ Invalid 2FA Key. কোড জেনারেট করা সম্ভব হয়নি।")
    
    await state.update_data(two_fa_key=clean_key, two_fa_code=otp)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Refresh Code", "refresh_2fa_code", "5337132498965010628", "success")]])
    await message.answer(f"✅ Key সফলভাবে গ্রহণ করা হয়েছে।\n\n🔐 **2FA Code:** `{otp}`", reply_markup=kb, parse_mode="Markdown")
    
    await message.answer(f"{PE_FACEBOOK} Facebook-এ এই কোডটি বসিয়ে নিচে থেকে Complete and Submit বাটনে ক্লিক করুন।", reply_markup=get_submit_keyboard(), parse_mode="HTML")
    await state.set_state(FBTaskStates.waiting_for_fb_submit)

@router.message(F.text == "Complete and Submit", FBTaskStates.waiting_for_fb_submit)
async def submit_fb_task(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    task_type = data.get('task_type')
    uid = data.get('fb_uid')
    password = data.get('fb_password')
    
    if task_type == 'fb_cookie':
        two_fa_key = data.get('fb_cookie_data') # store cookies in two_fa_key column for now or we could add a new column
        two_fa_code = ""
    else:
        two_fa_key = data.get('two_fa_key')
        two_fa_code = data.get('two_fa_code')
        
    await add_task(message.from_user.id, uid, password, two_fa_key, two_fa_code, task_type)
    
    if 'task_msg_id' in data:
        try:
            await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
        except Exception:
            pass
            
    await state.clear()
    
    pending_count = await get_pending_tasks_count(message.from_user.id)
    accepted_count = await get_completed_tasks_count(message.from_user.id)
    
    text = (
        f"🎉 **Task Submitted Successfully!**\n\n"
        f"📊 **আপনার কাজের রিপোর্ট:**\n"
        f"⏳ **পেন্ডিং কাজ:** `{pending_count}` টি\n"
        f"✅ **অ্যাপ্রুভড কাজ:** `{accepted_count}` টি\n\n"
        f"নতুন কাজ শুরু করতে নিচের বাটনে ক্লিক করুন 👇"
    )
    
    insta_price = await get_setting('task_price')
    fb_2fa_price = await get_setting('fb_2fa_price')
    fb_cookie_price = await get_setting('fb_cookie_price')
    await message.answer(text, reply_markup=await get_task_submenu_keyboard(insta_price, fb_2fa_price, fb_cookie_price), parse_mode="Markdown")

@router.message(F.text.in_({"Back", "Cancel", "Back to Main Menu"}), StateFilter("*"))
async def handle_cancel_main(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if 'task_msg_id' in data:
        try:
            await bot.delete_message(chat_id=message.from_user.id, message_id=data['task_msg_id'])
        except Exception:
            pass
    current_state = await state.get_state()
    await state.clear()
    
    if current_state and current_state.startswith("AdminSettingsStates"):
        await message.answer("⚙️ Settings Menu", reply_markup=get_settings_keyboard())
    elif current_state and current_state.startswith("Admin"):
        await message.answer("⚙️ Admin Panel", reply_markup=get_admin_panel_keyboard(message.from_user.id))
    else:
        await message.answer("🏠 Main Menu", reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))

# ---------------------------------------------------------
# ADMIN HANDLERS
# ---------------------------------------------------------
@router.message(F.text == "Admin Panel", StateFilter("*"))
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id):
        total_users = await db.users.count_documents({})
        date_str_insta = await get_system_date('insta_2fa')
        date_str_fb = await get_system_date('fb_2fa')
        date_str_cookie = await get_system_date('fb_cookie')
        
        todays_tasks = (
            await db.tasks.count_documents({'date': date_str_insta, 'task_type': 'insta_2fa'}) +
            await db.tasks.count_documents({'date': date_str_fb, 'task_type': 'fb_2fa'}) +
            await db.tasks.count_documents({'date': date_str_cookie, 'task_type': 'fb_cookie'})
        )
        
        # Approximate unique users today by checking across the 3 dates
        unique_users_today = len(await db.tasks.distinct('tg_id', {'$or': [
            {'date': date_str_insta, 'task_type': 'insta_2fa'},
            {'date': date_str_fb, 'task_type': 'fb_2fa'},
            {'date': date_str_cookie, 'task_type': 'fb_cookie'}
        ]}))
        
        total_pending = await db.tasks.count_documents({'status': 'pending'})
        total_pending_withdraws = await db.withdraws.count_documents({'status': 'pending'})
        
        # Get details
        pending_insta = await db.tasks.count_documents({'status': 'pending', 'task_type': 'insta_2fa'})
        pending_fb_2fa = await db.tasks.count_documents({'status': 'pending', 'task_type': 'fb_2fa'})
        pending_fb_cookie = await db.tasks.count_documents({'status': 'pending', 'task_type': 'fb_cookie'})
        
        today_insta = await db.tasks.count_documents({'date': date_str_insta, 'task_type': 'insta_2fa'})
        today_fb_2fa = await db.tasks.count_documents({'date': date_str_fb, 'task_type': 'fb_2fa'})
        today_fb_cookie = await db.tasks.count_documents({'date': date_str_cookie, 'task_type': 'fb_cookie'})
        
        text = (
            f"⚙️ **Admin Panel**\n\n"
            f"👥 **Total Users:** `{total_users}`\n"
            f"👤 **Users Submitted Today:** `{unique_users_today}`\n\n"
            f"📊 **Today's Submits ({todays_tasks}):**\n"
            f" ├ Insta 2FA: `{today_insta}`\n"
            f" ├ FB 2FA: `{today_fb_2fa}`\n"
            f" └ FB Cookie: `{today_fb_cookie}`\n\n"
            f"⏳ **Pending Verification ({total_pending}):**\n"
            f" ├ Insta 2FA: `{pending_insta}`\n"
            f" ├ FB 2FA: `{pending_fb_2fa}`\n"
            f" └ FB Cookie: `{pending_fb_cookie}`\n\n"
            f"💳 **Pending Withdrawals:** `{total_pending_withdraws}`"
        )
        await message.answer(text, reply_markup=get_admin_panel_keyboard(message.from_user.id), parse_mode="Markdown")

@router.message(F.text == "Back to Admin Panel", StateFilter("*"))
async def back_to_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⚙️ Admin Panel", reply_markup=get_admin_panel_keyboard(message.from_user.id))

async def _get_filtered_pending_withdraws(filter_type):
    """Get pending withdrawals filtered by date. filter_type: all/today/yesterday/older"""
    now = datetime.utcnow() + timedelta(hours=6)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    
    pending_list = []
    cursor = db.withdraws.find({'status': 'pending'}).sort('date', -1)
    async for doc in cursor:
        if filter_type == 'all':
            pending_list.append(doc)
        else:
            doc_date = doc.get('date', '')[:10]  # "2026-07-10 12:30:00" -> "2026-07-10"
            if filter_type == 'today' and doc_date == today_str:
                pending_list.append(doc)
            elif filter_type == 'yesterday' and doc_date == yesterday_str:
                pending_list.append(doc)
            elif filter_type == 'older' and doc_date < yesterday_str:
                pending_list.append(doc)
    return pending_list

async def _get_pending_counts():
    """Get counts of pending withdrawals by date category"""
    now = datetime.utcnow() + timedelta(hours=6)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    
    total = 0
    today_count = 0
    yesterday_count = 0
    older_count = 0
    
    cursor = db.withdraws.find({'status': 'pending'})
    async for doc in cursor:
        total += 1
        doc_date = doc.get('date', '')[:10]
        if doc_date == today_str:
            today_count += 1
        elif doc_date == yesterday_str:
            yesterday_count += 1
        else:
            older_count += 1
    
    return total, today_count, yesterday_count, older_count

async def _build_pwd_card(doc, page, total, filter_type, bot):
    """Build a pending withdraw card message text and keyboard"""
    req_id = str(doc['_id'])
    tg_id = doc['tg_id']
    method = doc.get('method', 'N/A')
    details = doc.get('details', 'N/A')
    amount = doc.get('amount', 0)
    requested_amount = doc.get('requested_amount', amount)
    fee = doc.get('fee', 0)
    date = doc.get('date', 'N/A')
    
    try:
        user_chat = await bot.get_chat(tg_id)
        user_name = user_chat.full_name
    except:
        user_name = "Unknown"
    
    filter_labels = {'all': '📋 All', 'today': '📅 Today', 'yesterday': '📆 Yesterday', 'older': '📁 Older'}
    filter_label = filter_labels.get(filter_type, '📋 All')
    
    text = (
        f"📋 **Pending Withdraw [{page+1}/{total}]**\n"
        f"🏷️ Filter: **{filter_label}**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 **User:** [{user_name}](tg://user?id={tg_id})\n"
        f"🆔 **ID:** `{tg_id}`\n"
        f"💳 **Method:** {method}\n"
        f"📱 **Number:** `{details}`\n"
        f"💰 **Requested:** `{requested_amount} TK`\n"
        f"➖ **Fee:** `{fee} TK`\n"
        f"💵 **To Send:** `{amount} TK`\n"
        f"📅 **Date:** `{date}`\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(ibtn("Prev", f"pwdnav_{page-1}_{filter_type}", style="primary"))
    if page < total - 1:
        nav_buttons.append(ibtn("Next", f"pwdnav_{page+1}_{filter_type}", style="primary"))
    
    kb_rows = [
        [ibtn("Approve", f"pwd_approve_{req_id}_{tg_id}_{filter_type}", "5463060437572528782", "success"),
         ibtn("Reject",  f"pwd_reject_{req_id}_{tg_id}_{filter_type}",  "5974083768233760323", "danger")]
    ]
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([ibtn("Refresh",   f"pwd_refresh_{filter_type}", "5337132498965010628", "primary"),
                    ibtn("Filter Menu", "pwd_menu",                  "5420145051336485498", "primary")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    return text, kb

@router.message(F.text == "Pending Withdraws", StateFilter("*"))
async def pending_withdraws_panel(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not (is_admin(message.from_user.id, 'full_control') or str(message.from_user.id) == str(ADMIN_ID)):
        return
    
    total, today_count, yesterday_count, older_count = await _get_pending_counts()
    
    if total == 0:
        return await message.answer(
            "✅ কোনো pending withdrawal নেই।",
            reply_markup=get_admin_panel_keyboard(message.from_user.id)
        )
    
    text = (
        f"💳 **Pending Withdrawals**\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **মোট Pending:** `{total}` টি\n"
        f"📅 **আজকের:** `{today_count}` টি\n"
        f"📆 **গতকালের:** `{yesterday_count}` টি\n"
        f"📁 **পুরোনো:** `{older_count}` টি\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"কোন গুলো দেখতে চান? নিচে সিলেক্ট করুন:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn(f"All Pending ({total})",        "pwdfilter_all",       style="primary")],
        [ibtn(f"Today ({today_count})",         "pwdfilter_today",     style="success"),
         ibtn(f"Yesterday ({yesterday_count})", "pwdfilter_yesterday", style="primary")],
        [ibtn(f"Older ({older_count})",         "pwdfilter_older",     style="default")],
    ])
    
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "pwd_menu", StateFilter("*"))
async def pending_withdraw_menu(callback: CallbackQuery, bot: Bot):
    total, today_count, yesterday_count, older_count = await _get_pending_counts()
    
    if total == 0:
        return await callback.message.edit_text("✅ কোনো pending withdrawal নেই।")
    
    text = (
        f"💳 **Pending Withdrawals**\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **মোট Pending:** `{total}` টি\n"
        f"📅 **আজকের:** `{today_count}` টি\n"
        f"📆 **গতকালের:** `{yesterday_count}` টি\n"
        f"📁 **পুরোনো:** `{older_count}` টি\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"কোন গুলো দেখতে চান? নিচে সিলেক্ট করুন:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn(f"All Pending ({total})",        "pwdfilter_all",       style="primary")],
        [ibtn(f"Today ({today_count})",         "pwdfilter_today",     style="success"),
         ibtn(f"Yesterday ({yesterday_count})", "pwdfilter_yesterday", style="primary")],
        [ibtn(f"Older ({older_count})",         "pwdfilter_older",     style="default")],
    ])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except:
        await callback.answer("No changes.")

@router.callback_query(F.data.startswith("pwdfilter_"), StateFilter("*"))
async def pending_withdraw_filter(callback: CallbackQuery, bot: Bot):
    filter_type = callback.data.split("_")[1]  # all/today/yesterday/older
    
    pending_list = await _get_filtered_pending_withdraws(filter_type)
    total = len(pending_list)
    
    filter_labels = {'all': 'All', 'today': 'আজকের', 'yesterday': 'গতকালের', 'older': 'পুরোনো'}
    
    if total == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ibtn("Filter Menu", "pwd_menu", "5420145051336485498", "primary")]
        ])
        return await callback.message.edit_text(
            f"✅ {filter_labels.get(filter_type, '')} কোনো pending withdrawal নেই।",
            reply_markup=kb
        )
    
    doc = pending_list[0]
    text, kb = await _build_pwd_card(doc, 0, total, filter_type, bot)
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except:
        await callback.answer("No changes.")

@router.callback_query(F.data.startswith("pwdnav_"))
async def pending_withdraw_navigate(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    page = int(parts[1])
    filter_type = parts[2] if len(parts) > 2 else 'all'
    
    pending_list = await _get_filtered_pending_withdraws(filter_type)
    total = len(pending_list)
    
    if total == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ibtn("Filter Menu", "pwd_menu", "5420145051336485498", "primary")]
        ])
        return await callback.message.edit_text("✅ কোনো pending withdrawal নেই।", reply_markup=kb)
    
    if page >= total:
        page = 0
    
    doc = pending_list[page]
    text, kb = await _build_pwd_card(doc, page, total, filter_type, bot)
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except:
        await callback.answer("No changes.")

@router.callback_query(F.data.startswith("pwd_refresh_"), StateFilter("*"))
async def pending_withdraw_refresh(callback: CallbackQuery, bot: Bot):
    filter_type = callback.data.split("_")[2] if len(callback.data.split("_")) > 2 else 'all'
    
    pending_list = await _get_filtered_pending_withdraws(filter_type)
    total = len(pending_list)
    
    if total == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ibtn("Filter Menu", "pwd_menu", "5420145051336485498", "primary")]
        ])
        return await callback.message.edit_text("✅ কোনো pending withdrawal নেই।", reply_markup=kb)
    
    doc = pending_list[0]
    text, kb = await _build_pwd_card(doc, 0, total, filter_type, bot)
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except:
        await callback.answer("No changes.")

@router.callback_query(F.data.startswith("pwd_approve_") | F.data.startswith("pwd_reject_"), StateFilter("*"))
async def process_pending_withdraw_panel_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    parts = callback.data.split("_")
    action = parts[1]  # approve or reject
    req_id = parts[2]
    user_id = parts[3]
    filter_type = parts[4] if len(parts) > 4 else 'all'
    
    if action == "reject":
        await update_withdraw_status(req_id, "rejected")
        req = await get_withdraw_request(req_id)
        if req:
            await update_balance(int(user_id), req[4])
        
        await callback.answer("❌ Rejected & Refunded!", show_alert=True)
        try:
            await bot.send_message(chat_id=user_id, text="❌ আপনার Withdraw Request প্রত্যাখ্যান করা হয়েছে। টাকা ফেরত দেওয়া হয়েছে।")
        except:
            pass
        
        # Refresh the filtered list
        pending_list = await _get_filtered_pending_withdraws(filter_type)
        total = len(pending_list)
        
        if total == 0:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [ibtn("Filter Menu", "pwd_menu", "5420145051336485498", "primary")]
            ])
            return await callback.message.edit_text("✅ সব withdrawal process করা হয়েছে। কোনো pending নেই।", reply_markup=kb)
        
        doc = pending_list[0]
        text, kb = await _build_pwd_card(doc, 0, total, filter_type, bot)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        
    elif action == "approve":
        await state.update_data(approve_req_id=req_id, approve_user_id=user_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        
        req = await get_withdraw_request(req_id)
        number = req[3] if req else "Unknown"
        amount = req[4] if req else "Unknown"
        method = req[2] if req else "Unknown"
        
        try:
            user_chat = await bot.get_chat(user_id)
            user_name = user_chat.full_name
            user_name = user_name.replace('<', '&lt;').replace('>', '&gt;')
        except:
            user_name = "Unknown"
            
        await callback.message.answer(
            f"✅ <b>Approve Selected!</b>\n\n"
            f"👤 <b>User:</b> {user_name} (ID: <code>{user_id}</code>)\n"
            f"💳 <b>Method:</b> {method}\n"
            f"📱 <b>Number:</b> <code>{number}</code>\n"
            f"💰 <b>Amount to Send:</b> <code>{amount} TK</code>\n\n"
            f"📸 অনুগ্রহ করে উপরের নাম্বারে টাকা পাঠিয়ে এখানে <b>পেমেন্ট স্ক্রিনশট</b> আপলোড করুন।",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_withdraw_screenshot)

@router.message(F.text == "Leaderboard", StateFilter("*"))
async def worker_leaderboard(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not is_admin(message.from_user.id, 'stats'): return
    
    await message.answer("⏳ লিডারবোর্ড লোড হচ্ছে...")
    
    # Aggregate top 20 workers by accepted tasks
    pipeline = [
        {'$match': {'status': 'accepted'}},
        {'$group': {'_id': '$tg_id', 'total_tasks': {'$sum': 1}}},
        {'$sort': {'total_tasks': -1}},
        {'$limit': 20}
    ]
    
    top_workers = []
    async for doc in db.tasks.aggregate(pipeline):
        top_workers.append(doc)
    
    if not top_workers:
        return await message.answer("❌ কোনো approved task নেই।", reply_markup=get_admin_panel_keyboard(message.from_user.id))
    
    # Also get today's stats
    date_str_insta = await get_system_date('insta_2fa')
    date_str_fb = await get_system_date('fb_2fa')
    date_str_cookie = await get_system_date('fb_cookie')
    
    pipeline_today = [
        {'$match': {'status': 'accepted', '$or': [
            {'task_type': 'insta_2fa', 'date': date_str_insta},
            {'task_type': 'fb_2fa', 'date': date_str_fb},
            {'task_type': 'fb_cookie', 'date': date_str_cookie}
        ]}},
        {'$group': {'_id': '$tg_id', 'today_tasks': {'$sum': 1}}},
        {'$sort': {'today_tasks': -1}},
        {'$limit': 20}
    ]
    today_data = {}
    async for doc in db.tasks.aggregate(pipeline_today):
        today_data[doc['_id']] = doc['today_tasks']
    
    medals = ['🥇', '🥈', '🥉']
    lines = []
    
    for i, worker in enumerate(top_workers):
        tg_id = worker['_id']
        total = worker['total_tasks']
        today = today_data.get(tg_id, 0)
        
        # Get user info
        try:
            chat = await bot.get_chat(tg_id)
            name = chat.first_name or 'Unknown'
            name = name.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
        except:
            name = 'Unknown'
        
        rank = medals[i] if i < 3 else f"<code>{i+1}.</code>"
        today_str = f" ┃ আজ: <code>{today}</code>" if today > 0 else ""
        lines.append(f"{rank} {name}\n   ┗ মোট: <b>{total}</b> টি{today_str}")
    
    total_accepted = await db.tasks.count_documents({'status': 'accepted'})
    total_workers = len(await db.tasks.distinct('tg_id', {'status': 'accepted'}))
    
    text = (
        f"🏆 <b>Worker Leaderboard (Top 20)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(lines)
        + f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>সর্বমোট:</b> <code>{total_accepted}</code> approved tasks\n"
        f"👥 <b>মোট Workers:</b> <code>{total_workers}</code> জন"
    )
    
    await message.answer(text, reply_markup=get_admin_panel_keyboard(message.from_user.id), parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# DELETE ALL UIDs  (bulk UID blacklist — admin must confirm first)
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text == "Delete All (UIDs)")
async def delete_all_uids_start(message: Message, state: FSMContext):
    await state.clear()
    if not (is_admin(message.from_user.id, 'full_control') or str(message.from_user.id) == str(ADMIN_ID)):
        return
    await message.answer(
        "🔐 <b>Admin Verification Required</b>\n\n"
        "এই অ্যাকশন করার আগে নিশ্চিত করুন।\n"
        "নিচে <code>CONFIRM DELETE</code> টাইপ করুন:",
        reply_markup=get_cancel_keyboard(), parse_mode="HTML"
    )
    await state.set_state(AdminDeleteUIDStates.waiting_for_confirm)

@router.message(AdminDeleteUIDStates.waiting_for_confirm)
async def delete_all_uids_confirm(message: Message, state: FSMContext):
    if message.text in ["Cancel ❌", "Cancel", "বাতিল", "Back 🔙", "Back to Main Menu"]:
        await state.clear()
        return await message.answer("❌ বাতিল করা হয়েছে।", reply_markup=get_admin_panel_keyboard(message.from_user.id))

    if message.text.strip() != "CONFIRM DELETE":
        return await message.answer(
            "⚠️ সঠিক confirmation দিন।\n"
            "ঠিক এভাবে লিখুন: <code>CONFIRM DELETE</code>",
            parse_mode="HTML"
        )

    await message.answer(
        "✅ Verified!\n\n"
        "📝 এখন প্রতি লাইনে একটি করে UID দিন।\n"
        "উদাহরণ:\n"
        "<code>61000000000001\n"
        "61000000000002\n"
        "61000000000003</code>\n\n"
        "⚠️ যেসব UID-এর পেমেন্ট হয়ে গেছে সেগুলো task থেকে মুছে যাবে,\n"
        "কিন্তু withdrawal history থাকবে।",
        reply_markup=get_cancel_keyboard(), parse_mode="HTML"
    )
    await state.set_state(AdminDeleteUIDStates.waiting_for_uids)

@router.message(AdminDeleteUIDStates.waiting_for_uids)
async def delete_all_uids_process(message: Message, state: FSMContext, bot: Bot):
    if message.text in ["Cancel ❌", "Cancel", "বাতিল", "Back 🔙", "Back to Main Menu"]:
        await state.clear()
        return await message.answer("❌ বাতিল করা হয়েছে।", reply_markup=get_admin_panel_keyboard(message.from_user.id))

    lines = [l.strip() for l in message.text.strip().splitlines() if l.strip()]
    if not lines:
        return await message.answer("⚠️ কোনো UID পাওয়া যায়নি। দয়া করে আবার চেষ্টা করুন।")

    status_msg = await message.answer(f"⏳ {len(lines)} টি UID processing হচ্ছে...")

    deleted_count   = 0
    skipped_count   = 0
    blacklisted_new = 0
    notified_users  = set()

    for uid in lines:
        # ── blacklist (upsert — skip if already there) ──
        existing_bl = await db.deleted_uids.find_one({'uid': uid})
        if not existing_bl:
            await db.deleted_uids.insert_one({'uid': uid, 'deleted_at': __import__('datetime').datetime.utcnow().isoformat()})
            blacklisted_new += 1

        # ── find & delete all task records for this UID ──
        tasks_cursor = db.tasks.find({'name': uid})
        async for task_doc in tasks_cursor:
            tg_id      = task_doc.get('tg_id')
            prev_status = task_doc.get('status', 'pending')

            # If accepted, the user already got paid — we do NOT deduct balance
            # (payment is intentionally kept); just remove the task record.
            # If pending, remove without any balance change.
            await db.tasks.update_one({'_id': task_doc['_id']}, {'$set': {'uid_deleted': True, 'name': ''}})
            deleted_count += 1

            # Notify user once per user
            if tg_id and tg_id not in notified_users:
                notified_users.add(tg_id)
                try:
                    lang = await get_user_lang(tg_id)
                    if lang == 'bn':
                        note = (
                            f"ℹ️ Admin আপনার একটি UID রেকর্ড মুছে দিয়েছেন।\n"
                            f"আপনার পেমেন্ট নিরাপদ আছে।"
                        )
                    else:
                        note = (
                            f"ℹ️ An admin has removed one of your submitted UID records.\n"
                            f"Your payment history remains intact."
                        )
                    await bot.send_message(chat_id=tg_id, text=note)
                except Exception:
                    pass

    try:
        await status_msg.edit_text(
            f"✅ <b>Delete All UIDs — সম্পন্ন!</b>\n\n"
            f"📋 মোট UID দেওয়া হয়েছে: <code>{len(lines)}</code>\n"
            f"🔒 Task records লক হয়েছে: <code>{deleted_count}</code>\n"
            f"🚫 নতুন blacklist entry: <code>{blacklisted_new}</code>\n"
            f"👤 Notified users: <code>{len(notified_users)}</code>\n\n"
            f"এই UIDs আর কেউ submit করতে পারবে না।",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await state.clear()
    await message.answer("🏠 Admin Panel", reply_markup=get_admin_panel_keyboard(message.from_user.id))

@router.message(F.text == "Export Data")
async def export_data(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'export'): return
    
    date_str_insta = await get_system_date('insta_2fa')
    date_str_fb = await get_system_date('fb_2fa')
    date_str_cookie = await get_system_date('fb_cookie')
    
    # Get counts for each type
    insta_today = await db.tasks.count_documents({'date': date_str_insta, 'task_type': 'insta_2fa'})
    fb2fa_today = await db.tasks.count_documents({'date': date_str_fb, 'task_type': 'fb_2fa'})
    fbcookie_today = await db.tasks.count_documents({'date': date_str_cookie, 'task_type': 'fb_cookie'})
    
    insta_all = await db.tasks.count_documents({'task_type': 'insta_2fa'})
    fb2fa_all = await db.tasks.count_documents({'task_type': 'fb_2fa'})
    fbcookie_all = await db.tasks.count_documents({'task_type': 'fb_cookie'})
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn(f"Insta 2FA (আজ: {insta_today} | সব: {insta_all})",    "exp_type_insta_2fa",  "5334868205091459431", "success")],
        [ibtn(f"FB 2FA (আজ: {fb2fa_today} | সব: {fb2fa_all})",         "exp_type_fb_2fa",     "5334807341109908955", "primary")],
        [ibtn(f"FB Cookie (আজ: {fbcookie_today} | সব: {fbcookie_all})","exp_type_fb_cookie",  "5334807341109908955", "primary")],
        [ibtn("সব Export (আজকের)",                                                   "exp_type_all_today",  "5463352748751753567", "success")],
        [ibtn("বাতিল",                                                                "exp_cancel",          "5974083768233760323", "danger")],
    ])
    
    await message.answer(
        f"📁 **Export Data**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 **আজকের তারিখসমূহ:**\n"
        f"  • Insta 2FA: `{date_str_insta}`\n"
        f"  • FB 2FA: `{date_str_fb}`\n"
        f"  • FB Cookie: `{date_str_cookie}`\n\n"
        f"কোন টাইপের ডেটা export করতে চান? 👇",
        reply_markup=kb, parse_mode="Markdown"
    )

@router.callback_query(F.data == "exp_cancel", StateFilter("*"))
async def exp_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Export বাতিল করা হয়েছে।")
    await callback.answer()

@router.callback_query(F.data == "exp_type_all_today", StateFilter("*"))
async def exp_all_today(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'export'): return
    await callback.answer("⏳ সব export হচ্ছে...")
    
    types = ['insta_2fa', 'fb_2fa', 'fb_cookie']
    sent_any = False
    
    for t in types:
        date_str = await get_system_date(t)
        file_path, count = await export_tasks_xlsx(date_str, t)
        if file_path and count > 0 and os.path.exists(file_path):
            await callback.message.answer_document(
                FSInputFile(file_path), 
                caption=f"📊 Export: {date_str}\n📌 Type: {t}\n📋 Total: {count} accounts"
            )
            sent_any = True
            
    if not sent_any:
        await callback.message.answer("❌ আজকের জন্য কোনো ডেটা নেই।")
    else:
        await callback.message.answer("✅ সব export সম্পন্ন!")
    
    await callback.message.delete()

@router.callback_query(F.data.startswith("exp_type_"), StateFilter("*"))
async def exp_select_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'export'): return
    
    task_type = callback.data.replace("exp_type_", "")
    type_labels = {'insta_2fa': f'{PEB_INSTAGRAM} Insta 2FA', 'fb_2fa': f'{PEB_FACEBOOK} FB 2FA', 'fb_cookie': f'{PEB_FACEBOOK} FB Cookie'}
    label = type_labels.get(task_type, task_type)
    
    date_str = await get_system_date(task_type)
    # For date history buttons, we need logical today
    logical_today = datetime.strptime(date_str, "%Y-%m-%d")
    
    # Get counts
    today_count = await db.tasks.count_documents({'date': date_str, 'task_type': task_type})
    all_count = await db.tasks.count_documents({'task_type': task_type})
    
    # Get last 7 days with counts
    date_buttons = []
    for i in range(7):
        d = (logical_today - timedelta(days=i)).strftime("%Y-%m-%d")
        c = await db.tasks.count_documents({'date': d, 'task_type': task_type})
        if c > 0 or i == 0:
            day_label = "আজ" if i == 0 else ("গতকাল" if i == 1 else d)
            date_buttons.append(
                [ibtn(f"{day_label} ({c} টি)", f"exp_do_{task_type}_{d}", style="primary")]
            )
    
    kb_rows = []
    kb_rows.extend(date_buttons)
    if all_count > 0:
        kb_rows.append([ibtn(f"সব তারিখ ({all_count} টি)", f"exp_do_{task_type}_all", "5463352748751753567", "success")])
    kb_rows.append([ibtn("Back", "exp_back", "5420145051336485498", "primary")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    await callback.message.edit_text(
        f"📁 **{label} Export**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"কোন তারিখের ডেটা export করতে চান? 👇",
        reply_markup=kb, parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "exp_back", StateFilter("*"))
async def exp_back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Go back to export type selection"""
    if not is_admin(callback.from_user.id, 'export'): return
    
    date_str_insta = await get_system_date('insta_2fa')
    date_str_fb = await get_system_date('fb_2fa')
    date_str_cookie = await get_system_date('fb_cookie')
    
    insta_today = await db.tasks.count_documents({'date': date_str_insta, 'task_type': 'insta_2fa'})
    fb2fa_today = await db.tasks.count_documents({'date': date_str_fb, 'task_type': 'fb_2fa'})
    fbcookie_today = await db.tasks.count_documents({'date': date_str_cookie, 'task_type': 'fb_cookie'})
    
    insta_all = await db.tasks.count_documents({'task_type': 'insta_2fa'})
    fb2fa_all = await db.tasks.count_documents({'task_type': 'fb_2fa'})
    fbcookie_all = await db.tasks.count_documents({'task_type': 'fb_cookie'})
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn(f"Insta 2FA (আজ: {insta_today} | সব: {insta_all})",    "exp_type_insta_2fa",  "5334868205091459431", "success")],
        [ibtn(f"FB 2FA (আজ: {fb2fa_today} | সব: {fb2fa_all})",         "exp_type_fb_2fa",     "5334807341109908955", "primary")],
        [ibtn(f"FB Cookie (আজ: {fbcookie_today} | সব: {fbcookie_all})","exp_type_fb_cookie",  "5334807341109908955", "primary")],
        [ibtn("সব Export (আজকের)",                                                   "exp_type_all_today",  "5463352748751753567", "success")],
        [ibtn("বাতিল",                                                                "exp_cancel",          "5974083768233760323", "danger")],
    ])
    
    await callback.message.edit_text(
        f"📁 **Export Data**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 **আজকের তারিখসমূহ:**\n"
        f"  • Insta 2FA: `{date_str_insta}`\n"
        f"  • FB 2FA: `{date_str_fb}`\n"
        f"  • FB Cookie: `{date_str_cookie}`\n\n"
        f"কোন টাইপের ডেটা export করতে চান? 👇",
        reply_markup=kb, parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("exp_do_"), StateFilter("*"))
async def exp_do_export(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'export'): return
    
    # Parse: exp_do_{task_type}_{date}
    parts = callback.data.replace("exp_do_", "").rsplit("_", 1)
    if len(parts) == 2:
        # Handle task types with underscores (e.g., insta_2fa, fb_2fa, fb_cookie)
        date_part = parts[1]
        type_part = callback.data.replace("exp_do_", "").replace(f"_{date_part}", "", 1)
    else:
        await callback.answer("❌ Error parsing request")
        return
    
    # For 'all' date, the date_part could be 'all' or a date like '2026-07-07'
    # But since task_type has underscores, we need smarter parsing
    # Format: exp_do_insta_2fa_2026-07-07 or exp_do_insta_2fa_all
    # Let's parse from the known task types
    task_type = None
    date_str = None
    remaining = callback.data.replace("exp_do_", "")
    
    for tt in ['insta_2fa', 'fb_2fa', 'fb_cookie']:
        if remaining.startswith(tt + "_"):
            task_type = tt
            date_str = remaining[len(tt) + 1:]
            break
    
    if not task_type or not date_str:
        await callback.answer("❌ Error")
        return
    
    await callback.answer("⏳ Export হচ্ছে...")
    
    type_labels = {'insta_2fa': f'{PEB_INSTAGRAM} Insta 2FA', 'fb_2fa': f'{PEB_FACEBOOK} FB 2FA', 'fb_cookie': f'{PEB_FACEBOOK} FB Cookie'}
    label = type_labels.get(task_type, task_type)
    date_label = "সব তারিখ" if date_str == "all" else date_str
    
    file_path, count = await export_tasks_xlsx(date_str, task_type)
    
    if file_path and count > 0 and os.path.exists(file_path):
        await callback.message.answer_document(
            FSInputFile(file_path),
            caption=f"📊 **Export Complete**\n\n📌 Type: {label}\n📅 Date: {date_label}\n📋 Total: {count} accounts",
            parse_mode="Markdown"
        )
        await callback.message.edit_text(f"✅ {label} — {date_label} — {count} টি account export হয়েছে!")
    else:
        await callback.message.edit_text(f"❌ {label} — {date_label} — কোনো ডেটা পাওয়া যায়নি।")

@router.message(F.text == "Verify Accounts")
async def start_verify(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'verify'): return
    await message.answer("Select verification action:", reply_markup=get_verify_action_keyboard())

@router.callback_query(F.data.startswith("verify_action_"))
async def process_verify_action(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[2]
    await state.update_data(verify_action=action)
    await callback.message.delete()
    
    if action == "both":
        prompt = "Send a `.csv` file where Column 1 = OK IDs, Column 2 = BAD IDs."
    elif action == "approve":
        prompt = "✅ **Approve Accounts**\n\nSend a `.txt` file OR directly paste the usernames here (one per line):"
    elif action == "auto":
        prompt = "🔄 **Approve OK & Auto Reject Rest**\n\nSend a `.txt` file OR directly paste the **OK** usernames here (one per line).\n\n⚠️ **WARNING:** ALL other pending tasks will be automatically REJECTED!"
    else:
        prompt = "❌ **Reject Accounts**\n\nSend a `.txt` file OR directly paste the usernames here (one per line):"
        
    await callback.message.answer(prompt, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminVerifyStates.waiting_for_input)

@router.message(AdminVerifyStates.waiting_for_input)
async def process_verify_input(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    action = data.get('verify_action', 'both')
    
    valid_usernames = []
    bad_usernames = []
    
    if message.document:
        file_name = message.document.file_name
        if not (file_name.endswith('.txt') or file_name.endswith('.csv')):
            return await message.answer("⚠️ Please send a .txt or .csv file.")
            
        file_path = (await bot.get_file(message.document.file_id)).file_path
        download_path = f"temp_{message.from_user.id}_{file_name}"
        await bot.download_file(file_path, download_path)
        
        try:
            if file_name.endswith('.csv'):
                import csv
                with open(download_path, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if not row: continue
                        if row[0].strip().lower() in ['ok_ids', 'ok', 'good', 'username']: continue
                        
                        if action == "both":
                            if len(row) > 0 and row[0].strip():
                                valid_usernames.append(row[0].strip())
                            if len(row) > 1 and row[1].strip():
                                bad_usernames.append(row[1].strip())
                        elif action == "approve":
                            if len(row) > 0 and row[0].strip():
                                valid_usernames.append(row[0].strip())
                        elif action == "reject":
                            if len(row) > 0 and row[0].strip():
                                bad_usernames.append(row[0].strip())
            else:
                with open(download_path, 'r', encoding='utf-8') as f:
                    lines = [line.strip() for line in f if line.strip()]
                    if action == "approve" or action == "auto":
                        valid_usernames = lines
                    elif action == "reject":
                        bad_usernames = lines
                    else:
                        valid_usernames = lines
        finally:
            if os.path.exists(download_path): os.remove(download_path)
            
    elif message.text:
        if action == "both":
            await state.clear()
            return await message.answer("⚠️ For 'Both', you must upload a CSV file with 2 columns.")
            
        lines = [line.strip() for line in message.text.split('\n') if line.strip()]
        if action == "approve" or action == "auto":
            valid_usernames = lines
        elif action == "reject":
            bad_usernames = lines
            
    else:
        await state.clear()
        return await message.answer("⚠️ Please send text or a file.")
        
    try:
        accepted = await verify_and_credit_accounts(valid_usernames, bot) if valid_usernames else 0
        rejected = 0
        
        if action == "auto":
            rejected = await reject_all_pending_accounts(bot)
        elif bad_usernames:
            rejected = await reject_accounts(bad_usernames, bot)
            
        await message.answer(f"✅ {accepted} accounts credited.\n❌ {rejected} accounts rejected.", reply_markup=get_main_keyboard(is_admin(message.from_user.id), await get_user_lang(message.from_user.id)))
    except Exception as e:
        await message.answer(f"❌ Error: {e}")
        
    await state.clear()


@router.message(F.text == "Manage Admins", StateFilter("*"))
async def manage_admins(message: Message, state: FSMContext):
    await state.clear()
    if not is_primary_admin(message.from_user.id):
        return
    configured = "\n".join(
        f"• <code>{admin.telegram_id}</code> — {admin.username}"
        for admin in ADMINS
    )
    await message.answer(
        "👥 <b>Configured Admins</b>\n\n"
        f"{configured}\n\n"
        f"সর্বোচ্চ {len(ADMINS)}/{MAX_ADMINS} জন configured।\n"
        "নিচের বাটন ব্যবহার করে admin যোগ বা বাদ দিন।",
        reply_markup=get_admin_panel_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@router.message(F.text == "View Admins", StateFilter("*"))
async def view_admins(message: Message, state: FSMContext):
    await state.clear()
    if not is_primary_admin(message.from_user.id):
        return
    configured = "\n".join(
        f"• <code>{admin.telegram_id}</code> — {admin.username}"
        for admin in ADMINS
    )
    await message.answer(
        f"👥 <b>Current Admins</b>\n\n{configured}",
        parse_mode="HTML",
    )


@router.message(F.text == "Add Admin", StateFilter("*"))
async def add_admin_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_primary_admin(message.from_user.id):
        return
    if len(ADMINS) >= MAX_ADMINS:
        return await message.answer(
            f"সর্বোচ্চ {MAX_ADMINS} জন admin রাখা যাবে।"
        )
    await message.answer(
        "নতুন Admin-এর Telegram ID দিন।\n"
        "User ID জানতে সেই user-এর কাছ থেকে /start message forward করতে পারেন।",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(AdminManagementStates.waiting_for_admin_id)


@router.message(F.text == "Edit Admin", StateFilter("*"))
async def edit_admin_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_primary_admin(message.from_user.id):
        return
    await message.answer(
        "যে Admin-এর username পরিবর্তন করতে চান তার Telegram ID দিন।",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(AdminManagementStates.waiting_for_edit_admin_id)


@router.message(AdminManagementStates.waiting_for_edit_admin_id)
async def process_edit_admin_id(message: Message, state: FSMContext):
    if not is_primary_admin(message.from_user.id):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        return await message.answer("সঠিক Telegram ID দিন।")
    admin_id = int(raw_id)
    if not get_admin_by_id(admin_id):
        await state.clear()
        return await message.answer("এই user Admin তালিকায় নেই।")
    await state.update_data(edit_admin_id=admin_id)
    await message.answer(
        "নতুন Admin username দিন।",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(AdminManagementStates.waiting_for_edit_admin_username)


@router.message(AdminManagementStates.waiting_for_edit_admin_username)
async def process_edit_admin_username(message: Message, state: FSMContext):
    if not is_primary_admin(message.from_user.id):
        return
    username = (message.text or "").strip()
    if not username:
        return await message.answer("সঠিক username দিন।")
    username = username if username.startswith("@") else f"@{username}"
    data = await state.get_data()
    admin_id = int(data["edit_admin_id"])
    await raw_db.admins.update_one(
        {"tg_id": admin_id},
        {"$set": {"username": username}},
        upsert=True,
    )
    update_admin_cache(admin_id, username)
    await state.clear()
    await message.answer(
        f"✅ Admin username পরিবর্তন হয়েছে: <code>{admin_id}</code> — {username}",
        parse_mode="HTML",
    )


@router.message(AdminManagementStates.waiting_for_admin_id)
async def process_add_admin_id(message: Message, state: FSMContext):
    if not is_primary_admin(message.from_user.id):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        return await message.answer("সঠিক Telegram ID দিন।")
    admin_id = int(raw_id)
    if admin_id == PRIMARY_ADMIN_ID:
        await state.clear()
        return await message.answer("এই user ইতিমধ্যেই Main Admin।")
    if get_admin_by_id(admin_id):
        await state.clear()
        return await message.answer("এই user ইতিমধ্যেই Admin হিসেবে যুক্ত আছে।")
    await state.update_data(new_admin_id=admin_id)
    await message.answer(
        "নতুন Admin-এর Telegram username দিন।\n"
        "@ ছাড়া বা @-সহ—দুইভাবেই দিতে পারেন।",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(AdminManagementStates.waiting_for_admin_username)


@router.message(AdminManagementStates.waiting_for_admin_username)
async def process_add_admin_username(message: Message, state: FSMContext):
    if not is_primary_admin(message.from_user.id):
        return
    username = (message.text or "").strip()
    if not username:
        return await message.answer("সঠিক username দিন।")
    data = await state.get_data()
    await state.update_data(
        new_admin_username=username if username.startswith("@") else f"@{username}",
        new_admin_perms=[],
    )
    await message.answer(
        "এই Admin-এর permissions বেছে নিন। তারপর Save চাপুন।",
        reply_markup=get_permissions_keyboard([]),
    )
    await state.set_state(AdminManagementStates.waiting_for_admin_permissions)


@router.callback_query(
    F.data.startswith("toggle_perm_"),
    AdminManagementStates.waiting_for_admin_permissions,
)
async def toggle_admin_perm(callback: CallbackQuery, state: FSMContext):
    if not is_primary_admin(callback.from_user.id):
        return
    perm = callback.data.replace("toggle_perm_", "")
    data = await state.get_data()
    perms = list(data.get("new_admin_perms", []))
    if perm in perms:
        perms.remove(perm)
    else:
        perms.append(perm)
    await state.update_data(new_admin_perms=perms)
    await callback.message.edit_reply_markup(
        reply_markup=get_permissions_keyboard(perms)
    )
    await callback.answer()


@router.callback_query(
    F.data == "save_admin_perms",
    AdminManagementStates.waiting_for_admin_permissions,
)
async def save_admin_perms(callback: CallbackQuery, state: FSMContext):
    if not is_primary_admin(callback.from_user.id):
        return
    data = await state.get_data()
    admin_id = int(data["new_admin_id"])
    username = data["new_admin_username"]
    permissions = list(data.get("new_admin_perms", []))
    if len(ADMINS) >= MAX_ADMINS and not get_admin_by_id(admin_id):
        await state.clear()
        return await callback.message.edit_text(
            f"সর্বোচ্চ {MAX_ADMINS} জন admin রাখা যাবে।"
        )
    await raw_db.admins.update_one(
        {"tg_id": admin_id},
        {"$set": {
            "tg_id": admin_id,
            "username": username,
            "permissions": permissions,
        }},
        upsert=True,
    )
    await initialize_admin_settings(admin_id)
    ADMIN_PERMISSIONS[str(admin_id)] = permissions
    update_admin_cache(admin_id, username)
    await state.clear()
    await callback.answer("Admin যুক্ত হয়েছে।")
    await callback.message.edit_text(
        f"✅ Admin যুক্ত হয়েছে: <code>{admin_id}</code> — {username}",
        parse_mode="HTML",
    )


@router.message(F.text == "Remove Admin", StateFilter("*"))
async def remove_admin_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_primary_admin(message.from_user.id):
        return
    await message.answer(
        "যে Admin-কে বাদ দিতে চান তার Telegram ID দিন।",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(AdminManagementStates.waiting_for_remove_admin_id)


@router.message(AdminManagementStates.waiting_for_remove_admin_id)
async def process_remove_admin_id(message: Message, state: FSMContext):
    if not is_primary_admin(message.from_user.id):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        return await message.answer("সঠিক Telegram ID দিন।")
    admin_id = int(raw_id)
    if admin_id == PRIMARY_ADMIN_ID:
        await state.clear()
        return await message.answer("Main Admin-কে বাদ দেওয়া যাবে না।")
    if not get_admin_by_id(admin_id):
        await state.clear()
        return await message.answer("এই user Admin তালিকায় নেই।")
    await raw_db.admins.delete_one({"tg_id": admin_id})
    ADMIN_PERMISSIONS.pop(str(admin_id), None)
    update_admin_cache(admin_id, remove=True)
    await state.clear()
    await message.answer(f"✅ Admin বাদ দেওয়া হয়েছে: {admin_id}")


@router.message(F.text == "Leave Admin", StateFilter("*"))
async def leave_admin_role(message: Message, state: FSMContext):
    await state.clear()
    if is_primary_admin(message.from_user.id):
        return await message.answer("Main Admin role ছাড়তে পারবেন না।")
    if not get_admin_by_id(message.from_user.id):
        return
    await raw_db.admins.delete_one({"tg_id": message.from_user.id})
    ADMIN_PERMISSIONS.pop(str(message.from_user.id), None)
    update_admin_cache(message.from_user.id, remove=True)
    await message.answer(
        "✅ আপনার Admin role সরানো হয়েছে।",
        reply_markup=get_main_keyboard(False, await get_user_lang(message.from_user.id)),
    )


@router.message(F.text == "Toggle Join Bonus", StateFilter("*"))
async def toggle_join_bonus(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_setting('join_bonus_status') or 'off'
    new_st = 'on' if current == 'off' else 'off'
    await db.settings.update_one({'key': 'join_bonus_status'}, {'$set': {'value': new_st}}, upsert=True)
    await message.answer(f"✅ Join Bonus is now **{'ON' if new_st=='on' else 'OFF'}**.", parse_mode="Markdown")

@router.message(F.text == "Toggle Refer Income", StateFilter("*"))
async def toggle_refer_income(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_setting('refer_income_status') or 'on'
    new_st = 'off' if current == 'on' else 'on'
    await update_setting('refer_income_status', new_st)
    status_text = '🟢 **ON**' if new_st == 'on' else '🔴 **OFF**'
    await message.answer(f"✅ Refer Income is now {status_text}.", parse_mode="Markdown")

@router.message(F.text == "Change Join Bonus", StateFilter("*"))
async def change_join_bonus_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_setting('join_bonus_amount') or 0
    await message.answer(f"Current Join Bonus Amount: `{current}` à§³\n\nSend the new amount for Join Bonus (e.g. 5):", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminSettingsStates.waiting_for_join_bonus_amount)

@router.message(AdminSettingsStates.waiting_for_join_bonus_amount)
async def process_join_bonus_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    try:
        new_amt = float(message.text.strip())
        if new_amt < 0: raise ValueError
        await db.settings.update_one({'key': 'join_bonus_amount'}, {'$set': {'value': new_amt}}, upsert=True)
        await message.answer(f"✅ Join Bonus Amount successfully updated to `{new_amt}` ৳.", reply_markup=get_settings_keyboard(), parse_mode="Markdown")
        await state.clear()
    except ValueError:
        await message.answer("⚠️ Please enter a valid number (e.g., 5 or 5.5). Try again:")

@router.message(F.text == "Settings")
async def admin_settings(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    insta_price = await get_setting('task_price')
    fb_2fa_price = await get_setting('fb_2fa_price')
    fb_cookie_price = await get_setting('fb_cookie_price')
    pw_mode = await get_setting('password_mode')
    min_wd = await get_setting('min_withdraw_amount')
    wd_status = await get_setting('withdraw_status')
    text = f"⚙️ **Settings:**\n💵 Insta Price: `{insta_price} TK`\n💵 FB 2FA Price: `{fb_2fa_price} TK`\n💵 FB Cookie Price: `{fb_cookie_price} TK`\n💰 Min WD: `{min_wd} TK`\n💳 WD: `{wd_status.upper()}`\n🔐 Mode: `{pw_mode}`"
    await message.answer(text, reply_markup=get_settings_keyboard(), parse_mode="Markdown")

@router.message(F.text == "Change Insta Price")
async def change_price_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new Insta price:", reply_markup=get_cancel_keyboard())
    await state.update_data(price_setting_key="task_price", price_label="Instagram")
    await state.set_state(AdminStates.waiting_for_new_price)

@router.message(AdminStates.waiting_for_new_price)
async def process_new_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'):
        return
    data = await state.get_data()
    price_key = data.get("price_setting_key", "task_price")
    price_label = data.get("price_label", "Instagram")
    try:
        price = float(message.text.strip())
        if price < 0:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        return await message.answer("⚠️ Please enter a valid non-negative number.")
    await update_setting(price_key, message.text.strip())
    await message.answer(f"✅ {price_label} price updated to {price:g} BDT.", reply_markup=get_settings_keyboard())
    await state.clear()

@router.message(F.text == "Change FB 2FA Price")
async def change_fb_2fa_price_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new FB 2FA price:", reply_markup=get_cancel_keyboard())
    await state.update_data(price_setting_key="fb_2fa_price", price_label="Facebook 2FA")
    await state.set_state(AdminStates.waiting_for_new_price)

@router.message(F.text == "Change FB Cookie Price")
async def change_fb_cookie_price_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new FB Cookie price:", reply_markup=get_cancel_keyboard())
    await state.update_data(price_setting_key="fb_cookie_price", price_label="Facebook Cookie")
    await state.set_state(AdminStates.waiting_for_new_price)



@router.message(F.text == "Change Refer %")
async def change_refer_percent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new referral reward percentage (e.g. 10 for 10%):", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminSettingsStates.waiting_for_refer_percent)

@router.message(AdminSettingsStates.waiting_for_refer_percent)
async def process_refer_percent(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("⚠️ Number only.")
    await update_setting('refer_percentage', message.text)
    await message.answer("✅ Updated!", reply_markup=get_settings_keyboard())
    await state.clear()




async def parse_and_save_time(message: Message, state: FSMContext, key_name: str, label: str):
    time_str = message.text.strip().upper()
    # Clean up: remove extra spaces, dots to colons
    time_str = time_str.replace(".", ":").replace(" ", "")
    parsed_time = None
    
    # Try multiple formats
    formats_to_try = []
    if "AM" in time_str or "PM" in time_str:
        formats_to_try = ["%I:%M%p", "%I%p", "%I:%M %p", "%I %p"]
    else:
        formats_to_try = ["%H:%M", "%H"]
    
    for fmt in formats_to_try:
        try:
            parsed_time = datetime.strptime(time_str, fmt)
            break
        except ValueError:
            continue
            
    if parsed_time:
        db_time = parsed_time.strftime("%H:%M")
        await update_setting(key_name, db_time)
        display_time = parsed_time.strftime("%I:%M %p")
        await message.answer(
            f"✅ **{label}** সময় সেট হয়েছে: **{display_time}** (BD Time)\n\n"
            f"📌 প্রতিদিন এই সময়ে auto export হবে।",
            reply_markup=get_settings_keyboard(), parse_mode="Markdown"
        )
        await state.clear()
    else:
        await message.answer(
            "⚠️ **ভুল ফরম্যাট!**\n\n"
            "✅ সঠিক উদাহরণ:\n"
            "• `8PM` বা `8 PM`\n"
            "• `8:30 PM` বা `8:30PM`\n"
            "• `20:00` (24hr ফরম্যাট)\n\n"
            "আবার চেষ্টা করুন:", parse_mode="Markdown"
        )

async def get_current_export_time_display(key_name):
    """Get current export time in 12-hour AM/PM format"""
    time_val = await get_setting(key_name)
    if not time_val:
        return "Not Set"
    try:
        t = datetime.strptime(time_val, "%H:%M")
        return t.strftime("%I:%M %p")
    except:
        return time_val

@router.message(F.text == "Auto Export (Insta)")
async def set_export_insta(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_current_export_time_display('auto_export_time')
    await message.answer(
        f"🕒 **Insta 2FA Auto Export Time**\n\n"
        f"📌 বর্তমান সময়: **{current}**\n\n"
        f"নতুন সময় লিখুন:\n"
        f"• `8PM` / `8:30 PM` (12hr)\n"
        f"• `20:00` (24hr)",
        reply_markup=get_cancel_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(AdminExportStates.waiting_for_auto_export_insta)

@router.message(AdminExportStates.waiting_for_auto_export_insta)
async def process_export_insta(message: Message, state: FSMContext):
    await parse_and_save_time(message, state, 'auto_export_time', 'Insta 2FA Export')

@router.message(F.text == "Auto Export (FB 2FA)")
async def set_export_fb_2fa(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_current_export_time_display('auto_export_time_fb_2fa')
    await message.answer(
        f"🕒 **FB 2FA Auto Export Time**\n\n"
        f"📌 বর্তমান সময়: **{current}**\n\n"
        f"নতুন সময় লিখুন:\n"
        f"• `9PM` / `9:30 PM` (12hr)\n"
        f"• `21:00` (24hr)",
        reply_markup=get_cancel_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(AdminExportStates.waiting_for_auto_export_fb_2fa)

@router.message(AdminExportStates.waiting_for_auto_export_fb_2fa)
async def process_export_fb_2fa(message: Message, state: FSMContext):
    await parse_and_save_time(message, state, 'auto_export_time_fb_2fa', 'FB 2FA Export')

@router.message(F.text == "Auto Export (Cookie)")
async def set_export_fb_cookie(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_current_export_time_display('auto_export_time_fb_cookie')
    await message.answer(
        f"🕒 **FB Cookie Auto Export Time**\n\n"
        f"📌 বর্তমান সময়: **{current}**\n\n"
        f"নতুন সময় লিখুন:\n"
        f"• `10PM` / `10:30 PM` (12hr)\n"
        f"• `22:00` (24hr)",
        reply_markup=get_cancel_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(AdminExportStates.waiting_for_auto_export_fb_cookie)

@router.message(AdminExportStates.waiting_for_auto_export_fb_cookie)
async def process_export_fb_cookie(message: Message, state: FSMContext):
    await parse_and_save_time(message, state, 'auto_export_time_fb_cookie', 'FB Cookie Export')


@router.message(F.text == "Set Support Link")
async def set_support_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Please enter the support username (e.g., @admin_support or https://t.me/admin_support):", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminSettingsStates.waiting_for_support_link)

@router.message(AdminSettingsStates.waiting_for_support_link)
async def process_support_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    link = message.text.strip()
    if link.startswith('@'):
        link = f"https://t.me/{link[1:]}"
    await update_setting('support_link', link)
    await state.clear()
    await message.answer("🎧 Support Link Updated!", reply_markup=get_settings_keyboard())



def get_admin_video_link_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Instagram 2FA ভিডিও লিংক",    "admin_setvidlink_insta_2fa",    "5334868205091459431", "success")],
        [ibtn("Instagram Cookies ভিডিও লিংক","admin_setvidlink_insta_cookie", "5334868205091459431", "success")],
        [ibtn("Facebook 2FA ভিডিও লিংক",      "admin_setvidlink_fb_2fa",       "5334807341109908955", "primary")],
        [ibtn("Facebook Cookies ভিডিও লিংক",  "admin_setvidlink_fb_cookie",    "5334807341109908955", "primary")],
    ])

_ADMIN_VIDEO_LINK_MAP = {
    "admin_setvidlink_insta_2fa":    ("Instagram 2FA",      "video_link_insta_2fa"),
    "admin_setvidlink_insta_cookie": ("Instagram Cookies",  "video_link_insta_cookie"),
    "admin_setvidlink_fb_2fa":       ("Facebook 2FA",       "video_link_fb_2fa"),
    "admin_setvidlink_fb_cookie":    ("Facebook Cookies",   "video_link_fb_cookie"),
}

@router.message(F.text == "কাজের ভিডিও")
async def admin_video_link_menu(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await state.clear()
    await message.answer(
        "🎥 <b>কাজের ভিডিও লিংক সেট করুন</b>\n\nকোন কাজের ভিডিও লিংক সেট করতে চান?",
        reply_markup=get_admin_video_link_inline(),
        parse_mode="HTML"
    )

@router.callback_query(F.data.in_(set(_ADMIN_VIDEO_LINK_MAP.keys())))
async def admin_video_link_select(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'settings'):
        return await callback.answer("❌ Permission denied.")
    name, setting_key = _ADMIN_VIDEO_LINK_MAP[callback.data]
    current = await get_setting(setting_key) or "সেট করা হয়নি"
    await state.update_data(video_link_key=setting_key, video_link_name=name)
    await state.set_state(AdminSettingsStates.waiting_for_video_link)
    await callback.message.answer(
        f"🎥 <b>{name} ভিডিও লিংক</b>\n\n"
        f"📌 বর্তমান লিংক: <code>{current}</code>\n\n"
        f"নতুন ভিডিও লিংক পাঠান (YouTube/Drive লিংক):",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AdminSettingsStates.waiting_for_video_link)
async def process_video_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    data = await state.get_data()
    setting_key = data.get('video_link_key')
    name = data.get('video_link_name')
    link = message.text.strip()
    await update_setting(setting_key, link)
    await state.clear()
    await message.answer(
        f"✅ <b>{name} ভিডিও লিংক সফলভাবে সেট হয়েছে!</b>\n\n🔗 {link}",
        reply_markup=get_settings_keyboard(),
        parse_mode="HTML"
    )

@router.message(F.text == "Set Date")
async def set_manual_date_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    
    current_insta = await get_setting('manual_date_override_insta_2fa')
    current_fb2fa = await get_setting('manual_date_override_fb_2fa')
    current_cookie = await get_setting('manual_date_override_fb_cookie')
    current_global = await get_setting('manual_date_override')
    
    status = f"Global: `{current_global}`\nInsta 2FA: `{current_insta}`\nFB 2FA: `{current_fb2fa}`\nFB Cookie: `{current_cookie}`"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Insta 2FA",  "set_date_type_insta_2fa",  "5334868205091459431", "success"),
         ibtn("FB 2FA",      "set_date_type_fb_2fa",     "5334807341109908955", "primary")],
        [ibtn("FB Cookie",   "set_date_type_fb_cookie",  "5334807341109908955", "primary"),
         ibtn("Global/All",               "set_date_type_global",     "5463352748751753567", "success")],
        [ibtn("Cancel",                   "set_date_cancel",           "5974083768233760323", "danger")],
    ])
    
    await message.answer(
        f"📅 **Set System Date**\n\n"
        f"**Current Dates:**\n{status}\n\n"
        f"Select which task type you want to set the date for 👇",
        reply_markup=kb, parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("set_date_type_"))
async def process_set_date_type(callback: CallbackQuery, state: FSMContext):
    type_map = {
        'set_date_type_insta_2fa': ('insta_2fa', 'Insta 2FA'),
        'set_date_type_fb_2fa': ('fb_2fa', 'FB 2FA'),
        'set_date_type_fb_cookie': ('fb_cookie', 'FB Cookie'),
        'set_date_type_global': ('global', 'Global/All')
    }
    
    task_key, task_name = type_map[callback.data]
    await state.update_data(target_date_type=task_key)
    
    await callback.message.edit_text(
        f"📅 **Set Date for {task_name}**\n\n"
        f"Enter the new date in exactly `YYYY-MM-DD` format (e.g., `2026-07-15`).",
        parse_mode="Markdown"
    )
    await state.set_state(AdminSettingsStates.waiting_for_manual_date)
    await callback.answer()

@router.callback_query(F.data == "set_date_cancel")
async def set_date_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.clear()
    await callback.answer()

@router.message(AdminSettingsStates.waiting_for_manual_date)
async def process_manual_date(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get('target_date_type', 'global')
    
    try:
        # validate format
        datetime.strptime(message.text.strip(), "%Y-%m-%d")
        
        db_key = 'manual_date_override' if target == 'global' else f'manual_date_override_{target}'
        await update_setting(db_key, message.text.strip())
        
        await message.answer(f"✅ Date for `{target}` set to `{message.text.strip()}`.", reply_markup=get_settings_keyboard(), parse_mode="Markdown")
        await state.clear()
    except ValueError:
        await message.answer("❌ Invalid format. Please use exactly `YYYY-MM-DD` (e.g., `2026-07-15`).", parse_mode="Markdown")


@router.message(F.text == "Set Payout Group")
async def set_payout_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_setting('payout_group_id')
    await message.answer(f"Enter the Payout Group/Channel ID (e.g., -100123456789) where payment proofs will be posted.\nCurrent: `{current}`", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminSettingsStates.waiting_for_payout_channel)

@router.message(AdminSettingsStates.waiting_for_payout_channel)
async def process_payout_channel(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    payout_id = message.text.strip()
    
    if payout_id.lower() in ['none', '0', 'off', 'disable', 'cancel', 'no']:
        await update_setting('payout_group_id', '')
        await message.answer("✅ Payout Group Disabled!", reply_markup=get_settings_keyboard())
        await state.clear()
        return

    try:
        # Check if bot can access the group
        chat = await bot.get_chat(payout_id)
        if not chat.username:
            # Need to export link if no username, also checks admin rights
            await bot.export_chat_invite_link(payout_id)
        await update_setting('payout_group_id', payout_id)
        await message.answer("✅ Payout Group Updated successfully! Users can now join it.", reply_markup=get_settings_keyboard())
        await state.clear()
    except Exception as e:
        await message.answer(f"⚠️ **Error:** Could not verify Payout Group.\n\nMake sure the bot is an **Admin** in the group/channel and the ID is correct. Error details: `{e}`\n\nPlease try again or type `0` to disable.", parse_mode="Markdown")
@router.message(F.text == "Set Channel ID")
async def change_channel_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Please enter the new Channel ID (e.g., -100123456789 or @username):", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminSettingsStates.waiting_for_channel_id)

@router.message(AdminSettingsStates.waiting_for_channel_id)
async def process_channel_id(message: Message, bot: Bot, state: FSMContext):
    channel_id = message.text.strip()
    
    if channel_id.lower() in ['none', '0', 'off', 'disable', 'cancel', 'no']:
        await update_setting('channel_id', '')
        await message.answer("✅ Channel Requirement Disabled!", reply_markup=get_settings_keyboard())
        await state.clear()
        return

    try:
        chat = await bot.get_chat(channel_id)
        if not chat.username:
            await bot.export_chat_invite_link(channel_id)
        await update_setting('channel_id', channel_id)
        await message.answer("✅ Channel ID Updated successfully!", reply_markup=get_settings_keyboard())
        await state.clear()
    except Exception as e:
        await message.answer(f"⚠️ **Error:** Could not verify Channel.\n\nMake sure the bot is an **Admin** in the channel and the ID is correct. Error details: `{e}`\n\nPlease try again or type `0` to disable.", parse_mode="Markdown")

@router.message(F.text == "Set Channel Link")
async def change_channel_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Please enter the new Channel Link (e.g., https://t.me/yourchannel):", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminSettingsStates.waiting_for_channel_link)

@router.message(AdminSettingsStates.waiting_for_channel_link)
async def process_channel_link(message: Message, state: FSMContext):
    await update_setting('channel_link', message.text.strip())
    await message.answer("✅ Channel Link Updated!", reply_markup=get_settings_keyboard())
    await state.clear()

async def get_admin_tutorial_task_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Insta 2FA",  "admin_tut_insta_2fa", "5334868205091459431", "success")],
        [ibtn("FB 2FA",      "admin_tut_fb_2fa",    "5334807341109908955", "primary")],
        [ibtn("FB Cookie",   "admin_tut_fb_cookie", "5334807341109908955", "primary")],
    ])

@router.message(F.text == "Set Tutorial")
async def set_tutorial_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Select a task to manage tutorials:", reply_markup=await get_admin_tutorial_task_keyboard())

@router.callback_query(F.data.startswith("admin_tut_"))
async def admin_tut_manage(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id, 'settings'): return
    if callback.data == "admin_tut_back":
        return await callback.message.edit_text("Select a task to manage tutorials:", reply_markup=await get_admin_tutorial_task_keyboard())
    
    task_key = callback.data.replace("admin_tut_", "")
    
    cursor = db.tutorials.find({'task_type': task_key}).sort('order', 1)
    items = await cursor.to_list(length=100)
    
    text = f"<b>Tutorials for {task_key}</b>\n\n"
    if not items:
        text += "No tutorials set yet.\n"
    
    kb_buttons = []
    for idx, item in enumerate(items):
        text += f"{idx+1}. Type: {item['msg_type']}\n"
        kb_buttons.append([ibtn(f"\U0001f5d1 Delete {idx+1}", f"del_tut_{item['_id']}", style="danger")])
        
    kb_buttons.append([ibtn("\u2795 Add New Message", f"add_tut_{task_key}", style="success")])
    kb_buttons.append([ibtn("\U0001f519 Back", "admin_tut_back", "5420145051336485498", "primary")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("del_tut_"))
async def del_tut_item(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id, 'settings'): return
    tut_id = callback.data.replace("del_tut_", "")
    from bson import ObjectId
    await db.tutorials.delete_one({'_id': ObjectId(tut_id)})
    await callback.answer("\u2705 Tutorial deleted!", show_alert=True)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await bot.send_message(chat_id=callback.from_user.id, text="Select a task to manage tutorials:", reply_markup=await get_admin_tutorial_task_keyboard())

@router.callback_query(F.data.startswith("add_tut_"))
async def add_tut_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'settings'): return
    task_key = callback.data.replace("add_tut_", "")
    await state.update_data(tut_task_key=task_key)
    await state.set_state(AdminSettingsStates.waiting_for_tutorial_msg)
    await callback.message.answer("Send the tutorial message (Text, Photo, or Video).", reply_markup=get_cancel_keyboard())

@router.message(AdminSettingsStates.waiting_for_tutorial_msg, F.content_type.in_({ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO}))
async def process_tutorial_msg(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    data = await state.get_data()
    task_key = data.get('tut_task_key')
    
    msg_type = ''
    content = ''
    caption = ''
    if message.text:
        msg_type = 'text'
        content = message.text
    elif message.photo:
        msg_type = 'photo'
        content = message.photo[-1].file_id
        caption = message.caption or ''
    elif message.video:
        msg_type = 'video'
        content = message.video.file_id
        caption = message.caption or ''
        
    order = await db.tutorials.count_documents({'task_type': task_key})
    
    await db.tutorials.insert_one({
        'task_type': task_key,
        'msg_type': msg_type,
        'content': content,
        'caption': caption,
        'order': order
    })
    
    await state.clear()
    await message.answer("✅ Tutorial message added!", reply_markup=get_settings_keyboard())

@router.message(F.text == "Change Min Withdraw")
async def change_min_wd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new min withdraw:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminStates.waiting_for_min_withdraw)

@router.message(AdminStates.waiting_for_min_withdraw)
async def process_new_min_wd(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("⚠️ Number only.")
    await update_setting('min_withdraw_amount', message.text)
    await message.answer("✅ Updated!", reply_markup=get_settings_keyboard())
    await state.clear()

@router.message(F.text == "Change Binance Rate")
async def change_binance_rate_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new Binance rate (e.g. 122):", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminStates.waiting_for_binance_rate)

@router.message(AdminStates.waiting_for_binance_rate)
async def process_new_binance_rate(message: Message, state: FSMContext):
    try:
        float(message.text)
    except ValueError:
        return await message.answer("⚠️ Number only.")
    await update_setting('binance_rate', message.text)
    await message.answer("✅ Binance Rate Updated!", reply_markup=get_settings_keyboard())
    await state.clear()

@router.message(F.text == "Toggle Insta Screenshot")
async def toggle_insta_screenshot(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    current = await get_setting('insta_screenshot_status') or 'on'
    new_status = 'off' if current == 'on' else 'on'
    await update_setting('insta_screenshot_status', new_status)
    await message.answer(f"✅ Insta Screenshot requirement is now **{new_status.upper()}**")

@router.message(F.text == "Toggle Tasks")
async def toggle_tasks_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Select a task to toggle ON/OFF:", reply_markup=await get_tasks_toggle_inline())

@router.callback_query(F.data.startswith("toggle_tsk_"))
async def process_toggle_task(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'settings'): return
    task_key = callback.data.replace("toggle_tsk_", "")
    db_key = f"task_status_{task_key}"
    current = await get_setting(db_key) or 'on'
    new_status = 'off' if current == 'on' else 'on'
    await update_setting(db_key, new_status)
    await callback.message.edit_text("Select a task to toggle ON/OFF:", reply_markup=await get_tasks_toggle_inline())

async def get_payment_methods_toggle_inline():
    bkash_status = await get_setting('pm_bkash') or 'on'
    nagad_status = await get_setting('pm_nagad') or 'on'
    rocket_status = await get_setting('pm_rocket') or 'on'
    binance_status = await get_setting('pm_binance') or 'on'
    bep20_status = await get_setting('pm_bep20') or 'on'

    def _ts(s): return "success" if s == 'on' else "danger"
    return InlineKeyboardMarkup(inline_keyboard=[
        [ibtn(f"bKash: {' ON' if bkash_status == 'on' else ' OFF'}",     "toggle_pm_bkash",   "5348469219761626211", _ts(bkash_status))],
        [ibtn(f"Nagad: {' ON' if nagad_status == 'on' else ' OFF'}",       "toggle_pm_nagad",   "5346042941196507141", _ts(nagad_status))],
        [ibtn(f"Rocket: {' ON' if rocket_status == 'on' else ' OFF'}",    "toggle_pm_rocket",  "5346042941196507141", _ts(rocket_status))],
        [ibtn(f"Binance: {' ON' if binance_status == 'on' else ' OFF'}", "toggle_pm_binance", "5348212415077064131", _ts(binance_status))],
        [ibtn(f"BEP20 (USDT): {' ON' if bep20_status == 'on' else ' OFF'}", "toggle_pm_bep20", "5348212415077064131", _ts(bep20_status))],
    ])

@router.message(F.text == "Toggle Payment Methods")
async def toggle_pm_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Select a payment method to toggle ON/OFF:", reply_markup=await get_payment_methods_toggle_inline())

@router.callback_query(F.data.startswith("toggle_pm_"))
async def process_toggle_pm(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id, 'settings'): return
    pm_key = callback.data.replace("toggle_", "") # e.g. pm_bkash
    current = await get_setting(pm_key) or 'on'
    new_status = 'off' if current == 'on' else 'on'
    await update_setting(pm_key, new_status)
    await callback.message.edit_text("Select a payment method to toggle ON/OFF:", reply_markup=await get_payment_methods_toggle_inline())

@router.message(F.text == "Toggle Withdraw")
async def toggle_withdraw(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    new_status = 'off' if await get_setting('withdraw_status') == 'on' else 'on'
    await update_setting('withdraw_status', new_status)
    await message.answer(f"✅ Withdrawals are **{new_status.upper()}**")

@router.message(F.text == "Toggle W/D Force Join")
async def toggle_withdraw_force_join(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    new_status = 'off' if await get_setting('withdraw_force_join') == 'on' else 'on'
    await update_setting('withdraw_force_join', new_status)
    await message.answer(f"✅ Withdraw Force Join is **{new_status.upper()}**")

@router.message(F.text == "Set Password Mode")
async def set_pw_mode(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'settings'): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Insta 2FA",  "pw_task_insta_2fa", "5334868205091459431", "success")],
        [ibtn("FB 2FA",      "pw_task_fb_2fa",    "5334807341109908955", "primary")],
        [ibtn("FB Cookie",   "pw_task_fb_cookie", "5334807341109908955", "primary")],
    ])
    await message.answer("কোন টাস্কের Password Mode সেট করতে চান?", reply_markup=kb)

@router.callback_query(F.data.startswith("pw_task_"))
async def pw_task_select(callback: CallbackQuery, state: FSMContext):
    task_type = callback.data.replace("pw_task_", "")
    await state.update_data(pw_task_type=task_type)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Random (Hard)", "set_pw_random", "5267421176841398765", "primary")],
        [ibtn("Fixed",         "set_pw_fixed",  "5337132498965010628", "success")],
    ])
    await callback.message.edit_text(f"Select Mode for `{task_type}`:", reply_markup=kb)

@router.callback_query(F.data.startswith("set_pw_"))
async def process_pw_mode(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_type = data.get('pw_task_type', 'insta_2fa')
    mode = callback.data.split("_")[2]
    
    await update_setting(f'password_mode_{task_type}', mode)
    
    if mode == "random":
        await callback.message.edit_text(f"✅ `{task_type}` password mode set to Random.")
    else:
        await callback.message.delete()
        await callback.message.answer(f"Enter fixed password for `{task_type}`:", reply_markup=get_cancel_keyboard())
        await state.set_state(AdminStates.waiting_for_fixed_password)

@router.message(AdminStates.waiting_for_fixed_password)
async def process_fixed_pw(message: Message, state: FSMContext):
    data = await state.get_data()
    task_type = data.get('pw_task_type', 'insta_2fa')
    
    await update_setting(f'fixed_password_{task_type}', message.text)
    await message.answer(f"✅ Updated fixed password for `{task_type}`!", reply_markup=get_settings_keyboard())
    await state.clear()

@router.message(F.text == "Change Start Msg")
async def change_start_msg(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'settings'): return
    await message.answer("Enter new /start msg:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminStates.waiting_for_start_msg)

@router.message(AdminStates.waiting_for_start_msg)
async def process_start_msg(message: Message, state: FSMContext):
    await update_setting('start_message', message.text)
    await message.answer("✅ Updated!", reply_markup=get_settings_keyboard())
    await state.clear()

@router.callback_query(F.data.startswith("wd_approve_") | F.data.startswith("wd_reject_"), StateFilter("*"))
async def process_withdraw_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    parts = callback.data.split("_")
    action, req_id, user_id = parts[1], parts[2], parts[3]
    req = await get_withdraw_request(req_id)
    if not req or str(req[1]) != str(user_id):
        await callback.message.edit_reply_markup(reply_markup=None)
        return await callback.message.reply(
            "❌ এই withdrawal request আপনার admin scope-এর মধ্যে নেই বা আর valid নয়।"
        )
    
    if action == "reject":
        await update_withdraw_status(req_id, "rejected")
        await update_balance(int(user_id), req[4])
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("❌ Rejected and refunded.")
        try: await bot.send_message(chat_id=user_id, text="❌ Withdraw rejected, refunded.")
        except: pass
    elif action == "approve":
        await state.update_data(approve_req_id=req_id, approve_user_id=user_id)
        await callback.message.delete()
        
        req = await get_withdraw_request(req_id)
        number = req[3] if req else "Unknown"
        amount = req[4] if req else "Unknown"
        method = req[2] if req else "Unknown"
        
        try:
            user_chat = await bot.get_chat(user_id)
            user_name = user_chat.full_name
            user_name = user_name.replace('<', '&lt;').replace('>', '&gt;')
        except:
            user_name = "Unknown"
            
        await callback.message.answer(
            f"✅ <b>Approve Selected!</b>\n\n"
            f"👤 <b>User:</b> {user_name} (ID: <code>{user_id}</code>)\n"
            f"💳 <b>Method:</b> {method}\n"
            f"📱 <b>Number:</b> <code>{number}</code>\n"
            f"💰 <b>Amount to Send:</b> <code>{amount} TK</code>\n\n"
            f"📸 অনুগ্রহ করে উপরের নাম্বারে টাকা পাঠিয়ে এখানে <b>পেমেন্ট স্ক্রিনশট</b> আপলোড করুন।",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_withdraw_screenshot)

@router.message(AdminStates.waiting_for_withdraw_screenshot, F.photo)
async def process_withdraw_screenshot(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id, user_id = data['approve_req_id'], data['approve_user_id']
    await update_withdraw_status(req_id, "approved")
    req = await get_withdraw_request(req_id)
    amount = req[4] if req else "Unknown"
    
    user_doc = await db.users.find_one({'tg_id': int(user_id)})
    try:
        user_chat = await bot.get_chat(user_id)
        full_name = user_chat.full_name
    except:
        full_name = 'Unknown'
    balance = user_doc.get('balance', 0) if user_doc else 0
    
    withdraw_count = await db.withdraws.count_documents({'tg_id': int(user_id), 'status': 'approved'})
    
    cursor = db.tasks.aggregate([
        {'$match': {'tg_id': int(user_id), 'status': 'accepted'}},
        {'$group': {'_id': '$task_type', 'count': {'$sum': 1}}}
    ])
    task_counts = {}
    async for doc in cursor:
        task_counts[doc['_id']] = doc['count']
        
    task_text = ""
    for t_type, t_count in task_counts.items():
        if not t_type or t_type == 'insta_2fa': task_text += f"- Insta 2FA: {t_count}\n"
        elif t_type == 'fb_2fa': task_text += f"- FB 2FA: {t_count}\n"
        elif t_type == 'fb_cookie': task_text += f"- FB Cookie: {t_count}\n"
        else: task_text += f"- {t_type}: {t_count}\n"
    if not task_text: task_text = "N/A\n"
        
    number = req[3] if req else "Unknown"
    method = req[2] if req else "Unknown"
    masked_number = number[:3] + "*****" + number[-2:] if len(number) > 5 else "*****"

    user_caption = (
        f"✅ <b>Payment Complete!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Sent Number:</b> <code>{number}</code>\n"
        f"💸 <b>Amount Received:</b> <code>{amount} TK</code>\n"
        f"💰 <b>Current Balance:</b> <code>{balance} TK</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>Keep working and earning!</i>"
    )
    
    bot_username = (await bot.me()).username
    share_msg = "💸 আমি কাজ করে পেমেন্ট পেলাম!\n\nআপনিও চাইলে খুব সহজেই প্রতিদিন **Instagram 2FA Account** জমা দিয়ে পেমেন্ট নিতে পারবেন 💯\n\n👇 এখনই জয়েন করুন এবং কাজ শুরু করুন:"
    encoded_text = __import__('urllib').parse.quote(share_msg)
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_username}?start={user_id}&text={encoded_text}"
    start_work_url = f"https://t.me/{bot_username}?start=work"
    
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Share with your friends", url=share_url,     emoji_id="5420396762189831222", style="success")],
        [ibtn("Start Work",              url=start_work_url, emoji_id="5352877703043258544", style="primary")],
    ])
    
    group_caption = (
        f"🎉 <b>New Withdrawal Successful!</b> 🎉\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> <code>{full_name}</code>\n"
        f"💰 <b>Total Amount:</b> <code>{amount} BDT</code>\n"
        f"💳 <b>Method:</b> <code>{method}</code>\n"
        f"📱 <b>Number:</b> <code>{masked_number}</code>\n\n"
        f"📊 <b>Tasks Done:</b>\n"
        f"{task_text}\n"
        f"🏆 <b>Total Withdrawals:</b> <code>{withdraw_count} times</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <i>Keep working and earning!</i>"
    )
    group_kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("Start Work",    url=start_work_url,                                emoji_id="5352877703043258544", style="primary")],
        [ibtn("Refer and Earn", url=f"https://t.me/{bot_username}?start=ref",     emoji_id="5420396762189831222", style="success")],
    ])
    
    try:
        await bot.send_photo(chat_id=user_id, photo=message.photo[-1].file_id, caption=user_caption, reply_markup=user_kb, parse_mode="HTML")
        await message.answer("✅ Screenshot sent to user.")
        
        payout_group_id = await get_setting('payout_group_id')
        if payout_group_id:
            try:
                await bot.send_photo(chat_id=payout_group_id, photo=message.photo[-1].file_id, caption=group_caption, reply_markup=group_kb, parse_mode="HTML")
                await message.answer("✅ Forwarded to Payout Group.")
            except Exception as e:
                await message.answer(f"⚠️ Failed to forward to Payout Group: {e}")
                
    except Exception as e:
        await message.answer(f"❌ Failed: {e}")
        
    await state.clear()
    
    # Auto-show next pending withdraw
    pending_list = await _get_filtered_pending_withdraws('all')
    total = len(pending_list)
    if total > 0:
        doc = pending_list[0]
        text, kb = await _build_pwd_card(doc, 0, total, 'all', bot)
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer("✅ সব Withdrawal সম্পূর্ণ করা হয়েছে! আর কোনো pending নেই।")

@router.message(F.text == "Broadcast")
async def start_broadcast(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'broadcast'): return
    users = await get_all_users()
    await message.answer(f"📢 **Broadcast**\nTotal Users: {len(users)}\nSend message/media.", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_for_broadcast_msg)

@router.message(AdminStates.waiting_for_broadcast_msg)
async def preview_broadcast(message: Message, state: FSMContext):
    await state.update_data(broadcast_msg_id=message.message_id, broadcast_chat_id=message.chat.id)
    await message.reply("Preview. Send it?", reply_markup=get_broadcast_preview_keyboard())

@router.callback_query(F.data == "send_broadcast", AdminStates.waiting_for_broadcast_msg)
async def send_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    msg_id, chat_id = data['broadcast_msg_id'], data['broadcast_chat_id']
    users = await get_all_users()
    
    status_msg = await callback.message.edit_text("🚀 Broadcasting... (0%)")
    
    success = 0
    failed = 0
    total = len(users)
    
    if total == 0:
        await status_msg.edit_text("✅ No users found.")
        await state.clear()
        return

    batch_size = 35
    
    # Helper to send message
    async def send_msg(uid):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id)
            return True
        except Exception:
            return False

    for i in range(0, total, batch_size):
        batch = users[i:i + batch_size]
        tasks = [send_msg(uid) for uid in batch]
        results = await asyncio.gather(*tasks)
        
        for res in results:
            if res:
                success += 1
            else:
                failed += 1
        
        # Update progress every 3 batches (~100 users) to avoid edit rate limits
        if (i // batch_size) % 3 == 0:
            percent = int(((i + len(batch)) / total) * 100)
            try:
                await status_msg.edit_text(f"🚀 Broadcasting... ({percent}%)\n🟢 Sent: {success} | 🔴 Failed: {failed}")
            except Exception:
                pass
                
        await asyncio.sleep(1.0)

    await status_msg.edit_text(f"✅ **Completed!**\n🟢 Sent: {success}\n🔴 Failed: {failed}")
    await state.clear()

@router.callback_query(F.data == "cancel_broadcast", AdminStates.waiting_for_broadcast_msg)
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Cancelled.")
    await state.clear()
    await callback.answer()

# ---------------------------------------------------------
# MAIN 
# ---------------------------------------------------------
async def process_export_and_send(bot: Bot, date_str, task_type, export_admins, file_path, count):
    if not file_path or not __import__('os').path.exists(file_path):
        logging.error(f"process_export_and_send: file_path is invalid or doesn't exist: {file_path}")
        return
        
    sent_to = set()
    owner_admin_id = current_owner_id() or ADMIN_ID
    # Send to buyers
    buyers = await db.buyers.find().to_list(length=100)
    logging.info(f"Auto export: Found {len(buyers)} buyers in database for {task_type}")
    
    for buyer in buyers:
        types = buyer.get('types', [])
        if task_type in types:
            buyer_id = buyer.get('tg_id')
            username = buyer.get('username', 'No Username')
            try:
                support_link = await get_setting('support_link')
                kb = None
                if support_link:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Contact Admin", url=support_link, emoji_id="5420145051336485498", style="primary")]])
                    
                caption = f"📁 Auto Export: {date_str}\n📌 Type: {task_type}\n📊 Total Accounts: {count}"
                await bot.send_document(chat_id=int(buyer_id), document=FSInputFile(file_path), caption=caption, reply_markup=kb)
                sent_to.add(str(buyer_id))
                logging.info(f"Auto export: Sent {task_type} file to buyer {buyer_id} (@{username})")
                
                # Notify Main Admin
                admin_msg = f"✅ <b>File Successfully Sent to Buyer</b>\n\n👤 Buyer: @{username}\n🆔 ID: <code>{buyer_id}</code>\n📌 Type: {task_type}\n📊 Total Accounts: {count}"
                await bot.send_document(chat_id=int(owner_admin_id), document=FSInputFile(file_path), caption=admin_msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Auto export: Failed to send to buyer {buyer_id} (@{username}): {e}")
                try:
                    await bot.send_message(chat_id=int(owner_admin_id), text=f"⚠️ <b>Auto Export Failed for Buyer</b>\n\n👤 Buyer: @{username}\n🆔 ID: <code>{buyer_id}</code>\n📌 Type: {task_type}\n❌ Error: {str(e)}", parse_mode="HTML")
                except:
                    pass
        else:
            logging.info(f"Auto export: Buyer {buyer.get('tg_id')} skipped - {task_type} not in their types: {types}")
                
    for admin in export_admins:
        if str(admin) in sent_to:
            continue
        try: 
            caption = f"📊 Auto Export for {date_str} ({task_type})\nTotal Accounts: {count}"
            await bot.send_document(chat_id=int(admin), document=FSInputFile(file_path), caption=caption)
        except Exception as e:
            logging.error(f"Auto export: Failed to send to admin {admin}: {e}")

@router.message(F.text == "Manage Buyers", StateFilter("*"))
async def manage_buyers(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id, 'full_control'): return
    kb = ReplyKeyboardMarkup(keyboard=[
        [btn("Add/Edit Buyer", "5337132498965010628", "success"),
         btn("Remove Buyer",     "5974083768233760323", "danger")],
        [btn("View Buyers",     "5463352748751753567", "primary"),
         btn("Back to Admin Panel","5420145051336485498", "primary")],
    ], resize_keyboard=True)
    await message.answer("🛍️ **Buyer Management**", reply_markup=kb, parse_mode="Markdown")

@router.message(F.text == "View Buyers", StateFilter("*"))
async def view_buyers(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'full_control'): return
    try:
        buyers = await db.buyers.find().to_list(length=100)
        if not buyers:
            return await message.answer("No buyers found.")
        
        text = "📋 <b>Current Buyers:</b>\n\n"
        for buyer in buyers:
            types = buyer.get('types', [])
            types_str = ", ".join(types) if isinstance(types, list) else str(types)
            tg_id = buyer.get('tg_id', 'Unknown')
            username = buyer.get('username', 'No Username')
            buyer_info = f"ID: <code>{tg_id}</code>\nUsername: @{username}\nFiles: {types_str}\n\n"
            if len(text) + len(buyer_info) > 4000:
                await message.answer(text, parse_mode="HTML")
                text = ""
            text += buyer_info
        if text:
            await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Error viewing buyers: {str(e)}")

@router.message(F.text == "Add/Edit Buyer", StateFilter("*"))
async def add_buyer_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'full_control'): return
    await message.answer("Send the Telegram ID of the buyer:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminBuyerStates.waiting_for_buyer_id)

@router.message(AdminBuyerStates.waiting_for_buyer_id)
async def process_buyer_id(message: Message, state: FSMContext, bot: Bot):
    tg_id = message.text.strip()
    
    if not tg_id.isdigit():
        if not tg_id.startswith('@'):
            tg_id = '@' + tg_id
        try:
            chat = await bot.get_chat(tg_id)
            tg_id = str(chat.id)
            await message.answer(f"✅ Username resolved to ID: `{tg_id}`", parse_mode="Markdown")
        except Exception:
            return await message.answer("⚠️ Username not found. Please use their numeric ID.")
            
    username = "No Username"
    try:
        chat = await bot.get_chat(int(tg_id))
        username = chat.username or chat.first_name or "No Username"
    except Exception:
        pass

    existing = await db.buyers.find_one({'tg_id': tg_id})
    current_types = existing.get('types', []) if existing else []

    await state.update_data(buyer_id_temp=tg_id, buyer_username_temp=username, new_buyer_types=current_types)
    await message.answer(f"Select files for buyer (ID: {tg_id}, Name: {username}):", reply_markup=get_buyer_file_types_keyboard(current_types))
    await state.set_state(AdminBuyerStates.buyer_id_temp)

@router.callback_query(F.data.startswith("toggle_buyer_type_"), AdminBuyerStates.buyer_id_temp)
async def toggle_buyer_type(callback: CallbackQuery, state: FSMContext):
    file_type = callback.data.replace("toggle_buyer_type_", "")
    data = await state.get_data()
    types = data.get('new_buyer_types', [])
    if file_type in types:
        types.remove(file_type)
    else:
        types.append(file_type)
    await state.update_data(new_buyer_types=types)
    await callback.message.edit_reply_markup(reply_markup=get_buyer_file_types_keyboard(types))
    await callback.answer()

@router.callback_query(F.data == "save_buyer", AdminBuyerStates.buyer_id_temp)
async def save_buyer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get('buyer_id_temp')
    username = data.get('buyer_username_temp')
    types = data.get('new_buyer_types', [])
    
    await db.buyers.update_one({'tg_id': tg_id}, {'$set': {'username': username, 'types': types}}, upsert=True)
    
    await callback.answer("✅ Buyer Saved!", show_alert=False)
    await callback.message.edit_text(f"✅ Buyer <code>{tg_id}</code> (@{username}) saved with files: {', '.join(types) if types else 'None'}", parse_mode="HTML")
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [btn("Add/Edit Buyer", "5337132498965010628", "success"),
         btn("Remove Buyer",     "5974083768233760323", "danger")],
        [btn("View Buyers",     "5463352748751753567", "primary"),
         btn("Back to Admin Panel","5420145051336485498", "primary")],
    ], resize_keyboard=True)
    await callback.message.answer("🛍️ Buyer Saved.", reply_markup=kb)
    await state.clear()

@router.message(F.text == "Remove Buyer", StateFilter("*"))
async def remove_buyer_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'full_control'): return
    await message.answer("Send the Telegram ID of the buyer to remove:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminBuyerStates.waiting_for_remove_buyer_id)

@router.message(AdminBuyerStates.waiting_for_remove_buyer_id)
async def process_remove_buyer(message: Message, state: FSMContext):
    tg_id = message.text.strip()
    await db.buyers.delete_one({'tg_id': tg_id})
    kb = ReplyKeyboardMarkup(keyboard=[
        [btn("Add/Edit Buyer", "5337132498965010628", "success"),
         btn("Remove Buyer",     "5974083768233760323", "danger")],
        [btn("View Buyers",     "5463352748751753567", "primary"),
         btn("Back to Admin Panel","5420145051336485498", "primary")],
    ], resize_keyboard=True)
    await message.answer(f"✅ Buyer `{tg_id}` removed.", reply_markup=kb, parse_mode="Markdown")
    await state.clear()

# ==========================================
# USER MANAGEMENT (ADD BALANCE)
# ==========================================
@router.message(F.text == "Manage Users", StateFilter("*"))
async def manage_users_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id, 'full_control'): return
    await message.answer("Please send the Telegram ID of the user you want to manage:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminUserStates.waiting_for_user_id)

@router.message(AdminUserStates.waiting_for_user_id)
async def process_user_search(message: Message, state: FSMContext):
    tg_id = message.text.strip()
    if not tg_id.isdigit():
        return await message.answer("⚠️ Invalid ID. Please send a numeric Telegram ID.", reply_markup=get_cancel_keyboard())
    
    user = await get_user(int(tg_id))
    if not user:
        return await message.answer(f"❌ User with ID `{tg_id}` not found. Try another ID or click Cancel.", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
        
    user_doc = await db.users.find_one({'tg_id': int(tg_id)})
    is_banned = user_doc.get('is_banned', False) if user_doc else False
    status_text = "🚫 Banned" if is_banned else "✅ Active"
        
    await state.update_data(target_user_id=int(tg_id))
    
    full_name = tg_id  # tg_id used as identifier (name not stored in DB)
    balance = user[2]
    
    text = (
        f"👤 **User Found!**\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 **ID:** `{tg_id}`\n"
        f"📝 **Name:** {full_name}\n"
        f"💰 **Current Balance:** {balance} TK\n"
        f"📊 **Status:** {status_text}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💳 Please enter the amount to **ADD** to their balance (e.g., 50.5):\n"
        f"*(Use a negative number like -50 to deduct)*\n"
        f"*(Type `unban` to unblock this user if banned)*"
    )
    await message.answer(text, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminUserStates.waiting_for_balance_amount)

@router.message(AdminUserStates.waiting_for_balance_amount)
async def process_add_balance(message: Message, state: FSMContext, bot: Bot):
    text_input = message.text.strip().lower()
    data = await state.get_data()
    target_id = data.get('target_user_id')
    
    if text_input == 'unban':
        await db.users.update_one({'tg_id': target_id}, {'$set': {'is_banned': False, 'screenshot_strikes': 0}})
        await state.clear()
        return await message.answer(f"✅ User {target_id} has been successfully unbanned and strikes reset.", reply_markup=get_admin_panel_keyboard(message.from_user.id))

    try:
        amount = float(message.text.strip())
    except ValueError:
        return await message.answer("⚠️ Invalid amount. Please enter a valid number (e.g. 50 or -20), or 'unban'.", reply_markup=get_cancel_keyboard())
        
    user = await get_user(target_id)
    if not user:
        await state.clear()
        return await message.answer("❌ Error: User not found anymore.", reply_markup=get_admin_panel_keyboard(message.from_user.id))
        
    new_balance = user[2] + amount
    await db.users.update_one({'tg_id': target_id}, {'$set': {'balance': new_balance}})
    
    await message.answer(f"✅ Successfully updated balance!\n\n👤 **ID:** `{target_id}`\n💵 **Amount:** {amount} TK\n💰 **New Balance:** {new_balance} TK", reply_markup=get_admin_panel_keyboard(message.from_user.id), parse_mode="Markdown")
    
    try:
        if amount > 0:
            await bot.send_message(target_id, f"🎁 **Bonus Received!**\n\nAdmin has added **{amount} TK** to your account.\n💰 Current Balance: {new_balance} TK", parse_mode="Markdown")
        elif amount < 0:
            await bot.send_message(target_id, f"⚠️ **Balance Adjusted!**\n\nAdmin has deducted **{abs(amount)} TK** from your account.\n💰 Current Balance: {new_balance} TK", parse_mode="Markdown")
    except:
        await message.answer("⚠️ Note: Could not send notification to the user (they might have blocked the bot).")
        
    await state.clear()



async def auto_export_task(bot: Bot):
    last_exported = {'insta_2fa': None, 'fb_2fa': None, 'fb_cookie': None}
    last_late_exported = None
    while True:
        try:
            # Use Bangladesh Time (UTC+6) instead of server time
            now = datetime.utcnow() + timedelta(hours=6)
            current_time = now.strftime("%H:%M")
            date_str = await get_system_date()
            date_str_insta = await get_system_date('insta_2fa')
            date_str_fb = await get_system_date('fb_2fa')
            date_str_cookie = await get_system_date('fb_cookie')
            
            insta_time = await get_setting('auto_export_time')
            fb_2fa_time = await get_setting('auto_export_time_fb_2fa')
            fb_cookie_time = await get_setting('auto_export_time_fb_cookie')
            
            if not insta_time: insta_time = "20:00"
            if not fb_2fa_time: fb_2fa_time = "21:00"
            if not fb_cookie_time: fb_cookie_time = "22:00"
            
            # Send to main admin and any admin with 'export' perm
            export_admins = {str(current_owner_id() or ADMIN_ID)}

            # Helper: check if current time is within 1.5 minutes of target time
            def is_time_match(target_time_str):
                try:
                    target = datetime.strptime(target_time_str, "%H:%M")
                    current = datetime.strptime(current_time, "%H:%M")
                    diff = abs((current - target).total_seconds())
                    return diff <= 90
                except:
                    return current_time == target_time_str

            # === Auto Export: Export ALL tasks for the day ===
            async def do_auto_export(task_type, type_key, target_time, t_date):
                file_path, count = await export_tasks_xlsx(t_date, task_type)
                if file_path and count > 0:
                    asyncio.create_task(process_export_and_send(bot, t_date, task_type, export_admins, file_path, count))
                    last_exported[type_key] = f"{t_date}_{target_time}"
                    logging.info(f"Auto export {task_type}: {count} accounts")
                else:
                    last_exported[type_key] = f"{t_date}_{target_time}"
                    logging.info(f"Auto export {task_type}: No tasks found for {t_date}")
                    
            if is_time_match(insta_time) and last_exported['insta_2fa'] != f"{date_str_insta}_{insta_time}":
                await do_auto_export('insta_2fa', 'insta_2fa', insta_time, date_str_insta)
                
            if is_time_match(fb_2fa_time) and last_exported['fb_2fa'] != f"{date_str_fb}_{fb_2fa_time}":
                await do_auto_export('fb_2fa', 'fb_2fa', fb_2fa_time, date_str_fb)
                
            if is_time_match(fb_cookie_time) and last_exported['fb_cookie'] != f"{date_str_cookie}_{fb_cookie_time}":
                await do_auto_export('fb_cookie', 'fb_cookie', fb_cookie_time, date_str_cookie)
            
            # === Late Night Export (Optional): Re-export full day's data ===
            late_status = await get_setting('late_export_status') or 'on'
            late_time = await get_setting('late_export_time') or '23:55'
            
            if late_status == 'on' and is_time_match(late_time) and last_late_exported != f"{date_str}_{late_time}":
                logging.info(f"Late night export started for {date_str}")
                
                # Late export: re-export ALL tasks for the day (Full Day's Data)
                for task_type in ['insta_2fa', 'fb_2fa', 'fb_cookie']:
                    t_date = await get_system_date(task_type)
                    file_path, count = await export_tasks_xlsx(t_date, task_type)
                    if file_path and count > 0:
                        async def process_late_export_and_send(task_type_inner, file_path_inner, count_inner, date_str_inner):
                            sent_to = set()
                            buyers = await db.buyers.find().to_list(length=100)
                            for buyer in buyers:
                                types = buyer.get('types', [])
                                if task_type_inner in types:
                                    buyer_id = buyer.get('tg_id')
                                    username = buyer.get('username', 'No Username')
                                    try:
                                        support_link = await get_setting('support_link')
                                        kb = None
                                        if support_link:
                                            kb = InlineKeyboardMarkup(inline_keyboard=[[ibtn("Contact Admin", url=support_link, emoji_id="5420145051336485498", style="primary")]])
                                            
                                        caption = (
                                            f"🌙 Late Night Export (Final): {date_str_inner}\n"
                                            f"📌 Type: {task_type_inner}\n"
                                            f"📊 Total Accounts: {count_inner}\n\n"
                                            f"⚠️ এটি আজকের সম্পূর্ণ ও চূড়ান্ত ফাইল"
                                        )
                                        await bot.send_document(chat_id=int(buyer_id), document=FSInputFile(file_path_inner), caption=caption, reply_markup=kb)
                                        sent_to.add(str(buyer_id))
                                        
                                        admin_msg = (
                                            f"🌙 <b>Late Export Sent to Buyer</b>\n\n"
                                            f"👤 Buyer: @{username}\n"
                                            f"🆔 ID: <code>{buyer_id}</code>\n"
                                            f"📌 Type: {task_type_inner}\n"
                                            f"📊 Total: {count_inner}"
                                        )
                                        await bot.send_document(chat_id=int(current_owner_id() or ADMIN_ID), document=FSInputFile(file_path_inner), caption=admin_msg, parse_mode="HTML")
                                    except Exception as e:
                                        logging.error(f"Late export: Failed to send to buyer {buyer_id}: {e}")
                            
                            # Send to admins
                            for admin in export_admins:
                                if str(admin) in sent_to:
                                    continue
                                try:
                                    caption = f"🌙 Late Night Export: {date_str_inner} ({task_type_inner})\nTotal: {count_inner} (Full day's data)"
                                    await bot.send_document(chat_id=int(admin), document=FSInputFile(file_path_inner), caption=caption)
                                except:
                                    pass
                                    
                            logging.info(f"Late export {task_type_inner}: {count_inner} accounts")
                            
                        asyncio.create_task(process_late_export_and_send(task_type, file_path, count, t_date))
                
                last_late_exported = f"{date_str}_{late_time}"
                
            await asyncio.sleep(30)
            
        except Exception as e:
            logging.error(f"Auto export error: {e}")
            import traceback
            logging.error(traceback.format_exc())
            await asyncio.sleep(60)


async def run_tenant_auto_export(bot: Bot, owner_id: int):
    token = CURRENT_OWNER_ID.set(owner_id)
    try:
        await auto_export_task(bot)
    finally:
        CURRENT_OWNER_ID.reset(token)


@router.message(F.text == "Manage OCR API Keys", StateFilter("*"))
async def manage_ocr_keys(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("View Keys", "ocr_view", style="primary")],
        [ibtn("Add Key",   "ocr_add",  style="success"), ibtn("Delete Key", "ocr_del", style="danger")],
    ])
    await message.answer("🔑 **Manage OCR API Keys**\nSelect an option below:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "ocr_view")
async def ocr_view_keys(callback: CallbackQuery):
    keys = await db.ocr_keys.find({}).to_list(None)
    if not keys:
        return await callback.message.edit_text("No OCR API keys found. Using default 'helloworld'.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ibtn("Back", "ocr_back", "5420145051336485498", "primary")]]))
    
    text = "🔑 **Saved OCR API Keys:**\n\n"
    for k in keys:
        text += f"• `{k['key']}` (Usage: {k.get('usage', 0)}/25000) [Month: {k.get('month', 'N/A')}]\n"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ibtn("Back", "ocr_back", "5420145051336485498", "primary")]]))

@router.callback_query(F.data == "ocr_back")
async def ocr_back_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ibtn("View Keys", "ocr_view", style="primary")],
        [ibtn("Add Key",   "ocr_add",  style="success"), ibtn("Delete Key", "ocr_del", style="danger")],
    ])
    await callback.message.edit_text("🔑 **Manage OCR API Keys**\nSelect an option below:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "ocr_add")
async def ocr_add_key_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("Send the new OCR API Key to add:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminOCRStates.waiting_for_ocr_key)

@router.message(AdminOCRStates.waiting_for_ocr_key)
async def process_add_ocr_key(message: Message, state: FSMContext):
    key = message.text.strip()
    current_month = datetime.now().strftime("%Y-%m")
    await db.ocr_keys.update_one({'key': key}, {'$set': {'usage': 0, 'month': current_month}}, upsert=True)
    await state.clear()
    await message.answer(f"✅ OCR API Key `{key}` added successfully!", parse_mode="Markdown", reply_markup=get_settings_keyboard())

@router.callback_query(F.data == "ocr_del")
async def ocr_del_key_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("Send the OCR API Key you want to delete:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdminOCRStates.waiting_for_remove_ocr_key)

@router.message(AdminOCRStates.waiting_for_remove_ocr_key)
async def process_del_ocr_key(message: Message, state: FSMContext):
    key = message.text.strip()
    res = await db.ocr_keys.delete_one({'key': key})
    await state.clear()
    if res.deleted_count > 0:
        await message.answer(f"✅ OCR API Key `{key}` deleted!", parse_mode="Markdown", reply_markup=get_settings_keyboard())
    else:
        await message.answer("❌ Key not found.", reply_markup=get_settings_keyboard())

async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("Error: Replace HARDCODED_BOT_TOKEN in main.py")
        return

    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(OwnerContextMiddleware())
    dp.callback_query.middleware(OwnerContextMiddleware())
    dp.message.middleware(AntiSpamMiddleware(limit=1.0))
    dp.callback_query.middleware(AntiSpamMiddleware(limit=1.0))
    session_timeout_middleware = SessionTimeoutMiddleware(timeout_seconds=3600)
    dp.message.middleware(session_timeout_middleware)
    dp.callback_query.middleware(session_timeout_middleware)

    dp.include_router(router)

    # Each admin gets an independent export loop with its own Mongo scope.
    for admin in ADMINS:
        asyncio.create_task(run_tenant_auto_export(bot, admin.telegram_id))

    # Start dummy web server for Render Free Tier
    try:
        from aiohttp import web
        async def handle_ping(request):
            return web.Response(text="Bot is running!")
        app = web.Application()
        app.add_routes([web.get('/', handle_ping)])
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", 8000))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"Dummy web server started on port {port}")
    except Exception as e:
        print(f"Failed to start web server: {e}")

    print("Bot is running...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
