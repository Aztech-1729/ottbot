"""
Razorpay Webhook Handler
Handles payment notifications from Razorpay via FastAPI
"""
import hmac
import hashlib
import logging
from config import RAZORPAY_WEBHOOK_SECRET, ADMIN_IDS

# Hardcoded owner admin (always notified)
OWNER_ADMIN_ID = 6670166083

logger = logging.getLogger(__name__)


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify Razorpay webhook signature"""
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning("Webhook secret not configured, skipping verification")
        return True
    
    expected_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_signature, signature)


async def handle_payment_captured(bot, mongo, data: dict, state_dict: dict):
    """Handle payment.captured event (for QR code payments)"""
    try:
        payload = data.get('payload', {})
        payment = payload.get('payment', {}).get('entity', {})
        
        payment_id = payment.get('id')
        amount_paise = payment.get('amount', 0)
        amount_inr = amount_paise // 100
        
        # Get user_id from payment notes
        notes = payment.get('notes', {})
        user_id = notes.get('telegram_user_id')
        
        if not user_id:
            logger.error("No telegram_user_id in payment notes")
            return
        
        user_id = int(user_id)
        
        logger.info(f"Processing payment.captured: Payment ID: {payment_id}, User: {user_id}, Amount: ₹{amount_inr}")
        
        # Check for duplicate payment
        existing = await mongo.payments.find_one({"razorpay_payment_id": payment_id})
        if existing:
            logger.warning(f"Duplicate payment detected: {payment_id}")
            return
        
        # Treat amount as credits for wallet balance (1 INR paid = 1 Credit)
        credits_to_add = float(amount_inr)
        
        # Create payment record
        # For Razorpay: amount_inr is actual INR paid, amount_usd is for wallet display
        from bson import ObjectId
        from datetime import datetime, timezone
        
        doc_id = ObjectId()
        payment_doc = {
            "_id": doc_id,
            "payment_id": str(doc_id),  # Use doc_id as payment_id string
            "razorpay_payment_id": payment_id,
            "user_id": user_id,
            "method": "razorpay",
            "amount_inr": amount_inr,  # Actual INR paid
            "amount_usd": amount_inr,  # Store as INR for correct display (will be converted in admin panel)
            "is_razorpay": True,  # Flag to identify Razorpay payments
            "status": "approved",
            "created_at": datetime.now(timezone.utc),
            "approved_at": datetime.now(timezone.utc),
        }
        await mongo.payments.insert_one(payment_doc)
        
        # Add credits to user wallet
        await mongo.users.update_one(
            {"_id": user_id},
            {"$inc": {"balance_inr": credits_to_add}}
        )
        
        # Delete QR message if exists
        try:
            logger.info(f"Checking state_dict for user {user_id}. Keys: {list(state_dict.keys())}")
            
            if user_id in state_dict:
                logger.info(f"User {user_id} found in state: {state_dict[user_id]}")
                message_id_to_delete = state_dict[user_id].get("qr_message_id")
                
                if message_id_to_delete:
                    logger.info(f"Attempting to delete message {message_id_to_delete} for user {user_id}")
                    try:
                        await bot.delete_message(
                            chat_id=user_id,
                            message_id=message_id_to_delete
                        )
                        logger.info(f"✓ Successfully deleted QR message {message_id_to_delete} for user {user_id}")
                    except Exception as del_err:
                        logger.error(f"✗ Failed to delete QR message: {del_err}")
                    finally:
                        # Clear state regardless of delete success
                        state_dict.pop(user_id, None)
                        logger.info(f"Cleared state for user {user_id}")
                else:
                    logger.warning(f"No qr_message_id found in state for user {user_id}")
                    state_dict.pop(user_id, None)
            else:
                logger.warning(f"User {user_id} not found in state")
        except Exception as e:
            logger.error(f"Error in QR deletion logic: {e}", exc_info=True)
        
        # Notify user (no payment ID shown)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ <b>Payment Successful!</b>\n\n💰 <b>{int(credits_to_add)} Credits</b> added to your wallet.\n💵 Amount Paid: <b>₹{amount_inr}</b>\n\n🎉 <i>Thank you for your payment!</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user: {e}")
        
        # Notify admins (full details with payment ID)
        for admin_id in ({OWNER_ADMIN_ID} | set(ADMIN_IDS)):
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"💳 <b>Razorpay Payment Received</b>\n\n"
                        f"👤 User ID: <code>{user_id}</code>\n"
                        f"💵 Amount Paid: <b>₹{amount_inr}</b>\n"
                        f"🎁 Credits Added: <b>{int(credits_to_add)} Credits</b>\n"
                        f"🆔 Payment ID: <code>{payment_id}</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        
        logger.info(f"Razorpay payment processed: User {user_id}, Amount ₹{amount_inr}, Credits {credits_to_add}")
    
    except Exception as e:
        logger.error(f"Error processing payment.captured: {e}", exc_info=True)


async def handle_qr_payment_success(bot, mongo, data: dict, state_dict: dict):
    """Handle successful QR code payment (qr_code.credited event)"""
    try:
        payload = data.get('payload', {})
        qr_code = payload.get('qr_code', {}).get('entity', {})
        payment = payload.get('payment', {}).get('entity', {})
        
        qr_code_id = qr_code.get('id')
        payment_id = payment.get('id')
        amount_paise = payment.get('amount', 0)
        amount_inr = amount_paise // 100
        
        # Get user_id from QR code notes
        notes = qr_code.get('notes', {})
        user_id = notes.get('telegram_user_id')
        
        if not user_id:
            logger.error("No telegram_user_id in QR code notes")
            return
        
        user_id = int(user_id)
        
        # Check for duplicate payment
        existing = await mongo.payments.find_one({"razorpay_payment_id": payment_id})
        if existing:
            logger.warning(f"Duplicate payment detected: {payment_id}")
            return
        
        # Treat amount as credits for wallet balance (1 INR paid = 1 Credit)
        credits_to_add = float(amount_inr)
        
        # Create payment record
        # For Razorpay: amount_inr is actual INR paid, amount_usd stores INR value (will be converted in admin panel)
        from bson import ObjectId
        from datetime import datetime, timezone
        
        doc_id = ObjectId()
        payment_doc = {
            "_id": doc_id,
            "payment_id": str(doc_id),  # Use doc_id as payment_id string
            "razorpay_payment_id": payment_id,
            "razorpay_qr_id": qr_code_id,
            "user_id": user_id,
            "method": "razorpay",
            "amount_inr": amount_inr,  # Actual INR paid
            "amount_usd": amount_inr,  # Store as INR for correct display (will be converted in admin panel)
            "is_razorpay": True,  # Flag to identify Razorpay payments
            "status": "approved",
            "created_at": datetime.now(timezone.utc),
            "approved_at": datetime.now(timezone.utc),
        }
        await mongo.payments.insert_one(payment_doc)
        
        # Add credits to user wallet
        await mongo.users.update_one(
            {"_id": user_id},
            {"$inc": {"balance_inr": credits_to_add}}
        )
        
        # Delete QR message if exists
        qr_deleted = False
        message_id_to_delete = None
        
        try:
            logger.info(f"Checking state_dict for user {user_id}. Keys: {list(state_dict.keys())}")
            
            if user_id in state_dict:
                logger.info(f"User {user_id} found in state: {state_dict[user_id]}")
                message_id_to_delete = state_dict[user_id].get("qr_message_id")
                
                if message_id_to_delete:
                    logger.info(f"Attempting to delete message {message_id_to_delete} for user {user_id}")
                    try:
                        await bot.delete_message(
                            chat_id=user_id,
                            message_id=message_id_to_delete
                        )
                        qr_deleted = True
                        logger.info(f"✓ Successfully deleted QR message {message_id_to_delete} for user {user_id}")
                    except Exception as del_err:
                        logger.error(f"✗ Failed to delete QR message: {del_err}")
                    finally:
                        # Clear state regardless of delete success
                        state_dict.pop(user_id, None)
                        logger.info(f"Cleared state for user {user_id}")
                else:
                    logger.warning(f"No qr_message_id found in state for user {user_id}")
                    state_dict.pop(user_id, None)
            else:
                logger.warning(f"User {user_id} not found in state")
        except Exception as e:
            logger.error(f"Error in QR deletion logic: {e}", exc_info=True)
        
        # Notify user (no payment ID shown)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ <b>Payment Successful!</b>\n\n💰 <b>{int(credits_to_add)} Credits</b> added to your wallet.\n💵 Amount Paid: <b>₹{amount_inr}</b>\n\n🎉 <i>Thank you for your payment!</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user: {e}")
        
        # Notify admins (full details with payment ID)
        for admin_id in ({OWNER_ADMIN_ID} | set(ADMIN_IDS)):
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"💳 <b>Razorpay Payment Received</b>\n\n"
                        f"👤 User ID: <code>{user_id}</code>\n"
                        f"💵 Amount Paid: <b>₹{amount_inr}</b>\n"
                        f"🎁 Credits Added: <b>{int(credits_to_add)} Credits</b>\n"
                        f"🆔 Payment ID: <code>{payment_id}</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        
        logger.info(f"Razorpay payment processed: User {user_id}, Amount ₹{amount_inr}, Wallet ${credits_to_add}")
    
    except Exception as e:
        logger.error(f"Error processing Razorpay QR payment: {e}", exc_info=True)
