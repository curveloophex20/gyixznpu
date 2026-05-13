# Steam Trading Bot — ARCHITECTURE.md
> Вставляй этот файл в начало каждой новой сессии с ИИ вместо кода.

---

## Стек
- Python 3.11+, asyncio
- **aiosteampy** ≥ 0.7 — Steam API (login, inventory, listings, orders, history, trade offers)
- **aiohttp** + **httpx** (для CSFloat)
- **SQLite** (через stdlib sqlite3) — локальный кеш
- **protobuf** ≥ 5.26 — CS2 asset properties
- **python-dotenv** — env-переменные

---

## Файловая структура
```
simple.py           — главный скрипт (~6639 строк): CLI-меню, логин, sweep, все команды
cache.py            — SQLite-кеш (~1147 строк)
item_info.py        — просмотр предмета, стаканы, флоаты (~1415 строк)
patterns.py         — детектор редких паттернов CS2
steam_errors.py     — классификатор ошибок Steam
price_suggest.py    — NEW: авто-подбор цены (Path A / B / C)

accounts/<name>/
  account.json      — {label, username, password, steam_id}
  *.maFile          — Steam Desktop Authenticator file

data/
  cache.sqlite3     — SQLite база
  7patterns.txt     — base_name'ы скинов с редкими паттернами (через запятую)
  7patterns.json    — точные paint_seed по тирам

proxies.txt         — прокси-пул (по одному на строку, http/socks5)
.steam_session/     — кешированные cookies (username.cookies)
```

---

## ВАЖНО: Steam Market 2026 API (новый)

Steam перешёл на новый Market API. Теперь для листингов нужен **GID** (вида `G[0-9A-Fa-f]+`), а не `item_nameid`.

- GID — базовый ID скина, один на все wear/StatTrak/Souvenir варианты
- Получается 301-редиректом с `/market/listings/<app>/<name>`
- Кешируется в таблице `market_gids`
- Path A (коммодити) по-прежнему использует `item_nameid` + `get_item_orders_histogram`
- Path B (скины с флоатом) использует GID → POST `/market/listings/{app_id}/{GID}`

---

## cache.py — публичный API

```python
open_db() -> sqlite3.Connection
record_account(username, label)
get_account_steam_id(username) -> str | None
record_balance(username, balance_cents, on_hold_cents, currency_code)
record_listings(username, listings_iter, currency_code, partial=True) -> int
record_buy_orders(username, orders_iter, currency_code) -> int
record_history_events(username, events_iter, currency_code, price_extractor=None) -> int
record_inventory(username, app_context_name, items_iter, *, paint_seed_extractor,
                 paint_wear_extractor, state_extractor, partial=False) -> int
update_hidden_from_public(username, app_context, public_asset_ids) -> int
get_last_refresh(username, resource) -> datetime | None
get_latest_balance(username) -> dict | None
get_listed_asset_ids(username) -> set[str]
find_listing_by_asset_id(username, asset_id) -> str | None
get_listing_by_asset_id(username, asset_id) -> dict | None
insert_placed_listing(username, listing_id, *, unowned_id, market_hash_name, price_cents, currency_code)
delete_listing(username, listing_id)
delete_buy_order(username, order_id)
mark_inventory_state_by_asset_id(username, asset_id, new_state) -> int
iter_inventory(username=None, app_context=None) -> list[dict]
iter_account_summaries() -> list[dict]
iter_all_market_history(limit=None) -> list[dict]
get_buy_orders_total(username) -> dict | None
get_known_event_ids(username) -> set[str]
get_cached_nameid(app_id, market_hash_name) -> int | None
cache_nameid(app_id, market_hash_name, item_nameid)
get_cached_gid(app_id, market_hash_name) -> str | None   # NEW: GID Steam Market 2026
cache_gid(app_id, market_hash_name, gid)                 # NEW
```

