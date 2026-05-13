"""Классификатор и красивый форматтер ошибок Steam.

Голый `print(f"... {exc!r}")` для исключений из `aiosteampy` (EResultError,
ClientResponseError и пр.) выдаёт полотно текста, которое неприятно читать
в bulk-операциях. Этот модуль превращает такой `exc` в короткое
структурированное сообщение в стиле:

    Макс. баланс кошелька превышен: 18 971,18 / 19 000,00 NOK. Хедрум: 28,82.

Использование (минимальный пример):

    from steam_errors import classify_steam_error, format_for_log

    try:
        await client.place_sell_listing(item, price=cents, confirm=True)
    except Exception as exc:  # noqa: BLE001
        err = classify_steam_error(exc)
        print(format_for_log(err, prefix=f"#{i} {name} (asset_id={item.asset_id})"))
        if err.fatal_for_batch:
            break  # max wallet → дальше выставлять бессмысленно
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


# Категории ошибок. Используются вызывающим кодом, чтобы решать тактику
# (например, прерывать ли bulk-операцию).
CAT_MAX_WALLET = "max_wallet"  # баланс + ордера + цена > max wallet
CAT_RATE_LIMITED = "rate_limited"  # 429 от Steam
CAT_NEED_MOBILE_CONFIRM = "need_mobile_confirm"  # надо подтвердить в мобильном гварде
CAT_ITEM_UNAVAILABLE = "item_unavailable"  # предмета нет в инвентаре / trade-protect
CAT_PRICE_TOO_LOW = "price_too_low"
CAT_PRICE_TOO_HIGH = "price_too_high"
CAT_SESSION_EXPIRED = "session_expired"  # cookies протухли
CAT_NOT_LOGGED_IN = "not_logged_in"  # вообще не залогинились
CAT_NETWORK = "network"  # connection / DNS / timeout
CAT_TRANSIENT_LISTING = "transient_listing"  # Steam-эпизод: "There was a problem listing your item"
CAT_UNKNOWN = "unknown"


@dataclass
class SteamError:
    """Структурированная ошибка Steam.

    Attributes:
        category: один из CAT_* выше.
        short: короткая локализованная строка для глаза (без лишних деталей).
        details: словарь с распарсенными значениями (для логов / дальнейшей логики).
            Для CAT_MAX_WALLET содержит:
                'balance' / 'outstanding' / 'max_wallet'  — Decimal
                'currency_symbol'                          — str (напр. 'kr', 'NOK', '$')
                'headroom'                                 — Decimal (max − balance − outstanding)
        fatal_for_batch: если True, дальше выставлять/слать в bulk бесполезно
            (max wallet, session expired) — вызывающий код должен прервать цикл.
        retryable: если True, есть смысл повторить попытку позже (429, network).
        raw: исходное исключение (для traceback'а / отладки).
    """

    category: str
    short: str
    details: dict[str, Any] = field(default_factory=dict)
    fatal_for_batch: bool = False
    retryable: bool = False
    raw: BaseException | None = None


# =============================================================================
# Парсинг чисел в любом локальном формате
# =============================================================================
def _parse_localized_number(raw: str) -> Decimal | None:
    """Парсит число в любом формате: '18.971,18', '1,234.56', '50', '0,00'.

    Правило: если в строке есть и `.` и `,` — последний из них считается
    десятичным разделителем, остальное игнорируем. Если только один — он
    десятичный (для двухзначных хвостов) или разделитель тысяч (если >= 4 цифр
    после него — но это редкость, для надёжности всегда считаем десятичным).
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("\u00a0", "").replace(" ", "")
    # Срежем всё что не цифра / `.` / `,` / `-` (валютные символы и т.п.)
    cleaned = re.sub(r"[^\d.,\-]", "", cleaned)
    if not cleaned:
        return None
    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # оба разделителя: последний — десятичный, остальной — тысячный
        if last_dot > last_comma:
            normalized = cleaned.replace(",", "")
        else:
            normalized = cleaned.replace(".", "").replace(",", ".")
    elif last_comma >= 0:
        # только запятая → десятичная (Steam в EU так и пишет: "0,00")
        normalized = cleaned.replace(",", ".")
    else:
        # только точка или ничего — оставляем
        normalized = cleaned
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


# =============================================================================
# Извлечение валютного суффикса из «18.971,18 kr»
# =============================================================================
_AMOUNT_TOKEN_RE = re.compile(r"[\d][\d.,\u00a0\s]*[\d]")


def _split_amount_and_currency(piece: str) -> tuple[Decimal | None, str]:
    """Из «18.971,18 kr» / «$1,234.56» возвращает (Decimal, 'kr' | '$').

    Без знания всех локалей: ищем самый длинный число-подобный токен,
    остальное — валюта. Срезаем трейлинговую пунктуацию (точка,
    запятая в конце фразы).
    """
    piece = piece.strip().rstrip(".,;:")
    if not piece:
        return None, ""
    match = _AMOUNT_TOKEN_RE.search(piece)
    if not match:
        return _parse_localized_number(piece), ""
    amount = _parse_localized_number(match.group(0))
    currency = (piece[: match.start()] + piece[match.end() :]).strip()
    return amount, currency


def _fmt_decimal(d: Decimal | None) -> str:
    if d is None:
        return "?"
    # Печатаем с двумя знаками после точки и тонкими пробелами как тысячный
    # разделитель — стиль ближе к локалям, которые шлёт Steam. (Не критично.)
    sign = "-" if d < 0 else ""
    d = abs(d)
    s = f"{d:.2f}"
    int_part, _, frac_part = s.partition(".")
    if len(int_part) > 3:
        # вставим пробел каждые 3 цифры с конца
        rev = int_part[::-1]
        grouped = " ".join(rev[i : i + 3] for i in range(0, len(rev), 3))
        int_part = grouped[::-1]
    return f"{sign}{int_part},{frac_part}"


# =============================================================================
# Парсеры конкретных шаблонов Steam-сообщений
# =============================================================================
# «outstanding listings (0,00 kr) and your current Steam Wallet balance
#  of 18.971,18 kr exceed the maximum wallet balance of 19.000,00 kr.»
# `bal` и `max` могут быть «1.234,56 kr» (2 токена) или «$1,234.56» (1 токен),
# но всегда ≤ 2 пробельных токенов. Это даёт нам стабильный якорь без жадности.
_MAX_WALLET_RE = re.compile(
    r"outstanding listings\s*\(\s*(?P<out>[^)]+?)\s*\)\s*"
    r"and your current Steam Wallet balance of\s+(?P<bal>\S+(?:\s+\S+)?)\s+"
    r"exceed the maximum wallet balance of\s+(?P<max>\S+(?:\s+\S+)?)",
    re.IGNORECASE,
)

# «The price entered is too low. The minimum amount allowed is …»
_PRICE_LOW_RE = re.compile(
    r"price\s+(?:entered\s+)?(?:is|was)?\s*too\s+low", re.IGNORECASE
)
# «The price entered is too high.» / «maximum amount allowed is …»
_PRICE_HIGH_RE = re.compile(
    r"price\s+(?:entered\s+)?(?:is|was)?\s*too\s+high", re.IGNORECASE
)

# «Mobile Confirmation» / «needs to be confirmed»
_MOBILE_CONFIRM_RE = re.compile(
    r"(mobile\s+confirm|confirm\s+on\s+(?:your\s+)?mobile|needs?\s+to\s+be\s+confirmed)",
    re.IGNORECASE,
)

# «item is no longer in your inventory» / «item might not be in your inventory»
_ITEM_GONE_RE = re.compile(
    r"item\s+(?:is\s+)?(?:no\s+longer|might\s+not\s+be)\s+in\s+your\s+inventory",
    re.IGNORECASE,
)

# «not logged in» / «You must be logged in»
_NOT_LOGGED_IN_RE = re.compile(
    r"(must\s+be\s+logged\s+in|not\s+logged\s+in|please\s+log\s+in)", re.IGNORECASE
)

# «trade-protected» / «trade protection»
_TRADE_PROTECT_RE = re.compile(r"trade[-\s]?protect", re.IGNORECASE)

# «There was a problem listing your item. Refresh the page and try again.»
# Steam отдаёт это на временный сбой backend'а — листинг почти всегда проходит
# с ретрая через 2-3 секунды.
_TRANSIENT_LISTING_RE = re.compile(
    r"(?:problem\s+listing\s+your\s+item|refresh\s+the\s+page\s+and\s+try\s+again)",
    re.IGNORECASE,
)


# =============================================================================
# Главная функция: classify_steam_error
# =============================================================================
def classify_steam_error(exc: BaseException) -> SteamError:  # noqa: C901, PLR0911, PLR0912
    """Превращает произвольное исключение в `SteamError`.

    Безопасно для любых типов: если не распознали — возвращается `SteamError`
    с категорией `CAT_UNKNOWN` и `short` = краткий repr.
    """
    # --- aiohttp ClientResponseError / status -----------------------------
    status = getattr(exc, "status", None)
    if status == 429 or "429" in str(exc) or "Too Many Requests" in str(exc):
        return SteamError(
            category=CAT_RATE_LIMITED,
            short="Steam режет за частоту запросов (429). Подожди и попробуй снова.",
            details={"http_status": 429},
            retryable=True,
            raw=exc,
        )
    if status in (401, 403):
        return SteamError(
            category=CAT_SESSION_EXPIRED,
            short=f"Сессия протухла (HTTP {status}). Нужен перелогин.",
            details={"http_status": status},
            fatal_for_batch=True,
            raw=exc,
        )

    # --- aiosteampy EResultError ------------------------------------------
    msg = getattr(exc, "msg", None)
    if msg is None:
        # стандартный путь: EResultError.__str__ = f"{msg}; {result}"
        text = str(exc)
        # отрежем хвост "; <EResult...>" если есть
        text = re.sub(r";\s*<EResult\..*$", "", text).strip()
    else:
        text = str(msg).strip()

    # 1) Max wallet превышен — это самая частая «полезная» ошибка.
    if "exceed the maximum wallet balance" in text or "maximum wallet balance" in text:
        m = _MAX_WALLET_RE.search(text)
        if m:
            out_amt, _ = _split_amount_and_currency(m.group("out"))
            bal_amt, bal_cur = _split_amount_and_currency(m.group("bal"))
            max_amt, _ = _split_amount_and_currency(m.group("max"))
            headroom = None
            if max_amt is not None and bal_amt is not None:
                headroom = max_amt - bal_amt - (out_amt or Decimal(0))
            cur_short = bal_cur or ""
            details: dict[str, Any] = {
                "balance": bal_amt,
                "outstanding": out_amt,
                "max_wallet": max_amt,
                "headroom": headroom,
                "currency_symbol": cur_short,
            }
            parts = [
                f"Макс. баланс кошелька превышен: "
                f"{_fmt_decimal(bal_amt)} / {_fmt_decimal(max_amt)} {cur_short}".strip()
            ]
            if out_amt and out_amt > 0:
                parts.append(f"+ ордера {_fmt_decimal(out_amt)} {cur_short}".strip())
            if headroom is not None:
                parts.append(f"свободно: {_fmt_decimal(headroom)} {cur_short}".strip())
            return SteamError(
                category=CAT_MAX_WALLET,
                short=". ".join(parts) + ".",
                details=details,
                fatal_for_batch=True,
                raw=exc,
            )
        # Не распарсили числа — но категорию определили.
        return SteamError(
            category=CAT_MAX_WALLET,
            short="Макс. баланс кошелька превышен. Steam отказал в выставлении.",
            details={},
            fatal_for_batch=True,
            raw=exc,
        )

    # 2) Цена слишком низкая / высокая.
    if _PRICE_LOW_RE.search(text):
        return SteamError(
            category=CAT_PRICE_TOO_LOW,
            short="Цена слишком низкая — Steam отказал. Подними цену.",
            details={},
            raw=exc,
        )
    if _PRICE_HIGH_RE.search(text):
        return SteamError(
            category=CAT_PRICE_TOO_HIGH,
            short="Цена слишком высокая — Steam отказал. Сбрось цену.",
            details={},
            raw=exc,
        )

    # 3) Нужно мобильное подтверждение.
    if _MOBILE_CONFIRM_RE.search(text):
        return SteamError(
            category=CAT_NEED_MOBILE_CONFIRM,
            short="Нужно подтвердить в Steam Mobile Authenticator (проверь maFile).",
            details={},
            raw=exc,
        )

    # 4) Предмет недоступен (нет в инвентаре / trade-protect).
    if _ITEM_GONE_RE.search(text):
        return SteamError(
            category=CAT_ITEM_UNAVAILABLE,
            short="Предмета больше нет в инвентаре (продан/обменян/перенесён).",
            details={},
            raw=exc,
        )
    if _TRADE_PROTECT_RE.search(text):
        return SteamError(
            category=CAT_ITEM_UNAVAILABLE,
            short="Предмет под Trade Protection — продать через маркет нельзя.",
            details={},
            raw=exc,
        )

    # 4.5) Временный сбой Steam ("There was a problem listing your item. Refresh
    #      the page and try again.") — ретраить с задержкой, скорее всего пройдёт.
    if _TRANSIENT_LISTING_RE.search(text):
        return SteamError(
            category=CAT_TRANSIENT_LISTING,
            short="Steam: \"There was a problem listing your item\" (временно). Можно повторить.",
            details={},
            retryable=True,
            raw=exc,
        )

    # 5) Не залогинены.
    if _NOT_LOGGED_IN_RE.search(text):
        return SteamError(
            category=CAT_NOT_LOGGED_IN,
            short="Steam считает что мы не залогинены. Нужен перелогин.",
            details={},
            fatal_for_batch=True,
            raw=exc,
        )

    # 6) Сетевые проблемы.
    exc_name = type(exc).__name__
    if exc_name in {
        "ClientConnectionError",
        "ClientConnectorError",
        "ServerDisconnectedError",
        "TimeoutError",
        "asyncio.TimeoutError",
    } or "Connection" in exc_name:
        return SteamError(
            category=CAT_NETWORK,
            short=f"Сетевая ошибка ({exc_name}). Можно попробовать снова.",
            details={"exc_type": exc_name},
            retryable=True,
            raw=exc,
        )

    # 7) Неизвестно — оставляем компактный fallback.
    short = text[:200] + ("…" if len(text) > 200 else "")
    if not short.strip():
        short = f"{exc_name}: <без сообщения>"
    return SteamError(
        category=CAT_UNKNOWN,
        short=short,
        details={"exc_type": exc_name},
        raw=exc,
    )


# =============================================================================
# Форматирование для CLI (print)
# =============================================================================
def format_for_log(err: SteamError, *, prefix: str = "") -> str:
    """Возвращает одно-двустрочное человекочитаемое представление.

    `prefix` — что показать первой строкой (например, имя и asset_id предмета).
    Если короткий summary `err.short` помещается рядом с prefix'ом, печатаем
    одной строкой; иначе — двумя.
    """
    head = f"[!] {prefix}" if prefix else "[!]"
    one_line = f"{head}  {err.short}"
    # Если совсем коротко (≤ 100 символов) — одной строкой.
    if len(one_line) <= 100:
        return one_line
    return f"{head}\n    └ {err.short}"


def format_short(err: SteamError) -> str:
    """Только `short`, без префикса. Удобно для bulk-сводки в конце."""
    return err.short