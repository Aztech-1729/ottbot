"""Telegram Shop Bot (aiogram v3 + MongoDB motor)

Requirements from todo.txt:
- Inline buttons for user navigation.
- MongoDB as the only storage (including small flow-state).
- Admin manages catalog/stock/payments via inline panel.

Run:
  python bot.py

Config:
- All configuration is imported from `config.py`.

Force join note:
- If FORCE_JOIN_CHANNEL is a private invite link (t.me/+...), Telegram API can't reliably validate membership.
  In that case, the bot will not hard-block users (to avoid deadlock) but will show a join button.
"""

from __future__ import annotations

import asyncio
import logging
import re
import httpx
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import FastAPI, Request
import uvicorn

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, ReturnDocument

import config

api = FastAPI()

bot_instance: Bot | None = None
mongo: Mongo | None = None

# Razorpay state management (for QR message deletion)
RAZORPAY_STATE: dict[int, dict] = {}

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ott_shop_bot")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Hardcoded owner admin (always has access)
OWNER_ADMIN_ID = 6670166083


def is_admin(user_id: int) -> bool:
    return int(user_id) in ({OWNER_ADMIN_ID} | set(int(x) for x in config.ADMIN_IDS))


@api.post("/razorpay/webhook")
async def razorpay_webhook(req: Request):
    """Handle Razorpay webhook for QR code payments"""
    try:
        from razorpay_webhook import verify_webhook_signature, handle_qr_payment_success, handle_payment_captured
        
        # Get signature from headers
        signature = req.headers.get('X-Razorpay-Signature', '')
        
        # Read raw body for signature verification
        payload = await req.body()
        
        # Verify signature
        if not verify_webhook_signature(payload, signature):
            logger.warning("Invalid Razorpay webhook signature")
            return {"ok": False, "error": "Invalid signature"}
        
        # Parse JSON
        data = await req.json()
        event = data.get('event')
        
        # Handle different event types
        if event == 'payment.captured':
            # Main event for QR code payments
            await handle_payment_captured(bot_instance, mongo, data, RAZORPAY_STATE)
        elif event == 'qr_code.credited':
            # Alternative event
            await handle_qr_payment_success(bot_instance, mongo, data, RAZORPAY_STATE)
        elif event == 'payment.authorized':
            # Just log, we process on payment.captured
            logger.info(f"Payment authorized, waiting for capture: {data.get('payload', {}).get('payment', {}).get('entity', {}).get('id')}")
        else:
            logger.info(f"Unhandled Razorpay event: {event}")
        
        return {"ok": True}
    
    except Exception as e:
        logger.error(f"Razorpay webhook error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@api.post("/oxapay/webhook")
async def oxapay_webhook(req: Request):
    """Handle OxaPay webhook for crypto payments"""
    
    # OxaPay Security: Validates via track_id + database verification
    # Note: OxaPay doesn't use signature headers, relies on callback URL + track_id validation

    data = await req.json()
    track_id = data.get("track_id")
    status = str(data.get("status", "")).lower()
    amount = data.get("amount")

    # FIX #6: Webhook payload validation
    if not track_id:
        logger.error("OxaPay webhook missing track_id")
        return {"ok": False, "error": "missing track_id"}
    
    if not isinstance(amount, (int, float)):
        logger.error(f"OxaPay webhook invalid amount: {amount}")
        return {"ok": False, "error": "invalid amount"}

    # FIX #2: Add 'completed' status
    if status not in ["paid", "confirming", "completed"]:
        logger.info(f"OxaPay webhook ignored status: {status}")
        return {"ok": False, "error": "invalid status"}

    # Security Layer 1: Validate track_id exists in our database
    payment = await mongo.payments.find_one({"oxapay_track_id": track_id})
    if not payment:
        logger.warning(f"OxaPay webhook with invalid track_id: {track_id}")
        return {"ok": False, "error": "payment not found"}
    
    # Security Layer 2: Verify payment method is oxapay
    if payment.get("method") != "oxapay":
        logger.warning(f"OxaPay webhook for non-oxapay payment: {track_id}")
        return {"ok": False, "error": "invalid payment method"}

    # FIX #4: Duplicate webhook protection
    if payment.get("status") == "approved":
        logger.info(f"OxaPay payment already processed: {track_id}")
        return {"ok": True, "msg": "already processed"}

    # FIX #3: Correct wallet credit logic (USD to INR conversion)
    amount_usd = float(payment.get("amount_usd", 0))
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    amount_inr = int(amount_usd * usd_to_inr)

    # Update payment status FIRST (atomic operation)
    result = await mongo.payments.update_one(
        {"oxapay_track_id": track_id, "status": {"$ne": "approved"}},
        {"$set": {"status": "approved", "auto": True}},
    )
    
    # Only credit wallet if we successfully updated the status (prevents double credit)
    if result.modified_count > 0:
        await mongo.users.update_one(
            {"_id": payment["user_id"]},
            {"$inc": {"balance_inr": amount_inr}},
        )
        logger.info(f"OxaPay payment processed: user={payment['user_id']}, amount=${amount_usd}, credited=₹{amount_inr}")

    # Delete the payment link message if available
    msg_id = payment.get("payment_message_id")
    if msg_id:
        with contextlib.suppress(Exception):
            await bot_instance.delete_message(payment["user_id"], msg_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Buy Now", callback_data="menu_buy")]
        ]
    )

    amount_usd = float(payment.get('amount_usd', 0))
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    amount_inr = int(amount_usd * usd_to_inr)
    
    await bot_instance.send_message(
        payment["user_id"],
        f"✅ Payment received!\n\n💰 ${amount_usd:.2f} (₹{amount_inr}) credited to your wallet.",
        reply_markup=kb,
    )

    return {"ok": True}

async def handle_oxapay_payment(bot: Bot, m: Mongo, user_id: int, amount_usd: float):
    """Create OxaPay payment request and show payment link"""
    try:
        # Import OxaPay module
        from oxapay import create_payment
        import time
        
        # Get user info
        user = await m.users.find_one({"_id": user_id})
        username = user.get("username") if user else None
        
        # Get webhook URL from config
        webhook_url = getattr(config, 'OXAPAY_WEBHOOK_URL', 'https://example.com/oxapay/webhook')

        # Create OxaPay payment
        payment_result = create_payment(
            amount_usd=amount_usd,
            user_id=user_id,
            webhook_url=webhook_url,
            username=username
        )

        track_id = payment_result["track_id"]
        pay_link = payment_result["payment_url"]
        order_id = payment_result.get("order_id", f"OXA_{user_id}_{int(time.time())}")
        
        # Clear flow
        await m.users.update_one({"_id": user_id}, {"$set": {"flow": None}})

        # Calculate credits user will receive
        usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
        credits_to_receive = int(amount_usd * usd_to_inr)

        text = (
            f"💎 <b>Crypto Payment (OxaPay)</b>\n\n"
            f"💰 Amount: <b>${amount_usd:.2f} USD</b>\n"
            f"🎁 You'll receive: <b>{credits_to_receive} Credits</b>\n"
            f"💡 <i>1 USD = {int(usd_to_inr)} INR = {int(usd_to_inr)} Credits</i>\n\n"
            f"📱 <b>How to Pay:</b>\n"
            f"1️⃣ Click 'Pay with Crypto' button below\n"
            f"2️⃣ Choose your crypto (USDT, BTC, ETH, etc.)\n"
            f"3️⃣ Complete the payment\n"
            f"4️⃣ Your wallet will be credited automatically!\n\n"
            f"⚡ <b>Instant verification - No manual approval needed!</b>\n"
            f"🆔 Track ID: <code>{track_id}</code>"
        )

        # Store payment in database
        from bson import ObjectId
        payment_id = ObjectId()
        payment_doc = {
            "_id": payment_id,
            "payment_id": str(payment_id),  # Add payment_id field
            "user_id": user_id,
            "amount_usd": amount_usd,
            "method": "oxapay",
            "status": "pending",
            "oxapay_track_id": track_id,
            "oxapay_pay_link": pay_link,
            "oxapay_order_id": order_id,
            "created_at": datetime.now(timezone.utc),
        }
        await m.payments.insert_one(payment_doc)

        # Send payment link with cancel button
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Pay with Crypto", url=pay_link)],
                [InlineKeyboardButton(text="❌ Cancel Payment", callback_data=f"cancel_oxapay:{track_id}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")]
            ]
        )

        payment_msg = await bot.send_message(
            user_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )

        logger.info(f"OxaPay payment created: user={user_id}, amount=${amount_usd}, track_id={track_id}")
        
        # Start 30-minute expiry timer with animation
        asyncio.create_task(handle_oxapay_expiry(bot, m, user_id, payment_msg.message_id, track_id, credits_to_receive, amount_usd))
                
    except Exception as e:
        logger.error(f"OxaPay payment error: {e}", exc_info=True)
        await bot.send_message(
            user_id,
            "❌ Error creating payment. Please try again or contact support.",
            reply_markup=kb_back("menu_addfunds")
        )
        await m.users.update_one({"_id": user_id}, {"$set": {"flow": None}})


def money_int(amount: str) -> int:
    """Parse user-entered amount to integer USD."""
    try:
        dec = Decimal(str(amount).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Invalid amount")
    if dec <= 0:
        raise ValueError("Amount must be positive")
    return int(dec)


def chunk_lines(text: str) -> list[str]:
    return [ln.strip() for ln in str(text).splitlines() if ln.strip()]


def _to_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as e:
        raise ValueError("Invalid id") from e


async def handle_razorpay_expiry(bot: Bot, m: Mongo, user_id: int, message_id: int, qr_code_id: str, credits: int, amount_inr: int, qr_bytes: bytes):
    """Handle Razorpay QR code expiry after 5 minutes with animation"""
    try:
        expiry_seconds = 300  # 5 minutes
        update_interval = 60  # Update every 60 seconds
        
        for remaining in range(expiry_seconds, 0, -update_interval):
            await asyncio.sleep(update_interval)
            
            # Check if payment was completed
            if user_id in RAZORPAY_STATE:
                state = RAZORPAY_STATE[user_id]
                if state.get("qr_code_id") != qr_code_id:
                    # Payment completed or new QR generated
                    logger.info(f"Razorpay timer stopped - payment completed for user {user_id}")
                    return
            else:
                # State cleared (payment completed)
                logger.info(f"Razorpay timer stopped - state cleared for user {user_id}")
                return
            
            minutes_left = remaining // 60
            
            try:
                # Update caption with countdown
                text = (
                    f"💳 <b>UPI Payment</b>\n\n"
                    f"💰 Amount: <b>₹{amount_inr}</b>\n"
                    f"🎁 Credits: <b>{credits} Credits</b>\n"
                    f"💡 <i>1 INR = 1 Credit</i>\n\n"
                    f"⏱️ <b>Expires in: {minutes_left} minute(s)</b>\n"
                    f"⚡ Scan QR code to complete payment!\n\n"
                    f"⚡ <b>Instant verification - No manual approval needed!</b>"
                )
                
                from aiogram.types import BufferedInputFile
                qr_photo = BufferedInputFile(qr_bytes, filename="qr_code.png")
                
                # Add cancel button with timer
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="❌ Cancel Payment", callback_data=f"cancel_razorpay:{qr_code_id}")],
                        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")]
                    ]
                )
                
                await bot.edit_message_media(
                    chat_id=user_id,
                    message_id=message_id,
                    media=InputMediaPhoto(
                        media=qr_photo,
                        caption=text,
                        parse_mode=ParseMode.HTML
                    ),
                    reply_markup=kb
                )
                
                logger.info(f"Razorpay timer updated: {minutes_left} min left for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to update Razorpay timer: {e}")
        
        # Payment expired - delete message
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
        except Exception:
            pass
        
        # Clear state
        if user_id in RAZORPAY_STATE:
            RAZORPAY_STATE.pop(user_id, None)
        
        # Send expiry notification
        await bot.send_message(
            user_id,
            "⏱️ <b>Payment Expired</b>\n\n"
            "Your UPI QR code has expired after 5 minutes.\n"
            "Please create a new payment request.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("menu_addfunds")
        )
        
        logger.info(f"Razorpay QR expired for user {user_id}, QR ID: {qr_code_id}")
        
    except Exception as e:
        logger.error(f"Error in Razorpay expiry handler: {e}")


