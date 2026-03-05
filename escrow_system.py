import asyncio
import aiohttp
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from config import CRYPTOBOT_TOKEN, logger

class EscrowManager:
    def __init__(self):
        self.cryptobot_token = CRYPTOBOT_TOKEN
        self.base_url = "https://pay.crypt.bot/api"
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def get_session(self) -> aiohttp.ClientSession:
        """Получает или создает HTTP сессию"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
        
    async def close_session(self):
        """Закрывает HTTP сессию"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def set_webhook(self, webhook_url: str) -> bool:
        """Регистрирует webhook URL в CryptoBot API."""
        try:
            session = await self.get_session()
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token,
                "Content-Type": "application/json",
            }
            async with session.post(
                f"{self.base_url}/setWebhook",
                json={"url": webhook_url},
                headers=headers,
            ) as response:
                response_text = await response.text()
                if response.status == 200:
                    data = json.loads(response_text)
                    if data.get("ok"):
                        logger.info("Webhook CryptoBot успешно установлен: %s", webhook_url)
                        return True
                    logger.error("Ошибка установки webhook: %s", data)
                    return False
                logger.error("HTTP ошибка при установке webhook: %s, body: %s", response.status, response_text)
                return False
        except Exception as e:
            logger.error("Исключение при установке webhook: %s", e)
            return False
            
    async def create_invoice_in_bot(self, game_id: str, amount: float, description: str) -> Optional[Dict[str, Any]]:
        """Создает счет внутри бота через CryptoBot API"""
        try:
            session = await self.get_session()
            
            payload = {
                "asset": "USDT",
                "amount": str(amount),
                "description": description,
                "hidden_message": f"Оплата участия в игре {game_id}",
                "paid_btn_name": "callback",
                "paid_btn_url": f"https://t.me/your_bot?start=game_{game_id}"
            }
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token,
                "Content-Type": "application/json"
            }
            
            async with session.post(
                f"{self.base_url}/createInvoice",
                json=payload,
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        invoice = data["result"]
                        # Используем pay_url для открытия диалога с @CryptoBot
                        return {
                            "invoice_id": invoice["invoice_id"],
                            "pay_url": invoice["pay_url"],  # Открывает диалог с @CryptoBot
                            "amount": invoice["amount"],
                            "asset": invoice["asset"]
                        }
                    else:
                        logger.error(f"Ошибка создания счета: {data}")
                        return None
                else:
                    logger.error(f"HTTP ошибка при создании счета: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"Исключение при создании счета: {e}")
            return None
    
    async def create_payment(self, game_id: str, amount: float, user1_id: int, user2_id: int) -> Optional[Dict[str, Any]]:
        """Создает эскроу платеж"""
        try:
            session = await self.get_session()
            
            # Создаем счет для эскроу
            payload = {
                "amount": amount,
                "currency": "USDT",
                "description": f"Dice Game {game_id}",
                "return_url": f"https://t.me/dice_bot",
                "expires_in": 300,  # 5 минут
                "custom_data": f"game_{game_id}"
            }
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token,
                "Content-Type": "application/json"
            }
            
            async with session.post(
                f"{self.base_url}/createInvoice",
                json=payload,
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        invoice = data["result"]
                        return {
                            "payment_id": invoice["invoice_id"],
                            "payment_link": invoice["pay_url"],
                            "amount": invoice["amount"],
                            "currency": invoice["currency"]
                        }
                    else:
                        logger.error(f"Ошибка создания платежа: {data}")
                        return None
                else:
                    logger.error(f"HTTP ошибка при создании платежа: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"Исключение при создании платежа: {e}")
            return None
            
    async def check_payment_status(self, payment_id: str) -> str:
        """Проверяет статус платежа"""
        try:
            session = await self.get_session()
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token
            }
            
            async with session.get(
                f"{self.base_url}/getInvoices",
                params={"invoice_ids": payment_id},
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok") and data["result"]["items"]:
                        invoice = data["result"]["items"][0]
                        status = invoice["status"]
                        
                        if status == "paid":
                            return "paid"
                        elif status == "expired":
                            return "expired"
                        else:
                            return "pending"
                    else:
                        logger.error(f"Ошибка получения статуса платежа: {data}")
                        return "error"
                else:
                    logger.error(f"HTTP ошибка при проверке платежа: {response.status}")
                    return "error"
                    
        except Exception as e:
            logger.error(f"Исключение при проверке платежа: {e}")
            return "error"
            
    async def create_check_for_user(self, user_id: int, amount: float, game_id: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
        """Создает персональный чек для конкретного пользователя
        
        Returns:
            Optional[Dict[str, Any]]: Словарь с ключами 'check_id', 'check_link', 'amount', 'user_id', 'game_id'
            или None в случае ошибки
        """
        attempt = 0
        while attempt < max_retries:
            attempt += 1
            try:
                session = await self.get_session()
                
                payload = {
                    "asset": "USDT",
                    "amount": str(amount),
                    "pin_to_user_id": user_id,
                    "description": f"Выигрыш в игре {game_id}"
                }
                headers = {
                    "Crypto-Pay-API-Token": self.cryptobot_token,
                    "Content-Type": "application/json"
                }
                
                async with session.post(
                    f"{self.base_url}/createCheck",
                    json=payload,
                    headers=headers
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Response status: {response.status}, body: {response_text}")
                    
                    data = None
                    if response.status == 200:
                        data = json.loads(response_text)
                        if data.get("ok"):
                            check = data["result"]
                            check_id = check.get("check_id")
                            check_link = f"https://t.me/CryptoBot?start={check['bot_check_url'].split('start=')[1]}"
                            logger.info(f"Создан чек для пользователя {user_id}: check_id={check_id}, link={check_link}")
                            return {
                                "check_id": check_id,
                                "check_link": check_link,
                                "amount": amount,
                                "user_id": user_id,
                                "game_id": game_id,
                                "created_at": datetime.now().isoformat()
                            }
                        else:
                            error_name = data.get("error", {}).get("name")
                    else:
                        try:
                            data = json.loads(response_text)
                            error_name = data.get("error", {}).get("name")
                        except json.JSONDecodeError:
                            error_name = None
                        logger.error(f"HTTP ошибка при создании чека: {response.status}, body: {response_text}")
                    
                    if error_name == "NOT_ENOUGH_COINS" and attempt < max_retries:
                        wait_time = 2 * attempt
                        logger.warning(
                            "Недостаточно монет для создания чека (попытка %s/%s). Ждем %s с. и повторяем.",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if error_name:
                        logger.error(f"Ошибка создания чека: {data}")
                    return None
                    
            except Exception as e:
                logger.error(f"Исключение при создании чека (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * attempt)
                else:
                    return None
        return None
    
    async def get_checks(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Получает список чеков из CryptoBot API
        
        Args:
            status: Фильтр по статусу (active - неактивированные, activated - активированные, None для всех)
            limit: Максимальное количество чеков для получения
            
        Returns:
            Список словарей с информацией о чеках
        """
        try:
            session = await self.get_session()
            
            params = {"count": limit}
            if status:
                params["status"] = status
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token
            }
            
            async with session.get(
                f"{self.base_url}/getChecks",
                params=params,
                headers=headers
            ) as response:
                response_text = await response.text()
                logger.info(f"Response status: {response.status}, body length: {len(response_text)}")
                
                if response.status == 200:
                    data = json.loads(response_text)
                    if data.get("ok"):
                        checks = data["result"].get("items", [])
                        logger.info(f"Получено {len(checks)} чеков из CryptoBot API (статус: {status or 'all'})")
                        return checks
                    else:
                        error_info = data.get("error", {})
                        logger.error(f"Ошибка получения чеков: {error_info}")
                        return []
                else:
                    logger.error(f"HTTP ошибка при получении чеков: {response.status}")
                    return []
                    
        except Exception as e:
            logger.error(f"Исключение при получении чеков: {e}")
            return []
    
    async def delete_check(self, check_id: int) -> bool:
        """Отменяет чек через CryptoBot API
        
        Args:
            check_id: ID чека для отмены
            
        Returns:
            bool: True если чек успешно отменен, False в случае ошибки
        """
        try:
            session = await self.get_session()
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token,
                "Content-Type": "application/json"
            }
            
            async with session.post(
                f"{self.base_url}/deleteCheck",
                json={"check_id": check_id},
                headers=headers
            ) as response:
                response_text = await response.text()
                logger.info(f"Response status: {response.status}, body: {response_text}")
                
                if response.status == 200:
                    data = json.loads(response_text)
                    if data.get("ok"):
                        logger.info(f"Чек {check_id} успешно отменен")
                        return True
                    else:
                        logger.error(f"Ошибка отмены чека: {data}")
                        return False
                else:
                    logger.error(f"HTTP ошибка при отмене чека: {response.status}, body: {response_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Исключение при отмене чека: {e}")
            return False
    
    async def process_payout(self, payment_id: str, winner_id: int, amount: float) -> bool:
        """Обрабатывает выплату победителю"""
        try:
            session = await self.get_session()
            
            # Создаем выплату победителю
            payload = {
                "user_id": winner_id,
                "asset": "USDT",
                "amount": str(amount),
                "description": f"Выигрыш в Dice Game"
            }
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token,
                "Content-Type": "application/json"
            }
            
            async with session.post(
                f"{self.base_url}/transfer",
                json=payload,
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        logger.info(f"Выплата {amount} USDT успешно отправлена пользователю {winner_id}")
                        return True
                    else:
                        logger.error(f"Ошибка выплаты: {data}")
                        return False
                else:
                    logger.error(f"HTTP ошибка при выплате: {response.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"Исключение при выплате: {e}")
            return False
            
    async def get_balance(self) -> Optional[Dict[str, float]]:
        """Получает баланс бота"""
        try:
            session = await self.get_session()
            
            headers = {
                "Crypto-Pay-API-Token": self.cryptobot_token
            }
            
            async with session.get(
                f"{self.base_url}/getBalance",
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        balances = {}
                        for item in data["result"]:
                            balances[item["currency_code"]] = float(item["available_balance"])
                        return balances
                    else:
                        logger.error(f"Ошибка получения баланса: {data}")
                        return None
                else:
                    logger.error(f"HTTP ошибка при получении баланса: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"Исключение при получении баланса: {e}")
            return None
            
    async def create_test_payment(self, game_id: str, amount: float) -> Dict[str, Any]:
        """Создает тестовый платеж для локального тестирования"""
        # Для локального тестирования возвращаем моковые данные
        return {
            "payment_id": f"test_{game_id}",
            "payment_link": f"https://t.me/CryptoBot?start=test_{game_id}",
            "amount": amount,
            "currency": "USDT"
        }
        
    async def check_test_payment_status(self, payment_id: str) -> str:
        """Проверяет статус тестового платежа"""
        # Для локального тестирования всегда возвращаем "paid"
        # В реальной версии здесь будет логика проверки через CryptoBot API
        return "paid"
        
    async def process_test_payout(self, payment_id: str, winner_id: int, amount: float) -> bool:
        """Обрабатывает тестовую выплату"""
        # Для локального тестирования всегда возвращаем успех
        logger.info(f"Тестовая выплата {amount} USDT пользователю {winner_id}")
        return True
    
    async def refund_stake(self, user_id: int, amount: float, commission_rate: float = 0.03) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Возвращает ставку игроку с учетом комиссии 3% через чек
        
        Returns:
            tuple[bool, str, Optional[Dict]]: (success, check_link или error_message, check_data или None)
        """
        try:
            # Рассчитываем сумму возврата с комиссией 3%
            commission = amount * commission_rate
            refund_amount = amount - commission
            refund_amount = round(refund_amount, 2)
            
            logger.info(f"Возврат ставки пользователю {user_id}: {amount} USDT - {commission} USDT (комиссия {int(commission_rate*100)}%) = {refund_amount} USDT")
            
            # Используем создание чека вместо прямого перевода
            check_data = await self.create_check_for_user(
                user_id, 
                refund_amount, 
                f"refund_{user_id}_{int(datetime.now().timestamp())}"
            )
            
            if check_data and check_data.get("check_link"):
                logger.info(f"Чек для возврата создан: {check_data['check_link']}")
                return True, check_data["check_link"], check_data
            else:
                logger.error(f"Не удалось создать чек для возврата")
                return False, "Ошибка создания чека", None
                    
        except Exception as e:
            logger.error(f"Исключение при возврате ставки: {e}")
            return False, str(e), None
