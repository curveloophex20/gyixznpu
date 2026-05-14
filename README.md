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
simple.py           — главный скрипт (~6904 строк): CLI-меню, логин, sweep, все команды
cache.py            — SQLite-кеш (~1147 строк)
item_info.py        — просмотр предмета, стаканы, флоаты (~1415 строк)
patterns.py         — детектор редких паттернов CS2
steam_errors.py     — классификатор ошибок Steam
price_suggest.py    — авто-подбор цены (Path A / B / C) (~412 строк)

accounts/<name>/
  account.json      — {label, username, password, steam_id}
  *.maFile          — Steam Desktop Authenticator file

data/
  cache.sqlite3     — SQLite база
  7patterns.txt     — base_name'ы скинов с редкими паттернами (через запятую)
  7patterns.json    — точные paint_seed по тирам [{base_name, tiers:[{note, patterns:[]}]}]

proxies.txt         — прокси-пул (по одному на строку, http/socks5)
.steam_session/     — кешированные cookies (username.cookies)
```

---

## ВАЖНО: Steam Market 2026 API

Steam перешёл на новый Market API. Теперь для фильтрованных листингов нужен **GID** (вида `G[0-9A-Fa-f]+`), а не `item_nameid`.

- GID — базовый ID скина, один на все wear/StatTrak/Souvenir варианты
- Получается 301-редиректом с `/market/listings/<app>/<name>`
- Кешируется в таблице `market_gids`
- **Path A** (коммодити): по-прежнему использует `item_nameid` + `get_item_orders_histogram`
- **Path B** (скины с флоатом): GID → POST `/market/listings/{app_id}/{GID}`

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
get_cached_gid(app_id, market_hash_name) -> str | None   # GID Steam Market 2026
cache_gid(app_id, market_hash_name, gid)
```

### SQLite таблицы
| Таблица | PK | Назначение |
|---|---|---|
| `accounts` | username | label, label_num, steam_id_64, last_seen_at |
| `wallet_snapshots` | username+snapshot_at | баланс в центах |
| `listings_cache` | username+listing_id | активные листинги |
| `buy_orders_cache` | username+order_id | ордера покупки |
| `market_history` | username+event_id | история маркета (append-only) |
| `inventory_cache` | username+app_context+asset_id | инвентарь с state/float/seed/hidden |
| `market_nameids` | app_id+market_hash_name | item_nameid (для histogram, Path A) |
| `market_gids` | app_id+market_hash_name | GID (для нового Market API, Path B) |
| `refresh_log` | username+resource | таймстамп последнего обновления |

