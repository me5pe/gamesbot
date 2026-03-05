from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List
from telegram import User


@dataclass
class RockPaperScissorsPlayer:
    """Игрок в камень-ножницы-бумага"""
    user: User
    wins: int = 0
    current_choice: Optional[str] = None  # "rock", "paper", "scissors"
    
    def reset_choice(self):
        """Сбрасывает текущий выбор для нового раунда"""
        self.current_choice = None


class RockPaperScissorsGame:
    """Игра Камень-Ножницы-Бумага"""
    
    # Правила игры
    BEATS = {
        "rock": "scissors",      # камень бьет ножницы
        "scissors": "paper",     # ножницы режут бумагу
        "paper": "rock"          # бумага оборачивает камень
    }
    
    EMOJI = {
        "rock": "🗿",
        "paper": "📄",
        "scissors": "✂️"
    }
    
    NAMES = {
        "rock": "Камень",
        "paper": "Бумага",
        "scissors": "Ножницы"
    }
    
    def __init__(self, game_id: str, challenger: User, target_username: str, bet_amount: float, chat_id: int):
        self.game_id = game_id
        self.challenger = challenger
        self.target_username = target_username
        self.target_user: Optional[User] = None
        self.bet_amount = bet_amount
        self.chat_id = chat_id
        self.status = "waiting"  # waiting, payment_pending, playing, finished, cancelled, declined
        
        # Платежи
        self.challenger_payment_id: Optional[str] = None
        self.target_payment_id: Optional[str] = None
        self.challenger_paid = False
        self.target_paid = False
        
        # Игроки
        self.challenger_player: Optional[RockPaperScissorsPlayer] = None
        self.target_player: Optional[RockPaperScissorsPlayer] = None
        
        # Игровые данные
        self.current_round = 1
        self.rounds_history: List[Dict] = []  # История раундов
        self.wins_needed = 3  # Побед для победы
        
        # Временные метки
        self.created_at = datetime.now()
        self.payment_start_time: Optional[datetime] = None
        self.message_id: Optional[int] = None
        
    def initialize_players(self):
        """Инициализирует игроков"""
        if not self.challenger_player:
            self.challenger_player = RockPaperScissorsPlayer(user=self.challenger)
        if not self.target_player and self.target_user:
            self.target_player = RockPaperScissorsPlayer(user=self.target_user)
    
    def set_choice(self, user_id: int, choice: str) -> bool:
        """Устанавливает выбор игрока (rock/paper/scissors)"""
        if user_id == self.challenger.id and self.challenger_player:
            self.challenger_player.current_choice = choice
            return True
        elif self.target_user and user_id == self.target_user.id and self.target_player:
            self.target_player.current_choice = choice
            return True
        return False
    
    def has_both_choices(self) -> bool:
        """Проверяет, сделали ли оба игрока выбор"""
        return (self.challenger_player and self.challenger_player.current_choice is not None and
                self.target_player and self.target_player.current_choice is not None)
    
    def get_round_winner(self) -> Optional[str]:
        """Определяет победителя раунда. Возвращает 'challenger', 'target' или 'draw'"""
        if not self.has_both_choices():
            return None
        
        c_choice = self.challenger_player.current_choice
        t_choice = self.target_player.current_choice
        
        if c_choice == t_choice:
            return "draw"
        
        # Проверяем, бьет ли выбор challenger выбор target
        if self.BEATS[c_choice] == t_choice:
            return "challenger"
        else:
            return "target"
    
    def process_round(self) -> Dict:
        """Обрабатывает текущий раунд и возвращает результат"""
        winner = self.get_round_winner()
        
        round_result = {
            "round": self.current_round,
            "challenger_choice": self.challenger_player.current_choice,
            "target_choice": self.target_player.current_choice,
            "winner": winner,
            "challenger_wins": self.challenger_player.wins,
            "target_wins": self.target_player.wins
        }
        
        # Увеличиваем счет победителя
        if winner == "challenger":
            self.challenger_player.wins += 1
        elif winner == "target":
            self.target_player.wins += 1
        
        # Обновляем счет после увеличения
        round_result["challenger_wins"] = self.challenger_player.wins
        round_result["target_wins"] = self.target_player.wins
        
        # Сохраняем в историю
        self.rounds_history.append(round_result)
        
        # Готовимся к следующему раунду (только если игра не закончена)
        if not self.is_game_finished():
            self.current_round += 1
            self.challenger_player.reset_choice()
            self.target_player.reset_choice()
        
        return round_result
    
    def is_game_finished(self) -> bool:
        """Проверяет, закончена ли игра (кто-то набрал 3 победы)"""
        if not self.challenger_player or not self.target_player:
            return False
        return (self.challenger_player.wins >= self.wins_needed or 
                self.target_player.wins >= self.wins_needed)
    
    def get_game_winner(self) -> Optional[User]:
        """Возвращает победителя игры или None, если игра не закончена"""
        if not self.is_game_finished():
            return None
        
        if self.challenger_player.wins >= self.wins_needed:
            return self.challenger
        elif self.target_player.wins >= self.wins_needed:
            return self.target_user
        return None
    
    def get_loser(self) -> Optional[User]:
        """Возвращает проигравшего"""
        winner = self.get_game_winner()
        if not winner:
            return None
        
        if winner.id == self.challenger.id:
            return self.target_user
        else:
            return self.challenger
    
    def format_round_result(self, round_result: Dict) -> str:
        """Форматирует результат раунда для отображения"""
        c_choice = round_result["challenger_choice"]
        t_choice = round_result["target_choice"]
        winner = round_result["winner"]
        
        c_emoji = self.EMOJI[c_choice]
        t_emoji = self.EMOJI[t_choice]
        c_name = self.NAMES[c_choice]
        t_name = self.NAMES[t_choice]
        
        result_text = (
            f"🎮 <b>Раунд {round_result['round']}</b>\n\n"
            f"{c_emoji} @{self.challenger.username}: <b>{c_name}</b>\n"
            f"{t_emoji} @{self.target_user.username}: <b>{t_name}</b>\n\n"
        )
        
        if winner == "draw":
            result_text += "🤝 <b>Ничья!</b> Переигровка раунда.\n"
        elif winner == "challenger":
            result_text += f"🏆 Победа @{self.challenger.username}!\n"
        else:
            result_text += f"🏆 Победа @{self.target_user.username}!\n"
        
        result_text += f"\n📊 Счет: {round_result['challenger_wins']} - {round_result['target_wins']}"
        
        return result_text
    
    def get_score_text(self) -> str:
        """Возвращает текущий счет игры"""
        if not self.challenger_player or not self.target_player:
            return ""
        
        return f"📊 Счет: {self.challenger_player.wins} - {self.target_player.wins}"
