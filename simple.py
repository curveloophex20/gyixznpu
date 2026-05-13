"""Простой однофайловый скрипт: вход в Steam по mafile + меню функций.

Запустил, авторизовался один раз (или используется кеш сессии) — и в меню
можно крутить разные действия, не перелогиниваясь:

    1. Баланс кошелька
    2. Активные buy-ордера и sell-листинги на маркете
    3. История последних 10 событий маркета (покупки / продажи / листинги)
    4. Инвентари: Steam-карточки, CS2, Dota 2, TF 2 (для CS2 — с float и seed)
    0. Выход

Используется библиотека **aiosteampy** (v0.7) — у `steampy` сейчас поломан
login flow (Steam изменил endpoints), а `aiosteampy` корректно проходит весь
auth, генерирует Steam Guard код из mafile и возвращает данные.

Как пользоваться:
    1. В терминале установи зависимости (один раз):
           pip install "aiosteampy>=0.7" "protobuf>=5.26" python-dotenv
    2. Заполни 2 переменные ниже (STEAM_PASSWORD / MAFILE_PATH).
       Логин и steam_id скрипт возьмёт ИЗ САМОГО maFile (поля `account_name`
       и `Session.SteamID`), руками вводить не надо.
       Если хочешь хранить пароль отдельно — создай рядом со скриптом файл
       `.env` со строкой `STEAM_PASSWORD=...`.
    3. Нажми ▶️ в VS Code или запусти `python simple.py`.

После первого успешного логина cookies сохранятся в `.steam_session/`,
и при следующих запусках скрипт сразу попадёт в меню без логина (пока
живёт refresh-токен — обычно от 30 дней до полугода для «доверенных» ПК).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import steam_errors  # классификатор + короткий форматтер ошибок Steam

# =============================================================================
# 1) ТВОИ ДАННЫЕ — заполни эти строки (или используй .env рядом со скриптом)
# =============================================================================
STEAM_PASSWORD = "your_password"
MAFILE_PATH = "secrets/your.maFile"  # путь к maFile из Steam Desktop Authenticator

# Опционально — обычно подтягивается из maFile, но можно и принудительно задать:
STEAM_USERNAME = ""  # пусто → возьмётся `account_name` из maFile
STEAM_ID = 0  # 0 → возьмётся `Session.SteamID` из maFile

# Если хочешь принудительно перезалогиниться (игнорируя кеш) — поставь True:
FORCE_RELOGIN = False

# Сколько предметов из инвентаря показывать на одной странице (как в браузерном инвентаре):
INVENTORY_PAGE_SIZE = 25
# Сколько событий маркета на одной странице:
HISTORY_PAGE_SIZE = 10
# Сколько sell-листингов на одной странице:
LISTINGS_PAGE_SIZE = 10
# =============================================================================

SESSION_DIR_NAME = ".steam_session"  # рядом со скриптом, в .gitignore


# =============================================================================
# Утилиты конфига
# =============================================================================
def _load_dotenv_if_present() -> None:
    """Если рядом со скриптом есть .env — подхватим переменные оттуда."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).with_name(".env"))


def _extract_steam_id(mafile_data: dict) -> int | None:
    """maFile может хранить SteamID в разных местах — ищем во всех."""
    candidates = [
        mafile_data.get("Session", {}).get("SteamID")
        if isinstance(mafile_data.get("Session"), dict)
        else None,
        mafile_data.get("steamid"),
        mafile_data.get("SteamID"),
        mafile_data.get("steam_id"),
    ]
    for value in candidates:
        if value is None:
            continue
        try:
            sid = int(value)
        except (TypeError, ValueError):
            continue
        if sid > 0:
            return sid
    return None


# =============================================================================
# Вспомогательные форматтеры
# =============================================================================
_CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "RUB": "₽",
    "UAH": "₴",
    "KZT": "₸",
    "JPY": "¥",
    "CNY": "¥",
    "TRY": "₺",
    "BRL": "R$",
    "INR": "₹",
}


def _currency_symbol(currency_enum, currency_code: int) -> str:
    try:
        iso = currency_enum(currency_code).name
    except ValueError:
        iso = f"CUR{currency_code}"
    return _CURRENCY_SYMBOLS.get(iso, iso)


def _format_price(cents: int | None, currency_enum, currency_code: int) -> str:
    if cents is None:
        return "—"
    return f"{cents / 100:.2f} {_currency_symbol(currency_enum, currency_code)}"


def _format_listing_price(net_cents: int | None, currency_enum, currency_code: int) -> str:
    """Формат цены листинга: «50.00 € (получишь 43.49 €)».

    `net_cents` — это `lst.price` из aiosteampy для своих листингов
    (Steam отдаёт ту сумму, что мы получим — без комиссии).
    """
    if net_cents is None:
        return "—"
    sym = _currency_symbol(currency_enum, currency_code)
    try:
        from aiosteampy.utils import receive_to_buyer_pays

        _s_fee, _p_fee, buyer_pays = receive_to_buyer_pays(net_cents)
    except Exception:  # noqa: BLE001
        return f"{net_cents / 100:.2f} {sym}"
    return f"{buyer_pays / 100:.2f} {sym} (получишь {net_cents / 100:.2f} {sym})"


def _format_token_lifetime(client) -> str | None:
    """Срок жизни refresh-токена; None если нет данных."""
    try:
        decoded = client.refresh_token_decoded
    except Exception:  # noqa: BLE001
        return None
    if not decoded:
        return None
    exp = decoded.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    seconds_left = int(exp) - int(time.time())
    expires_at = datetime.fromtimestamp(int(exp), tz=timezone.utc)
    iso = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    if seconds_left <= 0:
        return f"Refresh token уже протух (истёк {iso}) — при следующем запуске будет релогин."
    days, rem = divmod(seconds_left, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes and not days:
        parts.append(f"{minutes}мин")
    return f"Refresh token живёт ещё {' '.join(parts) or '<1мин'} (до {iso})."


# =============================================================================
# Парсинг CS2-специфичных свойств (float, paint seed) из EconItem.properties.
# Steam с 2024 г. сам отдаёт эти значения в инвентаре — protobuf-блок парсить
# не нужно: aiosteampy уже распарсил их в `EconItem.properties`.
# =============================================================================
# https://steamapi.xpaw.me/IEconService#GetAssetPropertySchema
CS2_PROP_PAINT_SEED = 1  # int  — паттерн (paint seed)
CS2_PROP_WEAR = 2  # float — износ (paintwear / float value)
CS2_PROP_STICKER_WEAR = 4  # float — степень износа стикера (0.0 = новый, 1.0 = стёртый)

# Регексы для парсинга стикеров/чармов из HTML описания.
# Steam отдаёт что-то типа:
#   <center><img src="..." title="Sticker name 1"><img src="..." title="Sticker name 2">
#   <br>Sticker: <i>name 1</i>, <i>name 2</i></center>
# Имена есть и в title="" у img, и в <i></i> в тексте — берём из img (надёжнее).
_STICKER_IMG_RE = re.compile(r'<img[^>]*title="([^"]*)"[^>]*>', re.IGNORECASE)
# Известные propertyid для CS2: скины/стикеры. Всё остальное (charm pattern и т.д.)
# Steam отдаёт с разными id, но в имени (`prop.name`) всегда есть подсказка.
_CS2_KNOWN_PROP_IDS = {CS2_PROP_PAINT_SEED, CS2_PROP_WEAR, CS2_PROP_STICKER_WEAR}


def _cs2_extract_wear_seed(item) -> tuple[float | None, int | None]:
    """Возвращает (float, seed) для CS2-предмета или (None, None) если их нет."""
    wear: float | None = None
    seed: int | None = None
    for prop in item.properties or ():
        if prop.id == CS2_PROP_WEAR and prop.float_value is not None:
            wear = prop.float_value
        elif prop.id == CS2_PROP_PAINT_SEED and prop.int_value is not None:
            seed = prop.int_value
    return wear, seed


def _cs2_extract_stickers(item) -> list[tuple[str, float | None]]:
    """Возвращает [(имя стикера, износ 0..1 или None), ...] для CS2-предмета.

    Имена парсятся из HTML описания (атрибут title= у img-тегов внутри блока
    «Sticker: ...»). Износ — из item.properties с propertyid=4 (берём в порядке
    появления, по слотам). Если стикеров нет — возвращаем пустой список.
    """
    descr = item.description
    if not descr or not descr.descriptions:
        return []

    sticker_names: list[str] = []
    for entry in descr.descriptions:
        value = entry.value or ""
        # Steam в одном описании держит и блок Sticker:, и блок Charm:; для оружий
        # стикеры всегда префиксованы «Sticker:». Чармы (id=4 keychain) пропустим.
        if "Sticker:" not in value:
            continue
        for raw_name in _STICKER_IMG_RE.findall(value):
            # title часто уже содержит «Sticker: name» — отрежем префикс.
            name = raw_name.strip()
            if name.lower().startswith("sticker:"):
                name = name.split(":", 1)[1].strip()
            sticker_names.append(name)

    if not sticker_names:
        return []

    # Износы — все property с id=4, в порядке слотов 0..N.
    wear_levels: list[float] = [
        prop.float_value
        for prop in item.properties or ()
        if prop.id == CS2_PROP_STICKER_WEAR and prop.float_value is not None
    ]

    pairs: list[tuple[str, float | None]] = []
    for i, name in enumerate(sticker_names):
        wear = wear_levels[i] if i < len(wear_levels) else None
        pairs.append((name, wear))
    return pairs


def _cs2_extract_charms(item) -> list[tuple[str, int | None]]:
    """Возвращает [(имя чарма, pattern или None), ...].

    Steam рендерит «брелоки» (а заодно sticker capsule, sticker slab — крафт стикеров,
    которые ставятся на тыльную сторону пушки) в одном HTML-блоке
    `<div id="keychain_info">` с <img title="...">. Префикс title может быть
    «Charm:», «Keychain:», «Sticker Slab:», «Sticker Capsule:» — отрезаем все.
    Pattern, если приходит, — int-property с именем, содержащим keychain/charm/slab.
    """
    descr = item.description
    if not descr or not descr.descriptions:
        return []

    charm_names: list[str] = []
    for entry in descr.descriptions:
        value = entry.value or ""
        # Главный признак — div с id="keychain_info". Старые поля Charm:/Keychain:
        # тоже поддерживаем на всякий случай.
        if 'id="keychain_info"' not in value and "Charm:" not in value and "Keychain:" not in value:
            continue
        for raw_name in _STICKER_IMG_RE.findall(value):
            name = raw_name.strip()
            for prefix in ("Sticker Capsule:", "Sticker Slab:", "Charm:", "Keychain:"):
                if name.lower().startswith(prefix.lower()):
                    name = name[len(prefix) :].strip()
                    break
            if name:
                charm_names.append(name)

    if not charm_names:
        return []

    # Pattern ищем по имени свойства. На практике Steam использует имена
    # вроде "keychain_pattern" / "charm_pattern" / "charmpattern"; но их может не быть
    # совсем — тогда ищем просто int-пропы с неизвестным id.
    patterns: list[int] = []
    for prop in item.properties or ():
        prop_name = (prop.name or "").lower()
        if ("charm" in prop_name or "keychain" in prop_name) and prop.int_value is not None:
            patterns.append(prop.int_value)
    if not patterns:
        # fallback: иногда Steam отдаёт charm pattern без name — берём любой
        # int-property с неизвестным id (всё, что не paint_seed/wear/sticker_wear).
        for prop in item.properties or ():
            if prop.id in _CS2_KNOWN_PROP_IDS:
                continue
            if prop.int_value is not None:
                patterns.append(prop.int_value)

    pairs: list[tuple[str, int | None]] = []
    for i, name in enumerate(charm_names):
        pattern = patterns[i] if i < len(patterns) else None
        pairs.append((name, pattern))
    return pairs


# =============================================================================
# Меню — вспомогательные функции ввода
# =============================================================================
async def _ask(prompt: str) -> str:
    """`input()` в потоке, чтобы не блокировать event loop."""
    return (await asyncio.to_thread(input, prompt)).strip()


async def _press_enter_to_continue() -> None:
    await _ask("\nEnter — назад в меню ...")


async def _ask_int(prompt: str, default: int, *, allowed: set[int] | None = None) -> int:
    """Спросить число; пустой ввод → default; вне allowed → переспрос."""
    while True:
        raw = (await _ask(prompt)).strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("   Это не число, попробуй ещё раз.")
            continue
        if allowed is not None and value not in allowed:
            print(f"   Допустимо только: {sorted(allowed)}")
            continue
        return value


async def _ask_yes_no(prompt: str, *, default_no: bool = True) -> bool:
    """Спросить y/n с дефолтом."""
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    raw = (await _ask(prompt + suffix)).strip().lower()
    if not raw:
        return not default_no
    return raw in ("y", "yes", "д", "да")


async def _with_retry(coro_factory, *, what: str, attempts: int = 3):
    """Выполнить async-операцию с retry на 429 Too Many Requests.

    Steam охотно отдаёт 429 если запросы идут слишком плотно. Спим
    с экспоненциальным backoff и пробуем снова. На последнем падении
    бросаем исключение наверх.
    """
    delays = [3, 8, 20]
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            is_429 = status == 429 or "429" in str(exc) or "Too Many Requests" in str(exc)
            if not is_429 or attempt == attempts:
                raise
            wait_s = delays[min(attempt - 1, len(delays) - 1)]
            print(
                f"   [!] Steam ответил 429 ({what}). Жду {wait_s}с и пробую снова "
                f"({attempt}/{attempts - 1}) ..."
            )
            await asyncio.sleep(wait_s)
    return None  # unreachable


async def _cancel_all_pending_confirmations(client, *, label: str = "") -> tuple[int, int]:
    """Снимает ВСЕ листинги из 'My listings awaiting confirmation' у аккаунта.

    Делается перед bulk-выставлением чтобы:
      - не накапливать «зависшие» от прошлых попыток,
      - в случае дубля Steam не вернул confused-ответ.

    Возвращает (n_found, n_cancelled). Если найденных нет — тихо возвращает (0, 0).
    """
    try:
        active, to_confirm, _bo, _total = await _with_retry(
            lambda: client.get_my_listings(start=0, count=100),
            what=f"get_my_listings (cleanup-pending {label})",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] cleanup-pending {label}: не смог получить листинги: {exc!r}")
        return (0, 0)

    pending = list(to_confirm or [])
    if not pending:
        return (0, 0)

    print(f"   [..] cleanup-pending {label}: найдено {len(pending)} зависших — отменяю ...")
    cancelled = 0
    for lst in pending:
        lid = getattr(lst, "id", None)
        if lid is None:
            continue
        async def _do_cancel(_lid=lid):
            cm = client.cancel_sell_listing(_lid)
            async with cm as resp:
                return resp.status
        try:
            status = await _with_retry(
                _do_cancel, what=f"cancel_sell_listing (cleanup lid={lid})",
            )
            if status is None or status < 400:
                cancelled += 1
        except Exception as exc:  # noqa: BLE001
            print(f"       [!] cancel {lid} failed: {exc!r}")
        await asyncio.sleep(0.3)

    print(f"   [OK] cleanup-pending {label}: отменено {cancelled}/{len(pending)}.")
    if cancelled > 0:
        await asyncio.sleep(1.5)  # Дать Steam время вернуть предметы в инвентарь
    return (len(pending), cancelled)


async def _place_sell_listing_with_retry(
    client,
    item_or_asset_id,
    app_context,
    *,
    price: int,
    what: str,
):
    """place_sell_listing с авто-ретраем 429 (через `_with_retry`) и доп. ретраем
    при сбое mobile-confirm (Steam: «Failed to perform confirmation action»).

    Поведение при confirm-сбое (задача 1 из бэклога):
        1) выждать пару секунд,
        2) найти pending-листинг этого asset_id в `listings_to_confirm`/`active`,
        3) `cancel_sell_listing` → предмет возвращается в инвентарь,
        4) `place_sell_listing` повторно (1 раз).

    `app_context` обязателен, если `item_or_asset_id` — это int (asset_id).
    Для EconItem можно передать None — aiosteampy достанет app_context из объекта.
    """
    asset_id = getattr(item_or_asset_id, "asset_id", None) or int(item_or_asset_id)

    def _build_call():
        if app_context is None:
            return lambda: client.place_sell_listing(
                item_or_asset_id, price=price, confirm=True,
            )
        return lambda: client.place_sell_listing(
            item_or_asset_id, app_context, price=price, confirm=True,
        )

    try:
        return await _with_retry(_build_call(), what=what)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        is_confirm_fail = (
            "perform confirmation" in msg
            or "confirmation action" in msg
        )
        if not is_confirm_fail:
            raise
        print(
            f"   [!] {what}: подтверждение листинга упало ({exc!r}).\n"
            "       Пробую найти pending-листинг и пересоздать заново."
        )
        await asyncio.sleep(2.5)

        # Find pending listing in to_confirm/active by asset_id.
        pending_id = None
        try:
            active, to_confirm, _bo, _total = await _with_retry(
                lambda: client.get_my_listings(start=0, count=100),
                what="get_my_listings (place-retry lookup)",
            )
            for lst in list(to_confirm or []) + list(active or []):
                try:
                    item = getattr(lst, "item", None)
                    if item is None:
                        continue
                    if str(getattr(item, "asset_id", None)) == str(asset_id):
                        pending_id = lst.id
                        break
                    unowned = getattr(item, "unowned_id", None)
                    if unowned is not None and str(unowned) == str(asset_id):
                        pending_id = lst.id
                        break
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            print(f"       [!] Не смог получить листинги для поиска pending: {e!r}.")
            raise exc

        if pending_id is None:
            print(
                "       [..] Pending-листинг не найден (Steam, возможно, не успел "
                "его записать). Просто повторно выставляю."
            )
        else:
            print(f"       [..] Отменяю pending-листинг {pending_id} ...")
            async def _do_cancel(lid=pending_id):
                cm = client.cancel_sell_listing(lid)
                async with cm as resp:
                    return resp.status
            try:
                status = await _with_retry(
                    _do_cancel, what=f"cancel_sell_listing (retry, lid={pending_id})",
                )
                if status is not None and status >= 400:
                    raise RuntimeError(f"HTTP {status}")
            except Exception as e:  # noqa: BLE001
                print(
                    f"       [!] Не смог отменить pending-листинг {pending_id}: {e!r}.\n"
                    "       Повторно выставлять не буду — нужно почистить вручную."
                )
                raise exc
            await asyncio.sleep(1.5)

        # One more attempt.
        try:
            result = await _with_retry(_build_call(), what=f"{what} (retry)")
            print(f"   [OK] {what}: перевыставлено успешно.")
            return result
        except Exception as exc2:  # noqa: BLE001
            print(f"       [!] Повторное выставление тоже упало: {exc2!r}")
            raise


# =============================================================================
# Команды меню
# =============================================================================
async def menu_balance(client, currency_enum, currency_code: int) -> None:
    """1) Баланс кошелька + что в холде."""
    print("\n=== БАЛАНС ===")
    info = await client.get_wallet_info()
    balance_cents = int(info.get("wallet_balance", 0))
    on_hold_cents = int(info.get("wallet_delayed_balance", 0))
    code = int(info.get("wallet_currency", 0)) or currency_code
    country = info.get("wallet_country") or "?"
    symbol = _currency_symbol(currency_enum, code)
    iso = currency_enum(code).name if code else "?"

    print(f"Доступно:  {balance_cents / 100:.2f} {symbol}  ({iso}, страна {country})")
    if on_hold_cents:
        print(f"В холде:   {on_hold_cents / 100:.2f} {symbol}")

    # Пишем в SQLite-кеш для сводки между аккаунтами.
    try:
        import cache

        cache.record_balance(client.username, balance_cents, on_hold_cents, code)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог записать в кеш: {exc!r}")


async def _paginate(
    items,
    page_size: int,
    render,
    *,
    extra_commands: dict | None = None,
    bulk_commands: dict | None = None,
) -> None:
    """Универсальный постраничный просмотрщик.

    render(items_slice, start_index_1based) — рисует одну страницу.
    extra_commands: {"key": async func(item, idx_1based, items_list)} — действие
        над выбранным элементом, формат `<key> <N>`.
    bulk_commands: {"key": async func(items_list)} — действие без аргумента,
        например, «снять все отфильтрованные».
    """
    if not items:
        return
    page = 0
    while True:
        # Пересчитываем total_pages в КАЖДОЙ итерации — список items может
        # измениться (например, после bulk-cancel мы выкидываем снятые листинги).
        if not items:
            print("\n(пусто)")
            return
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        if page >= total_pages:
            page = total_pages - 1
        start = page * page_size
        end = min(start + page_size, len(items))
        print(
            f"\n--- Страница {page + 1}/{total_pages}"
            f"   (элементы {start + 1}–{end} из {len(items)}) ---"
        )
        render(items[start:end], start + 1)
        if total_pages <= 1 and not extra_commands and not bulk_commands:
            return

        extra_hint = ""
        if extra_commands:
            extra_hint += " / " + " / ".join(f"{k} <N>" for k in extra_commands.keys())
        if bulk_commands:
            extra_hint += " / " + " / ".join(bulk_commands.keys())
        prompt_tail = (
            " | n=след / p=пред / f=первая / l=последняя" + extra_hint + " / Enter=выход: "
        )
        cmd_raw = (await _ask(f"\n   страница {page + 1}/{total_pages}" + prompt_tail)).strip()
        cmd_lower = cmd_raw.lower()
        if cmd_lower in ("", "q", "e", "exit", "quit"):
            return
        if cmd_lower == "n":
            if total_pages == 1 or page + 1 >= total_pages:
                print("   Это последняя страница.")
            else:
                page = (page + 1) % total_pages
        elif cmd_lower == "p":
            if total_pages == 1 or page == 0:
                print("   Это первая страница.")
            else:
                page = (page - 1) % total_pages
        elif cmd_lower == "f":
            page = 0
        elif cmd_lower == "l":
            page = total_pages - 1
        elif bulk_commands and cmd_lower in bulk_commands:
            try:
                await bulk_commands[cmd_lower](items)
            except Exception as exc:  # noqa: BLE001
                print(f"   [ERROR] {exc!r}")
                traceback.print_exc()
        elif extra_commands:
            head, _sep, tail = cmd_raw.partition(" ")
            head_l = head.lower()
            handler = extra_commands.get(head_l)
            if (
                handler is None
                and len(head_l) >= 2
                and head_l[0] in extra_commands
                and head_l[1:].isdigit()
            ):
                handler = extra_commands[head_l[0]]
                tail = head_l[1:]
                head_l = head_l[0]
            if handler is None:
                print(
                    "   Не понял — n / p / f / l / "
                    + " / ".join(extra_commands.keys())
                    + (" / " + " / ".join(bulk_commands.keys()) if bulk_commands else "")
                    + " / Enter"
                )
                continue
            try:
                idx_1b = int(tail)
            except ValueError:
                print(f"   Команда «{head_l}» ждёт номер элемента, например: {head_l} 3")
                continue
            if not (1 <= idx_1b <= len(items)):
                print(f"   Номер вне диапазона: 1..{len(items)}.")
                continue
            try:
                await handler(items[idx_1b - 1], idx_1b, items)
            except Exception as exc:  # noqa: BLE001
                print(f"   [ERROR] {exc!r}")
                traceback.print_exc()
        else:
            print("   Не понял — n / p / f / l / Enter")


async def _paginate_lazy(
    total: int,
    page_size: int,
    fetch_more,
    render,
    *,
    extra_commands: dict | None = None,
) -> None:
    """Пагинатор с random-access по страницам.

    fetch_more(start, count) -> list — загружает именно запрошенный диапазон.
    Прыжок на последнюю (`l`) или произвольную (`g <N>`) страницу НЕ требует
    последовательной догрузки всех предыдущих — сразу запрашивается нужный диапазон.

    extra_commands: {"key": async func(item, idx_1based, loaded_dict)} — для действий
        над выбранным элементом (например, "c" = снять с продажи).
    """
    if total <= 0:
        return
    loaded: dict[int, object] = {}  # ключ — глобальный 0-based индекс
    page = 0
    total_pages = max(1, (total + page_size - 1) // page_size)

    async def _ensure_page(p: int) -> tuple[int, int]:
        """Гарантирует, что элементы страницы p загружены. Возвращает (start, actual_end)."""
        ps = p * page_size
        pe = min(ps + page_size, total)
        missing = [j for j in range(ps, pe) if j not in loaded]
        if missing:
            fetch_start = missing[0]
            fetch_count = pe - fetch_start
            print(
                f"[..] Загружаю элементы {fetch_start + 1}-"
                f"{fetch_start + fetch_count} из {total} ..."
            )
            try:
                chunk = await _with_retry(
                    lambda: fetch_more(fetch_start, fetch_count),
                    what="загрузка страницы",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"   [ERROR] Не смог загрузить: {exc!r}")
                return ps, ps
            if chunk:
                for offset, it in enumerate(chunk):
                    loaded[fetch_start + offset] = it
        # Вычисляем фактический конец (вдруг Steam вернул меньше).
        actual = max((j for j in range(ps, pe) if j in loaded), default=ps - 1) + 1
        return ps, max(actual, ps)

    while True:
        start, actual_end = await _ensure_page(page)
        if actual_end <= start:
            print("(на этой странице нет данных)")
            return
        page_items = [loaded[j] for j in range(start, actual_end) if j in loaded]
        print(
            f"\n--- Страница {page + 1}/{total_pages}"
            f"   (элементы {start + 1}-{actual_end} из {total}) ---"
        )
        render(page_items, start + 1)

        extra_hint = ""
        if extra_commands:
            extra_hint = " / " + " / ".join(f"{k} <N>" for k in extra_commands.keys())
        prompt_tail = (
            " | n=след / p=пред / f=первая / l=последняя / g <N>=на стр. N"
            + extra_hint
            + " / Enter=выход: "
        )
        cmd_raw = (await _ask(f"\n   страница {page + 1}/{total_pages}" + prompt_tail)).strip()
        cmd_lower = cmd_raw.lower()
        if cmd_lower in ("", "q", "e", "exit", "quit"):
            return
        if cmd_lower == "n":
            if page + 1 >= total_pages:
                print("   Это последняя страница.")
            else:
                page += 1
        elif cmd_lower == "p":
            if page == 0:
                print("   Это первая страница.")
            else:
                page -= 1
        elif cmd_lower == "f":
            page = 0
        elif cmd_lower == "l":
            page = total_pages - 1
        elif cmd_lower.startswith("g"):
            head, _sep, tail = cmd_raw.partition(" ")
            tail_clean = tail.strip() or head[1:].strip()
            try:
                target_page = int(tail_clean)
            except ValueError:
                print("   Команда `g` ждёт номер страницы, например: g 17")
                continue
            if not (1 <= target_page <= total_pages):
                print(f"   Номер страницы вне диапазона: 1..{total_pages}")
                continue
            page = target_page - 1
        elif extra_commands:
            head, _sep, tail = cmd_raw.partition(" ")
            head_l = head.lower()
            handler = extra_commands.get(head_l)
            if handler is None and (
                len(head_l) >= 2 and head_l[0] in extra_commands and head_l[1:].isdigit()
            ):
                handler = extra_commands[head_l[0]]
                tail = head_l[1:]
                head_l = head_l[0]
            if handler is None:
                print(
                    "   Не понял — n / p / f / l / g <N> / "
                    + " / ".join(f"{k} <N>" for k in extra_commands.keys())
                    + " / Enter"
                )
                continue
            try:
                idx_1b = int(tail)
            except ValueError:
                print(f"   Команда «{head_l}» ждёт номер элемента, например: {head_l} 3")
                continue
            if not (1 <= idx_1b <= total):
                print(f"   Номер вне диапазона: 1..{total}.")
                continue
            target = loaded.get(idx_1b - 1)
            if target is None:
                print(
                    f"   Элемент #{idx_1b} ещё не загружен — пролистай на нужную страницу и попробуй снова."
                )
                continue
            try:
                await handler(target, idx_1b, loaded)
            except Exception as exc:  # noqa: BLE001
                print(f"   [ERROR] {exc!r}")
                traceback.print_exc()
        else:
            print("   Не понял — n / p / f / l / g <N> / Enter")


def _resolve_app_for_item(item_or_descr):
    """Возвращает aiosteampy.constants.App из ItemDescription / Item / BuyOrder.

    None если ничего не достать (например, у Steam Cards в нашем кеше
    бывает app=753 но фактически разные подкаталоги).
    """
    try:
        from aiosteampy.constants import App
    except ImportError:
        return None
    # Item имеет .description.app, BuyOrder — .item_description.app,
    # ItemDescription — .app.
    descr = (
        getattr(item_or_descr, "description", None)
        or getattr(item_or_descr, "item_description", None)
        or item_or_descr
    )
    app = getattr(descr, "app", None)
    if app is None:
        return None
    if isinstance(app, App):
        return app
    try:
        return App(int(app))
    except (TypeError, ValueError):
        return None


def _resolve_market_hash_name(item_or_descr) -> str | None:
    descr = (
        getattr(item_or_descr, "description", None)
        or getattr(item_or_descr, "item_description", None)
        or item_or_descr
    )
    return getattr(descr, "market_hash_name", None) or getattr(descr, "name", None)


def _make_item_info_callback(client, currency_enum, currency_code: int, item_or_descr):
    """Возвращает async-callback () -> None: открывает item-info по конкретному
    предмету. Используется как `i_callback` в `_ask_price_cents` — на вводе `i`
    в момент ввода цены пользователь видит график/стаканы перед покупкой.

    Если у item нет распознаваемого App или market_hash_name — callback
    выводит сообщение и не падает.
    """
    async def _cb():
        try:
            import item_info as _ii
        except ImportError as exc:
            print(f"   [BUG] item_info модуль не загружен: {exc}")
            return
        name = _resolve_market_hash_name(item_or_descr)
        if not name:
            print("   [!] У предмета нет market_hash_name — info недоступно.")
            return
        app = _resolve_app_for_item(item_or_descr)
        if app is None:
            print("   [!] Не могу определить app (CS2/Steam/Dota2/TF2) у предмета.")
            return
        sym = _currency_symbol(currency_enum, currency_code) if currency_code else ""
        await _ii.show_item_info_menu(
            client, name, app, currency_enum, currency_code,
            ask=_ask, currency_sym=sym,
        )
    return _cb


async def _auto_suggest_price(
    client, currency_enum, currency_code: int,
    *, name: str, paint_seed: int | None, paint_wear: float | None,
) -> tuple[int, str, str] | None:
    """Авто-подбор цены: Path A (коммодити) / B (с флоатом) / C (редкий).

    Возвращает (cents, reason, "A"|"B"|"C") или None если данных не хватило.
    """
    from aiosteampy.constants import App

    try:
        import item_info as _ii
        import price_suggest as _ps
    except ImportError as exc:  # pragma: no cover
        print(f"   [BUG] модуль не загружен: {exc}")
        return None

    # Скин определяем как CS2; для остальных app'ов авто-цена пока не считается
    # (нет смысла — Path A работает для cs2 коммодити, для прочих игр нужна
    # отдельная валидация).
    app = App.CS2
    app_id = int(app.value)

    path = _ps.classify(name, paint_seed)

    if path == "C":
        try:
            import patterns

            res = patterns.is_rare_pattern(name, int(paint_seed or 0))
            tier = res.tier_note or "?"
        except Exception:  # noqa: BLE001
            tier = "?"
        print(
            f"   ⚠  Path C: «{name}» seed={paint_seed} — редкий паттерн ({tier}).\n"
            f"   Автоматику для редких НЕ применяю — введи цену вручную."
        )
        return None

    # Для Path A/B нам нужны daily_sales (всем) + sell_table (Path A) / GID (Path B).
    history = None
    try:
        history = await client.fetch_price_history(name, app)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] price_history недоступна: {type(exc).__name__}: {exc}")

    daily = _ps.daily_sales_from_history(history) if history else 0.0
    week = _ps.week_pct_from_history(history) if history else None
    if daily <= 0:
        print("   [!] нет daily_sales (price_history пустая / закрыта).")
        return None

    if path == "B":
        gid = await _ii.resolve_gid(client, app_id, name)
        if not gid:
            print("   [!] GID не получен — Path B не доступен.")
            return None
        quality_tags, exterior_tags = _ii._default_filters_from_name(name)  # noqa: SLF001
        q_tag = quality_tags[0] if quality_tags else None
        e_tag = exterior_tags[0] if exterior_tags else None
        sug = await _ps.path_b_suggest(
            client.session, app_id, gid,
            our_float=float(paint_wear or 0.0),
            quality_tag=q_tag,
            exterior_tag=e_tag,
            currency_code=currency_code,
            daily_sales=daily,
        )
        if sug.cents is None:
            return None
        return (sug.cents, sug.reason, "B")

    # Path A: используем histogram (sell_order_table).
    nameid = await _ii.resolve_item_nameid(client, app_id, name)
    if nameid is None:
        print("   [!] item_nameid не получен — Path A без histogram не работает.")
        return None
    try:
        histogram, _ = await client.get_item_orders_histogram(nameid)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] histogram недоступен: {type(exc).__name__}: {exc}")
        return None

    sell_table = []
    for row in histogram.sell_order_table or ():
        p = getattr(row, "price", None)
        q = getattr(row, "quantity", None)
        if p is not None and q is not None:
            sell_table.append((int(p), int(q)))
    if not sell_table:
        print("   [!] sell_order_table пустой.")
        return None
    sug = _ps.path_a_suggest(sell_table, daily, week)
    if sug.cents is None:
        return None
    return (sug.cents, sug.reason, "A")


