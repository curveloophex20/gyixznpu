"""SQLite-кеш данных по всем аккаунтам.

Идея:
- Один файл `data/cache.sqlite3` рядом со скриптом — сюда складываются снимки
  балансов / ордеров / листингов / истории маркета / инвентарей по всем
  аккаунтам, по которым ты заходил.
- На каждый ресурс пишется `last_refresh_at` (timestamp последнего обновления).
- Историю маркета пишем append-only с уникальным `event_id`, чтобы при повторном
  ручном чеке старые события не дублировались, а только добавились новые.
- Listings / orders / inventory — переписываем «в лоб» при каждом обновлении
  (для конкретного username), потому что то, что пропало — пропало.

Публичный API:
    open_db() -> sqlite3.Connection
    record_account(username, label)
    record_balance(username, balance_cents, on_hold_cents, currency_code)
    record_listings(username, listings_iter, currency_code, partial=True)
    record_buy_orders(username, orders_iter, currency_code)
    record_history_events(username, events_iter, currency_code) -> int (added)
    record_inventory(username, app_context_name, items_iter, partial=True)
    get_last_refresh(username, resource) -> datetime | None
    iter_account_summaries() -> list[dict]
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "cache.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username        TEXT PRIMARY KEY,
    label           TEXT,
    label_num       INTEGER,    -- если label парсится как int — кладём сюда для сортировки
    last_seen_at    TEXT
);

CREATE TABLE IF NOT EXISTS wallet_snapshots (
    username        TEXT NOT NULL,
    snapshot_at     TEXT NOT NULL,
    balance_cents   INTEGER,
    on_hold_cents   INTEGER,
    currency_code   INTEGER,
    PRIMARY KEY (username, snapshot_at)
);

CREATE TABLE IF NOT EXISTS listings_cache (
    username        TEXT NOT NULL,
    listing_id      TEXT NOT NULL,
    asset_id        TEXT,    -- asset_id предмета на листинге (Steam теперь оставляет
                             -- его в инвентаре, поэтому нужно для cross-ref)
    unowned_id      TEXT,    -- оригинальный asset_id до выставления
    market_hash_name TEXT,
    price_cents     INTEGER,
    currency_code   INTEGER,
    time_created    TEXT,
    last_updated_at TEXT,
    PRIMARY KEY (username, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_username_name
    ON listings_cache(username, market_hash_name);

CREATE TABLE IF NOT EXISTS buy_orders_cache (
    username        TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    market_hash_name TEXT,
    price_cents     INTEGER,
    qty_remaining   INTEGER,
    qty_total       INTEGER,
    currency_code   INTEGER,
    last_updated_at TEXT,
    PRIMARY KEY (username, order_id)
);

CREATE TABLE IF NOT EXISTS market_history (
    username        TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    event_type      TEXT,
    market_hash_name TEXT,
    time_event      TEXT,
    price_cents     INTEGER,
    currency_code   INTEGER,
    raw_json        TEXT,
    PRIMARY KEY (username, event_id)
);
CREATE INDEX IF NOT EXISTS idx_history_username_time
    ON market_history(username, time_event DESC);

CREATE TABLE IF NOT EXISTS inventory_cache (
    username        TEXT NOT NULL,
    app_context     TEXT NOT NULL,
    asset_id        TEXT NOT NULL,
    market_hash_name TEXT,
    amount          INTEGER,
    paint_seed      INTEGER,
    paint_wear      REAL,
    extra_json      TEXT,    -- raw asset_properties + description-entries JSON,
                             -- удобно дебажить чармы/стикеры/новые поля Steam
    last_updated_at TEXT,
    PRIMARY KEY (username, app_context, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_username_name
    ON inventory_cache(username, market_hash_name);

CREATE TABLE IF NOT EXISTS refresh_log (
    username        TEXT NOT NULL,
    resource        TEXT NOT NULL,
    last_refresh_at TEXT,
    extra_json      TEXT,
    PRIMARY KEY (username, resource)
);

-- Маппинг (app_id, market_hash_name) → item_nameid (внутренний числовой ID
-- Steam, нужен для get_item_orders_histogram). Получаем парсингом
-- HTML-страницы /market/listings/<app>/<name>; кешируем чтобы не дёргать
-- Steam каждый раз — id у предмета не меняется.
CREATE TABLE IF NOT EXISTS market_nameids (
    app_id              INTEGER NOT NULL,
    market_hash_name    TEXT    NOT NULL,
    item_nameid         INTEGER NOT NULL,
    fetched_at          TEXT,
    PRIMARY KEY (app_id, market_hash_name)
);

-- Маппинг (app_id, market_hash_name) → GID нового Steam Market 2026.
-- GID — базовый идентификатор скина (без учёта wear/StatTrak/
-- Souvenir): один GID группирует все экстерьеры/варианты. Получаем
-- 301-редиректом со старого URL /market/listings/<app>/<name>;
-- кешируем — GID стабилен.
CREATE TABLE IF NOT EXISTS market_gids (
    app_id              INTEGER NOT NULL,
    market_hash_name    TEXT    NOT NULL,
    gid                 TEXT    NOT NULL,
    fetched_at          TEXT,
    PRIMARY KEY (app_id, market_hash_name)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    return None


# Миграции: (table, column, ddl_column_def). Накатываем `ALTER TABLE ... ADD COLUMN`
# только если столбца ещё нет (через PRAGMA table_info). Так мы не зависим от
# отлова `sqlite3.OperationalError: duplicate column name` — IDE-шный дебаггер
# всё равно бьёт breakpoint на raise, даже если мы потом исключение глотаем.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # Numeric ordering field for the account picker — populated from account.json `label`
    # if it parses as int. NULL → sorted last.
    ("accounts", "label_num", "INTEGER"),
    # asset_id / unowned_id для cross-ref «предмет уже на листинге» с инвентарём.
    ("listings_cache", "asset_id", "TEXT"),
    ("listings_cache", "unowned_id", "TEXT"),
    # extra_json со старых БД мог быть удалён прошлым миграционным шагом — вернём.
    ("inventory_cache", "extra_json", "TEXT"),
    # state предмета — нужно для группировки cross-account stats:
    # "free" / "on_market" / "trade_protect" / "trade_hold".
    ("inventory_cache", "state", "TEXT"),
    # tradable_after (ISO) — для trade_hold/trade_protect показать таймер.
    ("inventory_cache", "tradable_after", "TEXT"),
    # steam_id_64 — нужен чтобы фетчить публичный инвентарь акка для diff'a
    # (предмет в нашей выдаче — но не в публичной = «недавно разлочен»).
    ("accounts", "steam_id_64", "TEXT"),
    # hidden_from_public — bool-флаг (0/1). 1 = предмет в нашем приватном инвентаре,
    # но НЕ виден в публичной выдаче (display cooldown ~3 дня после разлока).
    ("inventory_cache", "hidden_from_public", "INTEGER"),
    # last_public_check_at — ISO время последнего сравнения с публичным инвентарём.
    ("inventory_cache", "last_public_check_at", "TEXT"),
]


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True если в `table` уже есть столбец `column` (PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # row format: (cid, name, type, notnull, dflt_value, pk)
    return any(r[1] == column for r in rows)


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if _table_has_column(conn, table, column):
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError:
            # На случай гонки — между PRAGMA и ALTER кто-то уже добавил столбец.
            pass
    conn.commit()
    return conn


@contextmanager
def _db():
    conn = open_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_refresh(conn: sqlite3.Connection, username: str, resource: str, *, extra=None):
    conn.execute(
        """
        INSERT INTO refresh_log (username, resource, last_refresh_at, extra_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username, resource) DO UPDATE SET
            last_refresh_at = excluded.last_refresh_at,
            extra_json = excluded.extra_json
        """,
        (username, resource, _now_iso(), json.dumps(extra) if extra is not None else None),
    )


def record_account(
    username: str,
    label: str | None = None,
    *,
    steam_id_64: str | int | None = None,
) -> None:
    label_num = None
    if label is not None:
        try:
            label_num = int(str(label).strip())
        except (TypeError, ValueError):
            label_num = None
    sid_str: str | None = None
    if steam_id_64 is not None:
        try:
            sid_str = str(int(steam_id_64))
        except (TypeError, ValueError):
            sid_str = None
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO accounts (username, label, label_num, steam_id_64, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                label = COALESCE(excluded.label, accounts.label),
                label_num = COALESCE(excluded.label_num, accounts.label_num),
                steam_id_64 = COALESCE(excluded.steam_id_64, accounts.steam_id_64),
                last_seen_at = excluded.last_seen_at
            """,
            (username, label, label_num, sid_str, _now_iso()),
        )


