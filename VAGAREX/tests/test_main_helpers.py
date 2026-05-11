"""Тесты helper-функций из main.py.

Фокус — алгоритм переиспользования номеров миссий
(`_smallest_unused_mission_id`). После удаления миссии её ID освобождается
и должен достаться следующей новой миссии, чтобы каталог не «разъезжался»
в большие числа при активной генерации/удалении.

Запуск:
    cd VAGAREX
    python -m unittest tests.test_main_helpers -v
"""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# main.py использует относительные пути ("static", "templates") при импорте,
# поэтому работаем из корня проекта.
os.chdir(ROOT)

# main.py → auth.py → passlib.context.CryptContext. В тестовом окружении
# passlib может отсутствовать — алгоритм поиска свободного ID к нему
# никак не привязан, поэтому ставим лёгкую заглушку до импорта main.
if "passlib" not in sys.modules:
    _passlib = types.ModuleType("passlib")
    _passlib_ctx = types.ModuleType("passlib.context")

    class _CryptContextStub:
        def __init__(self, *_a, **_kw): pass
        def hash(self, s):       return s
        def verify(self, s, h):  return s == h

    _passlib_ctx.CryptContext = _CryptContextStub
    sys.modules["passlib"]         = _passlib
    sys.modules["passlib.context"] = _passlib_ctx

from sqlalchemy import create_engine                         # noqa: E402
from sqlalchemy.orm import sessionmaker                       # noqa: E402

from database import Base                                     # noqa: E402
from models import Mission, User                              # noqa: E402
from main import _smallest_unused_mission_id                  # noqa: E402


def _mk_mission(id_: int, owner_id: int) -> Mission:
    """Минимальная миссия с заданным ID — для тестов алгоритма поиска
    свободного номера."""
    return Mission(
        id=id_,
        owner_id=owner_id,
        title=f"M{id_}",
        description="",
        level=1,
        waypoints="[]",
        danger_zones="[]",
        actions_required="[]",
        reference_voice="[]",
        reference_code="",
        safety_margin_cm=5.0,
    )


class TestSmallestUnusedMissionId(unittest.TestCase):
    """In-memory SQLite — отдельная БД на каждый тест."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.db = Session()
        # FK на users.id требует существующего юзера.
        self.user = User(username="u1", password_hash="x")
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _add(self, *ids: int):
        for n in ids:
            self.db.add(_mk_mission(n, self.user.id))
        self.db.commit()

    # ── базовые случаи ───────────────────────────────────────────────────

    def test_empty_db_returns_1(self):
        self.assertEqual(_smallest_unused_mission_id(self.db), 1)

    def test_consecutive_returns_next(self):
        self._add(1, 2, 3)
        self.assertEqual(_smallest_unused_mission_id(self.db), 4)

    def test_reuses_smallest_gap(self):
        """Дырка в середине — алгоритм возвращает её, а не «следующий за max»."""
        self._add(1, 2, 4, 5)
        self.assertEqual(_smallest_unused_mission_id(self.db), 3)

    def test_reuses_first_id_when_gap_at_start(self):
        """Дырка с самого начала — возвращаем 1."""
        self._add(3, 5, 7)
        self.assertEqual(_smallest_unused_mission_id(self.db), 1)

    def test_returns_2_when_only_1_taken(self):
        self._add(1)
        self.assertEqual(_smallest_unused_mission_id(self.db), 2)

    # ── граничные случаи ─────────────────────────────────────────────────

    def test_unaffected_by_large_existing_ids(self):
        """Алгоритм не зависит от величины существующих ID — пропуски
        в начале выигрывают, даже если есть «огромные» номера."""
        self._add(100, 200, 9999)
        self.assertEqual(_smallest_unused_mission_id(self.db), 1)

    def test_after_simulated_delete_id_reused(self):
        """Сценарий из жизни: создали 1, 2, 3, удалили 2 — следующая
        генерация даёт 2, а не 4."""
        self._add(1, 2, 3)
        self.db.query(Mission).filter(Mission.id == 2).delete()
        self.db.commit()
        self.assertEqual(_smallest_unused_mission_id(self.db), 2)

    def test_max_attempts_caps_search(self):
        """`max_attempts=1` ограничивает поиск — для плотно занятого
        диапазона алгоритм всё равно вернёт следующий ID, но не уйдёт
        в бесконечный цикл."""
        self._add(*range(1, 6))
        result = _smallest_unused_mission_id(self.db, max_attempts=1)
        # Алгоритм останавливается по условию `n < max_attempts + len(existing)`.
        # Для existing={1..5} и max_attempts=1 граница 6, значит выход на n=6.
        self.assertEqual(result, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