def _make_auto_price_callback(
    client, currency_enum, currency_code: int, item,
):
    """async-callback для `_ask_price_cents.a_callback`."""
    async def _cb():
        name = _resolve_market_hash_name(item)
        if not name:
            print("   [!] нет market_hash_name — auto-price невозможна.")
            return None
        _wear, seed = _cs2_extract_wear_seed(item)
        return await _auto_suggest_price(
            client, currency_enum, currency_code,
            name=name, paint_seed=seed, paint_wear=_wear,
        )
    return _cb


def _item_info_action_factory(client, currency_enum, currency_code: int, sym: str):
    """Возвращает async-хендлер для команды `i <N>` — показать info по предмету.

    Подходит для buy-orders / sell-listings / inventory меню. Использует
    `item_info.show_item_info_menu`.
    """

    async def _action(item, idx_1b: int, _all):  # noqa: ARG001
        try:
            import item_info
        except ImportError as exc:
            print(f"   [BUG] item_info модуль не загружен: {exc}")
            return
        market_hash_name = _resolve_market_hash_name(item)
        if not market_hash_name:
            print("   [!] У предмета нет market_hash_name — не могу запросить info.")
            return
        app = _resolve_app_for_item(item)
        if app is None:
            print(
                "   [!] Не могу определить app (CS2/Steam/Dota2/TF2) у предмета. "
                "Если это редкий случай — введи market_hash_name вручную через `new`."
            )
            return
        await item_info.show_item_info_menu(
            client,
            market_hash_name,
            app,
            currency_enum,
            currency_code,
            ask=_ask,
            currency_sym=sym,
        )

    return _action


async def _cancel_buy_order_action(order, idx_1b: int, _all):
    """Хендлер `c <N>` в меню buy-ордеров: отменяет ордер по индексу."""
    descr = order.item_description
    name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", "?")
    print(f"   Отменить buy-ордер: «{name}» (id={order.id})")
    if not await _ask_yes_no("   Подтвердить отмену?"):
        print("   Отменено, ничего не меняем.")
        return
    client = _CURRENT_CLIENT
    if client is None:
        print("   [BUG] client не передан в action.")
        return
    print("   [..] Отправляю запрос ...")
    try:
        await _with_retry(
            lambda: client.cancel_buy_order(order.id),
            what="cancel_buy_order",
        )
    except Exception as exc:  # noqa: BLE001
        err = steam_errors.classify_steam_error(exc)
        print("   " + steam_errors.format_for_log(err, prefix="Steam отклонил"))
        return
    print(f"   [OK] buy-ордер {order.id} отменён.")
    # Удаляем из кеша + из списка.
    try:
        import cache

        cache.delete_buy_order(client.username, order.id)
    except Exception as exc:  # noqa: BLE001
        # Не критично — синканёмся при следующем заходе в меню.
        print(f"   [!] Не смог удалить из кеша: {exc!r}")
    if isinstance(_all, list):
        try:
            _all.remove(order)
        except ValueError:
            pass


async def _create_buy_order_handler(buy_orders_list):
    """Хендлер `new` (bulk_command) — создаёт новый buy-ордер.

    Спрашивает:
        1) market_hash_name (по умолчанию подсказывает топ-5 из истории кеша
           inventory_cache того же аккаунта);
        2) цену за штуку (в валюте кошелька);
        3) количество.
    """
    client = _CURRENT_CLIENT
    if client is None:
        print("   [BUG] client не передан в action.")
        return
    cur = getattr(client, "currency", None)
    cur_code = getattr(cur, "value", 0) if cur is not None else 0
    try:
        from aiosteampy.constants import Currency

        sym = _currency_symbol(Currency, cur_code) if cur_code else ""
    except Exception:  # noqa: BLE001
        sym = ""

    # 1) market_hash_name
    print("\n   === НОВЫЙ BUY-ОРДЕР ===")
    print("   (на любом промпте: q/b/Enter — назад)")
    # Подсказки: что чаще встречается в инвентаре этого аккаунта.
    suggestions: list[tuple[str, int]] = []
    try:
        import cache

        with cache._db() as conn:  # noqa: SLF001 — внутренний хелпер тестируется
            rows = conn.execute(
                """
                SELECT market_hash_name, COUNT(*) AS c
                FROM inventory_cache
                WHERE username=? AND market_hash_name IS NOT NULL
                GROUP BY market_hash_name
                ORDER BY c DESC LIMIT 5
                """,
                (client.username,),
            ).fetchall()
            suggestions = [(r[0], r[1]) for r in rows]
    except Exception:  # noqa: BLE001
        suggestions = []
    if suggestions:
        print("   Подсказки из твоего инвентаря на этом аккаунте:")
        for i, (n, c) in enumerate(suggestions, 1):
            print(f"     {i}) {n}  ({c} шт. в инвентаре)")
        print("   Введи market_hash_name либо номер (1..N) из подсказок:")
    raw = (await _ask("   market_hash_name (или N из подсказок): ")).strip()
    if raw == "" or raw.lower() in ("q", "b"):
        print("   Отменено, ордер не создан.")
        return
    if raw.isdigit() and suggestions and 1 <= int(raw) <= len(suggestions):
        market_hash_name = suggestions[int(raw) - 1][0]
        print(f"   Выбран: {market_hash_name}")
    else:
        market_hash_name = raw

    # Определяем app (CS2 / Steam Cards / Dota2 / TF2). По умолчанию CS2 если
    # неясно — пользователь поправит командой.
    from aiosteampy.constants import App

    app_choice_map = {
        "1": (App.CS2, "CS2"),
        "2": (App.STEAM, "Steam Cards"),
        "3": (App.DOTA2, "Dota 2"),
        "4": (App.TF2, "TF 2"),
    }
    # Если подсказка совпала с предметом — попробуем угадать app по
    # inventory_cache.app_context.
    guess: tuple | None = None
    if suggestions and raw.isdigit():
        try:
            import cache

            with cache._db() as conn:  # noqa: SLF001
                row = conn.execute(
                    """
                    SELECT app_context FROM inventory_cache
                    WHERE username=? AND market_hash_name=? LIMIT 1
                    """,
                    (client.username, market_hash_name),
                ).fetchone()
                if row:
                    ctx_name = (row[0] or "").upper()
                    for key, (app_enum, label) in app_choice_map.items():
                        if app_enum.name in ctx_name:
                            guess = (app_enum, label)
                            break
        except Exception:  # noqa: BLE001
            pass
    if guess is not None:
        app_enum, app_label = guess
        print(f"   Игра: {app_label} (определена по инвентарю).")
    else:
        print(
            "   Игра (нужна для place_buy_order):\n"
            "     1) CS2\n     2) Steam Cards\n     3) Dota 2\n     4) TF 2"
        )
        raw_app = (await _ask("   Выбор [1-4]: ")).strip()
        if raw_app == "" or raw_app.lower() in ("q", "b"):
            print("   Отменено, ордер не создан.")
            return
        if raw_app not in app_choice_map:
            print("   Не понял. Отменено.")
            return
        app_enum, app_label = app_choice_map[raw_app]

    # 2) Цена
    raw_price = (
        await _ask(f"   Цена за штуку в {sym or 'валюте кошелька'} (например 0.49, q/b/Enter — назад): ")
    ).strip()
    if raw_price == "" or raw_price.lower() in ("q", "b"):
        print("   Отменено, ордер не создан.")
        return
    try:
        price_cents = int(round(float(raw_price.replace(",", ".")) * 100))
    except ValueError:
        print("   Цена должна быть числом. Отменено.")
        return
    if price_cents <= 0:
        print("   Цена должна быть > 0. Отменено.")
        return

    # 3) Количество
    raw_qty = (await _ask("   Количество (например 10): ")).strip()
    if raw_qty == "" or raw_qty.lower() in ("q", "b"):
        print("   Отменено, ордер не создан.")
        return
    try:
        qty = int(raw_qty)
    except ValueError:
        print("   Количество должно быть целым. Отменено.")
        return
    if qty <= 0:
        print("   Количество > 0. Отменено.")
        return

    total = price_cents * qty
    print(
        f"\n   Создать buy-ордер: «{market_hash_name}» ({app_label})\n"
        f"     цена: {price_cents / 100:.2f} {sym} × {qty} шт. = {total / 100:.2f} {sym}"
    )
    if not await _ask_yes_no("   Подтвердить?"):
        print("   Отменено, ордер не создан.")
        return

    print("   [..] Отправляю запрос ...")
    try:
        result = await _with_retry(
            lambda: client.place_buy_order(
                market_hash_name, app=app_enum, price=price_cents, quantity=qty
            ),
            what="place_buy_order",
        )
    except Exception as exc:  # noqa: BLE001
        err = steam_errors.classify_steam_error(exc)
        print("   " + steam_errors.format_for_log(err, prefix="Steam отклонил"))
        return
    print(f"   [OK] Buy-ордер создан (id={result}).")

    # Обновляем список buy_orders на месте — refetch с Steam'а и заменяем
    # содержимое `buy_orders_list` (`_paginate` рендерит его при следующем
    # цикле). Это работает, потому что bulk_command получает ссылку на
    # `items` из вызывающего `_paginate`.
    try:
        _active, _to_confirm, fresh_orders, _total = await _with_retry(
            lambda: client.get_my_listings(start=0, count=LISTINGS_PAGE_SIZE),
            what="get_my_listings (refresh after new)",
        )
        if isinstance(buy_orders_list, list):
            buy_orders_list[:] = fresh_orders
        # И в кеш тоже.
        try:
            import cache

            cache.record_buy_orders(client.username, fresh_orders, getattr(cur, "value", 0) or 0)
        except Exception:  # noqa: BLE001
            pass
        print(f"   (Список обновлён: теперь {len(fresh_orders)} buy-ордеров.)")
    except Exception as exc:  # noqa: BLE001
        # Refresh не критичен — ордер уже создан.
        print(f"   [!] Не смог обновить список ({exc!r}). Перезайди в меню — увидишь его.")


async def menu_buy_orders(client, currency_enum, currency_code: int) -> None:
    """2) Активные buy-ордера + создание/отмена."""
    print("\n=== BUY-ОРДЕРА ===")
    print("[..] Загружаю ...")
    _active, _to_confirm, buy_orders, _total = await _with_retry(
        lambda: client.get_my_listings(start=0, count=LISTINGS_PAGE_SIZE),
        what="get_my_listings",
    )
    print(f"Buy-ордеров: {len(buy_orders)}.")

    # Пишем в кеш сразу — buy-ордера обычно влезают в одну страницу.
    try:
        import cache

        cache.record_buy_orders(client.username, buy_orders, currency_code)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог записать в кеш: {exc!r}")

    sym = _currency_symbol(currency_enum, currency_code) if currency_code else ""

    def _render_orders(slice_, start_idx):
        for i, order in enumerate(slice_, start=start_idx):
            descr = order.item_description
            name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", "?")
            price_str = _format_price(order.price, currency_enum, currency_code)
            remaining = f"{order.quantity_remaining}/{order.quantity}"
            print(f"   {i:>3}. {name}")
            print(f"        цена за шт: {price_str}  |  осталось: {remaining}  |  id: {order.id}")

    print(
        "   Команды: `c <N>` — отменить ордер, `i <N>` — listings + график цены,\n"
        "           `new` — создать новый ордер."
    )
    info_action = _item_info_action_factory(client, currency_enum, currency_code, sym)
    if not buy_orders:
        print("(нет активных ордеров на покупку — но можно создать новый: `new`)")
        # Даже без существующих ордеров отдельная страница не имеет смысла
        # для _paginate. Дадим короткий промпт; если пользователь создал
        # ордер — рекурсивно перезагрузим меню чтобы показать его.
        choice = (await _ask("   `new` — создать ордер, Enter — назад: ")).strip().lower()
        if choice == "new":
            # buy_orders — это локальная переменная; передаём в неё ссылку,
            # которую обработчик заполнит свежим списком.
            await _create_buy_order_handler(buy_orders)
            if buy_orders:
                # Получился непустой список — заходим в обычный пагер.
                await _paginate(
                    buy_orders,
                    LISTINGS_PAGE_SIZE,
                    _render_orders,
                    extra_commands={"c": _cancel_buy_order_action, "i": info_action},
                    bulk_commands={"new": _create_buy_order_handler},
                )
        return

    await _paginate(
        buy_orders,
        LISTINGS_PAGE_SIZE,
        _render_orders,
        extra_commands={"c": _cancel_buy_order_action, "i": info_action},
        bulk_commands={"new": _create_buy_order_handler},
    )


async def _cancel_listing_action(lst, idx_1b: int, _all):
    """Хендлер команды `c <N>` в меню sell-листингов — снимает листинг с продажи."""
    name = getattr(lst.item.description, "market_hash_name", None) or "?"
    print(f"   Снять с продажи: «{name}» (id={lst.id}) — вернётся в инвентарь.")
    if not await _ask_yes_no("   Подтвердить снятие?"):
        print("   Отменено, ничего не меняем.")
        return
    client = _CURRENT_CLIENT
    if client is None:
        print("   [BUG] client не передан в action.")
        return
    print("   [..] Отправляю запрос ...")

    # cancel_sell_listing возвращает aiohttp _RequestContextManager; нужно зайти
    # в него и дождаться ответа, а не просто await на самом cm.
    async def _do_cancel():
        cm = client.cancel_sell_listing(lst.id)
        async with cm as resp:
            return resp.status

    try:
        status = await _with_retry(_do_cancel, what="cancel_sell_listing")
    except Exception as exc:  # noqa: BLE001
        err = steam_errors.classify_steam_error(exc)
        print("   " + steam_errors.format_for_log(err, prefix="Steam отклонил"))
        return

    if status is None or status < 400:
        print(f"   [OK] Листинг {lst.id} снят. Предмет вернётся в инвентарь.")
        # Удалим из кеша + из in-memory списка чтобы он не висел на следующих страницах.
        try:
            import cache

            cache.delete_listing(client.username, lst.id)
            # F5: предмет вернулся в инвентарь как «free» — отметим в кеше сразу,
            # чтобы он не отображался как `on_market` до следующего sweep'а.
            for aid in (
                getattr(lst.item, "unowned_id", None),
                getattr(lst.item, "asset_id", None),
            ):
                if aid is not None:
                    cache.mark_inventory_state_by_asset_id(client.username, aid, "free")
        except Exception as exc:  # noqa: BLE001
            print(f"   [!] Не смог удалить из кеша: {exc!r}")
        # _all может быть list (из _paginate) или dict {idx: lst} (из _paginate_lazy)
        if isinstance(_all, list):
            try:
                _all.remove(lst)
            except ValueError:
                pass
        elif isinstance(_all, dict):
            for k, v in list(_all.items()):
                if v is lst:
                    del _all[k]
        # #2: предложить снять все остальные с тем же market_hash_name.
        # Только когда _all — это плоский list (т.е. меню filtered/grouped).
        if isinstance(_all, list) and name and name != "?":
            same_name = [
                other
                for other in _all
                if (getattr(other.item.description, "market_hash_name", None) or "") == name
            ]
            if same_name:
                if await _ask_yes_no(f"   Снять и остальные «{name}» — ещё {len(same_name)} шт.?"):
                    before_ids = {x.id for x in same_name}
                    await _bulk_cancel_listings(client, same_name, ask_confirm=False)
                    # same_name был мутирован: остались только не-снятые.
                    still_ids = {x.id for x in same_name}
                    cancelled_ids = before_ids - still_ids
                    if cancelled_ids:
                        _all[:] = [it for it in _all if it.id not in cancelled_ids]
    else:
        print(f"   [!] Steam ответил статусом {status} — проверь в браузере.")


# Глобальный «текущий клиент» — нужен чтобы action-хендлеры в _paginate_lazy
# могли достучаться до него без проброса через все слои.
_CURRENT_CLIENT = None


async def _bulk_cancel_listings(client, listings: list, *, ask_confirm: bool = True) -> int:
    """Снимает с продажи список листингов с подтверждением и троттлингом.

    Передаваемый list **мутируется**: после удачного cancel удалённый листинг
    выкидывается из него. Это нужно чтобы вызывающий пагинатор увидел свежее
    состояние и не продолжал показывать снятые предметы (#1 в фидбеке).

    Возвращает кол-во успешно снятых листингов.
    """
    if not listings:
        print("   (нечего снимать)")
        return 0
    # Группировка для понятного отчёта.
    from collections import Counter

    by_name: Counter = Counter()
    for lst in listings:
        name = getattr(lst.item.description, "market_hash_name", None) or "?"
        by_name[name] += 1
    print(f"\n   К снятию: {len(listings)} листинга(ов)")
    for name, cnt in by_name.most_common(20):
        print(f"     • {name} ×{cnt}")
    if len(by_name) > 20:
        print(f"     ... и ещё {len(by_name) - 20} разных нейма")
    if ask_confirm and not await _ask_yes_no("   Подтвердить массовое снятие?"):
        print("   Отменено.")
        return 0

    cancelled_ids: set = set()
    ok = 0
    fail = 0
    total = len(listings)
    for i, lst in enumerate(list(listings), 1):  # копия — мутируем оригинал ниже

        async def _do_cancel(L=lst):
            cm = client.cancel_sell_listing(L.id)
            async with cm as resp:
                return resp.status

        try:
            status = await _with_retry(_do_cancel, what=f"cancel #{i}")
        except Exception as exc:  # noqa: BLE001
            err = steam_errors.classify_steam_error(exc)
            print(steam_errors.format_for_log(err, prefix=f"#{i} {lst.id}"))
            fail += 1
            # На «fatal-for-batch» ошибках (протухшая сессия и пр.) бессмысленно
            # продолжать — остальные упадут с той же ошибкой.
            if err.fatal_for_batch:
                remaining = total - i
                if remaining > 0:
                    print(f"   [stop] прерываю bulk-cancel: {remaining} пропущено.")
                break
            continue
        if status is None or status < 400:
            ok += 1
            cancelled_ids.add(lst.id)
            # Сразу пишем в кеш — иначе `[на маркете]` маркер останется висеть.
            try:
                import cache

                cache.delete_listing(client.username, lst.id)
                # F5: предмет вернётся в free-состояние сразу.
                for aid in (
                    getattr(lst.item, "unowned_id", None),
                    getattr(lst.item, "asset_id", None),
                ):
                    if aid is not None:
                        cache.mark_inventory_state_by_asset_id(
                            client.username, aid, "free"
                        )
            except Exception as exc:  # noqa: BLE001
                print(f"   [!] не смог удалить из кеша {lst.id}: {exc!r}")
        else:
            fail += 1
            print(f"   [!] #{i} {lst.id}: status {status}")
        if i % 5 == 0 or i == total:
            print(f"   ... {i}/{total} обработано (ok={ok}, fail={fail})")
        # Тонкий троттлинг чтобы не получить 429.
        await asyncio.sleep(0.3)

    # Мутируем переданный список: оставляем только не-снятые.
    if cancelled_ids:
        listings[:] = [lst for lst in listings if lst.id not in cancelled_ids]
    print(f"\n[OK] Снято: {ok}.  Ошибок: {fail}.")
    return ok


async def _menu_sell_listings_filtered(
    client, currency_enum, currency_code: int, query: str
) -> None:
    """Меню sell-листингов с фильтром по части имени.

    Steam игнорирует параметр `query` на /market/mylistings (это поведение
    проверено эмпирически — он возвращает все листинги). Поэтому подгружаем
    все страницы и фильтруем у себя case-insensitive substring match.
    """
    q_lower = query.lower()
    print(f"\n[..] Загружаю все листинги и фильтрую локально по '{query}' ...")
    all_listings: list = []
    start = 0
    page = 100
    total = 0
    while True:
        try:
            active, _to_confirm, _bo, total = await _with_retry(
                lambda s=start: client.get_my_listings(start=s, count=page),
                what="get_my_listings (страница)",
            )
        except KeyError as exc:
            # см. #4: Steam иногда отвечает без `listings` если диапазон вышел за пределы.
            if exc.args and exc.args[0] == "listings":
                break
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {exc!r}")
            return
        if not active:
            break
        all_listings.extend(active)
        if len(all_listings) >= total:
            break
        start += page
        await asyncio.sleep(0.4)

    listings = [
        lst
        for lst in all_listings
        if q_lower in (getattr(lst.item.description, "market_hash_name", "") or "").lower()
    ]

    if not listings:
        print(f"(по запросу '{query}' из {len(all_listings)} листингов ничего не нашлось)")
        return
    print(f"Найдено по запросу '{query}': {len(listings)} из {len(all_listings)}.")

    # Раз уж загрузили все — обновим кеш листингов целиком (replace-all).
    try:
        import cache

        cache.record_listings(client.username, all_listings, currency_code, partial=False)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог записать в кеш: {exc!r}")

    def _render(slice_, start_idx):
        for i, lst in enumerate(slice_, start=start_idx):
            name = getattr(lst.item.description, "market_hash_name", None) or "?"
            price_str = _format_listing_price(lst.price, currency_enum, currency_code)
            when = lst.time_created.strftime("%Y-%m-%d") if lst.time_created else "?"
            print(f"   {i:>3}. {name}")
            print(f"        цена: {price_str}  |  выставлен: {when}  |  id: {lst.id}")

    async def _bulk_cancel_handler(items_list):
        await _bulk_cancel_listings(client, items_list)

    sym = _currency_symbol(currency_enum, currency_code) if currency_code else ""
    info_action = _item_info_action_factory(client, currency_enum, currency_code, sym)
    await _paginate(
        listings,
        LISTINGS_PAGE_SIZE,
        _render,
        extra_commands={"c": _cancel_listing_action, "i": info_action},
        bulk_commands={"cancel-all": _bulk_cancel_handler},
    )


async def menu_sell_listings(client, currency_enum, currency_code: int) -> None:
    """3) Активные sell-листинги (выставленные на продажу)."""
    print("\n=== SELL-ЛИСТИНГИ (выставлено на продажу) ===")
    name_filter = (await _ask("Фильтр по имени предмета (часть названия), Enter — все: ")).strip()
    if name_filter:
        await _menu_sell_listings_filtered(client, currency_enum, currency_code, name_filter)
        return

    print(f"[..] Загружаю первые {LISTINGS_PAGE_SIZE} sell-листингов ...")
    active_listings, listings_to_confirm, _buy_orders, total = await _with_retry(
        lambda: client.get_my_listings(start=0, count=LISTINGS_PAGE_SIZE),
        what="get_my_listings",
    )
    print(f"Всего активных sell-листингов: {total}.")
    if listings_to_confirm:
        print(f"Ждут подтверждения в Steam Mobile App: {len(listings_to_confirm)}.")

    if total == 0:
        print("(нет активных листингов)")
        return

    def _render_listings(slice_, start_idx):
        for i, lst in enumerate(slice_, start=start_idx):
            name = getattr(lst.item.description, "market_hash_name", None) or "?"
            price_str = _format_listing_price(lst.price, currency_enum, currency_code)
            when = lst.time_created.strftime("%Y-%m-%d") if lst.time_created else "?"
            print(f"   {i:>3}. {name}")
            print(f"        цена: {price_str}  |  выставлен: {when}  |  id: {lst.id}")

    already_loaded = list(active_listings)

    # Записываем сразу первую страницу.
    try:
        import cache

        cache.record_listings(client.username, already_loaded, currency_code, partial=True)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог записать в кеш: {exc!r}")

    async def _fetch_more(start_idx: int, count: int):
        if start_idx < len(already_loaded):
            end_idx = min(start_idx + count, len(already_loaded))
            return already_loaded[start_idx:end_idx]
        try:
            more_active, _to_confirm, _bo, _total = await client.get_my_listings(
                start=start_idx, count=count
            )
        except KeyError as exc:
            # #4: после массовых cancel-ов Steam иногда отвечает без `listings`,
            # если запрашиваемый диапазон уже за пределами актуального количества.
            # Это не ошибка с нашей стороны — просто страницы больше нет.
            if exc.args and exc.args[0] == "listings":
                print("   [info] Steam вернул пустую страницу (диапазон за пределами).")
                return []
            raise
        more_list = list(more_active)
        try:
            import cache

            cache.record_listings(client.username, more_list, currency_code, partial=True)
        except Exception:  # noqa: BLE001
            pass
        return more_list

    sym = _currency_symbol(currency_enum, currency_code) if currency_code else ""
    info_action = _item_info_action_factory(client, currency_enum, currency_code, sym)
    await _paginate_lazy(
        total,
        LISTINGS_PAGE_SIZE,
        _fetch_more,
        _render_listings,
        extra_commands={"c": _cancel_listing_action, "i": info_action},
    )

    if listings_to_confirm:
        print(f"\n[ Ждут подтверждения в Steam Mobile App: {len(listings_to_confirm)} ]")
        for lst in listings_to_confirm:
            name = getattr(lst.item.description, "market_hash_name", None) or "?"
            print(f"   • {name} (id: {lst.id})")


_HISTORY_TYPE_LABEL = {
    # имена приходят как MarketHistoryEventType.<NAME>
    "LISTING_CREATED": "выставил",
    "LISTING_CANCELED": "снял",
    "LISTING_CANCELLED": "снял",
    "LISTING_SOLD": "ПРОДАЛ",
    "LISTING_PURCHASED": "КУПИЛ",
    # на всякий случай — старые camelCase варианты
    "ListingCreated": "выставил",
    "ListingCancelled": "снял",
    "ListingSold": "ПРОДАЛ",
    "ListingPurchased": "КУПИЛ",
}


def _history_event_price_cents(ev) -> int | None:
    """Берёт цену события так, как её показывает Steam в веб-UI:

    - КУПИЛ (LISTING_PURCHASED) → GROSS, что списалось с моего кошелька
      = paid_amount + paid_fee (база + комиссия Steam сверху).
    - ПРОДАЛ (LISTING_SOLD) → NET, что я получил после комиссии
      = received_amount (или paid_amount, это та же база без комиссии).
    - ВЫСТАВИЛ / СНЯЛ (LISTING_CREATED / LISTING_CANCELLED) → GROSS-цена листинга
      = price + fee (что увидит покупатель).
    Возвращает int (центы) или None.
    """
    listing = getattr(ev, "listing", None)
    if listing is None:
        return None
    type_name = getattr(getattr(ev, "type", None), "name", "") or ""

    paid_amount = getattr(listing, "paid_amount", None)
    paid_fee = getattr(listing, "paid_fee", None)
    received_amount = getattr(listing, "received_amount", None)
    price = getattr(listing, "price", None)
    fee = getattr(listing, "fee", None)

    if type_name in ("LISTING_PURCHASED", "ListingPurchased"):
        # GROSS — что мы заплатили
        if paid_amount is not None and paid_fee is not None:
            return paid_amount + paid_fee
        return paid_amount

    if type_name in ("LISTING_SOLD", "ListingSold"):
        # NET — что мы получили на кошелёк
        if received_amount is not None:
            return received_amount
        return paid_amount  # та же база без комиссии

    # CREATED / CANCELLED → gross-цена листинга
    if price is not None and fee is not None:
        return price + fee
    if price is not None:
        return price
    if paid_amount is not None and paid_fee is not None:
        return paid_amount + paid_fee
    return paid_amount or received_amount