def get_account_steam_id(username: str) -> str | None:
    """Возвращает закешированный steam_id_64 для username (или None)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT steam_id_64 FROM accounts WHERE username=?",
            (username,),
        ).fetchone()
    return (row[0] if row and row[0] else None)


def update_hidden_from_public(
    username: str,
    app_context: str,
    hidden_asset_ids: Iterable[str],
    *,
    visible_asset_ids: Iterable[str] | None = None,
) -> tuple[int, int]:
    """Обновляет колонку `hidden_from_public` для всех строк аккаунта+контекста.

    `hidden_asset_ids` — asset_id, которых нет в публичной выдаче, но есть в нашей
    (предметы в display cooldown). Эти ставим в 1.

    `visible_asset_ids` — те, что видны публично; ставим в 0. Если None — НЕ
    обнуляем остальные строки (на случай частичного обновления).

    `last_public_check_at` проставляется в now для всех затронутых строк.

    Возвращает (n_hidden, n_visible) — сколько строк помечено.
    """
    now = _now_iso()
    hidden_set = {str(x) for x in hidden_asset_ids if x is not None}
    visible_set = (
        {str(x) for x in visible_asset_ids if x is not None}
        if visible_asset_ids is not None else None
    )
    with _db() as conn:
        n_hidden = 0
        for aid in hidden_set:
            cur = conn.execute(
                "UPDATE inventory_cache "
                "SET hidden_from_public=1, last_public_check_at=? "
                "WHERE username=? AND app_context=? AND asset_id=?",
                (now, username, app_context, aid),
            )
            n_hidden += cur.rowcount
        n_visible = 0
        if visible_set is not None:
            for aid in visible_set:
                cur = conn.execute(
                    "UPDATE inventory_cache "
                    "SET hidden_from_public=0, last_public_check_at=? "
                    "WHERE username=? AND app_context=? AND asset_id=?",
                    (now, username, app_context, aid),
                )
                n_visible += cur.rowcount
    return (n_hidden, n_visible)


def record_balance(
    username: str,
    balance_cents: int | None,
    on_hold_cents: int | None,
    currency_code: int | None,
) -> None:
    with _db() as conn:
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO wallet_snapshots
                (username, snapshot_at, balance_cents, on_hold_cents, currency_code)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username, snapshot_at) DO NOTHING
            """,
            (username, now, balance_cents, on_hold_cents, currency_code),
        )
        _mark_refresh(conn, username, "balance")


