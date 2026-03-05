import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

class CheckInfo:
    """Информация о чеке"""
    def __init__(self, check_id: int, check_link: str, user_id: int, amount: float, game_id: str, created_at: str = None):
        self.check_id = check_id
        self.check_link = check_link
        self.user_id = user_id
        self.amount = amount
        self.game_id = game_id
        self.created_at = created_at or datetime.now().isoformat()
        self.cancelled = False
        self.cancelled_at: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Сериализует информацию о чеке в словарь"""
        return {
            "check_id": self.check_id,
            "check_link": self.check_link,
            "user_id": self.user_id,
            "amount": self.amount,
            "game_id": self.game_id,
            "created_at": self.created_at,
            "cancelled": self.cancelled,
            "cancelled_at": self.cancelled_at
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        """Создает информацию о чеке из словаря"""
        check = cls(
            data["check_id"],
            data["check_link"],
            data["user_id"],
            data["amount"],
            data["game_id"],
            data.get("created_at")
        )
        check.cancelled = data.get("cancelled", False)
        check.cancelled_at = data.get("cancelled_at")
        return check

class CheckManager:
    """Менеджер чеков"""
    def __init__(self, storage_path: Optional[str] = None):
        self.checks: Dict[int, CheckInfo] = {}  # check_id -> CheckInfo
        default_dir = Path(__file__).resolve().parent / "data"
        data_dir = Path(os.getenv("DATA_DIR", str(default_dir)))
        if storage_path:
            self.storage_path = Path(storage_path)
        else:
            self.storage_path = data_dir / "checks.json"
        self._load_checks()
    
    def _ensure_storage_dir(self):
        """Создает директорию для хранения при необходимости"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _load_checks(self):
        """Загружает чеки из JSON файла"""
        if not self.storage_path.exists():
            return
        
        try:
            with self.storage_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for check_data in data.values():
                check = CheckInfo.from_dict(check_data)
                self.checks[check.check_id] = check
            logger.info("Загружено %d чеков", len(self.checks))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Не удалось загрузить чеки: %s", e)
    
    def save_checks(self):
        """Сохраняет чеки в JSON файл"""
        self._ensure_storage_dir()
        data = {str(check_id): check.to_dict() for check_id, check in self.checks.items()}
        try:
            with self.storage_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("Не удалось сохранить чеки: %s", e)
    
    def add_check(self, check_data: Dict) -> Optional[CheckInfo]:
        """Добавляет чек в хранилище
        
        Args:
            check_data: Словарь с данными чека (check_id, check_link, user_id, amount, game_id, created_at)
            
        Returns:
            CheckInfo или None в случае ошибки
        """
        try:
            check_id = check_data.get("check_id")
            if not check_id:
                logger.error("check_id отсутствует в данных чека")
                return None
            
            check = CheckInfo(
                check_id=check_id,
                check_link=check_data.get("check_link", ""),
                user_id=check_data.get("user_id", 0),
                amount=check_data.get("amount", 0.0),
                game_id=check_data.get("game_id", ""),
                created_at=check_data.get("created_at")
            )
            
            self.checks[check_id] = check
            self.save_checks()
            logger.info(f"Чек {check_id} добавлен в хранилище")
            return check
        except Exception as e:
            logger.error(f"Ошибка при добавлении чека: {e}")
            return None
    
    def get_check(self, check_id: int) -> Optional[CheckInfo]:
        """Получает информацию о чеке по ID"""
        return self.checks.get(check_id)
    
    def mark_cancelled(self, check_id: int):
        """Помечает чек как отмененный"""
        check = self.checks.get(check_id)
        if check:
            check.cancelled = True
            check.cancelled_at = datetime.now().isoformat()
            self.save_checks()
            logger.info(f"Чек {check_id} помечен как отмененный")
    
    def get_active_checks(self, limit: int = 50) -> List[CheckInfo]:
        """Возвращает список активных (не отмененных) чеков
        
        Args:
            limit: Максимальное количество чеков для возврата
            
        Returns:
            Список CheckInfo, отсортированный по дате создания (новые первыми)
        """
        active_checks = [check for check in self.checks.values() if not check.cancelled]
        # Сортируем по дате создания (новые первыми)
        active_checks.sort(key=lambda x: x.created_at, reverse=True)
        return active_checks[:limit]
    
    def get_checks_by_user(self, user_id: int, limit: int = 50) -> List[CheckInfo]:
        """Возвращает чеки конкретного пользователя"""
        user_checks = [check for check in self.checks.values() if check.user_id == user_id]
        user_checks.sort(key=lambda x: x.created_at, reverse=True)
        return user_checks[:limit]
    
    def format_check_list(self, checks: List[CheckInfo], max_items: int = 20) -> str:
        """Форматирует список чеков для отображения"""
        if not checks:
            return "📋 <b>Чеки не найдены</b>"
        
        text = f"📋 <b>Список чеков</b> (показано {min(len(checks), max_items)} из {len(checks)})\n\n"
        
        for i, check in enumerate(checks[:max_items], 1):
            status = "❌ Отменен" if check.cancelled else "✅ Активен"
            created_date = datetime.fromisoformat(check.created_at).strftime("%d.%m.%Y %H:%M")
            text += (
                f"{i}. <code>{check.check_id}</code> | {check.amount} USDT\n"
                f"   Пользователь: {check.user_id} | Игра: {check.game_id}\n"
                f"   Статус: {status} | Создан: {created_date}\n\n"
            )
        
        return text
