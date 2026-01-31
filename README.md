# 🛒 OTT Shop Bot - Digital Products Marketplace

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![aiogram](https://img.shields.io/badge/aiogram-3.7.0-blue.svg)](https://docs.aiogram.dev/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Telegram](https://img.shields.io/badge/Developer-@aztechdeveloper-blue.svg)](https://t.me/aztechdeveloper)

A professional Telegram bot for selling digital products (OTT subscriptions, premium accounts, etc.) with integrated payment gateways and automated delivery system.

## ✨ Features

### 💳 Payment Integration
- **Razorpay UPI** - Instant QR code payments (₹ INR)
- **OxaPay Crypto** - Multi-cryptocurrency support ($ USD)
- Automatic payment verification via webhooks
- Dual currency display (INR/USD)
- Real-time wallet crediting

### 🛍️ Product Management
- Unlimited products and categories
- Stock management system
- Bulk import/export capabilities
- Product enable/disable controls
- Custom product images

### 👥 User Features
- Wallet system with auto-recharge
- Order history tracking
- Instant digital delivery
- Dual currency balance display
- Referral system ready

### 🎛️ Admin Panel
- Complete dashboard
- User management
- Payment approval system
- Order management
- Sales analytics
- Product stock monitoring

### 🔒 Security
- Webhook signature verification
- Duplicate payment protection
- Atomic transaction handling
- MongoDB with async operations
- Input validation and sanitization

## 📋 Requirements

- Python 3.8 or higher
- MongoDB database
- Razorpay account (for UPI payments)
- OxaPay account (for crypto payments)
- Cloudflare tunnel (for webhooks)

## 🚀 Installation

### 1. Clone Repository
```bash
git clone <your-repo-url>
cd OTT_SELL_BOT
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Settings
Edit `config.py` with your credentials:

```python
# Bot Configuration
BOT_TOKEN = "your_bot_token"
BOT_USERNAME = "your_bot_username"

# MongoDB
MONGO_URI = "your_mongodb_uri"

# Razorpay
RAZORPAY_KEY_ID = "your_razorpay_key"
RAZORPAY_KEY_SECRET = "your_razorpay_secret"

# OxaPay
OXAPAY_API_KEY = "your_oxapay_key"

# Admin IDs
ADMIN_IDS = [123456789]
```

### 4. Setup Cloudflare Tunnel
```bash
# Install cloudflared
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared-linux-amd64
sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared

# Start tunnel
cloudflared tunnel --url http://localhost:8000
```

### 5. Configure Webhooks

**Razorpay Dashboard:**
- URL: `https://your-tunnel.trycloudflare.com/razorpay/webhook`
- Event: `payment.captured`
- Secret: Set in config.py

**OxaPay Dashboard:**
- Callback URL: `https://your-tunnel.trycloudflare.com/oxapay/webhook`

### 6. Run Bot
```bash
python bot.py
```

## 📁 Project Structure

```
OTT_SELL_BOT/
├── bot.py                      # Main bot application
├── config.py                   # Configuration settings
├── requirements.txt            # Python dependencies
├── razorpay_handler.py        # Razorpay payment handler
├── razorpay_webhook.py        # Razorpay webhook processor
├── oxapay.py                  # OxaPay payment handler
├── reset_orders_and_stock.py # Database utility script
└── README.md                  # Documentation
```

## 💰 Payment Flow

### Razorpay (UPI)
1. User selects amount in INR
2. Bot generates unique QR code
3. User scans and pays via any UPI app
4. Webhook confirms payment instantly
5. Wallet credited automatically

**Conversion:** `1 INR = 1 Credit`

### OxaPay (Crypto)
1. User selects amount in USD
2. Bot creates payment invoice
3. User pays with crypto (USDT, BTC, ETH, etc.)
4. Webhook confirms payment
5. Wallet credited automatically

**Conversion:** `1 USD = 90 Credits (₹90)`

## 🎨 User Interface

### Main Menu
- 🛒 Buy Products
- 💰 Add Funds
- 👤 My Profile
- 📦 My Orders
- 💬 Support

### Admin Panel
- 📊 Statistics
- 📦 Products Management
- 💳 Payments Management
- 👥 Users Management
- ⚙️ Settings

## 🔧 Configuration Options

### Exchange Rate
```python
USD_TO_INR_RATE = 90.0  # 1 USD = 90 INR
```

### Payment Methods
```python
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
```

## 🛠️ Utilities

### Reset Orders & Stock
```bash
python reset_orders_and_stock.py
```
Clears all orders and stock while preserving products and users.

## 📊 Database Schema

### Collections
- **users** - User accounts and wallets
- **products** - Product catalog
- **stocks** - Digital product inventory
- **orders** - Purchase history
- **payments** - Payment transactions

## 🔐 Security Features

- ✅ Webhook signature verification
- ✅ Track ID validation
- ✅ Duplicate payment prevention
- ✅ Atomic database operations
- ✅ Input sanitization
- ✅ MongoDB injection protection

## 🐛 Troubleshooting

### Webhook Not Working
1. Check tunnel is running: `ps aux | grep cloudflared`
2. Verify webhook URL in dashboards
3. Check logs: `tail -f bot.log`

### Payment Not Credited
1. Check webhook logs
2. Verify signature/secret match
3. Check database for payment record

### Bot Not Responding
1. Check bot process: `ps aux | grep python`
2. View logs: `tail -f bot.log`
3. Verify MongoDB connection

## 📝 API Documentation

### Webhook Endpoints

**Razorpay:**
```
POST /razorpay/webhook
Headers: x-razorpay-signature
Event: payment.captured
```

**OxaPay:**
```
POST /oxapay/webhook
Events: payment.paid, payment.confirming, payment.completed
```

## 🔄 Updates & Maintenance

### Updating Dependencies
```bash
pip install --upgrade -r requirements.txt
```

### Backing Up Database
```bash
mongodump --uri="your_mongodb_uri" --out=/backup/path
```

## 📈 Scaling

- Use MongoDB replica sets for high availability
- Deploy multiple bot instances with load balancing
- Use Redis for session management (optional)
- Implement rate limiting for API calls

## 🤝 Support

For issues, questions, or custom development:

- **Developer:** [@aztechdeveloper](https://t.me/aztechdeveloper)
- **Telegram:** t.me/aztechdeveloper

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Credits

**Developed by:** [AZ Tech Developer](https://t.me/aztechdeveloper)

**Technologies Used:**
- [aiogram](https://github.com/aiogram/aiogram) - Telegram Bot Framework
- [FastAPI](https://fastapi.tiangolo.com/) - Webhook Server
- [MongoDB](https://www.mongodb.com/) - Database
- [Razorpay](https://razorpay.com/) - UPI Payment Gateway
- [OxaPay](https://oxapay.com/) - Crypto Payment Gateway

## ⚠️ Disclaimer

This bot is for educational and commercial use. Ensure compliance with local laws and regulations regarding digital product sales and payment processing.

---

**Made with ❤️ by [AZ Tech Developer](https://t.me/aztechdeveloper)**

*Professional Telegram Bot Development Services Available*