async def handle_oxapay_expiry(bot: Bot, m: Mongo, user_id: int, message_id: int, track_id: str, credits: int, amount_usd: float):
    """Handle OxaPay payment expiry after 30 minutes with animation"""
    try:
        expiry_seconds = 1800  # 30 minutes
        update_interval = 60  # Update every 60 seconds
        usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
        
        # Get initial payment link
        payment = await m.payments.find_one({"oxapay_track_id": track_id})
        pay_link = payment.get("oxapay_pay_link", "#") if payment else "#"
        
        for remaining in range(expiry_seconds, 0, -update_interval):
            await asyncio.sleep(update_interval)
            
            # Check if payment was completed
            payment = await m.payments.find_one({"oxapay_track_id": track_id})
            if payment:
                if payment.get("status") == "approved":
                    logger.info(f"OxaPay timer stopped - payment completed for user {user_id}")
                    return
                if payment.get("status") in {"cancelled", "expired"}:
                    logger.info(f"OxaPay timer stopped - payment {payment.get('status')} for user {user_id}")
                    return
            
            minutes_left = remaining // 60
            
            try:
                # Update text with countdown
                text = (
                    f"💎 <b>Crypto Payment (OxaPay)</b>\n\n"
                    f"💰 Amount: <b>${amount_usd:.2f} USD</b>\n"
                    f"🎁 You'll receive: <b>{credits} Credits</b>\n"
                    f"💡 <i>1 USD = {int(usd_to_inr)} Credits</i>\n\n"
                    f"⏱️ <b>Expires in: {minutes_left} minute(s)</b>\n"
                    f"⚡ Click 'Pay with Crypto' to complete payment!\n\n"
                    f"⚡ <b>Instant verification - No manual approval needed!</b>\n"
                    f"🆔 Track ID: <code>{track_id}</code>"
                )
                
                # Keep buttons with cancel option during timer
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Pay with Crypto", url=pay_link)],
                        [InlineKeyboardButton(text="❌ Cancel Payment", callback_data=f"cancel_oxapay:{track_id}")],
                        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")]
                    ]
                )
                
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb
                )
                
                logger.info(f"OxaPay timer updated: {minutes_left} min left for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to update OxaPay timer: {e}")
        
        # Payment expired - mark as expired and delete message
        payment = await m.payments.find_one({"oxapay_track_id": track_id})
        if payment and payment.get("status") == "pending":
            await m.payments.update_one(
                {"oxapay_track_id": track_id},
                {"$set": {"status": "expired"}}
            )
        else:
            # If user cancelled, don't mark as expired
            pass
        
        # Delete message
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
        except Exception:
            pass
        
        # Send expiry notification
        await bot.send_message(
            user_id,
            "⏱️ <b>Payment Expired</b>\n\n"
            "Your crypto payment invoice has expired after 30 minutes.\n"
            "Please create a new payment request.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("menu_addfunds")
        )
        
        logger.info(f"OxaPay payment expired for user {user_id}, track_id: {track_id}")
        
    except Exception as e:
        logger.error(f"Error in OxaPay expiry handler: {e}")


async def handle_razorpay_payment(bot: Bot, m: Mongo, user_id: int, amount_usd: float, username: str = None):
    """Create Razorpay UPI QR code and show it to user with instant verification"""
    try:
        from razorpay_handler import RazorpayHandler
        from aiogram.types import BufferedInputFile
        
        # Show loading animation
        loading_msg = await bot.send_message(
            chat_id=user_id,
            text="⏳ <b>Generating UPI QR code...</b>\n\n░░░░░░░░░░ 0%",
            parse_mode=ParseMode.HTML
        )
        
        # Animate loading
        async def animate_loading():
            percentages = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 98, 100]
            for pct in percentages:
                try:
                    filled = int(pct / 10)
                    empty = 10 - filled
                    bar = "█" * filled + "░" * empty
                    
                    await loading_msg.edit_text(
                        f"⏳ <b>Generating UPI QR code...</b>\n\n{bar} {pct}%",
                        parse_mode=ParseMode.HTML
                    )
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
        
        # Start animation
        animation_task = asyncio.create_task(animate_loading())
        await asyncio.sleep(0.2)
        
        # Create Razorpay UPI QR code
        razorpay = RazorpayHandler()
        qr_info = razorpay.create_upi_qr(
            amount_usd=amount_usd,
            user_id=user_id,
            username=username
        )
        
        # Fetch and crop QR image
        qr_bytes = razorpay.fetch_qr_image(qr_info["image_url"])
        
        # Wait for animation to complete
        await asyncio.sleep(1.5)
        animation_task.cancel()
        
        # Show completion
        try:
            await loading_msg.edit_text(
                "✅ <b>QR Code Ready!</b>\n\n██████████ 100%",
                parse_mode=ParseMode.HTML
            )
            await asyncio.sleep(0.5)
        except Exception:
            pass
        
        # Delete loading message
        try:
            await loading_msg.delete()
        except Exception:
            pass
        
        # Clear flow
        await m.users.update_one({"_id": user_id}, {"$set": {"flow": None}})
        
        # Send QR code with instructions (expiry timer will be shown during updates)
        credits = int(amount_usd)  # 1 INR = 1 Credit
        text = (
            f"💳 <b>UPI Payment</b>\n\n"
            f"💰 Amount: <b>₹{int(amount_usd)}</b>\n"
            f"🎁 Credits: <b>{credits} Credits</b>\n"
            f"💡 <i>1 INR = 1 Credit</i>\n\n"
            f"📱 <b>How to Pay:</b>\n"
            f"1️⃣ Open any UPI app (GPay, PhonePe, Paytm, etc.)\n"
            f"2️⃣ Scan the QR code below\n"
            f"3️⃣ Complete the payment\n"
            f"4️⃣ Your wallet will be credited instantly!\n\n"
            f"⚡ <b>Instant verification - No manual approval needed!</b>"
        )
        
        # Send QR code image with cancel button
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel Payment", callback_data=f"cancel_razorpay:{qr_info['qr_code_id']}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")]
            ]
        )
        
        qr_photo = BufferedInputFile(qr_bytes, filename="qr_code.png")
        qr_msg = await bot.send_photo(
            chat_id=user_id,
            photo=qr_photo,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
        
        # Store QR message ID in state for later deletion
        RAZORPAY_STATE[user_id] = {
            "qr_message_id": qr_msg.message_id,
            "qr_code_id": qr_info["qr_code_id"],
            "amount": amount_usd
        }
        
        logger.info(f"Razorpay QR created for user {user_id}, amount ₹{int(amount_usd)}, QR ID: {qr_info['qr_code_id']}")
        
        # Start 5-minute expiry timer with animation
        asyncio.create_task(handle_razorpay_expiry(bot, m, user_id, qr_msg.message_id, qr_info["qr_code_id"], credits, int(amount_usd), qr_bytes))
    
    except Exception as e:
        logger.error(f"Razorpay payment error: {e}", exc_info=True)
        await bot.send_message(
            chat_id=user_id,
            text="❌ An error occurred while generating the QR code. Please try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("menu_addfunds")
        )
        await m.users.update_one({"_id": user_id}, {"$set": {"flow": None}})


# -----------------------------
# MongoDB
# -----------------------------


@dataclass(frozen=True)
class Collections:
    users: str = "users"
    products: str = "products"
    categories: str = "categories"
    stocks: str = "stocks"
    orders: str = "orders"
    payments: str = "payments"
    discounts: str = "discounts"
    announcements: str = "announcements"
    admin_logs: str = "admin_logs"


COLL = Collections()


class Mongo:
    def __init__(self, uri: str, db_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]

    @property
    def users(self):
        return self.db[COLL.users]

    @property
    def categories(self):
        return self.db[COLL.categories]

    @property
    def products(self):
        return self.db[COLL.products]

    @property
    def stocks(self):
        return self.db[COLL.stocks]

    @property
    def orders(self):
        return self.db[COLL.orders]

    @property
    def payments(self):
        return self.db[COLL.payments]

    @property
    def discounts(self):
        return self.db[COLL.discounts]

    @property
    def announcements(self):
        return self.db[COLL.announcements]

    @property
    def admin_logs(self):
        return self.db[COLL.admin_logs]


async def ensure_indexes(m: Mongo) -> None:
    # Note: MongoDB always has a built-in unique index on `_id`.
    # Do NOT attempt to recreate it with `unique=True`.
    await m.products.create_index([("category_id", ASCENDING), ("enabled", ASCENDING)])
    await m.stocks.create_index([("product_id", ASCENDING), ("status", ASCENDING)])
    await m.orders.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
    await m.payments.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    await m.discounts.create_index([("product_id", ASCENDING)])


async def seed_from_config(m: Mongo) -> None:
    has_any_category = (await m.categories.estimated_document_count()) > 0
    if has_any_category:
        return

    # Seeding from config removed; catalog should be managed in MongoDB.
    logger.info("Seeding skipped (catalog is managed via MongoDB)")
    now = utcnow()

    category_docs: list[dict[str, Any]] = []
    product_docs: list[dict[str, Any]] = []

    product_categories = getattr(config, "PRODUCT_CATEGORIES", None)
    if not product_categories:
        return

    for slug, cat in product_categories.items():
        title = cat.get("title") or slug
        # also set `category_id` for compatibility with DBs that index that field
        category_docs.append({"_id": slug, "category_id": slug, "title": title, "created_at": now})

        for p in cat.get("products", []):
            product_docs.append(
                {
                    "_id": ObjectId(),
                    "category_id": slug,
                    "name": p["name"],
                    "price_inr": int(p.get("price_inr", 0)),
                    "price_usd": float(p.get("price_usd", 0.0)),
                    "enabled": True,
                    "created_at": now,
                    "updated_at": now,
                }
            )

    if category_docs:
        await m.categories.insert_many(category_docs)
    if product_docs:
        await m.products.insert_many(product_docs)


async def upsert_user(m: Mongo, update_from: Message | CallbackQuery) -> dict[str, Any]:
    u = update_from.from_user
    now = utcnow()

    # Some databases enforce uniqueness on `user_id` (separate from `_id`).
    # We cannot safely set `user_id` in both `$setOnInsert` and `$set` in one update,
    # and this MongoDB server doesn't support `$setOnInsert` as a pipeline stage.
    # So we do:
    #  1) upsert the user doc (classic update document)
    #  2) ensure `user_id` is set if missing/null (separate update)
    #  3) read the final doc

    await m.users.update_one(
        {"_id": u.id},
        {
            "$setOnInsert": {
                "_id": u.id,
                "user_id": u.id,
                "created_at": now,
                "balance_inr": 0,
                "currency": "USD",
                "banned": False,
            },
            "$set": {
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "updated_at": now,
            },
        },
        upsert=True,
    )

    # Fix legacy/bad docs where user_id is missing or null (would break unique index).
    await m.users.update_one({"_id": u.id, "user_id": {"$exists": False}}, {"$set": {"user_id": u.id}})
    await m.users.update_one({"_id": u.id, "user_id": None}, {"$set": {"user_id": u.id}})

    doc = await m.users.find_one({"_id": u.id})
    return doc or {"_id": u.id, "user_id": u.id}


async def admin_log(m: Mongo, admin_id: int, action: str, data: Optional[dict[str, Any]] = None) -> None:
    await m.admin_logs.insert_one(
        {"admin_id": admin_id, "action": action, "data": data or {}, "created_at": utcnow()}
    )


# -----------------------------
# Force join helpers
# -----------------------------

USERNAME_RE = re.compile(r"^https?://t\.me/(?P<username>[A-Za-z0-9_]{5,})/?$")


def extract_force_join_chat_id(force_join_value: str) -> Optional[str]:
    v = (force_join_value or "").strip()
    if not v:
        return None
    if v.startswith("@"):  # @channel
        return v
    if v.lstrip("-").isdigit():
        return v
    m = USERNAME_RE.match(v)
    if m:
        return "@" + m.group("username")
    return None


FORCE_JOIN_CHAT_ID = extract_force_join_chat_id(getattr(config, "FORCE_JOIN_CHANNEL", ""))


async def check_force_join(bot: Bot, user_id: int) -> bool:
    if not getattr(config, "FORCE_JOIN_CHANNEL", ""):
        return True

    if FORCE_JOIN_CHAT_ID is None:
        # private invite link: can't check -> don't block
        return True

    try:
        member = await bot.get_chat_member(FORCE_JOIN_CHAT_ID, user_id)
        return member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        }
    except Exception:
        logger.exception("Force-join check failed")
        return True


def kb_force_join() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Join Channel", url=str(config.FORCE_JOIN_CHANNEL))
    b.button(text="🔄 I Joined", callback_data="forcejoin_check")
    b.adjust(1)
    return b.as_markup()


# -----------------------------
# Keyboards
# -----------------------------


def kb_main(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Buy Products", callback_data="menu_buy")
    b.button(text="💰 Add Funds", callback_data="menu_addfunds")
    b.button(text="📦 My Orders", callback_data="menu_orders")
    b.button(text="📞 Support", callback_data="menu_support")
    b.button(text="👤 My Profile", callback_data="menu_profile")
    if is_admin(user_id):
        b.button(text="🛠 Admin Panel", callback_data="admin_home")
        b.adjust(2, 2, 2)
    else:
        b.adjust(2, 2, 1)
    return b.as_markup()


def kb_back(cb: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data=cb)]])


