# =========================
# BOT CONFIGURATION
# =========================
import os

BOT_TOKEN = "8577775254:AAFspU4Hjr-IMyaf1YXLvyO0OI5K0P02-wU"
BOT_USERNAME = "Optic_S_Shop_bot"

# Force join (private invite link supported)
FORCE_JOIN_CHANNEL = "https://t.me/+4nRUAm6UdUUwYWQ9"

START_IMAGE = "https://i.postimg.cc/z8VTfHV1/start.jpg"

# Support username (without @)
SUPPORT_USERNAME = "Soulxmerchant"

# =========================
# ADMIN SETTINGS
# =========================
ADMIN_IDS = [
    7677304116
]

# =========================
# MongoDB
# =========================
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://ottbot:ottbot@cluster0.hjj08pf.mongodb.net/ottbot_db?retryWrites=true&w=majority",
)
DB_NAME = os.getenv("DB_NAME", "ottbot_db")

# =========================
# PAYMENT CONFIG
# =========================
PAYMENTS = {
    "razorpay": {
        "name": "UPI (Razorpay)",
        "note": "Instant verification - All UPI apps supported",
    },
    "oxapay": {
        "name": "Crypto (USDT)",
        "note": "Pay with USDT TRC20 - Fast and low fees",
    },
}

# =========================
# OXAPAY CONFIG
# =========================
OXAPAY_API_KEY = "M5XPON-2LRZT4-YHNYYV-TPS0RQ"
# Correct API URL for invoice creation
OXAPAY_MERCHANT_API_URL = "https://api.oxapay.com/v1/payment/invoice"
# Cloudflare Tunnel webhook URL (example)
# Set this in OxaPay Dashboard callback/webhook URL:
#   https://YOUR_TUNNEL.trycloudflare.com/oxapay/webhook
OXAPAY_WEBHOOK_URL = os.getenv(
    "OXAPAY_WEBHOOK_URL",
    "https://YOUR_TUNNEL.trycloudflare.com/oxapay/webhook",
)
# Note: OxaPay doesn't use webhook signatures, security via track_id validation

# =========================
# RAZORPAY CONFIG
# =========================
# Get these from Razorpay Dashboard (https://dashboard.razorpay.com/app/keys)
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_live_SA6CCXikZsbELv")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "g7gFFQS1yk9csCUbEeLc339b")

# Webhook secret for payment verification
# Configure this same value in Razorpay Dashboard webhook settings
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "change-me")

# Cloudflare Tunnel webhook URL (example)
# Set this in Razorpay Dashboard:
#   https://YOUR_TUNNEL.trycloudflare.com/razorpay/webhook
RAZORPAY_WEBHOOK_URL = os.getenv(
    "RAZORPAY_WEBHOOK_URL",
    "https://YOUR_TUNNEL.trycloudflare.com/razorpay/webhook",
)

# Bot runs on port 8000 (FastAPI server)
RAZORPAY_WEBHOOK_PORT = int(os.getenv("RAZORPAY_WEBHOOK_PORT", "8000"))

# =========================
# EXCHANGE RATE
# =========================
# Used for displaying wallet balance in both currencies
USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_RATE", "90.0"))  # 1 USD = 90 INR

# =========================
# Product images (used in Buy Products screens)
# =========================
# The bot will pick an image based on keywords in product name.
PRODUCT_IMAGE_RULES = [
    ("adobe", "https://www.dolphincomputer.co.in/wp-content/uploads/2024/05/3-1080x599.png"),
    ("chatgpt", "https://www.internetmatters.org/wp-content/uploads/2025/06/Chat-GPT-logo.webp"),
    ("gemini", "https://www.gstatic.com/lamda/images/gemini_aurora_thumbnail_4g_e74822ff0ca4259beb718.png"),
    ("github student", "https://muralisugumar.com/wp-content/uploads/2024/11/maxresdefault.jpg"),
    ("perplexity", "https://cdn.analyticsvidhya.com/wp-content/uploads/2025/08/Everything-you-need-to-know-about-Perplexity-Pro.webp"),
    ("amazon blank", "https://m.media-amazon.com/images/I/31epF-8N9LL.png"),
    ("amazon prime", "https://images.moneycontrol.com/static-mcnews/2024/07/20240725074952_WhatsApp-Image-2024-07-25-at-12.17.26.jpeg"),
    ("spotify", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTZHkw8hJp2hf8jYZ3fvIgTyOvsEJ1xo8Hefw&s"),
    ("youtube", "https://www.gstatic.com/youtube/img/promos/growth/YTP_logo_social_1200x630.png?days_since_epoch=20461"),
    ("nordvpn", "https://static0.howtogeekimages.com/wordpress/wp-content/uploads/2025/01/nordvpn-3.jpeg"),
    ("surfshark", "https://www.thefastmode.com/media/k2/items/src/62ac1f5c67d50ada30f1e7759102fb13.jpg?t=20230802_100215"),
]
