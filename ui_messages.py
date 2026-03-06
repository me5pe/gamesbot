import os
from typing import Optional
from telegram import User
from game_logic import DiceGame
from multi_game_logic import MultiDiceGame

class MessageFormatter:
    # ============================================================
    # PREMIUM EMOJI CONFIG
    # Меняйте emoji-id в этом блоке, чтобы быстро обновлять эмодзи
    # в конкретных сообщениях/кнопках.
    # Пустое значение = будет использован обычный fallback-эмодзи.
    # ============================================================
    PREMIUM_TEXT_EMOJI = {
        # [DUEL] Заголовок сообщения вызова на дуэль (format_challenge_message)
        "duel_invite": os.getenv("PE_DUEL_INVITE", "5280816565657300091"),
        # [BLACKJACK] Заголовок приглашения в blackjack (blackjack_command в bot.py)
        "blackjack_invite": os.getenv("PE_BLACKJACK_INVITE", "6028206863038811654"),
        # [RPS] Заголовок приглашения в КНБ (knb_command в bot.py)
        "knb_invite": os.getenv("PE_KNB_INVITE", "5269640498112378277"),
        # [MULTI] Заголовок создания/поиска игроков в мульти-игре (multiduel_command в bot.py)
        "multi_invite": os.getenv("PE_MULTI_INVITE", "5280816565657300091"),
    }

    PREMIUM_BUTTON_EMOJI = {
        # [BUTTON] Кнопка "Принять" (все игры)
        "accept": os.getenv("PE_BTN_ACCEPT", "5273806972871787310"),
        # [BUTTON] Кнопка "Отклонить"/"Отменить" (все игры)
        "decline": os.getenv("PE_BTN_DECLINE", "5271934564699226262"),
        # [BUTTON] Кнопка "Принять предложение" (мульти-игра)
        "multi_join": os.getenv("PE_BTN_MULTI_JOIN", "5273806972871787310"),
    }

    def __init__(self):
        self.bot_name = "Illidan Games"

    def get_premium_text_emoji(self, key: str, fallback: str) -> str:
        """Возвращает premium-эмодзи для текста (HTML) или fallback."""
        emoji_id = (self.PREMIUM_TEXT_EMOJI.get(key) or "").strip()
        if not emoji_id:
            return fallback
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

    def build_button_api_kwargs(self, style: str, emoji_key: Optional[str] = None) -> dict:
        """
        Формирует api_kwargs для InlineKeyboardButton:
        - style: primary/success/danger
        - icon_custom_emoji_id: берется из PREMIUM_BUTTON_EMOJI
        """
        kwargs = {"style": style}
        if emoji_key:
            emoji_id = (self.PREMIUM_BUTTON_EMOJI.get(emoji_key) or "").strip()
            if emoji_id:
                kwargs["icon_custom_emoji_id"] = emoji_id
        return kwargs
        
    def format_challenge_message(self, challenger_username: str, target_username: str, bet_amount: float, dice_count: int = 3) -> str:
        """Форматирует сообщение с вызовом на дуэль"""
        dice_text = "3 кубика" if dice_count == 3 else f"{dice_count} кубик" if dice_count == 1 else f"{dice_count} кубика"
        invite_emoji = self.get_premium_text_emoji("duel_invite", "🎲")
        return (
            f"{invite_emoji} <b>Кубик</b>\n\n"
            f"@{challenger_username} вызывает @{target_username} на {bet_amount} USDT.\n"
            f"🎲 Количество кубиков: <b>{dice_text}</b>\n\n"
            f"Принять?"
        )
        
    def format_payment_request(self, game_id: str, bet_amount: float, payment_link: str) -> str:
        """Форматирует сообщение с запросом оплаты"""
        return (
            f"📋 <b>Матч {game_id}</b>\n"
            f"Ставка {bet_amount} USDT.\n"
            f"Списано: 0.00.\n"
            f"К оплате: {bet_amount:.2f}.\n"
            f"Оплатите участие в течение 5 мин."
        )
        
    def format_payment_confirmation(self, username: str) -> str:
        """Форматирует сообщение о подтверждении оплаты"""
        return f"ℹ️ @{username} оплатил."
        
    def format_game_start(self, game) -> str:
        """Форматирует сообщение о начале игры"""
        # Проверяем, является ли это мультиигрой (MultiDiceGame), а не обычной дуэлью DiceGame
        # Используем isinstance, чтобы не путать с обычной игрой, где теперь тоже есть поле dice_count
        if isinstance(game, MultiDiceGame):
            # Это MultiDiceGame
            return (
                f"🎲 <b>Мульти-куб началась!</b>\n\n"
                f"Участников: {len(game.players)}\n"
                f"Ставка: {game.bet_amount} USDT\n"
                f"Кубиков: {game.dice_count}\n\n"
                f"Каждый игрок делает по {game.dice_count} броска. Победитель - тот, у кого больше очков!"
            )
        else:
            # Это обычный DiceGame
            return (
                f"🎲 <b>Игра началась!</b>\n\n"
                f"@{game.challenger.username} vs @{game.target_username}\n"
                f"Ставка: {game.bet_amount} USDT\n\n"
                f"Каждый игрок делает по {getattr(game, 'dice_count', 3)} броска. Победитель - тот, у кого больше очков!"
            )
        
    def format_scoreboard(self, game: DiceGame) -> str:
        """Форматирует табло игры"""
        max_rolls = getattr(game, 'dice_count', 3)
        challenger_rolls_text = f"({len(game.challenger_rolls)}/{max_rolls})"
        target_rolls_text = f"({len(game.target_rolls)}/{max_rolls})"
        
        # Формируем строки с бросками
        challenger_rolls_display = " + ".join(str(r) for r in game.challenger_rolls) if game.challenger_rolls else "—"
        target_rolls_display = " + ".join(str(r) for r in game.target_rolls) if game.target_rolls else "—"
        
        scoreboard = (
            f"📊 <b>Табло</b>\n\n"
            f"@{game.challenger.username}: {challenger_rolls_display} = <b>{game.challenger_score}</b> {challenger_rolls_text}\n"
            f"@{game.target_username}: {target_rolls_display} = <b>{game.target_score}</b> {target_rolls_text}"
        )
        
        # Добавляем информацию о текущем ходе
        if not game.is_game_finished():
            current_player_username = game.challenger.username if game.current_player == "challenger" else game.target_username
            scoreboard += f"\n\n🎯 Ход @{current_player_username}"
            
        return scoreboard
        
    def format_multi_scoreboard(self, game: MultiDiceGame) -> str:
        """Форматирует табло для мультидуэли"""
        active_players = game.rematch_players if game.is_rematch and game.rematch_players else game.players
        lines = []
        for player in active_players:
            rolls = game.players_rolls.get(player.id, [])
            rolls_display = " + ".join(str(r) for r in rolls) if rolls else "—"
            score = game.players_scores.get(player.id, 0)
            lines.append(
                f"@{player.username}: {rolls_display} = <b>{score}</b> ({len(rolls)}/{game.dice_count})"
            )
        
        scoreboard = "📊 <b>Табло мульти-куба</b>\n\n" + "\n".join(lines)
        
        if not game.is_game_finished():
            current_player = game.get_current_player()
            scoreboard += f"\n\n🎯 Ход @{current_player.username}"
        
        return scoreboard
        
    def format_game_result(self, game: DiceGame, winner: Optional[User], payout_amount: float, check_link: str = None) -> str:
        """Форматирует результаты игры"""
        # Формируем строки с бросками
        challenger_rolls_display = " + ".join(str(r) for r in game.challenger_rolls)
        target_rolls_display = " + ".join(str(r) for r in game.target_rolls)
        
        result_text = (
            f"🏁 <b>Итоги дуэли</b>\n\n"
            f"@{game.challenger.username}: {challenger_rolls_display} = <b>{game.challenger_score}</b>\n"
            f"@{game.target_username}: {target_rolls_display} = <b>{game.target_score}</b>\n\n"
        )
        
        if winner:
            result_text += f"🏆 Победитель: @{winner.username}\n\n"
            result_text += f"💰 К выплате: {payout_amount} USDT\n"
            if check_link:
                result_text += f"🔗 Чек для победителя: {check_link}"
        else:
            result_text += "🤝 Ничья! Ставки возвращаются игрокам."
            
        return result_text
        
    def format_cancel_message(self, challenger_username: str, target_username: str) -> str:
        """Форматирует сообщение об отмене игры"""
        return (
            f"❌ <b>Игра отменена администратором</b>\n\n"
            f"Дуэль между @{challenger_username} и @{target_username} была отменена."
        )
        
    def format_decline_message(self, challenger_username: str, target_username: str, decliner_username: str = None) -> str:
        """Форматирует сообщение об отклонении игры"""
        if decliner_username:
            return (
                f"❌ <b>Игра отклонена</b>\n\n"
                f"@{decliner_username} отклонил дуэль между @{challenger_username} и @{target_username}."
            )
        else:
            return (
                f"❌ <b>Игра отклонена</b>\n\n"
                f"@{target_username} отклонил вызов от @{challenger_username}."
            )
        
    def format_roll_result(self, game: DiceGame, roll_value: int) -> str:
        """Форматирует результат броска"""
        dice_emoji = self.get_dice_emoji(roll_value)
        current_player_username = game.challenger.username if game.current_player == "challenger" else game.target_username
        
        return (
            f"🎲 {dice_emoji}\n\n"
            f"@{current_player_username} выбросил {roll_value}!"
        )
        
    def get_dice_emoji(self, value: int) -> str:
        """Возвращает эмодзи кубика для заданного значения"""
        dice_emojis = {
            1: "⚀",
            2: "⚁", 
            3: "⚂",
            4: "⚃",
            5: "⚄",
            6: "⚅"
        }
        return dice_emojis.get(value, "🎲")
        
    def format_commission_info(self, total_amount: float, commission_rate: float) -> str:
        """Форматирует информацию о комиссии"""
        commission_amount = total_amount * commission_rate
        payout_amount = total_amount - commission_amount
        
        return (
            f"💳 <b>Информация о выплате</b>\n\n"
            f"Общая сумма: {total_amount} USDT\n"
            f"Комиссия ({commission_rate*100}%): {commission_amount:.2f} USDT\n"
            f"К выплате: {payout_amount:.2f} USDT"
        )
        
    def format_error_message(self, error_type: str) -> str:
        """Форматирует сообщения об ошибках"""
        error_messages = {
            "invalid_command": "❌ Неверная команда! Используйте /duel <сумма> @username",
            "invalid_amount": "❌ Неверная сумма ставки!",
            "insufficient_funds": "❌ Недостаточно средств для игры!",
            "game_not_found": "❌ Игра не найдена!",
            "not_your_turn": "❌ Не ваш ход!",
            "payment_failed": "❌ Ошибка при обработке платежа!",
            "payout_failed": "❌ Ошибка при выплате!",
            "permission_denied": "❌ У вас нет прав для выполнения этой команды!"
        }
        
        return error_messages.get(error_type, "❌ Произошла неизвестная ошибка!")
        
    def format_help_message(self) -> str:
        """Форматирует справочное сообщение"""
        return (
            f"❓ <b>Помощь — Illidan Games</b>\n\n"
            f"🎮 <b>Основные команды</b>\n"
            f"/start - Главное меню\n"
            f"/duel &lt;сумма&gt; @username - Вызвать игрока на дуэль\n"
            f"/duel &lt;сумма&gt; (ответ на сообщение) - Вызвать игрока\n"
            f"/blackjack &lt;сумма&gt; @username - Дуэль в Blackjack (21)\n"
            f"/knb &lt;сумма&gt; @username - Камень-Ножницы-Бумага\n"
            f"/multiduel &lt;сумма&gt; &lt;игроки&gt; &lt;кубики&gt; - Создать мультиигру (3-5 игроков)\n"
            f"/help - Показать это сообщение\n\n"
            f"📋 <b>Правила игр</b>\n\n"
            f"🎲 <b>Кубики:</b>\n"
            f"• 3 броска на игрока\n"
            f"• Побеждает большая сумма\n"
            f"• При ничьей — переигровка\n\n"
            f"🃏 <b>Blackjack:</b>\n"
            f"• Цель: набрать 21 очко\n"
            f"• Ближе к 21 — победа\n"
            f"• Перебор — проигрыш\n\n"
            f"🗿📄✂️ <b>КНБ:</b>\n"
            f"• Игра до 3 побед\n"
            f"• При ничьей — переигровка раунда\n\n"
            f"🎲 <b>Мульти-дуэли:</b>\n"
            f"• 3-5 игроков\n"
            f"• Настраиваемое количество кубиков\n\n"
            f"💰 <b>Финансы</b>\n"
            f"💵 Валюта: <b>USDT</b>\n"
            f"📈 Комиссия: <b>8%</b>\n"
            f"🔒 Эскроу-система\n\n"
            f"📝 <b>Примеры использования:</b>\n"
            f"• <code>/duel 1 @username</code> - Вызов на дуэль\n"
            f"• <code>/duel 1</code> (ответ на сообщение) - Вызов по ответу\n"
            f"• <code>/knb 2 @username</code> - КНБ на 2 USDT\n"
            f"• <code>/multiduel 2 4 3</code> - Мультиигра: 2 USDT, 4 игрока, 3 кубика\n\n"
            f"💡 <i>Все игры проходят через эскроу-систему для вашей безопасности!</i>"
        )
    
    def format_info_message(self) -> str:
        """Форматирует информационное сообщение"""
        return (
            f"📋 <b>О боте — Illidan Games</b>\n\n"
            f"🎯 <b>О проекте</b>\n"
            f"Illidan Games — это платформа для честных игровых дуэлей на базе Telegram и CryptoBot.\n\n"
            f"Мы предоставляем безопасную и прозрачную систему для игр между пользователями.\n\n"
            f"🔒 <b>Безопасность</b>\n"
            f"✅ Эскроу-система\n"
            f"✅ Автоматические выплаты\n"
            f"✅ Прозрачная комиссия\n"
            f"✅ Защита от мошенничества\n\n"
            f"⚖️ <b>Важно</b>\n"
            f"Бот <b>не является</b> казино-проектом. Это инструмент для честных игр между пользователями.\n\n"
            f"📞 <b>Сотрудничество:</b>\n"
            f"По вопросам сотрудничества обращайтесь в Техническую Поддержку.\n\n"
            f"💬 <b>Поддержка:</b>\n"
            f"Все вопросы и предложения приветствуются!"
        )
