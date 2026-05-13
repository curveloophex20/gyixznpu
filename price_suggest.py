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

    Делает POST на market-эндпоинт с фильтрами quality + float ∈ [0.0, our_float * 1.10].
    Exterior-тег (FN/MW/FT/...) намеренно НЕ используем: иначе у FT-скина фильтр
    обрежет листинги до FT (min float ≈ 0.15) и мы пропустим более дешёвые
    FN/MW-листинги с лучшим флоатом, которые уже конкурируют за того же покупателя.

    Логика выбора цены:
      1. Фильтруем листинги с price=0 (баг Steam).
      2. Если первый валидный листинг < 25% от второго — outlier (опечатка/
         дампер), скипаем и берём следующий.
      3. min_price = buyer-facing цена оставшегося первого листинга.
      4. suggest = min_price − 0.01 (always undercut, без условий по daily).

    Возвращает суггест в центах + reason. Параметр `exterior_tag` оставлен ради
    обратной совместимости и игнорируется.
    """
    # Локальный импорт чтобы не тянуть item_info на верхний уровень.
    import item_info

    # Не обрезаем снизу по флоату (0.0): если есть FN/MW с флоатом
    # лучше нашего — они реальные конкуренты по цене.
    f_max = max(0.0, min(1.0, our_float * 1.10))

    category_filters: dict[str, list[str]] = {}
    if quality_tag:
        category_filters["category_730_Quality"] = [quality_tag]
    # exterior_tag намеренно не добавляем — см. docstring.

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

    # Берём buyer-facing цену = unPricePerUnit + unFeePerUnit. unPricePerUnit один —
    # это сколько ПОЛУЧИТ продавец после комиссии Steam (~15%), а нам
    # нужна цена «для покупателя» — та же, что показывает Market UI и которую
    # бот передаёт в place_sell_listing(price=...).
    def _buyer_price(li: dict) -> int:
        try:
            return (int(li.get("unPricePerUnit") or 0)
                    + int(li.get("unFeePerUnit") or 0))
        except (TypeError, ValueError):
            return 0

    # Steam иногда возвращает листинги с price=0 (баг) — фильтруем их,
    # иначе min_price=0 и весь Path B падает с "не смог распарсить".
    valid = [li for li in listings if _buyer_price(li) > 0]
    zero_skipped = len(listings) - len(valid)
    if not valid:
        return PathBSuggestion(
            None, "все листинги с нулевой ценой (Steam bug)"
        )

    # Outlier-защита от дамперов/опечаток: если первый валидный листинг
    # резко дешевле второго (порог: < 25% от второго) — пропускаем его
    # как outlier и берём следующий.
    outlier_skipped: int | None = None
    if len(valid) >= 2:
        p1 = _buyer_price(valid[0])
        p2 = _buyer_price(valid[1])
        # p1 * 4 < p2 эквивалентно p1 < p2 * 0.25 без float-арифметики.
        if p1 * 4 < p2:
            outlier_skipped = p1
            valid = valid[1:]

    min_price = _buyer_price(valid[0])
    if min_price <= 0:
        return PathBSuggestion(None, "не смог распарсить min_price")

    # qty_at_min — для информации в reason'е. По новой логике always undercut
    # она цену больше не определяет (раньше зависело от daily_sales).
    qty_at_min = 0
    for li in valid:
        if _buyer_price(li) == min_price:
            qty_at_min += 1
        else:
            break

    suggest = max(1, min_price - 1)
    parts = [
        f"undercut min={min_price/100:.2f} −0.01 "
        f"(qty {qty_at_min}, daily {daily_sales:.0f})"
    ]
    if zero_skipped:
        parts.append(f"skip {zero_skipped}× price=0")
    if outlier_skipped is not None:
        parts.append(f"skip outlier={outlier_skipped/100:.2f}")
    return PathBSuggestion(suggest, "; ".join(parts))


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