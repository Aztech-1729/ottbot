"""
Razorpay Payment Handler
Handles UPI QR code generation and verification
"""
import razorpay
import requests
from requests.auth import HTTPBasicAuth
from config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET
import logging

logger = logging.getLogger(__name__)


class RazorpayHandler:
    def __init__(self):
        if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
            raise ValueError("Razorpay credentials not configured in config.py")
        
        self.client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        self.auth = HTTPBasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    
    def create_upi_qr(self, amount_usd: float, user_id: int, username: str = None) -> dict:
        """
        Create a Razorpay UPI QR Code using direct API (working!)
        
        Args:
            amount_usd: Amount in USD (will be used as INR for display)
            user_id: Telegram user ID
            username: Telegram username (optional)
        
        Returns:
            dict with 'qr_code_id', 'image_url', 'amount'
        """
        try:
            import time
            import json
            
            # Convert USD to paise (treating $1 as ₹1 for wallet balance)
            amount_inr = int(amount_usd)
            amount_paise = amount_inr * 100
            
            display_name = f"@{username}" if username else f"User {user_id}"
            
            # Prepare minimal payload that works
            payload = {
                "type": "upi_qr",
                "usage": "single_use",
                "fixed_amount": True,
                "payment_amount": amount_paise,
                "description": f"Credits for {display_name}",
                "notes": {
                    "telegram_user_id": str(user_id),
                    "username": username or "",
                }
            }
            
            # Direct API call (this works!)
            url = "https://api.razorpay.com/v1/payments/qr_codes"
            
            response = requests.post(
                url,
                auth=self.auth,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=10
            )
            
            if response.status_code != 200:
                error_msg = response.text
                logger.error(f"Razorpay API error: {response.status_code} - {error_msg}")
                raise Exception(f"API Error {response.status_code}: {error_msg}")
            
            qr_code = response.json()
            logger.info(f"QR Code created successfully: {qr_code.get('id')}")
            
            return {
                "qr_code_id": qr_code['id'],
                "image_url": qr_code['image_url'],
                "amount": amount_inr,
                "status": qr_code.get('status', 'active')
            }
        except Exception as e:
            logger.error(f"Failed to create UPI QR code: {e}")
            raise
    
    def verify_payment_signature(self, payment_link_id: str, payment_link_reference_id: str, 
                                  payment_link_status: str, razorpay_signature: str) -> bool:
        """
        Verify Razorpay webhook signature
        
        Args:
            payment_link_id: Payment link ID
            payment_link_reference_id: Reference ID
            payment_link_status: Payment status
            razorpay_signature: Signature from webhook
        
        Returns:
            bool: True if signature is valid
        """
        try:
            # Construct the expected signature payload
            payload = f"{payment_link_id}|{payment_link_reference_id}|{payment_link_status}"
            
            # Verify signature using Razorpay utility
            self.client.utility.verify_payment_link_signature({
                'payment_link_id': payment_link_id,
                'payment_link_reference_id': payment_link_reference_id,
                'payment_link_status': payment_link_status,
                'razorpay_signature': razorpay_signature
            })
            return True
        except razorpay.errors.SignatureVerificationError:
            logger.warning("Invalid Razorpay signature")
            return False
        except Exception as e:
            logger.error(f"Error verifying signature: {e}")
            return False
    
    def get_payment_details(self, payment_link_id: str) -> dict:
        """
        Get payment link details
        
        Args:
            payment_link_id: Payment link ID
        
        Returns:
            dict with payment details
        """
        try:
            payment_link = self.client.payment_link.fetch(payment_link_id)
            return {
                "id": payment_link['id'],
                "amount": payment_link['amount'] // 100,  # Convert paise to rupees
                "status": payment_link['status'],
                "user_id": payment_link.get('notes', {}).get('user_id'),
            }
        except Exception as e:
            logger.error(f"Failed to fetch payment details: {e}")
            raise
    
    def fetch_qr_image(self, image_url: str) -> bytes:
        """
        Fetch QR code image from Razorpay and crop to show only QR code
        
        Args:
            image_url: Razorpay QR code image URL
        
        Returns:
            bytes: Cropped QR code image data
        """
        try:
            from PIL import Image
            from io import BytesIO
            
            # Fetch the original image
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            
            # Open image with PIL
            img = Image.open(BytesIO(response.content))
            
            # Crop to remove text at bottom (keep only QR code)
            # Razorpay QR images typically have the QR code at the top
            # and text at the bottom, so we crop the bottom portion
            width, height = img.size
            
            # Crop: remove bottom 25% (where text/name appears)
            crop_height = int(height * 0.75)
            cropped_img = img.crop((0, 0, width, crop_height))
            
            # Convert to bytes
            output = BytesIO()
            cropped_img.save(output, format='PNG')
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Failed to fetch/crop QR image: {e}")
            raise
