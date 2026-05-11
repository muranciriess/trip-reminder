import os

SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
DATABASE = os.environ.get('DATABASE', os.path.join(os.path.dirname(__file__), 'trip.db'))
TIMEZONE = 'Asia/Shanghai'

MORNING_REMINDER_HOUR = int(os.environ.get('MORNING_REMINDER_HOUR', 8))
MORNING_REMINDER_MINUTE = int(os.environ.get('MORNING_REMINDER_MINUTE', 0))
EVENING_REMINDER_HOUR = int(os.environ.get('EVENING_REMINDER_HOUR', 21))
EVENING_REMINDER_MINUTE = int(os.environ.get('EVENING_REMINDER_MINUTE', 0))
BEFORE_TRIP_CHECK_INTERVAL_MINUTES = int(os.environ.get('BEFORE_TRIP_CHECK_INTERVAL_MINUTES', 5))

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '1234')

# HF Spaces 自动提供 SPACE_HOST 环境变量，格式: username-spacename.hf.space
_space_host = os.environ.get('SPACE_HOST', '')
KEEP_ALIVE_URL = f"https://{_space_host}/student" if _space_host else ''

QR_CODE_DIR = os.path.join(os.path.dirname(__file__), 'static')
