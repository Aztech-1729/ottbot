"""
OxaPay Crypto Payment Integration
Official API: https://docs.oxapay.com/api/
"""

import requests
import json
import logging
import time

logger = logging.getLogger(__name__)

# OxaPay API endpoint for invoice creation
OXAPAY_API_URL = "https://api.oxapay.com/v1/payment/invoice"


def create_payment(amount_usd: float, user_id: int, webhook_url: str, username: str = None) -> dict:
    """
    Create OxaPay payment invoice for crypto payments
    
    Args:
        amount_usd: Amount in USD
        user_id: Telegram user ID
        webhook_url: Webhook URL for payment notifications
        username: Telegram username (optional)
    
    Returns:
        dict with 'track_id', 'payment_url', 'amount', 'expired_at'
    
    Raises:
        Exception: If payment creation fails
    """
    from config import OXAPAY_API_KEY
    
    # Prepare headers
    headers = {
        'merchant_api_key': OXAPAY_API_KEY,
        'Content-Type': 'application/json'
    }
    
    # Generate unique order ID
    order_id = f"USER_{user_id}_{int(time.time())}"
    
    # Prepare payment data according to OxaPay API
    data = {
        "amount": float(amount_usd),
        "currency": "USD",
        "lifetime": 30,  # Payment valid for 30 minutes
        "fee_paid_by_payer": 1,  # Customer pays network fees
        "under_paid_coverage": 2.5,  # Allow 2.5% underpayment
        "to_currency": "USDT",  # Accept USDT payments
        "auto_withdrawal": False,  # Manual withdrawal
        "mixed_payment": True,  # Allow multiple payment methods
        "callback_url": webhook_url,
        "return_url": "https://t.me/your_bot",  # Return to bot after payment
        "email": f"user{user_id}@telegram.user",  # Placeholder email
        "order_id": order_id,
        "thanks_message": "Thank you! Your wallet will be credited shortly.",
        "description": f"Wallet Top-up for @{username or user_id}",
        "sandbox": False  # Production mode (set True for testing)
    }
    
    try:
        logger.info(f"Creating OxaPay payment: user={user_id}, amount=${amount_usd}")
        
        # Make API request
        response = requests.post(
            OXAPAY_API_URL, 
            data=json.dumps(data), 
            headers=headers,
            timeout=15
        )
        
        # Parse response
        result = response.json()
        
        logger.info(f"OxaPay response: status={result.get('status')}, message={result.get('message')}")
        
        # Check if request was successful
        if result.get("status") == 200 and "data" in result:
            payment_data = result["data"]
            
            logger.info(f"✓ OxaPay payment created: track_id={payment_data.get('track_id')}")
            
            return {
                "track_id": payment_data["track_id"],
                "payment_url": payment_data["payment_url"],
                "amount": amount_usd,
                "expired_at": payment_data.get("expired_at"),
                "order_id": order_id
            }
        else:
            # Handle API errors
            error = result.get("error", {})
            error_type = error.get("type", "unknown")
            error_key = error.get("key", "unknown")
            error_message = error.get("message", result.get("message", "Unknown error"))
            
            logger.error(f"OxaPay API error: type={error_type}, key={error_key}, message={error_message}")
            logger.error(f"Full response: {json.dumps(result, indent=2)}")
            
            raise Exception(f"OxaPay error: {error_message}")
    
    except requests.exceptions.Timeout:
        logger.error("OxaPay API request timed out")
        raise Exception("Payment service timeout. Please try again.")
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error while creating OxaPay payment: {e}")
        raise Exception(f"Network error: {str(e)}")
    
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OxaPay response: {e}")
        raise Exception("Invalid response from payment service")
    
    except Exception as e:
        logger.error(f"Unexpected error creating OxaPay payment: {e}", exc_info=True)
        raise


def verify_webhook_signature(webhook_secret: str, received_secret: str) -> bool:
    """
    Verify OxaPay webhook signature
    
    Args:
        webhook_secret: Expected secret from config
        received_secret: Secret received from webhook
    
    Returns:
        bool: True if signature is valid
    """
    if not webhook_secret or not received_secret:
        logger.warning("Missing webhook secret for verification")
        return False
    
    is_valid = webhook_secret == received_secret
    
    if not is_valid:
        logger.warning(f"Invalid webhook signature: expected={webhook_secret}, received={received_secret}")
    
    return is_valid
