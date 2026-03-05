import asyncio
import json
import logging
import os
import pickle
import time
from pathlib import Path
from io import BytesIO
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode, DiceEmoji
from telegram.error import RetryAfter, TelegramError, TimedOut, BadRequest, Conflict
try:
    import httpx
    HAS_HTTPX = True
    HTTPX_ERRORS = (httpx.ReadError, httpx.WriteError, httpx.ConnectError, httpx.NetworkError)
except ImportError:
    HAS_HTTPX = False
    HTTPX_ERRORS = ()  # Пустой кортеж, если httpx не установлен
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any, Set
import random

from config import BOT_TOKEN, ADMIN_USER_IDS, COMMISSION_RATE, MIN_BET, MAX_BET, logger
from game_logic import DiceGame
from multi_game_logic import MultiDiceGame
from blackjack_game import BlackjackGame, BlackjackPlayer
from knb_game import RockPaperScissorsGame
from PIL import Image, ImageDraw, ImageFont
from escrow_system import EscrowManager
from ui_messages import MessageFormatter
from user_profile import ProfileManager
from check_manager import CheckManager
from db_storage import DatabaseManager


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Создает клавиатуру с учетом прав администратора"""
    buttons = [
        [KeyboardButton("🏠 Главное меню")],
        [KeyboardButton("👤 Профиль"), KeyboardButton("📊 Статистика")],
        [KeyboardButton("ℹ️ О боте"), KeyboardButton("❓ Помощь")]
    ]
    
    # Добавляем кнопку администратора, если пользователь - админ
    if is_admin(user_id):
        logger.info(f"Добавлена кнопка админа для пользователя {user_id} (ADMIN_USER_IDS: {ADMIN_USER_IDS})")
        buttons.append([KeyboardButton("⚙️ Админ панель")])
    else:
        logger.debug(f"Пользователь {user_id} не является админом (ADMIN_USER_IDS: {ADMIN_USER_IDS})")
    
    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True,
        one_time_keyboard=False
    )

class DiceBot:
    def __init__(self):
        self.active_games: Dict[str, DiceGame] = {}
        self.active_multi_games: Dict[str, MultiDiceGame] = {}
        self.escrow_manager = EscrowManager()
        self.message_formatter = MessageFormatter()
        self.profile_manager = ProfileManager()
        self.check_manager = CheckManager()
        self.registered_users: set[int] = set()  # Список пользователей для рассылки
        default_dir = Path(__file__).resolve().parent / "data"
        self._data_dir = Path(os.getenv("DATA_DIR", str(default_dir)))
        self._registered_users_path = self._data_dir / "registered_users.json"
        self._load_registered_users()
        self._dice_lock = asyncio.Lock()
        self._last_dice_time = 0.0
        self._dice_rate_limit = float(os.getenv("DICE_RATE_LIMIT", 1.3))
        self._callback_cooldowns: Dict[int, float] = {}
        self._callback_cooldown = float(os.getenv("CALLBACK_COOLDOWN", 1.8))
        self._dice_button_cooldowns: Dict[int, float] = {}
        # Дополнительный flood control для любых Telegram API вызовов (без изменения таймаутов)
        self._api_rate_lock = asyncio.Lock()
        self._last_api_call_time = 0.0
        self._last_chat_api_call_time: Dict[int, float] = {}
        self._global_api_rate_limit = float(os.getenv("GLOBAL_API_RATE_LIMIT", 0.12))
        self._chat_api_rate_limit = float(os.getenv("CHAT_API_RATE_LIMIT", 0.35))
        self._payment_state_lock = asyncio.Lock()
        self._state_snapshot_lock = asyncio.Lock()
        self.application: Optional[Application] = None
        self._webhook_runner: Optional[web.AppRunner] = None
        self._webhook_site: Optional[web.TCPSite] = None
        self._processed_crypto_updates: Set[int] = set()
        self.db = DatabaseManager()
        self._cryptobot_webhook_host = os.getenv("CRYPTOBOT_WEBHOOK_HOST", "0.0.0.0")
        self._cryptobot_webhook_port = int(os.getenv("CRYPTOBOT_WEBHOOK_PORT", os.getenv("PORT", "8000")))
        self._cryptobot_webhook_secret = os.getenv("CRYPTOBOT_WEBHOOK_SECRET", "").strip()
        self._public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if self._cryptobot_webhook_secret:
            self._cryptobot_webhook_path = f"/cryptobot/webhook/{self._cryptobot_webhook_secret}"
        else:
            self._cryptobot_webhook_path = "/cryptobot/webhook"
        self.active_blackjack_games: Dict[str, BlackjackGame] = {}
        self.active_knb_games: Dict[str, RockPaperScissorsGame] = {}
    
    def _ensure_data_dir(self):
        """Гарантирует наличие директории data"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_registered_users(self):
        """Загружает список пользователей для рассылки из JSON"""
        if not self._registered_users_path.exists():
            return
        try:
            with self._registered_users_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, list):
                self.registered_users = {int(user_id) for user_id in data}
            logger.info("Загружено %d зарегистрированных пользователей", len(self.registered_users))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            logger.error("Не удалось загрузить registered_users: %s", error)
    
    def _save_registered_users(self):
        """Сохраняет список пользователей для рассылки в JSON"""
        self._ensure_data_dir()
        data = list(self.registered_users)
        try:
            with self._registered_users_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
        except OSError as error:
            logger.error("Не удалось сохранить registered_users: %s", error)

    async def _cryptobot_health_handler(self, request: web.Request):
        return web.json_response({"ok": True, "service": "cryptobot-webhook"})

    async def _cryptobot_webhook_handler(self, request: web.Request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        await self._process_cryptobot_update(payload)
        return web.json_response({"ok": True})

    async def _process_cryptobot_update(self, update_data: Dict[str, Any]):
        update_id = update_data.get("update_id")
        if isinstance(update_id, int):
            try:
                is_new = await self.db.mark_update_processed(update_id)
            except Exception as e:
                logger.error("Ошибка durable idempotency для update_id=%s: %s", update_id, e)
                # Fallback in-memory, чтобы не потерять защиту от дублей при проблеме с БД.
                if update_id in self._processed_crypto_updates:
                    return
                self._processed_crypto_updates.add(update_id)
                if len(self._processed_crypto_updates) > 5000:
                    self._processed_crypto_updates = set(list(self._processed_crypto_updates)[-2500:])
            else:
                if not is_new:
                    return

        update_type = str(update_data.get("update_type", "")).lower()
        payload = update_data.get("payload") or {}
        if not isinstance(payload, dict):
            return

        invoice_id = payload.get("invoice_id")
        invoice_status = str(payload.get("status", "")).lower()

        # Принимаем как явное событие invoice_paid, так и обновление инвойса со статусом paid.
        if "invoice" not in update_type and invoice_status != "paid":
            return
        if invoice_status and invoice_status != "paid":
            return
        if invoice_id is None:
            return

        await self._apply_invoice_paid(invoice_id)

    async def _apply_invoice_paid(self, invoice_id: Any):
        """Применяет событие оплаты по invoice_id ко всем активным играм."""
        if not self.application:
            return

        invoice_id_str = str(invoice_id)
        context = self.application

        async with self._payment_state_lock:
            # Обычные дуэли
            for game in self.active_games.values():
                if game.status != "payment_pending":
                    continue
                changed = False
                if game.challenger_payment_id and str(game.challenger_payment_id) == invoice_id_str and not game.challenger_paid:
                    game.challenger_paid = True
                    changed = True
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        f"ℹ️ @{game.challenger.username} оплатил (webhook).",
                        parse_mode=ParseMode.HTML,
                    )
                if game.target_payment_id and str(game.target_payment_id) == invoice_id_str and not game.target_paid:
                    game.target_paid = True
                    changed = True
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        f"ℹ️ @{game.target_username} оплатил (webhook).",
                        parse_mode=ParseMode.HTML,
                    )
                if changed and game.challenger_paid and game.target_paid and game.status == "payment_pending":
                    await self._start_duel_game(context, game)
                if changed:
                    await self._persist_runtime_state()
                    return

            # Blackjack
            for game in self.active_blackjack_games.values():
                if game.status != "payment_pending":
                    continue
                changed = False
                if game.challenger_payment_id and str(game.challenger_payment_id) == invoice_id_str and not game.challenger_paid:
                    game.challenger_paid = True
                    changed = True
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        f"ℹ️ @{game.challenger.username} оплатил участие в Blackjack (webhook).",
                        parse_mode=ParseMode.HTML,
                    )
                if game.target_payment_id and str(game.target_payment_id) == invoice_id_str and not game.target_paid:
                    game.target_paid = True
                    changed = True
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        f"ℹ️ @{game.target_username} оплатил участие в Blackjack (webhook).",
                        parse_mode=ParseMode.HTML,
                    )
                if changed and game.challenger_paid and game.target_paid and game.status == "payment_pending":
                    await self.start_blackjack_game(context, game)
                if changed:
                    await self._persist_runtime_state()
                    return

            # КНБ
            for game in self.active_knb_games.values():
                if game.status != "payment_pending":
                    continue
                changed = False
                if game.challenger_payment_id and str(game.challenger_payment_id) == invoice_id_str and not game.challenger_paid:
                    game.challenger_paid = True
                    changed = True
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        f"ℹ️ @{game.challenger.username} оплатил участие в КНБ (webhook).",
                        parse_mode=ParseMode.HTML,
                    )
                if game.target_payment_id and str(game.target_payment_id) == invoice_id_str and not game.target_paid:
                    game.target_paid = True
                    changed = True
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        f"ℹ️ @{game.target_username} оплатил участие в КНБ (webhook).",
                        parse_mode=ParseMode.HTML,
                    )
                if changed and game.challenger_paid and game.target_paid and game.status == "payment_pending":
                    game.status = "playing"
                    game.initialize_players()
                    await self._bot_send_message_with_retry(
                        context.bot,
                        game.chat_id,
                        (
                            f"🗿📄✂️ <b>Игра начинается!</b>\n\n"
                            f"@{game.challenger.username} vs @{game.target_username}\n"
                            f"Ставка: {game.bet_amount} USDT\n\n"
                            f"Игра до 3 побед. Удачи!"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                    await self._send_knb_choice_menu(game, context)
                if changed:
                    await self._persist_runtime_state()
                    return

            # Мультиигры
            for game in self.active_multi_games.values():
                if game.status != "payment_pending":
                    continue
                for player in game.players:
                    player_invoice_id = game.players_payment_ids.get(player.id)
                    if player_invoice_id and str(player_invoice_id) == invoice_id_str and not game.players_paid.get(player.id, False):
                        game.players_paid[player.id] = True
                        await self._bot_send_message_with_retry(
                            context.bot,
                            game.chat_id,
                            f"ℹ️ @{player.username} оплатил (webhook).",
                            parse_mode=ParseMode.HTML,
                        )
                        if game.all_paid() and game.status == "payment_pending":
                            await self._start_multi_game(context, game)
                        await self._persist_runtime_state()
                        return

    async def _start_cryptobot_webhook_server(self):
        if self._webhook_runner:
            return

        app = web.Application()
        app.router.add_get("/health", self._cryptobot_health_handler)
        # Fallback: некоторые настройки в @CryptoBot указывают webhook на корень домена.
        app.router.add_post("/", self._cryptobot_webhook_handler)
        app.router.add_post(self._cryptobot_webhook_path, self._cryptobot_webhook_handler)

        self._webhook_runner = web.AppRunner(app)
        await self._webhook_runner.setup()
        self._webhook_site = web.TCPSite(
            self._webhook_runner,
            host=self._cryptobot_webhook_host,
            port=self._cryptobot_webhook_port,
        )
        await self._webhook_site.start()
        logger.info(
            "CryptoBot webhook server запущен: %s:%s%s",
            self._cryptobot_webhook_host,
            self._cryptobot_webhook_port,
            self._cryptobot_webhook_path,
        )

    async def _configure_cryptobot_webhook(self):
        if not self._public_base_url:
            logger.warning(
                "PUBLIC_BASE_URL не задан. "
                "Укажите webhook URL вручную в @CryptoBot -> My Apps -> Webhooks."
            )
            return

        webhook_url = f"{self._public_base_url}{self._cryptobot_webhook_path}"
        logger.info(
            "Crypto Pay API не поддерживает setWebhook через HTTP API. "
            "Настройте webhook вручную в @CryptoBot."
        )
        logger.info("Рекомендуемый Webhook URL: %s", webhook_url)
        logger.info("Дополнительный fallback URL: %s/", self._public_base_url)

    async def on_startup(self, application: Application):
        self.application = application
        await self.db.connect()
        await self._restore_runtime_state()
        await self._start_cryptobot_webhook_server()
        await self._configure_cryptobot_webhook()

    async def on_shutdown(self, application: Application):
        await self._persist_runtime_state()
        if self._webhook_runner:
            try:
                await self._webhook_runner.cleanup()
            finally:
                self._webhook_runner = None
                self._webhook_site = None
        await self.db.close()
        await self.escrow_manager.close_session()

    def _serialize_runtime_state(self) -> bytes:
        state = {
            "active_games": self.active_games,
            "active_multi_games": self.active_multi_games,
            "active_blackjack_games": self.active_blackjack_games,
            "active_knb_games": self.active_knb_games,
        }
        return pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)

    async def _persist_runtime_state(self):
        async with self._state_snapshot_lock:
            payload = self._serialize_runtime_state()
            await self.db.save_runtime_snapshot("active_games", payload)

    async def _restore_runtime_state(self):
        try:
            payload = await self.db.load_runtime_snapshot("active_games")
            if not payload:
                return
            state = pickle.loads(payload)
            if not isinstance(state, dict):
                return
            self.active_games = state.get("active_games", {}) or {}
            self.active_multi_games = state.get("active_multi_games", {}) or {}
            self.active_blackjack_games = state.get("active_blackjack_games", {}) or {}
            self.active_knb_games = state.get("active_knb_games", {}) or {}
            logger.info(
                "Восстановлено из PostgreSQL: duel=%s, multi=%s, blackjack=%s, knb=%s",
                len(self.active_games),
                len(self.active_multi_games),
                len(self.active_blackjack_games),
                len(self.active_knb_games),
            )
        except Exception as e:
            logger.error("Ошибка восстановления runtime state из PostgreSQL: %s", e)

    async def persist_runtime_state_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Периодический snapshot активных игр в PostgreSQL."""
        try:
            await self._persist_runtime_state()
        except Exception as e:
            logger.error("Ошибка snapshot активных игр: %s", e)

    async def _upsert_state_event(self, reason: str):
        """Моментальный upsert состояния после изменения runtime state."""
        try:
            await self._persist_runtime_state()
        except Exception as e:
            logger.error("Ошибка event upsert (%s): %s", reason, e)

    async def _ensure_payout_check(
        self,
        game_id: str,
        game_type: str,
        winner_id: int,
        amount: float,
        check_game_ref: str,
    ) -> Optional[str]:
        """Транзакционный payout lifecycle: pending -> check_created."""
        payout = await self.db.get_or_create_payout(game_id, game_type, winner_id, amount)
        status = str(payout.get("status", "pending"))

        if status in {"check_created", "notified", "completed"} and payout.get("check_link"):
            return payout.get("check_link")

        if status == "pending":
            check_data = await self.escrow_manager.create_check_for_user(
                winner_id,
                amount,
                check_game_ref,
            )
            if not check_data:
                return None

            self.check_manager.add_check(check_data)
            await self.db.mark_payout_check_created(
                game_id,
                check_data.get("check_id"),
                check_data.get("check_link"),
            )
            return check_data.get("check_link")

        return payout.get("check_link")

    async def _mark_payout_notified_completed(self, game_id: str):
        """Lifecycle continuation: check_created -> notified -> completed."""
        try:
            await self.db.set_payout_status(game_id, "notified")
            await self.db.set_payout_status(game_id, "completed")
        except Exception as e:
            logger.error("Ошибка обновления статуса payout %s: %s", game_id, e)

    async def _throttle_api_call(self, chat_id: Optional[int] = None):
        """Ограничивает частоту Telegram API вызовов глобально и по чату."""
        async with self._api_rate_lock:
            now = time.monotonic()

            global_wait = self._global_api_rate_limit - (now - self._last_api_call_time)
            if global_wait > 0:
                await asyncio.sleep(global_wait)
                now = time.monotonic()

            if chat_id is not None:
                last_chat_call = self._last_chat_api_call_time.get(chat_id, 0.0)
                chat_wait = self._chat_api_rate_limit - (now - last_chat_call)
                if chat_wait > 0:
                    await asyncio.sleep(chat_wait)
                    now = time.monotonic()
                self._last_chat_api_call_time[chat_id] = now

            self._last_api_call_time = now

    async def _send_message_with_retry(
        self,
        chat,
        *args,
        max_retries: int = 3,
        **kwargs,
    ):
        """Универсальная отправка сообщений с учетом flood control и таймаутов."""
        attempt = 0
        chat_id = getattr(chat, "id", None)
        while attempt < max_retries:
            try:
                await self._throttle_api_call(chat_id=chat_id)
                return await chat.send_message(*args, **kwargs)
            except RetryAfter as exc:
                attempt += 1
                wait_time = exc.retry_after + 0.5
                logger.warning(
                    "Flood control при send_message (попытка %s/%s). Ждем %.1f c.",
                    attempt,
                    max_retries,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
            except TimedOut as exc:
                attempt += 1
                logger.warning(
                    "Таймаут при send_message (попытка %s/%s): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                await asyncio.sleep(2.0 * attempt)
            except TelegramError as exc:
                logger.error("Ошибка Telegram API при send_message: %s", exc)
                break

        logger.error("Не удалось отправить сообщение после %s попыток", max_retries)
        return None

    async def _bot_send_message_with_retry(
        self,
        bot,
        chat_id: int,
        text: str,
        max_retries: int = 3,
        **kwargs,
    ):
        """Отправка сообщения через bot.send_message с retry и throttling."""
        attempt = 0
        while attempt < max_retries:
            try:
                await self._throttle_api_call(chat_id=chat_id)
                return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            except RetryAfter as exc:
                attempt += 1
                wait_time = exc.retry_after + 0.5
                logger.warning(
                    "Flood control при bot.send_message (попытка %s/%s). Ждем %.1f c.",
                    attempt,
                    max_retries,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
            except TimedOut as exc:
                attempt += 1
                logger.warning(
                    "Таймаут при bot.send_message (попытка %s/%s): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                await asyncio.sleep(2.0 * attempt)
            except TelegramError as exc:
                logger.error("Ошибка Telegram API при bot.send_message: %s", exc)
                break

        logger.error("Не удалось отправить сообщение через bot.send_message после %s попыток", max_retries)
        return None

    async def send_dice_with_retry(self, chat, max_retries: int = 3):
        """Отправляет кубик с обработкой flood control и таймаутов"""
        retry_count = 0

        async with self._dice_lock:
            while retry_count < max_retries:
                # Пауза для соблюдения общего лимита запросов
                now = time.monotonic()
                wait_for = self._dice_rate_limit - (now - self._last_dice_time)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                self._last_dice_time = time.monotonic()

                try:
                    dice_message = await chat.send_dice(emoji=DiceEmoji.DICE)
                    return dice_message  # Успешно отправлено
                except RetryAfter as e:
                    retry_after = e.retry_after
                    logger.warning(
                        "Flood control: ожидание %s секунд перед повторной попыткой",
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    retry_count += 1
                except TimedOut as e:
                    logger.warning(
                        "Таймаут при отправке кубика (попытка %s/%s): %s",
                        retry_count + 1,
                        max_retries,
                        e,
                    )
                    retry_count += 1
                    if retry_count < max_retries:
                        # Увеличиваем задержку при таймауте
                        await asyncio.sleep(2.0 * retry_count)
                    else:
                        logger.error(
                            "Не удалось отправить кубик из-за таймаутов после всех попыток",
                        )
                except TelegramError as e:
                    logger.error(f"Ошибка Telegram API: {e}")
                    retry_count += 1
                    if retry_count < max_retries:
                        await asyncio.sleep(1.0)
                    else:
                        # Только в последней попытке отправляем сообщение об ошибке
                        await self._send_message_with_retry(
                            chat,
                            "❌ Ошибка при отправке кубика. Попробуйте позже.",
                            parse_mode=ParseMode.HTML,
                        )
                        return None

        logger.error("Не удалось отправить кубик после всех попыток")
        await self._send_message_with_retry(
            chat,
            "❌ Не удалось бросить кубик. Попробуйте позже.",
            parse_mode=ParseMode.HTML,
        )
        return None
    
    def _is_dice_press_too_fast(self, user_id: int) -> bool:
        """Возвращает True, если пользователь слишком часто жмет кнопку броска"""
        now = time.monotonic()
        last_press = self._dice_button_cooldowns.get(user_id, 0.0)
        if now - last_press < self._callback_cooldown:
            return True
        self._dice_button_cooldowns[user_id] = now
        return False
    
    async def send_blackjack_card(self, bot, chat_id: int, player_username: str, card: tuple, score: int, reply_markup: InlineKeyboardMarkup | None = None):
        image = self._build_card_image(card)
        caption = (
            f"🃏 @{player_username} вытянул карту {card[0]}{card[1]}.\n"
            f"Текущее количество очков: <b>{score}</b>."
        )
        try:
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return message
        except Exception as e:
            logger.error(f"Не удалось отправить карту игроку @{player_username}: {e}")
            return None
    
    def format_blackjack_scoreboard(self, game: BlackjackGame, reveal_all: bool = False) -> str:
        """Форматирует табло Blackjack. Если reveal_all=False, скрывает карты и очки соперника."""
        challenger = game.players["challenger"]
        target = game.players["target"]
        
        def format_player(player: BlackjackPlayer, is_revealed: bool) -> str:
            if not player:
                return "ожидание игрока"
            if is_revealed:
                cards_display = ', '.join([f'{c[0]}{c[1]}' for c in player.cards]) or 'нет карт'
                return f"@{player.user.username}: {player.score} очков ({cards_display})"
            else:
                # Скрываем карты и очки соперника
                card_count = len(player.cards)
                return f"@{player.user.username}: ??? очков ({card_count} карт)"
        
        challenger_line = format_player(challenger, is_revealed=reveal_all)
        target_line = format_player(target, is_revealed=reveal_all) if target else "ожидание игрока"
        
        return (
            f"🃏 <b>Blackjack</b>\n\n"
            f"{challenger_line}\n"
            f"{target_line}"
        )
    
    def build_blackjack_keyboard(self, game: BlackjackGame) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🃏 Вытянуть карту", callback_data=f"blackjack_hit_{game.game_id}")],
            [InlineKeyboardButton("✋ Остановиться", callback_data=f"blackjack_stand_{game.game_id}")]
        ])
    
    async def _edit_query_message(self, query, text: str, reply_markup=None, max_retries: int = 3):
        chat_id = query.message.chat_id if query and query.message else None
        attempt = 0

        while attempt < max_retries:
            try:
                await self._throttle_api_call(chat_id=chat_id)
                if query.message.photo:
                    await query.message.edit_caption(
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                else:
                    await query.message.edit_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                return
            except RetryAfter as e:
                attempt += 1
                wait_time = e.retry_after + 0.5
                logger.warning(
                    "Flood control при edit_message (попытка %s/%s). Ждем %.1f c.",
                    attempt,
                    max_retries,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
            except TimedOut as e:
                attempt += 1
                logger.warning(
                    "Таймаут при edit_message (попытка %s/%s): %s",
                    attempt,
                    max_retries,
                    e,
                )
                await asyncio.sleep(1.0 * attempt)
            except BadRequest as e:
                # Игнорируем ошибку "Message is not modified" - это не критично
                if "Message is not modified" in str(e):
                    logger.debug(f"Сообщение не изменилось (это нормально): {e}")
                else:
                    logger.warning(f"Не удалось обновить сообщение игрока: {e}")
                return
            except Exception as e:
                logger.warning(f"Не удалось обновить сообщение игрока: {e}")
                return

        logger.warning("Не удалось обновить сообщение после %s попыток", max_retries)
    
    async def _send_message_with_logo(self, update_message, text: str, reply_markup: InlineKeyboardMarkup, logo_filenames: List[str], max_retries: int = 2):
        """Отправляет сообщение с фото, если файл найден, иначе текстом"""
        logo_dir = Path(__file__).parent
        logo_path = None
        for filename in logo_filenames:
            path = logo_dir / filename
            if path.exists():
                logo_path = path
                break
        
        if logo_path:
            # Пытаемся отправить фото с повторными попытками
            for attempt in range(max_retries):
                try:
                    with open(logo_path, "rb") as photo:
                        return await update_message.reply_photo(
                            photo=photo,
                            caption=text,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.HTML
                        )
                except (TimedOut, Exception) as e:
                    error_name = type(e).__name__
                    # Проверяем, является ли это сетевой ошибкой
                    is_network_error = (
                        isinstance(e, TimedOut) or
                        (HAS_HTTPX and isinstance(e, HTTPX_ERRORS)) or
                        'Timeout' in error_name or
                        any(keyword in error_name for keyword in ['ReadError', 'WriteError', 'ConnectError', 'NetworkError'])
                    )
                    
                    if attempt < max_retries - 1 and is_network_error:
                        # Если это сетевая ошибка и есть еще попытки, ждем и повторяем
                        wait_time = 1.0 * (attempt + 1)
                        logger.warning(f"Сетевая ошибка при отправке фото {logo_path.name} (попытка {attempt + 1}/{max_retries}): {error_name}. Повтор через {wait_time}с...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        # Если это не сетевая ошибка или последняя попытка, логируем и отправляем текстом
                        logger.warning(f"Не удалось отправить фото {logo_path.name}: {error_name}: {e}. Отправляем текстовое сообщение.")
                        break
        
        return await update_message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    async def send_blackjack_prompt(self, context: ContextTypes.DEFAULT_TYPE, game: BlackjackGame, player: BlackjackPlayer):
        bot = context.bot
        try:
            keyboard = self.build_blackjack_keyboard(game)
            cards_text = ", ".join([f"{c[0]}{c[1]}" for c in player.cards]) or "нет карт"
            await bot.send_message(
                chat_id=player.user.id,
                text=(
                    f"🃏 <b>Ваша очередь в Blackjack {game.game_id[:6]}</b>\n\n"
                    f"Ваши карты: {cards_text}\n"
                    f"Очки: <b>{player.score}</b>\n\n"
                    f"Выберите действие:"
                ),
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Не удалось отправить ход игроку @{player.user.username}: {e}")
            await context.bot.send_message(
                game.chat_id,
                f"⚠️ @{player.user.username}, напишите /start боту в ЛС, чтобы продолжить игру!",
                parse_mode=ParseMode.HTML
            )

    def _build_card_image(self, card: tuple) -> BytesIO:
        """Загружает готовое изображение карты из папки cards/ или генерирует fallback"""
        rank, suit, value = card
        
        # Пытаемся загрузить готовое изображение
        cards_dir = Path(__file__).parent / "cards"
        card_filename = f"{rank}{suit}.png"
        card_path = cards_dir / card_filename
        
        # Также пробуем альтернативные форматы
        alternative_formats = [".png", ".jpg", ".jpeg"]
        
        for ext in alternative_formats:
            test_path = cards_dir / f"{rank}{suit}{ext}"
            if test_path.exists():
                try:
                    img = Image.open(test_path)
                    bio = BytesIO()
                    img.save(bio, format="PNG")
                    bio.seek(0)
                    return bio
                except Exception as e:
                    logger.warning(f"Не удалось загрузить изображение карты {test_path}: {e}")
        
        # Fallback: программная генерация, если изображение не найдено
        logger.warning(f"Изображение карты {card_filename} не найдено, используется программная генерация")
        return self._build_card_image_fallback(card)
    
    def _build_card_image_fallback(self, card: tuple) -> BytesIO:
        """Генерирует изображение карты программно (fallback)"""
        rank, suit, value = card
        width, height = 360, 520
        background = (255, 255, 255)
        border_color = (0, 0, 0)
        text_color = (200, 0, 0) if suit in ("♥", "♦") else (0, 0, 0)
        img = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(img)
        draw.rectangle([(10, 10), (width - 10, height - 10)], outline=border_color, width=4)
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
        draw.text((30, 30), f"{rank}{suit}", fill=text_color, font=font_small)
        draw.text((width - 70, height - 70), f"{rank}{suit}", fill=text_color, font=font_small)
        draw.text((width // 2 - 20, height // 2 - 20), f"{rank}{suit}", fill=text_color, font=font_large)
        if value is not None:
            draw.text((width // 2 - 40, height // 2 + 40), f"{value} очков", fill=text_color, font=font_small)
        bio = BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start - показывает главное меню только в ЛС"""
        # Сохраняем пользователя для рассылки
        if update.effective_user:
            if update.effective_user.id not in self.registered_users:
                self.registered_users.add(update.effective_user.id)
                self._save_registered_users()
        
        # Проверяем, что команда отправлена в личных сообщениях
        if update.effective_chat.type != "private":
            # Убираем клавиатуру из группового чата
            await update.message.reply_text(
                "❌ Команда /start работает только в личных сообщениях с ботом.\n"
                "Напишите боту в ЛС для доступа к меню.",
                parse_mode=ParseMode.HTML,
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        await self.show_main_menu(update)
        # Отправляем постоянную клавиатуру только при первом запуске
        await update.message.reply_text(
            "🎯 Используйте кнопки ниже для навигации по боту!",
            reply_markup=get_main_keyboard(update.effective_user.id)
        )
    
    async def refresh_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обновляет клавиатуру пользователя (только в ЛС)"""
        # Проверяем, что команда отправлена в личных сообщениях
        if update.effective_chat.type != "private":
            # Убираем клавиатуру из группового чата
            await update.message.reply_text(
                "❌ Команда /refresh работает только в личных сообщениях с ботом.",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        user_id = update.effective_user.id
        await update.message.reply_text(
            "🔄 Клавиатура обновлена!",
            reply_markup=get_main_keyboard(user_id)
        )
        
    async def show_main_menu(self, update: Update):
        """Показывает главное меню (личный кабинет)"""
        user = update.effective_user
        
        menu_text = (
            f"🎲 <b>Illidan Games</b>\n\n"
            f"👋 Добро пожаловать, <b>@{user.username}</b>!\n\n"
            f"📊 <b>Статус системы</b>\n"
            f"🟢 Работает в штатном режиме\n"
            f"💰 Валюта: <b>USDT</b>\n"
            f"📈 Комиссия: <b>{int(COMMISSION_RATE*100)}%</b>\n"
            f"💵 Ставки: <b>{MIN_BET}-{MAX_BET} USDT</b>\n\n"
            f"🎮 <b>Доступные игры:</b>\n"
            f"• 🎲 Кубики (Dice)\n"
            f"• 🃏 Blackjack (21)\n"
            f"• 🗿📄✂️ Камень-Ножницы-Бумага\n"
            f"• 🎲 Мульти-дуэли (3-5 игроков)\n\n"
            f"Выберите раздел ниже ⬇️"
        )
        
        keyboard = [
            # Профиль и статистика
            [
                InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                InlineKeyboardButton("📊 Статистика", callback_data="stats")
            ],
            [
                InlineKeyboardButton("🏆 Топ-10 игроков", callback_data="top_players")
            ],
            # Информация
            [
                InlineKeyboardButton("📋 О боте", callback_data="info_menu"),
                InlineKeyboardButton("❓ Помощь", callback_data="help_menu")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.message:
            # Используем локальный файл с повторными попытками при сетевых ошибках
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    with open('dice_menu_image.jpg', 'rb') as photo:
                        await update.message.reply_photo(
                            photo=photo,
                            caption=menu_text,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.HTML
                        )
                        return  # Успешно отправили, выходим
                except FileNotFoundError:
                    # Если файл не найден, отправляем без фото
                    await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
                    return
                except (TimedOut, Exception) as e:
                    error_name = type(e).__name__
                    # Проверяем, является ли это сетевой ошибкой
                    is_network_error = (
                        isinstance(e, TimedOut) or
                        (HAS_HTTPX and isinstance(e, HTTPX_ERRORS)) or
                        'Timeout' in error_name or
                        any(keyword in error_name for keyword in ['ReadError', 'WriteError', 'ConnectError', 'NetworkError'])
                    )
                    
                    if attempt < max_retries - 1 and is_network_error:
                        # Если это сетевая ошибка и есть еще попытки, ждем и повторяем
                        wait_time = 1.0 * (attempt + 1)
                        logger.warning(f"Сетевая ошибка при отправке фото dice_menu_image.jpg (попытка {attempt + 1}/{max_retries}): {error_name}. Повтор через {wait_time}с...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        # Если это не сетевая ошибка или последняя попытка, логируем и отправляем текстом
                        logger.warning(f"Не удалось отправить фото dice_menu_image.jpg: {error_name}: {e}. Отправляем текстовое сообщение.")
                        await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
                        return
        elif update.callback_query:
            # Для callback редактируем сообщение
            if update.callback_query.message.photo:
                await update.callback_query.edit_message_caption(
                    caption=menu_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.callback_query.edit_message_text(
                    text=menu_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
        
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        help_text = self.message_formatter.format_help_message()
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
        
    async def duel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /duel"""
        # Проверяем наличие аргументов
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "❌ Неверный формат команды!\n\n"
                "Вариант 1: /duel <сумма> [кол-во_кубиков] @username\n"
                "Вариант 2: Ответьте на сообщение пользователя: /duel <сумма> [кол-во_кубиков]\n\n"
                "Пример: /duel 1 @username\n"
                "Пример: /duel 1 2 @username (2 кубика на игрока)\n"
                "Пример: (ответ на сообщение) /duel 1 3"
            )
            return
            
        try:
            bet_amount = float(context.args[0])
            
            if bet_amount < MIN_BET:
                await update.message.reply_text(f"❌ Минимальная ставка: {MIN_BET} USDT!")
                return
            
            if bet_amount > MAX_BET:
                await update.message.reply_text(f"❌ Максимальная ставка: {MAX_BET} USDT!")
                return
            
            # Определяем количество кубиков (от 1 до 3) и target username
            target_username = None
            target_user = None
            dice_count = 3  # По умолчанию 3 кубика
            
            # Проверяем, является ли это ответом на сообщение
            if update.message.reply_to_message:
                # Формат: /duel <сумма> [dice_count]
                if len(context.args) >= 2:
                    # Второй аргумент может быть количеством кубиков
                    try:
                        dice_count = int(context.args[1])
                    except ValueError:
                        await update.message.reply_text(
                            "❌ Количество кубиков должно быть числом от 1 до 3!"
                        )
                        return
                # Приглашаем пользователя, на чье сообщение ответили
                target_user = update.message.reply_to_message.from_user
                target_username = target_user.username
                
                if not target_username:
                    await update.message.reply_text("❌ У пользователя нет username!")
                    return
                    
                if target_user.is_bot:
                    await update.message.reply_text("❌ Нельзя играть с ботом!")
                    return
                    
                logger.info(f"Дуэль по ответу на сообщение с @{target_username} (dice_count={dice_count})")
            else:
                # Форматы:
                # /duel <сумма> @username
                # /duel <сумма> <dice_count> @username
                if len(context.args) >= 3:
                    # /duel 1 2 @username
                    try:
                        dice_count = int(context.args[1])
                    except ValueError:
                        await update.message.reply_text(
                            "❌ Количество кубиков должно быть числом от 1 до 3!"
                        )
                        return
                    target_username = context.args[2].replace('@', '')
                elif len(context.args) >= 2:
                    # /duel 1 @username
                    target_username = context.args[1].replace('@', '')
                else:
                    await update.message.reply_text(
                        "❌ Укажите username или ответьте на сообщение пользователя!\n\n"
                        "Пример 1: /duel 1 @username\n"
                        "Пример 2: /duel 1 2 @username\n"
                        "Пример 3: (ответ на сообщение) /duel 1 3"
                    )
                    return

            # Проверяем корректность количества кубиков
            if dice_count < 1 or dice_count > 3:
                await update.message.reply_text("❌ Количество кубиков должно быть от 1 до 3!")
                return
                
            challenger = update.effective_user
            
            # Проверяем, что не играет сам с собой
            if challenger.username == target_username:
                await update.message.reply_text("❌ Вы не можете играть сами с собой!")
                return
                
            game_id = str(uuid.uuid4())[:8]
            
            # Создаем новую игру
            game = DiceGame(
                game_id=game_id,
                challenger=challenger,
                target_username=target_username,
                bet_amount=bet_amount,
                chat_id=update.effective_chat.id,
                dice_count=dice_count
            )
            
            self.active_games[game_id] = game
            await self._upsert_state_event("duel_created")
            
            # Отправляем сообщение с вызовом
            challenge_text = self.message_formatter.format_challenge_message(
                challenger.username, target_username, bet_amount, dice_count
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Принять", callback_data=f"accept_{game_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{game_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            duel_logo_files = [
                "duel_logo.jpg",
                "duel_logo.png",
                "dice_logo.jpg",
                "dice_logo.png",
                "dice_image.jpg"
            ]
            message = await self._send_message_with_logo(
                update.message,
                challenge_text,
                reply_markup,
                duel_logo_files
            )
            game.message_id = message.message_id
            
        except ValueError:
            await update.message.reply_text("❌ Неверная сумма ставки!")
        except Exception as e:
            logger.error(f"Ошибка в команде duel: {e}")
            await update.message.reply_text("❌ Произошла ошибка при создании игры!")

    async def blackjack_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Создание дуэли в 21 очко"""
        if update.effective_chat.type == "private":
            await update.message.reply_text("❌ Blackjack доступен только в группах!")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Неверный формат!\n"
                "Используйте: /blackjack <ставка> @username или ответьте на сообщение игрока."
            )
            return
        
        try:
            bet_amount = float(context.args[0])
            if bet_amount < MIN_BET:
                await update.message.reply_text(f"❌ Минимальная ставка: {MIN_BET} USDT!")
                return
            
            if bet_amount > MAX_BET:
                await update.message.reply_text(f"❌ Максимальная ставка: {MAX_BET} USDT!")
                return
            
            target_username = None
            target_user = None
            
            if update.message.reply_to_message:
                target_user = update.message.reply_to_message.from_user
                target_username = target_user.username
                if not target_username:
                    await update.message.reply_text("❌ У пользователя нет username!")
                    return
                if target_user.is_bot:
                    await update.message.reply_text("❌ Нельзя играть с ботом!")
                    return
            elif len(context.args) >= 2:
                target_username = context.args[1].replace("@", "")
            else:
                await update.message.reply_text(
                    "❌ Укажите username или ответьте на сообщение пользователя!"
                )
                return
            
            challenger = update.effective_user
            if challenger.username == target_username:
                await update.message.reply_text("❌ Нельзя играть с самим собой!")
                return
            
            game_id = str(uuid.uuid4())[:8]
            game = BlackjackGame(
                game_id=game_id,
                challenger=challenger,
                target_username=target_username,
                bet_amount=bet_amount,
                chat_id=update.effective_chat.id
            )
            
            if target_user:
                game.set_target_user(target_user)
            
            self.active_blackjack_games[game_id] = game
            await self._upsert_state_event("blackjack_created")
            
            text = (
                f"🃏 @{challenger.username} приглашает @{target_username} сыграть в Blackjack!\n"
                f"Ставка: <b>{bet_amount} USDT</b>\n\n"
                f"Ожидание ответа от @{target_username}."
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Принять", callback_data=f"blackjack_accept_{game_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"blackjack_decline_{game_id}")
                ]
            ]
            
            # Пытаемся отправить с фото логотипа Blackjack
            logo_paths = [
                Path(__file__).parent / "blackjack_logo.jpg",
                Path(__file__).parent / "blackjack_logo.png",
                Path(__file__).parent / "blackjack_banner.jpg",
                Path(__file__).parent / "blackjack_banner.png"
            ]
            
            logo_path = None
            for path in logo_paths:
                if path.exists():
                    logo_path = path
                    break
            
            if logo_path:
                # Отправляем с фото
                try:
                    with open(logo_path, 'rb') as photo:
                        message = await update.message.reply_photo(
                            photo=photo,
                            caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                except Exception as e:
                    logger.warning(f"Не удалось отправить фото логотипа Blackjack: {e}, отправляем без фото")
                    message = await update.message.reply_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            else:
                # Отправляем без фото, если логотип не найден
                message = await update.message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            game.message_id = message.message_id
        except ValueError:
            await update.message.reply_text("❌ Неверная сумма ставки!")
        except Exception as e:
            logger.error(f"Ошибка в команде blackjack: {e}")
            await update.message.reply_text("❌ Не удалось создать игру Blackjack.")
    
    async def knb_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Создание дуэли Камень-Ножницы-Бумага"""
        if update.effective_chat.type == "private":
            await update.message.reply_text("❌ КНБ доступен только в группах!")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Неверный формат!\n"
                "Используйте: /knb <ставка> @username или ответьте на сообщение игрока."
            )
            return
        
        try:
            bet_amount = float(context.args[0])
            if bet_amount < MIN_BET:
                await update.message.reply_text(f"❌ Минимальная ставка: {MIN_BET} USDT!")
                return
            
            if bet_amount > MAX_BET:
                await update.message.reply_text(f"❌ Максимальная ставка: {MAX_BET} USDT!")
                return
            
            target_username = None
            target_user = None
            
            if update.message.reply_to_message:
                target_user = update.message.reply_to_message.from_user
                target_username = target_user.username
                if not target_username:
                    await update.message.reply_text("❌ У пользователя нет username!")
                    return
                if target_user.is_bot:
                    await update.message.reply_text("❌ Нельзя играть с ботом!")
                    return
            elif len(context.args) >= 2:
                target_username = context.args[1].replace("@", "")
            else:
                await update.message.reply_text(
                    "❌ Укажите username или ответьте на сообщение пользователя!"
                )
                return
            
            challenger = update.effective_user
            if challenger.username == target_username:
                await update.message.reply_text("❌ Нельзя играть с самим собой!")
                return
            
            game_id = str(uuid.uuid4())[:8]
            game = RockPaperScissorsGame(
                game_id=game_id,
                challenger=challenger,
                target_username=target_username,
                bet_amount=bet_amount,
                chat_id=update.effective_chat.id
            )
            
            if target_user:
                game.target_user = target_user
            
            self.active_knb_games[game_id] = game
            await self._upsert_state_event("knb_created")
            
            text = (
                f"🗿📄✂️ @{challenger.username} приглашает @{target_username} сыграть в Камень-Ножницы-Бумага!\n"
                f"Ставка: <b>{bet_amount} USDT</b>\n\n"
                f"Игра до 3 побед. При ничье - переигровка раунда.\n\n"
                f"Ожидание ответа от @{target_username}."
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Принять", callback_data=f"knb_accept_{game_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"knb_decline_{game_id}")
                ]
            ]
            
            # Пытаемся отправить с фото логотипа КНБ
            logo_paths = [
                Path(__file__).parent / "knb_logo.jpg",
                Path(__file__).parent / "knb_logo.png",
                Path(__file__).parent / "knb_banner.jpg",
                Path(__file__).parent / "knb_banner.png"
            ]
            
            logo_path = None
            for path in logo_paths:
                if path.exists():
                    logo_path = path
                    break
            
            if logo_path:
                # Отправляем с фото
                try:
                    with open(logo_path, 'rb') as photo:
                        message = await update.message.reply_photo(
                            photo=photo,
                            caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                except Exception as e:
                    logger.warning(f"Не удалось отправить фото логотипа КНБ: {e}, отправляем без фото")
                    message = await update.message.reply_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            else:
                # Отправляем без фото, если логотип не найден
                message = await update.message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            game.message_id = message.message_id
        except ValueError:
            await update.message.reply_text("❌ Неверная сумма ставки!")
        except Exception as e:
            logger.error(f"Ошибка в команде knb: {e}")
            await update.message.reply_text("❌ Не удалось создать игру КНБ.")
    
    async def multiduel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /multiduel для мультиигры"""
        # Проверяем, что команда НЕ в личных сообщениях
        if update.effective_chat.type == "private":
            await update.message.reply_text(
                "❌ Мультидуэли можно создавать только в группах!\n\n"
                "Добавьте бота в группу и используйте команду там."
            )
            return
        
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "❌ Неверный формат команды!\n\n"
                "<b>Формат 1 (старый):</b>\n"
                "/multiduel &lt;ставка&gt; &lt;количество_игроков&gt; &lt;количество_кубиков&gt;\n"
                "Пример: /multiduel 1 4 2\n\n"
                "<b>Формат 2 (новый с кубиками):</b>\n"
                "/multiduel &lt;ставка&gt; &lt;количество_кубиков&gt; @player1 @player2 @player3\n"
                "Пример: /multiduel 1 2 @user1 @user2 @user3\n\n"
                "<b>Формат 3 (новый без кубиков, по умолчанию 1):</b>\n"
                "/multiduel &lt;ставка&gt; @player1 @player2 @player3\n"
                "Пример: /multiduel 1 @user1 @user2 @user3",
                parse_mode=ParseMode.HTML
            )
            return
            
        try:
            bet_amount = float(context.args[0])
            
            # Проверки ставки
            if bet_amount < MIN_BET:
                await update.message.reply_text(f"❌ Минимальная ставка: {MIN_BET} USDT!")
                return
            
            if bet_amount > MAX_BET:
                await update.message.reply_text(f"❌ Максимальная ставка: {MAX_BET} USDT!")
                return
            
            creator = update.effective_user
            game_id = str(uuid.uuid4())[:8]
            
            # Определяем формат команды
            # Логика определения:
            # 1. Если args[1] начинается с @, то это новый формат без кубиков
            # 2. Если args[1] - число и args[2] начинается с @, то это новый формат с кубиками
            # 3. Если args[1] - число и args[2] - число, то это старый формат
            
            is_new_format = False
            dice_count = 1  # По умолчанию 1 кубик
            start_index = 1  # Индекс начала списка @username или количества игроков
            
            if len(context.args) > 1:
                if context.args[1].startswith('@'):
                    # Новый формат без кубиков: /multiduel <ставка> @player1 @player2 @player3
                    is_new_format = True
                    start_index = 1
                elif len(context.args) > 2:
                    # Проверяем третий аргумент
                    try:
                        # Пытаемся преобразовать второй аргумент в число
                        second_arg = int(context.args[1])
                        if context.args[2].startswith('@'):
                            # Новый формат с кубиками: /multiduel <ставка> <кубики> @player1 @player2 @player3
                            is_new_format = True
                            dice_count = second_arg
                            if not (1 <= dice_count <= 3):
                                await update.message.reply_text("❌ Количество кубиков должно быть от 1 до 3!")
                                return
                            start_index = 2
                        # Иначе это старый формат (args[2] тоже число)
                    except ValueError:
                        # args[1] не число, но и не @username - ошибка
                        pass
            
            if is_new_format:
                # Новый формат: /multiduel <ставка> [<кубики>] @player1 @player2 @player3
                
                # Собираем всех @username
                usernames = []
                for arg in context.args[start_index:]:
                    if arg.startswith('@'):
                        username = arg[1:]  # Убираем @
                        usernames.append(username)
                    else:
                        await update.message.reply_text(f"❌ Неверный формат: ожидался @username, получено: {arg}")
                        return
                
                # Проверяем количество игроков
                if len(usernames) < 2 or len(usernames) > 4:
                    await update.message.reply_text(
                        "❌ Укажите от 2 до 4 игроков (создатель уже включен)!\n"
                        "Всего игроков должно быть от 3 до 5."
                    )
                    return
                
                # Проверяем, что создатель не указан в списке
                if creator.username in usernames:
                    await update.message.reply_text("❌ Вы уже являетесь создателем игры!")
                    return
                
                max_players = len(usernames) + 1  # +1 для создателя
                
                # Создаем новую мультиигру
                multi_game = MultiDiceGame(
                    game_id=game_id,
                    creator=creator,
                    bet_amount=bet_amount,
                    max_players=max_players,
                    dice_count=dice_count,
                    chat_id=update.effective_chat.id
                )
                
                # Ищем и приглашаем игроков по username
                bot = update.message.get_bot()
                invited_count = 0
                not_found = []
                
                for username in usernames:
                    try:
                        # Пытаемся получить информацию о пользователе из чата
                        # Передаем username без @
                        chat_member = await bot.get_chat_member(update.effective_chat.id, username)
                        if chat_member.user:
                            user = chat_member.user
                            if user.is_bot:
                                not_found.append(f"@{username} (это бот)")
                            else:
                                if multi_game.invite_player(user):
                                    invited_count += 1
                                else:
                                    not_found.append(f"@{username} (уже приглашен или нет места)")
                    except Exception as e:
                        logger.warning(f"Не удалось найти пользователя @{username}: {e}")
                        not_found.append(f"@{username}")
                
                self.active_multi_games[game_id] = multi_game
                await self._upsert_state_event("multi_created_with_invites")
                
                # Формируем сообщение
                players_list = multi_game.get_players_list_text()
                search_text = (
                    f"🎲 <b>Мульти-куб создан!</b>\n\n"
                    f"Ставка: <b>{bet_amount} USDT</b>\n"
                    f"Кубиков: <b>{dice_count}</b>\n\n"
                    f"{players_list}"
                )
                
                reply_markup = self._build_multi_game_keyboard(multi_game)
                dice_logo_files = ["dice_logo.jpg", "dice_logo.png", "dice_image.jpg"]
                message = await self._send_message_with_logo(
                    update.message,
                    search_text,
                    reply_markup,
                    dice_logo_files
                )
                multi_game.message_id = message.message_id
                multi_game.message_has_photo = bool(message.photo)
                
            else:
                # Старый формат: /multiduel <ставка> <количество_игроков> <количество_кубиков>
                if len(context.args) < 3:
                    await update.message.reply_text(
                        "❌ Неверный формат команды!\n\n"
                        "Используйте: /multiduel &lt;ставка&gt; &lt;игроки&gt; &lt;кубики&gt;\n\n"
                        "&lt;ставка&gt; - сумма ставки\n"
                        "&lt;игроки&gt; - количество игроков (3-5)\n"
                        "&lt;кубики&gt; - количество кубиков (1-3)\n\n"
                        "Пример: /multiduel 1 4 2"
                    )
                    return
                
                max_players = int(context.args[1])
                dice_count = int(context.args[2])
                
                # Проверки
                if not (3 <= max_players <= 5):
                    await update.message.reply_text("❌ Количество игроков должно быть от 3 до 5!")
                    return
                    
                if not (1 <= dice_count <= 3):
                    await update.message.reply_text("❌ Количество кубиков должно быть от 1 до 3!")
                    return
                
                # Создаем новую мультиигру
                multi_game = MultiDiceGame(
                    game_id=game_id,
                    creator=creator,
                    bet_amount=bet_amount,
                    max_players=max_players,
                    dice_count=dice_count,
                    chat_id=update.effective_chat.id
                )
                
                self.active_multi_games[game_id] = multi_game
                await self._upsert_state_event("multi_created")
                
                # Отправляем сообщение с поиском игроков
                players_list = multi_game.get_players_list_text()
                search_text = (
                    f"🎲 <b>Поиск игроков для Мульти-куб</b>\n\n"
                    f"Ставка: <b>{bet_amount} USDT</b>\n"
                    f"Кубиков: <b>{dice_count}</b>\n\n"
                    f"{players_list}"
                )
                
                reply_markup = self._build_multi_game_keyboard(multi_game)
                dice_logo_files = ["dice_logo.jpg", "dice_logo.png", "dice_image.jpg"]
                message = await self._send_message_with_logo(
                    update.message,
                    search_text,
                    reply_markup,
                    dice_logo_files
                )
                multi_game.message_id = message.message_id
                multi_game.message_has_photo = bool(message.photo)
            
        except ValueError:
            await update.message.reply_text("❌ Неверный формат чисел!")
        except Exception as e:
            logger.error(f"Ошибка в команде multiduel: {e}")
            await update.message.reply_text("❌ Произошла ошибка при создании мультиигры!")
    
    def _build_multi_game_keyboard(self, game: MultiDiceGame) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton("✅ Принять предложение", callback_data=f"multi_join_{game.game_id}")],
            [InlineKeyboardButton("❌ Отменить мульти-дуэль", callback_data=f"multi_cancel_{game.game_id}")]
        ]
        return InlineKeyboardMarkup(buttons)
    
    async def update_multi_game_message(self, game: MultiDiceGame, bot):
        """Обновляет сообщение игры в чате"""
        if not game.message_id:
            return False
        
        try:
            players_list = game.get_players_list_text()
            # Определяем заголовок
            if game.invited_players or len(game.players) > 1:
                title = "🎲 <b>Мульти-куб</b>"
            else:
                title = "🎲 <b>Поиск игроков для Мульти-куб</b>"
            
            search_text = (
                f"{title}\n\n"
                f"Ставка: <b>{game.bet_amount} USDT</b>\n"
                f"Кубиков: <b>{game.dice_count}</b>\n\n"
                f"{players_list}"
            )
            
            if game.is_full():
                search_text += "\n\n✅ <b>Все игроки набраны! Ожидание оплаты...</b>"
            
            reply_markup = self._build_multi_game_keyboard(game)
            
            edit_kwargs = dict(
                chat_id=game.chat_id,
                message_id=game.message_id,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
            if game.message_has_photo:
                await bot.edit_message_caption(
                    caption=search_text,
                    **edit_kwargs,
                )
            else:
                await bot.edit_message_text(
                    text=search_text,
                    **edit_kwargs,
                )
            return True
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение игры {game.game_id}: {e}")
            return False
    
    async def multiduelkick_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /multiduelkick для удаления игрока из мультидуэли (только создатель)"""
        # Проверяем, что команда НЕ в личных сообщениях
        if update.effective_chat.type == "private":
            await update.message.reply_text("❌ Эта команда работает только в группах!")
            return
        
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "❌ Неверный формат команды!\n\n"
                "Используйте: /multiduelkick @username\n\n"
                "Пример: /multiduelkick @player1"
            )
            return
        
        try:
            username = context.args[0].replace('@', '')
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            
            # Находим последнюю активную мультидуэль в этом чате
            active_multi_game = None
            latest_multi_time = None
            
            for game in self.active_multi_games.values():
                if game.chat_id == chat_id and game.status == "waiting":
                    if latest_multi_time is None or game.created_at > latest_multi_time:
                        active_multi_game = game
                        latest_multi_time = game.created_at
            
            if not active_multi_game:
                await update.message.reply_text("❌ В этом чате нет активной мультидуэли в статусе ожидания!")
                return
            
            # Проверяем, что пользователь - создатель игры
            if active_multi_game.creator.id != user_id:
                await update.message.reply_text("❌ Только создатель игры может удалять игроков!")
                return
            
            # Ищем игрока для удаления
            player_to_remove = None
            for player in active_multi_game.players:
                if player.username == username:
                    player_to_remove = player
                    break
            
            if not player_to_remove:
                await update.message.reply_text(f"❌ Игрок @{username} не найден в игре!")
                return
            
            # Удаляем игрока
            if active_multi_game.remove_player(player_to_remove.id):
                # Обновляем сообщение игры
                bot = update.message.get_bot()
                if await self.update_multi_game_message(active_multi_game, bot):
                    await update.message.reply_text(f"✅ Игрок @{username} удален из игры.")
                else:
                    await update.message.reply_text(f"✅ Игрок @{username} удален из игры, но не удалось обновить сообщение игры.")
            else:
                await update.message.reply_text(f"❌ Не удалось удалить игрока @{username}!")
                
        except Exception as e:
            logger.error(f"Ошибка в команде multiduelkick: {e}")
            await update.message.reply_text("❌ Произошла ошибка при удалении игрока!")
    
    async def invitem_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /invitem для приглашения игрока в мультидуэль (только создатель)"""
        # Проверяем, что команда НЕ в личных сообщениях
        if update.effective_chat.type == "private":
            await update.message.reply_text("❌ Эта команда работает только в группах!")
            return
        
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "❌ Неверный формат команды!\n\n"
                "Используйте: /invitem @username\n\n"
                "Пример: /invitem @player1"
            )
            return
        
        try:
            username = context.args[0].replace('@', '')
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            
            # Находим последнюю активную мультидуэль в этом чате
            active_multi_game = None
            latest_multi_time = None
            
            for game in self.active_multi_games.values():
                if game.chat_id == chat_id and game.status == "waiting":
                    if latest_multi_time is None or game.created_at > latest_multi_time:
                        active_multi_game = game
                        latest_multi_time = game.created_at
            
            if not active_multi_game:
                await update.message.reply_text("❌ В этом чате нет активной мультидуэли в статусе ожидания!")
                return
            
            # Проверяем, что пользователь - создатель игры
            if active_multi_game.creator.id != user_id:
                await update.message.reply_text("❌ Только создатель игры может приглашать игроков!")
                return
            
            # Проверяем, не полна ли игра
            if active_multi_game.is_full():
                await update.message.reply_text("❌ Игра уже полна!")
                return
            
            # Ищем пользователя в чате
            bot = update.message.get_bot()
            try:
                # Передаем username без @
                chat_member = await bot.get_chat_member(chat_id, username)
                if not chat_member.user:
                    await update.message.reply_text(f"❌ Пользователь @{username} не найден в чате!")
                    return
                
                user = chat_member.user
                
                # Проверяем, что это не бот
                if user.is_bot:
                    await update.message.reply_text("❌ Нельзя приглашать ботов!")
                    return
                
                # Проверяем, что это не создатель
                if user.id == active_multi_game.creator.id:
                    await update.message.reply_text("❌ Вы уже являетесь создателем игры!")
                    return
                
                # Приглашаем игрока
                if active_multi_game.invite_player(user):
                    # Обновляем сообщение игры
                    if await self.update_multi_game_message(active_multi_game, bot):
                        await update.message.reply_text(f"✅ Игрок @{username} приглашен в игру.")
                    else:
                        await update.message.reply_text(f"✅ Игрок @{username} приглашен в игру, но не удалось обновить сообщение игры.")
                    
                    # Уведомляем приглашенного игрока
                    try:
                        await bot.send_message(
                            user.id,
                            f"📩 Вас пригласили в мультидуэль!\n\n"
                            f"Ставка: {active_multi_game.bet_amount} USDT\n"
                            f"Кубиков: {active_multi_game.dice_count}\n\n"
                            f"Вернитесь в чат и нажмите кнопку «Принять предложение».",
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить уведомление пользователю @{username}: {e}")
                else:
                    await update.message.reply_text(
                        f"❌ Не удалось пригласить @{username}!\n"
                        f"Возможно, он уже в игре или уже приглашен."
                    )
                    
            except Exception as e:
                logger.error(f"Ошибка при поиске пользователя @{username}: {e}")
                await update.message.reply_text(f"❌ Пользователь @{username} не найден в чате!")
                
        except Exception as e:
            logger.error(f"Ошибка в команде invitem: {e}")
            await update.message.reply_text("❌ Произошла ошибка при приглашении игрока!")
            



    async def _cancel_regular_game(self, game, context: ContextTypes.DEFAULT_TYPE):
        """Выполняет отмену обычной дуэли и возвращает текст уведомления"""
        refund_messages = []

        if game.challenger_paid:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.challenger.id,
                game.bet_amount,
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                try:
                    await context.bot.send_message(
                        game.challenger.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML,
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.challenger.username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.challenger.username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.challenger.username}")

        if game.target_paid and game.target_user:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.target_user.id,
                game.bet_amount,
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                try:
                    await context.bot.send_message(
                        game.target_user.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML,
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.target_username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.target_username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.target_username}")

        if game.message_id:
            try:
                await context.bot.delete_message(
                    chat_id=game.chat_id,
                    message_id=game.message_id,
                )
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение игры {game.game_id}: {e}")

        game.status = "cancelled"
        if game.game_id in self.active_games:
            del self.active_games[game.game_id]
        await self._upsert_state_event("regular_game_cancelled")

        cancel_text = self.message_formatter.format_cancel_message(
            game.challenger.username,
            game.target_username,
        )

        if refund_messages:
            cancel_text += "\n\n" + "\n".join(refund_messages)

        return cancel_text

    async def _cancel_multi_game(self, game, context: ContextTypes.DEFAULT_TYPE, cancelled_by_creator: bool = False):
        """Отменяет мультиигру и возвращает текст уведомления"""
        refund_messages = []
        for player in game.players:
            if game.players_paid.get(player.id):
                success, check_link, check_data = await self.escrow_manager.refund_stake(
                    player.id,
                    game.bet_amount,
                )
                if success and check_data:
                    # Сохраняем чек в хранилище
                    self.check_manager.add_check(check_data)
                if success:
                    try:
                        await context.bot.send_message(
                            player.id,
                            f"💰 <b>Возврат ставки</b>\n\n"
                            f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                            f"Чек для получения:\n{check_link}",
                            parse_mode=ParseMode.HTML,
                        )
                        refund_messages.append(f"✅ Чек отправлен @{player.username} в ЛС")
                    except Exception as e:
                        logger.error(f"Ошибка отправки чека в ЛС: {e}")
                        refund_messages.append(f"❌ Ошибка отправки чека @{player.username}")
                else:
                    refund_messages.append(f"❌ Ошибка возврата ставки @{player.username}")

        if game.message_id:
            try:
                await context.bot.delete_message(
                    chat_id=game.chat_id,
                    message_id=game.message_id,
                )
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение мультиигры {game.game_id}: {e}")

        game.status = "cancelled"
        if game.game_id in self.active_multi_games:
            del self.active_multi_games[game.game_id]
        await self._upsert_state_event("multi_game_cancelled")

        players_list = ", ".join([f"@{p.username}" for p in game.players])
        reason = "создателем" if cancelled_by_creator else "администратором"
        cancel_text = f"❌ <b>Мультиигра отменена {reason}</b>\n\nУчастники: {players_list}"

        if refund_messages:
            cancel_text += "\n\n" + "\n".join(refund_messages)

        return cancel_text

    async def _cancel_blackjack_game(self, game: BlackjackGame, context: ContextTypes.DEFAULT_TYPE, reason: str = "администратором"):
        refund_messages = []
        players = [
            (game.challenger, game.challenger_paid),
            (game.target_user, game.target_paid)
        ]
        for player, paid in players:
            if player and paid:
                success, check_link, check_data = await self.escrow_manager.refund_stake(player.id, game.bet_amount)
                if success and check_data:
                    # Сохраняем чек в хранилище
                    self.check_manager.add_check(check_data)
                if success:
                    try:
                        await context.bot.send_message(
                            player.id,
                            f"💰 <b>Возврат ставки</b>\n\n"
                            f"Чек: {check_link}",
                            parse_mode=ParseMode.HTML
                        )
                        refund_messages.append(f"✅ Чек отправлен @{player.username}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки чека @{player.username}: {e}")
                        refund_messages.append(f"❌ Ошибка отправки @{player.username}")
                else:
                    refund_messages.append(f"❌ Ошибка возврата @{player.username}")
        if game.message_id:
            try:
                await context.bot.delete_message(chat_id=game.chat_id, message_id=game.message_id)
            except Exception:
                pass
        if game.game_id in self.active_blackjack_games:
            del self.active_blackjack_games[game.game_id]
        await self._upsert_state_event("blackjack_game_cancelled")
        text = (
            f"❌ <b>Blackjack отменён {reason}</b>\n"
            f"Участники: @{game.challenger.username} и @{game.target_username}"
        )
        if refund_messages:
            text += "\n\n" + "\n".join(refund_messages)
        await context.bot.send_message(game.chat_id, text, parse_mode=ParseMode.HTML)
    
    async def _cancel_knb_game(self, game: RockPaperScissorsGame, context: ContextTypes.DEFAULT_TYPE, reason: str = "администратором"):
        """Отменяет игру КНБ с возвратом ставок"""
        refund_messages = []
        players = [
            (game.challenger, game.challenger_paid),
            (game.target_user, game.target_paid)
        ]
        for player, paid in players:
            if player and paid:
                success, check_link, check_data = await self.escrow_manager.refund_stake(player.id, game.bet_amount)
                if success and check_data:
                    # Сохраняем чек в хранилище
                    self.check_manager.add_check(check_data)
                if success:
                    try:
                        await context.bot.send_message(
                            player.id,
                            f"💰 <b>Возврат ставки</b>\n\n"
                            f"Чек: {check_link}",
                            parse_mode=ParseMode.HTML
                        )
                        refund_messages.append(f"✅ Чек отправлен @{player.username}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки чека @{player.username}: {e}")
                        refund_messages.append(f"❌ Ошибка отправки @{player.username}")
                else:
                    refund_messages.append(f"❌ Ошибка возврата @{player.username}")
        if game.message_id:
            try:
                await context.bot.delete_message(chat_id=game.chat_id, message_id=game.message_id)
            except Exception:
                pass
        if game.game_id in self.active_knb_games:
            del self.active_knb_games[game.game_id]
        await self._upsert_state_event("knb_game_cancelled")
        text = (
            f"❌ <b>КНБ отменён {reason}</b>\n"
            f"Участники: @{game.challenger.username} и @{game.target_username}"
        )
        if refund_messages:
            text += "\n\n" + "\n".join(refund_messages)
        await context.bot.send_message(game.chat_id, text, parse_mode=ParseMode.HTML)
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /cancel (только для администратора)
        
        Теперь поддерживает формат: /cancel <код_игры>, где код_игры — это
        короткий ID из админ-панели (например, 73c661).
        """
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ У вас нет прав для выполнения этой команды!")
            return
        
        # Проверяем аргументы
        if not context.args:
            await update.message.reply_text(
                "❌ Неверный формат команды!\n\n"
                "Используйте: /cancel <код_игры>\n\n"
                "Код игры можно посмотреть в админ-панели в разделе «Активные игры» "
                "(например: 73c661)."
            )
            return
        
        short_id = context.args[0].strip()
        if len(short_id) < 3:
            await update.message.reply_text("❌ Код игры должен содержать хотя бы 3 символа!")
            return
        
        # Ищем игру по коду (по префиксу ID) среди активных
        active_game = None
        active_multi_game = None
        active_blackjack_game = None
        active_knb_game = None
        matches_count = 0
        
        for game_id, game in self.active_games.items():
            if game_id.startswith(short_id) and game.status in ["waiting", "payment_pending", "playing"]:
                active_game = game
                matches_count += 1
        
        for game_id, game in self.active_multi_games.items():
            if game_id.startswith(short_id) and game.status in ["waiting", "payment_pending", "playing"]:
                active_multi_game = game
                matches_count += 1
        
        for game_id, game in self.active_blackjack_games.items():
            if game_id.startswith(short_id) and game.status in ["waiting", "payment_pending", "playing"]:
                active_blackjack_game = game
                matches_count += 1
        
        for game_id, game in self.active_knb_games.items():
            if game_id.startswith(short_id) and game.status in ["waiting", "payment_pending", "playing"]:
                active_knb_game = game
                matches_count += 1
        
        if matches_count == 0:
            await update.message.reply_text("❌ Игра с таким кодом не найдена среди активных!")
            return
        if matches_count > 1:
            await update.message.reply_text(
                "❌ Найдено несколько игр с таким кодом!\n"
                "Уточните код игры (используйте больше символов)."
            )
            return
        
        if active_game:
            cancel_text = await self._cancel_regular_game(active_game, context)
            await update.message.reply_text(cancel_text, parse_mode=ParseMode.HTML)
        elif active_multi_game:
            cancel_text = await self._cancel_multi_game(active_multi_game, context)
            await update.message.reply_text(cancel_text, parse_mode=ParseMode.HTML)
        elif active_blackjack_game:
            await self._cancel_blackjack_game(active_blackjack_game, context)
        elif active_knb_game:
            await self._cancel_knb_game(active_knb_game, context)
    
    async def cancelall_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отменяет все игры в статусе waiting (только для администратора)"""
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ У вас нет прав для выполнения этой команды!")
            return
        
        games_to_cancel = [
            game for game in self.active_games.values()
            if game.status == "waiting"
        ]
        multi_games_to_cancel = [
            game for game in self.active_multi_games.values()
            if game.status == "waiting"
        ]
        blackjack_games_to_cancel = [
            game for game in self.active_blackjack_games.values()
            if game.status == "waiting"
        ]
        knb_games_to_cancel = [
            game for game in self.active_knb_games.values()
            if game.status == "waiting"
        ]
        
        if not any([games_to_cancel, multi_games_to_cancel, blackjack_games_to_cancel, knb_games_to_cancel]):
            await update.message.reply_text("❌ Нет игр в статусе waiting для отмены!")
            return
        
        cancelled_messages = []
        
        for game in list(games_to_cancel):
            cancel_text = await self._cancel_regular_game(game, context)
            cancelled_messages.append(
                f"🎯 <b>Дуэль <code>{game.game_id[:6]}</code></b>\n{cancel_text}"
            )
        
        for game in list(multi_games_to_cancel):
            cancel_text = await self._cancel_multi_game(game, context)
            cancelled_messages.append(
                f"🎯 <b>Мультиигра <code>{game.game_id[:6]}</code></b>\n{cancel_text}"
            )
        
        for game in list(blackjack_games_to_cancel):
            await self._cancel_blackjack_game(game, context)
            cancelled_messages.append(
                f"🎯 <b>Blackjack <code>{game.game_id[:6]}</code></b>\nОтменено."
            )
        
        for game in list(knb_games_to_cancel):
            await self._cancel_knb_game(game, context)
            cancelled_messages.append(
                f"🎯 <b>КНБ <code>{game.game_id[:6]}</code></b>\nОтменено."
            )
        
        summary_text = "🧹 <b>Отмена игр в статусе waiting</b>\n\n" + "\n\n".join(cancelled_messages)
        
        await update.message.reply_text(summary_text, parse_mode=ParseMode.HTML)
        
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        user_id = query.from_user.id
        now = time.monotonic()
        last_press = self._callback_cooldowns.get(user_id, 0.0)
        if now - last_press < self._callback_cooldown:
            await query.answer("⏳ Не спешите! Подождите чуть-чуть.", show_alert=False)
            return
        self._callback_cooldowns[user_id] = now
        await query.answer()
        
        data = query.data
        
        # Кнопки главного меню
        if data == "profile":
            await self.show_profile(query)
        elif data == "stats":
            # Статистика - то же что и профиль
            await self.show_profile(query)
        elif data == "top_players":
            await self.show_top_players(query)
        elif data == "help_menu":
            help_text = self.message_formatter.format_help_message()
            keyboard = [[InlineKeyboardButton("« Назад", callback_data="main_menu")]]
            # Если сообщение с фото, редактируем caption
            if query.message.photo:
                await query.edit_message_caption(caption=help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        elif data == "main_menu":
            await self.show_main_menu(update)
        elif data == "info_menu":
            info_text = self.message_formatter.format_info_message()
            keyboard = [[InlineKeyboardButton("« Назад", callback_data="main_menu")]]
            # Если сообщение с фото, редактируем caption
            if query.message.photo:
                await query.edit_message_caption(caption=info_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(info_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        elif data == "admin_panel":
            await self.show_admin_panel(query=query)
        # Кнопки игры
        elif data.startswith("accept_"):
            game_id = data.split("_")[1]
            await self.handle_game_accept(query, game_id)
        elif data.startswith("decline_"):
            game_id = data.split("_")[1]
            await self.handle_game_decline(query, game_id)
        elif data.startswith("blackjack_accept_"):
            game_id = data.split("_")[2]
            await self.handle_blackjack_accept(query, context, game_id)
        elif data.startswith("blackjack_decline_"):
            game_id = data.split("_")[2]
            await self.handle_blackjack_decline(query, game_id)
        elif data.startswith("knb_accept_"):
            game_id = data.split("_")[2]
            await self.handle_knb_accept(query, context, game_id)
        elif data.startswith("knb_decline_"):
            game_id = data.split("_")[2]
            await self.handle_knb_decline(query, game_id)
        elif data.startswith("knb_choice_"):
            # Формат: knb_choice_{game_id}_{choice}
            parts = data.split("_")
            game_id = parts[2]
            choice = parts[3]  # rock, paper, scissors
            await self.handle_knb_choice(query, context, game_id, choice)
        elif data.startswith("roll_"):
            game_id = data.split("_")[1]
            await self.handle_dice_roll(query, game_id)
        # Кнопки мультиигры
        elif data.startswith("multi_join_"):
            game_id = data.split("_")[2]
            await self.handle_multi_join(query, game_id)
        elif data.startswith("multi_roll_"):
            game_id = data.split("_")[2]
            await self.handle_multi_dice_roll(query, game_id)
        elif data.startswith("blackjack_hit_"):
            game_id = data.split("_")[2]
            await self.handle_blackjack_hit(query, context, game_id)
        elif data.startswith("blackjack_stand_"):
            game_id = data.split("_")[2]
            await self.handle_blackjack_stand(query, context, game_id)
        elif data.startswith("blackjack_ace_"):
            # Формат: blackjack_ace_{game_id}_{value}
            parts = data.split("_")
            game_id = parts[2]
            ace_value = int(parts[3])
            await self.handle_blackjack_ace_choice(query, context, game_id, ace_value)
        elif data.startswith("multi_cancel_"):
            game_id = data.split("_")[2]
            await self.handle_multi_cancel(query, context, game_id)
        elif data == "admin_advert":
            await self.handle_admin_advert(query, context)
        # Кнопки админ панели
        elif data == "admin_broadcast":
            await self.handle_admin_broadcast(query, context)
        elif data == "admin_refresh":
            await self.show_admin_panel(query=query)
        elif data == "admin_checks":
            await self.show_admin_checks(query)
        elif data == "check_cancel_all":
            await self.handle_check_cancel_all(query)
        elif data.startswith("check_cancel_"):
            check_id = int(data.split("_")[2])
            await self.handle_check_cancel(query, check_id)
        elif data == "admin_checks_refresh":
            await self.show_admin_checks(query)
            
    async def update_user_keyboard(self, chat_id: int, user_id: int, bot):
        """Обновляет клавиатуру пользователя, отправляя новое сообщение"""
        try:
            await bot.send_message(
                chat_id,
                "Используйте кнопки ниже",
                reply_markup=get_main_keyboard(user_id)
            )
        except Exception as e:
            logger.error(f"Ошибка обновления клавиатуры: {e}")
    
    async def cancelr_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /cancelr для отмены рассылки (только для администратора)"""
        user_id = update.effective_user.id
        
        if not is_admin(user_id):
            await update.message.reply_text("❌ У вас нет прав для выполнения этой команды!")
            return
        
        # Проверяем, ожидается ли сообщение для рассылки
        cancelled = False
        if context.user_data.get('awaiting_broadcast'):
            context.user_data['awaiting_broadcast'] = False
            cancelled = True
        
        if context.user_data.get('awaiting_advert'):
            context.user_data['awaiting_advert'] = False
            cancelled = True
        
        if cancelled:
            await update.message.reply_text("❌ Рассылка отменена.")
            await self.show_admin_panel(update=update)
        else:
            await update.message.reply_text("ℹ️ Нет активной рассылки для отмены.")
    
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщений от пользователя в ЛС (кнопки, рассылки, реклама)"""
        if update.effective_chat.type != "private":
            return
        
        message = update.message
        user_id = update.effective_user.id
        
        # Обработка обычной рассылки
        if context.user_data.get('awaiting_broadcast') and is_admin(user_id):
            await message.reply_text("📤 Начинаю рассылку...")
            
            success_count = 0
            error_count = 0
            has_attachment = bool(message.effective_attachment)
            is_forwarded = bool(message.forward_date)
            is_plain_text = bool(message.text) and not has_attachment and not is_forwarded
            
            broadcast_text = None
            if is_plain_text:
                broadcast_text = (
                    "📢 <b>Уведомление от администратора</b>\n\n"
                    f"{message.text}"
                )
            
            for target_id in self.registered_users:
                try:
                    if is_plain_text:
                        await context.bot.send_message(
                            target_id,
                            broadcast_text,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await context.bot.copy_message(
                            chat_id=target_id,
                            from_chat_id=message.chat_id,
                            message_id=message.message_id
                        )
                    success_count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения пользователю {target_id}: {e}")
                    error_count += 1
            
            context.user_data['awaiting_broadcast'] = False
            
            await message.reply_text(
                f"✅ <b>Рассылка завершена!</b>\n\n"
                f"Успешно отправлено: {success_count}\n"
                f"Ошибок: {error_count}\n"
                f"Всего пользователей: {len(self.registered_users)}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(user_id)
            )
            return
        
        # Обработка рекламной рассылки
        if context.user_data.get('awaiting_advert') and is_admin(user_id):
            await message.reply_text("🪧 Начинаю рекламную рассылку...")
            
            success_count = 0
            error_count = 0
            
            for target_id in self.registered_users:
                try:
                    await context.bot.copy_message(
                        chat_id=target_id,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id
                    )
                    success_count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Ошибка отправки рекламы пользователю {target_id}: {e}")
                    error_count += 1
            
            context.user_data['awaiting_advert'] = False
            
            await message.reply_text(
                f"✅ <b>Реклама отправлена!</b>\n\n"
                f"Успешно: {success_count}\n"
                f"Ошибок: {error_count}\n"
                f"Всего пользователей: {len(self.registered_users)}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(user_id)
            )
            return
        
        text = message.text
        if not text:
            return
        
        if text == "🏠 Главное меню":
            # Показываем главное меню
            await self.show_main_menu(update)
        elif text == "👤 Профиль":
            # Показываем профиль
            user = update.effective_user
            profile_text = self.profile_manager.format_profile_text(user.id, user.username)
            keyboard = [[InlineKeyboardButton("« Назад", callback_data="main_menu")]]
            await update.message.reply_text(
                profile_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        elif text == "📊 Статистика":
            # Показываем статистику (то же что и профиль)
            user = update.effective_user
            profile_text = self.profile_manager.format_profile_text(user.id, user.username)
            keyboard = [[InlineKeyboardButton("« Назад", callback_data="main_menu")]]
            await update.message.reply_text(
                profile_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        elif text == "ℹ️ О боте":
            # Показываем информацию
            info_text = self.message_formatter.format_info_message()
            await update.message.reply_text(
                info_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(user_id)
            )
        elif text == "❓ Помощь":
            # Показываем помощь
            help_text = self.message_formatter.format_help_message()
            await update.message.reply_text(
                help_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(user_id)
            )
        elif text == "⚙️ Админ панель":
            # Показываем админ панель только администраторам
            if not is_admin(user_id):
                await update.message.reply_text("❌ У вас нет прав для доступа к админ панели!")
                return
            await self.show_admin_panel(update)
    
    async def show_admin_panel(self, update: Update = None, query = None):
        """Показывает админ панель"""
        # Определяем источник (update или query)
        if query:
            message = query.message
            user = query.from_user
            chat = query.message.chat
        else:
            message = update.message if update.message else None
            user = update.effective_user
            chat = update.effective_chat
        # Формируем список активных игр
        active_games_text = "🎮 <b>Активные игры</b>\n\n"
        
        # Обычные дуэли
        if self.active_games:
            active_games_text += "<b>Обычные дуэли:</b>\n"
            for game_id, game in self.active_games.items():
                status_emoji = {
                    "waiting": "⏳",
                    "payment_pending": "💳",
                    "playing": "🎲",
                    "finished": "✅",
                    "cancelled": "❌",
                    "declined": "❌"
                }.get(game.status, "❓")
                
                challenger_name = game.challenger.username if game.challenger else "?"
                target_name = game.target_username
                
                active_games_text += (
                    f"{status_emoji} <code>{game_id}</code> | "
                    f"@{challenger_name} vs @{target_name} | "
                    f"{game.bet_amount} USDT | {game.status}\n"
                )
        else:
            active_games_text += "Обычные дуэли: нет активных\n"
        
        active_games_text += "\n"
        
        # Мультидуэли
        if self.active_multi_games:
            active_games_text += "<b>Мультидуэли:</b>\n"
            for game_id, game in self.active_multi_games.items():
                status_emoji = {
                    "waiting": "⏳",
                    "payment_pending": "💳",
                    "playing": "🎲",
                    "finished": "✅",
                    "cancelled": "❌"
                }.get(game.status, "❓")
                
                players_count = len(game.players)
                players_names = ", ".join([f"@{p.username}" for p in game.players[:3]])
                if players_count > 3:
                    players_names += f" и еще {players_count - 3}"
                
                active_games_text += (
                    f"{status_emoji} <code>{game_id}</code> | "
                    f"{players_count}/{game.max_players} игроков ({players_names}) | "
                    f"{game.bet_amount} USDT | {game.status}\n"
                )
        else:
            active_games_text += "Мультидуэли: нет активных\n"

        # Blackjack игры
        if self.active_blackjack_games:
            active_games_text += "\n<b>Blackjack:</b>\n"
            for game_id, game in self.active_blackjack_games.items():
                status_emoji = {
                    "waiting": "⏳",
                    "payment_pending": "💳",
                    "playing": "🃏",
                    "finished": "✅",
                    "cancelled": "❌"
                }.get(game.status, "❓")
                opponent = game.target_username or "?"
                active_games_text += (
                    f"{status_emoji} <code>{game_id}</code> | "
                    f"@{game.challenger.username} vs @{opponent} | "
                    f"{game.bet_amount} USDT | {game.status}\n"
                )
        else:
            active_games_text += "\nBlackjack: нет активных\n"
        
        # КНБ игры
        if self.active_knb_games:
            active_games_text += "\n<b>КНБ:</b>\n"
            for game_id, game in self.active_knb_games.items():
                status_emoji = {
                    "waiting": "⏳",
                    "payment_pending": "💳",
                    "playing": "🗿",
                    "finished": "✅",
                    "cancelled": "❌",
                    "declined": "❌"
                }.get(game.status, "❓")
                opponent = game.target_username or "?"
                # Показываем счет, если игра идет
                score_text = ""
                if game.status == "playing" and game.challenger_player and game.target_player:
                    score_text = f" | {game.challenger_player.wins}-{game.target_player.wins}"
                active_games_text += (
                    f"{status_emoji} <code>{game_id}</code> | "
                    f"@{game.challenger.username} vs @{opponent}{score_text} | "
                    f"{game.bet_amount} USDT | {game.status}\n"
                )
        else:
            active_games_text += "\nКНБ: нет активных\n"
        
        active_games_text += f"\n📊 Всего пользователей: {len(self.registered_users)}"
        
        keyboard = [
            [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton("🪧 Реклама", callback_data="admin_advert")],
            [InlineKeyboardButton("💳 Управление чеками", callback_data="admin_checks")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin_refresh")],
            [InlineKeyboardButton("« Назад", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if query:
            # Редактируем сообщение из callback query
            try:
                if query.message.photo:
                    await query.edit_message_caption(
                        caption=active_games_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await query.edit_message_text(
                        text=active_games_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )
            except TelegramError as e:
                # Игнорируем ошибку, если сообщение не изменилось
                if "Message is not modified" in str(e):
                    await query.answer("✅ Информация актуальна")
                else:
                    logger.error(f"Ошибка редактирования сообщения админ панели: {e}")
        elif message:
            # Отправляем новое сообщение
            await message.reply_text(
                active_games_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
    
    async def handle_admin_broadcast(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает запрос на рассылку"""
        if not is_admin(query.from_user.id):
            await query.answer("❌ У вас нет прав!")
            return
        
        context.user_data['awaiting_advert'] = False
        
        await query.edit_message_text(
            "📢 <b>Рассылка сообщений</b>\n\n"
            "Отправьте сообщение, которое хотите разослать всем пользователям.\n"
            "Или напишите /cancelr для отмены.",
            parse_mode=ParseMode.HTML
        )
        
        # Сохраняем состояние ожидания сообщения для рассылки
        context.user_data['awaiting_broadcast'] = True
    
    async def handle_admin_advert(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Готовит бота к приёму рекламного сообщения"""
        if not is_admin(query.from_user.id):
            await query.answer("❌ У вас нет прав!")
            return
        
        context.user_data['awaiting_broadcast'] = False
        
        await query.edit_message_text(
            "🪧 <b>Рекламная рассылка</b>\n\n"
            "Отправьте готовый пост в личном чате: это может быть текст, фото, видео, GIF, "
            "кнопки и ссылки. Сообщение будет скопировано всем пользователям.\n\n"
            "Для отмены используйте /cancelr.",
            parse_mode=ParseMode.HTML
        )
        
        context.user_data['awaiting_advert'] = True
    
    async def show_admin_checks(self, query):
        """Показывает список чеков для управления"""
        if not is_admin(query.from_user.id):
            await query.answer("❌ У вас нет прав!")
            return
        
        await query.answer("⏳ Загружаю чеки из CryptoBot...")
        
        # Получаем активные (неактивированные) чеки из CryptoBot API
        # Статус "active" означает неактивированные чеки, которые можно отменить
        api_checks = await self.escrow_manager.get_checks(status="active", limit=50)
        
        if not api_checks:
            text = "💳 <b>Управление чеками</b>\n\nНет активных чеков в CryptoBot."
            keyboard = [
                [InlineKeyboardButton("🔄 Обновить", callback_data="admin_checks_refresh")],
                [InlineKeyboardButton("« Назад в админ панель", callback_data="admin_panel")]
            ]
        else:
            # Формируем список чеков для отображения
            text = f"💳 <b>Управление чеками</b>\n\nНайдено активных чеков: {len(api_checks)}\n\n"
            
            # Сортируем чеки по дате создания (новые первыми)
            sorted_checks = sorted(api_checks, key=lambda x: x.get("date", 0), reverse=True)
            
            # Показываем максимум 10 чеков
            display_checks = sorted_checks[:10]
            
            for i, check in enumerate(display_checks, 1):
                check_id = check.get("check_id", "?")
                amount = check.get("amount", 0)
                asset = check.get("asset", "USDT")
                status = check.get("status", "unknown")
                date = check.get("date", 0)
                
                # Форматируем дату
                if date:
                    try:
                        from datetime import datetime
                        date_str = datetime.fromtimestamp(date).strftime("%d.%m.%Y %H:%M")
                    except:
                        date_str = "?"
                else:
                    date_str = "?"
                
                status_emoji = "⏳" if status == "active" else "✅" if status == "activated" else "❌"
                
                text += (
                    f"{i}. <code>{check_id}</code> | {amount} {asset}\n"
                    f"   Статус: {status_emoji} {status} | Создан: {date_str}\n\n"
                )
            
            if len(sorted_checks) > 10:
                text += f"\n<i>Показано 10 из {len(sorted_checks)} чеков</i>"
            
            text += "\n\n<i>Выберите чек для отмены:</i>"
            
            keyboard = []
            # Добавляем кнопки для отмены чеков (максимум 10)
            for check in display_checks:
                check_id = check.get("check_id")
                amount = check.get("amount", 0)
                asset = check.get("asset", "USDT")
                if check_id:
                    keyboard.append([
                        InlineKeyboardButton(
                            f"❌ Отменить чек {check_id} ({amount} {asset})",
                            callback_data=f"check_cancel_{check_id}"
                        )
                    ])
            
            # Добавляем кнопку для отмены всех чеков
            keyboard.append([InlineKeyboardButton("🗑️ Отменить все активные чеки", callback_data="check_cancel_all")])
            keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="admin_checks_refresh")])
            keyboard.append([InlineKeyboardButton("« Назад в админ панель", callback_data="admin_panel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            if query.message.photo:
                await query.edit_message_caption(
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            else:
                await query.edit_message_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Ошибка редактирования сообщения управления чеками: {e}")
                await query.answer("❌ Ошибка при обновлении")
    
    async def handle_check_cancel_all(self, query):
        """Обрабатывает отмену всех активных чеков"""
        if not is_admin(query.from_user.id):
            await query.answer("❌ У вас нет прав!")
            return
        
        await query.answer("⏳ Загружаю список чеков...")
        
        # Получаем все активные чеки
        api_checks = await self.escrow_manager.get_checks(status="active", limit=100)
        
        if not api_checks:
            await query.answer("✅ Нет активных чеков для отмены")
            await self.show_admin_checks(query)
            return
        
        # Показываем прогресс
        await query.edit_message_text(
            f"⏳ <b>Отмена всех чеков</b>\n\n"
            f"Найдено активных чеков: {len(api_checks)}\n"
            f"Отменяю...",
            parse_mode=ParseMode.HTML
        )
        
        # Отменяем все чеки
        success_count = 0
        failed_count = 0
        failed_checks = []
        
        for check in api_checks:
            check_id = check.get("check_id")
            if check_id:
                success = await self.escrow_manager.delete_check(check_id)
                if success:
                    success_count += 1
                    # Помечаем чек как отмененный в хранилище (если он там есть)
                    stored_check = self.check_manager.get_check(check_id)
                    if stored_check:
                        self.check_manager.mark_cancelled(check_id)
                else:
                    failed_count += 1
                    failed_checks.append(check_id)
                # Небольшая задержка между запросами
                await asyncio.sleep(0.5)
        
        # Формируем итоговое сообщение
        result_text = (
            f"✅ <b>Отмена чеков завершена</b>\n\n"
            f"Успешно отменено: {success_count}\n"
        )
        
        if failed_count > 0:
            result_text += f"Ошибок: {failed_count}\n"
            if len(failed_checks) <= 5:
                result_text += f"Не удалось отменить: {', '.join(map(str, failed_checks))}\n"
        
        result_text += f"\nВсего обработано: {len(api_checks)}"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Обновить список", callback_data="admin_checks_refresh")],
            [InlineKeyboardButton("« Назад в админ панель", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            result_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    async def handle_check_cancel(self, query, check_id: int):
        """Обрабатывает отмену чека"""
        if not is_admin(query.from_user.id):
            await query.answer("❌ У вас нет прав!")
            return
        
        # Показываем подтверждение
        await query.answer("⏳ Отменяю чек...")
        
        # Отменяем чек через CryptoBot API
        success = await self.escrow_manager.delete_check(check_id)
        
        if success:
            # Помечаем чек как отмененный в хранилище (если он там есть)
            check = self.check_manager.get_check(check_id)
            if check:
                self.check_manager.mark_cancelled(check_id)
            await query.answer(f"✅ Чек {check_id} успешно отменен!")
            # Обновляем список чеков
            await self.show_admin_checks(query)
        else:
            await query.answer("❌ Ошибка при отмене чека. Проверьте логи.")
    
    async def show_profile(self, query):
        """Показывает профиль пользователя"""
        user = query.from_user
        profile_text = self.profile_manager.format_profile_text(user.id, user.username)
        
        keyboard = [[InlineKeyboardButton("« Назад", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Если сообщение с фото, редактируем caption
        if query.message.photo:
            await query.edit_message_caption(caption=profile_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text(profile_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    
    async def show_top_players(self, query):
        """Показывает топ-10 игроков по ставкам"""
        top_players_text = self.profile_manager.format_top_players_text(10)
        
        keyboard = [[InlineKeyboardButton("« Назад", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Если сообщение с фото, редактируем caption
        if query.message.photo:
            await query.edit_message_caption(caption=top_players_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text(top_players_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    
    async def handle_multi_join(self, query, game_id: str):
        """Обработка присоединения к мультиигре"""
        if game_id not in self.active_multi_games:
            await query.answer("❌ Игра не найдена или уже началась!")
            return
            
        game = self.active_multi_games[game_id]
        user = query.from_user
        
        # Проверяем, что игрок еще не присоединился
        if user.id in [p.id for p in game.players]:
            await query.answer("❌ Вы уже присоединились к игре!")
            return
            
        # Проверяем, что есть места
        if game.is_full():
            await query.answer("❌ Все места заняты!")
            return
        
        # Проверяем, что игрок написал /start в ЛС бота
        bot = query.message.get_bot()
        try:
            await bot.send_chat_action(user.id, "typing")
        except Exception as e:
            logger.warning(f"Игрок @{user.username} не написал /start в ЛС бота: {e}")
            # Отправляем уведомление в чат
            await query.message.chat.send_message(
                f"⚠️ @{user.username}, вы не написали /start боту в личные сообщения!\n\n"
                f"Для участия в игре необходимо:\n"
                f"1️⃣ Перейти в Личные Сообщения с ботом\n"
                f"2️⃣ Нажать кнопку «Старт/Start» или написать /start\n"
                f"3️⃣ Вернуться сюда и снова нажать «Принять предложение»",
                parse_mode=ParseMode.HTML
            )
            await query.answer("⚠️ Сначала напишите /start боту в ЛС!")
            return
            
        # Добавляем игрока
        game.add_player(user)
        await self._upsert_state_event("multi_player_joined")
        
        # Обновляем сообщение игры
        await self.update_multi_game_message(game, bot)
        
        if game.is_full():
            # Все места заняты - начинаем оплату
            game.status = "payment_pending"
            game.payment_start_time = datetime.now()  # Начинаем отсчёт времени оплаты
            await self._upsert_state_event("multi_payment_pending")
            logger.info(f"Мультиигра {game_id}: начат отсчёт 5 минут на оплату")
            
            # Отправляем счета всем игрокам в ЛС
            for player in game.players:
                payment_info = await self.escrow_manager.create_invoice_in_bot(
                    f"{game_id}_{player.id}",
                    game.bet_amount,
                    f"Мульти-куб {game_id[:6]} • {game.bet_amount} USDT"
                )
                
                if payment_info:
                    game.players_payment_ids[player.id] = payment_info['invoice_id']
                    
                    try:
                        invoice_text = (
                            f"💰 <b>Мульти-куб {game_id[:6]}</b>\n\n"
                            f"Ставка: <b>{game.bet_amount} USDT</b>\n"
                            f"Игроков: {game.max_players}\n"
                            f"Кубиков: {game.dice_count}\n\n"
                            f"Оплатите участие в течение 5 мин.\n"
                            f"После оплаты бот подтвердит её автоматически (webhook)."
                        )
                        
                        keyboard = [[InlineKeyboardButton("💳 Оплатить в @CryptoBot", url=payment_info['pay_url'])]]
                        
                        await bot.send_message(
                            player.id,
                            invoice_text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки счета игроку {player.username}: {e}")
            await self._upsert_state_event("multi_invoices_created")
    
    async def handle_multi_cancel(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str):
        """Отмена мультиигры по кнопке (доступно только создателю)"""
        if game_id not in self.active_multi_games:
            await query.answer("❌ Игра не найдена или уже отменена!", show_alert=True)
            return
        
        game = self.active_multi_games[game_id]
        
        if query.from_user.id != game.creator.id:
            await query.answer("❌ Только создатель может отменить эту мульти-дуэль!", show_alert=True)
            return
        
        cancel_text = await self._cancel_multi_game(game, context, cancelled_by_creator=True)
        await context.bot.send_message(game.chat_id, cancel_text, parse_mode=ParseMode.HTML)
        await query.answer("✅ Мульти-дуэль отменена")
    
    async def handle_game_accept(self, query, game_id: str):
        """Обработка принятия игры"""
        if game_id not in self.active_games:
            await self._edit_query_message(query, "❌ Игра не найдена или уже завершена!")
            return
            
        game = self.active_games[game_id]
        bot = query.message.get_bot()
        
        # Проверяем, что принял приглашенный игрок
        if query.from_user.username != game.target_username:
            await query.answer("❌ Только приглашенный игрок может принять вызов!")
            return
            
        if query.from_user.id == game.challenger.id:
            await query.answer("❌ Вы не можете принять свой собственный вызов!")
            return
        
        # Проверяем, что оба игрока написали /start боту в ЛС
        users_to_check = [
            ("challenger", game.challenger),
            ("target", query.from_user)
        ]
        
        for role, user in users_to_check:
            try:
                await bot.send_chat_action(user.id, "typing")
            except Exception as e:
                logger.warning(f"Игрок @{user.username} не написал /start боту в ЛС: {e}")
                # Отправляем инструкцию в чат
                await query.message.chat.send_message(
                    f"⚠️ @{user.username}, вы не написали /start боту в личные сообщения!\n\n"
                    f"Для участия в игре необходимо:\n"
                    f"1️⃣ Перейти в Личные Сообщения с ботом\n"
                    f"2️⃣ Нажать кнопку «Старт/Start» или написать /start\n"
                    f"3️⃣ Вернуться сюда и снова нажать «Принять вызов»",
                    parse_mode=ParseMode.HTML
                )
                await query.answer("⚠️ Сначала напишите /start боту в ЛС!")
                return
            
        game.target_user = query.from_user
        game.status = "payment_pending"
        game.payment_start_time = datetime.now()  # Начинаем отсчёт времени оплаты
        await self._upsert_state_event("duel_payment_pending")
        logger.info(f"Игра {game_id}: @{game.target_username} принял вызов, начат отсчёт 5 минут на оплату")
        
        # Создаем счета для обоих игроков
        # Счет для challenger
        challenger_payment = await self.escrow_manager.create_invoice_in_bot(
            f"{game_id}_challenger", 
            game.bet_amount,
            f"Матч {game_id[:6]} • Ставка {game.bet_amount} USDT"
        )
        
        # Счет для target
        target_payment = await self.escrow_manager.create_invoice_in_bot(
            f"{game_id}_target", 
            game.bet_amount,
            f"Матч {game_id[:6]} • Ставка {game.bet_amount} USDT"
        )
        
        if challenger_payment and target_payment:
            game.challenger_payment_id = challenger_payment['invoice_id']
            game.target_payment_id = target_payment['invoice_id']
            await self._upsert_state_event("duel_invoices_created")
            
            # Отправляем счета игрокам в ЛС
            try:
                # Счет для challenger в ЛС
                challenger_invoice_text = (
                    f"💰 <b>Матч {game_id[:6]}</b>\n\n"
                    f"Ставка: <b>{game.bet_amount} USDT</b>\n"
                    f"Списано: 0.00\n"
                    f"К оплате: <b>{game.bet_amount} USDT</b>\n\n"
                    f"Оплатите участие в течение 5 мин.\n"
                    f"После оплаты бот подтвердит её автоматически (webhook)."
                )
                
                keyboard_challenger = [[InlineKeyboardButton("💳 Оплатить в @CryptoBot", url=challenger_payment['pay_url'])]]
                
                await bot.send_message(
                    game.challenger.id,
                    challenger_invoice_text,
                    reply_markup=InlineKeyboardMarkup(keyboard_challenger),
                    parse_mode=ParseMode.HTML
                )
                
                # Счет для target в ЛС
                target_invoice_text = (
                    f"💰 <b>Матч {game_id[:6]}</b>\n\n"
                    f"Ставка: <b>{game.bet_amount} USDT</b>\n"
                    f"Списано: 0.00\n"
                    f"К оплате: <b>{game.bet_amount} USDT</b>\n\n"
                    f"Оплатите участие в течение 5 мин.\n"
                    f"После оплаты бот подтвердит её автоматически (webhook)."
                )
                
                keyboard_target = [[InlineKeyboardButton("💳 Оплатить в @CryptoBot", url=target_payment['pay_url'])]]
                
                await bot.send_message(
                    game.target_user.id,
                    target_invoice_text,
                    reply_markup=InlineKeyboardMarkup(keyboard_target),
                    parse_mode=ParseMode.HTML
                )
                
                # В чате показываем уведомление
                chat_notification = (
                    f"📋 <b>Оплаты запрошены</b>\n\n"
                    f"Откройте ЛС с ботом. Оплатите счёт для игры!"
                )
                
                await self._edit_query_message(
                    query,
                    chat_notification,
                    reply_markup=None
                )
                
            except Exception as e:
                logger.error(f"Ошибка отправки счетов: {e}")
                await self._edit_query_message(query, "❌ Ошибка! Убедитесь, что вы написали боту /start в ЛС!")
        else:
            await self._edit_query_message(query, "❌ Ошибка при создании счетов!")
            
    async def handle_game_decline(self, query, game_id: str):
        """Обработка отклонения игры"""
        if game_id not in self.active_games:
            await self._edit_query_message(query, "❌ Игра не найдена!")
            return
            
        game = self.active_games[game_id]
        
        # Проверяем, что отклонил участник дуэли (challenger или target)
        if (query.from_user.id != game.challenger.id and 
            query.from_user.username != game.target_username):
            await query.answer("❌ Только участники дуэли могут отклонить вызов!")
            return
        
        # Возвращаем ставки, если они были оплачены
        refund_messages = []
        if game.challenger_paid:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.challenger.id,
                game.bet_amount
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                # Отправляем чек в ЛС игроку
                try:
                    await query.bot.send_message(
                        game.challenger.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.challenger.username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.challenger.username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.challenger.username}")
        
        if game.target_paid and game.target_user:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.target_user.id,
                game.bet_amount
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                # Отправляем чек в ЛС игроку
                try:
                    await query.bot.send_message(
                        game.target_user.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.target_username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.target_username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.target_username}")
            
        game.status = "declined"
        
        # Определяем, кто отклонил
        decliner_name = query.from_user.username
            
        decline_text = self.message_formatter.format_decline_message(
            game.challenger.username, game.target_username, decliner_name
        )
        
        if refund_messages:
            decline_text += "\n\n" + "\n".join(refund_messages)
        
        await self._edit_query_message(query, decline_text)
        del self.active_games[game_id]
        await self._upsert_state_event("duel_declined")

    async def handle_blackjack_accept(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str):
        if game_id not in self.active_blackjack_games:
            await query.answer("❌ Игра не найдена или уже завершена!")
            return
        
        game = self.active_blackjack_games[game_id]
        if query.from_user.username != game.target_username:
            await query.answer("❌ Только приглашенный игрок может принять вызов!")
            return
        
        if game.status != "waiting":
            await query.answer("❌ Игра уже принята!")
            return
        
        # Проверяем, что оба игрока написали /start боту в ЛС
        bot = query.message.get_bot()
        users_to_check = [
            ("challenger", game.challenger),
            ("target", query.from_user)
        ]
        
        for role, user in users_to_check:
            try:
                await bot.send_chat_action(user.id, "typing")
            except Exception as e:
                logger.warning(f"Игрок @{user.username} не написал /start боту в ЛС: {e}")
                # Отправляем инструкцию в чат
                await query.message.chat.send_message(
                    f"⚠️ @{user.username}, вы не написали /start боту в личные сообщения!\n\n"
                    f"Для участия в игре необходимо:\n"
                    f"1️⃣ Перейти в Личные Сообщения с ботом\n"
                    f"2️⃣ Нажать кнопку «Старт/Start» или написать /start\n"
                    f"3️⃣ Вернуться сюда и снова нажать «Принять»",
                    parse_mode=ParseMode.HTML
                )
                await query.answer("⚠️ Сначала напишите /start боту в ЛС!")
                return
        
        game.set_target_user(query.from_user)
        game.status = "payment_pending"
        game.payment_start_time = datetime.now()
        await self._upsert_state_event("blackjack_payment_pending")
        
        challenger_invoice = await self.escrow_manager.create_invoice_in_bot(
            f"{game_id}_bj_challenger",
            game.bet_amount,
            f"Blackjack {game_id[:6]} • {game.bet_amount} USDT"
        )
        target_invoice = await self.escrow_manager.create_invoice_in_bot(
            f"{game_id}_bj_target",
            game.bet_amount,
            f"Blackjack {game_id[:6]} • {game.bet_amount} USDT"
        )
        
        if not challenger_invoice or not target_invoice:
            await query.answer("❌ Не удалось создать счета, попробуйте позже.", show_alert=True)
            game.status = "cancelled"
            del self.active_blackjack_games[game_id]
            await self._upsert_state_event("blackjack_cancelled_invoice_error")
            return
        
        game.challenger_payment_id = challenger_invoice["invoice_id"]
        game.target_payment_id = target_invoice["invoice_id"]
        await self._upsert_state_event("blackjack_invoices_created")
        
        for player, invoice in [
            (game.challenger, challenger_invoice),
            (game.target_user, target_invoice)
        ]:
            try:
                keyboard = [[InlineKeyboardButton("💳 Оплатить в @CryptoBot", url=invoice["pay_url"])]]
                await context.bot.send_message(
                    chat_id=player.id,
                    text=(
                        f"🃏 <b>Blackjack {game_id[:6]}</b>\n\n"
                        f"Ставка: {game.bet_amount} USDT\n"
                        f"Оплатите участие в течение 5 минут.\n"
                        f"После оплаты бот подтвердит её автоматически (webhook)."
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Ошибка отправки счета игроку {player.username}: {e}")
        
        # Используем _edit_query_message для поддержки фото-сообщений
        await self._edit_query_message(
            query,
            f"🃏 Игра принята!\n"
            f"Ожидание оплаты от @{game.challenger.username} и @{game.target_username}.",
            reply_markup=None
        )

    async def handle_blackjack_decline(self, query, game_id: str):
        """Обработка отклонения приглашения на Blackjack"""
        if game_id not in self.active_blackjack_games:
            await query.answer("❌ Игра не найдена!")
            return
        
        game = self.active_blackjack_games[game_id]
        user_id = query.from_user.id
        user_username = query.from_user.username
        
        # Проверяем, что отклонил участник дуэли (challenger или target)
        is_challenger = user_id == game.challenger.id
        is_target = user_username == game.target_username
        
        if not (is_challenger or is_target):
            await query.answer("❌ Только участники дуэли могут отклонить вызов!")
            return
        
        # Возвращаем ставки, если они были оплачены
        refund_messages = []
        if game.challenger_paid:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.challenger.id,
                game.bet_amount
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                try:
                    await query.bot.send_message(
                        game.challenger.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.challenger.username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.challenger.username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.challenger.username}")
        
        if game.target_paid and game.target_user:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.target_user.id,
                game.bet_amount
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                try:
                    await query.bot.send_message(
                        game.target_user.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.target_username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.target_username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.target_username}")
        
        game.status = "declined"
        
        # Определяем, кто отклонил
        decliner_name = user_username
        if is_challenger:
            decline_text = f"❌ @{decliner_name} отклонил приглашение в Blackjack."
        else:
            decline_text = f"❌ @{decliner_name} отклонил приглашение в Blackjack."
        
        if refund_messages:
            decline_text += "\n\n" + "\n".join(refund_messages)
        
        await self._edit_query_message(query, decline_text, reply_markup=None)
        del self.active_blackjack_games[game_id]
        await self._upsert_state_event("blackjack_declined")
    
    async def handle_knb_accept(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str):
        """Обработка принятия приглашения на КНБ"""
        if game_id not in self.active_knb_games:
            await query.answer("❌ Игра не найдена или уже завершена!")
            return
        
        game = self.active_knb_games[game_id]
        if query.from_user.username != game.target_username:
            await query.answer("❌ Только приглашенный игрок может принять вызов!")
            return
        
        if game.status != "waiting":
            await query.answer("❌ Игра уже принята!")
            return
        
        # Проверяем, что оба игрока написали /start боту в ЛС
        bot = query.message.get_bot()
        users_to_check = [
            ("challenger", game.challenger),
            ("target", query.from_user)
        ]
        
        for role, user in users_to_check:
            try:
                await bot.send_chat_action(user.id, "typing")
            except Exception as e:
                logger.warning(f"Игрок @{user.username} не написал /start боту в ЛС: {e}")
                # Отправляем инструкцию в чат
                await query.message.chat.send_message(
                    f"⚠️ @{user.username}, вы не написали /start боту в личные сообщения!\n\n"
                    f"Для участия в игре необходимо:\n"
                    f"1️⃣ Перейти в Личные Сообщения с ботом\n"
                    f"2️⃣ Написать /start\n"
                    f"3️⃣ Вернуться сюда и принять вызов заново",
                    parse_mode=ParseMode.HTML
                )
                return
        
        # Устанавливаем target_user
        game.target_user = query.from_user
        game.status = "payment_pending"
        game.payment_start_time = datetime.now()
        await self._upsert_state_event("knb_payment_pending")
        
        # Создаем счета для обоих игроков
        challenger_payment = await self.escrow_manager.create_invoice_in_bot(
            f"{game_id}_knb_challenger",
            game.bet_amount,
            f"КНБ {game_id[:6]} • {game.bet_amount} USDT"
        )
        target_payment = await self.escrow_manager.create_invoice_in_bot(
            f"{game_id}_knb_target",
            game.bet_amount,
            f"КНБ {game_id[:6]} • {game.bet_amount} USDT"
        )
        
        if challenger_payment and target_payment:
            game.challenger_payment_id = challenger_payment['invoice_id']
            game.target_payment_id = target_payment['invoice_id']
            await self._upsert_state_event("knb_invoices_created")
            
            try:
                # Отправляем счет инициатору
                keyboard = [[InlineKeyboardButton("💳 Оплатить в @CryptoBot", url=challenger_payment['pay_url'])]]
                await context.bot.send_message(
                    chat_id=game.challenger.id,
                    text=(
                        f"🗿📄✂️ <b>КНБ {game_id[:6]}</b>\n\n"
                        f"Ставка: {game.bet_amount} USDT\n"
                        f"Оплатите участие в течение 5 минут.\n"
                        f"После оплаты бот подтвердит её автоматически (webhook)."
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                
                # Отправляем счет оппоненту
                keyboard = [[InlineKeyboardButton("💳 Оплатить в @CryptoBot", url=target_payment['pay_url'])]]
                await context.bot.send_message(
                    chat_id=game.target_user.id,
                    text=(
                        f"🗿📄✂️ <b>КНБ {game_id[:6]}</b>\n\n"
                        f"Ставка: {game.bet_amount} USDT\n"
                        f"Оплатите участие в течение 5 минут.\n"
                        f"После оплаты бот подтвердит её автоматически (webhook)."
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Ошибка отправки счета: {e}")
            
            await self._edit_query_message(
                query,
                f"🗿📄✂️ Игра принята!\n"
                f"Ожидание оплаты от @{game.challenger.username} и @{game.target_username}.",
                reply_markup=None
            )
        else:
            await self._edit_query_message(query, "❌ Ошибка при создании счетов!")
    
    async def handle_knb_decline(self, query, game_id: str):
        """Обработка отклонения приглашения на КНБ"""
        if game_id not in self.active_knb_games:
            await query.answer("❌ Игра не найдена!")
            return
        
        game = self.active_knb_games[game_id]
        user_id = query.from_user.id
        user_username = query.from_user.username
        
        # Проверяем, что отклонил участник дуэли (challenger или target)
        is_challenger = user_id == game.challenger.id
        is_target = user_username == game.target_username
        
        if not (is_challenger or is_target):
            await query.answer("❌ Только участники дуэли могут отклонить вызов!")
            return
        
        # Возвращаем ставки, если они были оплачены
        refund_messages = []
        if game.challenger_paid:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.challenger.id,
                game.bet_amount
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                try:
                    await query.bot.send_message(
                        game.challenger.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.challenger.username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.challenger.username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.challenger.username}")
        
        if game.target_paid and game.target_user:
            success, check_link, check_data = await self.escrow_manager.refund_stake(
                game.target_user.id,
                game.bet_amount
            )
            if success and check_data:
                # Сохраняем чек в хранилище
                self.check_manager.add_check(check_data)
            if success:
                try:
                    await query.bot.send_message(
                        game.target_user.id,
                        f"💰 <b>Возврат ставки</b>\n\n"
                        f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                        f"Чек для получения:\n{check_link}",
                        parse_mode=ParseMode.HTML
                    )
                    refund_messages.append(f"✅ Чек отправлен @{game.target_username} в ЛС")
                except Exception as e:
                    logger.error(f"Ошибка отправки чека в ЛС: {e}")
                    refund_messages.append(f"❌ Ошибка отправки чека @{game.target_username}")
            else:
                refund_messages.append(f"❌ Ошибка возврата ставки @{game.target_username}")
        
        game.status = "declined"
        
        # Определяем, кто отклонил
        decliner_name = user_username
        if is_challenger:
            decline_text = f"❌ @{decliner_name} отклонил приглашение в КНБ."
        else:
            decline_text = f"❌ @{decliner_name} отклонил приглашение в КНБ."
        
        if refund_messages:
            decline_text += "\n\n" + "\n".join(refund_messages)
        
        await self._edit_query_message(query, decline_text, reply_markup=None)
        del self.active_knb_games[game_id]
        await self._upsert_state_event("knb_declined")
    
    async def _start_duel_game(self, context: ContextTypes.DEFAULT_TYPE, game):
        """Запускает обычную дуэль после оплаты"""
        game.status = "playing"
        game.last_action_time = datetime.now()
        await self._upsert_state_event("duel_started")
        
        start_text = self.message_formatter.format_game_start(game)
        scoreboard_text = self.message_formatter.format_scoreboard(game)
        
        keyboard = [
            [InlineKeyboardButton(
                f"🎲 Бросок 1/{game.dice_count}",
                callback_data=f"roll_{game.game_id}"
            )]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await self._bot_send_message_with_retry(
            context.bot,
            game.chat_id,
            start_text,
            parse_mode=ParseMode.HTML
        )
        
        await self._bot_send_message_with_retry(
            context.bot,
            game.chat_id,
            scoreboard_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    async def _start_multi_game(self, context: ContextTypes.DEFAULT_TYPE, game):
        """Запускает мультидуэль после оплаты"""
        game.status = "playing"
        if hasattr(game, 'last_action_time'):
            game.last_action_time = datetime.now()
        await self._upsert_state_event("multi_started")
        
        start_text = (
            f"🎲 <b>Мульти-куб начинается!</b>\n\n"
            f"Участников: {len(game.players)}\n"
            f"Ставка: {game.bet_amount} USDT\n"
            f"Кубиков: {game.dice_count}\n\n"
            f"Порядок ходов определяется очередью присоединения!"
        )
        
        await self._bot_send_message_with_retry(
            context.bot,
            game.chat_id,
            start_text,
            parse_mode=ParseMode.HTML
        )
        
        first_player = game.get_current_player()
        scoreboard = game.get_scoreboard_text()
        
        keyboard = [[InlineKeyboardButton(
            f"🎲 Бросок 1/{game.dice_count}",
            callback_data=f"multi_roll_{game.game_id}"
        )]]
        
        await self._bot_send_message_with_retry(
            context.bot,
            game.chat_id,
            scoreboard + f"\n\n🎯 Ход @{first_player.username}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def handle_knb_choice(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str, choice: str):
        """Обработка выбора игрока (rock/paper/scissors)"""
        if game_id not in self.active_knb_games:
            await query.answer("❌ Игра не найдена!")
            return
        
        game = self.active_knb_games[game_id]
        
        if game.status != "playing":
            await query.answer("❌ Игра не активна!")
            return
        
        user_id = query.from_user.id
        
        # Проверяем, что игрок - участник игры
        if user_id != game.challenger.id and user_id != game.target_user.id:
            await query.answer("❌ Вы не участник этой игры!")
            return
        
        # Проверяем, что игрок еще не сделал выбор
        if user_id == game.challenger.id and game.challenger_player.current_choice:
            await query.answer("❌ Вы уже сделали выбор!")
            return
        if user_id == game.target_user.id and game.target_player.current_choice:
            await query.answer("❌ Вы уже сделали выбор!")
            return
        
        # Сохраняем выбор
        game.set_choice(user_id, choice)
        
        # Подтверждаем выбор игроку
        choice_emoji = game.EMOJI[choice]
        choice_name = game.NAMES[choice]
        await query.answer(f"✅ Вы выбрали: {choice_emoji} {choice_name}")
        
        # Обновляем сообщение в ЛС
        await query.edit_message_text(
            f"✅ Вы выбрали: {choice_emoji} <b>{choice_name}</b>\n\n"
            f"Ожидание выбора соперника...",
            parse_mode=ParseMode.HTML
        )
        
        # Если оба игрока сделали выбор - обрабатываем раунд
        if game.has_both_choices():
            await self._process_knb_round(game, context)
        
    async def _process_knb_round(self, game: RockPaperScissorsGame, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает раунд КНБ и определяет победителя"""
        round_result = game.process_round()
        
        # Формируем текст результата раунда
        result_text = game.format_round_result(round_result)
        
        # Отправляем результат раунда в чат
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=result_text,
            parse_mode=ParseMode.HTML
        )
        
        # Проверяем, закончена ли игра
        if game.is_game_finished():
            winner = game.get_game_winner()
            loser = game.get_loser()
            
            # Начисляем выигрыш
            total_pot = game.bet_amount * 2
            commission = total_pot * COMMISSION_RATE
            payout = total_pot - commission
            
            check_link = await self._ensure_payout_check(
                game_id=game.game_id,
                game_type="knb",
                winner_id=winner.id,
                amount=payout,
                check_game_ref=f"КНБ {game.game_id[:6]}",
            )
            
            # Формируем итоговое сообщение в формате, как в других играх
            result_text = (
                f"🏁 <b>Итоги КНБ</b>\n\n"
                f"@{game.challenger.username}: <b>{game.challenger_player.wins}</b> побед\n"
                f"@{game.target_username}: <b>{game.target_player.wins}</b> побед\n\n"
            )
            
            result_text += f"🏆 Победитель: @{winner.username}\n\n"
            result_text += f"💰 К выплате: {payout:.2f} USDT\n"
            
            if check_link:
                result_text += f"🔗 Чек для победителя: {check_link}"
            else:
                result_text += "❌ Ошибка при выплате. Обратитесь к администратору."
            
            await context.bot.send_message(
                chat_id=game.chat_id,
                text=result_text,
                parse_mode=ParseMode.HTML
            )
            if check_link:
                await self._mark_payout_notified_completed(game.game_id)
            
            # Обновляем профили
            winner_profile = self.profile_manager.get_profile(winner.id, winner.username)
            winner_profile.add_game_result(won=True, wager=game.bet_amount, payout=payout)
            
            loser_profile = self.profile_manager.get_profile(loser.id, loser.username)
            loser_profile.add_game_result(won=False, wager=game.bet_amount, payout=0)
            
            self.profile_manager.save_profiles()
            
            # Удаляем игру
            game.status = "finished"
            del self.active_knb_games[game.game_id]
            await self._upsert_state_event("knb_finished")
        else:
            # Игра продолжается - отправляем меню выбора для следующего раунда
            await self._send_knb_choice_menu(game, context)
    
    async def _send_knb_choice_menu(self, game: RockPaperScissorsGame, context: ContextTypes.DEFAULT_TYPE):
        """Отправляет меню выбора обоим игрокам"""
        keyboard = [
            [
                InlineKeyboardButton("🗿 Камень", callback_data=f"knb_choice_{game.game_id}_rock"),
                InlineKeyboardButton("📄 Бумага", callback_data=f"knb_choice_{game.game_id}_paper")
            ],
            [
                InlineKeyboardButton("✂️ Ножницы", callback_data=f"knb_choice_{game.game_id}_scissors")
            ]
        ]
        
        menu_text = (
            f"🗿📄✂️ <b>Раунд {game.current_round}</b>\n\n"
            f"{game.get_score_text()}\n\n"
            f"Сделайте свой выбор:"
        )
        
        # Отправляем меню обоим игрокам
        try:
            await context.bot.send_message(
                chat_id=game.challenger.id,
                text=menu_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Ошибка отправки меню challenger: {e}")
        
        try:
            await context.bot.send_message(
                chat_id=game.target_user.id,
                text=menu_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Ошибка отправки меню target: {e}")
        
    async def handle_dice_roll(self, query, game_id: str):
        """Обработка броска кубика - бот сам бросает и считает результат"""
        if game_id not in self.active_games:
            await query.answer("❌ Игра не найдена!")
            return
            
        game = self.active_games[game_id]
        
        if game.status != "playing":
            await query.answer("❌ Игра не активна!")
            return
            
        user_id = query.from_user.id
        if self._is_dice_press_too_fast(user_id):
            await query.answer("⏳ Не спешите! Подождите чуть-чуть.", show_alert=False)
            return
        
        if getattr(game, "roll_in_progress", False):
            await query.answer("⏳ Подождите, бросок уже выполняется!")
            return
        
        game.roll_in_progress = True
        try:
            # Проверяем, что бросает участник игры
            if (
                query.from_user.id != game.challenger.id
                and query.from_user.id != game.target_user.id
            ):
                await query.answer("❌ Только участники игры могут бросать кубик!")
                return

            # Проверяем, что бросает правильный игрок
            current_player = game.get_current_player()
            if query.from_user.id != current_player.id:
                await query.answer("❌ Не ваш ход!")
                return

            logger.info(f"Игрок {current_player.username} делает бросок {game.current_roll}/{game.dice_count}")

            # Обновляем время последнего действия
            game.last_action_time = datetime.now()

            # Удаляем кнопку броска
            await self._edit_query_message(
                query,
                f"🎲 @{current_player.username} бросает кубик..."
            )

            # Отправляем кубик с обработкой flood control
            dice_message = await self.send_dice_with_retry(query.message.chat)

            if dice_message is None:
                return

            # Получаем значение кубика сразу из ответа
            dice_value = dice_message.dice.value

            logger.info(f"Отправлен кубик, значение={dice_value}")

            # Сразу обрабатываем результат
            await self.process_dice_result(game, dice_value, dice_message)
        finally:
            game.roll_in_progress = False
        
    async def process_dice_result(self, game, dice_value: int, message):
        """Обрабатывает результат броска кубика"""
        logger.info(f"=== ОБРАБОТКА РЕЗУЛЬТАТА КУБИКА ===")
        logger.info(f"Значение кубика: {dice_value}")
        
        # Определяем текущего игрока ПО количеству бросков
        challenger_count = len(game.challenger_rolls)
        target_count = len(game.target_rolls)
        
        logger.info(f"ДО броска: challenger={challenger_count} бросков, target={target_count} бросков")
        
        # Сначала challenger делает dice_count бросков, потом target делает dice_count бросков
        if challenger_count < game.dice_count:
            # Challenger делает свои броски
            game.challenger_rolls.append(dice_value)
            game.challenger_score += dice_value
            
            if len(game.challenger_rolls) == game.dice_count:
                # Challenger закончил свои броски, переходим к target
                game.current_player = "target"
                game.current_roll = 1  # Сбрасываем счетчик для target
            else:
                # Challenger продолжает бросать
                game.current_player = "challenger"
                game.current_roll = len(game.challenger_rolls) + 1
                
            logger.info(f"Challenger: +{dice_value}, сумма={game.challenger_score}, броски={game.challenger_rolls}")
        else:
            # Target делает свои броски
            game.target_rolls.append(dice_value)
            game.target_score += dice_value
            game.current_player = "target"
            game.current_roll = len(game.target_rolls) + 1
            logger.info(f"Target: +{dice_value}, сумма={game.target_score}, броски={game.target_rolls}")
        
        # Показываем табло
        scoreboard_text = self.message_formatter.format_scoreboard(game)
        
        logger.info(f"Игра завершена: {game.is_game_finished()}, current_roll={game.current_roll}")
        
        # Проверяем, завершена ли игра
        if game.is_game_finished():
            # Игра завершена
            winner = game.get_winner()
            
            if winner is None:
                # НИЧЬЯ - Переигровка!
                rematch_text = (
                    f"🤝 <b>Ничья!</b>\n\n"
                    f"@{game.challenger.username}: {game.challenger_score}\n"
                    f"@{game.target_username}: {game.target_score}\n\n"
                    f"🔄 <b>Переигровка!</b> Ставки остаются прежними.\n"
                    f"Игра продолжается..."
                )
                await self._send_message_with_retry(
                    message.chat,
                    rematch_text,
                    parse_mode=ParseMode.HTML,
                )
                
                # Сбрасываем игру для переигровки
                game.reset_for_rematch()
                game.last_action_time = datetime.now()  # Сбрасываем таймер для переигровки
                
                # Отправляем кнопку для первого броска
                keyboard = [
                    [InlineKeyboardButton(
                        f"🎲 Бросок 1/{game.dice_count}",
                        callback_data=f"roll_{game.game_id}"
                    )]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                scoreboard_rematch = self.message_formatter.format_scoreboard(game)
                await self._send_message_with_retry(
                    message.chat,
                    scoreboard_rematch,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
            else:
                # Есть победитель
                payout_amount = game.calculate_payout(COMMISSION_RATE)
                
                logger.info(f"Создаем payout для победителя {winner.username} (ID: {winner.id}), сумма: {payout_amount}")
                check_link = await self._ensure_payout_check(
                    game_id=game.game_id,
                    game_type="duel",
                    winner_id=winner.id,
                    amount=payout_amount,
                    check_game_ref=game.game_id,
                )
                if check_link:
                    logger.info(f"УСПЕХ: payout check получен: {check_link}")
                else:
                    logger.error("ОШИБКА: Не удалось создать payout check для победителя!")
                
                # Формируем итоговое сообщение С чеком
                result_text = self.message_formatter.format_game_result(
                    game, winner, payout_amount, check_link
                )
                
                # Отправляем результаты
                await self._send_message_with_retry(
                    message.chat,
                    result_text,
                    parse_mode=ParseMode.HTML,
                )
                if check_link:
                    await self._mark_payout_notified_completed(game.game_id)
                
                # Обновление профилей
                winner_profile = self.profile_manager.get_profile(winner.id, winner.username)
                winner_profile.add_game_result(True, game.bet_amount, payout_amount)
                
                loser = game.target_user if winner.id == game.challenger.id else game.challenger
                loser_profile = self.profile_manager.get_profile(loser.id, loser.username)
                loser_profile.add_game_result(False, game.bet_amount, 0)
                
                self.profile_manager.save_profiles()
                
                del self.active_games[game.game_id]
                await self._upsert_state_event("duel_finished")
        else:
            # Игра продолжается - показываем кнопку для следующего броска
            keyboard = [
                [InlineKeyboardButton(
                    f"🎲 Бросок {game.current_roll}/{game.dice_count}",
                    callback_data=f"roll_{game.game_id}"
                )]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await self._send_message_with_retry(
                message.chat,
                scoreboard_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
            

    async def handle_multi_dice_roll(self, query, game_id: str):
        """Обработка броска кубика в мультиигре"""
        if game_id not in self.active_multi_games:
            await query.answer("❌ Игра не найдена!")
            return

        game = self.active_multi_games[game_id]

        if game.status != "playing":
            await query.answer("❌ Игра не активна!")
            return

        user_id = query.from_user.id
        if self._is_dice_press_too_fast(user_id):
            await query.answer("⏳ Не спешите! Подождите чуть-чуть.", show_alert=False)
            return

        if getattr(game, "roll_in_progress", False):
            await query.answer("⏳ Подождите, бросок уже выполняется!")
            return

        game.roll_in_progress = True
        try:
            current_player = game.get_current_player()
            if query.from_user.id != current_player.id:
                await query.answer("❌ Не ваш ход!")
                return

            logger.info(f"Мультиигра: игрок {current_player.username} бросает кубик")

            # Обновляем время последнего действия
            game.last_action_time = datetime.now()

            dice_message = await self.send_dice_with_retry(query.message.chat)
            if dice_message is None:
                return

            dice_value = dice_message.dice.value
            game.add_roll(current_player.id, dice_value)
            logger.info(
                f"Мультиигра: {current_player.username} бросил {dice_value}, сумма={game.players_scores[current_player.id]}"
            )

            game.next_player()

            if game.is_game_finished():
                winners = game.get_winners()

                if len(winners) > 1:
                    winners_names = ", ".join([f"@{w.username}" for w in winners])
                    max_score = game.players_scores[winners[0].id]
                    rematch_text = (
                        f"🤝 <b>Ничья между несколькими игроками!</b>\n\n"
                        f"Победители с {max_score} очками:\n{winners_names}\n\n"
                        f"🔄 <b>Переигровка!</b> Ставки остаются прежними."
                    )
                    await self._send_message_with_retry(
                        dice_message.chat,
                        rematch_text,
                        parse_mode=ParseMode.HTML,
                    )

                    game.reset_for_rematch(winners)

                    keyboard = [
                        [InlineKeyboardButton(
                            f"🎲 Бросок 1/{game.dice_count}",
                            callback_data=f"multi_roll_{game.game_id}"
                        )]
                    ]
                    scoreboard_text = self.message_formatter.format_multi_scoreboard(game)
                    await self._send_message_with_retry(
                        dice_message.chat,
                        scoreboard_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    winner = winners[0]
                    payout_amount = game.calculate_payout(COMMISSION_RATE)
                    check_link = await self._ensure_payout_check(
                        game_id=game.game_id,
                        game_type="multi",
                        winner_id=winner.id,
                        amount=payout_amount,
                        check_game_ref=game.game_id,
                    )

                    result_lines = []
                    for player in game.players:
                        rolls_display = " + ".join(str(r) for r in game.players_rolls[player.id])
                        result_lines.append(
                            f"@{player.username}: {rolls_display} = <b>{game.players_scores[player.id]}</b>"
                        )

                    result_text = (
                        f"🏁 <b>Итоги Мульти-куба</b>\n\n"
                        + "\n".join(result_lines)
                        + "\n\n"
                        f"🏆 Победитель: @{winner.username}\n\n"
                        f"💰 К выплате: {payout_amount} USDT\n"
                    )
                    if check_link:
                        result_text += f"🔗 Чек для победителя: {check_link}"

                    await self._send_message_with_retry(
                        dice_message.chat,
                        result_text,
                        parse_mode=ParseMode.HTML,
                    )
                    if check_link:
                        await self._mark_payout_notified_completed(game.game_id)

                    for player in game.players:
                        profile = self.profile_manager.get_profile(player.id, player.username)
                        if player.id == winner.id:
                            profile.add_game_result(True, game.bet_amount, payout_amount)
                        else:
                            profile.add_game_result(False, game.bet_amount, 0)
                    self.profile_manager.save_profiles()

                    del self.active_multi_games[game.game_id]
                    await self._upsert_state_event("multi_finished")
            else:
                next_player = game.get_current_player()
                current_rolls = len(game.players_rolls[next_player.id])
                keyboard = [
                    [InlineKeyboardButton(
                        f"🎲 Бросок {current_rolls + 1}/{game.dice_count}",
                        callback_data=f"multi_roll_{game.game_id}"
                    )]
                ]
                scoreboard_text = self.message_formatter.format_multi_scoreboard(game)
                await self._send_message_with_retry(
                    dice_message.chat,
                    scoreboard_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML,
                )
        finally:
            game.roll_in_progress = False

    async def handle_blackjack_hit(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str):
        if game_id not in self.active_blackjack_games:
            await query.answer("❌ Игра не найдена!")
            return
        game = self.active_blackjack_games[game_id]
        if game.status != "playing":
            await query.answer("❌ Игра не активна!")
            return
        state = game.get_player_state(query.from_user.id)
        if not state:
            await query.answer("❌ Вы не участвуете в этой игре!")
            return
        if state.standing:
            await query.answer("❌ Вы уже остановились!")
            return
        current_key = game.get_current_player_key()
        if game.players[current_key].user.id != query.from_user.id:
            await query.answer("❌ Не ваш ход!")
            return
        if game.card_in_progress:
            await query.answer("⏳ Подождите завершения предыдущего действия!")
            return
        game.card_in_progress = True
        try:
            await query.message.edit_reply_markup(reply_markup=None)
            card = game.draw_card()
            
            # Если вытянут туз, нужно выбрать значение
            if card[0] == "A":
                # Добавляем туз без значения (None)
                state.add_card(card, chosen_value=None)
                # Показываем карту с кнопками выбора значения
                ace_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("1 очко", callback_data=f"blackjack_ace_{game_id}_1")],
                    [InlineKeyboardButton("11 очков", callback_data=f"blackjack_ace_{game_id}_11")]
                ])
                card_message = await self.send_blackjack_card(
                    context.bot,
                    state.user.id,
                    state.user.username,
                    card,
                    state.score,
                    reply_markup=ace_keyboard
                )
                await query.answer("Выберите значение туза!", show_alert=False)
                return
            
            # Для обычных карт добавляем сразу
            state.add_card(card)
            # determine keyboard for next action
            next_keyboard = self.build_blackjack_keyboard(game)
            card_message = await self.send_blackjack_card(
                context.bot,
                state.user.id,
                state.user.username,
                card,
                state.score,
                reply_markup=next_keyboard
            )
            await query.answer("Карта отправлена в ЛС!", show_alert=False)
            
            if state.score > 21:
                other = game.players["target"] if state.user.id == game.challenger.id else game.players["challenger"]
                if card_message:
                    await card_message.edit_reply_markup(reply_markup=None)
                await self._edit_query_message(
                    query,
                    "💥 Вы перебрали! Ход передан сопернику."
                )
                await context.bot.send_message(
                    game.chat_id,
                    f"💥 @{state.user.username} перебрал ({state.score} очков)!",
                    parse_mode=ParseMode.HTML
                )
                await self.finish_blackjack_game(context, game, winner=other)
                return
            
            if state.score == 21:
                state.standing = True
                if card_message:
                    await card_message.edit_reply_markup(reply_markup=None)
                await self._edit_query_message(
                    query,
                    "🎯 Вы набрали 21 очко! Ход передан сопернику."
                )
                await context.bot.send_message(
                    game.chat_id,
                    f"🎯 @{state.user.username} набрал 21 очко!",
                    parse_mode=ParseMode.HTML
                )
                game.switch_turn()
                await self.check_blackjack_round(context, game)
                return
            
            if card_message and state.score < 21:
                await card_message.edit_caption(
                    caption=(
                        f"🃏 @{state.user.username} вытянул карту {card[0]}{card[1]}.\n"
                        f"Текущее количество очков: <b>{state.score}</b>.\n\n"
                        f"Выберите следующее действие:"
                    ),
                    reply_markup=next_keyboard,
                    parse_mode=ParseMode.HTML
                )
        finally:
            game.card_in_progress = False

    async def handle_blackjack_stand(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str):
        if game_id not in self.active_blackjack_games:
            await query.answer("❌ Игра не найдена!")
            return
        game = self.active_blackjack_games[game_id]
        if game.status != "playing":
            await query.answer("❌ Игра не активна!")
            return
        state = game.get_player_state(query.from_user.id)
        if not state:
            await query.answer("❌ Вы не участвуете в этой игре!")
            return
        current_key = game.get_current_player_key()
        if game.players[current_key].user.id != query.from_user.id:
            await query.answer("❌ Не ваш ход!")
            return
        state.standing = True
        game.switch_turn()
        await query.answer("Ход передан сопернику", show_alert=False)
        # Просто удаляем кнопки с сообщения, не редактируя текст/caption
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning(f"Не удалось удалить кнопки: {e}")
        await context.bot.send_message(
            game.chat_id,
            f"🛑 @{state.user.username} закончил ход.",
            parse_mode=ParseMode.HTML
        )
        await self.check_blackjack_round(context, game)

    async def handle_blackjack_ace_choice(self, query, context: ContextTypes.DEFAULT_TYPE, game_id: str, ace_value: int):
        """Обработка выбора значения туза (1 или 11)"""
        if game_id not in self.active_blackjack_games:
            await query.answer("❌ Игра не найдена!")
            return
        game = self.active_blackjack_games[game_id]
        if game.status != "playing":
            await query.answer("❌ Игра не активна!")
            return
        state = game.get_player_state(query.from_user.id)
        if not state:
            await query.answer("❌ Вы не участвуете в этой игре!")
            return
        current_key = game.get_current_player_key()
        if game.players[current_key].user.id != query.from_user.id:
            await query.answer("❌ Не ваш ход!")
            return
        
        # Устанавливаем выбранное значение для последнего туза
        ace_index = state.get_last_ace_index()
        if ace_index is None:
            await query.answer("❌ Ошибка: туз не найден!")
            return
        
        state.set_ace_value(ace_index, ace_value)
        await query.answer(f"Туз установлен как {ace_value} очко!", show_alert=False)
        
        # Обновляем сообщение с картой
        card = state.cards[ace_index]
        next_keyboard = self.build_blackjack_keyboard(game)
        
        try:
            if query.message.photo:
                await query.message.edit_caption(
                    caption=(
                        f"🃏 @{state.user.username} вытянул карту {card[0]}{card[1]} ({ace_value} очков).\n"
                        f"Текущее количество очков: <b>{state.score}</b>.\n\n"
                        f"Выберите следующее действие:"
                    ),
                    reply_markup=next_keyboard,
                    parse_mode=ParseMode.HTML
                )
            else:
                await query.message.edit_text(
                    f"🃏 @{state.user.username} вытянул карту {card[0]}{card[1]} ({ace_value} очков).\n"
                    f"Текущее количество очков: <b>{state.score}</b>.\n\n"
                    f"Выберите следующее действие:",
                    reply_markup=next_keyboard,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение с тузом: {e}")
        
        # Проверяем условия после установки значения туза
        if state.score > 21:
            other = game.players["target"] if state.user.id == game.challenger.id else game.players["challenger"]
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except:
                pass
            await self._edit_query_message(
                query,
                "💥 Вы перебрали! Ход передан сопернику."
            )
            await context.bot.send_message(
                game.chat_id,
                f"💥 @{state.user.username} перебрал ({state.score} очков)!",
                parse_mode=ParseMode.HTML
            )
            await self.finish_blackjack_game(context, game, winner=other)
            return
        
        if state.score == 21:
            state.standing = True
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except:
                pass
            await self._edit_query_message(
                query,
                "🎯 Вы набрали 21 очко! Ход передан сопернику."
            )
            await context.bot.send_message(
                game.chat_id,
                f"🎯 @{state.user.username} набрал 21 очко!",
                parse_mode=ParseMode.HTML
            )
            game.switch_turn()
            await self.check_blackjack_round(context, game)
            return

    async def check_blackjack_round(self, context: ContextTypes.DEFAULT_TYPE, game: BlackjackGame):
        players = [game.players["challenger"], game.players["target"]]
        if not all(players):
            return
        for player in players:
            if player.is_bust():
                other = players[0] if players[1] == player else players[1]
                await context.bot.send_message(
                    game.chat_id,
                    f"💥 @{player.user.username} перебрал ({player.score} очков)!",
                    parse_mode=ParseMode.HTML
                )
                await self.finish_blackjack_game(context, game, winner=other)
                return
        if game.all_standing():
            winner = game.get_winner()
            await self.finish_blackjack_game(context, game, winner=winner)
            return
        current_player = game.players[game.get_current_player_key()]
        # В чате не показываем детали, только уведомление о ходе
        await context.bot.send_message(
            game.chat_id,
            f"🎯 Ход @{current_player.user.username}",
            parse_mode=ParseMode.HTML
        )
        await self.send_blackjack_prompt(context, game, current_player)
    
    async def start_blackjack_game(self, context: ContextTypes.DEFAULT_TYPE, game: BlackjackGame):
        game.status = "playing"
        await self._upsert_state_event("blackjack_started")
        start_player = game.players[game.get_current_player_key()]
        # В чате не показываем детали, только уведомление о начале
        await context.bot.send_message(
            game.chat_id,
            f"🃏 <b>Blackjack начался!</b>\n\n🎯 Первый ход делает @{start_player.user.username}",
            parse_mode=ParseMode.HTML
        )
        await self.send_blackjack_prompt(context, game, start_player)

    async def finish_blackjack_game(self, context: ContextTypes.DEFAULT_TYPE, game: BlackjackGame, winner: Optional[BlackjackPlayer]):
        challenger = game.players["challenger"]
        target = game.players["target"]
        # В финале показываем все карты и очки
        result_text = self.format_blackjack_scoreboard(game, reveal_all=True) + "\n\n"
        
        if winner is None:
            result_text += (
                "🤝 <b>Ничья!</b>\n"
                "🔄 <b>Переигровка</b> — ставки остаются прежними."
            )
            await context.bot.send_message(game.chat_id, result_text, parse_mode=ParseMode.HTML)
            game.reset_for_rematch()
            await self.start_blackjack_game(context, game)
            return
        
        payout_amount = game.calculate_payout(COMMISSION_RATE)
        check_link = await self._ensure_payout_check(
            game_id=game.game_id,
            game_type="blackjack",
            winner_id=winner.user.id,
            amount=payout_amount,
            check_game_ref=f"blackjack_{game.game_id}",
        )
        
        result_text += f"🏆 Победитель: @{winner.user.username}\n"
        result_text += f"💰 Выплата: {payout_amount} USDT\n"
        if check_link:
            result_text += f"🔗 Чек: {check_link}"
        
        await context.bot.send_message(game.chat_id, result_text, parse_mode=ParseMode.HTML)
        if check_link:
            await self._mark_payout_notified_completed(game.game_id)
        
        if check_link:
            loser = target if winner.user.id == challenger.user.id else challenger
            winner_profile = self.profile_manager.get_profile(winner.user.id, winner.user.username)
            winner_profile.add_game_result(True, game.bet_amount, payout_amount)
            loser_profile = self.profile_manager.get_profile(loser.user.id, loser.user.username)
            loser_profile.add_game_result(False, game.bet_amount, 0)
            self.profile_manager.save_profiles()
        
        del self.active_blackjack_games[game.game_id]
        await self._upsert_state_event("blackjack_finished")
    
    async def check_payments(self, context: ContextTypes.DEFAULT_TYPE):
        """Периодическая проверка таймаутов оплаты (подтверждение оплаты приходит через webhook CryptoBot)."""
        for game_id, game in list(self.active_games.items()):
            if game.status == "payment_pending":
                # Проверяем истечение времени (5 минут с момента начала оплаты)
                if game.payment_start_time:
                    time_elapsed = datetime.now() - game.payment_start_time
                else:
                    time_elapsed = timedelta(0)  # Если payment_start_time не установлено, считаем что время не истекло
                
                if time_elapsed > timedelta(minutes=5) and game.payment_start_time:
                    # Время истекло - возвращаем ставки тем, кто оплатил
                    logger.info(f"Игра {game_id}: истекло время оплаты (прошло {time_elapsed.seconds} секунд)")
                    refund_messages = []
                    
                    if game.challenger_paid:
                        success, check_link, check_data = await self.escrow_manager.refund_stake(
                            game.challenger.id,
                            game.bet_amount
                        )
                        if success and check_data:
                            # Сохраняем чек в хранилище
                            self.check_manager.add_check(check_data)
                        if success:
                            # Отправляем чек в ЛС игроку
                            try:
                                await context.bot.send_message(
                                    game.challenger.id,
                                    f"💰 <b>Возврат ставки</b>\n\n"
                                    f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                                    f"Чек для получения:\n{check_link}",
                                    parse_mode=ParseMode.HTML
                                )
                                refund_messages.append(f"✅ Чек отправлен @{game.challenger.username} в ЛС")
                            except Exception as e:
                                logger.error(f"Ошибка отправки чека в ЛС: {e}")
                                refund_messages.append(f"❌ Ошибка отправки чека @{game.challenger.username}")
                        else:
                            refund_messages.append(f"❌ Ошибка возврата ставки @{game.challenger.username}")
                    
                    if game.target_paid and game.target_user:
                        success, check_link, check_data = await self.escrow_manager.refund_stake(
                            game.target_user.id,
                            game.bet_amount
                        )
                        if success and check_data:
                            # Сохраняем чек в хранилище
                            self.check_manager.add_check(check_data)
                        if success:
                            # Отправляем чек в ЛС игроку
                            try:
                                await context.bot.send_message(
                                    game.target_user.id,
                                    f"💰 <b>Возврат ставки</b>\n\n"
                                    f"Ваша ставка {game.bet_amount} USDT возвращена (с комиссией 3%).\n\n"
                                    f"Чек для получения:\n{check_link}",
                                    parse_mode=ParseMode.HTML
                                )
                                refund_messages.append(f"✅ Чек отправлен @{game.target_username} в ЛС")
                            except Exception as e:
                                logger.error(f"Ошибка отправки чека в ЛС: {e}")
                                refund_messages.append(f"❌ Ошибка отправки чека @{game.target_username}")
                        else:
                            refund_messages.append(f"❌ Ошибка возврата ставки @{game.target_username}")
                    
                    game.status = "expired"
                    
                    expire_text = "⏰ <b>Время оплаты истекло. Игра отменена.</b>"
                    if refund_messages:
                        expire_text += "\n\n" + "\n".join(refund_messages)
                    
                    await context.bot.send_message(
                        game.chat_id,
                        expire_text,
                        parse_mode=ParseMode.HTML
                    )
                    del self.active_games[game_id]
                    await self._upsert_state_event("duel_payment_expired")
        
        for game_id, game in list(self.active_blackjack_games.items()):
            if game.status == "payment_pending":
                if game.payment_start_time:
                    elapsed = datetime.now() - game.payment_start_time
                    if elapsed > timedelta(minutes=5):
                        await context.bot.send_message(
                            game.chat_id,
                            "⏰ Оплата не поступила вовремя. Игра отменена.",
                            parse_mode=ParseMode.HTML
                        )
                        if game.challenger_paid:
                            success, check_link, check_data = await self.escrow_manager.refund_stake(game.challenger.id, game.bet_amount)
                            if success and check_data:
                                self.check_manager.add_check(check_data)
                        if game.target_paid and game.target_user:
                            success, check_link, check_data = await self.escrow_manager.refund_stake(game.target_user.id, game.bet_amount)
                            if success and check_data:
                                self.check_manager.add_check(check_data)
                        del self.active_blackjack_games[game_id]
                        await self._upsert_state_event("blackjack_payment_expired")
        
        # Проверка таймаутов для КНБ
        for game_id, game in list(self.active_knb_games.items()):
            if game.status == "payment_pending":
                if game.payment_start_time:
                    elapsed = datetime.now() - game.payment_start_time
                    if elapsed > timedelta(minutes=5):
                        await context.bot.send_message(
                            game.chat_id,
                            "⏰ Оплата не поступила вовремя. Игра отменена.",
                            parse_mode=ParseMode.HTML
                        )
                        if game.challenger_paid:
                            success, check_link, check_data = await self.escrow_manager.refund_stake(game.challenger.id, game.bet_amount)
                            if success and check_data:
                                self.check_manager.add_check(check_data)
                        if game.target_paid and game.target_user:
                            success, check_link, check_data = await self.escrow_manager.refund_stake(game.target_user.id, game.bet_amount)
                            if success and check_data:
                                self.check_manager.add_check(check_data)
                        del self.active_knb_games[game_id]
                        await self._upsert_state_event("knb_payment_expired")
        
        # Проверка таймаутов для мультиигр
        for game_id, game in list(self.active_multi_games.items()):
            if game.status == "payment_pending":
                # Проверка таймаута (5 минут с момента начала оплаты)
                if game.payment_start_time:
                    time_elapsed = datetime.now() - game.payment_start_time
                else:
                    time_elapsed = timedelta(0)  # Если payment_start_time не установлено, считаем что время не истекло
                    
                if time_elapsed > timedelta(minutes=5) and game.payment_start_time:
                    # Возврат оплатившим
                    logger.info(f"Мультиигра {game_id}: истекло время оплаты (прошло {time_elapsed.seconds} секунд)")
                    for player in game.players:
                        if game.players_paid[player.id]:
                            success, check_link, check_data = await self.escrow_manager.refund_stake(
                                player.id, game.bet_amount
                            )
                            if success and check_data:
                                # Сохраняем чек в хранилище
                                self.check_manager.add_check(check_data)
                            if success:
                                try:
                                    await context.bot.send_message(
                                        player.id,
                                        f"💰 <b>Возврат ставки</b>\n\n"
                                        f"Мультиигра отменена (таймаут).\n"
                                        f"Чек для получения:\n{check_link}",
                                        parse_mode=ParseMode.HTML
                                    )
                                except:
                                    pass
                    
                    await context.bot.send_message(
                        game.chat_id,
                        "⏰ Время оплаты истекло. Мультиигра отменена.",
                        parse_mode=ParseMode.HTML
                    )
                    del self.active_multi_games[game_id]
                    await self._upsert_state_event("multi_payment_expired")
    
    async def check_afk_players(self, context: ContextTypes.DEFAULT_TYPE):
        """Проверка AFK игроков и автоматическая доигровка"""
        # Проверяем обычные дуэли
        for game_id, game in list(self.active_games.items()):
            # 1) Автоотмена игр, которые слишком долго висят в ожидании (waiting)
            if game.status == "waiting":
                try:
                    created_at = getattr(game, "created_at", None)
                    if created_at:
                        waiting_time = datetime.now() - created_at
                        if waiting_time > timedelta(minutes=15):
                            logger.info(
                                f"Игра {game_id}: в статусе waiting более 15 минут. Игра отменена автоматически (без уведомления в чат)."
                            )
                            del self.active_games[game_id]
                            await self._upsert_state_event("duel_waiting_expired")
                            continue
                except Exception as e:
                    logger.error(f"Ошибка при автоотмене игры {game_id} в статусе waiting: {e}")
            
            # 2) Автоматическая доигровка игр в статусе playing
            if game.status == "playing" and game.last_action_time:
                time_since_action = datetime.now() - game.last_action_time
                
                # Если прошло более 5 минут с последнего действия
                if time_since_action > timedelta(minutes=5):
                    current_player = game.get_current_player()
                    logger.info(f"Игрок {current_player.username} в AFK более 5 минут. Выполняется автоматический бросок.")
                    
                    try:
                        # Уведомляем об AFK
                        await context.bot.send_message(
                            game.chat_id,
                            f"⚠️ @{current_player.username} не сделал ход более 5 минут.\n"
                            f"Выполняется автоматический бросок...",
                            parse_mode=ParseMode.HTML
                        )
                        
                        # Выполняем автоматический бросок
                        dice_message = await context.bot.send_dice(game.chat_id)
                        dice_value = dice_message.dice.value
                        
                        # Обрабатываем результат броска
                        await self.process_dice_result(game, dice_value, dice_message)
                        
                    except Exception as e:
                        logger.error(f"Ошибка при автоматической доигровке: {e}")
        
        # Проверяем Blackjack (аналогично: автоотмена waiting без сообщения, AFK-логика остается как есть в игровом процессе)
        for game_id, game in list(self.active_blackjack_games.items()):
            if game.status == "waiting":
                try:
                    created_at = getattr(game, "created_at", None)
                    if created_at:
                        waiting_time = datetime.now() - created_at
                        if waiting_time > timedelta(minutes=15):
                            logger.info(
                                f"Blackjack {game_id}: в статусе waiting более 15 минут. Игра отменена автоматически (без уведомления в чат)."
                            )
                            del self.active_blackjack_games[game_id]
                            await self._upsert_state_event("blackjack_waiting_expired")
                            continue
                except Exception as e:
                    logger.error(f"Ошибка при автоотмене Blackjack {game_id} в статусе waiting: {e}")
        
        # Проверяем КНБ (камень-ножницы-бумага)
        for game_id, game in list(self.active_knb_games.items()):
            if game.status == "waiting":
                try:
                    created_at = getattr(game, "created_at", None)
                    if created_at:
                        waiting_time = datetime.now() - created_at
                        if waiting_time > timedelta(minutes=15):
                            logger.info(
                                f"КНБ {game_id}: в статусе waiting более 15 минут. Игра отменена автоматически (без уведомления в чат)."
                            )
                            del self.active_knb_games[game_id]
                            await self._upsert_state_event("knb_waiting_expired")
                            continue
                except Exception as e:
                    logger.error(f"Ошибка при автоотмене КНБ {game_id} в статусе waiting: {e}")
        
        # Проверяем мультиигры
        for game_id, game in list(self.active_multi_games.items()):
            # 1) Автоотмена мультиигр в статусе waiting более 15 минут
            if game.status == "waiting":
                try:
                    created_at = getattr(game, "created_at", None)
                    if created_at:
                        waiting_time = datetime.now() - created_at
                        if waiting_time > timedelta(minutes=15):
                            logger.info(
                                f"Мультиигра {game_id}: в статусе waiting более 15 минут. Игра отменена автоматически (без уведомления в чат)."
                            )
                            del self.active_multi_games[game_id]
                            await self._upsert_state_event("multi_waiting_expired")
                            continue
                except Exception as e:
                    logger.error(f"Ошибка при автоотмене мультиигры {game_id} в статусе waiting: {e}")
            
            # 2) Обработка AFK в играх в статусе playing
            if game.status == "playing" and game.last_action_time:
                time_since_action = datetime.now() - game.last_action_time
                
                # Если прошло более 5 минут с последнего действия
                if time_since_action > timedelta(minutes=5):
                    current_player = game.get_current_player()
                    logger.info(f"Мультиигра: игрок {current_player.username} в AFK более 5 минут. Выполняется автоматический бросок.")
                    
                    try:
                        # Уведомляем об AFK
                        await context.bot.send_message(
                            game.chat_id,
                            f"⚠️ @{current_player.username} не сделал ход более 5 минут.\n"
                            f"Выполняется автоматический бросок...",
                            parse_mode=ParseMode.HTML
                        )
                        
                        # Выполняем автоматический бросок
                        dice_message = await context.bot.send_dice(game.chat_id)
                        dice_value = dice_message.dice.value
                        
                        # Добавляем результат
                        game.add_roll(current_player.id, dice_value)
                        game.last_action_time = datetime.now()  # Обновляем время
                        
                        logger.info(
                            f"Автобросок в мультиигре: {current_player.username} бросил {dice_value}, "
                            f"сумма={game.players_scores[current_player.id]}"
                        )
                        
                        # Переходим к следующему игроку
                        game.next_player()
                        
                        # Проверяем завершение игры
                        if game.is_game_finished():
                            winners = game.get_winners()
                            
                            if len(winners) > 1:
                                winners_names = ", ".join([f"@{w.username}" for w in winners])
                                max_score = game.players_scores[winners[0].id]
                                rematch_text = (
                                    f"🤝 <b>Ничья между несколькими игроками!</b>\n\n"
                                    f"Победители с {max_score} очками:\n{winners_names}\n\n"
                                    f"🔄 <b>Переигровка!</b>"
                                )
                                await context.bot.send_message(
                                    game.chat_id,
                                    rematch_text,
                                    parse_mode=ParseMode.HTML
                                )
                                
                                game.reset_for_rematch(winners)
                                game.last_action_time = datetime.now()
                                
                                # Показываем табло для переигровки
                                scoreboard = game.get_scoreboard_text()
                                next_player = game.get_current_player()
                                
                                keyboard = [[InlineKeyboardButton(
                                    f"🎲 Бросок 1/{game.dice_count}",
                                    callback_data=f"multi_roll_{game_id}"
                                )]]
                                
                                await context.bot.send_message(
                                    game.chat_id,
                                    scoreboard + f"\n\n🎯 Ход @{next_player.username}",
                                    reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode=ParseMode.HTML
                                )
                            else:
                                # Один победитель
                                winner = winners[0]
                                payout_amount = game.calculate_payout(COMMISSION_RATE)
                                
                                check_link = await self._ensure_payout_check(
                                    game_id=game_id,
                                    game_type="multi",
                                    winner_id=winner.id,
                                    amount=payout_amount,
                                    check_game_ref=game_id,
                                )
                                
                                result_text = (
                                    f"🏆 <b>Победитель мультиигры!</b>\n\n"
                                    f"👤 @{winner.username}\n"
                                    f"💰 Выплата: {payout_amount} USDT\n"
                                    f"📊 Счёт: {game.players_scores[winner.id]}\n"
                                )
                                
                                if check_link:
                                    result_text += f"\n🔗 Чек: {check_link}"
                                
                                await context.bot.send_message(
                                    game.chat_id,
                                    result_text,
                                    parse_mode=ParseMode.HTML
                                )
                                if check_link:
                                    await self._mark_payout_notified_completed(game_id)
                                
                                if check_link:
                                    # Обновление статистики
                                    winner_profile = self.profile_manager.get_profile(winner.id, winner.username)
                                    winner_profile.add_game_result(True, game.bet_amount, payout_amount)
                                    
                                    for player in game.players:
                                        if player.id != winner.id:
                                            loser_profile = self.profile_manager.get_profile(player.id, player.username)
                                            loser_profile.add_game_result(False, game.bet_amount, 0)
                                    
                                    self.profile_manager.save_profiles()
                                
                                del self.active_multi_games[game_id]
                                await self._upsert_state_event("multi_finished_afk")
                        else:
                            # Игра продолжается
                            next_player = game.get_current_player()
                            scoreboard = game.get_scoreboard_text()
                            
                            rolls_count = len(game.players_rolls[current_player.id])
                            keyboard = [[InlineKeyboardButton(
                                f"🎲 Бросок {rolls_count + 1}/{game.dice_count}",
                                callback_data=f"multi_roll_{game_id}"
                            )]]
                            
                            await context.bot.send_message(
                                game.chat_id,
                                scoreboard + f"\n\n🎯 Ход @{next_player.username}",
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode=ParseMode.HTML
                            )
                            
                    except Exception as e:
                        logger.error(f"Ошибка при автоматической доигровке мультиигры: {e}")
    
def main():
    """Основная функция запуска бота"""
    bot = DiceBot()
    
    # Создаем приложение с увеличенными таймаутами
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(bot.on_startup)
        .post_shutdown(bot.on_shutdown)
        .build()
    )
    
    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("refresh", bot.refresh_command))
    application.add_handler(CommandHandler("duel", bot.duel_command))
    application.add_handler(CommandHandler("blackjack", bot.blackjack_command))
    application.add_handler(CommandHandler("knb", bot.knb_command))
    application.add_handler(CommandHandler("multiduel", bot.multiduel_command))
    application.add_handler(CommandHandler("multiduelkick", bot.multiduelkick_command))
    application.add_handler(CommandHandler("invitem", bot.invitem_command))
    application.add_handler(CommandHandler("cancel", bot.cancel_command))
    application.add_handler(CommandHandler("cancelall", bot.cancelall_command))
    application.add_handler(CommandHandler("cancelr", bot.cancelr_command))
    
    # Добавляем обработчик кнопок
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    
    # Добавляем обработчик текстовых сообщений для кнопок клавиатуры (только в приватных чатах)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, bot.handle_text_message))
    
    # Добавляем периодические задачи
    try:
        job_queue = application.job_queue
        if job_queue:
            # Проверка таймаутов оплаты каждую минуту (статус оплаты приходит через webhook)
            job_queue.run_repeating(bot.check_payments, interval=60.0, first=10.0)
            logger.info("JobQueue настроен успешно - проверка таймаутов оплаты запущена")
            
            # Проверка AFK игроков каждые 30 секунд
            job_queue.run_repeating(bot.check_afk_players, interval=30.0, first=10.0)
            logger.info("JobQueue настроен успешно - проверка AFK игроков запущена")

            # Snapshot активных игр в PostgreSQL
            job_queue.run_repeating(bot.persist_runtime_state_job, interval=15.0, first=15.0)
            logger.info("JobQueue настроен успешно - snapshot активных игр запущен")
            
        else:
            logger.error("JobQueue не инициализирован - проверка платежей и AFK отключена")
    except Exception as e:
        logger.error(f"Ошибка настройки JobQueue: {e}")
    
    # Добавляем обработчик ошибок
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает ошибки бота"""
        error = context.error
        
        # Обработка таймаутов - не логируем как критическую ошибку
        if isinstance(error, TimedOut):
            logger.warning(f"Таймаут при обработке обновления: {error}")
            return
        
        # Обработка таймаутов и сетевых ошибок на уровне httpcore/httpx
        error_name = type(error).__name__
        error_str = str(error).lower()
        
        # Проверяем таймауты
        if 'Timeout' in error_name or 'timeout' in error_str:
            logger.warning(f"Таймаут при обработке обновления ({error_name}): {error}")
            return
        
        # Проверяем ошибки чтения/записи сети (ReadError, WriteError, ConnectError и т.д.)
        if HAS_HTTPX and isinstance(error, HTTPX_ERRORS):
            logger.warning(f"Сетевая ошибка при обработке обновления ({error_name}): {error}")
            return
        
        # Проверяем по имени класса (на случай, если httpx не импортирован или ошибка обернута)
        if any(keyword in error_name for keyword in ['ReadError', 'WriteError', 'ConnectError', 'NetworkError', 'ConnectionError']):
            logger.warning(f"Сетевая ошибка при обработке обновления ({error_name}): {error}")
            return
        
        # Проверяем по строковому представлению ошибки
        if any(keyword in error_str for keyword in ['read error', 'write error', 'connect error', 'network error', 'connection error']):
            logger.warning(f"Сетевая ошибка при обработке обновления ({error_name}): {error}")
            return
        
        # Обработка конфликта (несколько экземпляров бота)
        if isinstance(error, Conflict):
            logger.error(
                "⚠️ КОНФЛИКТ: Запущено несколько экземпляров бота!\n"
                "Остановите другие экземпляры или подождите 10 секунд для автоматического переподключения."
            )
            # Не прерываем работу, бот попытается переподключиться
            return
        
        # Логируем остальные ошибки
        logger.error(f"Exception while handling an update: {error}", exc_info=error)
    
    application.add_error_handler(error_handler)
    
    # Запускаем бота с настройками для обработки конфликтов
    logger.info("Запуск Dice Bot...")
    try:
        application.run_polling(
            drop_pending_updates=True,  # Пропускаем старые обновления при перезапуске
            close_loop=False
        )
    except Conflict as e:
        logger.error(f"Критический конфликт: {e}")
        logger.error("⚠️ ОШИБКА: Запущено несколько экземпляров бота!")
        logger.error("Остановите другие экземпляры бота перед запуском!")
        raise

if __name__ == '__main__':
    main()
