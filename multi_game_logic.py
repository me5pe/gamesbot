import random
from datetime import datetime
from typing import Optional, Dict, List
from telegram import User
from ui_premium_emojis import premium_emoji, premiumize_text

class MultiDiceGame:
    """Класс для мультидуэли (3-5 игроков)"""
    
    def __init__(self, game_id: str, creator: User, bet_amount: float, max_players: int, dice_count: int, chat_id: int):
        self.game_id = game_id
        self.creator = creator
        self.bet_amount = bet_amount
        self.max_players = max_players  # 3-5 игроков
        self.dice_count = dice_count    # 1-3 кубика
        self.chat_id = chat_id
        self.status = "waiting"  # waiting, payment_pending, playing, finished, cancelled
        
        # Участники игры
        self.players: List[User] = [creator]  # Создатель автоматически в игре
        self.invited_players: List[User] = []  # Приглашенные игроки
        self.players_paid: Dict[int, bool] = {creator.id: False}
        self.players_payment_ids: Dict[int, str] = {}
        
        # Игровые данные
        self.players_rolls: Dict[int, List[int]] = {creator.id: []}
        self.players_scores: Dict[int, int] = {creator.id: 0}
        self.current_player_index = 0
        self.current_round = 1  # Раунд игры (1, 2, 3 если dice_count=3)
        
        # Данные переигровки
        self.is_rematch = False
        self.rematch_players: List[User] = []  # Игроки, участвующие в переигровке
        
        self.created_at = datetime.now()
        self.payment_start_time: Optional[datetime] = None  # Время начала отсчёта оплаты
        self.message_id: Optional[int] = None  # ID сообщения игры для обновления
        self.message_has_photo: bool = False  # Флаг, что сообщение было с фото
        self.last_action_time: Optional[datetime] = None  # Время последнего действия (для отслеживания AFK)
        
    def add_player(self, user: User) -> bool:
        """Добавляет игрока в игру"""
        if len(self.players) >= self.max_players:
            return False
            
        if user.id in [p.id for p in self.players]:
            return False  # Уже в игре
            
        self.players.append(user)
        self.players_paid[user.id] = False
        self.players_rolls[user.id] = []
        self.players_scores[user.id] = 0
        
        # Удаляем из списка приглашенных, если был там
        self.invited_players = [p for p in self.invited_players if p.id != user.id]
        
        return True
    
    def remove_player(self, user_id: int) -> bool:
        """Удаляет игрока из игры (только если игра еще не началась)"""
        if self.status != "waiting":
            return False  # Нельзя удалять после начала игры
            
        # Нельзя удалить создателя
        if user_id == self.creator.id:
            return False
            
        # Ищем и удаляем игрока
        player_to_remove = None
        for player in self.players:
            if player.id == user_id:
                player_to_remove = player
                break
                
        if not player_to_remove:
            return False  # Игрок не найден
            
        # Удаляем игрока
        self.players.remove(player_to_remove)
        del self.players_paid[user_id]
        if user_id in self.players_payment_ids:
            del self.players_payment_ids[user_id]
        del self.players_rolls[user_id]
        del self.players_scores[user_id]
        
        return True
    
    def invite_player(self, user: User) -> bool:
        """Приглашает игрока в игру"""
        # Проверяем, не в игре ли уже
        if user.id in [p.id for p in self.players]:
            return False  # Уже в игре
            
        # Проверяем, не приглашен ли уже
        if user.id in [p.id for p in self.invited_players]:
            return False  # Уже приглашен
            
        # Проверяем, есть ли место
        if len(self.players) >= self.max_players:
            return False
            
        self.invited_players.append(user)
        return True
    
    def is_invited(self, user_id: int) -> bool:
        """Проверяет, приглашен ли игрок"""
        return user_id in [p.id for p in self.invited_players]
        
    def is_full(self) -> bool:
        """Проверяет, набрано ли нужное количество игроков"""
        return len(self.players) == self.max_players
        
    def all_paid(self) -> bool:
        """Проверяет, все ли игроки оплатили"""
        return all(self.players_paid.values())
        
    def get_current_player(self) -> User:
        """Возвращает текущего игрока"""
        if self.is_rematch and self.rematch_players:
            return self.rematch_players[self.current_player_index]
        return self.players[self.current_player_index]
        
    def add_roll(self, user_id: int, dice_value: int):
        """Добавляет результат броска игроку"""
        if user_id not in self.players_rolls:
            return False
            
        self.players_rolls[user_id].append(dice_value)
        self.players_scores[user_id] += dice_value
        return True
        
    def next_player(self):
        """Переходит к следующему игроку"""
        active_players = self.rematch_players if self.is_rematch else self.players
        
        self.current_player_index += 1
        if self.current_player_index >= len(active_players):
            self.current_player_index = 0
            self.current_round += 1
            
    def is_game_finished(self) -> bool:
        """Проверяет, завершена ли игра"""
        active_players = self.rematch_players if self.is_rematch else self.players
        
        # Все игроки должны сделать все броски
        for player in active_players:
            if len(self.players_rolls[player.id]) < self.dice_count:
                return False
        return True
        
    def get_winners(self) -> List[User]:
        """Возвращает победителей (может быть несколько при ничье)"""
        if not self.is_game_finished():
            return []
            
        active_players = self.rematch_players if self.is_rematch else self.players
        
        # Находим максимальный счет
        max_score = max(self.players_scores[p.id] for p in active_players)
        
        # Находим всех игроков с максимальным счетом
        winners = [p for p in active_players if self.players_scores[p.id] == max_score]
        
        return winners
        
    def calculate_payout(self, commission_rate: float) -> float:
        """Рассчитывает сумму выплаты победителю"""
        total_pot = self.bet_amount * len(self.players)
        commission = total_pot * commission_rate
        payout = total_pot - commission
        return round(payout, 2)
        
    def reset_for_rematch(self, winners: List[User]):
        """Сбрасывает игру для переигровки между победителями"""
        self.is_rematch = True
        self.rematch_players = winners
        self.current_player_index = 0
        self.current_round = 1
        
        # Очищаем результаты только для участников переигровки
        for player in winners:
            self.players_rolls[player.id] = []
            self.players_scores[player.id] = 0
            
    def get_scoreboard_text(self) -> str:
        """Возвращает текст табло"""
        active_players = self.rematch_players if self.is_rematch else self.players
        
        scoreboard = "📊 <b>Табло</b>\n\n"
        
        for player in active_players:
            rolls_count = len(self.players_rolls[player.id])
            score = self.players_scores[player.id]
            
            # Формируем строку с бросками, как в обычной дуэли
            if self.players_rolls[player.id]:
                rolls_display = " + ".join(str(r) for r in self.players_rolls[player.id])
                scoreboard += f"@{player.username}: {rolls_display} = <b>{score}</b> ({rolls_count}/{self.dice_count})\n"
            else:
                scoreboard += f"@{player.username}: — = <b>{score}</b> ({rolls_count}/{self.dice_count})\n"
            
        return premiumize_text(scoreboard)
        
    def get_players_list_text(self) -> str:
        """Возвращает список принявших игроков и приглашенных"""
        participants_emoji = premium_emoji("multi_participants", "👥")
        waiting_emoji = premium_emoji("multi_waiting", "⚠️")
        text = f"{participants_emoji} <b>Участники ({len(self.players)}/{self.max_players})</b>\n\n"

        for i, player in enumerate(self.players, 1):
            creator_mark = " (создатель)" if player.id == self.creator.id else ""
            text += f"{i}. @{player.username}{creator_mark}\n"
        
        if self.invited_players:
            text += f"\n{waiting_emoji} <b>Ожидание приглашённых игроков:</b>\n"
            for player in self.invited_players:
                text += f"• @{player.username}\n"

        return premiumize_text(text)

