# -*- coding: utf-8 -*-
"""Авто-подбор цены выставления — Path A / B / C.

Логика выведена из реальных решений юзера на 5 живых стаканах (см. PR #4).

Path A: предмет без флоата (кейсы/наклейки/чармы — «коммодити»).
    threshold = 10% × daily_sales
    Идём по sell-стакану снизу вверх; пока qty_at_level ≤ threshold — accept.
    Как только встретили qty > threshold — стоп (даже если потом снова попадётся
    тонкий уровень — это уже «после стенки», эти доли отдадим).
    Берём самый высокий accepted уровень = base_price.
    Если рынок STABLE (|week_pct| ≤ 2%) и стенка существует — двигаемся на
    один шаг к стенке (= первый rejected уровень).

Path B: есть флоат, паттерн НЕ редкий.
    Фильтр POST: float ≤ f_my * 1.10, та же quality/exterior, sort ASC.
    Берём min_price + qty_at_min_price.
    Если qty_at_min < daily_sales (стенка маленькая) → ставим РОВНО min.
    Иначе → min − 0.01 (один шаг под минимум).

Path C: редкий паттерн.
    Никакой автоматики — просим ввести цену вручную.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# =============================================================================
# Path A
# =============================================================================
@dataclass
class PathASuggestion:
    cents: int | None
    reason: str  # короткое объяснение для таблицы


def path_a_suggest(
    sell_table: list[tuple[int, int]],
    daily_sales: float,
    week_pct: float | None,
) -> PathASuggestion:
    """Алгоритм авто-цены для коммодити-предмета.

    sell_table: [(price_cents, qty_at_this_price), ...] — отсортирован ASC,
        обычно даётся из histogram.sell_order_table (топ ~6 уровней).
    daily_sales: среднее число продаж в сутки (например, из price_history /
        Steam-aggregate за последний месяц).
    week_pct: процентное изменение цены за неделю; None если неизвестно.

    Возвращает PathASuggestion(cents, reason). cents=None если стакан пустой.
    """
    if not sell_table or daily_sales <= 0:
        return PathASuggestion(None, "no data")

    threshold = daily_sales * 0.10  # 10% от дневного объёма

    # Сначала пропускаем «аномальный толстый пол» внизу стакана: ботовый
    # floor / большой dump на самой дешёвой плите часто стоит первым и не
    # является «настоящей стенкой», на которую мы боимся налететь. Пример —
    # Gallery Case: 1.58$ qty=1502 (далеко больше 10% daily), затем тонкие
    # 1.61/1.62/1.63, потом снова thick 1.64 (уже настоящая стенка).
    i = 0
    while i < len(sell_table) and sell_table[i][1] > threshold:
        i += 1

    accepted: list[tuple[int, int]] = []
    rejected: tuple[int, int] | None = None
    for j in range(i, len(sell_table)):
        price, qty = sell_table[j]
        if qty <= threshold:
            accepted.append((price, qty))
        else:
            rejected = (price, qty)
            break

    if not accepted:
        # Все уровни «толстые» — низкая ликвидность. Подрезаем минимум на 1c.
        min_price = sell_table[0][0]
        return PathASuggestion(
            max(1, min_price - 1),
            f"low-liq (все уровни > {threshold:.0f}), undercut min={min_price/100:.2f}",
        )

    base_price, base_qty = accepted[-1]

    # Стабильный недельный тренд → можно на шаг ближе к стенке.
    stable = week_pct is not None and abs(week_pct) <= 2.0
    if stable and rejected is not None:
        chosen_price, chosen_qty = rejected
        reason = (
            f"stable (week={week_pct:+.1f}%), one-step-deeper: "
            f"reject@{chosen_price/100:.2f} (qty={chosen_qty}) "
            f"vs threshold={threshold:.0f}"
        )
    else:
        chosen_price = base_price
        wk = f"{week_pct:+.1f}%" if week_pct is not None else "?"
        reason = (
            f"accept last≤10%·daily: {chosen_price/100:.2f} "
            f"(qty={base_qty} ≤ {threshold:.0f}), week={wk}"
        )

    return PathASuggestion(chosen_price, reason)


# =============================================================================
# Path B — нужен живой POST-запрос к Steam, поэтому async
# =============================================================================
@dataclass
class PathBSuggestion:
    cents: int | None
    reason: str


async def path_b_suggest(
    session,
    app_id: int,
    gid: str,
    *,
    our_float: float,
    quality_tag: str | None,
    exterior_tag: str | None,
    currency_code: int,
    daily_sales: float,
) -> PathBSuggestion:
    """Авто-цена для скина с флоатом (не редкий паттерн).

    Делает POST на market-эндпоинт с фильтрами quality/exterior + float_max.
    Возвращает суггест в центах + reason.
    """
    # Локальный импорт чтобы не тянуть item_info на верхний уровень.
    import item_info

    f_max = max(0.0, min(1.0, our_float * 1.10))

    category_filters: dict[str, list[str]] = {}
    if quality_tag:
        category_filters["category_730_Quality"] = [quality_tag]
    if exterior_tag:
        category_filters["category_730_Exterior"] = [exterior_tag]

    data = await item_info._fetch_listings_page(  # noqa: SLF001
        session,
        app_id,
        gid,
        start=0,
        sort_field=0,
        sort_dir=0,
        category_filters=category_filters or None,
        wear_range=(0.0, f_max),
        currency_code=currency_code,
    )
    if not data:
        return PathBSuggestion(None, "POST listings вернул пусто")

    listings = data.get("listings") or []
    if not listings:
        return PathBSuggestion(
            None, f"нет листингов с float ≤ {f_max:.4f}"
        )

    # Цена первого = минимум. Steam уже отсортировал ASC.
    try:
        min_price = int(listings[0].get("unPricePerUnit") or 0)
    except (TypeError, ValueError):
        min_price = 0
    if min_price <= 0:
        return PathBSuggestion(None, "не смог распарсить min_price")

    # Считаем qty на min-price — листинги уже отсортированы по цене ASC.
    qty_at_min = 0
    for li in listings:
        try:
            p = int(li.get("unPricePerUnit") or 0)
        except (TypeError, ValueError):
            continue
        if p == min_price:
            qty_at_min += 1
        else:
            break

    # Если на странице 20 листингов и все по min — стенка ≥ 20, точное число
    # неизвестно, но Steam отсортировал по asc и страница забита одним уровнем.
    page_full_at_min = (qty_at_min == len(listings)) and len(listings) >= 20

    if (
        not page_full_at_min
        and daily_sales > 0
        and qty_at_min < daily_sales
    ):
        return PathBSuggestion(
            min_price,
            f"match min={min_price/100:.2f} (qty {qty_at_min} < daily {daily_sales:.0f})",
        )

    return PathBSuggestion(
        max(1, min_price - 1),
        f"undercut min={min_price/100:.2f} −0.01 (qty {qty_at_min} ≥ daily-wall)",
    )


# =============================================================================
# Классификация
# =============================================================================
def classify(name: str, paint_seed: int | None) -> str:
    """Решает, каким Path считать цену.

    A — нет seed/float (кейсы, наклейки, патчи, грэффити, монеты, etc).
    B — есть seed, паттерн не «редкий».
    C — есть seed, паттерн редкий (`patterns.is_rare_pattern(...).is_rare`).
    Uncertain (`is_rare=None`) трактуем как C — «попроси цену вручную»,
    потому что обычно это означает «в danger-list, но без точных номеров».
    """
    if paint_seed is None:
        return "A"
    try:
        import patterns
    except ImportError:
        return "B"
    res = patterns.is_rare_pattern(name, int(paint_seed))
    if res.is_rare in (True, None):
        return "C"
    return "B"


# =============================================================================
# Утилиты: метрики из price_history
# =============================================================================
def daily_sales_from_history(history, days: int = 30) -> float:
    """Среднее число продаж в сутки за последние `days` дней.

    history: список объектов с .date (datetime) и .daily_volume (int) — то,
    что `aiosteampy.fetch_price_history` возвращает
    (`List[PriceHistoryEntry]`).

    Делим суммарный объём в окне на `days`, а не на число точек: Steam в
    последние 24 часа отдаёт почасовые точки + дневные за остальной
    период, поэтому деление на «количество точек» искусственно занижает
    результат и расходится с тем, что показывает `i`-команда
    (`_sum_volume` в item_info.py).
    """
    if not history:
        return 0.0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    found_any = False
    for pt in history:
        d = getattr(pt, "date", None)
        if d is None:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d < cutoff:
            continue
        v = getattr(pt, "daily_volume", None) or 0
        try:
            total += int(v)
        except (TypeError, ValueError):
            continue
        found_any = True
    if not found_any or days <= 0:
        return 0.0
    return total / float(days)


def week_pct_from_history(history) -> float | None:
    """Изменение цены за последние ~7 дней (% от значения 7 дней назад).

    Возвращает None если данных недостаточно.
    """
    if not history or len(history) < 2:
        return None
    last = history[-1]
    last_price = getattr(last, "price", None)
    last_date = getattr(last, "date", None)
    if last_price is None or last_date is None:
        return None
    if last_date.tzinfo is None:
        last_date = last_date.replace(tzinfo=timezone.utc)
    target = last_date - timedelta(days=7)
    # Ищем ближайшую точку к target (но не позже него).
    candidate = None
    for pt in history:
        d = getattr(pt, "date", None)
        if d is None:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d <= target:
            candidate = pt
        else:
            break
    if candidate is None:
        # Не хватает истории на неделю — берём самую раннюю точку.
        candidate = history[0]
    base_price = getattr(candidate, "price", None)
    if not base_price:
        return None
    return (float(last_price) - float(base_price)) / float(base_price) * 100.0