### inventory_cache.state
- `"free"` — свободен, можно выставлять
- `"on_market"` — уже на листинге
- `"trade_protect"` — context=16, 7-дн. защита
- `"trade_hold"` — market-hold после покупки с ТП. Дата разлока берётся через `_effective_tradable_after` — сначала `EconItem.tradable_after` от aiosteampy, иначе парсим строку «Tradable/Marketable After <date> GMT» из `description.descriptions` (PR #9: на некоторых аккаунтах aiosteampy 0.7 не вытаскивает эту дату в bare поле, хотя Steam отдаёт её в HTML-атрибуте). `marketable=False` / `market_tradable_restriction` сами по себе НЕ дают `trade_hold` — только наличие реальной даты разлока (иначе Storage Unit / charms ушли бы в hold).

### inventory_cache.hidden_from_public
- `1` — display cooldown (в нашем инвентаре, нет в публичной выдаче, ~3 дня после разлока)
- `0` — виден публично
- `NULL` — ещё не проверялся

---

## price_suggest.py — авто-ценообразование

### Path A — коммодити (нет paint_seed: кейсы, наклейки, чармы)

```python
@dataclass
class PathASuggestion:
    cents: int | None
    reason: str

def path_a_suggest(
    sell_table: list[tuple[int, int]],   # [(price_cents, qty), ...] ASC из histogram
    daily_sales: float,
    week_pct: float | None,
) -> PathASuggestion:
```

**Алгоритм:**
- `threshold = 10% × daily_sales`
- Пропускаем аномальный ботовый пол (первые уровни с qty > threshold)
- Принимаем уровни с `qty ≤ threshold`, стоп на первом превышении
- Берём самый высокий accepted уровень как `base_price`
- `STABLE` (`|week_pct| ≤ 2%`) + есть стенка → делаем шаг к стенке (первый rejected)
- Если все уровни толстые → undercut минимум на 1¢

---

### Path B — скин с флоатом, паттерн не редкий

```python
@dataclass
class PathBSuggestion:
    cents: int | None
    reason: str

async def path_b_suggest(
    session,                 # aiohttp.ClientSession
    app_id: int,
    gid: str,                # GID из resolve_gid()
    *,
    our_float: float,
    quality_tag: str | None, # category_730_Quality (нужен)
    exterior_tag: str | None, # ИГНОРИРУЕТСЯ — намеренно не фильтруем по wear
    currency_code: int,
    daily_sales: float,
    week_pct: float | None = None,
) -> PathBSuggestion:
```

**Алгоритм (полностью переписан):**

POST `/market/listings/{app_id}/{GID}` с фильтром `quality` + `wear_range=(0.0, f_max)`, где

```
f_max = min(our_float × 1.10, wear_category_max(our_float))
```

**Узкое окно `×1.10`** — чтобы фильтр был специфичен под наш float (разные скины в группе видят разные стаканы и получают разные цены). **Cap `wear_category_max`** (PR #8) — чтобы это окно НЕ выходило за границу нашей wear-категории. CS2 границы:
- FN: float < 0.07
- MW: float < 0.15
- FT: float < 0.38
- WW: float < 0.45
- BS: float ≤ 1.00

Примеры:
- our_float=0.20 (FT) → `min(0.220, 0.38) = 0.220` — узкое окно
- our_float=0.3524 (FT) → `min(0.3877, 0.38) = 0.38` — cap сработал
- our_float=0.3762 (FT) → `min(0.4138, 0.38) = 0.38` — cap спасает от WW-захвата
- our_float=0.04 (FN) → `min(0.044, 0.07) = 0.044` — узкое окно

До PR #8 был только `our_float × 1.10`, без cap'а по wear-категории. Для FT-скина с
float ≥ 0.346 (напр. 0.376×1.10=0.414) это выходило за границу FT (0.38) и
захватывало WW-листинги. В первой странице (top-25) оседал дешёвый WW-сектор,
и реальная FT-стенка убегала на страницы 2+ — алгоритм её не видел и дампил
в мусорный пол WW.

**Exterior-тег намеренно не используется** — чтобы не обрезать FN/MW листинги с лучшим флоатом, которые конкурируют за того же покупателя. Нижняя граница фильтра всегда 0.0.

1. **Фильтрация нулей** — Steam иногда возвращает price=0 (баг), фильтруем
2. **Outlier-защита** — если первый листинг < 25% от второго → пропускаем как дампер/опечатку
3. **Цена = buyer-facing**: `unPricePerUnit + unFeePerUnit` (то, что видит покупатель)

**Четыре сигнала:**
- `density`: spread топ-5 листингов: `DENSE` (<3%) / `MEDIUM` / `SPARSE` (>10%)
- `wall` (PR #7): в top-25 ищем ценовой уровень с макс концентрацией копий (mode).
  Active если `wall_price ≥ p1×1.10` И `concentration ≥ 30%`. При равенстве
  count'ов берём самый дешёвый wall. Фиксит кейс «реальная стенка глубже
  top-5» — пол из нескольких тонких дамперов, которых никто не покупает.
- `velocity`: `FAST` (≥50/день) / `MED` / `SLOW` (≤5/день)
- `trend`: `RISING` (week_pct >+5%) / `FLAT` / `FALLING` (<-5%)

**Anchor** (приоритет WALL > SPARSE-skip-thin > p1):
- WALL active → anchor = wall_price
- иначе при `SPARSE` + `p2 > p1×1.05` → anchor = p2 (p1 «тонкий»)
- иначе → anchor = p1

**База:**
- WALL → anchor − 1¢ (undercut: на wall-уровне много копий, есть смысл подрезать)
- DENSE без WALL → match anchor (не теряем 0.01 на плотной стенке на полу)
- иначе → anchor − 1¢ (undercut)

**Модификатор trend×velocity (cap ±5%):**
- `RISING`: +1%, если ещё и `SLOW`: +2% (медленный рост → можно держать выше)
- `FALLING`: −1%, если ещё и `FAST`: −2% (быстро падает → агрессивнее уходим)

**Float-discount (PR #7):** если наш float хуже «floor float» в anchor-зоне на ≥0.005:
- WALL: floor = min float среди копий на wall-уровне
- p2-skip: floor = float p2-листинга
- p1: floor = float p1-листинга

Скидка = K=2% за каждые 0.01 разницы, cap −8%. Если наш float ЛУЧШЕ floor'а
(дельта ≤ 0.005) — discount=0.

**Общий floor:** `final ≥ anchor × 0.85` (не уезжаем больше чем −15% от стенки).

**reason** содержит всю логику, например:
`WALL·SPARSE·FAST·FALLING anchor=wall=0.43 (conc=52% n=13/25 p1=0.23) undercut −0.01=0.42 mod -2% float-disc -8.0% (our=0.3500 floor=0.2000) → 0.38 (spread=73.9% daily=80 week=-8.0%)`

---

### Path C — редкий паттерн
Никакой автоматики. Только ручной ввод. `is_rare=None` (uncertain) тоже трактуется как C.

---

### Утилиты

```python
def classify(name: str, paint_seed: int | None) -> "A" | "B" | "C"
def daily_sales_from_history(history, days=30) -> float
def week_pct_from_history(history) -> float | None
```

---

## item_info.py — публичный API

```python
# Резолвинг ID:
await resolve_item_nameid(client, app_id, market_hash_name) -> int | None
await resolve_gid(client, app_id, market_hash_name) -> str | None
  # GID для нового Steam Market 2026
  # cache.get_cached_gid → _fetch_item_page (301 redirect) → regex → cache.cache_gid

# Новый Steam Market 2026 API:
async def _fetch_listings_page(
    session, app_id, gid, *,
    start, sort_field=0, sort_dir=0,
    category_filters=None,      # {"category_730_Quality": ["tag_normal"]}
    wear_range=None,            # (float_min, float_max)
    seed_range=None,            # (int_min, int_max) — Steam хочет str!
    price_range=None,           # (unMin_cents, unMax_cents)
    text_query=None,
    currency_code=None,
) -> dict | None
  # POST /market/listings/{app_id}/{GID} с JSON body
  # Возвращает {listings, total_count, more, facets}

def _parse_listings_v2(data: dict) -> list[dict]
def _default_filters_from_name(name: str) -> (quality_tags, exterior_tags)

# Рендеринг:
render_histogram_block / render_price_chart_block / render_sales_volume_block
render_data_table / render_full_stack_block / render_listings_page

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

### Авто-ценообразование
```python
async def _auto_suggest_price(
    client, currency_enum, currency_code, *,
    name, paint_seed, paint_wear,
) -> tuple[int, str, "A"|"B"|"C"] | None:
    # Одиночный предмет. Path C → None. A → histogram. B → resolve_gid → path_b_suggest.

def _make_auto_price_callback(client, currency_enum, currency_code, item):
    # async-callback для _ask_price_cents(a_callback=...) при одиночном листинге

async def _auto_price_show_filter_listings(   # NEW
    client, app_id, gid, *,
    quality_tag, our_float, our_seed, currency_code, cur_sym, name,
) -> None:
    # Команда `i <N>` в _auto_price_group — показывает листинги под тем же фильтром что path_b.
    # Печатает топ-20: price / float / seed, маркирует * наш seed.
    # Нужна чтобы понимать ПОЧЕМУ авто-цена дала именно такую цифру.

async def _auto_price_group(
    client, currency_enum, currency_code, *,
    name, group: list[dict], label_lookup, cur_sym,
) -> ("uniform", cents) | ("per_item", {asset_id: cents}) | ("skip", None) | None:
    # Группа кандидатов cross-account bulk-list.
    # Path A → одна цена для всех → y/число/skip/n
    # Path B/C → таблица по каждому экз.:
    #   # / acc / float / seed / suggest / cur / path / reason
    # Команды: y / edit N PRICE / i N (→ _auto_price_show_filter_listings) / skip N / n
    # Пауза 0.5с между POST'ами path_b для разных флоатов (защита от 429)
    # week_pct теперь передаётся в path_b_suggest
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
await _bulk_cancel_cross_account(name, listed_rows, accounts_lookup, sessions, ...)
await _collect_all_listings(accounts, sessions, force_relogin)
  # Сбор ВСЕХ листингов: ProxyRotator → get_my_listings страницами → record_listings(partial=False)
  # Пауза 4-8с (random) между аккаунтами
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
2. **Steam Market 2026**: листинги через GID (POST), Path A по-прежнему через nameid+histogram
3. **seed_range в POST**: Steam требует `int_min`/`int_max` как строки, иначе возвращает seed=0
4. **buyer-facing цена**: `unPricePerUnit + unFeePerUnit` (не только unPricePerUnit!)
5. **exterior_tag в Path B**: намеренно НЕ используется — иначе срезаем конкурирующие FN/MW листинги
6. **Outlier-защита Path B**: первый листинг < 25% от второго → пропускаем как дампера
7. **Throttle Path B**: 0.5с между POST'ами при обработке группы (защита от 429)
8. **Троттлинг листинга**: 0.3–0.4с между place_sell, 0.5с между аккаунтами, 4-8с при сборе листингов
9. **429 retry**: `_with_retry` — 3 попытки, задержки 3/8/20с
10. **CSFloat**: ~30 req/min, пауза 2с, httpx предпочтительнее aiohttp (TLS fingerprint)
11. **Public inventory diff**: чистая aiohttp-сессия DummyCookieJar — иначе Steam 401/403
12. **Path C** (редкий + uncertain): только ручной ввод, никакой автоматики
13. **Авто-трейд**: items_to_give=[] — строгая проверка, иначе пропуск

---

## Текущая задача
<!-- ЗАПОЛНЯЙ ЭТО ПОЛЕ КАЖДУЮ СЕССИЮ -->
_Опиши что сейчас делаешь: какой модуль правишь, какая фича нужна, что не работает_
