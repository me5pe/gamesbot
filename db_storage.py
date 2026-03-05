import os
from decimal import Decimal
from typing import Optional, Dict, Any

import asyncpg

from config import logger


class DatabaseManager:
    """PostgreSQL storage for payouts, webhook idempotency and runtime snapshots."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if not self.dsn:
            raise RuntimeError("DATABASE_URL (или POSTGRES_DSN) не задан для PostgreSQL.")

        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=int(os.getenv("POSTGRES_POOL_MAX_SIZE", "8")),
            command_timeout=15,
        )
        await self.init_schema()
        logger.info("PostgreSQL подключен и схема инициализирована.")

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def init_schema(self):
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id BIGINT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payouts (
                    game_id TEXT PRIMARY KEY,
                    game_type TEXT NOT NULL,
                    winner_id BIGINT NOT NULL,
                    amount NUMERIC(18, 8) NOT NULL,
                    status TEXT NOT NULL,
                    check_id BIGINT,
                    check_link TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    state_key TEXT PRIMARY KEY,
                    payload BYTEA NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

    async def mark_update_processed(self, update_id: int) -> bool:
        """
        Durable idempotency for webhook updates.
        Returns True if update_id is new, False if already processed.
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                """
                INSERT INTO processed_updates(update_id)
                VALUES($1)
                ON CONFLICT (update_id) DO NOTHING
                RETURNING update_id;
                """,
                update_id,
            )
            return result is not None

    async def get_or_create_payout(
        self, game_id: str, game_type: str, winner_id: int, amount: float
    ) -> Dict[str, Any]:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM payouts WHERE game_id = $1 FOR UPDATE;",
                    game_id,
                )
                if row is None:
                    await conn.execute(
                        """
                        INSERT INTO payouts(game_id, game_type, winner_id, amount, status)
                        VALUES($1, $2, $3, $4, 'pending');
                        """,
                        game_id,
                        game_type,
                        winner_id,
                        Decimal(str(amount)),
                    )
                    row = await conn.fetchrow(
                        "SELECT * FROM payouts WHERE game_id = $1;",
                        game_id,
                    )
                return dict(row)

    async def mark_payout_check_created(self, game_id: str, check_id: Optional[int], check_link: str):
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE payouts
                SET status = 'check_created',
                    check_id = $2,
                    check_link = $3,
                    updated_at = NOW()
                WHERE game_id = $1;
                """,
                game_id,
                check_id,
                check_link,
            )

    async def set_payout_status(self, game_id: str, status: str):
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE payouts
                SET status = $2,
                    updated_at = NOW()
                WHERE game_id = $1;
                """,
                game_id,
                status,
            )

    async def get_payout(self, game_id: str) -> Optional[Dict[str, Any]]:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM payouts WHERE game_id = $1;", game_id)
            return dict(row) if row else None

    async def save_runtime_snapshot(self, state_key: str, payload: bytes):
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_state(state_key, payload, updated_at)
                VALUES($1, $2, NOW())
                ON CONFLICT (state_key)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW();
                """,
                state_key,
                payload,
            )

    async def load_runtime_snapshot(self, state_key: str) -> Optional[bytes]:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool не инициализирован.")

        async with self.pool.acquire() as conn:
            payload = await conn.fetchval(
                "SELECT payload FROM runtime_state WHERE state_key = $1;",
                state_key,
            )
            return payload