### SQLite таблицы
| Таблица | PK | Назначение |
|---|---|---|
| `accounts` | username | label, label_num, steam_id_64, last_seen_at |
| `wallet_snapshots` | username+snapshot_at | баланс в центах |
| `listings_cache` | username+listing_id | активные листинги |
| `buy_orders_cache` | username+order_id | ордера покупки |
| `market_history` | username+event_id | история маркета (append-only) |
| `inventory_cache` | username+app_context+asset_id | инвентарь с state/float/seed |
| `market_nameids` | app_id+market_hash_name | item_nameid (для histogram) |
| `market_gids` | app_id+market_hash_name | **NEW** GID (для нового Market API) |
| `refresh_log` | username+resource | таймстамп последнего обновления |

### inventory_cache.state
- `"free"` — свободен, можно выставлять
- `"on_market"` — уже на листинге
- `"trade_protect"` — context=16, 7-дн. защита
- `"trade_hold"` — market-hold после покупки с ТП

### inventory_cache.hidden_from_public
- `1` — display cooldown (есть у нас, нет в публичной выдаче, ~3 дня после разлока)
- `0` — виден публично
- `NULL` — ещё не проверялся

---

## price_suggest.py — NEW модуль

Логика авто-ценообразования. Три пути:

```python
# Path A: коммодити (кейсы, наклейки, чармы — нет paint_seed)
@dataclass
class PathASuggestion:
    cents: int | None
    reason: str

def path_a_suggest(
    sell_table: list[tuple[int, int]],   # [(price_cents, qty), ...] ASC из histogram
    daily_sales: float,
    week_pct: float | None,
) -> PathASuggestion:
    # threshold = 10% × daily_sales
    # Идём по стакану снизу: пропускаем аномальный пол (боты),
    # принимаем уровни с qty ≤ threshold, стоп на первом превышении.
    # Берём самый высокий accepted уровень.
    # STABLE (|week_pct| ≤ 2%) + есть стенка → шаг к стенке (первый rejected).

# Path B: скин с флоатом, паттерн не редкий
@dataclass
class PathBSuggestion:
    cents: int | None
    reason: str

async def path_b_suggest(
    session,                # aiohttp.ClientSession
    app_id: int,
    gid: str,               # GID из resolve_gid()
    *,
    our_float: float,
    quality_tag: str | None,
    exterior_tag: str | None,
    currency_code: int,
    daily_sales: float,
) -> PathBSuggestion:
    # POST /market/listings/{app_id}/{GID} с фильтром float ≤ our_float×1.10
    # qty_at_min < daily_sales → ставим РОВНО min
    # qty_at_min ≥ daily_sales → min − 0.01 (undercut)

# Path C: редкий паттерн — только ручной ввод, никакой автоматики

def classify(name: str, paint_seed: int | None) -> str:
    # "A" — нет paint_seed
    # "B" — есть seed, не редкий
    # "C" — редкий или uncertain (is_rare=None → трактуем как C)

# Утилиты из price_history:
def daily_sales_from_history(history, days=30) -> float
def week_pct_from_history(history) -> float | None
```

---

## item_info.py — публичный API

