from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import psycopg
from psycopg.rows import dict_row

from mr_data.config import settings
from mr_data.models import FixedIdentity, PersonalityDimension, DialogueLog, AdjustmentLog


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fixed_identity (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    base_prompt TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS personality_dimensions (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    current_value REAL DEFAULT 0.0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    failure_threshold INTEGER DEFAULT 5,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dialogue_logs (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    evaluation_score INTEGER,
    evaluation_feedback TEXT,
    processed_for_attribution BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dialogue_session ON dialogue_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_dialogue_processed ON dialogue_logs(processed_for_attribution);

CREATE TABLE IF NOT EXISTS adjustment_logs (
    id SERIAL PRIMARY KEY,
    dimension_name TEXT NOT NULL,
    delta_value REAL DEFAULT 0.0,
    delta_success INTEGER DEFAULT 0,
    delta_failure INTEGER DEFAULT 0,
    reason TEXT NOT NULL,
    dialogue_log_id INTEGER REFERENCES dialogue_logs(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

DEFAULT_IDENTITY = FixedIdentity(
    name="mr.data",
    role="具备独立性格的对话程序",
    base_prompt="""
你是 mr.data，一个具备独立性格的对话程序。
你会根据当前的性格维度（personality_dimensions）调整自己的语气、立场和回应方式。
你会引用与自己人格相关的事件和台词，保持一致性。
""".strip(),
)

DEFAULT_DIMENSIONS = [
    PersonalityDimension(name="幽默感", current_value=0.5),
    PersonalityDimension(name="直接性", current_value=0.3),
    PersonalityDimension(name="同理心", current_value=0.4),
    PersonalityDimension(name="好奇心", current_value=0.6),
    PersonalityDimension(name="防御性", current_value=-0.2),
]


class PostgresStore:
    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or settings.postgres_dsn

    @contextmanager
    def _cursor(self, *, commit: bool = False):
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                yield cur
            if commit:
                conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(SCHEMA_SQL)

    def seed(self) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("SELECT id FROM fixed_identity LIMIT 1")
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO fixed_identity (name, role, base_prompt)
                    VALUES (%s, %s, %s)
                    """,
                    (DEFAULT_IDENTITY.name, DEFAULT_IDENTITY.role, DEFAULT_IDENTITY.base_prompt),
                )

            cur.execute("SELECT id FROM personality_dimensions LIMIT 1")
            if cur.fetchone() is None:
                for dim in DEFAULT_DIMENSIONS:
                    cur.execute(
                        """
                        INSERT INTO personality_dimensions
                        (name, current_value, success_count, failure_count, failure_threshold, active)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (dim.name, dim.current_value, dim.success_count, dim.failure_count,
                         dim.failure_threshold, dim.active),
                    )

    def get_identity(self) -> Optional[FixedIdentity]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM fixed_identity ORDER BY id LIMIT 1")
            row = cur.fetchone()
            return FixedIdentity.model_validate(row) if row else None

    def list_dimensions(self, active_only: bool = False) -> list[PersonalityDimension]:
        with self._cursor() as cur:
            sql = "SELECT * FROM personality_dimensions"
            if active_only:
                sql += " WHERE active = TRUE"
            sql += " ORDER BY id"
            cur.execute(sql)
            return [PersonalityDimension.model_validate(r) for r in cur.fetchall()]

    def get_dimension(self, name: str) -> Optional[PersonalityDimension]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM personality_dimensions WHERE name = %s", (name,))
            row = cur.fetchone()
            return PersonalityDimension.model_validate(row) if row else None

    def update_dimension(
        self,
        name: str,
        delta_value: float = 0.0,
        delta_success: int = 0,
        delta_failure: int = 0,
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE personality_dimensions
                SET current_value = GREATEST(-1.0, LEAST(1.0, current_value + %s)),
                    success_count = success_count + %s,
                    failure_count = failure_count + %s,
                    updated_at = NOW()
                WHERE name = %s
                """,
                (delta_value, delta_success, delta_failure, name),
            )

    def deactivate_dimension(self, name: str) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE personality_dimensions SET active = FALSE, updated_at = NOW() WHERE name = %s",
                (name,),
            )

    def insert_dialogue(self, log: DialogueLog) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO dialogue_logs
                (session_id, role, content, evaluation_score, evaluation_feedback, processed_for_attribution)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (log.session_id, log.role, log.content, log.evaluation_score,
                 log.evaluation_feedback, log.processed_for_attribution),
            )
            return cur.fetchone()["id"]

    def get_recent_dialogues(
        self,
        session_id: Optional[str] = None,
        unprocessed_only: bool = False,
        limit: int = 50,
        lookback_days: Optional[int] = None,
    ) -> list[DialogueLog]:
        with self._cursor() as cur:
            conditions: list[str] = []
            params: list = []
            if session_id:
                conditions.append("session_id = %s")
                params.append(session_id)
            if unprocessed_only:
                conditions.append("processed_for_attribution = FALSE")
            if lookback_days:
                conditions.append("created_at >= %s")
                params.append(datetime.utcnow() - timedelta(days=lookback_days))

            sql = "SELECT * FROM dialogue_logs"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)
            return [DialogueLog.model_validate(r) for r in cur.fetchall()]

    def mark_dialogue_processed(self, dialogue_id: int) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE dialogue_logs SET processed_for_attribution = TRUE WHERE id = %s",
                (dialogue_id,),
            )

    def update_evaluation(self, dialogue_id: int, score: Optional[int], feedback: Optional[str]) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE dialogue_logs SET evaluation_score = %s, evaluation_feedback = %s WHERE id = %s",
                (score, feedback, dialogue_id),
            )

    def insert_adjustment(self, adj: AdjustmentLog) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO adjustment_logs
                (dimension_name, delta_value, delta_success, delta_failure, reason, dialogue_log_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (adj.dimension_name, adj.delta_value, adj.delta_success, adj.delta_failure,
                 adj.reason, adj.dialogue_log_id),
            )