async def menu_market_history(client, currency_enum, currency_code: int) -> None:
    """4) История событий маркета с lazy-loading пагинацией."""
    print("\n=== ИСТОРИЯ МАРКЕТА ===")
    print(
        "Размер пачки на странице? (10 / 50 / 100). "
        "Дальше можно листать командами n / p / f / l / Enter — следующие пачки догрузятся."
    )
    page_size = await _ask_int("  [по умолчанию 10]: ", default=10, allowed={10, 50, 100})

    # Делаем первый запрос, чтобы узнать total и получить первую пачку.
    print(f"[..] Загружаю первые {page_size} событий ...")
    try:
        first_events, total = await _with_retry(
            lambda: client.get_my_market_history(start=0, count=page_size),
            what="get_my_market_history",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Не удалось загрузить историю: {exc!r}")
        return

    if not first_events or total == 0:
        print("(история пуста)")
        return

    print(f"Всего событий в твоей истории маркета: {total}.")
    already_loaded = list(first_events)

    def _render(slice_, start_idx):
        for i, ev in enumerate(slice_, start=start_idx):
            listing = ev.listing
            name = "?"
            if listing and listing.item and listing.item.description:
                name = getattr(listing.item.description, "market_hash_name", None) or "?"
            when = ev.time_event.strftime("%Y-%m-%d %H:%M") if ev.time_event else "?"
            type_name = getattr(getattr(ev, "type", None), "name", "") or ""
            action = _HISTORY_TYPE_LABEL.get(type_name, type_name or str(ev.type))
            price_cents = _history_event_price_cents(ev)
            price_str = _format_price(price_cents, currency_enum, currency_code)
            print(f"   {i:>3}. [{when}]  {action}: {name}  ({price_str})")

    # Запишем первую страницу истории в кеш (append-only с дедупом).
    try:
        import cache

        added = cache.record_history_events(
            client.username,
            already_loaded,
            currency_code,
            price_extractor=_history_event_price_cents,
        )
        if added:
            print(
                f"   [cache] +{added} новых событий в БД ({len(already_loaded) - added} уже были)."
            )
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог записать в кеш: {exc!r}")

    async def _fetch_more(start_idx: int, count: int):
        if start_idx < len(already_loaded):
            end_idx = min(start_idx + count, len(already_loaded))
            return already_loaded[start_idx:end_idx]
        events, _total = await client.get_my_market_history(start=start_idx, count=count)
        events_list = list(events)
        try:
            import cache

            cache.record_history_events(
                client.username,
                events_list,
                currency_code,
                price_extractor=_history_event_price_cents,
            )
        except Exception:  # noqa: BLE001
            pass
        return events_list

    await _paginate_lazy(total, page_size, _fetch_more, _render)


def _is_trade_protected(item, *, protected_asset_ids: set[str] | None = None) -> bool:
    """True, если предмет под Trade Protection (получен через трейд, 7-дневная защита).

    Самый надёжный признак — предмет пришёл из `AppContext.CS2_PROTECTED`
    (context=16). Если у вызывающего есть `protected_asset_ids` (asset_id'ы
    из protected-контекста) — ориентируемся на этот set.

    Если protected-контекст не догружали (не CS2 или Steam отказал) — fallback
    на текст в `description.owner_descriptions` («trade-protected»).
    """
    if protected_asset_ids is not None and str(item.asset_id) in protected_asset_ids:
        return True
    descr = item.description
    if descr is None:
        return False
    if getattr(descr, "market_tradable_restriction", 0):
        return False  # market-hold, не trade-protect
    od = getattr(descr, "owner_descriptions", None) or ()
    for d in od:
        v = (getattr(d, "value", None) or "").lower()
        if "trade-protected" in v or "trade protection" in v:
            return True
    return False


def _inventory_state(
    item,
    listed_asset_ids: set[str] | None = None,
    protected_asset_ids: set[str] | None = None,
) -> str:
    """Одно из: 'on_market' / 'trade_protect' / 'trade_hold' / 'free'.

    Порядок проверки: на маркете → trade-protect (новый context=16) →
    trade-hold (после покупки с тп). Если ничего из этого — 'free'.

    На маркете перебивает остальное: Steam иногда возвращает «фейковый»
    tradable_after для уже выставленных предметов — это его косяк, наш
    софт должен помечать такие как просто «на маркете».
    """
    if listed_asset_ids and str(item.asset_id) in listed_asset_ids:
        return "on_market"
    if _is_trade_protected(item, protected_asset_ids=protected_asset_ids):
        return "trade_protect"
    if getattr(item, "tradable_after", None) is not None:
        return "trade_hold"
    return "free"


def _format_state_marker(state: str, tradable_after) -> str:
    """Маркер `[на маркете]` / `[trade-hold ещё ...]` / `[trade-protected ещё ...]` / ``."""
    if state == "on_market":
        return "  [на маркете]"
    if state == "free":
        return ""
    if tradable_after is None:
        # trade-hold/protect без даты — печатаем без таймера
        return f"  [{'trade-protected' if state == 'trade_protect' else 'trade-hold'}]"
    now = datetime.now(timezone.utc)
    ta = tradable_after if tradable_after.tzinfo else tradable_after.replace(tzinfo=timezone.utc)
    delta = ta - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return ""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    label = "trade-protected" if state == "trade_protect" else "trade-hold"
    if days > 0:
        return f"  [{label} ещё {days}д {hours}ч]"
    return f"  [{label} ещё {hours}ч]"


def _format_trade_hold(item) -> str:
    """Маркер trade-hold/trade-protect для предмета, без учёта on-market."""
    state = _inventory_state(item)
    if state in ("trade_protect", "trade_hold"):
        return _format_state_marker(state, getattr(item, "tradable_after", None))
    return ""


def _print_inventory_item(
    i: int,
    item,
    *,
    with_cs2_extras: bool,
    listed_asset_ids: set[str] | None = None,
    protected_asset_ids: set[str] | None = None,
) -> None:
    descr = item.description
    name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", "?")
    amount = item.amount
    amount_part = f" ×{amount}" if amount > 1 else ""
    state = _inventory_state(item, listed_asset_ids, protected_asset_ids)
    # Если предмет на маркете — Steam иногда отдаёт «фейковый» tradable_after
    # как часть листинга. Игнорируем его и печатаем только [на маркете].
    state_part = _format_state_marker(state, getattr(item, "tradable_after", None))
    if not with_cs2_extras:
        print(f"   {i:>3}. {name}{amount_part}{state_part}")
        return
    wear, seed = _cs2_extract_wear_seed(item)
    extras: list[str] = []
    if wear is not None:
        extras.append(f"float={wear:.6f}")
    if seed is not None:
        extras.append(f"seed={seed}")
    # Если сам предмет — это чарм/брелок (Charm | Foo), pattern лежит прямо
    # на нём в asset_properties с неизвестным id (Steam меняет схему). Берём
    # любой int-property, который не paint_seed/wear/sticker_wear.
    descr_name = (getattr(descr, "name", "") or "").lower()
    if descr_name.startswith(("charm ", "charm |", "keychain ")):
        for prop in item.properties or ():
            if prop.id in _CS2_KNOWN_PROP_IDS:
                continue
            if prop.int_value is not None:
                extras.append(f"pattern={prop.int_value}")
                break
    extras_str = f"   [{'  '.join(extras)}]" if extras else ""
    print(f"   {i:>3}. {name}{amount_part}{extras_str}{state_part}")
    stickers = _cs2_extract_stickers(item)
    for s_name, s_wear in stickers:
        # Steam не отдаёт wear-property когда износ ровно 0 (новый стикер).
        # Раньше мы печатали «потёртость неизвестна», что путало.
        if s_wear is None:
            print(f"          🟍 стикер: {s_name}  (новый, 0%)")
        else:
            print(f"          🟍 стикер: {s_name}  (потёртость {s_wear * 100:.0f}%)")
    charms = _cs2_extract_charms(item)
    for c_name, c_pattern in charms:
        if c_pattern is None:
            print(f"          🔗 чарм: {c_name}")
        else:
            print(f"          🔗 чарм: {c_name}  (pattern={c_pattern})")


async def _ask_price_cents(
    prompt: str, *, i_callback=None, a_callback=None, cur_sym: str = "",
) -> int | None:
    """Спросить цену типа `1.99`/`1,99` — вернёт центы (int) или None.

    None возвращается на пустой ввод и на «q» / «b» (back) — это
    стандартные команды отмены/возврата.

    Если задан `i_callback` (async no-arg вызываемый объект) — на вводе `i`
    он вызывается (обычно открывает item-info / график цен), потом снова
    спрашивается цена. Без callback `i` обрабатывается как невалидный ввод.

    Если задан `a_callback` (async no-arg → (cents:int, reason:str, path:str)
    | None) — на вводе `a` он вызывается. Если вернул не-None — печатается
    suggestion, и спрашиваем подтверждение: `y` принимает, число вводит своё,
    `q` отменяет.
    """
    while True:
        raw = (await _ask(prompt)).strip().replace(",", ".")
        if not raw or raw.lower() in ("q", "b"):
            return None
        if raw.lower() == "i" and i_callback is not None:
            try:
                await i_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"   [!] item-info упал: {exc!r}")
            continue
        if raw.lower() == "a" and a_callback is not None:
            try:
                sug = await a_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"   [!] auto-price упал: {exc!r}")
                continue
            if sug is None:
                print("   [!] авто-цена не смогла подобрать — введи цену вручную.")
                continue
            cents_a, reason, path = sug
            sym_part = f" {cur_sym}" if cur_sym else ""
            print(
                f"   ► Авто-цена (Path {path}): "
                f"{cents_a / 100:.2f}{sym_part}  ({reason})"
            )
            confirm = (await _ask(
                "   [y=принять / число=ввести своё / q=отмена]: "
            )).strip().lower().replace(",", ".")
            if confirm in ("y", "yes", ""):
                return cents_a
            if confirm in ("q", "b"):
                return None
            try:
                amount = float(confirm)
            except ValueError:
                print(f"   «{confirm}» не похоже на число — попробуй заново.")
                continue
            cents = int(round(amount * 100))
            if cents <= 0:
                print("   Цена должна быть > 0.")
                continue
            return cents
        try:
            amount = float(raw)
        except ValueError:
            print(f"   «{raw}» не похоже на число.")
            return None
        if amount <= 0:
            print("   Цена должна быть > 0.")
            return None
        cents = int(round(amount * 100))
        if cents <= 0:
            print("   Цена слишком мала.")
            return None
        return cents


async def _list_item_action(
    client,
    currency_enum,
    currency_code: int,
    *,
    listed_asset_ids=None,
    protected_asset_ids=None,
):
    """Возвращает async-хендлер `s <N>` для пагинатора инвентаря.

    listed_asset_ids — set asset_id, которые уже на листинге (для отказа от
    повторного выставления, см. #10).
    protected_asset_ids — set asset_id, пришедших из CS2_PROTECTED контекста
    (для надёжного определения trade-protected без зависимости от текста).
    """
    listed_asset_ids = listed_asset_ids or set()
    protected_asset_ids = protected_asset_ids or set()

    async def _handler(item, idx_1b: int, _all):
        from aiosteampy.utils import buyer_pays_to_receive

        import patterns

        descr = item.description
        name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", "?")

        # Live cross-ref: предмет уже на листинге — отказываем
        if str(item.asset_id) in listed_asset_ids:
            print(
                f"   «{name}» (asset_id={item.asset_id}) уже на листинге — повторно "
                "не выставляю.\n   Если хочешь поменять цену — снимай через `3) Sell-листинги`."
            )
            return

        if _is_trade_protected(item, protected_asset_ids=protected_asset_ids):
            ta = getattr(item, "tradable_after", None)
            when = ta.strftime("%Y-%m-%d %H:%M UTC") if ta else "?"
            print(
                f"   «{name}» под Trade Protection до {when} — выставить нельзя.\n"
                "      Подожди окончания защиты."
            )
            return

        if not getattr(descr, "marketable", True):
            print(f"   «{name}» нельзя продать на маркете (marketable=false).")
            return

        # Чарм/брелок — массово отказывали; для одиночки тоже предупреждаем,
        # но не блокируем (мало ли).
        if patterns.is_charm(name):
            if not await _ask_yes_no(
                f"   ⚠  «{name}» — это брелок. У всех брелоков есть редкие паттерны.\n"
                f"      Точно выставлять?"
            ):
                print("   Отменено.")
                return

        # #10 (паттерны): проверяем редкость по seed (если предмет CS2)
        _wear, seed = _cs2_extract_wear_seed(item)
        if seed is not None:
            res = patterns.is_rare_pattern(name, seed)
            if res.is_rare is True:
                tier = res.tier_note or "?"
                print(f"   ⚠  «{name}» seed={seed} — РЕДКИЙ паттерн ({tier}).")
                if not await _ask_yes_no("      Точно выставлять?"):
                    print("   Отменено.")
                    return
            elif res.is_rare is None:
                print(
                    f"   ⚠  «{name}» в danger-list (7patterns.txt), но точные номера "
                    f"паттернов не вписаны в 7patterns.json. seed={seed}."
                )
                if not await _ask_yes_no("      Точно выставлять?"):
                    print("   Отменено.")
                    return

        print(f"   Выставить «{name}» (asset_id={item.asset_id}) на продажу.")
        info_cb = _make_item_info_callback(client, currency_enum, currency_code, item)
        auto_cb = _make_auto_price_callback(client, currency_enum, currency_code, item)
        cents = await _ask_price_cents(
            "   Цена для покупателя в "
            + ("долларах" if currency_code == 1 else "валюте кошелька")
            + " (1.99, i=инфо/график, a=авто-цена, q/b/Enter — назад): ",
            i_callback=info_cb,
            a_callback=auto_cb,
            cur_sym=_currency_symbol(currency_enum, currency_code) if currency_code else "",
        )
        if cents is None:
            print("   Отменено.")
            return
        # Считаем, сколько ты получишь после комиссии Steam.
        try:
            _s_fee, _p_fee, to_receive = buyer_pays_to_receive(cents)
        except Exception:  # noqa: BLE001
            to_receive = None
        gross_str = _format_price(cents, currency_enum, currency_code)
        receive_str = _format_price(to_receive, currency_enum, currency_code) if to_receive else "?"
        print(f"   Покупатель платит: {gross_str}.   Ты получишь на кошелёк: {receive_str}.")
        if not await _ask_yes_no("   Выставить?"):
            print("   Отменено, листинг не создан.")
            return
        print("   [..] Отправляю запрос (может потребоваться mobile-confirm) ...")
        try:
            result = await _place_sell_listing_with_retry(
                client, item, app_context=None, price=cents,
                what="place_sell_listing",
            )
        except Exception as exc:  # noqa: BLE001
            err = steam_errors.classify_steam_error(exc)
            print("   " + steam_errors.format_for_log(err))
            return
        if result is None:
            print(
                "   [OK] Листинг отправлен. Если требуется ручное подтверждение — "
                "открой Steam Mobile App."
            )
        else:
            print(f"   [OK] Листинг создан и подтверждён (id={result}).")
            # Зафиксируем asset_id в локальном set чтобы повторно не пытались.
            listed_asset_ids.add(str(item.asset_id))
            # F2: пишем новый листинг в listings_cache сразу, чтобы потом можно
            # было его снять через глобальное меню «снять с продажи» без sweep'а.
            try:
                import cache

                cache.insert_placed_listing(
                    client.username,
                    result,
                    unowned_id=str(item.asset_id),
                    market_hash_name=name,
                    price_cents=int(cents),
                    currency_code=currency_code,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"   [!] не смог записать листинг в кеш: {exc!r}")
        # маленькая задержка чтобы Steam не считал спамом (#8 в фидбеке)
        await asyncio.sleep(0.3)

    return _handler


async def _cancel_inventory_item_action(
    client,
    currency_enum,
    currency_code: int,
    *,
    listed_asset_ids: set[str] | None = None,
):
    """Возвращает async-хендлер `c <N>` для пагинатора инвентаря.

    Снимает с продажи предмет, у которого `asset_id` в `listed_asset_ids`
    (т.е. отмечен как «на маркете»). listing_id резолвится из listings_cache.

    Это F1a: «когда в инвентаре переходишь в предмет, отмеченный как на маркете,
    хочется его сразу снять, а не лезть в меню Sell-листингов».
    """
    listed_asset_ids = listed_asset_ids or set()

    async def _handler(item, idx_1b: int, _all):
        import cache as _cache_mod

        descr = item.description
        name = (
            getattr(descr, "market_hash_name", None)
            or getattr(descr, "name", "?")
        )
        asset_id = str(item.asset_id)

        if asset_id not in listed_asset_ids:
            print(
                f"   «{name}» (asset_id={asset_id}) не отмечен как «на маркете» — "
                "снимать нечего. Команда `c` работает только для on_market.\n"
                "   Если ты уверен что предмет на маркете — запусти sweep чтобы "
                "обновить cross-ref."
            )
            return

        # Резолвим listing_id из кеша (по unowned_id или asset_id).
        listing_id = _cache_mod.find_listing_by_asset_id(client.username, asset_id)
        if not listing_id:
            unowned = getattr(item, "unowned_id", None)
            if unowned is not None:
                listing_id = _cache_mod.find_listing_by_asset_id(client.username, unowned)
        if not listing_id:
            print(
                f"   «{name}» (asset_id={asset_id}): не нашли listing_id в кеше.\n"
                "   Скорее всего, выставлен до того, как код стал писать листинги в кеш.\n"
                "   Сделай sweep (он обновит listings_cache) и попробуй снова."
            )
            return

        # Достанем цену листинга чтобы юзер понимал «что снимаем».
        row = _cache_mod.get_listing_by_asset_id(client.username, asset_id)
        price_str = "—"
        if row and row.get("price_cents") is not None:
            try:
                pc = int(row["price_cents"])
                cc = row.get("currency_code") or currency_code or 0
                price_str = _format_price(pc, currency_enum, cc) if cc else f"{pc/100:.2f}"
            except Exception:  # noqa: BLE001
                price_str = "—"

        print(
            f"   Снять с продажи: «{name}» (asset_id={asset_id}, "
            f"listing_id={listing_id}, цена={price_str})."
        )
        if not await _ask_yes_no("   Подтвердить снятие?"):
            print("   Отменено.")
            return

        async def _do_cancel(lid=listing_id):
            cm = client.cancel_sell_listing(lid)
            async with cm as resp:
                return resp.status

        try:
            status = await _with_retry(_do_cancel, what=f"cancel_sell_listing lid={listing_id}")
        except Exception as exc:  # noqa: BLE001
            err = steam_errors.classify_steam_error(exc)
            print("   " + steam_errors.format_for_log(err, prefix=f"Steam отклонил lid={listing_id}"))
            return

        if status is None or status < 400:
            print(f"   [OK] Листинг {listing_id} снят. Предмет вернётся в инвентарь.")
            try:
                _cache_mod.delete_listing(client.username, listing_id)
                _cache_mod.mark_inventory_state_by_asset_id(
                    client.username, asset_id, "free"
                )
                unowned = getattr(item, "unowned_id", None)
                if unowned is not None:
                    _cache_mod.mark_inventory_state_by_asset_id(
                        client.username, unowned, "free"
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"   [!] не смог обновить кеш: {exc!r}")
            # Чтобы повторно не предлагать снять — убираем из local listed set.
            try:
                listed_asset_ids.discard(asset_id)
                unowned = getattr(item, "unowned_id", None)
                if unowned is not None:
                    listed_asset_ids.discard(str(unowned))
            except Exception:  # noqa: BLE001
                pass
        else:
            print(f"   [!] Steam ответил статусом {status} — проверь в браузере.")
        await asyncio.sleep(0.3)

    return _handler


_SECTION_ORDER = ("free", "on_market", "trade_hold", "trade_protect")
_SECTION_LABELS = {
    "free": "Свободные",
    "on_market": "На маркете",
    "trade_hold": "Trade-hold (купленные с тп)",
    "trade_protect": "Trade-protected (получены через трейд)",
}


def _group_inventory(
    items: list,
    listed_asset_ids: set[str] | None = None,
    protected_asset_ids: set[str] | None = None,
) -> list[dict]:
    """Группирует предметы по (состояние, market_hash_name).

    Возвращает плоский список группов в порядке секций
    (свободные → на маркете → trade-hold → trade-protected). Внутри каждой
    секции сортировка по новизне (max asset_id убыванием) — самые свежие
    предметы оказываются вверху своей секции.
    """
    listed_asset_ids = listed_asset_ids or set()
    protected_asset_ids = protected_asset_ids or set()
    by_state: dict[str, dict[str, list]] = {s: {} for s in _SECTION_ORDER}
    for it in items:
        state = _inventory_state(it, listed_asset_ids, protected_asset_ids)
        descr = it.description
        name = getattr(descr, "market_hash_name", None) or getattr(descr, "name", "?")
        by_state[state].setdefault(name, []).append(it)

    out: list[dict] = []
    for state in _SECTION_ORDER:
        bucket = []
        for name, lst in by_state[state].items():
            total_amount = sum(getattr(it, "amount", 1) for it in lst)
            # «Новизна» группы — максимальный asset_id среди её предметов.
            # Steam раздаёт asset_id монотонно, так что чем больше — тем свежее.
            try:
                newest = max(int(getattr(it, "asset_id", 0)) for it in lst)
            except (TypeError, ValueError):
                newest = 0
            worst_hold = None
            for it in lst:
                ta = getattr(it, "tradable_after", None)
                if ta is not None and (worst_hold is None or ta > worst_hold):
                    worst_hold = ta
            bucket.append(
                {
                    "name": name,
                    "items": lst,
                    "total_amount": total_amount,
                    "newest": newest,
                    "worst_hold": worst_hold,
                    "section": state,
                }
            )
        bucket.sort(key=lambda g: (-g["newest"], g["name"]))
        out.extend(bucket)
    return out


def _print_inventory_group(i: int, group: dict) -> None:
    name = group["name"]
    total = group["total_amount"]
    n_unique = len(group["items"])
    extra = ""
    if n_unique > 1 and n_unique != total:
        # Несколько разных asset_id (CS2-скины с разным float/seed) — не классический stack.
        extra = f"  (разных: {n_unique})"
    state = group.get("section", "free")
    state_str = _format_state_marker(state, group.get("worst_hold"))
    print(f"   {i:>3}. {name} ×{total}{extra}{state_str}")


async def _bulk_list_group(
    client,
    group: dict,
    currency_enum,
    currency_code: int,
    *,
    with_cs2_extras: bool,
    listed_asset_ids: set[str] | None = None,
    protected_asset_ids: set[str] | None = None,
) -> None:
    """Массово выставляет предметы из одной группы (одинаковых по market_hash_name).

    Фильтры:
      - не-marketable → пропуск молча.
      - чарм/брелок → пропуск (у всех есть редкие паттерны, см. patterns.is_charm).
      - is_rare_pattern == True → пропуск (точно редкий, есть в 7patterns.json).
      - is_rare_pattern == None (uncertain) → спрашиваем для каждого предмета.
      - float < min_float → пропуск (защита редких/Factory New, если задан min_float).

    На любом промпте можно ввести «q» / «b» / Enter в нижнем регистре, чтобы
    выйти обратно в инвентарь без выключения скрипта.
    """
    from aiosteampy.utils import buyer_pays_to_receive

    import patterns

    items_in_group = group["items"]
    name = group["name"]
    print(f"\n=== МАССОВОЕ ВЫСТАВЛЕНИЕ: {name} ({len(items_in_group)} шт.) ===")
    print("   (на любом промпте: q/b/Enter — назад)")

    # Брелки целиком — отдельный сценарий, пока не делаем.
    if patterns.is_charm(name):
        print(
            "   У всех брелоков есть редкие паттерны (нет файла с перечнем). "
            "Массово не выставляем — выставляй каждый вручную через `e <N>` → `s <N>`."
        )
        return

    # Trade-protected (получены через трейд, 7 дней не продаются).
    before_protected = len(items_in_group)
    items_in_group = [
        it
        for it in items_in_group
        if not _is_trade_protected(it, protected_asset_ids=protected_asset_ids)
    ]
    skipped_protected = before_protected - len(items_in_group)
    if skipped_protected:
        print(f"   {skipped_protected} предметов под Trade Protection — пропускаю.")

    # Не-marketable (включает market-hold после покупки).
    marketable = [it for it in items_in_group if getattr(it.description, "marketable", True)]
    if len(marketable) < len(items_in_group):
        print(
            f"   {len(items_in_group) - len(marketable)} предметов нельзя продать "
            "на маркете — пропускаю."
        )
    # Live cross-ref: вырезаем то, что уже на листинге (по unowned_id из listings)
    if listed_asset_ids:
        before = len(marketable)
        marketable = [it for it in marketable if str(it.asset_id) not in listed_asset_ids]
        skipped_listed = before - len(marketable)
        if skipped_listed:
            print(f"   {skipped_listed} предметов уже на листинге — пропускаю.")
    if not marketable:
        print("   Нечего выставлять.")
        return

    # 1) Сколько штук выставить (как в _bulk_sell_cross_account).
    info_cb = _make_item_info_callback(
        client, currency_enum, currency_code, marketable[0],
    )
    auto_cb = _make_auto_price_callback(
        client, currency_enum, currency_code, marketable[0],
    )
    total_marketable = len(marketable)
    while True:
        raw_n = (await _ask(
            f"   Сколько штук выставить? (1..{total_marketable}, all=все, "
            f"i=инфо по предмету, q/b=отмена): "
        )).strip().lower()
        if raw_n in ("q", "b", ""):
            print("   Отменено, возвращаемся в инвентарь.")
            return
        if raw_n == "i":
            try:
                await info_cb()
            except Exception as exc:  # noqa: BLE001
                print(f"   [!] item-info упал: {exc!r}")
            continue
        break
    if raw_n == "all":
        n_target = total_marketable
    else:
        try:
            n_target = int(raw_n)
        except ValueError:
            print(f"   «{raw_n}» — не число, отмена.")
            return
    if n_target <= 0 or n_target > total_marketable:
        print(f"   Допустимо 1..{total_marketable}, отмена.")
        return

    # 2) Фильтр по float — НИЖНЯЯ граница (только CS2).
    # У редких/дорогих скинов float маленький (Factory New). Чтобы случайно
    # не выставить ценный экземпляр по средней цене, вводим min_float —
    # всё что НИЖЕ этого порога будет пропущено.
    min_float: float | None = None
    if with_cs2_extras:
        raw = (
            (
                await _ask(
                    "   Мин. float (пропустить всё ниже; "
                    "Enter — без ограничения, q/b — назад, например 0.20): "
                )
            )
            .strip()
            .replace(",", ".")
        )
        if raw.lower() in ("q", "b"):
            print("   Отменено, возвращаемся в инвентарь.")
            return
        if raw:
            try:
                min_float = float(raw)
                if not (0.0 <= min_float <= 1.0):
                    print("   Float вне диапазона 0..1 — игнорирую.")
                    min_float = None
            except ValueError:
                print(f"   «{raw}» не похоже на float — игнорирую.")

    # 3) Цена. С i=info колбэком и a=auto-price.
    cents = await _ask_price_cents(
        "   Цена для покупателя в "
        + ("долларах" if currency_code == 1 else "валюте кошелька")
        + " (например 1.99, i=инфо/график, a=авто-цена, q/b/Enter — назад): ",
        i_callback=info_cb,
        a_callback=auto_cb,
        cur_sym=_currency_symbol(currency_enum, currency_code) if currency_code else "",
    )
    if cents is None:
        print("   Отменено, возвращаемся в инвентарь.")
        return

    try:
        _s_fee, _p_fee, to_receive = buyer_pays_to_receive(cents)
    except Exception:  # noqa: BLE001
        to_receive = None
    gross_str = _format_price(cents, currency_enum, currency_code)
    receive_str = _format_price(to_receive, currency_enum, currency_code) if to_receive else "?"
    print(f"   Покупатель платит: {gross_str}.   Ты получишь на кошелёк: {receive_str}.")

    # Прогоняем каждый предмет через фильтры. Останавливаемся как только
    # набрали n_target кандидатов (пользователь хочет выставить N штук, не все).
    to_list: list = []
    skipped_by_float = 0
    skipped_by_pattern = 0
    skipped_uncertain_by_user = 0

    for it in marketable:
        if len(to_list) >= n_target:
            break
        wear, seed = (None, None)
        if with_cs2_extras:
            wear, seed = _cs2_extract_wear_seed(it)

        # Float-фильтр (нижняя граница — отсекаем редкие низкофлотные).
        if min_float is not None and wear is not None and wear < min_float:
            skipped_by_float += 1
            continue

        # Pattern-фильтр (только для CS2-скинов с seed).
        if with_cs2_extras:
            res = patterns.is_rare_pattern(name, seed)
            if res.is_rare is True:
                tier = res.tier_note or "?"
                print(f"   [skip] asset_id={it.asset_id}: редкий паттерн ({tier}, seed={seed})")
                skipped_by_pattern += 1
                continue
            if res.is_rare is None:
                # неопределённость: скин в .txt-блэклисте, но точных номеров нет
                hint = (
                    f"   ⚠  «{name}» в danger-list, но точные номера паттернов "
                    f"не вписаны в 7patterns.json.\n"
                    f"      asset_id={it.asset_id}, seed={seed if seed is not None else '?'}, "
                    f"float={f'{wear:.6f}' if wear is not None else '?'}.\n"
                    f"      Выставлять?"
                )
                if not await _ask_yes_no(hint):
                    skipped_uncertain_by_user += 1
                    continue

        to_list.append(it)

    print(f"\n   Итого к выставлению: {len(to_list)} из {n_target} запрошенных "
          f"({total_marketable} marketable доступно).")
    if skipped_by_float:
        print(f"   Пропущено по float < {min_float}: {skipped_by_float}.")
    if skipped_by_pattern:
        print(f"   Пропущено по редкому паттерну: {skipped_by_pattern}.")
    if skipped_uncertain_by_user:
        print(f"   Пропущено вручную (uncertain): {skipped_uncertain_by_user}.")

    if not to_list:
        print("   Нечего выставлять.")
        return

    if not await _ask_yes_no(f"   Выставить {len(to_list)} предметов по {gross_str}?"):
        print("   Отменено, возвращаемся в инвентарь.")
        return

    # Перед bulk'ом — чистим awaiting confirmation, чтобы не было дубликатов и
    # путаницы. Это особенно важно если ранее bulk обрывался на середине.
    await _cancel_all_pending_confirmations(
        client, label=f"({client.username}, pre-bulk-list)"
    )

    ok = 0
    fail = 0
    for i, item in enumerate(to_list, 1):
        try:
            result = await _place_sell_listing_with_retry(
                client, item, app_context=None, price=cents,
                what=f"place_sell_listing #{i}",
            )
            ok += 1
            # F2: пишем новый листинг в listings_cache сразу (если Steam вернул id —
            # т.е. confirm прошёл). Если confirm требовался вручную через mobile —
            # result=None, в кеш не пишем (нет listing_id), sweep потом подберёт.
            if result is not None:
                try:
                    import cache

                    cache.insert_placed_listing(
                        client.username,
                        result,
                        unowned_id=str(item.asset_id),
                        market_hash_name=(
                            getattr(item.description, "market_hash_name", None) or None
                        ),
                        price_cents=int(cents),
                        currency_code=currency_code,
                    )
                except Exception as exc:  # noqa: BLE001
                    # не критично — sweep всё равно подберёт
                    print(f"   [!] кеш записи листинга упал: {exc!r}")
            # Сохраним asset_id в локальный set, чтобы при повторном проходе
            # инвентаря на этой же сессии этот предмет не просился к выставлению.
            if listed_asset_ids is not None:
                listed_asset_ids.add(str(item.asset_id))
        except Exception as exc:  # noqa: BLE001
            fail += 1
            err = steam_errors.classify_steam_error(exc)
            item_name = (
                getattr(item.description, "market_hash_name", None) or "?"
            )
            prefix = f"#{i} {item_name} (asset_id={item.asset_id})"
            print("   " + steam_errors.format_for_log(err, prefix=prefix))
            # MAX_WALLET, SESSION_EXPIRED и т.п. — остальные листинги всё равно упадут
            # с той же ошибкой. Нет смысла тратить на них время.
            if err.fatal_for_batch:
                remaining = len(to_list) - i
                if remaining > 0:
                    print(
                        f"   [stop] прерываю bulk-list: пропущено {remaining} следующих."
                    )
                break
        if i % 5 == 0 or i == len(to_list):
            print(f"   ... {i}/{len(to_list)} обработано (ok={ok}, fail={fail})")
        # 0.3-0.4с между place_sell_listing — Steam охотно режет 429 (#8).
        await asyncio.sleep(0.4)

    print(f"\n[OK] Выставлено: {ok}.  Ошибок: {fail}.")