```python
# Резолвинг ID:
await resolve_item_nameid(client, app_id, market_hash_name) -> int | None
  # cache → Steam HTML парсинг → None

await resolve_gid(client, app_id, market_hash_name) -> str | None  # NEW
  # GID для нового Steam Market 2026
  # cache.get_cached_gid → _fetch_item_page (301 redirect) → regex → cache.cache_gid

# Новый Steam Market 2026 API:
async def _fetch_listings_page(              # NEW signature (принимает GID вместо nameid)
    session, app_id, gid, *,
    start, sort_field=0, sort_dir=0,
    category_filters=None,                   # {"category_730_Exterior": ["tag_FactoryNew"]}
    wear_range=None,                         # (float_min, float_max)
    seed_range=None,                         # (int_min, int_max) — Steam хочет str!
    price_range=None,                        # (unMin_cents, unMax_cents)
    text_query=None,
    currency_code=None,
) -> dict | None
  # POST /market/listings/{app_id}/{GID} с JSON body
  # Возвращает {listings, total_count, more, facets}

def _parse_listings_v2(data: dict) -> list[dict]   # парсит ответ нового API
def _default_filters_from_name(name: str) -> (quality_tags, exterior_tags)  # NEW

# Рендеринг:
render_histogram_block(histogram, sym, max_rows=10) -> list[str]
render_price_chart_block(history, label, sym) -> list[str]   # label="7d"|"30d"|"all"
render_sales_volume_block(history) -> list[str]
render_data_table(history, label, sym, limit=30) -> list[str]
render_full_stack_block(histogram, sym, side="sell"|"buy", limit=None) -> list[str]
render_listings_page(listings, sym, start_idx, total, floats=None) -> list[str]

# Главное меню предмета:
await show_item_info_menu(client, market_hash_name, app, currency_enum, currency_code, ask=None, currency_sym="")
await _show_listings_with_floats(...)
```

---

## patterns.py
```python
load_pattern_db(force=False) -> dict   # {danger_zone: set[str], rare_patterns: dict}
is_rare_pattern(market_hash_name, paint_seed) -> RarePatternResult
  # .is_rare: True | False | None(uncertain)
  # .tier_note: "Tier 1" если редкий
is_charm(market_hash_name) -> bool
```

## steam_errors.py
```python
classify_steam_error(exc) -> SteamError
  # .category: max_wallet | rate_limited | item_unavailable | price_too_low |
  #            price_too_high | need_mobile_confirm | session_expired | not_logged_in | network | unknown
  # .fatal_for_batch, .retryable
format_for_log(err, prefix="") -> str
```

---

## simple.py — ключевые функции

### Авто-ценообразование (NEW — интеграция price_suggest)
```python
async def _auto_suggest_price(
    client, currency_enum, currency_code, *,
    name, paint_seed, paint_wear,
) -> tuple[int, str, "A"|"B"|"C"] | None:
    # Единая точка для авто-цены одного предмета.
    # Path C → print("редкий") → None (вручную)
    # Path A → resolve_item_nameid → histogram → path_a_suggest
    # Path B → resolve_gid → _default_filters_from_name → path_b_suggest

def _make_auto_price_callback(client, currency_enum, currency_code, item):
    # Возвращает async-callback для _ask_price_cents(a_callback=...)
    # Используется в _list_item_action (одиночный предмет в меню инвентаря)

async def _auto_price_group(
    client, currency_enum, currency_code, *,
    name, group: list[dict], label_lookup, cur_sym,
) -> ("uniform", cents) | ("per_item", {asset_id: cents}) | ("skip", None) | None:
    # Авто-цена для группы кандидатов в cross-account bulk-list.
    # Path A (нет seed у всех) → одна цена для группы → confirm/edit
    # Path B/C (есть seed) → индивидуально для каждого:
    #   - таблица: # / acc / float / seed / suggest / path / reason
    #   - команды: y / edit N PRICE / skip N / n
```

### Логин и мульти-аккаунт
```python
_discover_accounts() -> list[dict]
_connect_account(account, force_relogin, proxy=None) -> (client, currency_code, cookies_file) | None
_try_resume(client, cookies_file) -> bool
```

### CS2-экстракция
```python
_cs2_extract_wear_seed(item) -> (float|None, int|None)
_cs2_extract_stickers(item) -> [(name, wear|None), ...]
_cs2_extract_charms(item)   -> [(name, pattern|None), ...]
```

### Place sell — умный ретрай
```python
await _place_sell_listing_with_retry(client, item_or_asset_id, app_context, *, price, what)
  # При "Failed to perform confirmation" → cancel pending → перевыставляет (1 раз)
await _cancel_all_pending_confirmations(client, label="") -> (n_found, n_cancelled)
```

