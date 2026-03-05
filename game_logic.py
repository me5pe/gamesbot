import random
from datetime import datetime
from typing import Optional, Dict, List
from telegram import User

class DiceGame:
    def __init__(self, game_id: str, challenger: User, target_username: str, bet_amount: float, chat_id: int, dice_count: int = 3):
        self.game_id = game_id
        self.challenger = challenger
        self.target_username = target_username
        self.target_user: Optional[User] = None
        self.bet_amount = bet_amount
        self.chat_id = chat_id
        self.status = "waiting"  # waiting, payment_pending, playing, finished, cancelled, declined, expired
        self.dice_count = max(1, min(3, dice_count))  # Количество бросков (1-3)
        self.challenger_payment_id: Optional[str] = None
        self.target_payment_id: Optional[str] = None
        self.challenger_paid = False
        self.target_paid = False
        self.is_rematch = False  # Флаг переигровки при ничье
        
        # Игровые данные
        self.challenger_rolls: List[int] = []
        self.target_rolls: List[int] = []
        self.challenger_score = 0
        self.target_score = 0
        self.current_player = "challenger"  # challenger или target
        self.current_roll = 1  # Номер текущего броска (1..dice_count)
        
        # Временные поля для отслеживания бросков
        self.pending_dice_message_id: Optional[int] = None
        self.message_id: Optional[int] = None  # ID сообщения вызова для удаления при отмене
        
        self.created_at = datetime.now()
        self.payment_start_time: Optional[datetime] = None  # Время начала отсчёта оплаты
        self.last_action_time: Optional[datetime] = None  # Время последнего действия (для отслеживания AFK)
        
    def get_current_player(self) -> User:
        """Возвращает текущего игрока"""
        if self.current_player == "challenger":
            return self.challenger
        else:
            return self.target_user
            
    def get_current_roll_number(self) -> int:
        """Возвращает номер текущего броска"""
        return self.current_roll
        
    def make_roll(self) -> bool:
        """Выполняет бросок кубика для текущего игрока
        
        Используется значение верхней грани кубика (от 1 до 6) для подсчёта очков.
        """
        if self.status != "playing":
            return False
            
        if self.current_roll > self.dice_count:
            return False
            
        # Генерируем значение верхней грани кубика от 1 до 6
        # Это соответствует значению, которое видно на верхней грани кубика
        roll_value = random.randint(1, 6)
        
        # Добавляем результат в соответствующий список
        # Значение верхней грани добавляется к счёту игрока
        if self.current_player == "challenger":
            self.challenger_rolls.append(roll_value)
            self.challenger_score += roll_value
        else:
            self.target_rolls.append(roll_value)
            self.target_score += roll_value
            
        # Переключаемся на следующего игрока или следующий бросок
        if self.current_player == "challenger":
            self.current_player = "target"
        else:
            self.current_player = "challenger"
            self.current_roll += 1
            
        return True
        
    def get_dice_value_from_emoji(self, dice_emoji: str) -> int:
        """Определяет значение кубика по эмодзи (для будущего использования с реальными эмодзи)"""
        # В Telegram эмодзи кубика всегда показывает случайное значение
        # Для локального тестирования возвращаем случайное значение
        return random.randint(1, 6)
        
    def is_game_finished(self) -> bool:
        """Проверяет, завершена ли игра"""
        return (
            len(self.challenger_rolls) == self.dice_count
            and len(self.target_rolls) == self.dice_count
        )
        
    def get_winner(self) -> Optional[User]:
        """Возвращает победителя игры"""
        if not self.is_game_finished():
            return None
            
        if self.challenger_score > self.target_score:
            return self.challenger
        elif self.target_score > self.challenger_score:
            return self.target_user
        else:
            return None  # Ничья
    
    def reset_for_rematch(self):
        """Сбрасывает игру для переигровки при ничье"""
        self.challenger_rolls = []
        self.target_rolls = []
        self.challenger_score = 0
        self.target_score = 0
        self.current_player = "challenger"
        self.current_roll = 1
        self.pending_dice_message_id = None
        self.is_rematch = True
        self.status = "playing"
            
    def calculate_payout(self, commission_rate: float) -> float:
        """Рассчитывает сумму выплаты победителю с учетом комиссии"""
        total_pot = self.bet_amount * 2  # Оба игрока внесли ставку
        commission = total_pot * commission_rate
        payout = total_pot - commission
        return round(payout, 2)
        
    def get_game_summary(self) -> Dict:
        """Возвращает сводку по игре"""
        return {
            "game_id": self.game_id,
            "challenger": self.challenger.username,
            "target": self.target_username,
            "bet_amount": self.bet_amount,
            "challenger_score": self.challenger_score,
            "target_score": self.target_score,
            "challenger_rolls": self.challenger_rolls,
            "target_rolls": self.target_rolls,
            "status": self.status,
            "current_player": self.current_player,
            "current_roll": self.current_roll,
            "is_finished": self.is_game_finished(),
            "winner": self.get_winner().username if self.get_winner() else None
        }
        
    def get_scoreboard_text(self) -> str:
        """Возвращает текст табло"""
        challenger_rolls_text = f"({len(self.challenger_rolls)}/{self.dice_count})"
        target_rolls_text = f"({len(self.target_rolls)}/{self.dice_count})"
        
        return (
            f"📊 <b>Табло</b>\n\n"
            f"@{self.challenger.username} — {self.challenger_score} {challenger_rolls_text}\n"
            f"@{self.target_username} — {self.target_score} {target_rolls_text}"
        )
        
    def get_current_turn_text(self) -> str:
        """Возвращает текст текущего хода"""
        current_player_username = self.challenger.username if self.current_player == "challenger" else self.target_username
        return f"Ход @{current_player_username} — бросок {self.current_roll}/{self.dice_count}"
        
    def get_dice_emoji(self, value: int) -> str:
        """Возвращает эмодзи кубика для заданного значения"""
        # Используем стандартный эмодзи кубика из Telegram
        return "🎲"
        
    def get_rolls_display(self) -> str:
        """Возвращает отображение всех бросков"""
        display = "🎲 <b>Результаты бросков:</b>\n\n"
        
        # Броски первого игрока
        challenger_rolls_display = " ".join([self.get_dice_emoji(roll) for roll in self.challenger_rolls])
        display += f"@{self.challenger.username}: {challenger_rolls_display}\n"
        
        # Броски второго игрока
        target_rolls_display = " ".join([self.get_dice_emoji(roll) for roll in self.target_rolls])
        display += f"@{self.target_username}: {target_rolls_display}\n"
        
        return display
