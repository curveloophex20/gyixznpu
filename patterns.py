"""Детектор редких паттернов CS2-скинов для безопасного массового выставления.

Использует два пользовательских файла:

- `data/7patterns.txt` — большая запятая-разделённая строка с base_name'ами скинов,
  у которых есть редкие паттерны (без указания конкретных номеров). Это «красная
  зона»: если ты знаешь, что у скина есть редкие паттерны, но не вписал номера в
  json — лучше на всякий случай не выставлять автоматом.

- `data/7patterns.json` — список объектов вида:
      {
        "enabled": true,
        "base_name": "Galil AR | Rainbow Spoon",
        "tiers": [
          {"enabled": true, "note": "Tier 1", "patterns": [0, 1, 3, ...]},
          ...
        ]
      }
  Если paint_seed предмета встречается в любом enabled-tier этого base_name —
  он считается **редким** и не должен выставляться без ручного подтверждения.

Публичный API:
    load_pattern_db()                       — загрузить и закешировать оба файла
    is_rare_pattern(market_hash_name, paint_seed) -> RarePatternResult

`market_hash_name` — полное имя как в Steam, например
    "Galil AR | Rainbow Spoon (Field-Tested)"
Из него выделяется base_name (часть до скобки с износом) для сравнения с базой.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Кеш на уровне модуля. Перезагружаем по запросу или если файлы изменились.
_DB_CACHE: dict | None = None
_DB_MTIMES: tuple[float, float] | None = None


@dataclass(frozen=True)
class RarePatternResult:
    """Результат проверки.

    is_rare:
        True  — точно редкий (paint_seed нашёлся в json).
        False — точно НЕ редкий.
        None  — неопределённо: скин в .txt-списке, но конкретных номеров в json нет
                → пользователь должен решать сам.
    tier_note:
        Имя тира (например "Tier 1") если is_rare=True. None в остальных случаях.
    base_name:
        Распознанное base_name (имя без скобки с износом).
    """

    is_rare: bool | None
    tier_note: str | None
    base_name: str


def _strip_wear(market_hash_name: str) -> str:
    """`AK-47 | Slate (Minimal Wear)` → `AK-47 | Slate`. Убираем StatTrak, Souvenir, ★."""
    name = market_hash_name.strip()
    # Убираем `StatTrak™ ` и `Souvenir ` префиксы (StatTrak™ может быть с спец. символом).
    name = re.sub(r"^(StatTrak™\s+|Souvenir\s+|★\s*)", "", name)
    # Убираем `★ ` префикс ножей (если остался).
    name = name.lstrip("★ ").strip()
    # Убираем хвост `(Wear)`.
    m = re.match(r"^(.*?)\s*\([^)]+\)\s*$", name)
    if m:
        name = m.group(1).strip()
    return name


def _files_paths() -> tuple[Path, Path]:
    base = Path(__file__).parent / "data"
    return base / "7patterns.txt", base / "7patterns.json"


def load_pattern_db(*, force: bool = False) -> dict:
    """Загружает (или возвращает из кеша) базу редких паттернов.

    Структура возвращаемого словаря:
        {
            "danger_zone": set[str]       # base_name'ы из 7patterns.txt
            "rare_patterns": dict[str, list[dict]]
                # base_name → список tier'ов: [{"note": "Tier 1", "patterns": set[int]}]
        }
    """
    global _DB_CACHE, _DB_MTIMES
    txt_path, json_path = _files_paths()
    txt_m = txt_path.stat().st_mtime if txt_path.is_file() else 0.0
    json_m = json_path.stat().st_mtime if json_path.is_file() else 0.0
    if not force and _DB_CACHE is not None and _DB_MTIMES == (txt_m, json_m):
        return _DB_CACHE

    danger_zone: set[str] = set()
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")
        for piece in raw.split(","):
            name = piece.strip()
            if name:
                danger_zone.add(name)

    rare_patterns: dict[str, list[dict]] = {}
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Невалидный JSON в {json_path}: {exc}") from exc
        for entry in data:
            if not entry.get("enabled", True):
                continue
            base_name = (entry.get("base_name") or "").strip()
            if not base_name:
                continue
            tiers_out: list[dict] = []
            for tier in entry.get("tiers", []) or []:
                if not tier.get("enabled", True):
                    continue
                pats = tier.get("patterns") or []
                if not pats:
                    continue
                tiers_out.append(
                    {
                        "note": (tier.get("note") or "").strip() or "?",
                        "patterns": set(int(p) for p in pats),
                    }
                )
            if tiers_out:
                rare_patterns[base_name] = tiers_out

    _DB_CACHE = {"danger_zone": danger_zone, "rare_patterns": rare_patterns}
    _DB_MTIMES = (txt_m, json_m)
    return _DB_CACHE


def is_rare_pattern(market_hash_name: str, paint_seed: int | None) -> RarePatternResult:
    """Решает, можно ли массово выставлять предмет.

    True  → пропустить (точно редкий, есть в json).
    None  → лучше уточнить у пользователя (skin в .txt-списке, но номеров в json нет).
    False → можно выставлять смело.
    """
    base = _strip_wear(market_hash_name)
    db = load_pattern_db()
    in_danger = base in db["danger_zone"]
    tiers = db["rare_patterns"].get(base)

    if tiers:
        if paint_seed is None:
            # знаем, что у этого скина есть редкие — но не знаем какой у нас → uncertain
            return RarePatternResult(is_rare=None, tier_note=None, base_name=base)
        for tier in tiers:
            if paint_seed in tier["patterns"]:
                return RarePatternResult(is_rare=True, tier_note=tier["note"], base_name=base)
        # есть конкретные номера, и наш не подходит → не редкий
        return RarePatternResult(is_rare=False, tier_note=None, base_name=base)

    if in_danger:
        # известно, что есть редкие паттерны, но конкретных номеров нет → uncertain
        return RarePatternResult(is_rare=None, tier_note=None, base_name=base)

    return RarePatternResult(is_rare=False, tier_note=None, base_name=base)


def is_charm(market_hash_name: str) -> bool:
    """Брелок (Charm) — у всех есть редкие паттерны, поэтому массово не выставляем."""
    name = market_hash_name.lower().strip()
    return name.startswith(("charm ", "charm |", "keychain "))