def record_listings(
    username: str,
    listings: Iterable,
    currency_code: int | None,
    *,
    partial: bool = True,
) -> int:
    """Запоминает выставленные листинги.

    partial=True (по умолчанию) — только UPSERT-им переданные ID, не трогая остальные.
        Используем когда подгружена только часть страниц (лень-пагинация).
    partial=False — вычищаем все листинги аккаунта и перезаписываем переданные.
        Использовать только если выгрузили ВЕСЬ список.
    """
    rows = []
    for lst in listings:
        item = getattr(lst, "item", None)
        descr = getattr(item, "description", None) if item else None
        name = getattr(descr, "market_hash_name", None) if descr else None
        asset_id = getattr(item, "asset_id", None) if item else None
        unowned_id = getattr(item, "unowned_id", None) if item else None
        rows.append(
            (
                username,
                str(lst.id),
                str(asset_id) if asset_id else None,
                str(unowned_id) if unowned_id else None,
                name,
                int(lst.price) if lst.price is not None else None,
                currency_code,
                _to_iso(getattr(lst, "time_created", None)),
                _now_iso(),
            )
        )
    with _db() as conn:
        if not partial:
            conn.execute("DELETE FROM listings_cache WHERE username=?", (username,))
        if rows:
            conn.executemany(
                """
                INSERT INTO listings_cache
                    (username, listing_id, asset_id, unowned_id, market_hash_name,
                     price_cents, currency_code, time_created, last_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, listing_id) DO UPDATE SET
                    asset_id         = excluded.asset_id,
                    unowned_id       = excluded.unowned_id,
                    market_hash_name = excluded.market_hash_name,
                    price_cents      = excluded.price_cents,
                    currency_code    = excluded.currency_code,
                    time_created     = excluded.time_created,
                    last_updated_at  = excluded.last_updated_at
                """,
                rows,
            )
        _mark_refresh(conn, username, "listings", extra={"partial": partial, "rows": len(rows)})
    return len(rows)


