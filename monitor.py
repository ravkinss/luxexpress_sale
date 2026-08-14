"""
Мониторинг цен на билеты Lux Express через официальный GraphQL API сайта.

Как это работает:
1. Playwright открывает главную страницу luxexpress.eu обычным headless-браузером —
   это нужно, чтобы пройти проверку Cloudflare и получить рабочую куку cf_clearance.
2. Дальше скрипт делает POST-запрос напрямую к /graphql (используя ту же куку)
   и получает список рейсов с ценами в чистом JSON — без парсинга HTML.
3. Сравнивает цены с прошлым запуском (price_history.json) и шлёт уведомление
   в Telegram, если что-то изменилось.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Настройка маршрутов для отслеживания
# ---------------------------------------------------------------------------
# from_stop_id / to_stop_id бери из адресной строки при поиске нужного
# маршрута на luxexpress.eu (параметры fromBusStopId / toBusStopId в URL)
ROUTES = [
    {
        "name": "Вильнюс → Варшава",
        "from_stop_id": 18862,
        "to_stop_id": 18925,
        "depart_date": "2026-09-04",
        "adults": 1,
    },
    # Добавляй новые маршруты сюда, например:
    # {
    #     "name": "Вильнюс → Рига",
    #     "from_stop_id": 18862,
    #     "to_stop_id": 12345,
    #     "depart_date": "2026-09-10",
    #     "adults": 1,
    # },
]

HOMEPAGE_URL = "https://luxexpress.eu/ru/"
GRAPHQL_URL = "https://luxexpress.eu/graphql"

# ---------------------------------------------------------------------------
# Настройка маршрутов Ecolines
# ---------------------------------------------------------------------------
# origin/destination — числовые ID городов из выпадающих списков на ecolines.by
# (видно в адресной строке как outwardOrigin=... / outwardDestination=...)
ECOLINES_ROUTES = [
    {
        "name": "[Ecolines] Минск → Варшава",
        "origin": 917,
        "destination": 100,
        "depart_date": "2026-09-04",
        "departure_times": ["09:15", "10:10"],
    },
    # Добавляй новые маршруты Ecolines сюда так же, с нужными departure_times
]

ECOLINES_SEARCH_URL = "https://booking.ecolines.by/search/result"
HISTORY_FILE = Path(__file__).parent / "price_history.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Минимальный набор полей — только то, что реально нужно для мониторинга цен.
# Полную версию запроса (со всеми полями рейса) можно найти в истории чата,
# но она не нужна для отслеживания цены и лишний вес только всё усложняет.
GRAPHQL_QUERY = """
query (
  $departureDate: Date!
  $originBusStopId: Int
  $destinationBusStopId: Int
  $lang: String
  $currency: String
  $fareClasses: [SearchFareClassInput]
  $promoCode: String
  $isPartOfRoundtrip: Boolean
  $onlyActive: Boolean
) {
  search(
    departureDate: $departureDate
    originBusStopId: $originBusStopId
    destinationBusStopId: $destinationBusStopId
    lang: $lang
    currency: $currency
    fareClasses: $fareClasses
    promoCode: $promoCode
    isPartOfRoundtrip: $isPartOfRoundtrip
    onlyActive: $onlyActive
  ) {
    JourneyId
    DepartureDateTime
    ArrivalDateTime
    OriginStopName
    DestinationStopName
    Currency
    RegularPrice
    BusinessClassPrice
    CampaignPrice
    IsForSale
    __typename
  }
}
"""


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы, пропускаю отправку.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    resp.raise_for_status()


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


async def fetch_route_journeys(request_ctx, route: dict):
    """Делает GraphQL-запрос к /graphql и возвращает список рейсов (data.search)."""
    search_page_url = (
        f"https://luxexpress.eu/ru/tickets/search/?promocode=&departDate={route['depart_date']}"
        f"&currency=EUR&fromBusStopId={route['from_stop_id']}&toBusStopId={route['to_stop_id']}"
        f"&adult={route['adults']}&senior=0&youth=0&pupil=0&child=0&affiliateId="
    )

    payload = {
        "variables": {
            "departureDate": route["depart_date"],
            "originBusStopId": route["from_stop_id"],
            "destinationBusStopId": route["to_stop_id"],
            "currency": "CURRENCY.EUR",
            "lang": "ru",
            "fareClasses": [{"Id": "BONUS_SCHEME_GROUP.ADULT", "Count": route["adults"]}],
            "promoCode": "",
            "isPartOfRoundtrip": False,
            "onlyActive": False,
        },
        "query": GRAPHQL_QUERY,
    }

    response = await request_ctx.post(
        GRAPHQL_URL,
        headers={
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://luxexpress.eu",
            "referer": search_page_url,
            "platform": "web-next",
        },
        data=json.dumps(payload),
    )

    if response.status != 200:
        raise RuntimeError(f"GraphQL вернул статус {response.status}: {await response.text()}")

    body = await response.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL вернул ошибки: {body['errors']}")

    return body.get("data", {}).get("search", [])


def extract_min_price(journeys: list):
    """Минимальная цена среди всех доступных для продажи рейсов (эконом или бизнес)."""
    prices = []
    for j in journeys:
        if not j.get("IsForSale"):
            continue
        for field in ("RegularPrice", "CampaignPrice", "BusinessClassPrice"):
            price = j.get(field)
            if price is not None:
                prices.append(float(price))
    return min(prices) if prices else None


async def fetch_ecolines_trip_prices(page, route: dict):
    """
    Открывает страницу поиска Ecolines и возвращает словарь
    {время_отправления: {"price": цена, "trip_id": id_рейса}} по всем прямым
    и с пересадкой рейсам, найденным на день.
    """
    url = (
        f"{ECOLINES_SEARCH_URL}?allowedCurrency=26&locale=by&currency=26"
        f"&outwardOrigin={route['origin']}&outwardDestination={route['destination']}"
        f"&outwardDate={route['depart_date']}"
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2000)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    trips = {}
    for card in soup.select("div.row.text-center"):
        times = card.select("h2.no-mp")
        btn = card.select_one("button.btn-primary.btn-lg[name=journey]")
        if len(times) >= 2 and btn:
            departure_time = times[0].get_text(strip=True)
            match = re.search(r"([\d.]+)\s*BYN", btn.get_text(" ", strip=True))
            if match:
                price = float(match.group(1))
                trip_id = btn.get("value")
                # Если один и тот же рейс встретился дважды, берём меньшую цену.
                if departure_time not in trips or price < trips[departure_time]["price"]:
                    trips[departure_time] = {"price": price, "trip_id": trip_id}

    return trips


async def fetch_ecolines_border_crossing(page, trip_id: str):
    """Возвращает строку с пунктом пропуска (например 'Berestovica (BY) → Bobrovniki (PL)')."""
    url = f"https://booking.ecolines.by/information/schedule/{trip_id}/outward"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1000)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    border_rows = []
    for row in soup.select("table.table tbody tr"):
        strong = row.select_one("td strong")
        if strong:
            # Берём только первый текстовый узел — внутри <strong> может быть
            # ещё вложенный <small>(BY)</small>, который иначе задвоит код страны.
            first_text = next(
                (c for c in strong.contents if isinstance(c, NavigableString)), ""
            ).strip()
            if first_text.lower().startswith("border"):
                border_rows.append(first_text)

    if len(border_rows) >= 2:
        return f"{border_rows[0]} → {border_rows[1]}"
    elif len(border_rows) == 1:
        return border_rows[0]
    return None


async def main():
    history = load_history()
    changes = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = await context.new_page()

        # Скрываем типичный признак headless-браузера, который проверяет Cloudflare.
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # Заходим на главную, чтобы пройти проверку Cloudflare и получить cf_clearance.
        # "networkidle" на сайтах с фоновой аналитикой почти никогда не наступает,
        # поэтому ждём только загрузку DOM и даём дополнительное время на JS-челлендж.
        await page.goto(HOMEPAGE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)

        cookies = await context.cookies()
        has_clearance = any(c["name"] == "cf_clearance" for c in cookies)
        if not has_clearance:
            print("Внимание: cf_clearance не получена, пробую подождать ещё раз...", file=sys.stderr)
            await page.wait_for_timeout(8000)
            cookies = await context.cookies()
            has_clearance = any(c["name"] == "cf_clearance" for c in cookies)
            if not has_clearance:
                print("cf_clearance так и не появилась — Cloudflare, вероятно, блокирует этот запуск.", file=sys.stderr)

        for route in ROUTES:
            key = f"{route['from_stop_id']}-{route['to_stop_id']}-{route['depart_date']}"
            try:
                journeys = await fetch_route_journeys(context.request, route)
            except Exception as e:
                print(f"Ошибка при запросе «{route['name']}»: {e}", file=sys.stderr)
                continue

            min_price = extract_min_price(journeys)
            if min_price is None:
                print(f"Не найдены доступные рейсы для «{route['name']}»", file=sys.stderr)
                continue

            prev_price = history.get(key, {}).get("price")
            history[key] = {"name": route["name"], "price": min_price, "date": route["depart_date"]}

            if prev_price is None:
                changes.append(f"📊 {route['name']} ({route['depart_date']}): текущая цена {min_price}€")
            elif min_price < prev_price:
                changes.append(f"📉 {route['name']} ({route['depart_date']}): цена упала {prev_price}€ → {min_price}€")
            elif min_price > prev_price:
                changes.append(f"📈 {route['name']} ({route['depart_date']}): цена выросла {prev_price}€ → {min_price}€")

        # --- Ecolines ---
        for route in ECOLINES_ROUTES:
            try:
                trips = await fetch_ecolines_trip_prices(page, route)
            except Exception as e:
                print(f"[Ecolines] Ошибка при запросе «{route['name']}»: {e}", file=sys.stderr)
                continue

            for target_time in route["departure_times"]:
                key = f"ecolines-{route['origin']}-{route['destination']}-{route['depart_date']}-{target_time}"
                trip = trips.get(target_time)

                if trip is None:
                    print(f"[Ecolines] Рейс {target_time} для «{route['name']}» не найден (возможно, распродан)", file=sys.stderr)
                    continue

                min_price = trip["price"]

                border = None
                if trip.get("trip_id"):
                    try:
                        border = await fetch_ecolines_border_crossing(page, trip["trip_id"])
                    except Exception as e:
                        print(f"[Ecolines] Не удалось получить погранпереход для {target_time}: {e}", file=sys.stderr)

                prev_price = history.get(key, {}).get("price")
                history[key] = {
                    "name": f"{route['name']} ({target_time})",
                    "price": min_price,
                    "date": route["depart_date"],
                    "border": border,
                }

                label = f"{route['name']} {target_time} ({route['depart_date']})"
                border_suffix = f"\n   Погранпереход: {border}" if border else ""

                if prev_price is None:
                    changes.append(f"📊 {label}: текущая цена {min_price} BYN{border_suffix}")
                elif min_price < prev_price:
                    changes.append(f"📉 {label}: цена упала {prev_price} → {min_price} BYN{border_suffix}")
                elif min_price > prev_price:
                    changes.append(f"📈 {label}: цена выросла {prev_price} → {min_price} BYN{border_suffix}")

        await browser.close()

    save_history(history)

    if changes:
        send_telegram("\n".join(changes))
        print("\n".join(changes))
    else:
        print("Изменений цен нет.")


if __name__ == "__main__":
    asyncio.run(main())