async def _show_inventory_generic(
    client,
    app_context,
    label: str,
    currency_enum,
    currency_code: int,
    *,
    with_cs2_extras: bool = False,
    sellable: bool = True,
) -> None:
    """Универсальный вывод инвентаря: сначала сгруппированно по нейму, можно развернуть."""
    from aiosteampy import AppContext

    print(f"\n=== ИНВЕНТАРЬ: {label} ===")
    print("[..] Загружаю инвентарь ...")
    try:
        items, total, _last_assetid = await _with_retry(
            lambda: client.get_inventory(app_context, count=2000),
            what="get_inventory",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Не удалось получить инвентарь: {exc!r}")
        return

    items = list(items) if items else []

    # Для CS2 стим хранит trade-protected скины в отдельном context=16
    # (`AppContext.CS2_PROTECTED`). В обычном CS2 (context=2) их нет.
    # Догружаем и мерджим — теперь видны 7-дневные защищённые после трейда.
    is_cs2 = app_context is AppContext.CS2
    protected_asset_ids: set[str] = set()
    if is_cs2:
        try:
            protected_items, p_total, _ = await _with_retry(
                lambda: client.get_inventory(AppContext.CS2_PROTECTED, count=2000),
                what="get_inventory (CS2_PROTECTED)",
            )
            extra = list(protected_items) if protected_items else []
            if extra:
                print(f"[..] Догружено trade-protected: {len(extra)} (контекст=16).")
                # Запоминаем asset_id'ы пришедших из protected-контекста — они
                # ВСЕ trade-protected по определению, независимо от текста в
                # owner_descriptions (на случай если Steam перестанет его слать).
                protected_asset_ids = {str(it.asset_id) for it in extra}
                items.extend(extra)
                total = (total or 0) + (p_total or len(extra))
        except Exception as exc:  # noqa: BLE001
            # Не критично — старые версии Steam-аккаунтов могут не иметь protected
            # context'а вообще.
            print(f"   [!] CS2_PROTECTED недоступен ({type(exc).__name__}): пропускаю.")

    if not items:
        print("(инвентарь пуст или закрыт настройками приватности)")
        return

    print(f"Получено {len(items)} (всего предметов в инвентаре: {total}).")

    # === Cross-ref «уже на маркете» через unowned_id из активных листингов ===
    # Это надёжнее SQLite-кеша: тянем live, без необходимости юзеру сначала
    # сходить в `3) Sell-листинги`. Берём unowned_id с MarketListingItem —
    # это asset_id, под которым предмет вернётся в инвентарь после cancel'а
    # (== тому asset_id, под которым он лежит в нашем inventory сейчас).
    listed_asset_ids: set[str] = set()
    try:
        active, _to_confirm, _bo, _total = await _with_retry(
            lambda: client.get_my_listings(start=0, count=100),
            what="get_my_listings (cross-ref)",
        )
        for lst in active:
            unowned = getattr(lst.item, "unowned_id", None)
            if unowned is not None:
                listed_asset_ids.add(str(unowned))
            # asset_id тоже добавим — для тех листингов, где unowned_id отсутствует.
            listed_asset_ids.add(str(lst.item.asset_id))
        # Если много страниц — догружаем.
        loaded = len(active)
        while loaded < (_total or 0):
            more_active, _, _, _ = await _with_retry(
                lambda s=loaded: client.get_my_listings(start=s, count=100),
                what="get_my_listings (cross-ref next)",
            )
            if not more_active:
                break
            for lst in more_active:
                unowned = getattr(lst.item, "unowned_id", None)
                if unowned is not None:
                    listed_asset_ids.add(str(unowned))
                listed_asset_ids.add(str(lst.item.asset_id))
            loaded += len(more_active)
            await asyncio.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог cross-ref'нуть с листингами ({type(exc).__name__}): {exc!r}")

    # Запишем в кеш — целиком, потому что get_inventory вернул нам всё.
    try:
        import cache

        def _seed(it):
            _, s = _cs2_extract_wear_seed(it)
            return s

        def _wear(it):
            w, _ = _cs2_extract_wear_seed(it)
            return w

        # P2-bugfix: тот же state_extractor, что и в sweep'е — иначе мы
        # перетрём `state` всех предметов этого акка на NULL, и
        # `_show_cs2_subgroups` покажет «state=NULL у N строк» (после захода
        # в инвентарь одного акка вся глобальная стата уезжала в NULL).
        def _state(it, _laids=listed_asset_ids, _pids=protected_asset_ids):
            return _inventory_state(it, _laids, _pids)

        ctx_name = getattr(app_context, "name", str(app_context))
        cache.record_inventory(
            client.username,
            ctx_name,
            items,
            paint_seed_extractor=_seed if with_cs2_extras else None,
            paint_wear_extractor=_wear if with_cs2_extras else None,
            state_extractor=_state,
            partial=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Не смог записать в кеш: {exc!r}")

    protected = sum(
        1 for it in items if _is_trade_protected(it, protected_asset_ids=protected_asset_ids)
    )
    if listed_asset_ids:
        marked = sum(1 for it in items if str(it.asset_id) in listed_asset_ids)
        msg = f"   [cross-ref] {marked} предметов сейчас на листинге"
        if protected:
            msg += f", {protected} под trade-protection"
        print(msg + ".")
    else:
        if protected:
            print(f"   [cross-ref] активных листингов нет; trade-protected: {protected}.")
        else:
            print("   [cross-ref] активных листингов нет.")

    sell_handler = None
    if sellable:
        sell_handler = await _list_item_action(
            client,
            currency_enum,
            currency_code,
            listed_asset_ids=listed_asset_ids,
            protected_asset_ids=protected_asset_ids,
        )

    # F1a: `c <N>` — снять с продажи предмет, отмеченный как on_market.
    # Доступен только если в инвентаре вообще есть предметы на листингах.
    cancel_handler = None
    if listed_asset_ids:
        cancel_handler = await _cancel_inventory_item_action(
            client,
            currency_enum,
            currency_code,
            listed_asset_ids=listed_asset_ids,
        )

    async def _show_flat(items_subset, header: str | None = None) -> None:
        if header:
            print(f"\n--- {header} ---")

        def _render(slice_, start_idx):
            for i, item in enumerate(slice_, start=start_idx):
                _print_inventory_item(
                    i,
                    item,
                    with_cs2_extras=with_cs2_extras,
                    listed_asset_ids=listed_asset_ids,
                    protected_asset_ids=protected_asset_ids,
                )

        sym2 = _currency_symbol(currency_enum, currency_code) if currency_code else ""
        info_action2 = _item_info_action_factory(client, currency_enum, currency_code, sym2)
        extra = {"i": info_action2}
        # Видимые хинты ставим только если в этом срезе есть соотв. предметы:
        # plain free → только `s`, on_market → только `c`, mixed → оба.
        any_free = any(
            _inventory_state(it, listed_asset_ids, protected_asset_ids) == "free"
            for it in items_subset
        )
        any_on_market = any(
            _inventory_state(it, listed_asset_ids, protected_asset_ids) == "on_market"
            for it in items_subset
        )
        if sellable and any_free:
            extra["s"] = sell_handler
            print("   Команда `s <номер>` — выставить предмет на продажу.")
        if cancel_handler is not None and any_on_market:
            extra["c"] = cancel_handler
            print("   Команда `c <номер>` — снять предмет с продажи (on_market).")
        print("   Команда `i <номер>` — листинги + график цены по предмету.")
        await _paginate(items_subset, INVENTORY_PAGE_SIZE, _render, extra_commands=extra)

    groups = _group_inventory(items, listed_asset_ids, protected_asset_ids)
    # Если все предметы уникальны (ни одного дубля по нейму) — сразу плоский режим.
    if len(groups) == len(items):
        await _show_flat(items)
        return

    # Сводка по секциям — что вообще есть в инвентаре.
    counts = {s: 0 for s in _SECTION_ORDER}
    for g in groups:
        counts[g["section"]] += g["total_amount"]
    summary_parts = [f"{_SECTION_LABELS[s]}: {counts[s]}" for s in _SECTION_ORDER if counts[s]]
    print(f"\nСгруппировано: {len(groups)} групп из {len(items)} предметов.")
    if summary_parts:
        print("   " + " | ".join(summary_parts))

    async def _expand_action(group, idx_1b: int, _all_groups):
        await _show_flat(group["items"], header=f"{group['name']} ({len(group['items'])} шт.)")

    async def _bulk_list_action(group, idx_1b: int, _all_groups):
        if group["section"] != "free":
            print(
                f"   Группа в секции «{_SECTION_LABELS[group['section']]}» — массовое "
                "выставление недоступно (предметы заблокированы или уже на маркете)."
            )
            return
        await _bulk_list_group(
            client,
            group,
            currency_enum,
            currency_code,
            with_cs2_extras=with_cs2_extras,
            listed_asset_ids=listed_asset_ids,
            protected_asset_ids=protected_asset_ids,
        )

    def _render_groups(slice_, start_idx):
        last_section: str | None = None
        for i, group in enumerate(slice_, start=start_idx):
            section = group["section"]
            if section != last_section:
                if last_section is not None:
                    print()
                print(f"   --- {_SECTION_LABELS[section]} ---")
                last_section = section
            _print_inventory_group(i, group)

    extra = {"e": _expand_action}
    print("   `e <номер>` — развернуть группу в плоский список (и там можно `s <N>`).")
    if sellable:
        # `s <N>` — выставление группы (раньше было `b`, переименовано задачей 10).
        # Сценарий теперь как у cross-account bulk-list: спрашиваем сколько штук,
        # min_float, цену (с `i=инфо` callback) — а не «всю группу одной кнопкой».
        extra["s"] = _bulk_list_action
        print(
            "   `s <номер>` — выставить часть группы по одной цене "
            "(спрошу сколько штук, min float, цену; i=инфо/график)."
        )
    # Сгруппированный вид показываем без пагинации — групп обычно мало,
    # листать неудобно (#6 в фидбеке).
    await _paginate(
        groups,
        max(len(groups), 1),
        _render_groups,
        extra_commands=extra,
    )


async def menu_inventory(client, currency_enum, currency_code: int) -> None:
    """5) Под-меню выбора игры для инвентаря."""
    from aiosteampy import AppContext

    while True:
        print("\n=== ИНВЕНТАРИ — выбери игру ===")
        print("   1) Steam Community  (карточки, значки, эмодзи)")
        print("   2) CS2              (с float и seed)")
        print("   3) Dota 2")
        print("   4) TF 2")
        print("   0) Назад в главное меню")
        choice = await _ask("\nВыбор: ")

        if choice == "1":
            await _show_inventory_generic(
                client,
                AppContext.STEAM_COMMUNITY,
                "Steam Community (карточки и т.п.)",
                currency_enum,
                currency_code,
            )
        elif choice == "2":
            await _show_inventory_generic(
                client,
                AppContext.CS2,
                "CS2",
                currency_enum,
                currency_code,
                with_cs2_extras=True,
            )
        elif choice == "3":
            await _show_inventory_generic(
                client,
                AppContext.DOTA2,
                "Dota 2",
                currency_enum,
                currency_code,
            )
        elif choice == "4":
            await _show_inventory_generic(
                client,
                AppContext.TF2,
                "TF 2",
                currency_enum,
                currency_code,
            )
        elif choice == "0":
            return
        else:
            print("Не понял — введи цифру 0..4")
            continue


# =============================================================================
# Логин и/или восстановление сессии
# =============================================================================
async def _try_resume(client, cookies_file: Path) -> bool:
    """Пробуем восстановить сессию из кешированных cookies. True — получилось."""
    if not cookies_file.is_file():
        return False
    print(f"[..] Пробуем переиспользовать прошлую сессию ({cookies_file.name}) ...")
    try:
        client.session.cookie_jar.load(cookies_file)
    except Exception as exc:  # noqa: BLE001
        print(f"[..] Кеш сессии битый ({type(exc).__name__}: {exc}), логинимся с нуля.")
        try:
            cookies_file.unlink()
        except OSError:
            pass
        return False
    # Проверка — лёгкий запрос за wallet_info; если вернул HTML без g_rgWalletInfo,
    # это значит cookies протухли и Steam редиректит на логин.
    try:
        await client.get_wallet_info()
    except Exception as exc:  # noqa: BLE001
        print(f"[..] Cookies протухли ({type(exc).__name__}: {exc}), логинимся заново.")
        client.session.cookie_jar.clear()
        return False
    return True


async def _full_login(client, username: str, steam_id: int, mafile_path: Path) -> bool:
    """Делает полноценный логин. True — успех, False — ошибка (уже выведена)."""
    from aiosteampy import LoginError

    print(f"[..] Логинимся как {username} (steam_id={steam_id}, mafile={mafile_path}) ...")
    try:
        await client.login()
    except LoginError as exc:
        print(f"\n[ERROR] Не удалось войти в Steam: {exc!r}", file=sys.stderr)
        if "step': 'nonce'" in str(exc) or "error': 8" in str(exc):
            print(
                "      Подсказка: Steam отбил последний шаг логина (InvalidParam). "
                "Часто — nonce «протух» или ты слишком часто логинишься. "
                "Подожди 30–60 секунд и попробуй снова.",
                file=sys.stderr,
            )
        elif "invalidpassword" in str(exc).lower():
            print("      Подсказка: неверный пароль или account_name.", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERROR] Неожиданная ошибка при логине: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        return False
    print("[OK] Залогинен.")
    return True


# =============================================================================
# Multi-account: реестр аккаунтов в `accounts/<name>/` (с account.json + maFile)
# =============================================================================
def _discover_accounts() -> list[dict]:
    """Сканирует папку `accounts/`. Каждая подпапка с `account.json` + maFile = аккаунт.

    Формат `accounts/<dir>/account.json`:
        {
          "label": "main",            # отображаемое имя в меню (опционально)
          "username": "verstor",       # Steam-логин
          "password": "...",           # пароль (можно вынести в .env как
                                       #   STEAM_PASSWORD_<USERNAME> — тогда здесь оставь "")
          "steam_id": 76561199...      # опционально, иначе берётся из maFile
        }

    maFile должен лежать рядом — любой файл с расширением `.maFile` в этой же папке.
    """
    accounts_dir = Path(__file__).parent / "accounts"
    if not accounts_dir.is_dir():
        return []
    out: list[dict] = []
    for sub in sorted(accounts_dir.iterdir()):
        if not sub.is_dir():
            continue
        cfg = sub / "account.json"
        if not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[!] Пропускаю {sub.name}: account.json не валидный JSON.")
            continue

        # Принимаем строки/числа/None в любом поле — кому-то удобнее «label»: 1.
        def _as_str(v) -> str:
            if v is None:
                return ""
            return str(v).strip()

        # Ищем maFile в этой же папке (любой *.maFile / *.mafile).
        mafile_path = None
        for f in sorted(sub.iterdir()):
            if f.suffix.lower() == ".mafile":
                mafile_path = f
                break
        if mafile_path is None:
            print(f"[!] Пропускаю {sub.name}: не нашёл *.maFile в папке.")
            continue
        username = _as_str(data.get("username"))
        if not username:
            # fallback — из самого maFile
            try:
                ma_data = json.loads(mafile_path.read_text(encoding="utf-8"))
                username = _as_str(ma_data.get("account_name"))
            except Exception:  # noqa: BLE001
                pass
        if not username:
            print(f"[!] Пропускаю {sub.name}: не определить username.")
            continue
        password = _as_str(data.get("password"))
        if not password:
            password = os.getenv(f"STEAM_PASSWORD_{username.upper()}", "").strip()
        steam_id_val = data.get("steam_id")
        try:
            steam_id_val = int(steam_id_val) if steam_id_val else 0
        except (TypeError, ValueError):
            steam_id_val = 0
        out.append(
            {
                "label": _as_str(data.get("label")) or sub.name,
                "username": username,
                "password": password,
                "mafile_path": mafile_path,
                "steam_id": steam_id_val,
                "dir": sub,
            }
        )
    return out


def _legacy_account() -> dict | None:
    """Старый режим: один аккаунт через STEAM_PASSWORD/MAFILE_PATH в шапке/`.env`."""
    password = (os.getenv("STEAM_PASSWORD") or STEAM_PASSWORD).strip()
    mafile_path_raw = (os.getenv("STEAM_MAFILE_PATH") or MAFILE_PATH).strip()
    if not password or password == "your_password" or not mafile_path_raw:
        return None
    mafile_path = Path(mafile_path_raw).expanduser()
    if not mafile_path.is_absolute():
        mafile_path = (Path(__file__).parent / mafile_path).resolve()
    if not mafile_path.is_file():
        return None
    username_override = (os.getenv("STEAM_USERNAME") or STEAM_USERNAME).strip()
    steam_id_env = os.getenv("STEAM_ID", "").strip()
    steam_id_override = int(steam_id_env) if steam_id_env.isdigit() else STEAM_ID
    return {
        "label": "default",
        "username": username_override,  # может быть пустым → возьмём из maFile при логине
        "password": password,
        "mafile_path": mafile_path,
        "steam_id": steam_id_override,
        "dir": Path(__file__).parent,
    }


_patch_aiosteampy_once_done = False


def _ensure_aiosteampy_patches() -> bool:
    """Накатывает monkey-patch на ItemDescription._set_d_id (один раз за процесс)."""
    global _patch_aiosteampy_once_done
    if _patch_aiosteampy_once_done:
        return True
    try:
        from aiosteampy.constants import App
        from aiosteampy.models import ItemDescription
    except ImportError:
        print(
            "[ERROR] Не установлена aiosteampy. Выполни в терминале:\n"
            '        pip install "aiosteampy>=0.7" "protobuf>=5.26" python-dotenv',
            file=sys.stderr,
        )
        return False

    # Патч бага в aiosteampy 0.7.21 (см. предыдущий коммит).
    def _set_d_id_safe(self) -> None:
        if self.app is not App.CS2:
            return
        action = next(filter(lambda a: "Inspect" in a.name, self.actions), None)
        if action is None:
            return
        link = action.link or ""
        parts = link.split("%D")
        if len(parts) < 2:
            return
        try:
            object.__setattr__(self, "d_id", int(parts[1]))
        except (TypeError, ValueError):
            pass

    ItemDescription._set_d_id = _set_d_id_safe

    # Патч бага aiosteampy: `_parse_buy_orders` падает с KeyError('description'),
    # если в ответе get_my_listings присутствует buy-ордер от не-CS2 игры
    # (например, Dota 2) — у такого ордера в JSON нет вложенного 'description',
    # а исходный код жёстко обращается к `o_data["description"]["instanceid"]`.
    # Оборачиваем по элементам: ломаные пропускаем, остальное возвращаем.
    try:
        from aiosteampy.mixins.market import MarketMixin
        from aiosteampy.models import BuyOrder
        from aiosteampy.utils import create_ident_code

        def _parse_buy_orders_safe(cls, orders, item_descrs_map):  # noqa: ANN001
            out = []
            skipped = 0
            for o_data in orders:
                try:
                    descr = o_data["description"]
                    ident = create_ident_code(
                        descr["instanceid"], descr["classid"], descr["appid"],
                    )
                    out.append(
                        BuyOrder(
                            id=int(o_data["buy_orderid"]),
                            price=int(o_data["price"]),
                            item_description=item_descrs_map[ident],
                            quantity=int(o_data["quantity"]),
                            quantity_remaining=int(o_data["quantity_remaining"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    # Чаще всего: ордер от другой игры (Dota 2) без вложенного
                    # description'а — нам он всё равно не нужен в меню CS2.
                    skipped += 1
                    continue
            if skipped:
                print(f"   [patch] _parse_buy_orders: пропущено {skipped} ордеров без description.")
            return out

        MarketMixin._parse_buy_orders = classmethod(_parse_buy_orders_safe)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] не смог запатчить _parse_buy_orders: {exc!r}")

    _patch_aiosteampy_once_done = True
    return True


async def _connect_account(account: dict, force_relogin: bool, *, proxy: str | None = None):
    """Логин одного аккаунта. Возвращает (client, currency_code) или None при ошибке.

    `proxy`: опционально HTTP/SOCKS URL прокси для ВСЕХ запросов этого SteamClient'а.
    Используется только в sweep'е (см. _run_sweep) — для меню и для place_sell /
    cancel_sell сессии создаются БЕЗ proxy (main IP), как просил Андрей.
    """
    if not _ensure_aiosteampy_patches():
        return None
    from aiosteampy import SteamClient
    from aiosteampy.constants import Currency

    mafile_path: Path = account["mafile_path"]
    try:
        mafile_data = json.loads(mafile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] maFile не валидный JSON: {exc}", file=sys.stderr)
        return None
    username = (account.get("username") or "").strip() or (
        mafile_data.get("account_name") or ""
    ).strip()
    if not username:
        print("[ERROR] Не удалось определить username для аккаунта.", file=sys.stderr)
        return None
    password = (account.get("password") or "").strip()
    if not password:
        # последняя попытка — переменная окружения STEAM_PASSWORD_<USERNAME>
        password = os.getenv(f"STEAM_PASSWORD_{username.upper()}", "").strip()
    if not password:
        print(
            f"[ERROR] Пароль для {username} не указан "
            "(ни в account.json, ни в STEAM_PASSWORD_<USERNAME>).",
            file=sys.stderr,
        )
        return None
    steam_id = int(account.get("steam_id") or 0) or _extract_steam_id(mafile_data) or 0
    if steam_id <= 0:
        print(f"[ERROR] Не удалось определить SteamID для {username}.", file=sys.stderr)
        return None
    shared_secret = (mafile_data.get("shared_secret") or "").strip()
    if not shared_secret:
        print(f"[ERROR] В maFile {mafile_path.name} нет 'shared_secret'.", file=sys.stderr)
        return None
    identity_secret = (mafile_data.get("identity_secret") or "").strip() or None

    session_dir = Path(__file__).parent / SESSION_DIR_NAME
    cookies_file = session_dir / f"{username}.cookies"
    if force_relogin and cookies_file.is_file():
        print("[..] FORCE_RELOGIN — игнорирую кеш сессии.")
        try:
            cookies_file.unlink()
        except OSError:
            pass

    client_kwargs = dict(
        steam_id=steam_id,
        username=username,
        password=password,
        shared_secret=shared_secret,
        identity_secret=identity_secret,
    )
    if proxy:
        # aiosteampy SteamClient прокидывает `proxy` во все aiohttp-запросы.
        client_kwargs["proxy"] = proxy
        print(f"[..] {username}: использую прокси {_mask_proxy_for_log(proxy)}")
    client = SteamClient(**client_kwargs)

    used_cache = False
    if not force_relogin and await _try_resume(client, cookies_file):
        used_cache = True
        print("[OK] Сессия из кеша жива — полноценный логин не понадобился.")
    else:
        ok = await _full_login(client, username, steam_id, mafile_path)
        if not ok:
            try:
                await client.session.close()
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            cookies_file.parent.mkdir(parents=True, exist_ok=True)
            client.session.cookie_jar.save(cookies_file)
            print(f"[OK] Сессия сохранена в {cookies_file.name}.")
        except Exception as exc:  # noqa: BLE001
            print(f"[!]  Не смог сохранить сессию ({exc!r}); ничего страшного.")

    if lifetime := _format_token_lifetime(client):
        print(f"[..] {lifetime}")
    if used_cache:
        try:
            client.session.cookie_jar.save(cookies_file)
        except Exception:  # noqa: BLE001
            pass

    # Гарантируем валидный Steam_Language cookie. aiosteampy читает его
    # через `client.language` (которое = `Language(cookie_value)`), и если
    # cookie отсутствует / битый / пустой — крашится `ValueError('None is
    # not a valid Language')` при первом get_inventory() и любом другом
    # запросе, парсящем язык. Это особенно бьёт по аккаунтам, у которых
    # сессия из кеша протухла или язык в Steam'е был сменён.
    try:
        from aiosteampy.constants import Language as _Language

        try:
            _cur_lang = client.language  # триггерит парсинг из cookie
        except (ValueError, TypeError, KeyError):
            _cur_lang = None
        if not isinstance(_cur_lang, _Language):
            client.language = _Language.ENGLISH
            print("[..] Steam_Language cookie был битый/пустой — выставил ENGLISH.")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Не смог проверить/выставить язык клиента: {exc!r} (продолжаем).")

    # Тянем валюту один раз.
    currency_code = 0
    try:
        wallet = await client.get_wallet_info()
        currency_code = int(wallet.get("wallet_currency", 0))
        if currency_code and not getattr(client, "currency", None):
            client.currency = Currency(currency_code)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Не смог сразу определить валюту кошелька: {exc!r} (продолжаем).")

    # Регистрируем в SQLite-кеше, чтобы аккаунт виден был в меню «Сводка».
    # steam_id_64 нужен чтобы фетчить публичный инвентарь (см. _show_recently_unlocked).
    try:
        import cache

        sid = getattr(client, "steam_id", None)
        cache.record_account(
            username, account.get("label"), steam_id_64=sid,
        )
    except Exception:  # noqa: BLE001
        pass

    return client, currency_code, cookies_file


def _label_num(account: dict) -> int | None:
    """Если `label` парсится как int — возвращает int, иначе None."""
    try:
        return int(str(account.get("label") or "").strip())
    except (TypeError, ValueError):
        return None


def _sort_accounts(accounts: list[dict]) -> list[dict]:
    """Сортирует аккаунты: с числовым label по возрастанию, остальные по username."""
    return sorted(
        accounts,
        key=lambda a: (
            0 if _label_num(a) is not None else 1,
            _label_num(a) if _label_num(a) is not None else 0,
            a.get("username") or "",
        ),
    )


# Steam позволяет держать активные buy-ордера на сумму до ~10× баланса аккаунта.
# Это эмпирическое правило (Steam точное число не публикует) — используется для
# подсветки headroom'а в общей сводке.
BUY_ORDER_LIMIT_MULTIPLIER = 10


def _fmt_money_cents(cents: int | None, sym: str = "") -> str:
    """Аккуратное «1 234,56 kr» для int-центов. Пустота / None → «—»."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    amount = abs(cents) / 100
    s = f"{amount:.2f}"
    int_part, _, frac_part = s.partition(".")
    if len(int_part) > 3:
        rev = int_part[::-1]
        grouped = " ".join(rev[i : i + 3] for i in range(0, len(rev), 3))
        int_part = grouped[::-1]
    out = f"{sign}{int_part},{frac_part}"
    return (out + " " + sym).strip()


def _orders_column(row: dict, currency_enum) -> str:
    """Строка для колонки «Buy-ордера»: «1 234,56 / 12 345,60 kr (10%)».

    Если ордеров нет — «—». Если нет баланса — показываем только сумму ордеров.
    """
    agg = row.get("buy_orders_agg")
    if not agg or not agg.get("orders_count"):
        return "—"
    used = agg["total_cents"]
    cur_code = agg.get("currency_code")
    if cur_code is None:
        bal = row.get("balance") or {}
        cur_code = bal.get("currency_code")
    try:
        sym = _currency_symbol(currency_enum, cur_code) if cur_code else ""
    except Exception:  # noqa: BLE001
        sym = ""

    bal = row.get("balance") or {}
    balance_cents = bal.get("balance_cents")
    if balance_cents is None or balance_cents <= 0:
        # Без баланса нет смысла считать лимит.
        return f"{_fmt_money_cents(used, sym)} (баланс?)"
    cap = balance_cents * BUY_ORDER_LIMIT_MULTIPLIER
    pct = used * 100 / cap if cap else 0
    return f"{_fmt_money_cents(used)} / {_fmt_money_cents(cap, sym)} ({pct:.0f}%)"


async def _show_cache_summary() -> None:
    """Печатает сводку по всем аккаунтам из SQLite-кеша (без обращения к Steam)."""
    from aiosteampy.constants import Currency

    try:
        import cache

        rows = cache.iter_account_summaries()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Не смог открыть кеш: {exc!r}")
        return
    if not rows:
        print("\n(в кеше пока нет данных — зайди хотя бы в один аккаунт и пройдись по меню)")
        return
    # Ширины подобраны так, чтобы 1 строка влезала в стандартное 120-символьное окно.
    line = "=" * 120
    print("\n" + line)
    print(
        f"   {'#':<4} {'Username':<20} {'Баланс':<14} "
        f"{'Listings':<9} {'Buy-ордера (исп. / лимит 10×bal)':<34} "
        f"{'История':<8} {'Инв.':<5}  last seen"
    )
    print(line)
    for r in rows:
        bal = r["balance"]
        if bal and bal["balance_cents"] is not None:
            cur_code = bal.get("currency_code") or 0
            try:
                sym = _currency_symbol(Currency, cur_code) if cur_code else ""
            except Exception:  # noqa: BLE001
                sym = ""
            bal_str = _fmt_money_cents(bal["balance_cents"], sym)
        else:
            bal_str = "—"
        last_seen = (r["last_seen_at"] or "—").split("T")[0]
        num_col = str(r["label_num"]) if r.get("label_num") is not None else "·"
        orders_str = _orders_column(r, Currency)
        print(
            f"   {num_col:<4} {r['username'][:20]:<20} {bal_str:<14} "
            f"{r['listings_cached']:<9} {orders_str:<34} "
            f"{r['history_events']:<8} {r['inventory_cached']:<5}  {last_seen}"
        )
    print(line)
    print(
        "   #  — это `label` из account.json. Если число — используется как номер для входа.\n"
        f"   Buy-ордера: исп. / макс. (= баланс × {BUY_ORDER_LIMIT_MULTIPLIER}). "
        "Если в кеше нет свежих ордеров — пройдись `2) Buy-ордера` по аккаунту."
    )


# =============================================================================
# Фаза 3 — sweep по всем аккаунтам + cross-account stats
# =============================================================================
# Те же 4 group'ы, что и в одно-аккаунтном `_show_inventory_generic`:
# `_SECTION_ORDER = ("free", "on_market", "trade_hold", "trade_protect")`.
# В кеш пишем именно эти значения через state_extractor → `_inventory_state`,
# который сам учитывает listed_asset_ids + protected_asset_ids.


# (AppContext.name → (app_id, context_id)) для публичного эндпоинта инвентаря.
# Используется в `_fetch_public_inventory_asset_ids` (см. задачу про «недавно
# разлоченные»: предмет в нашем приватном инвентаре, но не в публичной выдаче
# = display cooldown ~3 дня после разлока).
_PUBLIC_INVENTORY_APP_CONTEXT: dict[str, tuple[int, int]] = {
    "CS2":             (730, 2),
    "DOTA2":           (570, 2),
    "STEAM_COMMUNITY": (753, 6),
    "TF2":             (440, 2),
}


# Имитируем браузерный User-Agent для запросов к public inventory endpoint.
# Без него Steam часто отвечает 403 на «голый» aiohttp-запрос.
_PUBLIC_INV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://steamcommunity.com/",
}


async def _fetch_public_inventory_asset_ids(  # noqa: PLR0912, C901
    session,  # больше не используется — оставлен для обратной совместимости
    steam_id_64: int | str,
    ctx_str: str,
    *,
    proxy: str | None = None,
) -> tuple[set[str] | None, str | None]:
    """Возвращает (set asset_id'ов в публичной выдаче, error_reason | None).

    Использует `https://steamcommunity.com/inventory/<id>/<appid>/<ctxid>`
    через **отдельную чистую aiohttp-сессию без кук** (как в инкогнито).
    Если использовать `client.session` залогиненого SteamClient'а — Steam часто
    отвечает 401/403, потому что куки принадлежат другому юзеру.

    Стимовский лимит count=2000 за страницу (выше — 400). Идём страницами
    через `start_assetid` из `more_items`, max 10 страниц.

    Возвращает:
        (set, None)        — успех
        (None, "HTTP 401") — профиль приватный или Steam режект
        (None, "HTTP 429") — rate-limit
        (None, "HTTP 5xx") — Steam-серверная ошибка
        (None, "timeout")  — сетевой таймаут
        (None, "net:XXX")  — другая сетевая ошибка
        (None, "private")  — 200, но JSON без assets (профиль скрыл инвентарь)

    `proxy`: опционально HTTP/SOCKS прокси.
    """
    import aiohttp
    app_ctx = _PUBLIC_INVENTORY_APP_CONTEXT.get(ctx_str)
    if app_ctx is None:
        return (None, "unsupported-ctx")
    app_id, context_id = app_ctx
    url = (
        f"https://steamcommunity.com/inventory/{steam_id_64}/"
        f"{app_id}/{context_id}"
    )

    # Чистая сессия — без кук залогиненого аккаунта. cookie_jar=DummyCookieJar()
    # отключает приём set-cookie, чтобы между ретраями не накопить «трекинговые»
    # куки от Steam.
    connector_kwargs = {}
    timeout = aiohttp.ClientTimeout(total=30)
    out: set[str] = set()
    start_assetid: str | None = None
    last_error_reason: str | None = None

    try:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            headers=_PUBLIC_INV_HEADERS,
            timeout=timeout,
            **connector_kwargs,
        ) as fresh:
            for _page in range(10):
                params: dict[str, str | int] = {"l": "english", "count": 2000}
                if start_assetid:
                    params["start_assetid"] = start_assetid

                # Ретрай на 429 с backoff'ом (1с → 3с → 7с).
                backoffs = [0, 1, 3, 7]
                data = None
                for attempt, delay in enumerate(backoffs):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        get_kwargs: dict = dict(
                            url=url,
                            params=params,
                            raise_for_status=False,
                            allow_redirects=True,
                        )
                        if proxy:
                            get_kwargs["proxy"] = proxy
                        async with fresh.get(**get_kwargs) as resp:
                            status = resp.status
                            if status == 200:
                                try:
                                    data = await resp.json(content_type=None)
                                    break  # success — выйдем из retry-цикла
                                except Exception:  # noqa: BLE001
                                    last_error_reason = "parse-error"
                                    return (None, last_error_reason)
                            elif status == 429:
                                last_error_reason = f"HTTP 429"
                                # Идём на следующий backoff
                                continue
                            elif status in (401, 403):
                                # Профиль приватный / Steam режект — ретрай не поможет
                                return (None, f"HTTP {status}")
                            elif status >= 500:
                                last_error_reason = f"HTTP {status}"
                                # Steam-серверная — backoff может помочь
                                continue
                            else:
                                return (None, f"HTTP {status}")
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        last_error_reason = "timeout"
                        continue
                    except aiohttp.ClientError as exc:
                        last_error_reason = f"net:{type(exc).__name__}"
                        continue
                    except BaseException as exc:  # noqa: BLE001
                        last_error_reason = f"err:{type(exc).__name__}"
                        continue
                else:
                    # Все retry-попытки исчерпаны
                    return (None, last_error_reason or "retries-exhausted")

                if not isinstance(data, dict):
                    return (None, "not-json")
                assets = data.get("assets")
                # Профиль публичный, но инвентарь спрятан → 200, assets=None.
                if assets is None:
                    return (out if out else None, "private" if not out else None)
                for a in assets:
                    aid = a.get("assetid") or a.get("asset_id")
                    if aid:
                        out.add(str(aid))
                if not data.get("more_items"):
                    break
                nxt = data.get("last_assetid")
                if not nxt or str(nxt) == start_assetid:
                    break
                start_assetid = str(nxt)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        return (None, f"err:{type(exc).__name__}")

    return (out, None)


# Список (label_for_user, AppContext, app_context_str_name_for_cache).
def _sweep_inventory_contexts():
    """Возвращает list[(human_label, AppContext, ctx_str_for_cache)] для sweep'а."""
    from aiosteampy import AppContext

    return [
        ("Steam Cards", AppContext.STEAM_COMMUNITY, "STEAM_COMMUNITY"),
        ("CS2", AppContext.CS2, "CS2"),
        ("Dota 2", AppContext.DOTA2, "DOTA2"),
        ("TF 2", AppContext.TF2, "TF2"),
    ]


async def _sweep_one_account(  # noqa: PLR0912, PLR0915, C901
    account: dict,
    sessions: dict,
    force_relogin: bool,
    *,
    fetch_history: bool = True,
    proxy: str | None = None,
) -> dict:
    """Один аккаунт: login → balance → orders → inventories (+history delta).

    Возвращает dict со статусом для итоговой сводки.

    `proxy`: опциональный прокси (F3). Если задан, sweep этого акка пойдёт через
    proxy. ВАЖНО: если в `sessions` уже лежит main-IP-сессия для этого username,
    мы её НЕ переиспользуем (иначе sweep пойдёт без прокси). Делаем «proxy-only»
    сессию, sweep'имся, и НЕ кладём её в `sessions` — чтобы менюшные операции
    (place_sell / cancel_sell) шли через main IP, как просил Андрей.
    """
    from aiosteampy import AppContext
    import cache as _cache

    username = account["username"]
    label = account.get("label") or username
    result: dict = {
        "username": username,
        "label": label,
        "ok": False,
        "balance": None,
        "orders": None,
        "inventories": {},
        "history_added": 0,
        "errors": [],
    }

    # 1. login (или reuse из sessions). Если задан proxy — НЕ берём из sessions
    # (там main-IP-сессия), делаем отдельный логин и НЕ кладём результат в sessions.
    proxy_owned_client = None
    if proxy:
        connected = await _connect_account(account, force_relogin, proxy=proxy)
        if connected is None:
            result["errors"].append("login failed (proxy)")
            return result
        client, currency_code, cookies_file = connected
        proxy_owned_client = client  # закроем после sweep'а
    elif username in sessions:
        client, currency_code, cookies_file = sessions[username]
    else:
        connected = await _connect_account(account, force_relogin)
        if connected is None:
            result["errors"].append("login failed")
            return result
        client, currency_code, cookies_file = connected
        sessions[username] = (client, currency_code, cookies_file)

    # Признак «первый чек» определяем ДО `record_balance` — если в кеше
    # на этот username нет ни одного wallet_snapshots, значит это первый sweep.
    is_first_check = _cache.get_latest_balance(username) is None

    # 2. balance.
    try:
        info = await _with_retry(
            lambda: client.get_wallet_info(), what="get_wallet_info (sweep)"
        )
        balance_cents = int(info.get("wallet_balance", 0))
        on_hold_cents = int(info.get("wallet_delayed_balance", 0))
        code = int(info.get("wallet_currency", 0)) or currency_code
        _cache.record_balance(username, balance_cents, on_hold_cents, code)
        result["balance"] = {"balance_cents": balance_cents,
                              "on_hold_cents": on_hold_cents,
                              "currency_code": code}
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"balance: {type(exc).__name__}")
        balance_cents = None

    # 3. listings + buy-orders. Пагинируем `get_my_listings`, чтобы собрать
    #    ПОЛНЫЙ set listed_asset_ids (фаза 3 фикс F7 — иначе on_market не
    #    определяется правильно для предметов на маркете).
    listed_asset_ids: set[str] = set()
    try:
        active, _to_confirm, buy_orders, total_listings = await _with_retry(
            lambda: client.get_my_listings(start=0, count=100),
            what="get_my_listings (sweep)",
        )
        active = list(active) if active else []
        for lst in active:
            unowned = getattr(lst.item, "unowned_id", None)
            if unowned is not None:
                listed_asset_ids.add(str(unowned))
            listed_asset_ids.add(str(lst.item.asset_id))
        loaded = len(active)
        while loaded < (total_listings or 0):
            more_active, _, _, _ = await _with_retry(
                lambda s=loaded: client.get_my_listings(start=s, count=100),
                what="get_my_listings (sweep, more)",
            )
            more_active = list(more_active) if more_active else []
            if not more_active:
                break
            for lst in more_active:
                unowned = getattr(lst.item, "unowned_id", None)
                if unowned is not None:
                    listed_asset_ids.add(str(unowned))
                listed_asset_ids.add(str(lst.item.asset_id))
            loaded += len(more_active)
            await asyncio.sleep(0.3)
        _cache.record_buy_orders(username, buy_orders, currency_code)
        result["orders"] = len(buy_orders)
        result["listings_count"] = loaded
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"orders: {type(exc).__name__}")

    # 4. inventories: 4 игры. CS2 c доп-контекстом CS2_PROTECTED.
    for label_inv, app_context, ctx_str in _sweep_inventory_contexts():
        try:
            items, total, _ = await _with_retry(
                lambda ac=app_context: client.get_inventory(ac, count=2000),
                what=f"get_inventory ({label_inv}, sweep)",
            )
            items = list(items) if items else []
            protected_asset_ids: set[str] = set()
            if app_context is AppContext.CS2:
                try:
                    protected_items, _p_total, _ = await _with_retry(
                        lambda: client.get_inventory(AppContext.CS2_PROTECTED, count=2000),
                        what="get_inventory (CS2_PROTECTED, sweep)",
                    )
                    extra = list(protected_items) if protected_items else []
                    if extra:
                        protected_asset_ids = {str(it.asset_id) for it in extra}
                        items.extend(extra)
                        total = (total or 0) + len(extra)
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(
                        f"inv-{label_inv}-protected: {type(exc).__name__}"
                    )
            # state_extractor использует listed_asset_ids + protected_asset_ids
            # — те же 4 группы, что в одно-аккаунтном `_show_inventory_generic`:
            # "free" / "on_market" / "trade_hold" / "trade_protect".
            def _state(it, _laids=listed_asset_ids, _pids=protected_asset_ids):
                return _inventory_state(it, _laids, _pids)

            def _seed(it):
                _, s = _cs2_extract_wear_seed(it)
                return s

            def _wear(it):
                w, _ = _cs2_extract_wear_seed(it)
                return w

            _cache.record_inventory(
                username,
                ctx_str,
                items,
                paint_seed_extractor=_seed if app_context is AppContext.CS2 else None,
                paint_wear_extractor=_wear if app_context is AppContext.CS2 else None,
                state_extractor=_state,
                partial=False,
            )
            result["inventories"][label_inv] = len(items)
            # Параллельно дёргаем публичный инвентарь, чтобы посчитать diff:
            # asset_id, которых в публичной выдаче нет, но в приватной есть
            # = display cooldown (~3 дня после разлока, см. задачу 7-2).
            # Делаем ТОЛЬКО для CS2 — один доп. запрос на акк за весь sweep.
            try:
                sid = getattr(client, "steam_id", None)
                if sid and ctx_str == "CS2":
                    public_ids, err_reason = await _fetch_public_inventory_asset_ids(
                        client.session, sid, ctx_str, proxy=proxy,
                    )
                    if public_ids is not None:
                        priv_ids = {
                            str(it.asset_id) for it in items
                            if str(it.asset_id) not in protected_asset_ids
                        }
                        hidden = priv_ids - public_ids
                        visible = priv_ids & public_ids
                        n_hid, _n_vis = _cache.update_hidden_from_public(
                            username, ctx_str, hidden,
                            visible_asset_ids=visible,
                        )
                        result.setdefault("hidden_from_public", {})[label_inv] = n_hid
                    else:
                        # Эндпоинт вернул ошибку или 200 без assets — пишем точный код.
                        result["errors"].append(
                            f"public-inv-{label_inv}: {err_reason or 'unknown'}"
                        )
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(
                    f"public-inv-{label_inv}: {type(exc).__name__}"
                )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"inv-{label_inv}: {type(exc).__name__}")

    # 5. история сделок.
    #    Первый чек (баланса ДО sweep'а в БД не было) — качаем ТОЛЬКО 1 страницу
    #    (=10 событий), чтобы не лупасить Стим на новой установке.
    #    В последующие sweep'ы — идём страницами до первого known event_id,
    #    с hard-лимитом 20 страниц = 1000 событий.
    if fetch_history:
        try:
            known_ids = _cache.get_known_event_ids(username)
        except Exception:  # noqa: BLE001
            known_ids = set()
        if is_first_check:
            page_size = 10
            max_pages = 1
        else:
            page_size = 50
            max_pages = 20
        added_total = 0
        pages_fetched = 0
        try:
            start = 0
            while True:
                events, _total = await _with_retry(
                    lambda s=start, ps=page_size: client.get_my_market_history(
                        start=s, count=ps
                    ),
                    what=f"get_my_market_history (sweep, start={start})",
                )
                events_list = list(events)
                pages_fetched += 1
                if not events_list:
                    break
                # Останавливаемся, как только встретили любой известный event_id —
                # значит дальше всё уже в БД.
                hit_known = False
                fresh_events = []
                for ev in events_list:
                    listing = getattr(ev, "listing", None)
                    listing_id = getattr(listing, "id", None) if listing else None
                    ev_type = getattr(ev, "type", None)
                    ev_type_str = ev_type.name if hasattr(ev_type, "name") else str(ev_type)
                    time_event = getattr(ev, "time_event", None)
                    ev_id = f"{listing_id}:{_iso_or_none(time_event)}:{ev_type_str}"
                    if ev_id in known_ids:
                        hit_known = True
                        break
                    fresh_events.append(ev)
                if fresh_events:
                    added = _cache.record_history_events(
                        username, fresh_events, currency_code,
                        price_extractor=_history_event_price_cents,
                    )
                    added_total += added
                if (
                    hit_known
                    or len(events_list) < page_size
                    or pages_fetched >= max_pages
                ):
                    break
                start += page_size
                await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"history: {type(exc).__name__}")
        result["history_added"] = added_total
        result["history_first_check"] = is_first_check

    # Сохраняем cookies (cookies-jar отдельный на каждую сессию — proxy и
    # main session всё равно используют один и тот же cookies-файл и куки
    # перезатрут друг друга; это OK: куки между sweep'ом и обычными вызовами
    # совместимы).
    try:
        client.session.cookie_jar.save(cookies_file)
    except Exception:  # noqa: BLE001
        pass

    # Если работали с proxy — закроем эту сессию, чтобы не висели коннекты
    # и чтобы main-IP-сессия в `sessions` (если она там есть) осталась нетронутой.
    if proxy_owned_client is not None:
        try:
            await proxy_owned_client.session.close()
        except Exception:  # noqa: BLE001
            pass

    result["ok"] = not result["errors"]
    return result


def _iso_or_none(dt):
    """Мини-обёртка над cache._to_iso (чтобы не импортить приватный)."""
    from datetime import datetime as _dt, timezone as _tz
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, _dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.isoformat(timespec="seconds")
    return None


def _mask_proxy_for_log(proxy_url: str) -> str:
    """Маскирует логин/пароль в proxy URL для лога: `http://u:p@host:port` →
    `http://u:***@host:port`. Если креды отсутствуют — возвращает URL как есть.
    """
    if not proxy_url:
        return proxy_url
    try:
        from urllib.parse import urlsplit, urlunsplit
        s = urlsplit(proxy_url)
        netloc = s.netloc
        if "@" in netloc:
            creds, host = netloc.rsplit("@", 1)
            if ":" in creds:
                user, _pwd = creds.split(":", 1)
                netloc = f"{user}:***@{host}"
            else:
                netloc = f"{creds}@{host}"
        return urlunsplit((s.scheme, netloc, s.path, s.query, s.fragment))
    except Exception:  # noqa: BLE001
        return proxy_url


def _load_proxy_pool() -> list[str]:
    """Загружает список прокси для sweep'а (F3).

    Источник (в порядке приоритета):
      1) env `SWEEP_PROXY_FILE=/path/to/proxies.txt`
      2) `./proxies.txt` рядом с simple.py
      3) env `SWEEP_PROXY=http://...` — единственный прокси (legacy).

    Формат файла: одна строка = один прокси. Пустые строки и строки, начинающиеся
    с `#`, игнорируются. Допустимы схемы: `http://`, `https://`, `socks5://`,
    `socks5h://`. Допустимы креды: `http://user:pass@host:port`.

    Возвращает [] если пул не настроен — sweep пойдёт с main IP (как раньше).
    """
    candidates: list[Path] = []
    env_path = os.getenv("SWEEP_PROXY_FILE")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path(__file__).parent / "proxies.txt")

    for p in candidates:
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                print(f"[!] не смог прочитать {p}: {exc}")
                continue
            pool: list[str] = []
            for raw in lines:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Если в строке только host:port без схемы — добавим http://
                if "://" not in line:
                    line = "http://" + line
                pool.append(line)
            if pool:
                print(f"[..] Загружено прокси из {p}: {len(pool)} шт.")
                return pool

    # Fallback — единственный прокси через env.
    single = os.getenv("SWEEP_PROXY", "").strip()
    if single:
        if "://" not in single:
            single = "http://" + single
        print(f"[..] SWEEP_PROXY={_mask_proxy_for_log(single)} (один прокси)")
        return [single]
    return []


class _ProxyRotator:
    """Round-robin прокси с failover'ом.

    Используется одинаково в sweep / csfloat / сборе листингов:
        rot = _ProxyRotator(pool)
        for ... in N:
            for _ in range(max_tries):
                p = rot.current()
                ok = await do_thing(proxy=p)
                if ok: break
                rot.mark_bad()
            else:
                # все прокси не отвечают — выходим
                break
            rot.advance()

    Прокси, помеченный как `bad`, временно пропускается. Если все плохие — счётчик
    сбрасывается (вдруг сеть восстановилась), но больше одного полного круга
    не делаем.
    """

    def __init__(self, pool: list[str]) -> None:
        self._pool = list(pool)
        self._idx = 0
        self._bad: set[int] = set()

    @property
    def size(self) -> int:
        return len(self._pool)

    def current(self) -> str | None:
        if not self._pool:
            return None
        # ищем первый не-bad начиная с _idx
        for off in range(len(self._pool)):
            i = (self._idx + off) % len(self._pool)
            if i not in self._bad:
                self._idx = i
                return self._pool[i]
        # все bad — сбрасываем чёрный список и повторяем
        self._bad.clear()
        return self._pool[self._idx] if self._pool else None

    def mark_bad(self) -> None:
        if self._pool:
            self._bad.add(self._idx)
            self._idx = (self._idx + 1) % len(self._pool)

    def advance(self) -> None:
        """Просто перейти к следующему (без bad-mark)."""
        if self._pool:
            self._idx = (self._idx + 1) % len(self._pool)


async def _ask_use_proxy(operation_label: str) -> list[str]:
    """Если `proxies.txt` загрузился — спрашивает «использовать ли прокси».

    Возвращает list прокси (или [] если пул пуст / отказ). Печатает сводку.
    """
    pool = _load_proxy_pool()
    if not pool:
        return []
    ans = (await _ask(
        f"   Использовать прокси-пул ({len(pool)} шт.) для {operation_label}? "
        f"(y/N): "
    )).strip().lower()
    if ans in ("y", "yes", "д", "да"):
        return pool
    print(f"   [..] {operation_label}: иду с main IP (без прокси).")
    return []


async def _run_sweep(accounts: list[dict], sessions: dict, force_relogin: bool) -> None:
    """Запускает sweep по всем аккаунтам последовательно с прогрессом.

    Аккаунты сортируются тем же ключом, что и в `_pick_account`: сначала по
    числовому label по возрастанию (1, 2, 3, ..., 21, 22, ...), потом всё
    остальное по username. Раньше порядок шёл по `sub.name` из `iterdir`,
    т.е. лексикографически — поэтому 21 стоял раньше 5.
    """
    accounts = _sort_accounts(accounts)
    print("\n" + "=" * 120)
    print(f"   SWEEP — {len(accounts)} аккаунт(а/ов). Последовательно. Прерывание — Ctrl-C.")
    print("=" * 120)
    print(
        "  На каждом акке: balance → buy-orders → инвентари (Steam/CS2/Dota2/TF2) → "
        "дельта истории."
    )
    print("  Sell-листинги НЕ собираются (как ты просил — это слишком много запросов).")

    # F3: прокси-пул. Спрашиваем у пользователя, использовать ли его (если есть).
    proxy_pool = await _ask_use_proxy("sweep")
    if proxy_pool:
        print(
            f"  Proxy: {len(proxy_pool)} прокси в пуле, round-robin по аккаунтам + "
            f"failover. Только sweep — login/place_sell/cancel пойдут с main IP."
        )
    rotator = _ProxyRotator(proxy_pool)
    print()

    results: list[dict] = []
    started_at = datetime.now(timezone.utc)
    for idx, account in enumerate(accounts, 1):
        # F3.6: показываем номер акка (label_num) перед логином: `80 (alice)`.
        label_n = _label_num(account)
        username = account["username"]
        if label_n is not None:
            who = f"{label_n} ({username})"
        else:
            who = username
        prefix = f"[{idx}/{len(accounts)}] {who[:28]:<28}"
        # Жёсткий per-account timeout — без него медленный/мёртвый прокси может
        # подвесить ВЕСЬ sweep навечно. На Windows asyncio Ctrl-C не всегда
        # ловится, поэтому таймаут обязателен. 10 секунд на акк — с большим
        # запасом для нормальной сети.
        per_acc_timeout_sec = int(os.getenv("SWEEP_ACC_TIMEOUT_SEC", "10"))
        # На TIMEOUT (только!) ретраим тот же акк с СЛЕДУЮЩИМ прокси из ротатора.
        # Лимит ретраев: min(размер пула, 25). Если прокси нет — не ретраим вообще.
        max_proxy_attempts = min(rotator.size, 25) if rotator.size > 0 else 1
        res = None  # будет точно перезаписан
        proxy_attempt = 0
        keyboard_interrupted = False
        while True:
            proxy_attempt += 1
            proxy = rotator.current()
            attempt_tag = ""
            if rotator.size > 0 and max_proxy_attempts > 1:
                attempt_tag = f" (попытка {proxy_attempt}/{max_proxy_attempts})"
            if proxy:
                print(
                    f"{prefix}  логин (proxy {_mask_proxy_for_log(proxy)})"
                    f"{attempt_tag} ...",
                    flush=True,
                )
            else:
                print(f"{prefix}  логин{attempt_tag} ...", flush=True)
            try:
                res = await asyncio.wait_for(
                    _sweep_one_account(
                        account, sessions, force_relogin,
                        fetch_history=True, proxy=proxy,
                    ),
                    timeout=per_acc_timeout_sec,
                )
                break  # успех (даже если внутри res.ok=False, но не из-за таймаута)
            except asyncio.TimeoutError:
                # На таймаут — пробуем следующий прокси.
                print(
                    f"{prefix}  [TIMEOUT] не уложился в {per_acc_timeout_sec}s "
                    f"({'через прокси ' + _mask_proxy_for_log(proxy) if proxy else 'main IP'})."
                )
                if proxy:
                    rotator.mark_bad()
                if proxy_attempt >= max_proxy_attempts:
                    print(
                        f"{prefix}  [TIMEOUT] исчерпал {max_proxy_attempts} прокси-попыток."
                        " Пропускаю акк."
                    )
                    res = {
                        "username": username, "label": who,
                        "ok": False, "balance": None, "orders": None,
                        "inventories": {}, "history_added": 0,
                        "errors": [
                            f"TIMEOUT after {per_acc_timeout_sec}s × "
                            f"{max_proxy_attempts} попыток"
                        ],
                    }
                    break
                # Иначе — повторяем цикл, rotator.current() уже укажет на новый.
                continue
            except KeyboardInterrupt:
                print("\n[!] Прервано пользователем. Уже собранное в кеше осталось.")
                keyboard_interrupted = True
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{prefix}  [FATAL] {type(exc).__name__}: {exc}")
                res = {
                    "username": username, "label": who,
                    "ok": False, "balance": None, "orders": None,
                    "inventories": {}, "history_added": 0,
                    "errors": [f"FATAL: {type(exc).__name__}"],
                }
                break
        if keyboard_interrupted:
            break
        # who сохраняем в результат, чтобы в финальной сводке тоже была форма «80 (login)».
        res["who"] = who
        results.append(res)

        # Печатаем строку результата.
        bal_str = "—"
        if res.get("balance") and res["balance"].get("balance_cents") is not None:
            bal_str = _fmt_money_cents(res["balance"]["balance_cents"], "")
        inv_summary = ", ".join(
            f"{k}={v}" for k, v in res["inventories"].items()
        ) or "—"
        # Доп. сводка: сколько предметов в display-cooldown'е (hidden от публики).
        hidden_map = res.get("hidden_from_public") or {}
        hidden_summary = ""
        if hidden_map:
            n_total = sum(hidden_map.values())
            if n_total > 0:
                hidden_summary = f"  hidden={n_total}"
        if res["ok"]:
            print(
                f"{prefix}  OK  bal={bal_str}  orders={res['orders']}  "
                f"inv: {inv_summary}  history+{res['history_added']}{hidden_summary}"
            )
        else:
            print(
                f"{prefix}  FAIL  bal={bal_str}  orders={res['orders']}  "
                f"inv: {inv_summary}  errors: {'; '.join(res['errors'])}"
            )
        # Failover: если sweep вернул FAIL (но не по таймауту — таймауты уже
        # отыграли свой ретрай-цикл выше) и есть прокси — метим bad. На OK —
        # просто переходим к следующему прокси.
        if proxy and not res.get("ok"):
            rotator.mark_bad()
        else:
            rotator.advance()
        # Между аккаунтами небольшая пауза — Steam меньше нервничает.
        await asyncio.sleep(0.5)

    # Сводка.
    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    ok_count = sum(1 for r in results if r["ok"])
    fail_count = len(results) - ok_count
    total_history = sum(r["history_added"] for r in results)
    print("\n" + "=" * 120)
    print(
        f"   SWEEP завершён за {duration:.1f}s. "
        f"OK: {ok_count} / FAIL: {fail_count} из {len(results)}. "
        f"Всего новых событий истории: +{total_history}."
    )
    print("=" * 120)
    if fail_count:
        print("\n   Аккаунты с ошибками:")
        for r in results:
            if not r["ok"]:
                who = r.get("who") or r.get("label") or r["username"]
                print(f"     {who}: {'; '.join(r['errors']) or 'неизвестно'}")
    await _press_enter_to_continue()


# =============================================================================
# Глобальная статистика по всем инвентарям
# =============================================================================
_GAME_GROUPS = [
    ("Steam Cards", "STEAM_COMMUNITY"),
    ("CS2",         "CS2"),
    ("Dota 2",      "DOTA2"),
    ("TF 2",        "TF2"),
]

_CS2_STATE_GROUPS = [
    # human-label, state_value (как пишется в inventory_cache.state)
    ("Свободные",                       "free"),
    ("На маркете",                       "on_market"),
    ("Trade-protected (через трейд)",    "trade_protect"),
    ("Trade-hold (купленные с ТП)",      "trade_hold"),
]


def _summarize_inventory(
    rows: list[dict],
    *,
    label_lookup: dict[str, str] | None = None,
) -> list[tuple[str, int, int, str]]:
    """Группирует рядки `inventory_cache` по market_hash_name.

    Возвращает [(name, total_qty, accounts_count, examples), ...] упорядоченно
    по total_qty убыв. `examples` — строка вида «80×3, 81×5, ...» если у
    аккаунтов есть числовой label, иначе «alice×3, bob×5, ...».
    """
    from collections import defaultdict
    grouped: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        name = r.get("market_hash_name") or "(без имени)"
        grouped[name][r["username"]] += r.get("amount") or 1
    out: list[tuple[str, int, int, str]] = []
    for name, accs in grouped.items():
        total = sum(accs.values())
        examples_list = sorted(accs.items(), key=lambda x: -x[1])

        def _short(u: str) -> str:
            """Короткая форма для примеров: «80» если label числовой, иначе username."""
            if label_lookup:
                raw = label_lookup.get(u, "")
                if raw and raw.isdigit():
                    return raw
            return u

        examples = ", ".join(f"{_short(u)}×{q}" for u, q in examples_list[:5])
        if len(examples_list) > 5:
            examples += f", +{len(examples_list) - 5}"
        out.append((name, total, len(accs), examples))
    out.sort(key=lambda x: -x[1])
    return out


def _acc_display(username: str, label_lookup: dict[str, str] | None = None) -> str:
    """«80 (login)» если у аккаунта числовой label; иначе просто «login».

    label_lookup: dict username → label_num_as_str (или username, если label
    не числовой). Если None / пусто — fallback к username.
    """
    if not label_lookup:
        return username
    raw = label_lookup.get(username)
    if not raw:
        return username
    s = str(raw).strip()
    if s.isdigit() and s != username:
        return f"{s} ({username})"
    return username


def _build_label_lookup(accounts: list[dict]) -> dict[str, str]:
    """username → label_num (как строка), либо «» если label не числовой."""
    out: dict[str, str] = {}
    for a in accounts:
        u = a.get("username") or ""
        ln = _label_num(a)
        if ln is not None:
            out[u] = str(ln)
        else:
            out[u] = ""
    return out


async def _show_recently_unlocked(
    *,
    accounts: list[dict],
    sessions: dict,
    force_relogin: bool,
    label_lookup: dict[str, str],
) -> None:
    """Показывает предметы, недавно разлоченные (cross-account, ≤3 дня).

    Источник данных — флаг `hidden_from_public` в `inventory_cache`,
    проставляется во время sweep'а: diff между нашим (приватным) инвентарём
    и публичной выдачей Steam. Если предмет у нас есть, а в публичной выдаче
    его нет — он в display cooldown'е (~3 дня после разлока).

    Бэкап: если по какой-то причине флаг нигде не выставлен (профили закрыты
    или sweep ещё не делал public-diff), фоллбэчно показываем строки с
    `tradable_after ∈ [now-3d, now]` — как раньше.
    """
    import cache as _cache
    from datetime import datetime, timezone, timedelta

    all_rows = _cache.iter_inventory()
    now = datetime.now(timezone.utc)
    three_days_ago = now - timedelta(days=3)

    # Основной путь — по флагу hidden_from_public.
    recently = [r for r in all_rows if r.get("hidden_from_public")]
    source = "public-diff"
    if not recently:
        # Fallback на старую логику по tradable_after, для совместимости с
        # БД до этой миграции или с акками, у которых профиль закрыт.
        for r in all_rows:
            ta_raw = r.get("tradable_after")
            if not ta_raw:
                continue
            try:
                ta = datetime.fromisoformat(str(ta_raw))
                if ta.tzinfo is None:
                    ta = ta.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if three_days_ago <= ta <= now:
                recently.append(r)
        source = "tradable_after-fallback"

    if not recently:
        print(
            "\n   (недавно разлоченных предметов нет — пусто.\n"
            "    Сделай sweep если ещё не делал в этой сессии — он проставляет\n"
            "    флаг `hidden_from_public` сравнивая публичный инвентарь с приватным.)"
        )
        await _press_enter_to_continue()
        return

    from collections import defaultdict
    by_name: dict[str, list[dict]] = defaultdict(list)
    name_to_appctx: dict[str, str] = {}
    for r in recently:
        nm = r.get("market_hash_name") or "?"
        by_name[nm].append(r)
        name_to_appctx[nm] = r.get("app_context") or ""

    # (name, qty_recent, accounts_count_recent)
    grouped: list[tuple[str, int, int]] = []
    for nm, rs in by_name.items():
        grouped.append((nm, len(rs), len({rr["username"] for rr in rs})))
    grouped.sort(key=lambda x: -x[1])

    accounts_lookup = {a["username"]: a for a in accounts}

    def _render(items_slice, start_idx):
        print("\n" + "─" * 92)
        print(
            f"   Недавно разлочены (≤3 дня, {source}): "
            f"{len(grouped)} уник. имён, {len(recently)} предметов (cross-account)"
        )
        print("─" * 92)
        print(
            f"   {'#':<4} {'Имя':<50}  {'qty(новые)':<11}  {'аккаунтов':<10}"
        )
        print("─" * 92)
        for i, (nm, qty, accs_n) in enumerate(items_slice, start_idx):
            short = nm if len(nm) <= 50 else nm[:47] + "..."
            print(f"   {i:<4} {short:<50}  {qty:<11}  {accs_n:<10}")
        print("─" * 92)
        print(
            "   s <N> — bulk-list по имени (выставить N экз. с разных аккаунтов; "
            "берёт ВСЕ state=free, не только недавно разлоченные)"
        )

    async def _bulk_action(group_row, idx_1based, _all_groups):
        name = group_row[0]
        ctx_str = name_to_appctx.get(name) or ""
        # Все state=free для этого имени — НЕ ограничиваемся «разбанившимися»,
        # как и просит задача 7 («все конкретно, не только разбанившиеся»).
        candidates = [
            r for r in _cache.iter_inventory(app_context=ctx_str or None)
            if r.get("market_hash_name") == name
            and (r.get("state") or "") == "free"
        ]
        if not candidates:
            print(
                f"   Для «{name}» нет свободных экземпляров (state=free) — "
                "выставлять нечего."
            )
            return
        await _bulk_sell_cross_account(
            name=name,
            candidates=candidates,
            accounts_lookup=accounts_lookup,
            sessions=sessions,
            force_relogin=force_relogin,
            app_context_str=ctx_str,
            label_lookup=label_lookup,
        )

    page_size = max(len(grouped), 1)
    await _paginate(
        grouped, page_size, _render,
        extra_commands={"s": _bulk_action},
    )


# =============================================================================
# Задача 2: авто-принятие пустых трейдов в фоне.
#
# Идея: пользователь покупает кейсы на 5-6 аккаунтах ежедневно (через Trade Up
# Bot или подобный сервис). Бот шлёт трейд-офферы, где `items_to_give=[]`
# (юзер ничего не отдаёт, только получает). Если их не принять — товар
# отменяется. Скрипт в фоне поллит `get_trade_offers` каждые 5 мин и
# подтверждает пустые офферы автоматически.
#
# Безопасность: принимаем ТОЛЬКО офферы где `items_to_give` пуст. Если бот
# вдруг положит что-то «себе на отдачу» — пропустим, рукой принимай.
# =============================================================================
_AUTOTRADE_TASK = None  # type: ignore[var-annotated]
_AUTOTRADE_STATE: dict = {
    "usernames": [],
    "interval_sec": 300,
    "accepted": 0,
    "errors": 0,
    "last_poll": None,
    "started_at": None,
}


async def _autotrade_loop(
    usernames: list[str],
    sessions: dict,
    accounts_lookup: dict[str, dict],
    force_relogin: bool,
    label_lookup: dict[str, str],
    interval_sec: int,
) -> None:
    """Фоновый цикл: каждый `interval_sec` чекает офферы, принимает пустые.

    Шумит в stdout, но это «фоновое» в смысле asyncio — UI-поток продолжает
    обслуживать выбор аккаунта, и нам не приходится плодить отдельный поток.
    """
    print(
        f"[autotrade] start: {len(usernames)} акк(ов), poll every {interval_sec}s. "
        "Принимаем только офферы с items_to_give=[]."
    )
    _AUTOTRADE_STATE["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        while True:
            for username in list(usernames):
                try:
                    if username not in sessions:
                        acc = accounts_lookup.get(username)
                        if acc is None:
                            continue
                        connected = await _connect_account(acc, force_relogin)
                        if connected is None:
                            print(
                                f"[autotrade] {username}: login failed, пропускаю."
                            )
                            continue
                        sessions[username] = connected
                    client, _cur, _cf = sessions[username]
                    who = _acc_display(username, label_lookup)
                    try:
                        sent, recv, _total = await client.get_trade_offers(
                            active_only=True, sent=False, received=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        _AUTOTRADE_STATE["errors"] += 1
                        print(f"[autotrade] {who}: get_trade_offers упал: {exc!r}")
                        continue
                    for offer in (recv or []):
                        try:
                            if getattr(offer, "is_our_offer", False):
                                continue
                            if offer.items_to_give:
                                # юзер что-то отдаёт — НЕ автопринимаем
                                continue
                            n_recv = len(offer.items_to_receive)
                            try:
                                await client.accept_trade_offer(offer, confirm=True)
                                _AUTOTRADE_STATE["accepted"] += 1
                                print(
                                    f"[autotrade] {who}: принят оффер {offer.id} "
                                    f"(получаем {n_recv} предмет(ов), отдаём 0)."
                                )
                            except Exception as exc:  # noqa: BLE001
                                _AUTOTRADE_STATE["errors"] += 1
                                print(
                                    f"[autotrade] {who}: accept_trade_offer "
                                    f"{offer.id} упал: {exc!r}"
                                )
                        except Exception as exc:  # noqa: BLE001
                            _AUTOTRADE_STATE["errors"] += 1
                            print(
                                f"[autotrade] {who}: обработка оффера упала: {exc!r}"
                            )
                except Exception as exc:  # noqa: BLE001
                    _AUTOTRADE_STATE["errors"] += 1
                    print(f"[autotrade] {username}: poll упал: {exc!r}")
            _AUTOTRADE_STATE["last_poll"] = datetime.now(timezone.utc).isoformat()
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                raise
    except asyncio.CancelledError:
        print("[autotrade] остановлен пользователем.")
        raise


async def _collect_all_listings(
    accounts: list[dict],
    sessions: dict,
    force_relogin: bool,
) -> None:
    """Однократный сбор всех sell-листингов по всем акк-ам в `listings_cache`.

    - Идёт последовательно по акк-ам (по тому же порядку что sweep — label_num).
    - Спрашивает «использовать прокси?» (как обычный sweep).
    - Внутри каждого акка качает все страницы (count=100) до total.
    - Между акк-ами рандомная пауза 4-8 сек, чтобы Steam не насторожился.
    - На сетевую ошибку акка — если прокси, помечаем его как bad и пробуем
      следующий из ротатора; если main IP — просто переходим к следующему
      акку, в сводке отмечаем.
    - Записывает результат через `cache.record_listings(... partial=False)`,
      то есть стирает старые листинги акка и кладёт свежие.
    """
    import cache as _cache
    import random
    accounts = _sort_accounts(accounts)

    print("\n" + "=" * 100)
    print(
        f"   СБОР ВСЕХ SELL-ЛИСТИНГОВ по {len(accounts)} акк-ам "
        "(одноразовая операция)."
    )
    print("=" * 100)
    print(
        "   Каждый акк: get_my_listings страницами по 100, пока не дойдём до total.\n"
        "   Между акк-ами рандомная пауза 4-8 сек."
    )
    proxy_pool = await _ask_use_proxy("сбор листингов")
    rotator = _ProxyRotator(proxy_pool)
    if proxy_pool:
        print(
            f"   Proxy: {len(proxy_pool)} в пуле, round-robin + failover."
        )

    total_listings = 0
    ok_accs = 0
    fail_accs = 0
    started_at = datetime.now(timezone.utc)

    for idx, account in enumerate(accounts, 1):
        username = account["username"]
        label_n = _label_num(account)
        who = f"{label_n} ({username})" if label_n is not None else username
        prefix = f"   [{idx}/{len(accounts)}] {who[:30]:<30}"

        # Используем уже залогиненную сессию из `sessions`, если есть
        # И прокси не выбран (иначе строим отдельную сессию через _connect_account).
        proxy = rotator.current()
        client = None
        own_session = False
        if not proxy and username in sessions:
            client = sessions[username][0]
        else:
            # Логин с (опциональным) прокси. Сессию закроем после акка, в `sessions`
            # её не кладём (если был прокси — менюшным операциям он не нужен).
            connected = await _connect_account(
                account, force_relogin, proxy=proxy
            )
            if connected is None:
                print(f"{prefix}  [SKIP] login failed.")
                if proxy:
                    rotator.mark_bad()
                fail_accs += 1
                continue
            client = connected[0]
            own_session = True

        # Качаем все страницы.
        collected = []
        try:
            page_size = 100
            start = 0
            total = None
            while True:
                # _with_retry на сетевые таймауты
                active, _to_confirm, _bo, total = await _with_retry(
                    lambda s=start: client.get_my_listings(start=s, count=page_size),
                    what=f"get_my_listings (acc={username}, start={start})",
                )
                if not active:
                    break
                collected.extend(active)
                start += len(active)
                if total is not None and start >= total:
                    break
                # Мелкая пауза между страницами одного акка.
                await asyncio.sleep(random.uniform(1.0, 2.0))
            _cache.record_listings(username, collected, partial=False)
            print(
                f"{prefix}  OK  собрано {len(collected)} "
                f"(total Steam: {total if total is not None else '?'})"
            )
            total_listings += len(collected)
            ok_accs += 1
            if proxy:
                rotator.advance()
        except Exception as exc:  # noqa: BLE001
            print(f"{prefix}  FAIL  {type(exc).__name__}: {exc}")
            fail_accs += 1
            if proxy:
                rotator.mark_bad()
        finally:
            if own_session:
                try:
                    await client.session.close()
                except Exception:  # noqa: BLE001
                    pass

        # Пауза между акк-ами (рандомная, чтобы выглядело органично).
        if idx < len(accounts):
            pause = random.uniform(4.0, 8.0)
            await asyncio.sleep(pause)

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    print("\n" + "=" * 100)
    print(
        f"   Сбор листингов завершён за {duration:.1f}s. "
        f"Акк-ов OK: {ok_accs} / FAIL: {fail_accs}. Всего листингов: {total_listings}."
    )
    print("=" * 100)
    await _press_enter_to_continue()


async def _start_autotrade(
    accounts: list[dict],
    sessions: dict,
    force_relogin: bool,
) -> None:
    """Интерактивное меню задачи 2: старт / стоп / статус авто-принятия."""
    global _AUTOTRADE_TASK
    accounts_lookup = {a["username"]: a for a in accounts}
    label_lookup = _build_label_lookup(accounts)

    default_interval = int(os.getenv("AUTO_TRADE_POLL_SEC", "300") or 300)
    if default_interval < 30:
        default_interval = 30  # защита от спам-полла

    while True:
        running = _AUTOTRADE_TASK is not None and not _AUTOTRADE_TASK.done()
        print("\n" + "=" * 78)
        print("   АВТО-ПРИНЯТИЕ ПУСТЫХ ТРЕЙДОВ (фон)")
        print("=" * 78)
        print(
            f"   Статус: {'РАБОТАЕТ' if running else 'остановлено'}.  "
            f"Принято: {_AUTOTRADE_STATE['accepted']}, ошибок: "
            f"{_AUTOTRADE_STATE['errors']}."
        )
        if running:
            who = ", ".join(
                _acc_display(u, label_lookup)
                for u in _AUTOTRADE_STATE["usernames"]
            )
            print(f"   Аккаунты: {who}")
            print(
                f"   Интервал: {_AUTOTRADE_STATE['interval_sec']}с, "
                f"last_poll={_AUTOTRADE_STATE.get('last_poll') or '—'}"
            )
            print("   1) Остановить")
            print("   2) Статус (refresh)")
            print("   0) Назад (фон продолжает работать)")
        else:
            print("   1) Запустить (выбрать аккаунты)")
            print("   0) Назад")
        raw = (await _ask("\n   Выбор: ")).strip().lower()
        if raw in ("", "0"):
            return
        if running:
            if raw == "1":
                _AUTOTRADE_TASK.cancel()
                try:
                    await _AUTOTRADE_TASK
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    print(f"   [!] задача упала при остановке: {exc!r}")
                _AUTOTRADE_TASK = None
                print("   Авто-принятие остановлено.")
                continue
            if raw == "2":
                continue
            print("   Не понял.")
            continue
        if raw != "1":
            print("   Не понял.")
            continue
        # Запуск: выбор аккаунтов. Если у всех акков label — уникальные числа,
        # пользователь вводит именно эти label'ы (как в главном меню — 22, 53),
        # а не порядковую нумерацию.
        sorted_accs = _sort_accounts(accounts)
        label_nums = [_label_num(a) for a in sorted_accs]
        by_label = (
            all(n is not None for n in label_nums)
            and len(set(label_nums)) == len(label_nums)
        )
        print("\n   Аккаунты:")
        for i, a in enumerate(sorted_accs, 1):
            who = _acc_display(a["username"], label_lookup)
            key = str(_label_num(a)) if by_label else str(i)
            print(f"     {key:>4}. {who}")
        sel_raw = (await _ask(
            "\n   Введи "
            + ("номера акков (как в главном меню) " if by_label else "")
            + "через запятую (`22,53`) либо `all` для всех (q=отмена): "
        )).strip().lower()
        if sel_raw in ("", "q"):
            print("   Отменено.")
            continue
        if sel_raw == "all":
            usernames = [a["username"] for a in sorted_accs]
        else:
            usernames = []
            ok = True
            for part in sel_raw.replace(";", ",").split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    idx = int(part)
                except ValueError:
                    print(f"   «{part}» — не номер. Отмена.")
                    ok = False
                    break
                if by_label:
                    # Ищем акк по label_num
                    match = next(
                        (a for a in sorted_accs if _label_num(a) == idx),
                        None,
                    )
                    if match is None:
                        print(f"   Акка с label={idx} нет. Отмена.")
                        ok = False
                        break
                    usernames.append(match["username"])
                else:
                    if not (1 <= idx <= len(sorted_accs)):
                        print(
                            f"   {idx} вне диапазона 1..{len(sorted_accs)}. Отмена."
                        )
                        ok = False
                        break
                    usernames.append(sorted_accs[idx - 1]["username"])
            if not ok or not usernames:
                continue
            # дедуп с сохранением порядка
            seen = set()
            usernames = [u for u in usernames if not (u in seen or seen.add(u))]
        # Интервал.
        iv_raw = (await _ask(
            f"   Интервал между чеками в сек "
            f"(>=30, Enter={default_interval}): "
        )).strip()
        interval_sec = default_interval
        if iv_raw:
            try:
                interval_sec = max(30, int(iv_raw))
            except ValueError:
                print(f"   «{iv_raw}» — не число, оставляю {default_interval}с.")
                interval_sec = default_interval
        # Reset stats и поехали.
        _AUTOTRADE_STATE["usernames"] = list(usernames)
        _AUTOTRADE_STATE["interval_sec"] = interval_sec
        _AUTOTRADE_STATE["accepted"] = 0
        _AUTOTRADE_STATE["errors"] = 0
        _AUTOTRADE_STATE["last_poll"] = None
        _AUTOTRADE_TASK = asyncio.create_task(
            _autotrade_loop(
                usernames=usernames,
                sessions=sessions,
                accounts_lookup=accounts_lookup,
                force_relogin=force_relogin,
                label_lookup=label_lookup,
                interval_sec=interval_sec,
            )
        )
        print(
            f"   Запущено в фоне: {len(usernames)} акк(ов), интервал {interval_sec}с.\n"
            "   Меню остаётся активным; чтобы остановить — снова t) → 1) Остановить."
        )


async def _show_global_market_history(
    accounts: list[dict] | None = None,
    *,
    limit: int = 200,
) -> None:
    """Cross-account история сделок на маркете (последние N событий).

    Берёт записи из `market_history` (накапливается во время sweep'а), сортирует
    по времени убыв., печатает: [время] АККАУНТ — ТИП — name — цена currency.
    """
    import cache as _cache
    label_lookup = _build_label_lookup(accounts or [])
    from aiosteampy.constants import Currency as _Cur

    while True:
        events = _cache.iter_all_market_history(limit=limit)
        print("\n" + "=" * 100)
        print(f"   ИСТОРИЯ МАРКЕТА — последние {len(events)} событий (cross-account)")
        print("=" * 100)
        if not events:
            print("   (пусто — запусти sweep, во время него догрузится дельта истории)")
            print("\n   Enter — назад.")
            await _ask("")
            return

        # Группируем счётчики «куплено / продано / ещё что-то» — для шапки.
        from collections import Counter
        types = Counter(e.get("event_type") or "?" for e in events)
        accs = Counter(e.get("username") for e in events)
        print(
            "   Типы: "
            + ", ".join(f"{t}={n}" for t, n in types.most_common())
        )
        print(
            f"   С {len(accs)} аккаунтов (топ-5: "
            + ", ".join(
                f"{_acc_display(u, label_lookup)}={n}"
                for u, n in accs.most_common(5)
            )
            + ")"
        )
        print("-" * 100)
        print(
            f"   {'когда':<20} {'аккаунт':<18} {'тип':<14} "
            f"{'цена':<14}  название"
        )
        print("-" * 100)
        for ev in events:
            when = ev.get("time_event") or "—"
            who = _acc_display(ev.get("username") or "?", label_lookup)[:18]
            tp = (ev.get("event_type") or "?")[:14]
            name = ev.get("market_hash_name") or "?"
            pc = ev.get("price_cents")
            cc = ev.get("currency_code")
            if pc is None or not cc:
                price = "—"
            else:
                try:
                    price = _format_price(int(pc), _Cur, int(cc))
                except Exception:  # noqa: BLE001
                    price = f"{int(pc) / 100:.2f}"
            short_name = name if len(name) <= 50 else name[:47] + "..."
            print(f"   {when:<20} {who:<18} {tp:<14} {price:<14}  {short_name}")
        print("-" * 100)
        print(
            f"   `m <N>` — показать больше (например `m 500`; сейчас limit={limit}).\n"
            "   Enter / q — назад."
        )
        raw = (await _ask("\n   Выбор: ")).strip().lower()
        if not raw or raw in ("q", "0"):
            return
        if raw.startswith("m"):
            tail = raw[1:].strip()
            try:
                limit = int(tail) if tail else limit * 2
            except ValueError:
                print("   Не число.")
                continue
            if limit <= 0:
                limit = 200
                print("   Лимит должен быть > 0, ставлю 200.")
            continue
        print("   Не понял команду.")


async def _show_global_stats(
    accounts: list[dict] | None = None,
    sessions: dict | None = None,
    force_relogin: bool = False,
) -> None:  # noqa: PLR0912, C901
    """Меню глобальной cross-account статистики из inventory_cache.

    `accounts` — для отображения формата «80 (login)».
    `sessions` + `force_relogin` — пробрасываются в bulk-list (F3.3), чтобы
    переиспользовать уже открытые SteamClient сессии.
    """
    import cache as _cache
    label_lookup = _build_label_lookup(accounts or [])

    while True:
        print("\n" + "=" * 80)
        print("   ГЛОБАЛЬНАЯ СТАТИСТИКА (по всем аккаунтам, из кеша)")
        print("=" * 80)
        counts: dict[str, int] = {}
        for label, ctx_str in _GAME_GROUPS:
            counts[label] = sum(1 for r in _cache.iter_inventory(app_context=ctx_str))
        for i, (label, ctx_str) in enumerate(_GAME_GROUPS, 1):
            print(f"   {i}) {label:<14}— {counts[label]} предметов (в кеше)")
        print("   u) Недавно разлочены (≤3 дня, cross-account; diff публичный vs приватный)")
        print("   0) Назад")
        choice = (await _ask("\nВыбор: ")).strip()
        if choice == "0" or choice == "":
            return
        if choice.lower() == "u":
            await _show_recently_unlocked(
                accounts=accounts or [],
                sessions=sessions or {},
                force_relogin=force_relogin,
                label_lookup=label_lookup,
            )
            continue
        if choice not in {"1", "2", "3", "4"}:
            print("Не понял.")
            continue
        idx = int(choice) - 1
        game_label, ctx_str = _GAME_GROUPS[idx]
        rows = _cache.iter_inventory(app_context=ctx_str)
        if game_label == "CS2":
            await _show_cs2_subgroups(
                rows,
                label_lookup=label_lookup,
                accounts=accounts or [],
                sessions=sessions or {},
                force_relogin=force_relogin,
                app_context_str=ctx_str,
            )
        else:
            await _show_grouped_items(
                rows,
                title=game_label,
                label_lookup=label_lookup,
                accounts=accounts or [],
                sessions=sessions or {},
                force_relogin=force_relogin,
                app_context_str=ctx_str,
            )


async def _show_cs2_subgroups(
    rows: list[dict],
    *,
    label_lookup=None,
    accounts: list[dict] | None = None,
    sessions: dict | None = None,
    force_relogin: bool = False,
    app_context_str: str = "CS2",
) -> None:
    """Меню «4 группы CS2» — теперь те же 4 секции, что и в одно-аккаунтном
    инвентаре: Свободные / На маркете / Trade-protected / Trade-hold.
    """
    while True:
        print("\n" + "=" * 80)
        print("   CS2 — выбери группу")
        print("=" * 80)
        # Подсчёт по state.
        from collections import Counter
        c = Counter(r.get("state") or "?" for r in rows)
        for i, (label, state_val) in enumerate(_CS2_STATE_GROUPS, 1):
            print(f"   {i}) {label:<35}— {c.get(state_val, 0)} предметов")
        unknown = c.get("?", 0)
        if unknown:
            print(f"   (state=NULL у {unknown} строк — пройдись sweep'ом ещё раз)")
        print("   0) Назад")
        choice = (await _ask("\nВыбор: ")).strip()
        if choice == "0" or choice == "":
            return
        if choice not in {"1", "2", "3", "4"}:
            print("Не понял.")
            continue
        idx = int(choice) - 1
        label, state_val = _CS2_STATE_GROUPS[idx]
        sub = [r for r in rows if (r.get("state") or "") == state_val]
        await _show_grouped_items(
            sub,
            title=f"CS2 — {label}",
            label_lookup=label_lookup,
            accounts=accounts or [],
            sessions=sessions or {},
            force_relogin=force_relogin,
            app_context_str=app_context_str,
            state_filter=state_val,
        )


async def _show_grouped_items(
    rows: list[dict],
    *,
    title: str,
    label_lookup: dict[str, str] | None = None,
    accounts: list[dict] | None = None,
    sessions: dict | None = None,
    force_relogin: bool = False,
    app_context_str: str = "",
    state_filter: str | None = None,
) -> None:
    """Печатает сгруппированный список + пагинация.

    Команды:
        `i <N>` — детали по имени.
        `s <N>` — cross-account bulk-list (выставить N экз. с разных аккаунтов).
                  Показывается только если в выборке есть state=free.
        `c <N>` — cross-account bulk-cancel (снять N экз. с продажи).
                  Показывается только если в выборке есть state=on_market.

    `state_filter` — если вызвано из подгруппы CS2 (напр. «on_market»), используется
    в хинте и для выбора default-команды. Для flat-списков (Steam-cards / DOTA2 / TF2)
    приходит None — и s/c показываются по наличию элементов соотв. state.
    """
    if not rows:
        print(f"\n(пусто в группе «{title}». Запусти sweep, если ещё не запускал.)")
        await _press_enter_to_continue()
        return
    grouped = _summarize_inventory(rows, label_lookup=label_lookup)
    page_size = 15
    accounts_lookup = {a["username"]: a for a in (accounts or [])}

    has_free = any((r.get("state") or "") == "free" for r in rows)
    has_on_market = any((r.get("state") or "") == "on_market" for r in rows)

    # F1b: для on_market-row'ов подгрузим цену листинга из listings_cache.
    # Делаем это лениво — один INSERT-LATER-OK lookup на name (при entering details).
    import cache as _cache_mod
    from aiosteampy.constants import Currency as _CurrencyEnum

    def _enrich_listing_price(r: dict) -> dict:
        """Возвращает r + {'price_cents', 'currency_code', 'listing_id'} если on_market."""
        if (r.get("state") or "") != "on_market":
            return r
        row_with_price = dict(r)
        info = None
        try:
            info = _cache_mod.get_listing_by_asset_id(r["username"], r.get("asset_id"))
        except Exception:  # noqa: BLE001
            info = None
        if info:
            row_with_price["price_cents"] = info.get("price_cents")
            row_with_price["currency_code"] = info.get("currency_code")
            row_with_price["listing_id"] = info.get("listing_id")
        else:
            row_with_price["price_cents"] = None
            row_with_price["currency_code"] = None
            row_with_price["listing_id"] = None
        return row_with_price

    def _fmt_price_for_row(pc, cc) -> str:
        if pc is None:
            return "—"
        if cc:
            try:
                return _format_price(int(pc), _CurrencyEnum, int(cc))
            except Exception:  # noqa: BLE001
                pass
        return f"{int(pc) / 100:.2f}"

    # Для главной таблицы — собираем «price-range» для on_market-имён.
    # Это позволяет в таблице сразу видеть «1.99 — 2.50 ₽» если есть разные лоты.
    name_to_pricerange: dict[str, str] = {}
    if has_on_market:
        # Достанем все листинги для этих аккаунтов один раз — экономнее, чем
        # дёргать get_listing_by_asset_id для каждой строки.
        listing_lookup: dict[tuple[str, str], dict] = {}
        try:
            for r in rows:
                if (r.get("state") or "") != "on_market":
                    continue
                info = _cache_mod.get_listing_by_asset_id(r["username"], r.get("asset_id"))
                if info:
                    listing_lookup[(r["username"], str(r.get("asset_id")))] = info
        except Exception:  # noqa: BLE001
            listing_lookup = {}
        # Считаем диапазон цен на каждое имя.
        by_name: dict[str, list[tuple[int, int | None]]] = {}
        for r in rows:
            if (r.get("state") or "") != "on_market":
                continue
            info = listing_lookup.get((r["username"], str(r.get("asset_id"))))
            if not info or info.get("price_cents") is None:
                continue
            by_name.setdefault(r.get("market_hash_name") or "", []).append(
                (int(info["price_cents"]), info.get("currency_code"))
            )
        for name, prices in by_name.items():
            if not prices:
                continue
            min_p = min(p for p, _ in prices)
            max_p = max(p for p, _ in prices)
            cc = prices[0][1]  # берём валюту первого; если разные — _fmt вернёт «—»
            if min_p == max_p:
                name_to_pricerange[name] = _fmt_price_for_row(min_p, cc)
            else:
                name_to_pricerange[name] = (
                    f"{_fmt_price_for_row(min_p, cc)} – {_fmt_price_for_row(max_p, cc)}"
                )

    def _render(items_slice, start_idx_1based):
        print("\n" + "─" * 110)
        print(f"   {title}: {len(grouped)} уник. имён, всего {sum(g[1] for g in grouped)} шт.")
        print("─" * 110)
        if has_on_market:
            print(
                f"   {'#':<4} {'Имя':<40}  {'qty':<5}  {'акки':<5}  "
                f"{'цена':<16}  Примеры"
            )
        else:
            print(
                f"   {'#':<4} {'Имя':<46}  {'qty':<5}  {'аккаунтов':<10}  Примеры"
            )
        print("─" * 110)
        for i, (name, total, accs_n, examples) in enumerate(items_slice, start_idx_1based):
            if has_on_market:
                short_name = name if len(name) <= 40 else name[:37] + "..."
                price_col = name_to_pricerange.get(name, "—")
                print(f"   {i:<4} {short_name:<40}  {total:<5}  {accs_n:<5}  "
                      f"{price_col:<16}  {examples}")
            else:
                short_name = name if len(name) <= 46 else name[:43] + "..."
                print(f"   {i:<4} {short_name:<46}  {total:<5}  {accs_n:<10}  {examples}")
        print("─" * 110)
        hints = ["i <N> — детали (+ лоты по цене)"]
        if has_free:
            hints.append("s <N> — выставить N (free)")
        if has_on_market:
            hints.append("c <N> — снять N с продажи (on_market)")
        print("   " + "  /  ".join(hints))

    # `i <N>` — детали по имени с лотами (по цене) для on_market.
    async def _detail_action(group_row, idx_1based, all_rows):
        name = group_row[0]
        matches = [r for r in rows if r.get("market_hash_name") == name]

        def _sort_key(r):
            u = r["username"]
            raw = (label_lookup or {}).get(u, "")
            if raw and raw.isdigit():
                return (0, int(raw), u)
            return (1, 0, u)
        matches.sort(key=_sort_key)
        enriched = [_enrich_listing_price(r) for r in matches]

        # Группировка on_market по (price_cents, currency_code) — лоты.
        on_market = [r for r in enriched if (r.get("state") or "") == "on_market"]
        free = [r for r in enriched if (r.get("state") or "") == "free"]
        other = [r for r in enriched if (r.get("state") or "") not in ("on_market", "free")]

        from collections import defaultdict
        lots_map: dict[tuple, list[dict]] = defaultdict(list)
        for r in on_market:
            key = (r.get("price_cents"), r.get("currency_code"))
            lots_map[key].append(r)

        def _lot_sort_key(kv):
            (pc, _cc), items = kv
            # Сначала с известной ценой (по возрастанию), потом без цены.
            return (pc is None, pc if pc is not None else 0)

        lots = sorted(lots_map.items(), key=_lot_sort_key)

        # Цикл интерактивного выбора лота / снятия / выхода.
        while True:
            print(f"\n=== {name} — детали ===")
            print(f"   Всего экземпляров: {len(matches)} (с {group_row[2]} аккаунтов)")

            if free:
                print(f"\n   --- Свободные ({len(free)}) ---")
                for i, r in enumerate(free, 1):
                    extras = []
                    if r.get("paint_wear") is not None:
                        extras.append(f"float={float(r['paint_wear']):.4f}")
                    if r.get("paint_seed") is not None:
                        extras.append(f"seed={int(r['paint_seed'])}")
                    extras_str = "  " + " ".join(extras) if extras else ""
                    who = _acc_display(r["username"], label_lookup)
                    print(f"     {i:>3}. {who:<24}  asset={r['asset_id']:<16}{extras_str}")

            if other:
                print(f"\n   --- Прочие (trade-hold/protect) ({len(other)}) ---")
                for i, r in enumerate(other, 1):
                    state = r.get("state") or "?"
                    who = _acc_display(r["username"], label_lookup)
                    print(f"     {i:>3}. {who:<24}  asset={r['asset_id']:<16}  state={state}")

            if lots:
                print(f"\n   --- Лоты на маркете ({len(on_market)} шт., {len(lots)} лот(ов)) ---")
                for li, ((pc, cc), items_l) in enumerate(lots, 1):
                    by_acc_l = defaultdict(int)
                    for r in items_l:
                        by_acc_l[r["username"]] += 1
                    accs_str = ", ".join(
                        f"{_acc_display(u, label_lookup)}×{q}"
                        for u, q in sorted(by_acc_l.items(), key=lambda x: -x[1])
                    )
                    price_str = _fmt_price_for_row(pc, cc) if pc is not None else "(цена ?)"
                    print(f"     Лот {li}: {price_str:<14}  × {len(items_l):<3}  ({accs_str})")
            else:
                if on_market:
                    print(
                        f"\n   --- На маркете ({len(on_market)}) — нет цен в кеше ---"
                    )
                    for i, r in enumerate(on_market, 1):
                        who = _acc_display(r["username"], label_lookup)
                        print(f"     {i:>3}. {who:<24}  asset={r['asset_id']:<16}  "
                              "(цена ?, сделай sweep)")

            if not lots:
                # Если лотов нет — просто прощёлкиваем Enter (как раньше).
                await _press_enter_to_continue()
                return

            cmd = (await _ask(
                "\n   c <L> — снять лот L целиком / c all — снять все on_market / "
                "Enter — выход: "
            )).strip().lower()

            if cmd in ("", "q", "b", "exit", "quit"):
                return
            if cmd in ("c all", "c-all", "call", "c *"):
                # Снимаем все on_market независимо от лота.
                if not on_market:
                    print("   (нечего снимать)")
                    continue
                await _bulk_cancel_cross_account(
                    name=name,
                    listed_rows=on_market,
                    accounts_lookup=accounts_lookup,
                    sessions=sessions or {},
                    force_relogin=force_relogin,
                    label_lookup=label_lookup or {},
                    preselected=True,
                    title_suffix="все лоты",
                )
                # После снятия пересоберём enriched/lots.
                matches = [r for r in rows if r.get("market_hash_name") == name]
                matches.sort(key=_sort_key)
                enriched = [_enrich_listing_price(r) for r in matches]
                on_market = [r for r in enriched if (r.get("state") or "") == "on_market"]
                free = [r for r in enriched if (r.get("state") or "") == "free"]
                other = [r for r in enriched if (r.get("state") or "") not in ("on_market", "free")]
                lots_map = defaultdict(list)
                for r in on_market:
                    key = (r.get("price_cents"), r.get("currency_code"))
                    lots_map[key].append(r)
                lots = sorted(lots_map.items(), key=_lot_sort_key)
                continue
            if cmd.startswith("c "):
                parts = cmd.split()
                if len(parts) != 2 or not parts[1].isdigit():
                    print("   Формат: `c <номер лота>` или `c all`.")
                    continue
                li = int(parts[1])
                if li < 1 or li > len(lots):
                    print(f"   Лот {li} не существует (есть 1..{len(lots)}).")
                    continue
                (pc, cc), lot_items = lots[li - 1]
                price_str = _fmt_price_for_row(pc, cc) if pc is not None else "(цена ?)"
                await _bulk_cancel_cross_account(
                    name=name,
                    listed_rows=lot_items,
                    accounts_lookup=accounts_lookup,
                    sessions=sessions or {},
                    force_relogin=force_relogin,
                    label_lookup=label_lookup or {},
                    preselected=True,
                    title_suffix=f"лот {li}: {price_str}",
                )
                # Пересоберём.
                matches = [r for r in rows if r.get("market_hash_name") == name]
                matches.sort(key=_sort_key)
                enriched = [_enrich_listing_price(r) for r in matches]
                on_market = [r for r in enriched if (r.get("state") or "") == "on_market"]
                free = [r for r in enriched if (r.get("state") or "") == "free"]
                other = [r for r in enriched if (r.get("state") or "") not in ("on_market", "free")]
                lots_map = defaultdict(list)
                for r in on_market:
                    key = (r.get("price_cents"), r.get("currency_code"))
                    lots_map[key].append(r)
                lots = sorted(lots_map.items(), key=_lot_sort_key)
                continue
            print(f"   (не понял «{cmd}»)")

    # `s <N>` — cross-account bulk-list.
    async def _bulk_sell_action(group_row, idx_1based, all_rows):
        name = group_row[0]
        # Только state=free можно выставлять (on_market уже на маркете,
        # trade_hold / trade_protect — Steam откажет).
        candidates = [
            r for r in rows
            if r.get("market_hash_name") == name and (r.get("state") or "") == "free"
        ]
        if not candidates:
            print(f"\n   (нет свободных экземпляров «{name}» — все либо на маркете, "
                  f"либо в trade-hold / trade-protect)")
            await _press_enter_to_continue()
            return
        await _bulk_sell_cross_account(
            name=name,
            candidates=candidates,
            accounts_lookup=accounts_lookup,
            sessions=sessions or {},
            force_relogin=force_relogin,
            app_context_str=app_context_str,
            label_lookup=label_lookup or {},
        )

    # `c <N>` — cross-account bulk-cancel для всех on_market-экземпляров имени.
    # Сначала спрашивает «сколько снять» (1..N или all) — как раньше.
    async def _bulk_cancel_action(group_row, idx_1based, all_rows):
        name = group_row[0]
        listed = [
            r for r in rows
            if r.get("market_hash_name") == name and (r.get("state") or "") == "on_market"
        ]
        if not listed:
            print(f"\n   (нет выставленных экземпляров «{name}» — ничего снимать)")
            await _press_enter_to_continue()
            return
        await _bulk_cancel_cross_account(
            name=name,
            listed_rows=listed,
            accounts_lookup=accounts_lookup,
            sessions=sessions or {},
            force_relogin=force_relogin,
            label_lookup=label_lookup or {},
        )

    extra: dict = {"i": _detail_action}
    if has_free:
        extra["s"] = _bulk_sell_action
    if has_on_market:
        extra["c"] = _bulk_cancel_action

    # Task 3: позволяем менять порядок сортировки прямо в этой пагинации.
    # Команды без аргументов уходят в `bulk_commands` (точное совпадение строки).
    #
    # Сортировка работает над `grouped` in-place — `_paginate` каждой итерацией
    # пересекает items[start:end], так что новый порядок виден сразу.
    max_aid_by_name: dict[str, int] = {}
    for r in rows:
        nm = r.get("market_hash_name") or ""
        aid = r.get("asset_id")
        if aid is None:
            continue
        try:
            aid_int = int(aid)
        except (TypeError, ValueError):
            continue
        if aid_int > max_aid_by_name.get(nm, -1):
            max_aid_by_name[nm] = aid_int

    # Считаем макс. известную цену за штуку для каждого имени:
    # 1) сначала из активных листингов (listings_cache),
    # 2) затем добиваем из истории сделок маркета (market_history), чтобы
    #    `sort price` работал даже когда у нас ничего не на маркете прямо сейчас
    #    (например, во вьюхе «Свободные» — там has_on_market=False).
    max_price_by_name: dict[str, int] = {}
    if has_on_market:
        for r in rows:
            if (r.get("state") or "") != "on_market":
                continue
            info = listing_lookup.get((r["username"], str(r.get("asset_id"))))
            if not info or info.get("price_cents") is None:
                continue
            nm = r.get("market_hash_name") or ""
            try:
                pc = int(info["price_cents"])
            except (TypeError, ValueError):
                continue
            if pc > max_price_by_name.get(nm, -1):
                max_price_by_name[nm] = pc
    # market_history даёт реальные цены сделок (по всем аккаунтам), цены могут
    # быть в разных валютах — для сортировки нам годится «любая прокси-цена»,
    # так что игнорируем currency_code и берём max(price_cents).
    try:
        import cache as _cache_for_price  # лазовый импорт, без top-level
        for ev in _cache_for_price.iter_all_market_history(limit=10000):
            nm = ev.get("market_hash_name") or ""
            pc = ev.get("price_cents")
            if not nm or pc is None:
                continue
            try:
                pc = int(pc)
            except (TypeError, ValueError):
                continue
            if pc > max_price_by_name.get(nm, -1):
                max_price_by_name[nm] = pc
    except Exception as exc:  # noqa: BLE001
        # На отсутствие истории не падаем — просто `sort price` отсортирует те,
        # о которых данных нет, в конец (см. ниже).
        print(f"   [warn] не смог дочитать market_history для sort price: {exc!r}")

    def _resort_in_place(items, mode: str) -> None:
        if mode == "qty":
            items.sort(key=lambda g: -g[1])
        elif mode == "name":
            items.sort(key=lambda g: g[0].lower())
        elif mode == "new":
            items.sort(key=lambda g: -max_aid_by_name.get(g[0], -1))
        elif mode == "price":
            items.sort(key=lambda g: -max_price_by_name.get(g[0], -1))

    def _make_sort_action(mode: str):
        async def _act(items_list):
            _resort_in_place(items_list, mode)
            print(f"   [sort] переотсортировано по: {mode}.")
        return _act

    bulk = {
        "sort qty":   _make_sort_action("qty"),
        "sort new":   _make_sort_action("new"),
        "sort price": _make_sort_action("price"),
        "sort name":  _make_sort_action("name"),
    }

    print(
        "   Сортировка: `sort qty` (по умолчанию) / `sort new` (по новизне) "
        "/ `sort price` (макс. известная цена: листинги + история) / `sort name`."
    )

    await _paginate(
        grouped, page_size, _render,
        extra_commands=extra,
        bulk_commands=bulk,
    )


def _account_currency_code(
    username: str, sessions: dict, accounts_lookup: dict[str, dict]
) -> int | None:
    """Возвращает currency_code для аккаунта (или None, если не определён).

    Порядок: 1) `sessions[username][1]` (живой SteamClient или Currency-enum),
    2) `cache.get_latest_balance(...)` — fallback для акков без сессии.
    """
    if username in sessions:
        _client, cur, _cf = sessions[username]
        if cur is not None:
            try:
                return int(cur)
            except (TypeError, ValueError):
                pass
    try:
        import cache as _cache
        bal = _cache.get_latest_balance(username)
        if bal and bal.get("currency_code"):
            return int(bal["currency_code"])
    except Exception:  # noqa: BLE001
        pass
    return None


async def _auto_price_show_filter_listings(  # noqa: PLR0913
    client,
    app_id: int,
    gid: str,
    *,
    quality_tag: str | None,
    our_float: float,
    our_seed: int | None,
    currency_code: int,
    cur_sym: str,
    name: str,
) -> None:
    """Печатает листинги, отфильтрованные теми же параметрами что path_b_suggest.

    Команда `i <N>` в авто-подборе цены — чтобы видеть, какие конкретно
    листинги Steam отдаёт под фильтром (quality + float ∈ [0, our_float*1.10],
    без exterior) и почему авто-цена выдала именно такую цифру.
    """
    try:
        import item_info as _ii
    except ImportError as exc:  # pragma: no cover
        print(f"   [BUG] item_info не загружен: {exc}")
        return

    f_max = max(0.0, min(1.0, our_float * 1.10))
    cat_f: dict[str, list[str]] = {}
    if quality_tag:
        cat_f["category_730_Quality"] = [quality_tag]

    print()
    print(f"   === Листинги под path_b-фильтром: «{name}» ===")
    seed_part = f", seed={our_seed}" if our_seed is not None else ""
    print(
        f"   quality={quality_tag or '—'}  "
        f"float ∈ [0.0000, {f_max:.4f}]  (наш={our_float:.4f}{seed_part})"
    )

    try:
        data = await _ii._fetch_listings_page(  # noqa: SLF001
            client.session, app_id, gid,
            start=0, sort_field=0, sort_dir=0,
            category_filters=cat_f or None,
            wear_range=(0.0, f_max),
            currency_code=currency_code,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] POST упал: {type(exc).__name__}: {exc}")
        return
    if not data:
        print("   [!] POST вернул пусто.")
        return
    parsed = _ii._parse_listings_v2(data)  # noqa: SLF001
    total = int(data.get("total_count") or len(parsed))
    if not parsed:
        print(f"   [!] под фильтром листингов нет (total_count={total}).")
        return

    print(f"   Найдено {len(parsed)} (показываю до 20) из total≈{total}.")
    print(f"   {'#':<3} {'price':>12} {'float':>8} {'seed':>6}")
    print("   " + "-" * 35)
    for i, li in enumerate(parsed[:20], 1):
        price = li.get("price_cents") or 0
        fl = li.get("float")
        sd = li.get("paint_seed")
        price_str = f"{price/100:.2f} {cur_sym}"
        fl_str = f"{fl:.4f}" if isinstance(fl, (int, float)) else "—"
        sd_str = str(sd) if isinstance(sd, int) else "—"
        marker = " *" if our_seed is not None and sd == our_seed else "  "
        print(f"  {marker}{i:<3} {price_str:>12} {fl_str:>8} {sd_str:>6}")


async def _auto_price_group(  # noqa: PLR0912, PLR0915, C901
    client,
    currency_enum,
    currency_code: int,
    *,
    name: str,
    group: list[dict],
    label_lookup: dict[str, str],
    cur_sym: str,
) -> tuple[str, "int | dict[str, int]"] | None:
    """Авто-цена для группы candidates одной валюты в cross-account bulk-list.

    Возвращает:
      • ("uniform", cents)    — одна общая цена (Path A, без флоата);
      • ("per_item", {asset_id_str: cents, ...}) — индивидуальная (Path B);
      • ("skip", None)        — юзер выбрал «пропустить эту валюту»;
      • None                  — юзер отменил подбор, вернуть в основной промпт.
    """
    from aiosteampy.constants import App

    try:
        import item_info as _ii
        import price_suggest as _ps
    except ImportError as exc:  # pragma: no cover
        print(f"   [BUG] модуль не загружен: {exc}")
        return None

    print(f"\n   [auto-price] анализирую «{name}» для группы из {len(group)} экз. ...")

    app = App.CS2
    app_id = int(app.value)

    # Определяем «общий» путь группы: если ни у кого нет paint_seed → Path A.
    # Иначе классифицируем КАЖДЫЙ кандидат отдельно (поскольку seed/float у них разные).
    has_seed = any(c.get("paint_seed") is not None for c in group)

    # Базовые метрики (общие для всей группы).
    history = None
    try:
        history = await client.fetch_price_history(name, app)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] price_history недоступна: {type(exc).__name__}: {exc}")
    daily = _ps.daily_sales_from_history(history) if history else 0.0
    week = _ps.week_pct_from_history(history) if history else None
    if daily <= 0:
        print(
            "   [!] daily_sales = 0 — Path A/B не считается. Вводи цену вручную."
        )
        return None

    week_str = f"{week:+.1f}%" if week is not None else "?"
    print(f"   daily_sales≈{daily:.0f}/день, week={week_str}.")

    if not has_seed:
        # Path A — одна цена для всей группы.
        nameid = await _ii.resolve_item_nameid(client, app_id, name)
        if nameid is None:
            print("   [!] item_nameid не получен — Path A без histogram не работает.")
            return None
        try:
            histogram, _ = await client.get_item_orders_histogram(nameid)
        except Exception as exc:  # noqa: BLE001
            print(f"   [!] histogram недоступен: {type(exc).__name__}: {exc}")
            return None
        sell_table: list[tuple[int, int]] = []
        for row in histogram.sell_order_table or ():
            p = getattr(row, "price", None)
            q = getattr(row, "quantity", None)
            if p is not None and q is not None:
                sell_table.append((int(p), int(q)))
        if not sell_table:
            print("   [!] sell_order_table пустой.")
            return None
        sug = _ps.path_a_suggest(sell_table, daily, week)
        if sug.cents is None:
            print("   [!] Path A: суггест не вышел.")
            return None
        print(
            f"\n   ► Path A (коммодити, всем {len(group)} экз.): "
            f"{sug.cents/100:.2f} {cur_sym}"
        )
        print(f"   reason: {sug.reason}")
        ans = (await _ask(
            "   [y=принять / число=своя цена / skip=пропустить валюту / n=отмена]: "
        )).strip().lower().replace(",", ".")
        if ans in ("y", "yes", ""):
            return ("uniform", int(sug.cents))
        if ans in ("skip", "s"):
            return ("skip", None)
        if ans in ("n", "no", "q", "b"):
            return None
        try:
            cents = int(round(float(ans) * 100))
            if cents > 0:
                return ("uniform", cents)
        except ValueError:
            pass
        print(f"   Не понял «{ans}», отменяю авто-подбор.")
        return None

    # Path B/C — нужны индивидуальные цены.
    # Резолвим GID и теги один раз — название одинаковое у всей группы.
    gid = await _ii.resolve_gid(client, app_id, name)
    if not gid:
        print("   [!] GID не получен — Path B недоступен.")
        return None
    quality_tags, exterior_tags = _ii._default_filters_from_name(name)  # noqa: SLF001
    q_tag = quality_tags[0] if quality_tags else None
    e_tag = exterior_tags[0] if exterior_tags else None

    # Считаем суггест для каждого кандидата.
    suggestions: list[dict] = []  # {row, path, cents, reason}
    print()
    print(
        f"   {'#':<3} {'acc':<14} {'float':>7} {'seed':>5} "
        f"{'suggest':>10} {'cur':<4}path  reason"
    )
    print("   " + "-" * 90)
    for idx, c in enumerate(group, 1):
        seed = c.get("paint_seed")
        pw = c.get("paint_wear")
        who = _acc_display(c["username"], label_lookup)
        path = _ps.classify(name, seed)
        cents: int | None = None
        reason = ""
        if path == "C":
            try:
                import patterns
                tier = patterns.is_rare_pattern(name, int(seed or 0)).tier_note or "?"
            except Exception:  # noqa: BLE001
                tier = "?"
            reason = f"редкий ({tier}) — ручной ввод"
        elif path == "B":
            sug_b = await _ps.path_b_suggest(
                client.session, app_id, gid,
                our_float=float(pw or 0.0),
                quality_tag=q_tag,
                exterior_tag=e_tag,
                currency_code=currency_code,
                daily_sales=daily,
            )
            cents = sug_b.cents
            reason = sug_b.reason
        else:  # path == "A" — у seed нет, попадание сюда маловероятно но всё же
            # fallback к Path A (histogram). Использовать уже полученную таблицу,
            # если есть; если нет — пропустить.
            reason = "no seed → A (но группа с seed; пропускаю)"

        suggestions.append({
            "row": c, "path": path, "cents": cents, "reason": reason,
            "skip": path == "C" or cents is None,
        })
        cents_str = f"{cents/100:.2f}" if cents is not None else "—"
        fl_str = f"{float(pw):.4f}" if pw is not None else "—"
        seed_str = str(seed) if seed is not None else "—"
        print(
            f"   {idx:<3} {who[:14]:<14} {fl_str:>7} {seed_str:>5} "
            f"{cents_str:>10} {cur_sym:<4}{path}    {reason}"
        )
        # Троттлим между POST'ами path_b_suggest к Steam Market — без
        # паузы большая группа флоатов бьёт endpoint пачкой и Steam ловит 429.
        if path == "B" and idx < len(group):
            await asyncio.sleep(0.5)

    while True:
        print(
            "\n   [y=выставить, edit <N> <price>=поправить, "
            "i <N>=листинги под фильтром, skip <N>=исключить, n=отменить]: ",
            end="",
        )
        ans = (await _ask("")).strip()
        if not ans or ans.lower() in ("n", "no", "q", "b"):
            return None
        if ans.lower() in ("y", "yes"):
            # Фильтруем skip-помеченные.
            per_item: dict[str, int] = {}
            for s in suggestions:
                if s["skip"] or s["cents"] is None:
                    continue
                per_item[str(s["row"]["asset_id"])] = int(s["cents"])
            if not per_item:
                print("   [!] ни одной валидной цены — нечего выставлять.")
                return None
            n_skipped = sum(1 for s in suggestions if s["skip"])
            if n_skipped:
                print(f"   [info] {n_skipped} экз. пропущено (Path C / нет цены).")
            return ("per_item", per_item)
        # edit <N> <price>
        parts = ans.split()
        if len(parts) == 3 and parts[0].lower() == "edit":
            try:
                n = int(parts[1])
                new_cents = int(round(float(parts[2].replace(",", ".")) * 100))
            except ValueError:
                print("   [!] формат: edit <N> <price>, например `edit 3 5.21`.")
                continue
            if not 1 <= n <= len(suggestions):
                print(f"   [!] N должен быть 1..{len(suggestions)}.")
                continue
            if new_cents <= 0:
                print("   [!] цена должна быть > 0.")
                continue
            suggestions[n - 1]["cents"] = new_cents
            suggestions[n - 1]["skip"] = False
            suggestions[n - 1]["reason"] = "(ручная правка)"
            print(f"   [ok] {n}: {new_cents/100:.2f} {cur_sym}")
            continue
        if len(parts) == 2 and parts[0].lower() == "i":
            try:
                n = int(parts[1])
            except ValueError:
                print("   [!] формат: i <N>.")
                continue
            if not 1 <= n <= len(suggestions):
                print(f"   [!] N должен быть 1..{len(suggestions)}.")
                continue
            s = suggestions[n - 1]
            row = s["row"]
            pw_i = row.get("paint_wear")
            if pw_i is None:
                print("   [!] у предмета нет paint_wear — Path B info недоступна.")
                continue
            seed_i = row.get("paint_seed")
            await _auto_price_show_filter_listings(
                client, app_id, gid,
                quality_tag=q_tag,
                our_float=float(pw_i),
                our_seed=int(seed_i) if seed_i is not None else None,
                currency_code=currency_code, cur_sym=cur_sym,
                name=name,
            )
            continue
        if len(parts) == 2 and parts[0].lower() == "skip":
            try:
                n = int(parts[1])
            except ValueError:
                print("   [!] формат: skip <N>.")
                continue
            if not 1 <= n <= len(suggestions):
                print(f"   [!] N должен быть 1..{len(suggestions)}.")
                continue
            suggestions[n - 1]["skip"] = True
            print(f"   [ok] #{n} пропущен.")
            continue
        print(f"   [!] не понял «{ans}». Команды: y / edit N PRICE / i N / skip N / n.")


async def _bulk_sell_cross_account(  # noqa: PLR0912, PLR0915, C901
    *,
    name: str,
    candidates: list[dict],
    accounts_lookup: dict[str, dict],
    sessions: dict,
    force_relogin: bool,
    app_context_str: str,
    label_lookup: dict[str, str],
) -> None:
    """Выставляет N экземпляров `name` с разных аккаунтов.

    `candidates` — все строки inventory_cache для этого скина у state='free'.
    Сессии берутся из `sessions` (переиспользование), если нет — `_connect_account`.

    Мультивалютность: если у акков в выборке разные валюты, будет отдельный промпт
    цены по каждой валюте (без этого Steam поставил бы одну и ту же «1.99» в рублях
    и в долларах — получили бы несопоставимые цены).

    Поддерживает CS2 (с min-float фильтром) и любые другие игры.
    """
    from aiosteampy.constants import App, AppContext, Currency

    # Маппинг context_str → AppContext (нужен place_sell_listing).
    # В aiosteampy для Steam-cards правильное имя — STEAM_COMMUNITY.
    app_context_map = {
        "STEAM":           AppContext.STEAM_COMMUNITY,
        "STEAM_COMMUNITY": AppContext.STEAM_COMMUNITY,
        "CS2":             AppContext.CS2,
        "DOTA2":           AppContext.DOTA2,
        "TF2":             AppContext.TF2,
    }
    target_app_context = app_context_map.get(app_context_str)
    if target_app_context is None:
        print(f"   [ERR] Не знаю как маппить app_context_str={app_context_str!r}.")
        await _press_enter_to_continue()
        return
    # Для item_info нам нужен App (не AppContext) — это «игровой app_id».
    target_app = target_app_context.value[0]
    if isinstance(target_app, int):
        target_app = App(target_app)

    print(f"\n=== Cross-account bulk-list: «{name}» ===")
    print(f"   Всего свободных экземпляров (state=free): {len(candidates)}")
    print("   Hint: можно ввести 'i' чтобы открыть инфо по предмету "
          "(график + стаканы) перед выбором цены.")

    # Открыть item-info в валюте конкретного аккаунта. Если username=None,
    # берём первый из sessions или первого кандидата (обратная совместимость).
    async def _open_item_info(username: str | None = None):
        # Решаем, чей аккаунт использовать.
        if username is None:
            if sessions:
                username = next(iter(sessions.keys()))
            else:
                username = candidates[0]["username"]
        # Сессия.
        if username in sessions:
            info_client = sessions[username][0]
        else:
            acc = accounts_lookup.get(username)
            if acc is None:
                print(f"   [ERR] нет метаданных для {username}.")
                return
            connected = await _connect_account(acc, force_relogin)
            if connected is None:
                print(f"   [ERR] login failed для {username}.")
                return
            sessions[username] = connected
            info_client = connected[0]
        # Валюта этого аккаунта.
        cur_code = _account_currency_code(username, sessions, accounts_lookup) or 0
        info_sym = ""
        if cur_code:
            try:
                info_sym = _currency_symbol(Currency, cur_code)
            except Exception:  # noqa: BLE001
                info_sym = ""
        who = _acc_display(username, label_lookup)
        print(f"   [item-info в валюте аккаунта {who}: {info_sym or '?'} "
              f"(code={cur_code or '?'})]")
        try:
            import item_info as _ii
            await _ii.show_item_info_menu(
                info_client, name, target_app, Currency, cur_code or 1,
                ask=_ask, currency_sym=info_sym,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"   [ERR] item-info упал: {exc!r}")

    # 1) Сколько штук выставить.
    while True:
        raw_n = (await _ask(
            f"   Сколько штук выставить? (1..{len(candidates)}, all=все, "
            f"i=инфо по предмету, q=отмена): "
        )).strip().lower()
        if raw_n in ("q", "b", ""):
            print("   Отменено.")
            return
        if raw_n == "i":
            await _open_item_info()
            continue
        break
    if raw_n == "all":
        n_target = len(candidates)
    else:
        try:
            n_target = int(raw_n)
        except ValueError:
            print(f"   «{raw_n}» — не число, отмена.")
            return
    if n_target <= 0 or n_target > len(candidates):
        print(f"   Допустимо 1..{len(candidates)}, отмена.")
        return

    # 2) min-float (только CS2; для не-CS2 пропускаем).
    min_float: float | None = None
    if app_context_str == "CS2":
        raw_f = (await _ask(
            "   Min float (выставлять только с float ≥ X; пусто = без фильтра): "
        )).strip().replace(",", ".")
        if raw_f:
            try:
                min_float = float(raw_f)
                if not 0.0 <= min_float <= 1.0:
                    print("   float должен быть 0..1. Отмена.")
                    return
            except ValueError:
                print(f"   «{raw_f}» — не число, отмена.")
                return

    # 3) Фильтрация по min_float и выборка top-N.
    filtered = candidates
    skipped_by_float = 0
    if min_float is not None:
        new_filtered = []
        for c in candidates:
            pw = c.get("paint_wear")
            if pw is not None and float(pw) < min_float:
                skipped_by_float += 1
                continue
            new_filtered.append(c)
        filtered = new_filtered
        if not filtered:
            print(f"   После min_float={min_float} ничего не осталось. Отмена.")
            return
        if n_target > len(filtered):
            print(f"   После фильтра доступно только {len(filtered)} экз. "
                  f"(вместо запрошенных {n_target}). Скорректировано.")
            n_target = len(filtered)

    # Сортируем: сначала по username для стабильности, и берём top-N.
    # Для CS2 с min_float — сначала с худшим (наименьшим) float'ом, чтобы
    # «лучшие» (с большим float) оставались на потом.
    def _sort_key(c):
        u = c["username"]
        raw = label_lookup.get(u, "")
        label_n = int(raw) if raw and raw.isdigit() else 999999
        if app_context_str == "CS2" and min_float is not None:
            pw = c.get("paint_wear")
            return (float(pw) if pw is not None else 9.0, label_n, u)
        return (label_n, u)

    filtered.sort(key=_sort_key)
    to_list = filtered[:n_target]

    # 3.5) Проверка на брелки и редкие паттерны — та же, что в одиночном sell-
    #      flow (см. место с `patterns.is_charm` и `patterns.is_rare_pattern`).
    #      Раньше тут проверки не было — массовые выставления могли уехать в
    #      Steam с редким паттерном без предупреждения. Теперь делим to_list на
    #      «безопасные», «точно редкие» и «uncertain» (база содержит этот скин,
    #      но конкретных номеров seed нет) — и просим юзера решить.
    import patterns  # noqa: PLC0415  (локальный импорт — стиль файла)

    if patterns.is_charm(name):
        # У брелков seed «летает», централизованной таблицы тиров нет, поэтому
        # такие предметы в массовом выставлении исторически блокируются.
        print(
            f"   ⚠  «{name}» — это брелок. У всех брелоков есть редкие паттерны,\n"
            f"      и базы тиров нет. Выставляй вручную через `e <N>` → `s <N>`."
        )
        if not await _ask_yes_no("   Всё равно выставить массово?"):
            print("   Отменено.")
            return

    rare_items: list[tuple[dict, str]] = []     # (candidate, tier)
    uncertain_items: list[dict] = []
    for c in to_list:
        seed = c.get("paint_seed")
        if seed is None:
            # Не CS2 / brelock — skip pattern-check.
            continue
        res = patterns.is_rare_pattern(name, int(seed))
        if res.is_rare is True:
            rare_items.append((c, res.tier_note or "?"))
        elif res.is_rare is None:
            uncertain_items.append(c)

    if rare_items or uncertain_items:
        print("\n   ⚠  В выбранной партии найдены предметы с редкими паттернами:")
        if rare_items:
            print(f"   {len(rare_items)} точно редких (есть в 7patterns.json):")
            for c, tier in rare_items:
                who = _acc_display(c["username"], label_lookup)
                pw = c.get("paint_wear")
                pw_str = f"float={float(pw):.4f}" if pw is not None else "float=?"
                print(
                    f"     • seed={c.get('paint_seed')} ({tier}), {pw_str}, "
                    f"acc={who}, asset_id={c.get('asset_id', '?')}"
                )
        if uncertain_items:
            print(
                f"   {len(uncertain_items)} «возможно редкий» "
                f"(база `data/7patterns.txt` знает о редких паттернах\n"
                f"     у этого скина, но точные номера в `7patterns.json` "
                f"не вписаны — на всякий случай ставим вопрос):"
            )
            for c in uncertain_items:
                who = _acc_display(c["username"], label_lookup)
                pw = c.get("paint_wear")
                pw_str = f"float={float(pw):.4f}" if pw is not None else "float=?"
                print(
                    f"     • seed={c.get('paint_seed')}, {pw_str}, "
                    f"acc={who}, asset_id={c.get('asset_id', '?')}"
                )
        print(
            "\n   Варианты:\n"
            "     y      — выставлять ВСЁ (включая редкие/uncertain) по общей цене\n"
            "     skip   — пропустить редкие/uncertain, выставить только безопасные\n"
            "     n      — отменить всю операцию"
        )
        raw_p = (await _ask("   Выбор [y/skip/n]: ")).strip().lower()
        if raw_p in ("n", "no", "q", ""):
            print("   Отменено.")
            return
        if raw_p in ("skip", "s"):
            risky_ids = {id(c) for c, _ in rare_items} | {id(c) for c in uncertain_items}
            to_list = [c for c in to_list if id(c) not in risky_ids]
            n_skipped = len(rare_items) + len(uncertain_items)
            print(f"   [skip] пропущено {n_skipped} предметов с редкими/uncertain паттернами.")
            if not to_list:
                print("   После пропуска ничего не осталось. Отмена.")
                return
            n_target = len(to_list)
        elif raw_p in ("y", "yes"):
            print("   [!] Выставляем ВСЁ, включая редкие. Ответственность на тебе.")
        else:
            print(f"   Не понял «{raw_p}», отменяю.")
            return

    # 4) Группируем по валюте аккаунта. Если валюты разные — будет отдельный
    #    промпт цены по каждой валюте (без этого Steam поставил бы «1.99 RUB» и «1.99 USD»
    #    как несопоставимые цены — «выставит очень неправильно»).
    by_currency: dict[int | None, list[dict]] = {}
    for c in to_list:
        cur_code = _account_currency_code(c["username"], sessions, accounts_lookup)
        by_currency.setdefault(cur_code, []).append(c)

    multi_currency = len(by_currency) > 1
    if multi_currency:
        print(f"\n   ⚠  У аккаунтов {len(by_currency)} разных валют — цену спрошу по каждой отдельно:")
        for cur_code, group in by_currency.items():
            try:
                cur_sym = _currency_symbol(Currency, cur_code) if cur_code else "?"
            except Exception:  # noqa: BLE001
                cur_sym = "?"
            accs_n = len({c["username"] for c in group})
            print(f"     • {cur_sym} (code={cur_code or '?'}): {len(group)} экз. с {accs_n} акк.")
        if not await _ask_yes_no("   Продолжить?"):
            print("   Отменено.")
            return

    if min_float is not None:
        print(f"   Min float: {min_float}  (отфильтровано {skipped_by_float} экз.)")

    # 5) Для каждой валюты: спросить цену и выставить эту группу.
    ok = 0
    fail = 0
    fatal_accs: set[str] = set()

    # Для стабильного порядка — сортируем валюты по currency_code (None в конец).
    def _cur_sort_key(item):
        cc, _ = item
        return (cc is None, cc or 0)

    for cur_code, group in sorted(by_currency.items(), key=_cur_sort_key):
        try:
            cur_sym = _currency_symbol(Currency, cur_code) if cur_code else ""
        except Exception:  # noqa: BLE001
            cur_sym = ""
        any_username_for_currency = group[0]["username"]
        if multi_currency:
            print(f"\n   ——— Валюта: {cur_sym or '?'} (code={cur_code or '?'}) ———")

        # per_item_prices: если юзер выбрал авто-цену в режиме Path B, каждому
        # asset_id своя цена. При None — используем общую `price_cents`.
        per_item_prices: dict[str, int] | None = None

        # Промпт цены. 'i' — открывает item-info ИМЕННО в валюте группы.
        # 'a' — авто-подбор для всей группы.
        while True:
            raw_price = (await _ask(
                f"   Цена покупателя за штуку в {cur_sym or 'валюте акков этой группы'} "
                f"(например 1.99; i=инфо/график, a=авто-цена, "
                f"q=пропустить эту валюту): "
            )).strip().lower().replace(",", ".")
            if raw_price in ("q", "b"):
                if not multi_currency:
                    print("   Отменено.")
                    return
                print("   [SKIP] валюта пропущена, идём дальше.")
                raw_price = ""
                break
            if raw_price == "":
                if not multi_currency:
                    print("   Отменено.")
                    return
                # В мультивалютном случае пусто тоже = skip валюту.
                print("   [SKIP] валюта пропущена.")
                break
            if raw_price == "i":
                await _open_item_info(any_username_for_currency)
                continue
            if raw_price == "a":
                # Берём сессию клиента группы — там есть нужная валюта.
                if any_username_for_currency in sessions:
                    auto_client = sessions[any_username_for_currency][0]
                else:
                    acc = accounts_lookup.get(any_username_for_currency)
                    auto_client = None
                    if acc is not None:
                        connected = await _connect_account(acc, force_relogin)
                        if connected is not None:
                            sessions[any_username_for_currency] = connected
                            auto_client = connected[0]
                if auto_client is None:
                    print("   [!] auto-price: не смог получить клиента группы.")
                    continue
                outcome = await _auto_price_group(
                    auto_client, Currency, cur_code or 0,
                    name=name, group=group, label_lookup=label_lookup,
                    cur_sym=cur_sym,
                )
                if outcome is None:
                    # Юзер отменил подбор — снова спрашиваем цену.
                    continue
                kind, payload = outcome
                if kind == "skip":
                    raw_price = ""
                    break
                if kind == "uniform":
                    price_cents = payload
                    raw_price = f"auto:{price_cents / 100:.2f}"
                    break
                if kind == "per_item":
                    per_item_prices = payload
                    price_cents = 0  # не используется, перебивается per_item
                    raw_price = "auto:per_item"
                    break
                # Неизвестный исход — лупимся снова.
                continue
            try:
                amount = float(raw_price)
            except ValueError:
                print(f"   «{raw_price}» — не число.")
                continue
            if amount <= 0:
                print("   Цена должна быть > 0.")
                continue
            price_cents = int(round(amount * 100))
            if price_cents <= 0:
                print("   Цена слишком мала.")
                continue
            break
        if not raw_price:
            # Скип этой валюты — посчитаем как fail.
            fail += len(group)
            continue

        # Сводка для этой валютной группы.
        from collections import Counter
        by_acc = Counter(c["username"] for c in group)
        summary_accs = ", ".join(
            f"{_acc_display(u, label_lookup)}×{q}" for u, q in by_acc.most_common()
        )
        if per_item_prices is None:
            price_str = f"{price_cents / 100:.2f} {cur_sym}".strip()
            print(
                f"\n   ВЫСТАВИТЬ ({cur_sym or 'val'}): {len(group)} экз. "
                f"«{name}» по {price_str}"
            )
        else:
            min_p = min(per_item_prices.values())
            max_p = max(per_item_prices.values())
            print(
                f"\n   ВЫСТАВИТЬ ({cur_sym or 'val'}): {len(group)} экз. "
                f"«{name}» по индивидуальным ценам "
                f"({min_p/100:.2f}…{max_p/100:.2f} {cur_sym})"
            )
        print(f"   С аккаунтов: {summary_accs}")
        if not await _ask_yes_no("   Подтвердить эту валютную группу?"):
            print("   [SKIP] валюта пропущена, ничего не выставлено.")
            fail += len(group)
            continue

        # Выставляем эту валютную группу по аккаунтам.
        by_user: dict[str, list[dict]] = {}
        for c in group:
            by_user.setdefault(c["username"], []).append(c)

        for u_idx, (username, items_for_user) in enumerate(by_user.items(), 1):
            who = _acc_display(username, label_lookup)
            print(f"\n   [{u_idx}/{len(by_user)}] {who}: выставляю {len(items_for_user)} экз. ...")
            # Сессия.
            if username in sessions:
                client, _cur, _cf = sessions[username]
            else:
                acc = accounts_lookup.get(username)
                if acc is None:
                    print(f"      [SKIP] нет метаданных аккаунта (нет в loaded accounts).")
                    fail += len(items_for_user)
                    continue
                connected = await _connect_account(acc, force_relogin)
                if connected is None:
                    print(f"      [SKIP] login failed.")
                    fail += len(items_for_user)
                    continue
                client, _cur, _cf = connected
                sessions[username] = connected

            # Перед выставлением — чистим awaiting confirmation у ЭТОГО акка,
            # чтобы не было дубликатов от прошлых обрывов.
            await _cancel_all_pending_confirmations(
                client, label=f"({who}, pre-cross-bulk)"
            )

            # Выставляем по одному.
            for i, item_row in enumerate(items_for_user, 1):
                asset_id = int(item_row["asset_id"])
                # Если у нас per_item_prices (авто-Path B) — берём ОТТУДА цену.
                if per_item_prices is not None:
                    this_price = per_item_prices.get(str(asset_id), price_cents)
                else:
                    this_price = price_cents
                try:
                    listing_id = await _place_sell_listing_with_retry(
                        client, asset_id, target_app_context, price=this_price,
                        what=f"place_sell_listing #{i} (asset={asset_id}, user={username})",
                    )
                    ok += 1
                    # F2: пишем новый листинг в listings_cache сразу.
                    if listing_id is not None:
                        try:
                            import cache as _cache_ins

                            _cache_ins.insert_placed_listing(
                                username,
                                listing_id,
                                unowned_id=str(asset_id),
                                market_hash_name=name,
                                price_cents=int(this_price),
                                currency_code=int(cur_code) if cur_code is not None else None,
                            )
                        except Exception as exc_cache:  # noqa: BLE001
                            print(f"      [!] кеш записи листинга упал: {exc_cache!r}")
                except Exception as exc:  # noqa: BLE001
                    fail += 1
                    err = steam_errors.classify_steam_error(exc)
                    prefix = f"#{i} asset={asset_id}"
                    print("      " + steam_errors.format_for_log(err, prefix=prefix))
                    if err.fatal_for_batch:
                        remaining = len(items_for_user) - i
                        if remaining > 0:
                            print(
                                f"      [stop:{who}] прерываю этот акк: {remaining} пропущено. "
                                f"Продолжаю со следующего акка."
                            )
                        fatal_accs.add(username)
                        fail += remaining
                        break
                await asyncio.sleep(0.4)
            # Между аккаунтами пауза.
            await asyncio.sleep(0.5)

    print("\n" + "=" * 60)
    print(f"   ИТОГ: {ok} успешно / {fail} ошибок/пропущено")
    if fatal_accs:
        names = ", ".join(_acc_display(u, label_lookup) for u in fatal_accs)
        print(f"   Аккаунты с fatal-ошибкой (например max-wallet): {names}")
    print("=" * 60)
    await _press_enter_to_continue()


async def _bulk_cancel_cross_account(  # noqa: PLR0912, PLR0915, C901
    *,
    name: str,
    listed_rows: list[dict],
    accounts_lookup: dict[str, dict],
    sessions: dict,
    force_relogin: bool,
    label_lookup: dict[str, str],
    preselected: bool = False,
    title_suffix: str = "",
) -> None:
    """Снимает с продажи N экземпляров `name` с разных аккаунтов.

    `listed_rows` — все строки inventory_cache для этого скина у state='on_market'.
    Для каждого экземпляра резолвим listing_id из listings_cache по asset_id
    (cache.find_listing_by_asset_id) — и вызываем `client.cancel_sell_listing(...)`.

    Если `preselected=True` — пользователь уже выбрал конкретный лот (например,
    «лот 1: 1.99₽ × 5 шт.»), и спрашивать «сколько снять?» не нужно — снимаем
    все переданные `listed_rows`. `title_suffix` — дописка к заголовку для контекста
    (например, «лот 1.99 ₽»).

    Валюта здесь не важна (отмена листинга не требует цены).
    """
    header = f"\n=== Cross-account bulk-cancel: «{name}»"
    if title_suffix:
        header += f" — {title_suffix}"
    print(header + " ===")
    print(f"   Выставлено на маркете (передано на снятие): {len(listed_rows)}")

    if preselected:
        # Лот выбран заранее — никакого «сколько», снимаем всё.
        n_target = len(listed_rows)
    else:
        # 1) Сколько снять.
        while True:
            raw_n = (await _ask(
                f"   Сколько снять с продажи? (1..{len(listed_rows)}, all=все, q=отмена): "
            )).strip().lower()
            if raw_n in ("q", "b", ""):
                print("   Отменено.")
                return
            if raw_n == "all":
                n_target = len(listed_rows)
            else:
                try:
                    n_target = int(raw_n)
                except ValueError:
                    print(f"   «{raw_n}» — не число.")
                    continue
            if n_target <= 0 or n_target > len(listed_rows):
                print(f"   Допустимо 1..{len(listed_rows)}.")
                continue
            break

    # 2) Сортировка для стабильности (по label_num аккаунта).
    def _sort_key(r):
        u = r["username"]
        raw = label_lookup.get(u, "")
        label_n = int(raw) if raw and raw.isdigit() else 999999
        return (label_n, u, str(r.get("asset_id") or ""))

    listed_rows = sorted(listed_rows, key=_sort_key)
    to_cancel = listed_rows[:n_target]

    # 3) Подтверждение.
    from collections import Counter
    by_acc = Counter(c["username"] for c in to_cancel)
    summary_accs = ", ".join(
        f"{_acc_display(u, label_lookup)}×{q}" for u, q in by_acc.most_common()
    )
    print(f"\n   СНЯТЬ С ПРОДАЖИ: {len(to_cancel)} экз. «{name}»")
    print(f"   С аккаунтов: {summary_accs}")
    print("   (предметы вернутся в инвентарь, можно будет выставить по новой).")
    if not await _ask_yes_no("   Подтвердить?"):
        print("   Отменено.")
        return

    # 4) Снятие по аккаунтам.
    import cache as _cache
    ok = 0
    fail = 0
    not_found = 0
    by_user: dict[str, list[dict]] = {}
    for c in to_cancel:
        by_user.setdefault(c["username"], []).append(c)

    for u_idx, (username, items_for_user) in enumerate(by_user.items(), 1):
        who = _acc_display(username, label_lookup)
        print(f"\n   [{u_idx}/{len(by_user)}] {who}: снимаю {len(items_for_user)} экз. ...")
        # Сессия.
        if username in sessions:
            client, _cur, _cf = sessions[username]
        else:
            acc = accounts_lookup.get(username)
            if acc is None:
                print(f"      [SKIP] нет метаданных аккаунта.")
                fail += len(items_for_user)
                continue
            connected = await _connect_account(acc, force_relogin)
            if connected is None:
                print(f"      [SKIP] login failed.")
                fail += len(items_for_user)
                continue
            client, _cur, _cf = connected
            sessions[username] = connected

        # Попредметно: резолвим listing_id по asset_id, снимаем.
        for i, item_row in enumerate(items_for_user, 1):
            asset_id = item_row.get("asset_id")
            listing_id = _cache.find_listing_by_asset_id(username, asset_id)
            if not listing_id:
                not_found += 1
                fail += 1
                print(f"      #{i} asset={asset_id}: [SKIP] листинг не найден в listings_cache. "
                      "Сделай sweep (обнови листинги) и попробуй снова.")
                continue

            async def _do_cancel(lid=listing_id):
                cm = client.cancel_sell_listing(lid)
                async with cm as resp:
                    return resp.status

            try:
                status = await _with_retry(
                    _do_cancel, what=f"cancel_sell_listing #{i} (lid={listing_id}, user={username})"
                )
            except Exception as exc:  # noqa: BLE001
                err = steam_errors.classify_steam_error(exc)
                prefix = f"#{i} asset={asset_id} lid={listing_id}"
                print("      " + steam_errors.format_for_log(err, prefix=prefix))
                fail += 1
                await asyncio.sleep(0.4)
                continue

            if status is None or status < 400:
                ok += 1
                try:
                    _cache.delete_listing(username, listing_id)
                    # F5: предмет вернётся в инвентарь как «free» — пометим в кеше
                    # сразу, чтобы пользователь не видел его как `on_market`.
                    if asset_id is not None:
                        _cache.mark_inventory_state_by_asset_id(
                            username, asset_id, "free"
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"      [!] не смог удалить из кеша: {exc!r}")
                print(f"      #{i} asset={asset_id} lid={listing_id}: [OK]")
            else:
                fail += 1
                print(f"      #{i} lid={listing_id}: Steam ответил status={status}.")
            await asyncio.sleep(0.4)
        # Между аккаунтами пауза.
        await asyncio.sleep(0.5)

    print("\n" + "=" * 60)
    print(f"   ИТОГ: {ok} снято / {fail} ошибок")
    if not_found:
        print(f"   Не найдено listing_id в кеше: {not_found}. "
              "Запусти sweep чтобы обновить listings_cache.")
    print("=" * 60)
    await _press_enter_to_continue()


async def _pick_account(
    accounts: list[dict],
    *,
    sweep_callback=None,
    stats_callback=None,
    history_callback=None,
    autotrade_callback=None,
    collect_listings_callback=None,
) -> dict | None:
    """Меню выбора аккаунта.

    Если у всех аккаунтов label — это уникальные числа, в меню используется
    их label как номер (так удобнее с 100 аккаунтами: вводишь номер из тетрадки).
    Иначе — обычная порядковая нумерация 1..N.

    Callback'и (фаза 3 + задачи 6/2):
      sweep_callback   — `r` — refresh всех (sweep);
      stats_callback   — `g` — глобальная статистика по инвентарям;
      history_callback — `h` — общая история сделок маркета (cross-account);
      autotrade_callback — `t` — авто-принятие пустых трейдов (фон);
      collect_listings_callback — `L` — однократный сбор всех sell-листингов
        по всем акк-ам (опционально через прокси).
    """
    if len(accounts) == 1:
        return accounts[0]
    accounts = _sort_accounts(accounts)
    # Решаем — использовать label как ключ выбора или обычные 1..N
    label_nums = [_label_num(a) for a in accounts]
    by_label = all(n is not None for n in label_nums) and len(set(label_nums)) == len(label_nums)

    while True:
        # Множество username'ов под автопринятием прямо сейчас — чтобы поставить [auto].
        autotrade_running = (
            _AUTOTRADE_TASK is not None and not _AUTOTRADE_TASK.done()
        )
        auto_set: set[str] = (
            set(_AUTOTRADE_STATE.get("usernames") or [])
            if autotrade_running else set()
        )
        print("\n" + "=" * 50)
        print("   Выбери аккаунт")
        print("=" * 50)
        for i, acc in enumerate(accounts, 1):
            key = str(_label_num(acc)) if by_label else str(i)
            marker = (
                f" ({acc['label']})" if not by_label and acc["label"] != acc["username"] else ""
            )
            auto_marker = "  [auto]" if acc["username"] in auto_set else ""
            print(f"  {key}) {acc['username']}{marker}{auto_marker}")
        print("  s) Сводка по всем аккаунтам (из кеша)")
        if sweep_callback is not None:
            print("  r) Refresh всех (sweep: balance + orders + инвентари + дельта истории)")
        if stats_callback is not None:
            print("  g) Глобальная статистика по инвентарям (cross-account)")
        if history_callback is not None:
            print("  h) История маркета — все аккаунты (cross-account)")
        if autotrade_callback is not None:
            print("  t) Авто-принятие пустых трейдов в фоне")
        if collect_listings_callback is not None:
            print("  L) Собрать ВСЕ sell-листинги по всем акк-ам "
                  "(одноразово, опционально через прокси)")
        print("  0) Выход")
        raw = (await _ask("\nВыбор: ")).strip()
        if raw == "0":
            return None
        if raw.lower() == "s":
            await _show_cache_summary()
            continue
        if raw.lower() == "r" and sweep_callback is not None:
            try:
                await sweep_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] sweep упал: {exc!r}")
                traceback.print_exc()
            continue
        if raw.lower() == "g" and stats_callback is not None:
            try:
                await stats_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] stats упало: {exc!r}")
                traceback.print_exc()
            continue
        if raw.lower() == "h" and history_callback is not None:
            try:
                await history_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] history упало: {exc!r}")
                traceback.print_exc()
            continue
        if raw.lower() == "t" and autotrade_callback is not None:
            try:
                await autotrade_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] autotrade упало: {exc!r}")
                traceback.print_exc()
            continue
        if raw == "L" and collect_listings_callback is not None:
            try:
                await collect_listings_callback()
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] collect listings упало: {exc!r}")
                traceback.print_exc()
            continue
        try:
            idx = int(raw)
        except ValueError:
            print("Не понял — введи цифру.")
            continue
        if by_label:
            for acc in accounts:
                if _label_num(acc) == idx:
                    return acc
            print(f"Аккаунта с label={idx} нет в списке.")
            continue
        if 1 <= idx <= len(accounts):
            return accounts[idx - 1]
        print(f"Номер вне диапазона (1..{len(accounts)}).")


