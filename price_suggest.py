# -*- coding: utf-8 -*-
"""Авто-подбор цены выставления — Path A / B / C.

Логика выведена из реальных решений юзера на 5 живых стаканах (см. PR #4).

Path A: предмет без флоата (кейсы/наклейки/чармы — «коммодити»).
    threshold = 10% × daily_sales

    Алгоритм (PR #11) — единый cum-overflow stop:

    1. Пропускаем «аномальный толстый пол» внизу стакана: ботовый floor
       или большой dump на самой дешёвой плите. Это per-level qty >
       threshold у самых первых уровней.
    2. Идём дальше; copying per-level (qty > threshold → rejected) и
       одновременно cumulative (cum + qty > threshold → overflow).
       Останавливаемся при первом из событий.
    3. Если cum overflow случился ВНУТРИ accepted'ов раньше (или вообще)
       чем per-level стенка — base = последний уровень где cum остался
       ≤ threshold. Никакого step-deeper: путь до стенки уже жирный,
       занимать стенку плохо.
    4. Если cum overflow НЕ случился (cum_accepted ≤ threshold даже у
       последнего accepted) — путь до стенки тонкий. Тогда:
         - STABLE (|week_pct| ≤ 2%) + стенка есть → step-deeper к стенке.
         - иначе → base = последний accepted.

    Это симметрично:
      - Gallery Case (тонкий путь, стенка близко) → step-deeper к 1.64.
      - Spectrum 2 PR #5 case (тонкий путь к стенке 4.91) → 4.91 step-deeper.
      - Spectrum 2 #35 (cum переполнился рано) → cum-stop (~48.16).
      - Revolution Case (нет per-level стенки, но cum переполнился) →
        cum-stop ~ 4.49.
      - Жирный кластер ликвидности перед стенкой (новый репорт юзера на
        кейсе 9.x NOK / 14873 daily) → cum-stop 9.38, не stay@base 9.61.

Path B: есть флоат, паттерн НЕ редкий.
    Фильтр POST: float ≤ f_my * 1.10, та же quality (без exterior), sort ASC.
    Skip 0-price + outlier (<25% от next). Дальше — четыре сигнала:
      density: spread топ-5 листингов (DENSE <3%, SPARSE >10%, иначе MEDIUM).
      wall: в top-25 ищем ценовой уровень с макс концентрацией копий (mode).
        Если wall ≥ p1·1.10 и concentration ≥ 30% — active: настоящий
        рынок глубже top-5, пол из нескольких тонких дамперов, которых
        никто не покупает (PR #7).
      velocity: daily_sales (FAST ≥50, SLOW ≤5, иначе MED).
      trend: week_pct (RISING >+5%, FALLING <−5%, иначе FLAT).
    Решение:
      - WALL active → anchor=wall, undercut −0.01 (на wall много копий,
        подрезать имеет смысл).
      - иначе DENSE → match anchor (без −0.01, стенка и так нас прокатит).
      - иначе SPARSE & p2>p1·1.05 → anchor=p2, undercut −0.01 (p1 «thin»).
      - иначе → anchor=p1, undercut −0.01.
    Модификатор trend×velocity (мягкий, cap ±5%):
      - RISING: +1% (+1% если SLOW).
      - FALLING: −1% (−1% если FAST).
    Float-discount (PR #7): если наш float хуже флора в anchor-зоне
    (на wall — min float среди копий wall, иначе — float самого anchor-
    листинга) на ≥ 0.005 — скидка K=2% за каждые 0.01 разницы, cap −8%.
    И общий floor: final ≥ anchor×0.85 (не уезжаем больше чем −15%
    от стенки).

Path C: редкий паттерн.
    Никакой автоматики — просим ввести цену вручную.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# --- CS2 wear-categories (верхние границы float'а) ---------------------------
# Buyer, ищущий FT-скин, не видит WW-листинги в выдаче. Фильтр Path B должен
# ограничиваться сверху границей НАШЕЙ wear-категории — иначе захватываем
# листинги соседней-худшей категории (например, для FT с float=0.376 фильтр
# 1.10× = 0.414 выходит за FT-границу 0.38 и тянет WW), и в первой странице
# выдачи оседает дешёвый WW-сектор, а реальная FT-стенка убегает за горизонт.
WEAR_MAX_FN = 0.07
WEAR_MAX_MW = 0.15
WEAR_MAX_FT = 0.38
WEAR_MAX_WW = 0.45
WEAR_MAX_BS = 1.00


def wear_category_max(our_float: float) -> float:
    """Верхняя граница float'а нашей wear-категории (PR #8).

    Steam-границы: FN<0.07, MW<0.15, FT<0.38, WW<0.45, BS≤1.00.
    Используется как cap для wear_range — настоящий фильтр в Path B
    `min(our_float × 1.10, wear_category_max(our_float))`, см.
    `path_b_suggest`.
    """
    if our_float < WEAR_MAX_FN:
        return WEAR_MAX_FN
    if our_float < WEAR_MAX_MW:
        return WEAR_MAX_MW
    if our_float < WEAR_MAX_FT:
        return WEAR_MAX_FT
    if our_float < WEAR_MAX_WW:
        return WEAR_MAX_WW
    return WEAR_MAX_BS


# --- Path B пороги (вынесены наверх чтобы легко подкручивать) ----------------
WALL_WINDOW = 25            # сколько листингов смотрим для поиска стенки
WALL_MIN_RATIO = 1.10       # wall должен быть ≥ p1×1.10
WALL_MIN_CONCENTRATION = 0.30  # доля копий на wall-уровне в окне
FLOAT_DISC_THRESHOLD = 0.005   # мин. разница float'а чтобы включить discount
FLOAT_DISC_K = 2.0          # % за каждые 0.01 разницы
FLOAT_DISC_CAP = 8.0        # cap на float-discount (% от base)
FINAL_FLOOR_RATIO = 0.85    # final ≥ anchor × 0.85 (общий floor)


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
        per-уровень стакан до ~100 строк (PR #5). Источник —
        `_graph_to_deltas(histogram.sell_order_graph)` в `item_info.py`, НЕ
        `sell_order_table`: последний в нём — синтетический «всё ≥ X» bucket
        (для Spectrum 2 Case это давало 47.77 qty=14395, при том что реально
        на 47.77 стоит 1 лот, а массы стакана глубже).
    daily_sales: среднее число продаж в сутки (например, из price_history /
        Steam-aggregate за последний месяц).
    week_pct: процентное изменение цены за неделю; None если неизвестно.

    Возвращает PathASuggestion(cents, reason). cents=None если стакан пустой.
    """
    if not sell_table or daily_sales <= 0:
        return PathASuggestion(None, "no data")

    threshold = daily_sales * 0.10  # 10% от дневного объёма

    # 1) Skip аномальный толстый пол внизу: ботовый floor / большой dump
    #    на самой дешёвой плите. per-level qty > threshold у первых уровней.
    #    Пример — Gallery Case: 1.58$ qty=1502 (>> 10% daily), затем тонкие
    #    1.61/1.62/1.63, потом thick 1.64 (настоящая стенка).
    i = 0
    while i < len(sell_table) and sell_table[i][1] > threshold:
        i += 1

    # 2) Собираем accepted: уровни с per-level qty ≤ threshold, до первого
    #    per-level wall (rejected).
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

    stable = week_pct is not None and abs(week_pct) <= 2.0
    wk = f"{week_pct:+.1f}%" if week_pct is not None else "?"

    # 3) Ищем cum overflow ВНУТРИ accepted: первый индекс где cum + qty
    #    превышает threshold. Если такого нет — cum_accepted ≤ threshold
    #    (тонкий путь до стенки).
    cum = 0
    cum_overflow_idx: int | None = None
    for j, (_, qty) in enumerate(accepted):
        if cum + qty > threshold:
            cum_overflow_idx = j
            break
        cum += qty

    if cum_overflow_idx is None:
        # Тонкий путь: даже сумма по всем accepted ≤ threshold. Перед
        # стенкой стоит мало лотов — можно занять стенку (если рынок
        # стабилен) или верхний accepted (если нестабилен / стенки нет).
        base_price, base_qty = accepted[-1]
        if stable and rejected is not None:
            chosen_price, chosen_qty = rejected
            reason = (
                f"thin path (cum_accepted={cum}<={threshold:.0f}), "
                f"stable (week={wk}), step-deeper: "
                f"reject@{chosen_price/100:.2f} (qty={chosen_qty})"
            )
        else:
            chosen_price = base_price
            note = "no wall" if rejected is None else "not stable enough"
            reason = (
                f"thin path (cum_accepted={cum}<={threshold:.0f}), "
                f"{note}, top={chosen_price/100:.2f} (qty={base_qty}), week={wk}"
            )
        return PathASuggestion(chosen_price, reason)

    # 4) cum переполнился внутри accepted: останавливаемся на предыдущем
    #    уровне. Это значит «перед нами ≤ 10% daily лотов» — реальная
    #    зона ликвидности, не верхушка перед стенкой.
    if cum_overflow_idx == 0:
        # Уже первый уровень переполняет cum — фактически low-liquidity.
        min_price = sell_table[0][0]
        return PathASuggestion(
            max(1, min_price - 1),
            f"low-liq (cum overflow на первом уровне), undercut min={min_price/100:.2f}",
        )

    chosen_price, _chosen_qty = accepted[cum_overflow_idx - 1]
    overflow_price, overflow_qty = accepted[cum_overflow_idx]
    cum_at_stop = sum(q for _, q in accepted[:cum_overflow_idx])
    reason = (
        f"cum-stop: last cum<=thr on {chosen_price/100:.2f} "
        f"(cum={cum_at_stop}<={threshold:.0f}); next {overflow_price/100:.2f} "
        f"would push cum to {cum_at_stop + overflow_qty} > {threshold:.0f}, "
        f"week={wk}"
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
    week_pct: float | None = None,
) -> PathBSuggestion:
    """Авто-цена для скина с флоатом (не редкий паттерн), soft-max режим.

    Делает POST на market-эндпоинт с фильтрами quality + float ∈
    [0.0, min(our_float × 1.10, wear_max)] где wear_max — верхняя граница
    НАШЕЙ wear-категории (FN/MW/FT/WW/BS, см. `wear_category_max`).
    Узкое окно ×1.10 сохраняет специфику фильтра под наш float, а
    wear_max-cap убирает «WW-захват» когда наш FT-скин имеет высокий
    float и 1.10× выходит за FT-границу (PR #8). FN/MW-листинги с
    лучшим флоатом остаются как реальные конкуренты — нижнюю границу не
    обрезаем.

    Exterior-тег (FN/MW/FT/...) намеренно НЕ используем: фильтр по тегу
    обрезал бы FN/MW при нашем FT, а они дешевле и часть buyer'ов их видит
    в выдаче «все wear».

    Алгоритм (см. docstring модуля, секция Path B):
      1. Фильтруем 0-price листинги (баг Steam).
      2. Outlier-фильтр: первый валидный < 25% от второго — пропускаем.
      3. Считаем сигналы density / wall / velocity / trend.
      4. Выбираем anchor (WALL > p2-skip > p1) и базу (match / undercut −0.01).
      5. Применяем модификатор trend×velocity (cap ±5%) + float-discount
         (vs floor float в anchor-зоне, cap −8%).
      6. Общий floor: final ≥ anchor × 0.85.

    Параметр `exterior_tag` оставлен ради обратной совместимости и игнорируется.
    """
    # Локальный импорт чтобы не тянуть item_info на верхний уровень.
    import item_info

    # Не обрезаем снизу по флоату (0.0): если есть FN/MW с флоатом
    # лучше нашего — они реальные конкуренты по цене. Сверху:
    # min(our_float×1.10, wear_max) — узкое окно вокруг нашего float'а,
    # но НЕ выходящее за wear-категорию (PR #8: иначе для FT-скина с
    # float≈0.376 окно 1.10× захватывало WW и реальная FT-стенка
    # «убегала» на страницы 2+).
    f_max = min(our_float * 1.10, wear_category_max(our_float))

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

    p1 = _buyer_price(valid[0])
    if p1 <= 0:
        return PathBSuggestion(None, "не смог распарсить min_price")

    # Парсим листинги через item_info чтобы достать float'ы вместе с ценой
    # — нужно для wall-detection и float-discount (см. ниже). _parse_listings_v2
    # уже считает price = unPricePerUnit + unFeePerUnit (buyer-facing), так что
    # buy_price-индексы совпадают с _buyer_price(...).
    parsed_all = item_info._parse_listings_v2(data)  # noqa: SLF001
    # outlier-skip / zero-skip применялись только к valid → пересобираем
    # parsed под актуальный набор listing_id'шников.
    valid_ids = {str(li.get("listingid") or "") for li in valid}
    parsed = [p for p in parsed_all if p["listing_id"] in valid_ids] if valid_ids else parsed_all

    # === Сигналы ===========================================================
    # 1) density — насколько плотно стоят топ-5 листингов.
    top_k = min(5, len(valid))
    top_prices = [_buyer_price(li) for li in valid[:top_k]]
    pk = top_prices[-1]
    spread_pct = (pk - p1) * 100.0 / p1 if p1 > 0 else 0.0
    if spread_pct < 3.0:
        density = "DENSE"
    elif spread_pct > 10.0:
        density = "SPARSE"
    else:
        density = "MEDIUM"

    # 2) velocity — частота продаж.
    if daily_sales >= 50:
        velocity = "FAST"
    elif daily_sales <= 5:
        velocity = "SLOW"
    else:
        velocity = "MED"

    # 3) trend — недельное изменение цены.
    if week_pct is None:
        trend = "FLAT"
    elif week_pct > 5.0:
        trend = "RISING"
    elif week_pct < -5.0:
        trend = "FALLING"
    else:
        trend = "FLAT"

    # 4) wall — ищем уровень с max концентрацией копий в top-WALL_WINDOW.
    # При равенстве count'ов берём самый ДЕШЁВЫЙ (sort by (-count, price)).
    # Фиксит кейс «реальная стенка глубже top-5» — пол из нескольких тонких
    # дамперов закрывает обзор, и median/p1 промахиваются по настоящему рынку.
    wall_top = [p for p in parsed[:WALL_WINDOW] if p["price_cents"] > 0]
    wall_price: int | None = None
    wall_count = 0
    wall_conc = 0.0
    if wall_top:
        buckets = Counter(p["price_cents"] for p in wall_top)
        ranked = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
        cand_price, cand_count = ranked[0]
        total_window = sum(buckets.values())
        conc = cand_count / total_window if total_window > 0 else 0.0
        if (
            cand_price >= int(p1 * WALL_MIN_RATIO)
            and conc >= WALL_MIN_CONCENTRATION
        ):
            wall_price = cand_price
            wall_count = cand_count
            wall_conc = conc

    # === Anchor =============================================================
    # Приоритет: WALL > SPARSE-skip-thin-p1 > p1.
    anchor = p1
    anchor_kind = "p1"
    sparse_skip_p1 = False
    if wall_price is not None:
        anchor = wall_price
        anchor_kind = "wall"
    elif density == "SPARSE" and len(valid) >= 2:
        p2 = _buyer_price(valid[1])
        # p1 «тонкий» (≥5% дешевле p2) — ориентируемся на p2.
        if p2 > p1 * 1.05:
            anchor = p2
            anchor_kind = "p2"
            sparse_skip_p1 = True

    # === База: match wall vs undercut =======================================
    # WALL: подрезаем −0.01 (на wall-уровне много копий, есть смысл их
    # обогнать). DENSE без wall: matchим (стенка == пол, подрез бесполезен).
    if anchor_kind == "wall":
        base = max(1, anchor - 1)
        base_op = "undercut −0.01"
    elif density == "DENSE":
        base = anchor  # стенка плотная — просто matchим её, не теряем 0.01.
        base_op = "match"
    else:
        base = max(1, anchor - 1)
        base_op = "undercut −0.01"

    # === Модификатор: мягкий ± по тренду × velocity ========================
    mod_pct = 0.0
    if trend == "RISING":
        mod_pct += 1.0
        if velocity == "SLOW":
            mod_pct += 1.0  # медленный рост — можно держать ВЫШЕ стенки
    elif trend == "FALLING":
        mod_pct -= 1.0
        if velocity == "FAST":
            mod_pct -= 1.0  # быстрый падающий рынок — агрессивнее уходим
    # Cap ±5% чтобы не уезжать сильно в обе стороны.
    if mod_pct > 5.0:
        mod_pct = 5.0
    elif mod_pct < -5.0:
        mod_pct = -5.0

    # === Float-discount (PR #7) =============================================
    # Сравниваем наш float с «floor float» в anchor-зоне:
    #  - WALL: min float среди копий на wall-уровне (те, кто реально нас
    #    опередят за те же деньги).
    #  - p2-skip: float p2-листинга (наш новый якорь).
    #  - p1: float p1-листинга.
    # Если наш float ХУЖЕ floor'а на ≥ FLOAT_DISC_THRESHOLD — даём скидку
    # K% за каждые 0.01 разницы, cap FLOAT_DISC_CAP%. Если наш float ЛУЧШЕ
    # (или нет данных) — discount=0.
    floor_float: float | None = None
    if anchor_kind == "wall":
        wall_floats = [
            p["float"] for p in wall_top
            if p["price_cents"] == anchor and p["float"] is not None
        ]
        if wall_floats:
            floor_float = min(wall_floats)
    elif anchor_kind == "p2" and len(parsed) >= 2:
        floor_float = parsed[1].get("float")
    elif parsed:
        floor_float = parsed[0].get("float")

    float_disc_pct = 0.0
    if (
        floor_float is not None
        and our_float is not None
        and our_float > 0.0
    ):
        delta = our_float - floor_float
        if delta > FLOAT_DISC_THRESHOLD:
            float_disc_pct = -min(FLOAT_DISC_CAP, FLOAT_DISC_K * (delta / 0.01))

    total_mod_pct = mod_pct + float_disc_pct
    final = base
    if total_mod_pct != 0.0:
        final = max(1, int(round(base * (1.0 + total_mod_pct / 100.0))))
    # Общий floor: не уезжаем больше чем −15% от anchor.
    min_final = max(1, int(round(anchor * FINAL_FLOOR_RATIO)))
    if final < min_final:
        final = min_final

    # === Reason для таблицы =================================================
    sig_parts = [density, velocity, trend]
    if anchor_kind == "wall":
        sig_parts.insert(0, "WALL")
    sig = "·".join(sig_parts)
    parts = [sig]
    if anchor_kind == "wall":
        parts.append(
            f"anchor=wall={anchor/100:.2f} (conc={wall_conc*100:.0f}% "
            f"n={wall_count}/{len(wall_top)} p1={p1/100:.2f})"
        )
    elif sparse_skip_p1:
        parts.append(
            f"anchor=p2={anchor/100:.2f} (skip thin p1={p1/100:.2f})"
        )
    else:
        parts.append(f"anchor=p1={p1/100:.2f}")
    parts.append(f"{base_op}={base/100:.2f}")
    if mod_pct != 0.0:
        parts.append(f"mod {mod_pct:+.0f}%")
    if float_disc_pct != 0.0:
        ff = f"{floor_float:.4f}" if floor_float is not None else "?"
        of = f"{our_float:.4f}" if our_float is not None else "?"
        parts.append(
            f"float-disc {float_disc_pct:+.1f}% (our={of} floor={ff})"
        )
    parts.append(f"→ {final/100:.2f}")
    week_str = f"{week_pct:+.1f}%" if week_pct is not None else "?"
    parts.append(
        f"(spread={spread_pct:.1f}% daily={daily_sales:.0f} week={week_str})"
    )
    reason = " ".join(parts)
    if zero_skipped:
        reason += f"; skip {zero_skipped}× price=0"
    if outlier_skipped is not None:
        reason += f"; skip outlier={outlier_skipped/100:.2f}"
    return PathBSuggestion(final, reason)


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