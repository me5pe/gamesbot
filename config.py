import os
import logging
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройки бота
BOT_TOKEN = os.getenv('BOT_TOKEN')
CRYPTOBOT_TOKEN = os.getenv('CRYPTOBOT_TOKEN')
ADMIN_USER_IDS_ENV = os.getenv('ADMIN_USER_IDS')
if ADMIN_USER_IDS_ENV:
    ADMIN_USER_IDS = {
        int(user_id.strip())
        for user_id in ADMIN_USER_IDS_ENV.split(',')
        if user_id.strip()
    }
else:
    single_admin_id = os.getenv('ADMIN_USER_ID')
    ADMIN_USER_IDS = {int(single_admin_id)} if single_admin_id else set()

COMMISSION_RATE = float(os.getenv('COMMISSION_RATE', 0.08))

# Настройки ставок
MIN_BET = float(os.getenv('MIN_BET', 0.1))
MAX_BET = float(os.getenv('MAX_BET', 5000.0))

# Настройки логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Проверяем обязательные переменные
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")
if not CRYPTOBOT_TOKEN:
    raise ValueError("CRYPTOBOT_TOKEN не установлен!")
if not ADMIN_USER_IDS:
    raise ValueError("ADMIN_USER_IDS или ADMIN_USER_ID не установлен!")