def kb_categories(categories: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    seen: set[str] = set()
    for c in categories:
        # Use `category_id` (slug) as the stable identifier.
        cid = c.get("category_id") or c.get("_id")
        cid_s = str(cid)
        if cid_s in seen:
            continue
        seen.add(cid_s)
        b.button(text=c.get("title") or cid_s, callback_data=f"cat:{cid_s}")
    b.button(text="⬅️ Back", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def format_money(amount: float) -> str:
    """Format amount showing both USD and INR"""
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    inr = amount * usd_to_inr
    return f"${float(amount):.2f} (₹{int(inr)})"


def format_price(prod: dict[str, Any], currency: str) -> str:
    """Format product price showing both USD and INR"""
    usd = prod.get("price_usd")
    if usd is None:
        usd = prod.get("price_inr", 0)
    
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    inr = float(usd) * usd_to_inr
    return f"${round(float(usd), 2)} (₹{int(inr)})"


def format_credits(amount: float) -> str:
    """Format credits showing USD equivalent"""
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    usd = amount / usd_to_inr
    return f"{int(amount)} Credits (₹{int(amount)} / ${usd:.2f})"


def kb_products(products: list[dict[str, Any]], back_cb: str, currency: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p['name']} — {format_price(p, currency)}", callback_data=f"prod:{p['_id']}")
    b.button(text="⬅️ Back", callback_data=back_cb)
    b.adjust(1)
    return b.as_markup()


def kb_product_detail(prod_id: str, qty: int, back_cb: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➖", callback_data=f"qty:{prod_id}:{max(1, qty-1)}")
    b.button(text=f"Qty: {qty}", callback_data="noop")
    b.button(text="➕", callback_data=f"qty:{prod_id}:{qty+1}")
    b.button(text="✅ Buy Now", callback_data=f"buy:{prod_id}")
    b.button(text="⬅️ Back", callback_data=back_cb)
    b.adjust(3, 1, 1)
    return b.as_markup()


def kb_confirm_purchase(prod_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm Purchase", callback_data=f"confirm:{prod_id}")
    b.button(text="⬅️ Back", callback_data=f"prod:{prod_id}")
    b.adjust(1)
    return b.as_markup()


def kb_payments() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, p in config.PAYMENTS.items():
        b.button(text=p.get("name", key), callback_data=f"pay_method:{key}")
    b.button(text="⬅️ Back", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def kb_payment_method(method_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💵 Enter Amount", callback_data=f"pay_amount:{method_key}")
    b.button(text="⬅️ Back", callback_data="menu_addfunds")
    b.adjust(1)
    return b.as_markup()


def kb_oxapay_currencies() -> InlineKeyboardMarkup:
    """Keyboard for selecting cryptocurrency"""
    buttons = []
    # OxaPay currency list removed (OxaPay payment page provides all options)
    for crypto_code, crypto_name in {"USDT": "USDT"}.items():
        buttons.append([InlineKeyboardButton(
            text=f"💎 {crypto_name} ({crypto_code})",
            callback_data=f"oxapay_crypto:{crypto_code}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_admin_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Payments", callback_data="admin_payments_menu")
    b.button(text="📦 Products", callback_data="admin_products")
    b.button(text="📥 Add Stock", callback_data="admin_stock")
    b.button(text="📋 ALL Stocks", callback_data="admin_stocks_menu")
    b.button(text="🏷 Discounts", callback_data="admin_discounts")
    b.button(text="👥 All Users", callback_data="admin_all_users")
    b.button(text="⬅️ Back", callback_data="home")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def kb_admin_payment_review(payment_id: str, status: str = "pending") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if status == "pending":
        b.button(text="✅ Approve", callback_data=f"admin_pay:{payment_id}:approve")
        b.button(text="❌ Reject", callback_data=f"admin_pay:{payment_id}:reject")
        b.button(text="⬅️ Back", callback_data="admin_payments_pending:0")
    elif status == "approved":
        b.button(text="🔄 Resend Notification", callback_data=f"admin_pay:{payment_id}:resend")
        b.button(text="⬅️ Back", callback_data="admin_payments_confirmed:0")
    else:
        b.button(text="⬅️ Back", callback_data="admin_payments_menu")
    b.adjust(2, 1)
    return b.as_markup()


# -----------------------------
# Pricing helpers
# -----------------------------


async def best_discount_percent(m: Mongo, product_id: ObjectId, qty: int) -> int:
    doc = await m.discounts.find_one({"product_id": product_id})
    if not doc:
        return 0
    best = 0
    for rule in doc.get("rules", []):
        try:
            min_qty = int(rule.get("min_qty"))
            percent = int(rule.get("percent"))
        except Exception:
            continue
        if qty >= min_qty:
            best = max(best, percent)
    return best


async def render_product_detail(m: Mongo, prod: dict[str, Any], user: dict[str, Any]) -> tuple[str, int]:
    prod_id = str(prod["_id"])
    cart = user.get("cart") or {}
    qty = int(cart.get("qty", 1)) if cart.get("product_id") == prod_id else 1

    stock_count = await m.stocks.count_documents({"product_id": prod["_id"], "status": "available"})

    unit_usd = float(prod.get("price_usd", prod.get("price_inr", 0)))
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)

    discount_percent = await best_discount_percent(m, prod["_id"], qty)
    subtotal_usd = unit_usd * qty
    total_usd = subtotal_usd * (1 - discount_percent / 100)

    # Format with both currencies
    unit_str = f"${round(unit_usd, 2)} (₹{int(unit_usd * usd_to_inr)})"
    subtotal_str = f"${round(subtotal_usd, 2)} (₹{int(subtotal_usd * usd_to_inr)})"
    discount_str = f"{discount_percent}%"
    total_str = f"${round(total_usd, 2)} (₹{int(total_usd * usd_to_inr)})"

    text = (
        f"<b>{prod['name']}</b>\n"
        f"Category: <code>{prod['category_id']}</code>\n\n"
        f"💰 Price: <b>{unit_str}</b>\n"
        f"📦 Available stock: <b>{stock_count}</b>\n\n"
        f"🔢 Selected quantity: <b>{qty}</b>\n"
        f"💵 Subtotal: <b>{subtotal_str}</b>\n"
        f"🎁 Discount: <b>{discount_str}</b>\n"
        f"💳 Total: <b>{total_str}</b>\n\n"
        f"💰 Wallet: <b>{format_credits(float(user.get('balance_inr', 0)))}</b>"
    )
    return text, qty


# -----------------------------
# Dispatcher
# -----------------------------

dp = Dispatcher()


@dp.message(Command("bd"))
async def cmd_broadcast(message: Message, bot: Bot, m: Mongo):
    """Admin-only broadcast.

    Usage: reply to any message and type /bd

    - Forwards the replied message to all users AS-IS.
    - Preserves media and inline buttons.
    - Telegram will show the ORIGINAL sender/channel name (e.g., forwarded from channel).
    - No extra 'Broadcast' / 'From' header is sent.
    """

    await upsert_user(m, message)
    if not is_admin(message.from_user.id):
        return

    if not message.reply_to_message:
        # Ignore if not used as reply
        return

    users = m.users.find({}, {"_id": 1})
    sent = 0

    async for u in users:
        uid = int(u["_id"])
        try:
            # Prefer forwarding so attribution (user/channel) is visible.
            await message.reply_to_message.forward(chat_id=uid)
            sent += 1
        except Exception:
            # Fallback: copy if forwarding fails (still keeps media/buttons, but may lose attribution)
            try:
                await message.reply_to_message.copy_to(chat_id=uid)
                sent += 1
            except Exception:
                continue

    # Acknowledge to admin
    try:
        await message.reply(f"✅ Broadcast sent to {sent} users")
    except Exception:
        pass


# -----------------------------
# Message editing helper
# -----------------------------


def product_image_for(product_name: str) -> Optional[str]:
    name = (product_name or "").lower()
    for needle, url in getattr(config, "PRODUCT_IMAGE_RULES", []):
        if needle.lower() in name:
            return url
    return None


async def edit_ui_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    photo: Optional[str] = None,
) -> None:
    """Edit the single UI message (photo + caption). Never sends new messages."""
    media = InputMediaPhoto(
        media=str(photo or getattr(config, "START_IMAGE", "")),
        caption=text,
        parse_mode=ParseMode.HTML,
    )
    try:
        await bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        # Ignore "message is not modified" errors (user clicked same button twice)
        if "message is not modified" in str(e).lower():
            return
        raise


async def edit_ui_from_call(
    call: CallbackQuery,
    bot: Bot,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    photo: Optional[str] = None,
) -> None:
    await edit_ui_message(bot, call.message.chat.id, call.message.message_id, text, reply_markup, photo=photo)


async def get_ui_ref(m: Mongo, user_id: int) -> Optional[tuple[int, int]]:
    doc = await m.users.find_one({"_id": user_id}, {"ui": 1})
    ui = (doc or {}).get("ui")
    if not ui:
        return None
    try:
        return int(ui["chat_id"]), int(ui["message_id"])
    except Exception:
        return None


async def edit_ui_for_user(
    bot: Bot,
    m: Mongo,
    user_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    photo: Optional[str] = None,
) -> None:
    ref = await get_ui_ref(m, user_id)
    if not ref:
        return
    chat_id, message_id = ref
    await edit_ui_message(bot, chat_id, message_id, text, reply_markup, photo=photo)


async def edit_or_send(
    call: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    media: Optional[InputMediaPhoto] = None,
) -> None:
    """Backward-compatible helper.

    Edits the SAME UI message (the callback's message) and never sends a new message.
    If `media` is provided, we use its `media` as the photo and its `caption` as text.
    """
    bot: Bot = dp.workflow_data.get("bot")  # type: ignore[assignment]
    if media is not None:
        photo = str(media.media)
        caption = getattr(media, "caption", None) or text
        await edit_ui_from_call(call, bot, caption, reply_markup, photo=photo)
    else:
        await edit_ui_from_call(call, bot, text, reply_markup)


async def show_home(call: CallbackQuery) -> None:
    bot: Bot = dp.workflow_data.get("bot")  # type: ignore[assignment]
    await edit_ui_from_call(call, bot, "🏠 <b>Main Menu</b>", kb_main(call.from_user.id))


# -----------------------------
# Force join UI handler
# -----------------------------


@dp.callback_query(F.data == "forcejoin_check")
async def cb_forcejoin_check(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await call.answer("You still need to join.", show_alert=True)
        return
    await call.answer("✅ Verified")
    await show_home(call)


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# -----------------------------
# /start
# -----------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, m: Mongo):
    await upsert_user(m, message)

    ok = await check_force_join(bot, message.from_user.id)
    caption = "Welcome to the shop. Use the buttons below to continue." if ok else "🔒 Please join our channel to use the bot."
    kb = kb_main(message.from_user.id) if ok else kb_force_join()

    sent = await message.answer_photo(
        photo=str(getattr(config, "START_IMAGE", "")),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

    # Store UI message reference so text/photo flows can edit the same UI message.
    await m.users.update_one(
        {"_id": message.from_user.id},
        {"$set": {"ui": {"chat_id": sent.chat.id, "message_id": sent.message_id}}},
    )


# -----------------------------
# Main navigation
# -----------------------------


@dp.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    await call.answer()
    await show_home(call)


@dp.callback_query(F.data == "menu_buy")
async def cb_menu_buy(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    cats = await m.categories.find({}, {"title": 1, "category_id": 1}).sort("category_id", 1).to_list(200)
    await call.answer()
    await edit_or_send(call, "🛒 <b>Select a category</b>", kb_categories(cats))


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    cat_id = call.data.split(":", 1)[1]
    products = await m.products.find({"category_id": cat_id, "enabled": True}).sort("name", 1).to_list(500)
    user = await m.users.find_one({"_id": call.from_user.id})
    currency = "USD"

    await call.answer()
    if not products:
        await edit_or_send(call, "No products in this category right now.", kb_back("menu_buy"))
        return

    await edit_or_send(
        call,
        f"📦 <b>Products</b>\nCategory: <code>{cat_id}</code>",
        kb_products(products, "menu_buy", currency),
    )


@dp.callback_query(F.data.startswith("prod:"))
async def cb_product(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    prod_id = call.data.split(":", 1)[1]
    try:
        oid = _to_object_id(prod_id)
    except ValueError:
        await call.answer("Invalid product", show_alert=True)
        return

    prod = await m.products.find_one({"_id": oid, "enabled": True})
    if not prod:
        await call.answer("Product not found", show_alert=True)
        return

    await m.users.update_one({"_id": call.from_user.id}, {"$set": {"cart": {"product_id": prod_id, "qty": 1}}})

    user = await m.users.find_one({"_id": call.from_user.id})
    text, qty = await render_product_detail(m, prod, user)
    await call.answer()
    await edit_or_send(
        call,
        text,
        kb_product_detail(prod_id, qty, f"cat:{prod.get('category_id','')}"),
        media=InputMediaPhoto(
            media=str(product_image_for(prod.get("name", "")) or getattr(config, "START_IMAGE", "")),
            caption=text,
            parse_mode=ParseMode.HTML,
        ),
    )


@dp.callback_query(F.data.startswith("qty:"))
async def cb_qty(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    _, prod_id, qty_s = call.data.split(":", 2)
    qty = max(1, int(qty_s))

    await m.users.update_one({"_id": call.from_user.id}, {"$set": {"cart": {"product_id": prod_id, "qty": qty}}})

    try:
        oid = _to_object_id(prod_id)
    except ValueError:
        await call.answer("Invalid product", show_alert=True)
        return

    prod = await m.products.find_one({"_id": oid, "enabled": True})
    user = await m.users.find_one({"_id": call.from_user.id})
    if not prod or not user:
        await call.answer("Not found", show_alert=True)
        return

    text, qty = await render_product_detail(m, prod, user)
    await call.answer()
    await edit_or_send(
        call,
        text,
        kb_product_detail(prod_id, qty, f"cat:{prod.get('category_id','')}"),
        media=InputMediaPhoto(
            media=str(product_image_for(prod.get("name", "")) or getattr(config, "START_IMAGE", "")),
            caption=text,
            parse_mode=ParseMode.HTML,
        ),
    )


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    prod_id = call.data.split(":", 1)[1]
    try:
        oid = _to_object_id(prod_id)
    except ValueError:
        await call.answer("Invalid product", show_alert=True)
        return

    prod = await m.products.find_one({"_id": oid, "enabled": True})
    user = await m.users.find_one({"_id": call.from_user.id})
    if not prod or not user:
        await call.answer("Not found", show_alert=True)
        return

    text, _ = await render_product_detail(m, prod, user)
    text += "\n\n<b>Confirm purchase?</b>"
    await call.answer()
    await edit_or_send(
        call,
        text,
        kb_confirm_purchase(prod_id),
        media=InputMediaPhoto(
            media=str(product_image_for(prod.get("name", "")) or getattr(config, "START_IMAGE", "")),
            caption=text,
            parse_mode=ParseMode.HTML,
        ),
    )


async def purchase_transaction(m: Mongo, user_id: int, prod: dict[str, Any], qty: int, total: float) -> tuple[str, list[str]]:
    session = await m.client.start_session()
    async with session:
        async with session.start_transaction():
            user = await m.users.find_one({"_id": user_id}, session=session)
            if not user or user.get("banned"):
                raise ValueError("Access denied")

            balance = float(user.get("balance_inr", 0))
            if balance < float(total):
                raise ValueError("Insufficient wallet balance")

            stocks = await m.stocks.find(
                {"product_id": prod["_id"], "status": "available"},
                session=session,
            ).limit(qty).to_list(length=qty)

            if len(stocks) < qty:
                raise ValueError("Not enough stock available")

            delivered_lines = []
            stock_ids = [s["_id"] for s in stocks]
            for s in stocks:
                email = s.get("email") or ""
                password = s.get("password") or ""
                delivered_lines.append(f"Email: {email}\nPassword: {password}")  # clean format

            await m.stocks.update_many(
                {"_id": {"$in": stock_ids}},
                {"$set": {"status": "sold", "sold_to": user_id, "sold_at": utcnow()}},
                session=session,
            )

            await m.users.update_one(
                {"_id": user_id},
                {"$inc": {"balance_inr": -float(total)}},
                session=session,
            )

            order_id = ObjectId()
            await m.orders.insert_one(
                {
                    "_id": order_id,
                    "order_id": str(order_id),
                    "user_id": user_id,
                    "product_id": prod["_id"],
                    "product_name": prod["name"],
                    "qty": qty,
                    "total_usd": float(total),
                    "delivered": delivered_lines,
                    "created_at": utcnow(),
                },
                session=session,
            )

    # Low stock alert (post-transaction)
    try:
        remaining = await m.stocks.count_documents({"product_id": prod["_id"], "status": "available"})
        if remaining <= 3:
            for admin_id in ({OWNER_ADMIN_ID} | set(config.ADMIN_IDS)):
                try:
                    await dp.workflow_data["bot"].send_message(
                        int(admin_id),
                        f"⚠️ Low stock: <b>{prod['name']}</b>\nRemaining: <b>{remaining}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
    except Exception:
        pass

    return str(order_id), delivered_lines


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    prod_id = call.data.split(":", 1)[1]
    try:
        oid = _to_object_id(prod_id)
    except ValueError:
        await call.answer("Invalid product", show_alert=True)
        return

    user = await m.users.find_one({"_id": call.from_user.id})
    if not user or user.get("banned"):
        await call.answer("Access denied", show_alert=True)
        return

    cart = user.get("cart") or {}
    if cart.get("product_id") != prod_id:
        await call.answer("Cart expired. Open product again.", show_alert=True)
        return

    qty = max(1, int(cart.get("qty", 1)))
    prod = await m.products.find_one({"_id": oid, "enabled": True})
    if not prod:
        await call.answer("Product not found", show_alert=True)
        return

    unit = float(prod.get("price_usd", prod.get("price_inr", 0)))
    discount_percent = await best_discount_percent(m, prod["_id"], qty)
    subtotal = unit * qty
    discount = subtotal * (float(discount_percent) / 100.0)
    total = subtotal - discount

    try:
        order_id, delivered = await purchase_transaction(m, call.from_user.id, prod, qty, total)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return

    await call.answer("✅ Purchased")

    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    total_inr = int(float(total) * usd_to_inr)

    caption = (
        f"✅ <b>Purchase Successful</b>\n\n"
        f"📦 Product: <b>{prod['name']}</b>\n"
        f"🔢 Quantity: <b>{qty}</b>\n"
        f"💰 Total Paid: <b>${float(total):.2f} (₹{total_inr})</b>\n\n"
        f"<b>Account Details:</b>\n" + "\n\n".join(f"<code>{x}</code>" for x in delivered)
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 My Orders", callback_data="menu_orders")]
        ]
    )

    await bot.send_photo(
        call.from_user.id,
        photo=str(product_image_for(prod.get("name", "")) or getattr(config, "START_IMAGE", "")),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

    # Clean up the confirmation message
    with contextlib.suppress(Exception):
        await call.message.delete()



@dp.callback_query(F.data == "menu_orders")
async def cb_orders(call: CallbackQuery, bot: Bot, m: Mongo):
    await call.answer()
    await cb_orders_page(call, bot, m, 0)


@dp.callback_query(F.data.startswith("orders_page:"))
async def cb_orders_page_handler(call: CallbackQuery, bot: Bot, m: Mongo):
    await call.answer()
    page = int(call.data.split(":")[1])
    await cb_orders_page(call, bot, m, page)


async def cb_orders_page(call: CallbackQuery, bot: Bot, m: Mongo, page: int):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    per_page = 5
    skip = page * per_page
    
    orders = await m.orders.find({"user_id": call.from_user.id}).sort("created_at", -1).skip(skip).limit(per_page).to_list(per_page)
    total_orders = await m.orders.count_documents({"user_id": call.from_user.id})
    
    if not orders:
        await edit_or_send(call, "📦 <b>My Orders</b>\nNo orders yet.", kb_back("home"))
        return

    text = f"📦 <b>My Orders</b> (Page {page + 1}/{(total_orders + per_page - 1) // per_page})\n\nClick on an order to view details:"
    
    buttons = []
    for o in orders:
        product_name = o.get('product_name', 'Unknown')
        qty = o.get('qty', 1)
        order_id = str(o['_id'])
        buttons.append([InlineKeyboardButton(
            text=f"{product_name} x{qty}",
            callback_data=f"order_view:{order_id}"
        )])
    
    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"orders_page:{page-1}"))
    if skip + per_page < total_orders:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"orders_page:{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="home")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("order_view:"))
async def cb_order_view(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    order_id = call.data.split(":", 1)[1]
    oid = _to_object_id(order_id)
    order = await m.orders.find_one({"_id": oid})
    
    if not order:
        await call.answer("Order not found", show_alert=True)
        return
    
    if order["user_id"] != call.from_user.id:
        await call.answer("Not your order", show_alert=True)
        return

    await call.answer()
    
    product_name = order.get('product_name', 'Unknown')
    qty = order.get('qty', 1)
    total_usd = float(order.get('total_usd', order.get('total_inr', 0)))
    status = order.get('status', 'unknown')
    created_at = order.get('created_at', 'N/A')
    
    # Format date
    if hasattr(created_at, 'strftime'):
        date_str = created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        date_str = str(created_at)
    
    # Calculate INR
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    total_inr = int(total_usd * usd_to_inr)
    
    text = (
        f"📦 <b>Order Details</b>\n\n"
        f"📌 Product: <b>{product_name}</b>\n"
        f"🔢 Quantity: <b>{qty}</b>\n"
        f"💰 Amount: <b>${total_usd:.2f} (₹{total_inr})</b>\n"
        f"📅 Date: {date_str}\n"
        f"🆔 Order ID: <code>{order_id}</code>"
    )
    
    # Show delivered credentials if available
    if order.get("delivered"):
        text += "\n\n<b>Account Details:</b>\n"
        for i, item in enumerate(order["delivered"], 1):
            # item is like "Email: x\nPassword: y"
            text += f"<b>{i}.</b> <code>{item}</code>\n\n"
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Orders", callback_data="menu_orders")]
        ]
    )
    
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data == "menu_support")
async def cb_support(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    await call.answer()
    await edit_or_send(
        call,
        "📞 <b>Support</b>\nIf you need help, contact admin.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Open Support", url=f"https://t.me/{config.SUPPORT_USERNAME}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="home")],
            ]
        ),
    )


@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    await call.answer()

    # Get user data
    user_id = call.from_user.id
    u = await m.users.find_one({"_id": user_id})
    
    if not u:
        await edit_or_send(call, "❌ User profile not found.", kb_back("home"))
        return

    # Get total orders
    total_orders = await m.orders.count_documents({"user_id": user_id})
    
    username = u.get("username", "N/A") or "NoUsername"
    balance_inr = u.get("balance_inr", 0)
    
    # Convert INR to USD using exchange rate from config
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    balance_usd = float(balance_inr) / usd_to_inr

    # Calculate total spent
    pipeline = [
        {"$match": {"user_id": user_id}},
        {
            "$group": {
                "_id": None,
                "total": {
                    "$sum": {
                        "$ifNull": ["$total_usd", {"$ifNull": ["$total_inr", 0]}]
                    }
                },
            }
        },
    ]
    result = await m.orders.aggregate(pipeline).to_list(1)
    total_spent = float(result[0]["total"]) if result else 0.0

    text = (
        "👤 <b>My Profile</b>\n\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"👤 Username: @{username}\n\n"
        f"💰 <b>Wallet Balance:</b>\n"
        f"   💳 Credits: <b>{int(balance_inr)} Credits</b>\n"
        f"   💵 INR: <b>₹{int(balance_inr)}</b>\n"
        f"   💲 USD: <b>${balance_usd:.2f}</b>\n"
        f"   <i>Exchange: 1 USD = {int(usd_to_inr)} INR</i>\n\n"
        f"💳 Total Spent: <b>${total_spent:.2f}</b>\n"
        f"📦 Total Orders: <b>{total_orders}</b>"
    )
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back", callback_data="home")]
        ]
    )
    
    await edit_or_send(call, text, kb)


# -----------------------------
# Add funds flow (user -> admin approval)
# -----------------------------


@dp.callback_query(F.data == "menu_addfunds")
async def cb_addfunds(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    await call.answer()
    await edit_or_send(call, "💰 <b>Select payment method</b>", kb_payments())


@dp.callback_query(F.data.startswith("pay_method:"))
async def cb_pay_method(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    # Answer callback IMMEDIATELY to prevent timeout
    await call.answer()
    
    method = call.data.split(":", 1)[1]
    p = config.PAYMENTS.get(method)
    if not p:
        await edit_or_send(call, "❌ Payment method not found.", kb_back("menu_addfunds"))
        return

    # Handle OxaPay crypto payment - ask for USD amount directly
    if method == "oxapay":
        
        # Store flow to await USD amount
        await m.users.update_one(
            {"_id": call.from_user.id}, 
            {"$set": {"flow": {"type": "await_amount", "method": "oxapay"}}}
        )
        
        text = (
            f"<b>{p.get('name', 'Crypto Payment')}</b>\n\n"
            f"💎 {p.get('note', 'Pay with crypto')}\n\n"
            f"💵 Send the amount in USD (minimum $0.1)\n"
            f"💡 <i>Amount in USD will be credited to your wallet</i>\n\n"
            f"Example: <code>0.1</code> or <code>5</code> or <code>10.50</code>"
        )
        
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")]
            ]
        )
        
        await edit_or_send(call, text, kb)
        return
    
    # Handle Razorpay - prompt for amount directly
    if method == "razorpay":
        # Store flow to await INR amount
        await m.users.update_one(
            {"_id": call.from_user.id}, 
            {"$set": {"flow": {"type": "await_amount", "method": "razorpay"}}}
        )
        
        text = (
            f"<b>{p.get('name', 'UPI Payment')}</b>\n\n"
            f"⚡ {p.get('note', 'Instant verification')}\n\n"
            f"💵 Send the amount in INR (minimum ₹1)\n"
            f"💡 <i>1 INR = 1 Credit</i>\n\n"
            f"Example: <code>10</code> or <code>50</code> or <code>100</code>"
        )
        
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_addfunds")]
            ]
        )
        
        await edit_or_send(call, text, kb)
        return

    # For other methods, show QR image if available
    caption = [f"<b>{p.get('name', method)}</b>"]
    if method == "upi":
        caption.append(f"UPI ID: <code>{p.get('upi_id','')}</code>")
        caption.append(f"Name: <b>{p.get('upi_name','')}</b>")

    qr = p.get("qr_image")
    if qr:
        await edit_or_send(
            call,
            " ",
            kb_payment_method(method),
            media=InputMediaPhoto(media=str(qr), caption="\n".join([x for x in caption if x]), parse_mode=ParseMode.HTML),
        )
    else:
        await edit_or_send(call, "\n".join([x for x in caption if x]), kb_payment_method(method))






@dp.callback_query(F.data.startswith("pay_amount:"))
async def cb_pay_amount(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    ok = await check_force_join(bot, call.from_user.id)
    if not ok:
        await edit_or_send(call, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    method = call.data.split(":", 1)[1]
    p = config.PAYMENTS.get(method)
    if not p:
        await call.answer("Unknown method", show_alert=True)
        return

    # store flow in mongo
    await m.users.update_one({"_id": call.from_user.id}, {"$set": {"flow": {"type": "await_amount", "method": method}}})

    user = await m.users.find_one({"_id": call.from_user.id})
    
    # Different prompts for different payment methods
    if method == "razorpay":
        prompt = "💵 Send amount in INR (e.g., 10, 50, 100)\n\n💡 1 INR = 1 Credit"
    elif method == "oxapay":
        prompt = "💵 Send amount in USD (e.g., 0.1, 5, 10.50)"
    else:
        prompt = "💵 Send amount as a message (e.g., 10$)."

    # Keep showing the selected payment QR (do NOT revert to START_IMAGE)
    qr = p.get("qr_image")
    caption = []
    caption.append(f"<b>{p.get('name', method)}</b>")
    if method == "binance":
        caption.append(f"Binance UID: <code>{p.get('binance_id','')}</code>")
        if p.get("note"):
            caption.append(p["note"])
    if method == "upi":
        caption.append(f"UPI ID: <code>{p.get('upi_id','')}</code>")
        caption.append(f"Name: <b>{p.get('upi_name','')}</b>")
    caption.append("")
    caption.append(prompt)

    await call.answer()
    if qr:
        await edit_or_send(
            call,
            " ",
            kb_back("menu_addfunds"),
            media=InputMediaPhoto(media=str(qr), caption="\n".join([x for x in caption if x is not None]), parse_mode=ParseMode.HTML),
        )
    else:
        await edit_or_send(call, "\n".join([x for x in caption if x is not None]), kb_back("menu_addfunds"))


@dp.message(F.text)
async def on_text(message: Message, bot: Bot, m: Mongo):
    user = await upsert_user(m, message)
    if user.get("banned"):
        return

    ok = await check_force_join(bot, message.from_user.id)
    if not ok:
        # Don't send new messages; update the UI message if we have it.
        await edit_ui_for_user(bot, m, message.from_user.id, "🔒 Please join our channel to use the bot.", kb_force_join())
        return

    doc = await m.users.find_one({"_id": message.from_user.id}, {"flow": 1})
    flow = (doc or {}).get("flow")
    if not flow:
        return

    ftype = flow.get("type")

    if ftype == "await_amount":
        method = flow.get("method")
        
        # Handle Razorpay UPI payment with instant QR generation (INR = Credits)
        if method == "razorpay":
            try:
                # Parse INR amount (no $ symbol for Razorpay)
                amount_inr = float(message.text.strip().replace('₹', '').replace(',', '').replace('$', ''))
                
                # Validate minimum amount
                if amount_inr < 1:
                    await edit_ui_for_user(bot, m, message.from_user.id, "❌ Minimum amount is ₹1 (1 Credit)\nPlease send a valid amount.")
                    return
                
                # Generate Razorpay QR code
                await handle_razorpay_payment(bot, m, message.from_user.id, amount_inr, message.from_user.username)
                return
                
            except ValueError:
                await edit_ui_for_user(bot, m, message.from_user.id, "❌ Invalid amount. Send a number like 10 or 50 or 100")
                return
        
        # Handle OxaPay crypto payment with USD amount
        if method == "oxapay":
            try:
                # Parse USD amount
                amount_usd = float(message.text.strip().replace('$', '').replace(',', ''))
                
                # Validate minimum amount
                if amount_usd < 0.1:
                    await edit_ui_for_user(bot, m, message.from_user.id, "❌ Minimum amount is $0.1\nPlease send a valid amount.")
                    return
                
                # Generate payment link
                await handle_oxapay_payment(bot, m, message.from_user.id, amount_usd)
                return
                
            except ValueError:
                await edit_ui_for_user(bot, m, message.from_user.id, "❌ Invalid amount. Send a number like 0.1 or 5 or 10.50")
                return
        
        # Handle other payment methods (UPI, etc.) with USD amount
        try:
            amount = money_int(message.text)
        except ValueError:
            await edit_ui_for_user(bot, m, message.from_user.id, "Invalid amount. Send a number like 199")
            return

        pay_id = ObjectId()
        await m.payments.insert_one(
            {
                "_id": pay_id,
                # Some DBs enforce uniqueness on `payment_id`
                "payment_id": str(pay_id),
                "user_id": message.from_user.id,
                "method": method,
                "amount_inr": int(amount),
                "status": "await_proof",
                "created_at": utcnow(),
            }
        )
        await m.users.update_one(
            {"_id": message.from_user.id},
            {"$set": {"flow": {"type": "await_proof", "payment_id": str(pay_id)}}},
        )
        await edit_ui_for_user(
            bot,
            m,
            message.from_user.id,
            f"💵 Amount saved: <b>${amount}</b>\nNow send <b>payment screenshot only</b> (photo).",
            kb_back("menu_addfunds"),
        )
        return

    if ftype == "await_proof":
        # Screenshot-only flow: ignore any text and ask for photo.
        await edit_ui_for_user(bot, m, message.from_user.id, "Please send <b>payment screenshot only</b> (photo).", kb_back("menu_addfunds"))
        return

    # admin_broadcast_text removed (broadcast now via /bd reply command)

    if ftype == "admin_stock_paste" and is_admin(message.from_user.id):
        prod_id = flow.get("product_id")
        if not prod_id:
            await edit_ui_for_user(bot, m, message.from_user.id, "No product selected.")
            return

        # old admin_stock_paste flow removed

    if ftype == "admin_discount_rules" and is_admin(message.from_user.id):
        prod_id = flow.get("product_id")
        if not prod_id:
            await edit_ui_for_user(bot, m, message.from_user.id, "No product selected.")
            return

        oid = _to_object_id(prod_id)
        rules: list[dict[str, int]] = []
        for ln in chunk_lines(message.text):
            parts = ln.split()
            if len(parts) != 2:
                continue
            try:
                min_qty = max(1, int(parts[0]))
                percent = max(0, min(100, int(parts[1])))
            except Exception:
                continue
            rules.append({"min_qty": min_qty, "percent": percent})

        rules.sort(key=lambda r: r["min_qty"])
        await m.discounts.update_one(
            {"product_id": oid},
            {"$set": {"product_id": oid, "rules": rules, "updated_at": utcnow()}},
            upsert=True,
        )
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        await admin_log(m, message.from_user.id, "discount_rules_set", {"product_id": str(oid), "rules": rules})
        await edit_ui_for_user(bot, m, message.from_user.id, f"✅ Discount rules saved ({len(rules)} rules).", kb_admin_home())
        return

    if ftype == "admin_user_lookup" and is_admin(message.from_user.id):
        try:
            target_id = int(message.text.strip())
        except Exception:
            await edit_ui_for_user(bot, m, message.from_user.id, "Invalid user id")
            return

        u = await m.users.find_one({"_id": target_id})
        if not u:
            await edit_ui_for_user(bot, m, message.from_user.id, "User not found in DB yet.")
            await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
            return

        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        bal = int(u.get("balance_inr", 0))
        banned = bool(u.get("banned", False))
        await edit_ui_for_user(
            bot,
            m,
            message.from_user.id,
            f"👤 User: <code>{target_id}</code>\nBanned: <b>{banned}</b>\nBalance: <b>₹{bal}</b>",
            kb_admin_user_actions(target_id, banned),
        )
        return

    if ftype == "admin_user_balance" and is_admin(message.from_user.id):
        try:
            amt = money_int(message.text)
        except ValueError:
            await edit_ui_for_user(bot, m, message.from_user.id, "Invalid amount")
            return

        target_id = int(flow.get("target_id"))
        op = flow.get("op")
        delta = amt if op == "addbal" else -amt

        await m.users.update_one({"_id": target_id}, {"$inc": {"balance_inr": float(delta)}})
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        await admin_log(m, message.from_user.id, "user_balance_adjust", {"target_id": target_id, "delta": float(delta)})

        u = await m.users.find_one({"_id": target_id})
        bal = float((u or {}).get("balance_inr", 0))
        banned = bool((u or {}).get("banned", False))
        await edit_ui_for_user(
            bot,
            m,
            message.from_user.id,
            f"✅ Updated.\nUser: <code>{target_id}</code>\nBalance: <b>${bal:.2f}</b>",
            kb_admin_user_actions(target_id, banned),
        )
        return

    if ftype == "await_stock_email" and is_admin(message.from_user.id):
        email = message.text.strip()
        if not email:
            await edit_ui_for_user(bot, m, message.from_user.id, "Invalid email")
            return

        pid = flow.get("product_id")
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": {"type": "await_stock_password", "product_id": pid, "email": email}}})
        await edit_ui_for_user(bot, m, message.from_user.id, "🔑 Now send the <b>password</b>:", kb_back("admin_stock"))
        return

    if ftype == "await_stock_password" and is_admin(message.from_user.id):
        password = message.text.strip()
        if not password:
            await edit_ui_for_user(bot, m, message.from_user.id, "Invalid password")
            return

        pid = flow.get("product_id")
        email = flow.get("email")
        oid = _to_object_id(pid)

        await m.stocks.insert_one({
            "product_id": oid,
            "email": email,
            "password": password,
            "status": "available",
            "created_at": utcnow()
        })

        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        await admin_log(m, message.from_user.id, "stock_added", {"product_id": pid, "email": email})

        await edit_ui_for_user(bot, m, message.from_user.id, f"✅ Stock added!\n\nEmail: {email}\nPassword: {password}", kb_back("admin_home"))
        return

    if ftype == "await_stock_edit_email" and is_admin(message.from_user.id):
        new_email = message.text.strip()
        stock_id = flow.get("stock_id")
        oid = _to_object_id(stock_id)
        await m.stocks.update_one({"_id": oid}, {"$set": {"email": new_email}})
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        await edit_ui_for_user(bot, m, message.from_user.id, "✅ Email updated.", kb_back("admin_stocks_menu"))
        return

    if ftype == "await_stock_edit_pass" and is_admin(message.from_user.id):
        new_pass = message.text.strip()
        stock_id = flow.get("stock_id")
        oid = _to_object_id(stock_id)
        await m.stocks.update_one({"_id": oid}, {"$set": {"password": new_pass}})
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        await edit_ui_for_user(bot, m, message.from_user.id, "✅ Password updated.", kb_back("admin_stocks_menu"))
        return

    if ftype == "admin_prod_edit" and is_admin(message.from_user.id):
        prod_id = flow.get("product_id")
        field = flow.get("field")
        if not prod_id or field not in {"name", "inr", "usd"}:
            await edit_ui_for_user(bot, m, message.from_user.id, "Session expired.")
            return

        oid = _to_object_id(prod_id)
        update: dict[str, Any] = {"updated_at": utcnow()}

        if field == "name":
            new_name = message.text.strip()
            if not new_name:
                await edit_ui_for_user(bot, m, message.from_user.id, "Invalid name")
                return
            update["name"] = new_name

        elif field == "inr":
            try:
                update["price_inr"] = int(Decimal(message.text.strip()))
            except Exception:
                await edit_ui_for_user(bot, m, message.from_user.id, "Invalid INR price")
                return

        elif field == "usd":
            try:
                update["price_usd"] = float(Decimal(message.text.strip()))
            except Exception:
                await edit_ui_for_user(bot, m, message.from_user.id, "Invalid USD price")
                return

        await m.products.update_one({"_id": oid}, {"$set": update})
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        await admin_log(m, message.from_user.id, "product_edit", {"product_id": prod_id, "field": field})

        # Refresh UI by reopening product
        p = await m.products.find_one({"_id": oid})
        if not p:
            await edit_ui_for_user(bot, m, message.from_user.id, "Product not found.", kb_admin_home())
            return

        price_usd = p.get('price_usd', p.get('price_inr',0))
        usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
        price_inr = int(float(price_usd) * usd_to_inr)
        
        txt = (
            "📦 <b>Product Details</b>\n\n"
            f"🆔 ID: <code>{p['_id']}</code>\n"
            f"📌 Name: <b>{p.get('name','')}</b>\n"
            f"📂 Category: <code>{p.get('category_id','')}</code>\n"
            f"💰 Price: <b>${price_usd} (₹{price_inr})</b>\n"
            f"✅ Enabled: <b>{bool(p.get('enabled', True))}</b>"
        )
        await edit_ui_for_user(bot, m, message.from_user.id, txt, kb_admin_product_editor(str(p["_id"]), bool(p.get("enabled", True))))
        return

    if ftype == "admin_prod_add" and is_admin(message.from_user.id):
        step = flow.get("step")
        cat_id = flow.get("category_id")
        if not step or not cat_id:
            await edit_ui_for_user(bot, m, message.from_user.id, "Session expired.")
            return

        if step == "name":
            name = message.text.strip()
            if not name:
                await edit_ui_for_user(bot, m, message.from_user.id, "Invalid name")
                return
            await m.users.update_one(
                {"_id": message.from_user.id},
                {"$set": {"flow": {"type": "admin_prod_add", "step": "usd", "category_id": cat_id, "name": name}}},
            )
            await edit_ui_for_user(bot, m, message.from_user.id, "➕ Send USD price", kb_back("admin_products"))
            return


        if step == "usd":
            try:
                price_usd = float(Decimal(message.text.strip()))
            except Exception:
                await edit_ui_for_user(bot, m, message.from_user.id, "Invalid USD price")
                return

            name = str(flow.get("name") or "").strip()
            doc = {
                "_id": ObjectId(),
                "category_id": cat_id,
                "name": name,
                "price_inr": 0,
                "price_usd": float(price_usd),
                "enabled": True,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
            await m.products.insert_one(doc)
            await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
            await admin_log(m, message.from_user.id, "product_add", {"product_id": str(doc["_id"])})

            await edit_ui_for_user(bot, m, message.from_user.id, "✅ Product added.", kb_admin_home())
            return


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot, m: Mongo):
    user = await upsert_user(m, message)
    if user.get("banned"):
        return

    doc = await m.users.find_one({"_id": message.from_user.id}, {"flow": 1})
    flow = (doc or {}).get("flow")
    if not flow or flow.get("type") != "await_proof":
        return

    pay_id = flow.get("payment_id")
    if not pay_id:
        await edit_ui_for_user(bot, m, message.from_user.id, "Session expired. Start again.", kb_back("menu_addfunds"))
        await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})
        return

    oid = _to_object_id(pay_id)
    file_id = message.photo[-1].file_id

    pdoc = await m.payments.find_one({"_id": oid, "user_id": message.from_user.id})
    amount_inr = int((pdoc or {}).get("amount_inr", 0))
    method = str((pdoc or {}).get("method", ""))

    await m.payments.update_one(
        {"_id": oid, "user_id": message.from_user.id},
        {"$set": {"status": "pending", "proof_photo": file_id, "updated_at": utcnow()}},
    )
    await m.users.update_one({"_id": message.from_user.id}, {"$set": {"flow": None}})

    await edit_ui_for_user(bot, m, message.from_user.id, "✅ Screenshot submitted. Admin will review shortly.", kb_main(message.from_user.id))

    admin_msgs = []
    for admin_id in ({OWNER_ADMIN_ID} | set(config.ADMIN_IDS)):
        try:
            sent = await bot.send_photo(
                int(admin_id),
                photo=file_id,
                caption=(
                    f"💳 <b>New Payment Pending</b>\n"
                    f"Payment ID: <code>{oid}</code>\n"
                    f"User: <code>{message.from_user.id}</code>\n"
                    f"Method: <b>{method}</b>\n"
                    f"Amount: <b>₹{amount_inr}</b>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_admin_payment_review(str(oid)),
            )
            admin_msgs.append({"admin_id": int(admin_id), "chat_id": sent.chat.id, "message_id": sent.message_id})
        except Exception:
            pass

    if admin_msgs:
        await m.payments.update_one({"_id": oid}, {"$set": {"admin_messages": admin_msgs}})


# -----------------------------
# Admin panel
# -----------------------------


# ===========================
# Cancel Payment Handlers
# ===========================

@dp.callback_query(F.data.startswith("cancel_razorpay:"))
async def cb_cancel_razorpay(call: CallbackQuery, bot: Bot, m: Mongo):
    """Handle Razorpay payment cancellation"""
    await call.answer()
    
    qr_code_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id
    
    try:
        # Clear state
        if user_id in RAZORPAY_STATE:
            state = RAZORPAY_STATE[user_id]
            if state.get("qr_code_id") == qr_code_id:
                RAZORPAY_STATE.pop(user_id, None)
        
        # Delete payment message
        try:
            await bot.delete_message(chat_id=user_id, message_id=call.message.message_id)
        except Exception:
            pass
        
        # Send cancellation message
        await bot.send_message(
            user_id,
            "❌ <b>Payment Cancelled</b>\n\n"
            "Your UPI payment request has been cancelled.\n"
            "You can create a new payment anytime.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("menu_addfunds")
        )
        
        logger.info(f"User {user_id} cancelled Razorpay payment: {qr_code_id}")
    
    except Exception as e:
        logger.error(f"Error cancelling Razorpay payment: {e}")


@dp.callback_query(F.data.startswith("cancel_oxapay:"))
async def cb_cancel_oxapay(call: CallbackQuery, bot: Bot, m: Mongo):
    """Handle OxaPay payment cancellation"""
    await call.answer()
    
    track_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id
    
    try:
        # Mark payment as cancelled in database
        payment = await m.payments.find_one({"oxapay_track_id": track_id})
        if payment and payment.get("status") == "pending":
            await m.payments.update_one(
                {"oxapay_track_id": track_id},
                {"$set": {"status": "cancelled"}}
            )
        
        # Delete payment message
        try:
            await bot.delete_message(chat_id=user_id, message_id=call.message.message_id)
        except Exception:
            pass
        
        # Send cancellation message
        await bot.send_message(
            user_id,
            "❌ <b>Payment Cancelled</b>\n\n"
            "Your crypto payment request has been cancelled.\n"
            "You can create a new payment anytime.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("menu_addfunds")
        )
        
        logger.info(f"User {user_id} cancelled OxaPay payment: {track_id}")
    
    except Exception as e:
        logger.error(f"Error cancelling OxaPay payment: {e}")


@dp.callback_query(F.data == "admin_home")
async def cb_admin_home(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    await call.answer()
    await edit_or_send(call, "🛠 <b>Admin Panel</b>", kb_admin_home())


@dp.callback_query(F.data == "admin_payments_menu")
async def cb_admin_payments_menu(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    # Get payment statistics
    pending_count = await m.payments.count_documents({"status": "pending"})
    confirmed_count = await m.payments.count_documents({"status": "approved"})
    
    # Calculate total payments amount (need to handle Razorpay separately)
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    
    # Get all approved payments
    approved_payments = await m.payments.find({"status": "approved"}).to_list(None)
    
    total_usd = 0.0
    total_inr = 0
    
    for p in approved_payments:
        amount = float(p.get("amount_usd", 0))
        method = p.get("method", "")
        
        if method == "razorpay" or p.get("is_razorpay"):
            # Razorpay: amount is in INR
            total_inr += int(amount)
            total_usd += amount / usd_to_inr
        else:
            # OxaPay or others: amount is in USD
            total_usd += amount
            total_inr += int(amount * usd_to_inr)

    await call.answer()
    
    text = (
        "💳 <b>Payments Overview</b>\n\n"
        f"⏳ Pending Deposits: <b>{pending_count}</b>\n"
        f"✅ Confirmed: <b>{confirmed_count}</b>\n"
        f"💰 Total Payments: <b>${total_usd:.2f} (₹{total_inr})</b>"
    )
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏳ Pending Deposits", callback_data="admin_payments_pending:0"),
                InlineKeyboardButton(text="✅ Confirmed", callback_data="admin_payments_confirmed:0"),
            ],
            [InlineKeyboardButton(text="💰 Total Payments", callback_data="admin_payments_total")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_home")],
        ]
    )
    
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_payments_pending:"))
async def cb_admin_payments_pending(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    page = int(call.data.split(":")[1])
    per_page = 5
    skip = page * per_page

    pending_payments = await m.payments.find({"status": "pending"}).sort("created_at", -1).skip(skip).limit(per_page).to_list(per_page)
    total_pending = await m.payments.count_documents({"status": "pending"})

    await call.answer()
    
    if not pending_payments:
        await edit_or_send(call, "⏳ No pending payments found.", kb_back("admin_payments_menu"))
        return

    text = f"⏳ <b>Pending Deposits</b> (Page {page + 1})\n\n"
    
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    buttons = []
    for p in pending_payments:
        user_info = await m.users.find_one({"_id": p["user_id"]})
        username = user_info.get("username", "N/A") if user_info else "N/A"
        method = p.get("method", "N/A")
        amount = float(p.get("amount_usd", 0))
        payment_id = str(p["_id"])
        
        # Check if Razorpay payment (amount is in INR, not USD)
        if method == "razorpay" or p.get("is_razorpay"):
            amount_inr = int(amount)  # Already in INR
            amount_usd = amount / usd_to_inr
            text += f"• User: @{username} | Method: {method} | ₹{amount_inr} (${amount_usd:.2f})\n"
            buttons.append([InlineKeyboardButton(
                text=f"@{username} - ₹{amount_inr} (${amount_usd:.2f})",
                callback_data=f"admin_payment_view:{payment_id}"
            )])
        else:
            # OxaPay or other USD-based payments
            amount_inr = int(amount * usd_to_inr)
            text += f"• User: @{username} | Method: {method} | ${amount:.2f} (₹{amount_inr})\n"
            buttons.append([InlineKeyboardButton(
                text=f"@{username} - ${amount:.2f} (₹{amount_inr})",
                callback_data=f"admin_payment_view:{payment_id}"
            )])
    
    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Back", callback_data=f"admin_payments_pending:{page-1}"))
    if skip + per_page < total_pending:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_payments_pending:{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(text="🔙 Back to Payments", callback_data="admin_payments_menu")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_payment_view:"))
async def cb_admin_payment_view(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    payment_id = call.data.split(":", 1)[1]
    oid = _to_object_id(payment_id)
    p = await m.payments.find_one({"_id": oid})
    
    if not p:
        await call.answer("Payment not found", show_alert=True)
        return

    await call.answer()
    await _admin_show_payment(call, p, m)


async def _admin_show_payment(call: CallbackQuery, p: dict[str, Any], m: Mongo = None) -> None:
    proof = "(no proof)"
    if p.get("proof_text"):
        proof = f"Text: <code>{p['proof_text']}</code>"

    # Get username from database
    username = "N/A"
    if m:
        user_info = await m.users.find_one({"_id": p["user_id"]})
        if user_info:
            username = user_info.get("username", "N/A") or "N/A"

    amount = float(p.get('amount_usd',0))
    method = p.get('method','')
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    
    # Check if Razorpay payment (amount is in INR, not USD)
    if method == "razorpay" or p.get("is_razorpay"):
        amount_inr = int(amount)  # Already in INR
        amount_usd = amount / usd_to_inr
        amount_text = f"₹{amount_inr} (${amount_usd:.2f})"
    else:
        # OxaPay or other USD-based payments
        amount_usd = amount
        amount_inr = int(amount * usd_to_inr)
        amount_text = f"${amount_usd:.2f} (₹{amount_inr})"
    
    text = (
        "💳 <b>Payment Details</b>\n\n"
        f"🆔 Payment ID: <code>{p['_id']}</code>\n"
        f"👤 User ID: <code>{p['user_id']}</code>\n"
        f"📝 Username: @{username}\n"
        f"💳 Method: <b>{method}</b>\n"
        f"💰 Amount: <b>{amount_text}</b>\n"
        f"📊 Status: <b>{p.get('status','pending')}</b>\n"
        f"Proof: {proof}"
    )

    if p.get("proof_photo"):
        await edit_or_send(
            call,
            " ",
            kb_admin_payment_review(str(p["_id"]), p.get("status", "pending")),
            media=InputMediaPhoto(media=p["proof_photo"], caption=text, parse_mode=ParseMode.HTML),
        )
    else:
        await edit_or_send(call, text, kb_admin_payment_review(str(p["_id"]), p.get("status", "pending")))


@dp.callback_query(F.data.startswith("admin_payments_confirmed:"))
async def cb_admin_payments_confirmed(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    page = int(call.data.split(":")[1])
    per_page = 5
    skip = page * per_page

    confirmed_payments = await m.payments.find({"status": "approved"}).sort("reviewed_at", -1).skip(skip).limit(per_page).to_list(per_page)
    total_confirmed = await m.payments.count_documents({"status": "approved"})

    await call.answer()
    
    if not confirmed_payments:
        await edit_or_send(call, "✅ No confirmed payments found.", kb_back("admin_payments_menu"))
        return

    text = f"✅ <b>Confirmed Payments</b> (Page {page + 1})\n\n"
    
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    buttons = []
    for p in confirmed_payments:
        user_info = await m.users.find_one({"_id": p["user_id"]})
        username = user_info.get("username", "N/A") if user_info else "N/A"
        method = p.get("method", "N/A")
        amount = float(p.get("amount_usd", 0))
        payment_id = str(p["_id"])
        
        # Check if Razorpay payment (amount is in INR, not USD)
        if method == "razorpay" or p.get("is_razorpay"):
            amount_inr = int(amount)  # Already in INR
            amount_usd = amount / usd_to_inr
            text += f"• User: @{username} | Method: {method} | ₹{amount_inr} (${amount_usd:.2f})\n"
            buttons.append([InlineKeyboardButton(
                text=f"@{username} - ₹{amount_inr} (${amount_usd:.2f})",
                callback_data=f"admin_payment_view:{payment_id}"
            )])
        else:
            # OxaPay or other USD-based payments
            amount_inr = int(amount * usd_to_inr)
            text += f"• User: @{username} | Method: {method} | ${amount:.2f} (₹{amount_inr})\n"
            buttons.append([InlineKeyboardButton(
                text=f"@{username} - ${amount:.2f} (₹{amount_inr})",
                callback_data=f"admin_payment_view:{payment_id}"
            )])
    
    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Back", callback_data=f"admin_payments_confirmed:{page-1}"))
    if skip + per_page < total_confirmed:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_payments_confirmed:{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(text="🔙 Back to Payments", callback_data="admin_payments_menu")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_payments_total"))
async def cb_admin_payments_total(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    # Calculate total payments statistics (handle Razorpay separately)
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    
    # Get all approved payments
    approved_payments = await m.payments.find({"status": "approved"}).to_list(None)
    
    total_usd = 0.0
    total_inr = 0
    total_count = len(approved_payments)
    
    for p in approved_payments:
        amount = float(p.get("amount_usd", 0))
        method = p.get("method", "")
        
        if method == "razorpay" or p.get("is_razorpay"):
            # Razorpay: amount is in INR
            total_inr += int(amount)
            total_usd += amount / usd_to_inr
        else:
            # OxaPay or others: amount is in USD
            total_usd += amount
            total_inr += int(amount * usd_to_inr)

    await call.answer()
    
    text = (
        "💰 <b>Total Payments Summary</b>\n\n"
        f"Total Transactions: <b>{total_count}</b>\n"
        f"Total Amount: <b>${total_usd:.2f} (₹{total_inr})</b>\n"
    )
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Back to Payments", callback_data="admin_payments_menu")]
        ]
    )
    
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_pay:"))
async def cb_admin_pay_action(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    _, pid, action = call.data.split(":", 2)
    oid = _to_object_id(pid)

    p = await m.payments.find_one({"_id": oid})
    if not p:
        await call.answer("Payment not found", show_alert=True)
        return

    if action == "resend":
        # Resend notification for approved payment
        if p.get("status") != "approved":
            await call.answer("Payment not approved", show_alert=True)
            return
        
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛒 Buy Products", callback_data="menu_buy")]
                ]
            )
            method_key = str(p.get("method", ""))
            method_name = str(config.PAYMENTS.get(method_key, {}).get("name", method_key)).strip() or method_key
            amount = float(p.get("amount_usd", 0))
            usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
            
            # Check if Razorpay payment
            if method_key == "razorpay" or p.get("is_razorpay"):
                amount_inr = int(amount)
                amount_usd = amount / usd_to_inr
                amount_text = f"₹{amount_inr} (${amount_usd:.2f})"
            else:
                amount_usd = amount
                amount_inr = int(amount * usd_to_inr)
                amount_text = f"${amount_usd:.2f} (₹{amount_inr})"
            
            await bot.send_photo(
                int(p["user_id"]),
                photo=str(getattr(config, "START_IMAGE", "")),
                caption=(
                    f"✅ <b>Payment Approved (Resent)</b>\n"
                    f"💳 Method: <b>{method_name}</b>\n"
                    f"💰 Amount: <b>{amount_text}</b>\n\n"
                    f"Your wallet has been credited."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            await call.answer("Notification resent to user!")
        except Exception as e:
            await call.answer(f"Failed to send: {e}", show_alert=True)
        return

    if p.get("status") != "pending":
        await call.answer("Already processed", show_alert=True)
        return

    if action == "approve":
        await m.payments.update_one(
            {"_id": oid},
            {"$set": {"status": "approved", "reviewed_at": utcnow(), "reviewed_by": call.from_user.id}},
        )
        await m.users.update_one({"_id": p["user_id"]}, {"$inc": {"balance_inr": float(p.get("amount_usd", 0))}})
        await admin_log(m, call.from_user.id, "payment_approved", {"payment_id": pid})

        # Notify user with START_IMAGE and quick actions
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🛒 Buy Products", callback_data="menu_buy"),
                        InlineKeyboardButton(text="💼 Wallet", callback_data="menu_wallet"),
                    ]
                ]
            )
            method_key = str(p.get("method", ""))
            method_name = str(config.PAYMENTS.get(method_key, {}).get("name", method_key)).strip() or method_key
            amount = float(p.get("amount_usd", 0))
            usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
            
            # Check if Razorpay payment
            if method_key == "razorpay" or p.get("is_razorpay"):
                amount_inr = int(amount)
                amount_usd = amount / usd_to_inr
                amount_text = f"₹{amount_inr} (${amount_usd:.2f})"
            else:
                amount_usd = amount
                amount_inr = int(amount * usd_to_inr)
                amount_text = f"${amount_usd:.2f} (₹{amount_inr})"
            
            await bot.send_photo(
                int(p["user_id"]),
                photo=str(getattr(config, "START_IMAGE", "")),
                caption=(
                    f"✅ <b>Payment Approved</b>\n"
                    f"💳 Method: <b>{method_name}</b>\n"
                    f"💰 Amount: <b>{amount_text}</b>\n\n"
                    f"Your wallet has been credited."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            pass

        await call.answer("Approved")
    else:
        await m.payments.update_one(
            {"_id": oid},
            {"$set": {"status": "rejected", "reviewed_at": utcnow(), "reviewed_by": call.from_user.id}},
        )
        await admin_log(m, call.from_user.id, "payment_rejected", {"payment_id": pid})

        try:
            method_key = str(p.get("method", ""))
            method_name = str(config.PAYMENTS.get(method_key, {}).get("name", method_key)).strip() or method_key
            amount = float(p.get("amount_usd", 0))
            usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
            
            # Check if Razorpay payment
            if method_key == "razorpay" or p.get("is_razorpay"):
                amount_inr = int(amount)
                amount_usd = amount / usd_to_inr
                amount_text = f"₹{amount_inr} (${amount_usd:.2f})"
            else:
                amount_usd = amount
                amount_inr = int(amount * usd_to_inr)
                amount_text = f"${amount_usd:.2f} (₹{amount_inr})"
            
            await bot.send_photo(
                int(p["user_id"]),
                photo=str(getattr(config, "START_IMAGE", "")),
                caption=(
                    f"❌ <b>Payment Rejected</b>\n"
                    f"💳 Method: <b>{method_name}</b>\n"
                    f"💰 Amount: <b>{amount_text}</b>\n\n"
                    f"If this is a mistake, please contact support."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        await call.answer("Rejected")

    await edit_or_send(call, "🛠 <b>Admin Panel</b>", kb_admin_home())


# admin_broadcast button/flow removed (broadcast now via /bd reply command)


def kb_admin_product_editor(prod_id: str, enabled: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=("⛔ Disable" if enabled else "✅ Enable"), callback_data=f"admin_prod_toggle:{prod_id}")
    b.button(text="✏️ Edit Name", callback_data=f"admin_prod_edit:{prod_id}:name")
    b.button(text="💵 Edit USD", callback_data=f"admin_prod_edit:{prod_id}:usd")
    b.button(text="🗑 Delete", callback_data=f"admin_prod_delete:{prod_id}")
    b.button(text="⬅️ Back", callback_data="admin_products")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


@dp.callback_query(F.data == "admin_products")
async def cb_admin_products(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    prods = await m.products.find({}).sort("created_at", -1).limit(30).to_list(30)
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Product", callback_data="admin_prod_add")
    
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    for p in prods:
        status = "✅" if p.get("enabled") else "⛔"
        price = p.get("price_usd", p.get("price_inr", 0))
        price_inr = int(float(price) * usd_to_inr)
        b.button(text=f"{status} {p['name']} (${price} / ₹{price_inr})", callback_data=f"admin_prod_open:{p['_id']}")
    b.button(text="⬅️ Back", callback_data="admin_home")
    b.adjust(1)
    await call.answer()
    await edit_or_send(call, "📦 <b>Products</b>\nSelect a product to manage.", b.as_markup())


@dp.callback_query(F.data.startswith("admin_prod_open:"))
async def cb_admin_prod_open(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    oid = _to_object_id(call.data.split(":", 1)[1])
    p = await m.products.find_one({"_id": oid})
    if not p:
        await call.answer("Not found", show_alert=True)
        return

    price_usd = p.get('price_usd', p.get('price_inr',0))
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    price_inr = int(float(price_usd) * usd_to_inr)
    
    txt = (
        "📦 <b>Product Details</b>\n\n"
        f"🆔 ID: <code>{p['_id']}</code>\n"
        f"📌 Name: <b>{p.get('name','')}</b>\n"
        f"📂 Category: <code>{p.get('category_id','')}</code>\n"
        f"💰 Price: <b>${price_usd} (₹{price_inr})</b>\n"
        f"✅ Enabled: <b>{bool(p.get('enabled', True))}</b>"
    )
    await call.answer()
    await edit_or_send(call, txt, kb_admin_product_editor(str(p['_id']), bool(p.get('enabled', True))))


@dp.callback_query(F.data.startswith("admin_prod_toggle:"))
async def cb_admin_prod_toggle(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    oid = _to_object_id(call.data.split(":", 1)[1])
    p = await m.products.find_one({"_id": oid})
    if not p:
        await call.answer("Not found", show_alert=True)
        return

    new_val = not bool(p.get("enabled", True))
    await m.products.update_one({"_id": oid}, {"$set": {"enabled": new_val, "updated_at": utcnow()}})
    await admin_log(m, call.from_user.id, "product_toggle", {"product_id": str(oid), "enabled": new_val})
    await call.answer("Updated")
    await cb_admin_prod_open(call, m)


@dp.callback_query(F.data.startswith("admin_prod_delete:"))
async def cb_admin_prod_delete(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    oid = _to_object_id(call.data.split(":", 1)[1])
    await m.products.delete_one({"_id": oid})
    await admin_log(m, call.from_user.id, "product_delete", {"product_id": str(oid)})
    await call.answer("Deleted")
    await cb_admin_products(call, m)


@dp.callback_query(F.data.startswith("admin_prod_edit:"))
async def cb_admin_prod_edit(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    _, prod_id, field = call.data.split(":", 2)
    await m.users.update_one(
        {"_id": call.from_user.id},
        {"$set": {"flow": {"type": "admin_prod_edit", "product_id": prod_id, "field": field}}},
    )
    await call.answer()
    prompt = {
        "name": "Send new product name",
        "inr": "Send new INR price (number)",
        "usd": "Send new USD price (number)",
    }.get(field, "Send value")
    await edit_or_send(call, f"✏️ {prompt}", kb_back(f"admin_prod_open:{prod_id}"))


@dp.callback_query(F.data == "admin_prod_add")
async def cb_admin_prod_add(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    cats = await m.categories.find({}, {"title": 1, "category_id": 1}).sort("category_id", 1).to_list(200)
    b = InlineKeyboardBuilder()
    for c in cats:
        cid = c.get("category_id") or c.get("_id")
        b.button(text=c.get("title") or str(cid), callback_data=f"admin_prod_add_cat:{cid}")
    b.button(text="⬅️ Back", callback_data="admin_products")
    b.adjust(1)
    await call.answer()
    await edit_or_send(call, "➕ Select category", b.as_markup())


@dp.callback_query(F.data.startswith("admin_prod_add_cat:"))
async def cb_admin_prod_add_cat(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    cat_id = call.data.split(":", 1)[1]
    await m.users.update_one(
        {"_id": call.from_user.id},
        {"$set": {"flow": {"type": "admin_prod_add", "step": "name", "category_id": cat_id}}},
    )
    await call.answer()
    await edit_or_send(call, "➕ Send product name", kb_back("admin_prod_add"))


@dp.callback_query(F.data == "admin_discounts")
async def cb_admin_discounts(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    prods = await m.products.find({}).sort("name", 1).limit(50).to_list(50)
    b = InlineKeyboardBuilder()
    for p in prods:
        b.button(text=p["name"], callback_data=f"admin_disc_for:{p['_id']}")
    b.button(text="⬅️ Back", callback_data="admin_home")
    b.adjust(1)
    await call.answer()
    await edit_or_send(call, "🏷 <b>Select a product to set discount rules</b>", b.as_markup())


@dp.callback_query(F.data.startswith("admin_disc_for:"))
async def cb_admin_disc_for(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    prod_id = call.data.split(":", 1)[1]
    await m.users.update_one(
        {"_id": call.from_user.id},
        {"$set": {"flow": {"type": "admin_discount_rules", "product_id": prod_id}}},
    )
    await call.answer()
    await edit_or_send(
        call,
        "🏷 Send discount rules as text, one per line:\n<code>min_qty percent</code>\nExample:\n<code>3 5</code>\n<code>10 10</code>",
        kb_back("admin_discounts"),
    )


@dp.callback_query(F.data == "admin_all_users")
async def cb_admin_all_users(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    await call.answer()
    
    text = "👥 <b>All Users Management</b>\n\nSelect an option:"
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 Active Users", callback_data="admin_users_active:0")],
            [InlineKeyboardButton(text="👁️ View All Users", callback_data="admin_users_view:0")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_home")],
        ]
    )
    
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_users_active:"))
async def cb_admin_users_active(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    page = int(call.data.split(":")[1])
    per_page = 5
    skip = page * per_page

    # Get users with balance > 0 (active cash)
    users = await m.users.find({"balance_inr": {"$gt": 0}}).sort("balance_inr", -1).skip(skip).limit(per_page).to_list(per_page)
    total_users = await m.users.count_documents({"balance_inr": {"$gt": 0}})

    await call.answer()
    
    if not users:
        await edit_or_send(call, "💵 No active users with balance found.", kb_back("admin_all_users"))
        return

    text = f"💵 <b>Active Users (Cash)</b> (Page {page + 1}/{(total_users + per_page - 1) // per_page})\n\n"
    
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    for u in users:
        user_id = u.get("_id", "N/A")
        username = u.get("username", "N/A")
        balance = u.get("balance_inr", 0)
        balance_usd = float(balance) / usd_to_inr
        text += f"• ID: <code>{user_id}</code> | @{username} | 💰 {int(balance)} Credits (₹{int(balance)} / ${balance_usd:.2f})\n"
    
    # Navigation buttons
    buttons = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Back", callback_data=f"admin_users_active:{page-1}"))
    if skip + per_page < total_users:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_users_active:{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(text="🔙 Back to Users Menu", callback_data="admin_all_users")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_users_view:"))
async def cb_admin_users_view(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    page = int(call.data.split(":")[1])
    per_page = 5
    skip = page * per_page

    users = await m.users.find({}).sort("created_at", -1).skip(skip).limit(per_page).to_list(per_page)
    total_users = await m.users.count_documents({})

    await call.answer()
    
    if not users:
        await edit_or_send(call, "👥 No users found.", kb_back("admin_all_users"))
        return

    text = f"👁️ <b>All Users</b> (Page {page + 1}/{(total_users + per_page - 1) // per_page})\n\n"
    
    buttons = []
    for u in users:
        user_id = u.get("_id", "N/A")
        username = u.get("username", "N/A") or "NoUsername"
        buttons.append([InlineKeyboardButton(
            text=f"👤 @{username} - ID: {user_id}",
            callback_data=f"admin_user_profile:{user_id}"
        )])
    
    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Back", callback_data=f"admin_users_view:{page-1}"))
    if skip + per_page < total_users:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_users_view:{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(text="🔙 Back to Users Menu", callback_data="admin_all_users")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_user_profile:"))
async def cb_admin_user_profile(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    user_id = int(call.data.split(":")[1])
    u = await m.users.find_one({"_id": user_id})
    
    if not u:
        await call.answer("User not found", show_alert=True)
        return

    # Get total orders
    total_orders = await m.orders.count_documents({"user_id": user_id})
    
    username = u.get("username", "N/A") or "NoUsername"
    balance_inr = u.get("balance_inr", 0)
    banned = u.get("banned", False)
    created_at = u.get("created_at", "N/A")
    
    # Calculate USD equivalent
    usd_to_inr = getattr(config, 'USD_TO_INR_RATE', 90.0)
    balance_usd = float(balance_inr) / usd_to_inr

    await call.answer()
    
    text = (
        f"👤 <b>User Profile</b>\n\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📝 Username: @{username}\n"
        f"💰 Wallet: <b>{int(balance_inr)} Credits (₹{int(balance_inr)} / ${balance_usd:.2f})</b>\n"
        f"📦 Total Orders: <b>{total_orders}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"📅 Joined: {created_at}"
    )
    
    kb = kb_admin_user_actions(user_id, banned)
    await edit_or_send(call, text, kb)


def kb_admin_user_actions(target_id: int, banned: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if banned:
        b.button(text="✅ Unban", callback_data=f"admin_user:{target_id}:unban")
    else:
        b.button(text="⛔ Ban", callback_data=f"admin_user:{target_id}:ban")
    b.button(text="💰 Add Balance", callback_data=f"admin_user:{target_id}:addbal")
    b.button(text="💸 Remove Balance", callback_data=f"admin_user:{target_id}:subbal")
    b.button(text="⬅️ Back", callback_data="admin_home")
    b.adjust(2, 2, 1)
    return b.as_markup()


@dp.callback_query(F.data.startswith("admin_user:"))
async def cb_admin_user_action(call: CallbackQuery, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    _, target_s, action = call.data.split(":", 2)
    target_id = int(target_s)

    if action in {"ban", "unban"}:
        banned = action == "ban"
        await m.users.update_one({"_id": target_id}, {"$set": {"banned": banned}})
        await admin_log(m, call.from_user.id, f"user_{action}", {"target_id": target_id})
        await call.answer("Updated")
        u = await m.users.find_one({"_id": target_id})
        bal = int((u or {}).get("balance_inr", 0))
        await edit_or_send(
            call,
            f"👤 User: <code>{target_id}</code>\nBanned: <b>{bool((u or {}).get('banned', False))}</b>\nBalance: <b>${float(bal):.2f}</b>",
            kb_admin_user_actions(target_id, bool((u or {}).get("banned", False))),
        )
        return

    if action in {"addbal", "subbal"}:
        await m.users.update_one(
            {"_id": call.from_user.id},
            {"$set": {"flow": {"type": "admin_user_balance", "target_id": target_id, "op": action}}},
        )
        await call.answer()
        await edit_or_send(call, "Send amount in USD (e.g., 10.50)", kb_back("admin_home"))
        return


# -----------------------------
# Admin Stock Management
# -----------------------------


@dp.callback_query(F.data == "admin_stock")
async def cb_admin_stock(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    
    prods = await m.products.find({}).to_list(100)
    await call.answer()
    
    if not prods:
        await edit_or_send(call, "No products. Create one first.", kb_back("admin_home"))
        return
    
    b = InlineKeyboardBuilder()
    for p in prods:
        b.button(text=p["name"], callback_data=f"admin_stock_prod:{p['_id']}")
    b.button(text="⬅️ Back", callback_data="admin_home")
    b.adjust(1)
    
    await edit_or_send(call, "📥 <b>Add Stock</b>\n\nSelect product:", b.as_markup())


@dp.callback_query(F.data.startswith("admin_stock_prod:"))
async def cb_admin_stock_prod(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    
    pid = call.data.split(":", 1)[1]
    await m.users.update_one({"_id": call.from_user.id}, {"$set": {"flow": {"type": "await_stock_email", "product_id": pid}}})
    
    await call.answer()
    await edit_or_send(call, "📧 Send the <b>email/gmail</b> for this stock item:", kb_back("admin_stock"))


@dp.callback_query(F.data == "admin_stocks_menu")
async def cb_admin_stocks_menu(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    
    await call.answer()
    await cb_admin_stocks_page(call, bot, m, 0)


@dp.callback_query(F.data.startswith("admin_stocks_page:"))
async def cb_admin_stocks_page_handler(call: CallbackQuery, bot: Bot, m: Mongo):
    await call.answer()
    page = int(call.data.split(":")[1])
    await cb_admin_stocks_page(call, bot, m, page)


async def cb_admin_stocks_page(call: CallbackQuery, bot: Bot, m: Mongo, page: int):
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    
    # Get products with stock counts
    pipeline = [
        {"$lookup": {"from": "stocks", "localField": "_id", "foreignField": "product_id", "as": "stocks"}},
        {"$addFields": {"stock_count": {"$size": "$stocks"}}},
        {"$match": {"stock_count": {"$gt": 0}}}
    ]
    
    products = await m.products.aggregate(pipeline).to_list(100)
    
    if not products:
        await edit_or_send(call, "📋 <b>Stocks Added</b>\n\nNo stocks found.", kb_back("admin_home"))
        return
    
    per_page = 5
    skip = page * per_page
    page_products = products[skip:skip+per_page]
    
    text = f"📋 <b>ALL Stocks</b> (Page {page + 1}/{(len(products) + per_page - 1) // per_page})\n\nChoose a product to manage stocks:"
    
    buttons = []
    for p in page_products:
        stock_count = p.get("stock_count", 0)
        buttons.append([InlineKeyboardButton(
            text=f"{p['name']} ({stock_count} stocks)",
            callback_data=f"admin_stocks_view:{p['_id']}"
        )])
    
    # Navigation
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"admin_stocks_page:{page-1}"))
    if skip + per_page < len(products):
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_stocks_page:{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin_home")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_stocks_view:"))
async def cb_admin_stocks_view(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    
    pid = call.data.split(":", 1)[1]
    oid = _to_object_id(pid)
    
    product = await m.products.find_one({"_id": oid})
    if not product:
        await call.answer("Product not found", show_alert=True)
        return
    
    await call.answer()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Available Stocks", callback_data=f"admin_stocks_list:{pid}:available:0"),
                InlineKeyboardButton(text="❌ Sold Stocks", callback_data=f"admin_stocks_list:{pid}:sold:0"),
            ],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_stocks_menu")],
        ]
    )

    await edit_or_send(call, f"📋 <b>ALL Stocks</b>\n\nProduct: <b>{product['name']}</b>\n\nChoose a category:", kb)


@dp.callback_query(F.data.startswith("admin_stock_edit:"))
async def cb_admin_stock_edit(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    
    stock_id = call.data.split(":", 1)[1]
    oid = _to_object_id(stock_id)
    
    stock = await m.stocks.find_one({"_id": oid})
    if not stock:
        await call.answer("Stock not found", show_alert=True)
        return
    
    await call.answer()
    
    email = stock.get("email", "N/A")
    password = stock.get("password", "N/A")
    status = stock.get("status", "available")
    
    text = (
        f"📧 <b>Stock Item</b>\n\n"
        f"Email: <code>{email}</code>\n"
        f"Password: <code>{password}</code>\n"
        f"Status: <b>{status}</b>"
    )
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Edit Email", callback_data=f"admin_stock_edit_email:{stock_id}")],
            [InlineKeyboardButton(text="✏️ Edit Password", callback_data=f"admin_stock_edit_pass:{stock_id}")],
            [InlineKeyboardButton(text="🗑 Delete", callback_data=f"admin_stock_delete:{stock_id}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"admin_stocks_view:{stock.get('product_id')}")],
        ]
    )
    
    await edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("admin_stock_delete:"))
async def cb_admin_stock_delete(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    stock_id = call.data.split(":", 1)[1]
    oid = _to_object_id(stock_id)

    stock = await m.stocks.find_one({"_id": oid})
    if not stock:
        await call.answer("Stock not found", show_alert=True)
        return

    await m.stocks.delete_one({"_id": oid})

    await call.answer("✅ Deleted")

    # Back to product menu
    await cb_admin_stocks_view(call, bot, m)


@dp.callback_query(F.data.startswith("admin_stocks_list:"))
async def cb_admin_stocks_list(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    _, pid, status, page_s = call.data.split(":", 3)
    page = int(page_s)
    oid = _to_object_id(pid)

    product = await m.products.find_one({"_id": oid})
    if not product:
        await call.answer("Product not found", show_alert=True)
        return

    per_page = 5
    skip = page * per_page

    q = {"product_id": oid, "status": status}
    stocks = await m.stocks.find(q).sort("created_at", -1).skip(skip).limit(per_page).to_list(per_page)
    total = await m.stocks.count_documents(q)

    await call.answer()

    title = "✅ Available Stocks" if status == "available" else "❌ Sold Stocks"
    text = f"📋 <b>{title}</b>\nProduct: <b>{product['name']}</b>\nPage {page+1}/{(total+per_page-1)//per_page}\n\n"

    buttons = []
    for s in stocks:
        email = s.get("email", "")
        if status == "sold":
            buyer = await m.users.find_one({"_id": s.get("sold_to")})
            buyer_name = (buyer or {}).get("username") or str(s.get("sold_to"))
            sold_at = s.get("sold_at")
            sold_at_str = sold_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(sold_at, "strftime") else str(sold_at)
            buttons.append([InlineKeyboardButton(text=f"{email} • @{buyer_name}", callback_data=f"admin_stock_edit:{s['_id']}" )])
        else:
            buttons.append([InlineKeyboardButton(text=email or "(no email)", callback_data=f"admin_stock_edit:{s['_id']}" )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"admin_stocks_list:{pid}:{status}:{page-1}"))
    if skip + per_page < total:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_stocks_list:{pid}:{status}:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"admin_stocks_view:{pid}")])

    await edit_or_send(call, text, InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin_stock_edit_email:"))
async def cb_admin_stock_edit_email(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    stock_id = call.data.split(":", 1)[1]
    await m.users.update_one({"_id": call.from_user.id}, {"$set": {"flow": {"type": "await_stock_edit_email", "stock_id": stock_id}}})
    await call.answer()
    await edit_or_send(call, "✏️ Send new <b>Email</b>:", kb_back("admin_home"))


@dp.callback_query(F.data.startswith("admin_stock_edit_pass:"))
async def cb_admin_stock_edit_pass(call: CallbackQuery, bot: Bot, m: Mongo):
    await upsert_user(m, call)
    if not is_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return

    stock_id = call.data.split(":", 1)[1]
    await m.users.update_one({"_id": call.from_user.id}, {"$set": {"flow": {"type": "await_stock_edit_pass", "stock_id": stock_id}}})
    await call.answer()
    await edit_or_send(call, "✏️ Send new <b>Password</b>:", kb_back("admin_home"))


# -----------------------------
# Startup
# -----------------------------


# Local webhook handler removed (Cloudflare Worker handles webhooks)


# NOTE: Webhooks are handled via Cloudflare Worker only.
# This bot runs in polling mode and does not expose any local webhook server.


async def main() -> None:
    if not getattr(config, "BOT_TOKEN", None):
        raise RuntimeError("BOT_TOKEN is missing in config.py")

    global bot_instance, mongo
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    m = Mongo(config.MONGO_URI, config.DB_NAME)
    bot_instance = bot
    mongo = m

    # Provide dependencies for handler injection
    dp.workflow_data.update({"bot": bot, "m": m})

    await ensure_indexes(m)

    # Repair legacy docs for DBs that enforce uniqueness on `payment_id` / `order_id`.
    # If any docs were created without these fields, they may be null/missing and unique indexes can crash inserts.
    try:
        await m.payments.update_many(
            {"payment_id": {"$exists": False}},
            [{"$set": {"payment_id": {"$toString": "$_id"}}}],
        )
        await m.payments.update_many(
            {"payment_id": None},
            [{"$set": {"payment_id": {"$toString": "$_id"}}}],
        )

        await m.orders.update_many(
            {"order_id": {"$exists": False}},
            [{"$set": {"order_id": {"$toString": "$_id"}}}],
        )
        await m.orders.update_many(
            {"order_id": None},
            [{"$set": {"order_id": {"$toString": "$_id"}}}],
        )
    except Exception:
        # If pipeline updates are unsupported, ignore (new inserts are fixed anyway).
        pass

    logger.info("Bot started as @%s", getattr(config, "BOT_USERNAME", ""))

    # Start FastAPI server in the same event loop
    config_uvicorn = uvicorn.Config(api, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config_uvicorn)
    api_task = asyncio.create_task(server.serve())

    try:
        await dp.start_polling(bot)
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await api_task


if __name__ == "__main__":
    asyncio.run(main())