def record_buy_orders(username: str, orders: Iterable, currency_code: int | None) -> int:
    rows = []
    for o in orders:
        descr = o.item_description if hasattr(o, "item_description") else None
        name = getattr(descr, "market_hash_name", None) if descr else None
        rows.append(
            (
                username,
                str(o.id),
                name,
                int(o.price) if getattr(o, "price", None) is not None else None,
                int(getattr(o, "quantity_remaining", 0) or 0),
                int(getattr(o, "quantity", 0) or 0),
                currency_code,
                _now_iso(),
            )
        )
    with _db() as conn:
        # Buy orders переписываем целиком — их обычно <50, и они быстро меняются.
        conn.execute("DELETE FROM buy_orders_cache WHERE username=?", (username,))
        if rows:
            conn.executemany(
                """
                INSERT INTO buy_orders_cache
                    (username, order_id, market_hash_name, price_cents,
                     qty_remaining, qty_total, currency_code, last_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        _mark_refresh(conn, username, "orders", extra={"rows": len(rows)})
    return len(rows)


def record_history_events(
    username: str,
    events: Iterable,
    currency_code: int | None,
    *,
    price_extractor=None,
) -> int:
    """Append-only: возвращает кол-во новых добавленных событий.

    price_extractor — функция (event) -> int|None, чтобы вызывающий мог
    передать собственную логику (см. _history_event_price_cents в simple.py).
    """
    rows = []
    for ev in events:
        listing = getattr(ev, "listing", None)
        listing_id = getattr(listing, "id", None) if listing else None
        ev_type = getattr(ev, "type", None)
        ev_type_str = ev_type.name if hasattr(ev_type, "name") else str(ev_type)
        time_event = getattr(ev, "time_event", None)
        # Уникальный id события: listing.id + time + type — этого достаточно
        # для дедупа (один листинг может породить CREATED → SOLD/CANCELLED
        # с разными timestamp).
        ev_id = f"{listing_id}:{_to_iso(time_event)}:{ev_type_str}"

        # name: ev.listing.item.description.market_hash_name
        name = None
        item = getattr(listing, "item", None) if listing else None
        descr = getattr(item, "description", None) if item else None
        if descr is not None:
            name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", None)

        if price_extractor is not None:
            price = price_extractor(ev)
        else:
            # дефолтный fallback: paid/received/price на самом listing
            price = (
                getattr(listing, "paid_amount", None)
                or getattr(listing, "received_amount", None)
                or getattr(listing, "price", None)
            )

        rows.append(
            (
                username,
                ev_id,
                ev_type_str,
                name,
                _to_iso(time_event),
                int(price) if price is not None else None,
                currency_code,
                None,  # raw_json — резерв на будущее
            )
        )
    if not rows:
        with _db() as conn:
            _mark_refresh(conn, username, "history", extra={"added": 0})
        return 0
    with _db() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM market_history WHERE username=?", (username,)
        ).fetchone()[0]
        conn.executemany(
            """
            INSERT OR IGNORE INTO market_history
                (username, event_id, event_type, market_hash_name,
                 time_event, price_cents, currency_code, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM market_history WHERE username=?", (username,)
        ).fetchone()[0]
        added = after - before
        _mark_refresh(conn, username, "history", extra={"added": added})
    return added


def _dump_description_entry(entry) -> dict:
    """Все поля одной HTML-/text-записи описания, какие вытащит aiosteampy.

    PR #9: для дебага trade-hold-парсинга важны минимум `value`, `color`,
    `name`, `type` — атрибут «Tradable/Marketable After …» имеет
    `color=ff4040`, `name=attribute`.
    """
    return {
        "value": getattr(entry, "value", None),
        "color": getattr(entry, "color", None),
        "name": getattr(entry, "name", None),
        "type": getattr(entry, "type", None),
    }


def _item_extra_json(item) -> str | None:
    """Сериализует raw `properties` + `descriptions` + `owner_descriptions`
    + marketable-флаги предмета в JSON.

    Это нужно для дебага — Steam периодически меняет схему ответа (например,
    добавляет новые propertyid под чармы), и без сырого дампа сложно понять,
    почему наш парсер не нашёл то, что должен. Пишем в `inventory_cache.extra_json`.

    PR #9: добавили `owner_descriptions` (где Steam держит «Tradable/Marketable
    After …»), `marketable`/`market_tradable_restriction`/
    `market_marketable_restriction` (флаги market-cooldown) и `tradable_after`
    (то, что aiosteampy реально вытащил в bare поле — None vs datetime).
    Полные поля каждой description-записи (name/type/color/value) — чтобы
    видеть как aiosteampy парсит ответ Steam.
    """
    try:
        props = []
        for p in getattr(item, "properties", None) or ():
            props.append(
                {
                    "id": getattr(p, "id", None),
                    "name": getattr(p, "name", None),
                    "value": getattr(p, "value", None),
                    "float_value": getattr(p, "float_value", None),
                    "int_value": getattr(p, "int_value", None),
                }
            )
        descr = getattr(item, "description", None)
        descriptions = []
        owner_descriptions = []
        marketable_flags = {}
        if descr is not None:
            for entry in getattr(descr, "descriptions", None) or ():
                descriptions.append(_dump_description_entry(entry))
            for entry in getattr(descr, "owner_descriptions", None) or ():
                owner_descriptions.append(_dump_description_entry(entry))
            marketable_flags = {
                "marketable": getattr(descr, "marketable", None),
                "tradable": getattr(descr, "tradable", None),
                "commodity": getattr(descr, "commodity", None),
                "market_tradable_restriction": getattr(
                    descr, "market_tradable_restriction", None
                ),
                "market_marketable_restriction": getattr(
                    descr, "market_marketable_restriction", None
                ),
            }
        tags = []
        if descr is not None:
            for tag in getattr(descr, "tags", None) or ():
                tags.append(
                    {
                        "category": getattr(tag, "category", None),
                        "internal_name": getattr(tag, "internal_name", None),
                        "localized_tag_name": getattr(tag, "localized_tag_name", None),
                    }
                )
        return json.dumps(
            {
                "properties": props,
                "descriptions": descriptions,
                "owner_descriptions": owner_descriptions,
                "marketable_flags": marketable_flags,
                "tradable_after_raw": getattr(item, "tradable_after", None),
                "tags": tags,
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception:  # noqa: BLE001
        return None


def record_inventory(
    username: str,
    app_context_name: str,
    items: Iterable,
    *,
    paint_seed_extractor=None,
    paint_wear_extractor=None,
    state_extractor=None,
    partial: bool = False,
) -> int:
    """Сохраняет инвентарь одного app_context.

    paint_seed_extractor / paint_wear_extractor — опциональные функции
    (item) -> int|float|None для CS2-предметов.
    state_extractor — (item) -> str|None из множества:
        "marketable" / "not_marketable" / "trade_protect" / "trade_hold".
        Если не задан — поле останется NULL (старое поведение).
    """
    rows = []
    for it in items:
        descr = it.description
        name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", None)
        seed = paint_seed_extractor(it) if paint_seed_extractor else None
        wear = paint_wear_extractor(it) if paint_wear_extractor else None
        state = state_extractor(it) if state_extractor else None
        tradable_after = _to_iso(getattr(it, "tradable_after", None))
        rows.append(
            (
                username,
                app_context_name,
                str(it.asset_id),
                name,
                int(getattr(it, "amount", 1) or 1),
                seed,
                wear,
                _item_extra_json(it),
                state,
                tradable_after,
                _now_iso(),
            )
        )
    with _db() as conn:
        if not partial:
            conn.execute(
                "DELETE FROM inventory_cache WHERE username=? AND app_context=?",
                (username, app_context_name),
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO inventory_cache
                    (username, app_context, asset_id, market_hash_name, amount,
                     paint_seed, paint_wear, extra_json, state, tradable_after,
                     last_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, app_context, asset_id) DO UPDATE SET
                    market_hash_name = excluded.market_hash_name,
                    amount           = excluded.amount,
                    paint_seed       = COALESCE(excluded.paint_seed, inventory_cache.paint_seed),
                    paint_wear       = COALESCE(excluded.paint_wear, inventory_cache.paint_wear),
                    extra_json       = excluded.extra_json,
                    state            = COALESCE(excluded.state, inventory_cache.state),
                    tradable_after   = excluded.tradable_after,
                    last_updated_at  = excluded.last_updated_at
                """,
                rows,
            )
        _mark_refresh(
            conn,
            username,
            f"inventory_{app_context_name}",
            extra={"rows": len(rows), "partial": partial},
        )
    return len(rows)


def get_listed_asset_ids(username: str) -> set[str]:
    """Возвращает set asset_id, которые лежат в кеше активных листингов.

    Steam теперь оставляет вещь в инвентаре после выставления, поэтому надо
    фильтровать инвентарь по этому списку, чтобы не пытаться выставить уже-выставленное.
    Включаем и `asset_id`, и `unowned_id` — какой из них реально матчит инвентарь
    зависит от того, под каким полем Steam отдаёт предмет.
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT asset_id, unowned_id FROM listings_cache WHERE username=?
            """,
            (username,),
        ).fetchall()
    out: set[str] = set()
    for asset_id, unowned_id in rows:
        if asset_id:
            out.add(str(asset_id))
        if unowned_id:
            out.add(str(unowned_id))
    return out


def delete_listing(username: str, listing_id) -> None:
    """Убрать снятый листинг из кеша (после удачного cancel_sell_listing)."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM listings_cache WHERE username=? AND listing_id=?",
            (username, str(listing_id)),
        )


def find_listing_by_asset_id(username: str, asset_id) -> str | None:
    """Находит listing_id активного листинга по asset_id предмета (или unowned_id).

    Нужно для cross-account «снять с продажи»: в `inventory_cache` мы знаем
    asset_id экземпляра, а Steam'у для cancel_sell_listing нужен `listing_id`.

    Ищем сначала по `unowned_id` (== asset_id в инвентаре ДО выставления —
    основной кейс для аиостимпай-стартового потока), потом по `asset_id`
    листинга. Возвращает str (listing_id) или None если ничего не нашлось.
    """
    if asset_id is None:
        return None
    asset_id_str = str(asset_id)
    with _db() as conn:
        row = conn.execute(
            """
            SELECT listing_id FROM listings_cache
            WHERE username=? AND (unowned_id=? OR asset_id=?)
            LIMIT 1
            """,
            (username, asset_id_str, asset_id_str),
        ).fetchone()
    if not row:
        return None
    return str(row[0])


def get_listing_by_asset_id(username: str, asset_id) -> dict | None:
    """То же, что `find_listing_by_asset_id`, но возвращает все полезные поля.

    Используется для UI: показать цену листинга рядом с inventory-row у которого
    state='on_market'.
    """
    if asset_id is None:
        return None
    asset_id_str = str(asset_id)
    with _db() as conn:
        row = conn.execute(
            """
            SELECT listing_id, price_cents, currency_code, market_hash_name,
                   asset_id, unowned_id, time_created
            FROM listings_cache
            WHERE username=? AND (unowned_id=? OR asset_id=?)
            LIMIT 1
            """,
            (username, asset_id_str, asset_id_str),
        ).fetchone()
    if not row:
        return None
    return {
        "listing_id": str(row[0]),
        "price_cents": row[1],
        "currency_code": row[2],
        "market_hash_name": row[3],
        "asset_id": row[4],
        "unowned_id": row[5],
        "time_created": row[6],
    }


def insert_placed_listing(
    username: str,
    listing_id,
    *,
    asset_id=None,
    unowned_id=None,
    market_hash_name: str | None = None,
    price_cents: int | None = None,
    currency_code: int | None = None,
    time_created=None,
) -> None:
    """Записывает новый листинг в `listings_cache` сразу после успешного
    `place_sell_listing(...)`. Нужно чтобы пользователь сразу мог снять листинг
    «глобально», не дожидаясь sweep'а (там record_listings вызывается).

    Замечание про asset_id / unowned_id: после place_sell Steam переносит
    предмет под НОВЫЙ asset_id (для листинга), а исходный asset_id (из инвентаря)
    становится `unowned_id`. Поэтому минимально достаточно хранить
    `unowned_id = <тот asset_id, что мы передали в place_sell>` — `find_listing_by_asset_id`
    его найдёт. `asset_id` (новый) мы из place_sell ответа не получаем, поэтому
    оставляем None (sweep его потом проставит).
    """
    if listing_id is None:
        return
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO listings_cache
                (username, listing_id, asset_id, unowned_id, market_hash_name,
                 price_cents, currency_code, time_created, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, listing_id) DO UPDATE SET
                asset_id         = COALESCE(excluded.asset_id, listings_cache.asset_id),
                unowned_id       = COALESCE(excluded.unowned_id, listings_cache.unowned_id),
                market_hash_name = COALESCE(excluded.market_hash_name, listings_cache.market_hash_name),
                price_cents      = COALESCE(excluded.price_cents, listings_cache.price_cents),
                currency_code    = COALESCE(excluded.currency_code, listings_cache.currency_code),
                time_created     = COALESCE(excluded.time_created, listings_cache.time_created),
                last_updated_at  = excluded.last_updated_at
            """,
            (
                username,
                str(listing_id),
                str(asset_id) if asset_id is not None else None,
                str(unowned_id) if unowned_id is not None else None,
                market_hash_name,
                int(price_cents) if price_cents is not None else None,
                currency_code,
                _to_iso(time_created),
                _now_iso(),
            ),
        )


def mark_inventory_state_by_asset_id(
    username: str, asset_id, new_state: str
) -> int:
    """Точечно обновляет inventory_cache.state у записи (username, asset_id).

    Возвращает кол-во обновлённых строк (0 или 1). app_context не указываем
    (PK включает app_context, но asset_id у Steam уникален в пределах
    username, поэтому достаточно WHERE по asset_id).

    Используется чтобы сразу после cancel_sell_listing предмет перестал
    показываться как «на маркете» — не дожидаясь полного sweep'а.
    """
    if asset_id is None:
        return 0
    with _db() as conn:
        cur = conn.execute(
            """
            UPDATE inventory_cache
            SET state = ?, last_updated_at = ?
            WHERE username = ? AND asset_id = ?
            """,
            (new_state, _now_iso(), username, str(asset_id)),
        )
        return cur.rowcount or 0


def delete_buy_order(username: str, order_id) -> None:
    """Убрать отменённый buy-ордер из кеша (после удачного cancel_buy_order)."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM buy_orders_cache WHERE username=? AND order_id=?",
            (username, str(order_id)),
        )


def get_last_refresh(username: str, resource: str) -> datetime | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT last_refresh_at FROM refresh_log WHERE username=? AND resource=?",
            (username, resource),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def get_latest_balance(username: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            """
            SELECT snapshot_at, balance_cents, on_hold_cents, currency_code
            FROM wallet_snapshots
            WHERE username=?
            ORDER BY snapshot_at DESC LIMIT 1
            """,
            (username,),
        ).fetchone()
    if not row:
        return None
    return {
        "snapshot_at": row[0],
        "balance_cents": row[1],
        "on_hold_cents": row[2],
        "currency_code": row[3],
    }


def get_cached_nameid(app_id: int, market_hash_name: str) -> int | None:
    """Возвращает item_nameid из кеша, либо None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT item_nameid FROM market_nameids "
            "WHERE app_id=? AND market_hash_name=?",
            (int(app_id), market_hash_name),
        ).fetchone()
    return int(row[0]) if row else None


def cache_nameid(app_id: int, market_hash_name: str, item_nameid: int) -> None:
    """Кеширует (app_id, market_hash_name) → item_nameid."""
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_nameids "
            "(app_id, market_hash_name, item_nameid, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (int(app_id), market_hash_name, int(item_nameid), _now_iso()),
        )
        conn.commit()


def get_cached_gid(app_id: int, market_hash_name: str) -> str | None:
    """Возвращает GID (новый ид Steam Market 2026) из кеша, либо None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT gid FROM market_gids "
            "WHERE app_id=? AND market_hash_name=?",
            (int(app_id), market_hash_name),
        ).fetchone()
    return str(row[0]) if row else None


def cache_gid(app_id: int, market_hash_name: str, gid: str) -> None:
    """Кеширует (app_id, market_hash_name) → GID."""
    if not gid:
        return
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_gids "
            "(app_id, market_hash_name, gid, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (int(app_id), market_hash_name, str(gid), _now_iso()),
        )
        conn.commit()