# =============================================================================
# Главный цикл
# =============================================================================
async def _run() -> int:
    _load_dotenv_if_present()
    force_relogin = bool(os.getenv("FORCE_RELOGIN")) or FORCE_RELOGIN
    print(f"[..] Python {sys.version.split()[0]}")

    # Собираем все доступные аккаунты: сначала из accounts/, потом legacy single.
    accounts = _discover_accounts()
    if not accounts:
        legacy = _legacy_account()
        if legacy is not None:
            accounts = [legacy]
    if not accounts:
        print(
            "[ERROR] Не нашёл ни одного аккаунта. Варианты:\n"
            "   А) Создай папку `accounts/<имя>/` рядом со скриптом, положи в неё "
            'Steam.maFile и account.json {"username":..., "password":...}.\n'
            "   Б) Заполни STEAM_PASSWORD/MAFILE_PATH в шапке simple.py "
            "(или через .env-файл).",
            file=sys.stderr,
        )
        return 1

    # Сессии разных аккаунтов держим открытыми, чтобы не релогиниться при переключении.
    sessions: dict[str, tuple] = {}  # username → (client, currency_code, cookies_file)

    async def _close_all_sessions() -> None:
        # Сначала останавливаем фоновую автоторговлю (задача 2), чтобы она
        # не пыталась дёргать сессии после их закрытия.
        global _AUTOTRADE_TASK
        if _AUTOTRADE_TASK is not None and not _AUTOTRADE_TASK.done():
            _AUTOTRADE_TASK.cancel()
            try:
                await _AUTOTRADE_TASK
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _AUTOTRADE_TASK = None
        for _, (cl, _cc, cf) in list(sessions.items()):
            try:
                cl.session.cookie_jar.save(cf)
            except Exception:  # noqa: BLE001
                pass
            try:
                await cl.session.close()
            except Exception:  # noqa: BLE001
                pass
        sessions.clear()

    try:
        while True:
            account = await _pick_account(
                accounts,
                sweep_callback=lambda: _run_sweep(accounts, sessions, force_relogin),
                stats_callback=lambda: _show_global_stats(
                    accounts, sessions, force_relogin
                ),
                history_callback=lambda: _show_global_market_history(accounts),
                autotrade_callback=lambda: _start_autotrade(
                    accounts, sessions, force_relogin
                ),
                collect_listings_callback=lambda: _collect_all_listings(
                    accounts, sessions, force_relogin
                ),
            )
            if account is None:
                print("Bye!")
                return 0
            username = account["username"]
            if username in sessions:
                client, currency_code, cookies_file = sessions[username]
            else:
                connected = await _connect_account(account, force_relogin)
                if connected is None:
                    print(f"[!] Не смог подключить {username}, выберите другой аккаунт.")
                    await _press_enter_to_continue()
                    continue
                client, currency_code, cookies_file = connected
                sessions[username] = (client, currency_code, cookies_file)

            global _CURRENT_CLIENT
            _CURRENT_CLIENT = client
            from aiosteampy.constants import Currency

            switch_acc = await _account_menu(
                account, client, Currency, currency_code, cookies_file, multi=len(accounts) > 1
            )
            if switch_acc == "exit":
                print("Bye!")
                return 0
            # иначе — крутим внешний цикл, чтобы выбрать другой аккаунт.
    finally:
        await _close_all_sessions()


