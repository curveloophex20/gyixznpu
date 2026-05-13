# -*- coding: utf-8 -*-
"""Просмотр листингов и графика цены предмета на Steam Market.

Делает два вызова aiosteampy:
- `client.get_item_orders_histogram(item_nameid)` — топ buy- и sell-таблицы;
- `client.fetch_price_history(market_hash_name, app)` — список точек
  «дата → средняя цена → объём».
Плюс свой запрос листингов через новый POST-эндпоинт Steam
(SSR-redesign 2026), который отдаёт float/paint_seed прямо в ответе.

`item_nameid` (внутренний числовой ID Steam) НЕ совпадает с market_hash_name.
Получаем его одноразовым fetch'ем HTML-страницы предмета и кешируем в SQLite.
GID («базовый» ид скина в новом URL'е /market/listings/<app>/<gid>) резолвим
с этой же страницы редиректом — тоже кешируем.

Использование (как минимум):
    from item_info import show_item_info_menu
    await show_item_info_menu(client, "AK-47 | Redline (Field-Tested)", App.CS2,
                              Currency, currency_code)
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp


# =============================================================================
# Резолвинг item_nameid (одноразовый запрос + кеш)
# =============================================================================
_NAMEID_RE = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")
_GID_RE = re.compile(r"/market/listings/\d+/(G[0-9A-Fa-f]+)")

# Локальная база `market_hash_name -> item_nameid`. Steam с обновлением 2026 г.
# убрал `Market_LoadOrderSpread(...)` из публичного HTML листингов — теперь
# nameid из страницы вытащить нельзя. Поэтому держим оффлайн-словарь под рукой
# (`data/cs2_item_id_steam.json`). Файл опциональный: если его нет, просто
# работаем как раньше через cache → HTML.
import pathlib  # noqa: E402  (локальный импорт, чтобы не зашумлять top-level)

_NAMEID_JSON_PATH = pathlib.Path(__file__).parent / "data" / "cs2_item_id_steam.json"
_NAMEID_JSON_CACHE: dict[str, int] | None = None
_NAMEID_JSON_MTIME: float | None = None


def _load_nameid_json() -> dict[str, int]:
    """Лениво загружает `data/cs2_item_id_steam.json` с авто-перезагрузкой.

    Формат файла: плоский dict `market_hash_name → item_nameid`. Файл может
    отсутствовать — тогда возвращаем пустой словарь.
    """
    global _NAMEID_JSON_CACHE, _NAMEID_JSON_MTIME

    try:
        mtime = _NAMEID_JSON_PATH.stat().st_mtime
    except FileNotFoundError:
        _NAMEID_JSON_CACHE = {}
        _NAMEID_JSON_MTIME = None
        return _NAMEID_JSON_CACHE

    if _NAMEID_JSON_CACHE is not None and _NAMEID_JSON_MTIME == mtime:
        return _NAMEID_JSON_CACHE

    try:
        raw = json.loads(_NAMEID_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _NAMEID_JSON_CACHE = {}
        _NAMEID_JSON_MTIME = mtime
        return _NAMEID_JSON_CACHE

    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for name, nid in raw.items():
            if isinstance(name, str) and isinstance(nid, (int, str)):
                try:
                    out[name] = int(nid)
                except (TypeError, ValueError):
                    continue
    _NAMEID_JSON_CACHE = out
    _NAMEID_JSON_MTIME = mtime
    return _NAMEID_JSON_CACHE


async def _fetch_item_page(
    session: aiohttp.ClientSession, app_id: int, market_hash_name: str
) -> tuple[str, str] | None:
    """Скачивает страницу `/market/listings/{app_id}/{name}` Steam'а.

    Возвращает (final_url, html) либо None. final_url важен, потому
    что Steam в новом дизайне делает 301 со старого URL'а на новый
    `/market/listings/{app_id}/G<HEX>` — и оттуда мы вытащиваем GID.
    """
    url = (
        f"https://steamcommunity.com/market/listings/{app_id}/"
        + urllib.parse.quote(market_hash_name, safe="")
    )
    async with session.get(
        url, headers={"Accept-Language": "en-US,en;q=0.9"},
    ) as resp:
        if resp.status != 200:
            return None
        html = await resp.text()
        final_url = str(resp.url)
    return (final_url, html)


async def resolve_item_nameid(
    client, app_id: int, market_hash_name: str
) -> int | None:
    """Возвращает item_nameid с использованием SQLite-кеша.

    Идёт по порядку:
      1) cache.sqlite3,
      2) локальный JSON `data/cs2_item_id_steam.json`
         (после SSR-редизайна 2026 — основной источник),
      3) Steam HTML (legacy путь — практически не срабатывает),
      4) None.

    Если HTML отдал id — пишем в кеш. JSON-попадание тоже кешируется в SQLite,
    чтобы дальше не дёргать диск. Параллельно из HTML вытаскиваем GID (если его
    ещё нет в кеше) — экономим один HTTP-запрос.
    """
    try:
        import cache

        cached = cache.get_cached_nameid(app_id, market_hash_name)
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001
        # Кеш — не критично, продолжаем с сетью.
        pass

    # Шаг 2: локальный JSON (data/cs2_item_id_steam.json).
    json_db = _load_nameid_json()
    nameid = json_db.get(market_hash_name)
    if nameid is not None:
        try:
            import cache

            cache.cache_nameid(app_id, market_hash_name, nameid)
        except Exception:  # noqa: BLE001
            pass
        return nameid

    # Шаг 3: HTML (Steam с 2026 г. убрал nameid из публичного HTML, но оставим
    # на случай если положат обратно или это другой app_id с legacy-страницей).
    page = await _fetch_item_page(client.session, app_id, market_hash_name)
    if page is None:
        return None
    final_url, html = page

    # GID — попутно попробуем положить в кеш, раз уж HTML уже выкачали.
    gid_match = _GID_RE.search(final_url) or _GID_RE.search(html)
    if gid_match:
        try:
            import cache

            cache.cache_gid(app_id, market_hash_name, gid_match.group(1))
        except Exception:  # noqa: BLE001
            pass

    name_match = _NAMEID_RE.search(html)
    if not name_match:
        return None
    nameid = int(name_match.group(1))

    try:
        import cache

        cache.cache_nameid(app_id, market_hash_name, nameid)
    except Exception:  # noqa: BLE001
        pass
    return nameid


async def resolve_gid(
    client, app_id: int, market_hash_name: str
) -> str | None:
    """Резолвит (app_id, market_hash_name) -> GID нового Steam Market 2026.

    GID — базовый ид скина (вида `G[0-9A-Fa-f]+`); один GID группирует все
    экстерьеры/StatTrak/Souvenir-варианты предмета. Получаем редиректом
    со старого URL `/market/listings/<app>/<market_hash_name>` (Steam отвечает
    301 на новый идентификатор).

    Порядок: 1) cache.sqlite3 → 2) Steam HTML → 3) None.
    """
    try:
        import cache

        cached = cache.get_cached_gid(app_id, market_hash_name)
        if cached:
            return cached
    except Exception:  # noqa: BLE001
        pass

    page = await _fetch_item_page(client.session, app_id, market_hash_name)
    if page is None:
        return None
    final_url, html = page
    match = _GID_RE.search(final_url) or _GID_RE.search(html)
    if not match:
        return None
    gid = match.group(1)

    try:
        import cache

        cache.cache_gid(app_id, market_hash_name, gid)
    except Exception:  # noqa: BLE001
        pass
    return gid


# =============================================================================
# ASCII-график для price_history
# =============================================================================
_BLOCKS = " ▁▂▃▄▅▆▇█"  # 9 уровней высоты


@dataclass
class PriceChartSlice:
    label: str  # «7d» / «30d» / «all»
    points: list[Any]  # PriceHistoryEntry-like (.date, .price, .daily_volume)


def _slice_history(history: list, label: str) -> list:
    """Из истории отдаёт нужный отрезок.

    Steam отдаёт точки за последние ~30 дней (по 1 точке/день) + более редкие
    точки старше. `fetch_price_history` уже возвращает их по возрастанию даты.
    """
    if not history:
        return []
    now = datetime.now(timezone.utc)
    if label == "all":
        return history
    if label == "7d":
        cutoff = now.timestamp() - 7 * 24 * 3600
    elif label == "30d":
        cutoff = now.timestamp() - 30 * 24 * 3600
    else:
        return history
    out = []
    for p in history:
        dt = p.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.timestamp() >= cutoff:
            out.append(p)
    return out


def _downsample(values: list[float], width: int) -> tuple[list[float], list[float], list[float]]:
    """Даунсэмплим values до width столбцов. Возвращает (mean, min, max) на сегмент.

    Если len(values) <= width — возвращает значения как mean=min=max=value.
    """
    n = len(values)
    if n <= width:
        return list(values), list(values), list(values)
    step = n / width
    means: list[float] = []
    mins: list[float] = []
    maxs: list[float] = []
    for i in range(width):
        start = int(i * step)
        end = int((i + 1) * step)
        if end <= start:
            end = start + 1
        seg = values[start:end] or [values[start]]
        means.append(sum(seg) / len(seg))
        mins.append(min(seg))
        maxs.append(max(seg))
    return means, mins, maxs


def _render_ascii_chart(
    values: list[float], width: int = 60, height: int = 8
) -> list[str]:
    """Возвращает список строк ASCII-чарта (БЕЗ Y/X-осей).

    Использовать `_render_chart_with_axes` для версии с подписями.
    """
    if not values:
        return ["(нет данных)"]
    # Даунсэмплим до width столбцов.
    n = len(values)
    if n > width:
        values, _, _ = _downsample(values, width)
    elif n < width:
        width = n

    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return ["─" * width, f"{hi:.2f} (без изменений)"]

    rng = hi - lo
    rows: list[list[str]] = [[" "] * width for _ in range(height)]
    for col, v in enumerate(values):
        norm = (v - lo) / rng
        units = norm * (height * 8)
        full_rows = int(units // 8)
        rem = int(units) - full_rows * 8
        for r in range(full_rows):
            rows[height - 1 - r][col] = _BLOCKS[8]
        if rem > 0 and full_rows < height:
            rows[height - 1 - full_rows][col] = _BLOCKS[rem]
    out = ["".join(r) for r in rows]
    out.append(f"  min: {lo:.2f}   max: {hi:.2f}   ({len(values)} точек)")
    return out


_MONTHS_RU = [
    "", "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def _fmt_date_short(dt) -> str:
    """`Май 4` / `Дек 31` для оси X."""
    return f"{_MONTHS_RU[dt.month].capitalize()} {dt.day}"


def _render_chart_with_axes(  # noqa: PLR0912, PLR0915, C901
    points: list,  # PriceHistoryEntry-like
    sym: str,
    width: int = 60,
    height: int = 8,
) -> list[str]:
    """Чарт с Y-осью (цены слева) и X-осью (даты снизу).

    Использует min/max сегмента для тонкого «теневого» рендера — даёт ощущение
    высокого/низкого внутри-сегментного диапазона.
    """
    if not points:
        return ["(нет данных)"]
    values = [float(p.price) for p in points]
    n = len(values)
    # Даунсэмплим, если нужно. Заодно ужимаем даты пропорционально.
    if n > width:
        means, mins, maxs = _downsample(values, width)
        # Даты — берём центр каждого сегмента.
        step = n / width
        date_indexes = [min(n - 1, int((i + 0.5) * step)) for i in range(width)]
        dates = [points[i].date for i in date_indexes]
    else:
        means = list(values)
        mins = list(values)
        maxs = list(values)
        dates = [p.date for p in points]
        width = n

    lo = min(mins)
    hi = max(maxs)
    if hi <= lo:
        # Нет вариации.
        prefix = f"  {hi:.2f} {sym} ".rjust(10)
        return [f"{prefix}|{'─' * width}", f"  (без изменений за период)"]

    rng = hi - lo
    # Готовим сетку.
    grid: list[list[str]] = [[" "] * width for _ in range(height)]

    def _y_for(value: float) -> float:
        """Возвращает «высоту» 0..(height*8) для значения."""
        return (value - lo) / rng * (height * 8)

    for col, mv in enumerate(means):
        units = _y_for(mv)
        full_rows = int(units // 8)
        rem = int(units) - full_rows * 8
        # Mean — полностью заполненная колонка.
        for r in range(full_rows):
            grid[height - 1 - r][col] = _BLOCKS[8]
        if rem > 0 and full_rows < height:
            grid[height - 1 - full_rows][col] = _BLOCKS[rem]
        # Max — обозначаем «верхушку» тонким штрихом «·» там, где её ещё нет.
        max_units = _y_for(maxs[col])
        max_row = height - 1 - int(max_units // 8)
        if 0 <= max_row < height and grid[max_row][col] == " ":
            grid[max_row][col] = "·"

    # Y-axis labels: каждая строка = (lo + rng * (height - row_idx) / height).
    # Печатаем подписи через каждую строку (чтобы цифры не перекрывали).
    y_labels: list[str] = []
    for row in range(height):
        # Центр строки в координатах цены:
        frac = (height - row - 0.5) / height
        val = lo + frac * rng
        if row % 2 == 0:
            y_labels.append(f"{val:>7.2f} {sym}")
        else:
            y_labels.append(" " * (8 + len(sym)))

    chart_lines = [f"{y_labels[row]} │{''.join(grid[row])}" for row in range(height)]
    # Нижняя ось (горизонтальная линия).
    bottom = (" " * (8 + len(sym))) + " └" + ("─" * width)
    chart_lines.append(bottom)

    # X-axis labels.
    if dates:
        # Выберем 5-6 равноотстоящих делений.
        n_ticks = min(6, max(2, width // 12))
        tick_cols = [int(i * (width - 1) / (n_ticks - 1)) for i in range(n_ticks)]
        tick_labels = [(c, _fmt_date_short(dates[c])) for c in tick_cols]
        # Строим строку X-меток.
        line = [" "] * width
        for col, lbl in tick_labels:
            start = col
            # Сдвигаем влево, чтобы метка влезла.
            if start + len(lbl) > width:
                start = width - len(lbl)
            if start < 0:
                start = 0
            for j, ch in enumerate(lbl):
                if 0 <= start + j < width and line[start + j] == " ":
                    line[start + j] = ch
        chart_lines.append((" " * (8 + len(sym))) + "  " + "".join(line))
    # Подытог.
    chart_lines.append(
        f"  min: {lo:.2f} {sym}   max: {hi:.2f} {sym}   ({n} точек, {width} столбцов)"
    )
    return chart_lines


def _sum_volume(history: list, days: float) -> int:
    """Сумма daily_volume по всем точкам не старше `days` дней (от текущего момента).

    Steam отдаёт ~30 свежих дневных точек + более редкие точки старше; для
    «день/неделя/месяц» все нужные точки попадают в дневное окно, поэтому простая
    сумма работает корректно.
    """
    if not history:
        return 0
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - days * 24 * 3600
    total = 0
    for p in history:
        dt = p.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.timestamp() < cutoff:
            continue
        v = getattr(p, "daily_volume", None) or 0
        try:
            total += int(v)
        except (TypeError, ValueError):
            continue
    return total


def render_sales_volume_block(history: list) -> list[str]:
    """Кол-во продаж за день / неделю / месяц (из price_history → daily_volume).

    Это агрегированные сделки Steam Market (то же, что показывает сам Steam
    под графиком цены — но в одном месте и сразу за три периода).
    """
    lines = ["=== Продажи (Steam aggregate) ==="]
    if not history:
        lines.append("  (нет данных истории)")
        return lines
    day = _sum_volume(history, 1)
    week = _sum_volume(history, 7)
    month = _sum_volume(history, 30)
    lines.append(f"  За сутки:   {day} шт.")
    lines.append(f"  За неделю:  {week} шт.")
    lines.append(f"  За месяц:   {month} шт.")
    # Последняя точка — показывает «свежесть» данных Steam'а.
    last = history[-1]
    last_dt = last.date
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    last_date = last_dt.strftime("%Y-%m-%d")
    last_vol = getattr(last, "daily_volume", None) or 0
    try:
        last_vol_i = int(last_vol)
    except (TypeError, ValueError):
        last_vol_i = 0
    lines.append(f"  Последняя дневная точка: {last_date} → {last_vol_i} шт.")
    return lines


def render_price_chart_block(history: list, label: str, sym: str = "") -> list[str]:
    """Готовый блок строк: заголовок + чарт для одного периода + Y/X-оси."""
    points = _slice_history(history, label)
    title_map = {
        "7d": "за неделю",
        "30d": "за месяц",
        "all": "за всё время",
    }
    title = f"=== График цены {title_map.get(label, label)} ({label}) ==="
    if not points:
        return [title, "(нет точек за этот период)"]
    lines = [title]
    lines.extend(_render_chart_with_axes(points, sym=sym or "_", width=60, height=8))
    # Дополнительно — пара цифр.
    if len(points) >= 2:
        first_p = float(points[0].price)
        last_p = float(points[-1].price)
        delta = last_p - first_p
        pct = (delta / first_p * 100) if first_p else 0
        first_date = points[0].date.strftime("%Y-%m-%d")
        last_date = points[-1].date.strftime("%Y-%m-%d")
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"  {first_date}: {first_p:.2f} {sym}  ->  "
            f"{last_date}: {last_p:.2f} {sym}  ({sign}{delta:.2f} / {sign}{pct:.1f}%)"
        )
    return lines


def render_data_table(history: list, label: str, sym: str = "", limit: int = 30) -> list[str]:
    """Сырые точки `дата → цена → объём` за период (хвост — `limit` последних)."""
    points = _slice_history(history, label)
    title_map = {"7d": "неделя", "30d": "месяц", "all": "всё время"}
    if not points:
        return [f"=== Таблица точек ({title_map.get(label, label)}) ===", "(нет точек)"]
    tail = points[-limit:]
    lines = [
        f"=== Таблица точек ({title_map.get(label, label)}); последних {len(tail)} ===",
        f"    {'Дата':<20}{'Цена':>12}    {'Объём':>8}",
    ]
    for p in tail:
        date_str = p.date.strftime("%Y-%m-%d %H:%M")
        price_str = f"{float(p.price):.2f} {sym}"
        vol = getattr(p, "daily_volume", None) or 0
        lines.append(f"    {date_str:<20}{price_str:>12}    {int(vol):>8}")
    return lines


# =============================================================================
# Рендер таблицы топ-листингов и топ-ордеров (histogram)
# =============================================================================
def render_histogram_block(
    histogram, sym: str, max_rows: int = 10
) -> list[str]:
    """Возвращает строки с топ buy- и sell-таблицами.

    histogram — это `ItemOrdersHistogram` от aiosteampy (поля sell_order_table /
    buy_order_table — список NamedTuple'ов price/price_with_fee/quantity).
    """
    lines = ["=== Глубина рынка (histogram) ==="]
    lowest_sell = histogram.lowest_sell_order
    highest_buy = histogram.highest_buy_order
    spread_str = "—"
    if lowest_sell and highest_buy:
        spread = lowest_sell - highest_buy
        spread_str = f"{spread / 100:.2f} {sym} ({spread / lowest_sell * 100:.1f}%)"
    lowest_sell_str = f"{(lowest_sell or 0) / 100:.2f} {sym}"
    highest_buy_str = f"{(highest_buy or 0) / 100:.2f} {sym}"
    lines.append(
        f"  Наименьшее sell: {lowest_sell_str}   "
        f"Наибольшее buy:   {highest_buy_str}   "
        f"Спред: {spread_str}"
    )
    lines.append(
        f"  Всего sell-листингов: {histogram.sell_order_count}   "
        f"buy-ордеров: {histogram.buy_order_count}"
    )

    price_head = "  Цена"
    qty_head = "Q-ty"

    # Sell-table («Sell orders»): цена / комиссия / кол-во.
    lines.append("")
    lines.append("  Продают:")
    lines.append(f"    {price_head:<14}{qty_head:<8}")
    for row in (histogram.sell_order_table or [])[:max_rows]:
        price = getattr(row, "price", None)
        qty = getattr(row, "quantity", None)
        if price is None or qty is None:
            continue
        price_str = f"{price / 100:.2f} {sym}"
        lines.append(f"    {price_str:<14}{qty:<8}")
    if not histogram.sell_order_table:
        lines.append("    (пусто)")

    # Buy-table («Buy orders»): цена / кол-во.
    lines.append("")
    lines.append("  Покупают:")
    lines.append(f"    {price_head:<14}{qty_head:<8}")
    for row in (histogram.buy_order_table or [])[:max_rows]:
        price = getattr(row, "price", None)
        qty = getattr(row, "quantity", None)
        if price is None or qty is None:
            continue
        price_str = f"{price / 100:.2f} {sym}"
        lines.append(f"    {price_str:<14}{qty:<8}")
    if not histogram.buy_order_table:
        lines.append("    (пусто)")
    return lines


def _graph_to_deltas(graph) -> list[tuple[int, int]]:
    """Конвертит `sell_order_graph` / `buy_order_graph` из CUMULATIVE в DELTAS.

    Steam отдаёт graph как [(price, cumulative_qty, repr), ...] до 100 точек
    на сторону. Для отображения «цена → сколько ордеров на ЭТОЙ цене» нам нужны
    дельты между соседними точками.

    Возвращает [(price_cents, qty_at_this_price), ...].
    """
    rows: list[tuple[int, int]] = []
    prev = 0
    for entry in graph or []:
        price = getattr(entry, "price", None)
        cum = getattr(entry, "quantity", None)
        if price is None or cum is None:
            continue
        # graph иногда может прийти в float-долларах через старые версии aiosteampy.
        # Сейчас (0.7+) — int cents. Считаем int → 0.
        price_int = int(price)
        cum_int = int(cum)
        qty = max(0, cum_int - prev)
        prev = cum_int
        if qty > 0:
            rows.append((price_int, qty))
    return rows


def render_full_stack_block(
    histogram, sym: str, *, side: str, limit: int | None = None
) -> list[str]:
    """Печатает полный стакан (до ~100 строк, как у Steam).

    side="sell" — стакан продажи (sell_order_graph).
    side="buy"  — стакан покупки (buy_order_graph).
    Steam отдаёт до 100 точек в графе, мы берём ВСЕ (или `limit`, если задан).
    Source-of-truth — graph, а не table (которая обычно =6-10 строк).
    """
    assert side in ("sell", "buy")
    graph = histogram.sell_order_graph if side == "sell" else histogram.buy_order_graph
    total_count = histogram.sell_order_count if side == "sell" else histogram.buy_order_count
    side_label = "Продают" if side == "sell" else "Покупают"
    deltas = _graph_to_deltas(graph)
    if side == "buy":
        # buy: graph упорядочен от высокой цены к низкой. Так оставляем — самая
        # выгодная для продавца сверху.
        pass
    else:
        # sell: graph упорядочен от низкой цены к высокой. Тоже оставляем —
        # самые дешёвые сверху, как у Steam.
        pass
    if limit is not None and limit > 0:
        deltas = deltas[:limit]
    lines = [f"=== Полный {side_label.lower()}-стакан (max ~100 строк от Steam) ==="]
    lines.append(
        f"  Всего: {total_count} ордеров, показываем уникальных цен: {len(deltas)}."
    )
    price_head = "  Цена"
    qty_head = "Q-ty"
    cum_head = "Σ (cum.)"
    lines.append(f"    {price_head:<14}{qty_head:<8}{cum_head:<10}")
    cum = 0
    for price, qty in deltas:
        cum += qty
        price_str = f"{price / 100:.2f} {sym}"
        lines.append(f"    {price_str:<14}{qty:<8}{cum:<10}")
    if not deltas:
        lines.append("    (пусто)")
    return lines


# =============================================================================
# Listings viewer (POST /market/listings/<app>/<GID> — Steam Market 2026 v2)
# =============================================================================
# Старый GET `/render/`-эндпоинт убран Steam'ом 2025-11. Теперь это POST
# на тот же URL (с GID вместо market_hash_name), тело — JSON-массив с одним
# объектом {appid, strItemName, sort, filters, propertyFilters, start}, ответ
# уже содержит float (propertyid=2) и paint_seed (propertyid=1) — CSFloat
# больше не нужен. Полная спецификация в reports/steam_market_v2_api.md.

# Page size зашит у Steam'а в 20 — увеличить нельзя (отбивает 400). Если
# нужно больше — пагинируем через start += LISTINGS_PAGE_SIZE.
LISTINGS_PAGE_SIZE = 20

# Заголовки маркета 2026 — без них Steam отвечает 400 «Invalid action type».
# Action token "4OPT6VBA:Search" — стабильный route-hash для POST /listings/.
_MARKET_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Valve-Action-Type": "4OPT6VBA:Search",
    "X-Valve-Request-Type": "routeAction",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# CS2 categorical-фильтры (левая панель UI на listings-странице).
# Ключи попадают в JSON-тело как `filters`, значения — массив тэгов (OR).
# Полный список тэгов берётся из `facets` ответа, но эти — стабильные.
EXTERIOR_TAGS = {
    "fn": "tag_WearCategory0",  # Factory New
    "mw": "tag_WearCategory1",  # Minimal Wear
    "ft": "tag_WearCategory2",  # Field-Tested
    "ww": "tag_WearCategory3",  # Well-Worn
    "bs": "tag_WearCategory4",  # Battle-Scarred
}
QUALITY_TAGS = {
    "normal": "tag_normal",       # обычный (без StatTrak/Souvenir)
    "stattrak": "tag_strange",    # StatTrak™
    "st": "tag_strange",
    "souvenir": "tag_tournament", # Souvenir
    "sv": "tag_tournament",
}

# Маппинг человеческого имени экстерьера (как пишется в Steam в скобках, англ.) →
# тэг фильтра. Используется для автодетекта экстерьера из market_hash_name.
_EXTERIOR_BY_SUFFIX: dict[str, str] = {
    "Factory New":    "tag_WearCategory0",
    "Minimal Wear":   "tag_WearCategory1",
    "Field-Tested":   "tag_WearCategory2",
    "Well-Worn":      "tag_WearCategory3",
    "Battle-Scarred": "tag_WearCategory4",
}


def _default_filters_from_name(
    market_hash_name: str,
) -> tuple[list[str], list[str]]:
    """По имени предмета подбирает разумные фильтры по умолчанию.

    Возвращает (quality_tags, exterior_tags). Логика:
    - StatTrak™ <name> → quality = StatTrak™ (`tag_strange`).
    - Souvenir <name>  → quality = Souvenir (`tag_tournament`).
    - Иначе → quality = Normal (`tag_normal`).
    - Если в скобках указан экстерьер (Field-Tested и т.п.) — добавляем
      соответствующий exterior-тэг.

    Если экстерьера в имени нет (контейнеры, наклейки, кейсы и пр.) — список
    exterior_tags пустой, и фильтр по экстерьеру не накладывается.
    """
    name = market_hash_name.strip()

    if name.startswith("StatTrak\u2122 ") or name.startswith("StatTrak "):
        quality_tags = ["tag_strange"]
    elif name.startswith("Souvenir "):
        quality_tags = ["tag_tournament"]
    else:
        quality_tags = ["tag_normal"]

    exterior_tags: list[str] = []
    m = re.search(r"\(([^()]+)\)\s*$", name)
    if m:
        suffix = m.group(1).strip()
        tag = _EXTERIOR_BY_SUFFIX.get(suffix)
        if tag:
            exterior_tags = [tag]

    return quality_tags, exterior_tags


async def _fetch_listings_page(  # noqa: PLR0913
    session: aiohttp.ClientSession,
    app_id: int,
    gid: str,
    *,
    start: int,
    sort_field: int = 0,
    sort_dir: int = 0,
    category_filters: dict[str, list[str]] | None = None,
    wear_range: tuple[float, float] | None = None,
    seed_range: tuple[int, int] | None = None,
    price_range: tuple[int, int] | None = None,
    text_query: str | None = None,
    currency_code: int | None = None,
) -> dict | None:
    """POST `/market/listings/{app_id}/{GID}` — новый Steam Market 2026 API.

    Возвращает разобранный JSON (с ключами `listings`, `total_count`, `more`,
    `facets`) либо None при ошибке.

    Параметры
    ---------
    sort_field=0 — сортировка по цене (единственный режим, который умеет Steam).
    sort_dir=0|1 — направление: 0 = по возрастанию (дефолт), 1 = по убыванию.
    category_filters — {"category_730_Quality": ["tag_normal"], …}.
        Ключи: category_730_{Exterior,Quality,Type,Rarity,Weapon,
        Tournament,TournamentTeam,ProPlayer,ItemSet,Sticker,Charm}.
        Значения — массив тэгов (OR). Полный список в `facets` ответа.
    wear_range=(0.15, 0.20) — диапазон флоата (propertyid=2).
    seed_range=(100, 200) — диапазон paint seed (propertyid=1).
        ! Steam хочет именно строки в int_min/int_max — иначе вернёт листинги
        с seed=0. Преобразуем здесь автоматически.
    price_range=(unMin_cents, unMax_cents) — цены в центах, включительные.
    text_query — полнотекстовый поиск по market_hash_name.
    currency_code — eCurrency. Если None, Steam возьмёт из cookies сессии.
    """
    url = (
        f"https://steamcommunity.com/market/listings/{app_id}/"
        + urllib.parse.quote(gid, safe="")
    )

    property_filters: dict[str, dict] = {}
    if seed_range is not None:
        lo, hi = seed_range
        property_filters["1"] = {
            "property_id": 1,
            "int_min": str(int(lo)),
            "int_max": str(int(hi)),
        }
    if wear_range is not None:
        lo, hi = wear_range
        property_filters["2"] = {
            "property_id": 2,
            "float_min": float(lo),
            "float_max": float(hi),
        }

    body_obj: dict[str, Any] = {
        "appid": int(app_id),
        "strItemName": gid,
        "sort": {"field": int(sort_field), "direction": int(sort_dir)},
        "filters": category_filters or {},
        "accessoryFilters": {},
        "propertyFilters": property_filters,
        "start": int(start),
    }
    if price_range is not None:
        body_obj["price"] = {
            "eCurrency": int(currency_code or 1),
            "unMin": int(price_range[0]),
            "unMax": int(price_range[1]),
        }
    if text_query:
        body_obj["strQuery"] = text_query

    body = json.dumps([body_obj]).encode("utf-8")
    try:
        async with session.post(
            url, data=body, headers=_MARKET_HEADERS,
        ) as resp:
            if resp.status != 200:
                return None
            try:
                return await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                return None
    except aiohttp.ClientError:
        return None


def _parse_listings_v2(data: dict) -> list[dict]:
    """Из ответа нового POST-эндпоинта достаёт список листингов.

    Каждый элемент: {listing_id, asset_id, price_cents, float, paint_seed,
    inspect_url, market_hash_name}. Float и seed теперь приходят сразу — для
    CS2 они лежат в `asset.asset_properties` (propertyid=1 → seed,
    propertyid=2 → wear, propertyid=6 → d-value для inspect-URL).
    """
    out: list[dict] = []
    for li in (data or {}).get("listings") or []:
        try:
            asset = li["asset"]
            asset_id = str(asset["id"])
            listing_id = str(li["listingid"])
            # Цена: unPricePerUnit + unFeePerUnit (оба в центах).
            try:
                price_cents = (
                    int(li.get("unPricePerUnit") or 0)
                    + int(li.get("unFeePerUnit") or 0)
                )
            except (TypeError, ValueError):
                price_cents = 0

            seed: int | None = None
            wear: float | None = None
            d_value: str | None = None
            for prop in asset.get("asset_properties") or []:
                pid = prop.get("propertyid")
                if pid == 1 and "int_value" in prop:
                    try:
                        seed = int(prop["int_value"])
                    except (TypeError, ValueError):
                        pass
                elif pid == 2 and "float_value" in prop:
                    try:
                        wear = float(prop["float_value"])
                    except (TypeError, ValueError):
                        pass
                elif pid == 6 and "string_value" in prop:
                    d_value = str(prop["string_value"])

            descr = li.get("description") or {}
            actions = descr.get("market_actions") or []
            inspect = ""
            if actions:
                tpl = actions[0].get("link") or ""
                if tpl and d_value:
                    inspect = (
                        tpl.replace("%propid:6%", d_value)
                        # На случай legacy-шаблона %listingid%/%assetid%.
                        .replace("%listingid%", listing_id)
                        .replace("%assetid%", asset_id)
                    )

            out.append({
                "listing_id": listing_id,
                "asset_id": asset_id,
                "price_cents": price_cents,
                "float": wear,
                "paint_seed": seed,
                "inspect_url": inspect,
                "market_hash_name": descr.get("market_hash_name") or "",
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def render_listings_page(
    listings: list[dict],
    *,
    sym: str,
    start_idx: int,
    total: int,
    market_hash_name: str | None = None,
    floats: dict[str, tuple[float | None, int | None]] | None = None,  # noqa: ARG001
) -> list[str]:
    """Рисует страницу листингов.

    Float/seed теперь приходят прямо в листинге из Steam Market v2 — рисуем
    их всегда (если у предмета их нет, печатаем «—»). Параметр `floats`
    оставлен для обратной совместимости вызывающего кода, но игнорируется.

    Если задан `market_hash_name` — каждая строка с paint_seed проверяется через
    `patterns.is_rare_pattern`; редкие листинги помечаются «★» в первой колонке
    и перечисляются ниже отдельным блоком с указанием тира.
    """
    # Импорт здесь, а не на верхнем уровне, чтобы избежать циклической зависимости
    # и чтобы модуль `patterns` оставался опциональным (если файла нет — рендер
    # листингов всё равно работает).
    rare_marks: dict[int, tuple[str, int]] = {}
    if market_hash_name:
        try:
            import patterns  # type: ignore[import-not-found]

            for idx, item in enumerate(listings):
                sd = item.get("paint_seed")
                if isinstance(sd, int):
                    res = patterns.is_rare_pattern(market_hash_name, sd)
                    if res.is_rare is True:
                        rare_marks[idx] = (res.tier_note or "?", sd)
        except Exception:  # noqa: BLE001
            rare_marks = {}

    lines = []
    lines.append("=" * 88)
    lines.append(
        f"   Листинги ({start_idx + 1}..{start_idx + len(listings)} из {total})"
    )
    lines.append("=" * 88)
    lines.append(
        f"     {'#':<4}{'Цена':<14}{'Float':<10}{'Seed':<7}"
        f"{'listing_id':<22}asset_id"
    )
    lines.append("-" * 88)
    for i, item in enumerate(listings, start_idx + 1):
        idx_zero = i - start_idx - 1
        price_str = f"{item['price_cents'] / 100:.2f} {sym}"
        fl = item.get("float")
        sd = item.get("paint_seed")
        fl_str = f"{fl:.4f}" if isinstance(fl, (int, float)) else "—"
        sd_str = str(sd) if isinstance(sd, int) else "—"
        marker = "★" if idx_zero in rare_marks else " "
        lines.append(
            f"   {marker} {i:<4}{price_str:<14}{fl_str:<10}{sd_str:<7}"
            f"{item['listing_id']:<22}{item['asset_id']}"
        )
    lines.append("-" * 88)
    if rare_marks:
        lines.append("   ★ — редкий паттерн:")
        for idx, (tier, sd) in sorted(rare_marks.items()):
            row_num = start_idx + idx + 1
            lines.append(f"       #{row_num}: seed={sd} ({tier})")
    return lines


# =============================================================================
# Главное меню «Инфо о предмете»
# =============================================================================
async def show_item_info_menu(  # noqa: PLR0912, PLR0915, C901
    client,
    market_hash_name: str,
    app,  # aiosteampy.constants.App
    currency_enum,
    currency_code: int,
    *,
    ask=None,
    currency_sym: str = "",
) -> None:
    """CLI: показывает histogram + график цены, даёт переключать период графика.

    `ask` — асинхронная функция запроса строки от пользователя (как `_ask` в
    simple.py). Если None — функция отрисует один раз все 3 периода и вернётся.
    """
    print(f"\n=== ИНФОРМАЦИЯ ПО ПРЕДМЕТУ: {market_hash_name} ===")
    print("[..] Резолвлю item_nameid (из кеша или со страницы Steam) ...")
    app_id = int(app)
    nameid = await resolve_item_nameid(client, app_id, market_hash_name)
    histogram = None
    if nameid is None:
        # Steam c обновлением 2026 г. удалил `Market_LoadOrderSpread(...)` (и любую
        # другую упоминалку item_nameid) из публичного HTML листингов — теперь
        # клиент рендерит histogram через дополнительный auth-запрос, и без
        # авторизованного nameid публично получить топ buy/sell нельзя. Если в
        # cache.sqlite3 для этого предмета ничего не лежит, мы сюда и попадаем.
        # Не прерываемся — показываем то, что доступно: график цен и листинги.
        print(
            "   [!] item_nameid недоступен.\n"
            "       Не нашёл его ни в cache.sqlite3, ни в "
            "data/cs2_item_id_steam.json, ни в HTML Steam'а (после SSR-редизайна\n"
            "       Steam убрал nameid из публичных страниц).\n"
            "       Топ buy/sell-стакан в этой версии не покажу — "
            "но история цен и листинги ('f') работают."
        )
    else:
        print(f"   item_nameid = {nameid}.")
        print("[..] Гружу histogram (топ buy/sell) ...")
        try:
            histogram, _ = await client.get_item_orders_histogram(nameid)
        except Exception as exc:  # noqa: BLE001
            print(f"   [!] Histogram не загружен: {type(exc).__name__}: {exc}")
            histogram = None

    print("[..] Гружу историю цен (rate-limited Steam'ом) ...")
    try:
        history = await client.fetch_price_history(market_hash_name, app)
    except Exception as exc:  # noqa: BLE001
        # `fetch_price_history` доступен только если предмет хоть раз был
        # у тебя в инвентаре / в покупках. Steam возвращает 500/400 иначе.
        print(
            f"   [!] История цен недоступна "
            f"({type(exc).__name__}: {exc}). "
            "Это ОК, если предмет никогда не был в инвентаре этого акка. "
            "Histogram всё равно покажем."
        )
        history = []

    # Печатаем histogram.
    if histogram is not None:
        for line in render_histogram_block(histogram, currency_sym or "_"):
            print(line)

    sym = currency_sym or ""

    if not history:
        # Истории нет — графики/счётчики продаж пустые, но листинги ('f')
        # всё ещё работают: они тянутся напрямую через POST-эндпоинт, ему
        # ни nameid, ни истории не нужно. Предлагаем мини-меню вместо выхода.
        print("\n(без истории цен графики и счётчики продаж пусты)")
        if ask is None:
            return
        while True:
            cmd = (await ask(
                "  команды: f=листинги с флоатами / Enter=выход: "
            )).strip().lower()
            if cmd == "":
                return
            if cmd.startswith("f"):
                await _show_listings_with_floats(
                    client, app_id, market_hash_name, currency_code, sym, ask,
                )
            else:
                print(f"  (не понял «{cmd}»)")

    # Сводка продаж за день/неделю/месяц — печатается один раз перед графиком,
    # потому что цифры одинаковые для всех периодов (день/неделя/месяц считаются
    # из тех же daily_volume).
    print()
    for line in render_sales_volume_block(history):
        print(line)

    if ask is None:
        # Не интерактивно — печатаем все 3 периода один раз.
        for mode in ("7d", "30d", "all"):
            print()
            for line in render_price_chart_block(history, mode, sym):
                print(line)
        return

    # Интерактив: пользователь переключает период / разворачивает стаканы.
    current = "30d"
    while True:
        print()
        for line in render_price_chart_block(history, current, sym):
            print(line)
        print()
        cmd = (
            await ask(
                "  команды: 7=неделя / 30=месяц / a=всё / "
                "t=таблица точек / s=полный sell-стакан / b=полный buy-стакан / "
                "f=листинги с флоатами / "
                "Enter=выход: "
            )
        ).strip().lower()
        if cmd == "":
            return
        if cmd in ("7", "7d", "w", "week"):
            current = "7d"
        elif cmd in ("30", "30d", "m", "month"):
            current = "30d"
        elif cmd in ("a", "all"):
            current = "all"
        elif cmd in ("t", "table", "т", "таблица"):
            print()
            for line in render_data_table(history, current, sym):
                print(line)
            # Не меняем график — просто продолжаем цикл (он перерисует тот же
            # период следующей итерацией). Притормозим для удобства.
            await ask("  Enter — продолжить: ")
        elif cmd.startswith("s") and histogram is not None:
            # `s` — полный sell-стакан (через sell_order_graph, до ~100 строк);
            # `s 30` — первые 30. Старая `_table` всегда содержит только ~6-10
            # строк (это для preview), а `_graph` — реальный стакан до 100.
            parts = cmd.split()
            limit = None
            if len(parts) >= 2 and parts[1].isdigit():
                limit = int(parts[1])
            print()
            for line in render_full_stack_block(
                histogram, sym or "_", side="sell", limit=limit
            ):
                print(line)
            await ask("  Enter — продолжить: ")
        elif cmd.startswith("b") and histogram is not None:
            parts = cmd.split()
            limit = None
            if len(parts) >= 2 and parts[1].isdigit():
                limit = int(parts[1])
            print()
            for line in render_full_stack_block(
                histogram, sym or "_", side="buy", limit=limit
            ):
                print(line)
            await ask("  Enter — продолжить: ")
        elif cmd.startswith("f"):
            # Floats viewer — листинги с inspect-link'ами.
            # Размер страницы теперь зашит у Steam (20 шт.), параметр игнорируется.
            await _show_listings_with_floats(
                client, app_id, market_hash_name, currency_code, sym, ask,
            )
        else:
            print(f"  (не понял «{cmd}»)")


async def _show_listings_with_floats(  # noqa: PLR0912, PLR0915, C901
    client, app_id: int, market_hash_name: str, currency_code: int,
    sym: str, ask,
) -> None:
    """Просмотр выставленных листингов: цена + float + paint_seed + inspect-URL.

    Все фильтры серверные — Steam Market v2 возвращает float/seed сразу в ответе
    и умеет ограничивать выдачу по любому из них. CSFloat больше не нужен.

    Команды внутри:
        h / help            — показать подробную справку
        n / Enter           — следующая страница
        p                   — предыдущая
        flt 0.15 0.20       — фильтр по флоату (диапазон)
        flt off             — сбросить фильтр по флоату
        seed 100 200        — фильтр по paint seed
        seed 661            — точный seed
        seed off            — сбросить
        q normal/st/sv      — фильтр по качеству (Normal / StatTrak™ / Souvenir)
        q off               — сбросить
        ext fn/mw/ft/ww/bs  — фильтр по экстерьеру (можно через запятую: ext ft,mw)
        ext off             — сбросить
        clear               — сбросить все фильтры
        q / Enter (пустой)  — выход
    """
    # Page size зашит у Steam — игнорируем параметр (оставлен для backward-compat).
    eff_page = LISTINGS_PAGE_SIZE

    print(f"\n=== ЛИСТИНГИ: {market_hash_name} ===")
    print("   [..] Резолвлю GID …")
    gid = await resolve_gid(client, app_id, market_hash_name)
    if not gid:
        print("   [ERR] Не смог получить GID — Steam не вернул новый id-страницу.")
        return
    print(f"   GID = {gid}.  Размер страницы: {eff_page} (Steam-fixed).")
    print("   float/seed приходят сразу — CSFloat не нужен.")
    print("   Введи 'h' для справки.")

    start = 0
    page_listings: list[dict] = []
    total = 0
    loaded_key: tuple | None = None  # ключ кеша = (start, фильтры)

    # Состояние фильтров. По умолчанию: Quality=Normal + экстерьер из имени
    # (например, имя «PP-Bizon | RMX (Field-Tested)» → ext=Field-Tested).
    # Пользователь может всё это снять через `clear` / `q off` / `ext off`.
    wear_range: tuple[float, float] | None = None
    seed_range: tuple[int, int] | None = None
    quality_tags, exterior_tags = _default_filters_from_name(market_hash_name)

    def _category_filters() -> dict[str, list[str]]:
        f: dict[str, list[str]] = {}
        if exterior_tags:
            f["category_730_Exterior"] = list(exterior_tags)
        if quality_tags:
            f["category_730_Quality"] = list(quality_tags)
        return f

    def _filter_summary() -> str:
        bits: list[str] = []
        if wear_range:
            bits.append(f"flt={wear_range[0]:.4f}..{wear_range[1]:.4f}")
        if seed_range:
            bits.append(
                f"seed={seed_range[0]}"
                if seed_range[0] == seed_range[1]
                else f"seed={seed_range[0]}..{seed_range[1]}"
            )
        if quality_tags:
            human = {v: k for k, v in QUALITY_TAGS.items()}
            bits.append("q=" + ",".join(human.get(t, t) for t in quality_tags))
        if exterior_tags:
            human = {v: k for k, v in EXTERIOR_TAGS.items()}
            bits.append("ext=" + ",".join(human.get(t, t) for t in exterior_tags))
        return " | ".join(bits) if bits else "—"

    def _filter_key() -> tuple:
        return (
            wear_range,
            seed_range,
            tuple(quality_tags),
            tuple(exterior_tags),
        )

    def _help():
        print(
            "\n  СПРАВКА:\n"
            "    n / Enter            — следующая страница\n"
            "    p                    — предыдущая\n"
            "    flt 0.15 0.20        — фильтр по флоату (диапазон)\n"
            "    flt off              — сбросить фильтр по флоату\n"
            "    seed 100 200         — фильтр по paint seed\n"
            "    seed 661             — точное значение\n"
            "    seed off             — сбросить\n"
            "    q normal/st/sv       — качество (Normal / StatTrak™ / Souvenir);\n"
            "                            можно через запятую: q st,normal\n"
            "    q off                — сбросить\n"
            "    ext fn/mw/ft/ww/bs   — экстерьер; можно через запятую: ext ft,mw\n"
            "    ext off              — сбросить\n"
            "    clear                — сбросить все фильтры\n"
            "    q / Enter (пустой)   — выход"
        )

    while True:
        cur_key = (start, _filter_key())
        if loaded_key != cur_key:
            print(f"\n[..] Гружу листинги: start={start} ({_filter_summary()}) …")
            data = await _fetch_listings_page(
                client.session, app_id, gid,
                start=start,
                category_filters=_category_filters() or None,
                wear_range=wear_range,
                seed_range=seed_range,
                currency_code=currency_code,
            )
            if data is None:
                print("   [ERR] Не смог загрузить страницу листингов.")
                return
            total = int(data.get("total_count") or 0)
            page_listings = _parse_listings_v2(data)
            loaded_key = cur_key

        if not page_listings:
            print(
                f"\n(на этой странице нет листингов; фильтры: {_filter_summary()})"
            )
        else:
            for line in render_listings_page(
                page_listings, sym=sym, start_idx=start, total=total,
                market_hash_name=market_hash_name,
            ):
                print(line)

        page_num = start // eff_page + 1
        total_pages = max(1, (total + eff_page - 1) // eff_page)
        cmd = (await ask(
            f"  n=след / p=пред / flt X Y / seed A B / q normal|st|sv / "
            f"ext fn|mw|ft|ww|bs / clear / h=справка / Enter=выход "
            f"(стр. {page_num}/{total_pages}, фильтры: {_filter_summary()}): "
        )).strip().lower()

        if cmd in ("", "exit", "quit"):
            return
        if cmd in ("h", "help", "?"):
            _help()
            continue
        if cmd in ("n", "next"):
            new_start = start + eff_page
            if new_start >= total:
                print("  (это последняя страница)")
                continue
            start = new_start
        elif cmd in ("p", "prev"):
            new_start = max(0, start - eff_page)
            if new_start == start:
                print("  (это первая страница)")
                continue
            start = new_start
        elif cmd == "clear":
            wear_range = None
            seed_range = None
            quality_tags = []
            exterior_tags = []
            start = 0
            print("  [filter] все фильтры сброшены.")
        elif cmd.startswith("flt"):
            arg = cmd[3:].strip()
            if arg in ("off", "reset", "", "none"):
                wear_range = None
                start = 0
                print("  [filter] flt сброшен.")
                continue
            parts = arg.replace(",", " ").split()
            try:
                if len(parts) == 1:
                    hi = float(parts[0])
                    lo = 0.0
                elif len(parts) == 2:
                    lo, hi = float(parts[0]), float(parts[1])
                else:
                    raise ValueError("ожидаю 1 или 2 числа")
                if not (0.0 <= lo <= hi <= 1.0):
                    print("  flt: 0 ≤ lo ≤ hi ≤ 1, попробуй ещё раз.")
                    continue
                wear_range = (lo, hi)
                start = 0
                print(f"  [filter] flt={lo:.4f}..{hi:.4f}")
            except ValueError as exc:
                print(f"  flt: не понял «{arg}» ({exc}). Пример: flt 0.15 0.20")
        elif cmd.startswith("seed"):
            arg = cmd[4:].strip()
            if arg in ("off", "reset", "", "none"):
                seed_range = None
                start = 0
                print("  [filter] seed сброшен.")
                continue
            parts = arg.replace(",", " ").split()
            try:
                if len(parts) == 1:
                    v = int(parts[0])
                    if not 0 <= v <= 1000:
                        print("  seed: 0..1000.")
                        continue
                    seed_range = (v, v)
                elif len(parts) == 2:
                    lo, hi = int(parts[0]), int(parts[1])
                    if not (0 <= lo <= hi <= 1000):
                        print("  seed: 0 ≤ lo ≤ hi ≤ 1000.")
                        continue
                    seed_range = (lo, hi)
                else:
                    raise ValueError("ожидаю 1 или 2 целых числа")
                start = 0
                print(f"  [filter] seed={seed_range[0]}..{seed_range[1]}")
            except ValueError as exc:
                print(f"  seed: не понял «{arg}» ({exc}). Пример: seed 100 200")
        elif cmd.startswith("q "):
            arg = cmd[2:].strip()
            if arg in ("off", "reset", "", "none"):
                quality_tags = []
                start = 0
                print("  [filter] quality сброшен.")
                continue
            parts = [p.strip() for p in arg.replace(",", " ").split() if p.strip()]
            tags: list[str] = []
            for p in parts:
                t = QUALITY_TAGS.get(p)
                if not t:
                    print(f"  q: незнакомое значение «{p}». "
                          f"Допустимо: {', '.join(sorted(QUALITY_TAGS))}.")
                    tags = []
                    break
                if t not in tags:
                    tags.append(t)
            if tags:
                quality_tags = tags
                start = 0
                print(f"  [filter] quality = {tags}")
        elif cmd.startswith("ext"):
            arg = cmd[3:].strip()
            if arg in ("off", "reset", "", "none"):
                exterior_tags = []
                start = 0
                print("  [filter] exterior сброшен.")
                continue
            parts = [p.strip() for p in arg.replace(",", " ").split() if p.strip()]
            tags = []
            for p in parts:
                t = EXTERIOR_TAGS.get(p)
                if not t:
                    print(f"  ext: незнакомое значение «{p}». "
                          f"Допустимо: {', '.join(EXTERIOR_TAGS)}.")
                    tags = []
                    break
                if t not in tags:
                    tags.append(t)
            if tags:
                exterior_tags = tags
                start = 0
                print(f"  [filter] exterior = {tags}")
        elif cmd in ("q", "quit"):
            return
        else:
            print(f"  (не понял «{cmd}» — введи 'h' для справки)")