def get_buy_orders_total(username: str) -> dict | None:
    """Возвращает агрегаты по активным buy-ордерам аккаунта.

    {
        "total_cents": int — Σ(price_cents × qty_remaining) по всем ордерам,
        "currency_code": int | None — самая популярная валюта среди ордеров,
        "orders_count": int — кол-во активных ордеров,
        "items_count": int — Σ(qty_remaining) — кол-во ещё не выкупленных штук.
    }

    None если в кеше нет данных по этому аккаунту.
    """
    with _db() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(price_cents * qty_remaining), 0) AS total,
                COUNT(*)                                       AS orders_count,
                COALESCE(SUM(qty_remaining), 0)                AS items_count
            FROM buy_orders_cache
            WHERE username=?
            """,
            (username,),
        ).fetchone()
        if not row or row[1] == 0:
            return None
        # Самая популярная валюта среди ордеров (на случай если ордера в разных
        # валютах попали в кеш — в норме у одного аккаунта одна валюта).
        cur_row = conn.execute(
            """
            SELECT currency_code, COUNT(*) AS c
            FROM buy_orders_cache
            WHERE username=? AND currency_code IS NOT NULL
            GROUP BY currency_code
            ORDER BY c DESC LIMIT 1
            """,
            (username,),
        ).fetchone()
    return {
        "total_cents": int(row[0] or 0),
        "orders_count": int(row[1] or 0),
        "items_count": int(row[2] or 0),
        "currency_code": int(cur_row[0]) if cur_row and cur_row[0] is not None else None,
    }


def get_known_event_ids(username: str) -> set[str]:
    """Возвращает set всех event_id, уже записанных в market_history для username.

    Нужен для дельты истории при sweep'е: качаем `get_my_market_history(start=0,
    count=100)` страницами, и как только встречаем уже известный event_id —
    останавливаемся (значит дальше всё известно).
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT event_id FROM market_history WHERE username=?",
            (username,),
        ).fetchall()
    return {r[0] for r in rows if r[0]}


def iter_all_market_history(limit: int | None = None) -> list[dict]:
    """Все события `market_history` по всем аккаунтам, по `time_event DESC`.

    Возвращает список dict: {username, event_id, event_type, market_hash_name,
    time_event, price_cents, currency_code}. `limit` ограничивает выборку
    (None — без лимита; обычно ставим N=200 чтобы не тащить тысячи событий).

    Используется в задаче 6: общая cross-account история сделок маркета.
    """
    sql = (
        "SELECT username, event_id, event_type, market_hash_name, "
        "time_event, price_cents, currency_code "
        "FROM market_history "
        "ORDER BY time_event DESC, event_id DESC"
    )
    params: tuple = ()
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params = (int(limit),)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "username": r[0],
            "event_id": r[1],
            "event_type": r[2],
            "market_hash_name": r[3],
            "time_event": r[4],
            "price_cents": r[5],
            "currency_code": r[6],
        })
    return out


def get_balance_diff_since_yesterday(username: str) -> int | None:
    """Возвращает абсолютную разницу в центах между последним балансом и
    самым свежим балансом за прошлые сутки. None — недостаточно данных.

    Используется в sweep'е как «надо ли дёргать историю».
    """
    with _db() as conn:
        latest = conn.execute(
            """
            SELECT balance_cents, snapshot_at FROM wallet_snapshots
            WHERE username=? ORDER BY snapshot_at DESC LIMIT 1
            """,
            (username,),
        ).fetchone()
        if not latest:
            return None
        prev = conn.execute(
            """
            SELECT balance_cents FROM wallet_snapshots
            WHERE username=? AND snapshot_at < ?
            ORDER BY snapshot_at DESC LIMIT 1
            """,
            (username, latest[1]),
        ).fetchone()
    if not prev or prev[0] is None or latest[0] is None:
        return None
    return abs(int(latest[0]) - int(prev[0]))


def get_inventory_count(username: str, app_context: str | None = None) -> int:
    """Сколько строк в inventory_cache. Если app_context задан — только этот контекст."""
    with _db() as conn:
        if app_context is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM inventory_cache WHERE username=?",
                (username,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM inventory_cache "
                "WHERE username=? AND app_context=?",
                (username, app_context),
            ).fetchone()
    return int(row[0] or 0) if row else 0


def iter_inventory(
    username: str | None = None,
    app_context: str | None = None,
) -> list[dict]:
    """Все строки `inventory_cache` (опционально фильтр по username / app_context).

    Возвращает по dict на каждую строку:
        {"username", "app_context", "asset_id", "market_hash_name", "amount",
         "paint_seed", "paint_wear", "extra_json", "last_updated_at"}.
    """
    where = []
    params: list = []
    if username is not None:
        where.append("username=?")
        params.append(username)
    if app_context is not None:
        where.append("app_context=?")
        params.append(app_context)
    sql = (
        "SELECT username, app_context, asset_id, market_hash_name, amount, "
        "paint_seed, paint_wear, extra_json, state, tradable_after, "
        "last_updated_at, hidden_from_public, last_public_check_at "
        "FROM inventory_cache"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "username": r[0],
            "app_context": r[1],
            "asset_id": r[2],
            "market_hash_name": r[3],
            "amount": int(r[4] or 1),
            "paint_seed": r[5],
            "paint_wear": r[6],
            "extra_json": r[7],
            "state": r[8],
            "tradable_after": r[9],
            "last_updated_at": r[10],
            "hidden_from_public": bool(r[11]) if r[11] is not None else None,
            "last_public_check_at": r[12],
        }
        for r in rows
    ]


def iter_account_summaries() -> list[dict]:
    """Возвращает по строке на аккаунт со сводной информацией для меню.

    Сортировка: сначала аккаунты с числовым label (по возрастанию),
    потом всё остальное по username.
    """
    with _db() as conn:
        accs = conn.execute(
            """
            SELECT username, label, label_num, last_seen_at
            FROM accounts
            ORDER BY
                CASE WHEN label_num IS NULL THEN 1 ELSE 0 END,
                label_num,
                username
            """
        ).fetchall()
        out = []
        for username, label, label_num, seen in accs:
            bal = get_latest_balance(username)
            listings_n = conn.execute(
                "SELECT COUNT(*) FROM listings_cache WHERE username=?", (username,)
            ).fetchone()[0]
            orders_n = conn.execute(
                "SELECT COUNT(*) FROM buy_orders_cache WHERE username=?", (username,)
            ).fetchone()[0]
            history_n = conn.execute(
                "SELECT COUNT(*) FROM market_history WHERE username=?", (username,)
            ).fetchone()[0]
            inventory_n = conn.execute(
                "SELECT COUNT(*) FROM inventory_cache WHERE username=?", (username,)
            ).fetchone()[0]
            orders_agg = get_buy_orders_total(username)
            out.append(
                {
                    "username": username,
                    "label": label or username,
                    "label_num": label_num,
                    "last_seen_at": seen,
                    "balance": bal,
                    "listings_cached": listings_n,
                    "orders_cached": orders_n,
                    "history_events": history_n,
                    "inventory_cached": inventory_n,
                    "buy_orders_agg": orders_agg,
                }
            )
    return out