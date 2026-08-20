#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qa_content_workflow import (  # noqa: E402
    QA_SCHEMA,
    QaRow,
    WorkflowError,
    plan_changes,
    validate_rows,
)


def qa_row(
    row_no: int,
    action: str,
    item_id: int | None,
    question: str,
    answer: str,
    version: int | None,
) -> QaRow:
    return QaRow(
        row_no=row_no,
        action=action,
        id=item_id,
        question=question,
        answer=answer,
        answer_variants="",
        category="功能介绍",
        tags="测试",
        domains="qa",
        status="approved",
        enabled="1",
        version=version,
        change_note="unit test",
    )


class QaContentWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "qa.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.executescript(QA_SCHEMA)
        self.connection.executemany(
            """
            INSERT INTO qa_item
              (id, question, answer, category, tags, domains, status, version,
               enabled, deleted, created_by, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, 'approved', 1, '1', '0', 'seed', 'seed')
            """,
            [
                (1, "问题一", "旧答案一", "功能介绍", "测试", "qa"),
                (2, "问题二", "旧答案二", "功能介绍", "测试", "qa"),
            ],
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_update_add_and_disable_are_atomic_plan(self) -> None:
        rows = [
            qa_row(5, "KEEP", 1, "问题一", "新答案一", 1),
            qa_row(6, "DISABLE", 2, "问题二", "旧答案二", 1),
            qa_row(7, "ADD", None, "问题三", "新答案三", None),
        ]
        validate_rows(rows)
        operations, counts = plan_changes(
            self.connection, rows, approval_mode="approved", actor="tester"
        )
        self.assertEqual(counts["update"], 1)
        self.assertEqual(counts["disable"], 1)
        self.assertEqual(counts["add"], 1)
        for sql, params, _ in operations:
            self.connection.execute(sql, params)
        self.connection.commit()

        updated = self.connection.execute(
            "SELECT answer, status, version FROM qa_item WHERE id=1"
        ).fetchone()
        disabled = self.connection.execute(
            "SELECT enabled, status, version FROM qa_item WHERE id=2"
        ).fetchone()
        added = self.connection.execute(
            "SELECT answer, status FROM qa_item WHERE question='问题三'"
        ).fetchone()
        self.assertEqual(tuple(updated), ("新答案一", "approved", 2))
        self.assertEqual(tuple(disabled), ("0", "disabled", 2))
        self.assertEqual(tuple(added), ("新答案三", "approved"))

    def test_version_conflict_blocks_update(self) -> None:
        row = qa_row(5, "KEEP", 1, "问题一", "新答案一", 99)
        with self.assertRaisesRegex(WorkflowError, "版本冲突"):
            plan_changes(self.connection, [row], approval_mode="pending", actor="tester")

    def test_duplicate_questions_are_rejected(self) -> None:
        rows = [
            qa_row(5, "KEEP", 1, "重复问题", "答案一", 1),
            qa_row(6, "ADD", None, "重复问题", "答案二", None),
        ]
        with self.assertRaisesRegex(WorkflowError, "完全重复"):
            validate_rows(rows)


if __name__ == "__main__":
    unittest.main()