async def _account_menu(
    account: dict,
    client,
    Currency,
    currency_code: int,
    cookies_file: Path,
    *,
    multi: bool,
) -> str:
    """Меню для одного аккаунта. Возвращает 'switch' (вернуться к выбору акка) или 'exit'.

    Шапка показывает баланс + on-hold из кеша (`wallet_snapshots`). Если в кеше
    ещё нет данных — выводится подсказка «нажми 1) для свежего значения».
    Пункт «1) Баланс кошелька» убран — баланс показывается прямо в шапке
    (фаза 3 F5). Старые номера 2..9 не меняются — чтобы привычка не ломалась.
    """
    import cache as _cache
    username = account["username"]
    # Цифровой label (если есть) для отображения «80 (login)».
    label_n = _label_num(account)
    if label_n is not None:
        who_header = f"{label_n} ({username})"
    else:
        who_header = username
    while True:
        # Свежим вытащим баланс/on_hold из кеша.
        bal = _cache.get_latest_balance(username)
        if bal:
            cur_code = bal.get("currency_code") or currency_code
            sym = _currency_symbol(Currency, cur_code) if cur_code else ""
            bal_str = f"{(bal.get('balance_cents') or 0) / 100:.2f} {sym}"
            hold_cents = bal.get("on_hold_cents") or 0
            if hold_cents:
                bal_str += f" (+ {hold_cents / 100:.2f} в холде)"
            seen = (bal.get("snapshot_at") or "").split("T")[0]
            bal_str += f"  ·  обновлено: {seen or '—'}"
        else:
            bal_str = "(нет в кеше — пройди sweep 'r' либо зайди в 4) Историю)"

        print("\n" + "=" * 60)
        print(f"   Steam-кабинет: {who_header}")
        print(f"     Баланс: {bal_str}")
        print("=" * 60)
        print("  2) Buy-ордера (заявки на покупку)")
        print("  3) Sell-листинги (выставлено на продажу) — снять с продажи: c <N>")
        print("  4) История маркета")
        print("  5) Инвентари (Steam / CS2 / Dota 2 / TF 2) — выставить на продажу: s <N>")
        if multi:
            print("  9) Сменить аккаунт")
        print("  0) Выход")
        choice = await _ask("\nВыбор: ")

        if choice == "1":
            # Пункт 1 убран — баланс теперь в шапке. Мягко напомним.
            print("  (баланс теперь в шапке, обновляется через sweep 'r')")
            await _press_enter_to_continue()
        elif choice == "2":
            try:
                await menu_buy_orders(client, Currency, currency_code)
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {exc!r}")
                traceback.print_exc()
            await _press_enter_to_continue()
        elif choice == "3":
            try:
                await menu_sell_listings(client, Currency, currency_code)
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {exc!r}")
                traceback.print_exc()
            await _press_enter_to_continue()
        elif choice == "4":
            try:
                await menu_market_history(client, Currency, currency_code)
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {exc!r}")
                traceback.print_exc()
            await _press_enter_to_continue()
        elif choice == "5":
            await menu_inventory(client, Currency, currency_code)
        elif choice == "9" and multi:
            try:
                client.session.cookie_jar.save(cookies_file)
            except Exception:  # noqa: BLE001
                pass
            return "switch"
        elif choice == "0":
            try:
                client.session.cookie_jar.save(cookies_file)
            except Exception:  # noqa: BLE001
                pass
            return "exit"
        else:
            print("Не понял — введи 2..5 / 0" + (" / 9" if multi else ""))


def main() -> int:
    # На Windows стандартный Proactor event loop иногда конфликтует с aiohttp/SSL —
    # переключаемся на Selector, который ведёт себя стабильнее.
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())