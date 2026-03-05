import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple, Dict
from telegram import User


SUITS = ["♠", "♥", "♦", "♣"]
RANKS = [
    ("A", 11),
    ("K", 4),
    ("Q", 3),
    ("J", 2),
    ("10", 10),
    ("9", 9),
    ("8", 8),
    ("7", 7),
    ("6", 6),
    ("5", 5),
    ("4", 4),
    ("3", 3),
    ("2", 2),
]


def build_deck() -> List[Tuple[str, str, int]]:
    deck = []
    for suit in SUITS:
        for rank, value in RANKS:
            deck.append((rank, suit, value))
    random.shuffle(deck)
    return deck


@dataclass
class BlackjackPlayer:
    user: User
    cards: List[Tuple[str, str, Optional[int]]] = field(default_factory=list)  # Для туза value может быть None до выбора
    standing: bool = False

    def add_card(self, card: Tuple[str, str, int], chosen_value: Optional[int] = None):
        """Добавляет карту. Для туза chosen_value должен быть 1 или 11. Если None, туз сохраняется с None до выбора."""
        rank, suit, default_value = card
        if rank == "A":
            # Для туза сохраняем None до выбора значения, или выбранное значение
            self.cards.append((rank, suit, chosen_value))
        else:
            self.cards.append(card)

    def set_ace_value(self, card_index: int, value: int):
        """Устанавливает значение туза (1 или 11)"""
        if card_index < len(self.cards):
            rank, suit, _ = self.cards[card_index]
            if rank == "A":
                self.cards[card_index] = (rank, suit, value)

    def has_unset_ace(self) -> bool:
        """Проверяет, есть ли туз без выбранного значения"""
        return any(card[0] == "A" and card[2] is None for card in self.cards)

    def get_last_ace_index(self) -> Optional[int]:
        """Возвращает индекс последнего туза без выбранного значения"""
        for i in range(len(self.cards) - 1, -1, -1):
            if self.cards[i][0] == "A" and self.cards[i][2] is None:
                return i
        return None

    @property
    def score(self) -> int:
        # Считаем очки, тузы с None считаются как 0 (временно, до выбора значения)
        total = sum(card[2] if card[2] is not None else 0 for card in self.cards)
        return total

    def is_bust(self) -> bool:
        return self.score > 21


class BlackjackGame:
    def __init__(self, game_id: str, challenger: User, target_username: str, bet_amount: float, chat_id: int):
        self.game_id = game_id
        self.challenger = challenger
        self.target_username = target_username
        self.target_user: Optional[User] = None
        self.bet_amount = bet_amount
        self.chat_id = chat_id
        self.status = "waiting"

        self.challenger_payment_id: Optional[str] = None
        self.target_payment_id: Optional[str] = None
        self.challenger_paid = False
        self.target_paid = False

        self.created_at = datetime.now()
        self.payment_start_time: Optional[datetime] = None
        self.message_id: Optional[int] = None

        self.deck: List[Tuple[str, str, int]] = build_deck()
        self.players: Dict[str, BlackjackPlayer] = {
            "challenger": BlackjackPlayer(challenger),
            "target": None,  # заполнится позднее
        }
        self.turn_order: List[str] = ["challenger", "target"]
        random.shuffle(self.turn_order)
        self.current_turn_index = 0
        self.card_in_progress = False
        self.summary_message_id: Optional[int] = None

    def set_target_user(self, user: User):
        self.target_user = user
        self.players["target"] = BlackjackPlayer(user)

    def draw_card(self) -> Tuple[str, str, int]:
        if not self.deck:
            self.deck = build_deck()
        return self.deck.pop()

    def get_current_player_key(self) -> str:
        return self.turn_order[self.current_turn_index]

    def get_player_state(self, user_id: int) -> Optional[BlackjackPlayer]:
        for state in self.players.values():
            if state and state.user.id == user_id:
                return state
        return None

    def switch_turn(self):
        self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)

    def both_players_ready(self) -> bool:
        return self.players["target"] is not None

    def all_standing(self) -> bool:
        return all(player and (player.standing or player.is_bust()) for player in self.players.values())

    def get_scores(self) -> Dict[int, int]:
        return {player.user.id: player.score for player in self.players.values() if player}

    def get_winner(self) -> Optional[BlackjackPlayer]:
        challenger = self.players["challenger"]
        target = self.players["target"]
        if not challenger or not target:
            return None

        challenger_score = challenger.score if challenger.score <= 21 else 0
        target_score = target.score if target.score <= 21 else 0

        if challenger_score == target_score:
            return None
        if challenger_score > target_score:
            return challenger if challenger_score > 0 else None
        else:
            return target if target_score > 0 else None

    def calculate_payout(self, commission_rate: float) -> float:
        total_pot = self.bet_amount * 2
        commission = total_pot * commission_rate
        payout = total_pot - commission
        return round(payout, 2)

    def summary_text(self) -> str:
        challenger = self.players["challenger"]
        target = self.players["target"]
        if not target:
            return ""
        return (
            f"🃏 <b>Blackjack</b>\n\n"
            f"@{challenger.user.username}: {challenger.score} очков ({', '.join([f'{c[0]}{c[1]}' for c in challenger.cards]) or 'пусто'})\n"
            f"@{target.user.username}: {target.score} очков ({', '.join([f'{c[0]}{c[1]}' for c in target.cards]) or 'пусто'})"
        )

    def reset_for_rematch(self):
        """Сбрасывает игру для переигровки (используется при ничье)"""
        self.deck = build_deck()
        for player in self.players.values():
            if player:
                player.cards = []
                player.standing = False
        self.turn_order = ["challenger", "target"]
        random.shuffle(self.turn_order)
        self.current_turn_index = 0
        self.card_in_progress = False