### Sweep
```python
await _run_sweep(accounts, sessions, force_relogin)
await _sweep_one_account(account, sessions, force_relogin, fetch_history=True, proxy=None) -> dict
  # balance → listings → buy_orders → inventories → hidden_from_public diff → history delta
await _fetch_public_inventory_asset_ids(session, steam_id_64, ctx_str, proxy=None)
  # -> (set[asset_id]|None, error_reason|None)
  # Чистая сессия DummyCookieJar, backoff 0→1→3→7с, max 10 страниц по 2000
```

### Прокси
```python
_load_proxy_pool() -> list[str]
await _ask_use_proxy(label) -> list[str]
class _ProxyRotator:          # round-robin с failover
    current() / mark_bad() / advance()
```

### Bulk-операции
```python
await _bulk_list_group(client, group, currency_enum, currency_code, ...)
await _bulk_cancel_listings(client, listings, ask_confirm=True) -> int
await _bulk_sell_cross_account(name, candidates, accounts_lookup, sessions, ...)
  # Теперь вызывает _auto_price_group для авто-цены
await _bulk_cancel_cross_account(name, listed_rows, accounts_lookup, sessions, ...)
await _collect_all_listings(accounts, sessions, force_relogin)
  # Сбор ВСЕХ листингов: ProxyRotator → get_my_listings страницами → record_listings(partial=False)
```

### Авто-трейд (фоновый)
```python
_AUTOTRADE_TASK / _AUTOTRADE_STATE
await _autotrade_loop(...)    # принимает ТОЛЬКО items_to_give=[]
await _start_autotrade(accounts, sessions, force_relogin)
```

### Глобальная статистика
```python
await _show_global_stats(accounts, sessions, force_relogin)
await _show_cs2_subgroups(rows, ...)
await _show_grouped_items(rows, title, ...)
await _show_recently_unlocked(accounts, sessions, force_relogin, label_lookup)
  # hidden_from_public=1 → s<N> bulk-sell
await _show_global_market_history(accounts, limit=200)
  # m<N> → показать больше
```

### Пагинация
```python
await _paginate(items, page_size, render, extra_commands, bulk_commands)
await _paginate_lazy(total, page_size, fetch_more, render, extra_commands)
```

---

## Конфигурация
```python
STEAM_PASSWORD / MAFILE_PATH / FORCE_RELOGIN
INVENTORY_PAGE_SIZE=25 / HISTORY_PAGE_SIZE=10 / LISTINGS_PAGE_SIZE=10
BUY_ORDER_LIMIT_MULTIPLIER=10

# Env:
STEAM_PASSWORD_<USERNAME>
SWEEP_PROXY / SWEEP_PROXY_FILE
```

---

## Важные нюансы
1. **aiosteampy 0.7** — monkey-patch `ItemDescription._set_d_id`
2. **Steam Market 2026**: листинги теперь через GID (POST), а не nameid (GET). Path A всё ещё использует nameid+histogram.
3. **seed_range в POST**: Steam требует `int_min`/`int_max` как строки, иначе возвращает seed=0
4. **Троттлинг**: 0.3–0.4с между place_sell, 0.5с между аккаунтами, 4-8с (random) при сборе листингов
5. **429 retry**: `_with_retry` — 3 попытки, задержки 3/8/20с
6. **CSFloat**: ~30 req/min, пауза 2с, httpx предпочтительнее aiohttp (TLS fingerprint)
7. **Public inventory diff**: чистая aiohttp-сессия DummyCookieJar — иначе Steam 401/403
8. **Path C** (редкий паттерн): никакой автоматики, только ручной ввод
9. **Авто-трейд**: items_to_give=[] — строгая проверка; если бот просит что-то отдать — пропускаем
10. **_place_sell_listing_with_retry**: при confirm-сбое → cancel pending → перевыставляет 1 раз

---

## Текущая задача
<!-- ЗАПОЛНЯЙ ЭТО ПОЛЕ КАЖДУЮ СЕССИЮ -->
_Опиши что сейчас делаешь: какой модуль правишь, какая фича нужна, что не работает_
