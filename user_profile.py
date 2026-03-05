import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

class UserProfile:
    """Профиль пользователя"""
    def __init__(self, user_id: int, username: str):
        self.user_id = user_id
        self.username = username
        self.total_games = 0
        self.wins = 0
        self.losses = 0
        self.total_wagered = 0.0
        self.total_won = 0.0
        self.balance = 0.0
        self.created_at = datetime.now()
    
    def to_dict(self) -> Dict:
        """Сериализует профиль в словарь"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "total_games": self.total_games,
            "wins": self.wins,
            "losses": self.losses,
            "total_wagered": self.total_wagered,
            "total_won": self.total_won,
            "balance": self.balance,
            "created_at": self.created_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        """Создает профиль из словаря"""
        profile = cls(data["user_id"], data.get("username") or "")
        profile.total_games = data.get("total_games", 0)
        profile.wins = data.get("wins", 0)
        profile.losses = data.get("losses", 0)
        profile.total_wagered = data.get("total_wagered", 0.0)
        profile.total_won = data.get("total_won", 0.0)
        profile.balance = data.get("balance", 0.0)
        
        created_at = data.get("created_at")
        if created_at:
            try:
                profile.created_at = datetime.fromisoformat(created_at)
            except ValueError:
                logger.warning("Некорректный формат даты created_at для пользователя %s", data["user_id"])
        return profile
        
    def add_game_result(self, won: bool, wager: float, payout: float = 0):
        """Добавляет результат игры в профиль"""
        self.total_games += 1
        self.total_wagered += wager
        
        if won:
            self.wins += 1
            self.total_won += payout
        else:
            self.losses += 1
            
    def get_win_rate(self) -> float:
        """Возвращает процент побед"""
        if self.total_games == 0:
            return 0.0
        return (self.wins / self.total_games) * 100
        
    def get_profit(self) -> float:
        """Возвращает чистую прибыль"""
        return self.total_won - self.total_wagered

class ProfileManager:
    """Менеджер профилей пользователей"""
    def __init__(self, storage_path: Optional[str] = None):
        self.profiles: Dict[int, UserProfile] = {}
        default_dir = Path(__file__).resolve().parent / "data"
        data_dir = Path(os.getenv("DATA_DIR", str(default_dir)))
        if storage_path:
            self.storage_path = Path(storage_path)
        else:
            self.storage_path = data_dir / "profiles.json"
        self._load_profiles()
    
    def _ensure_storage_dir(self):
        """Создает директорию для хранения при необходимости"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _load_profiles(self):
        """Загружает профили из JSON файла"""
        if not self.storage_path.exists():
            return
        
        try:
            with self.storage_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for user_data in data.values():
                profile = UserProfile.from_dict(user_data)
                self.profiles[profile.user_id] = profile
            logger.info("Загружено %d профилей пользователей", len(self.profiles))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Не удалось загрузить профили пользователей: %s", e)
    
    def save_profiles(self):
        """Сохраняет профили в JSON файл"""
        self._ensure_storage_dir()
        data = {str(user_id): profile.to_dict() for user_id, profile in self.profiles.items()}
        try:
            with self.storage_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("Не удалось сохранить профили пользователей: %s", e)
        
    def get_profile(self, user_id: int, username: str) -> UserProfile:
        """Получает или создает профиль пользователя"""
        if user_id not in self.profiles:
            profile = UserProfile(user_id, username or "")
            self.profiles[user_id] = profile
            self.save_profiles()
            return profile
        
        profile = self.profiles[user_id]
        if username:
            profile.username = username
        return profile
        
    def format_profile_text(self, user_id: int, username: str) -> str:
        """Форматирует текст профиля пользователя"""
        profile = self.get_profile(user_id, username)
        
        return (
            f"👤 <b>Профиль @{username}</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"Всего игр: {profile.total_games}\n"
            f"Побед: {profile.wins}\n"
            f"Поражений: {profile.losses}\n"
            f"Процент побед: {profile.get_win_rate():.1f}%\n\n"
            f"💰 <b>Финансы:</b>\n"
            f"Всего поставлено: {profile.total_wagered:.2f} USDT\n"
            f"Всего выиграно: {profile.total_won:.2f} USDT\n"
            f"Прибыль: {profile.get_profit():.2f} USDT\n"
        )
    
    def get_top_players_by_wagered(self, limit: int = 10) -> List[Tuple[UserProfile, int]]:
        """Возвращает топ игроков по сумме ставок"""
        # Обновляем профили из файла перед получением топа
        self._load_profiles()
        
        # Фильтруем только игроков с ненулевыми ставками
        players_with_wagers = [
            (profile, profile.total_wagered) 
            for profile in self.profiles.values() 
            if profile.total_wagered > 0
        ]
        
        # Сортируем по убыванию суммы ставок
        players_with_wagers.sort(key=lambda x: x[1], reverse=True)
        
        # Возвращаем топ N игроков с их позицией
        return [(profile, idx + 1) for idx, (profile, _) in enumerate(players_with_wagers[:limit])]
    
    def format_top_players_text(self, limit: int = 10) -> str:
        """Форматирует текст топа игроков по ставкам"""
        top_players = self.get_top_players_by_wagered(limit)
        
        if not top_players:
            return (
                "🏆 <b>Топ-10 игроков:</b>\n\n"
                "Пока нет игроков с активностью.\n"
                "Сыграйте первую игру, чтобы попасть в топ!"
            )
        
        text = f"🏆 <b>Топ-{len(top_players)} игроков:</b>\n\n"
        
        medals = ["🥇", "🥈", "🥉"]
        for profile, position in top_players:
            medal = medals[position - 1] if position <= 3 else f"{position}."
            username = f"@{profile.username}" if profile.username else f"ID: {profile.user_id}"
            text += (
                f"{medal} {username}\n"
                f"   💰 Поставлено: {profile.total_wagered:.2f} USDT\n"
                f"   💵 Выиграно: {profile.total_won:.2f} USDT\n"
                f"   🎮 Игр: {profile.total_games} | Побед: {profile.wins}\n\n"
            )
        
        return text

