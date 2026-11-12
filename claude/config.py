import os
from dotenv import load_dotenv

load_dotenv()

# Gemini API
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'

# Alerts (ntfy recommended — works in Pakistan, no VPN)
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '').strip() or None
NTFY_TOPIC = os.getenv('NTFY_TOPIC', '').strip() or None

# Email fallback (Gmail app password)
SMTP_HOST = os.getenv('SMTP_HOST', '').strip() or None
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '').strip() or None
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '').strip() or None
ALERT_EMAIL_TO = os.getenv('ALERT_EMAIL_TO', '').strip() or None

# Risk thresholds
MAX_DD_INTRADAY_PCT = 2.0
DAILY_LOSS_LIMIT_PCT = 3.0
CORRELATION_THRESHOLD = 0.85

# Job schedules (UTC)
PRE_SESSION_TIME = "07:45"
POST_SESSION_TIME = "21:00"
WEEKLY_CORRELATION_TIME = "08:00"   # Sunday UTC
INTRADAY_CHECK_INTERVAL = 15 * 60   # seconds
