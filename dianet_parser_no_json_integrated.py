#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
from html import unescape
from pathlib import Path
from typing import Any

import clickhouse_connect
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright


import personal_config as cfg

CH_HOST_TARGET     = cfg.CH_HOST_TARGET
CH_PORT_TARGET     = cfg.CH_PORT_TARGET
CH_USER_TARGET     = cfg.CH_USER_TARGET
CH_PASSWORD_TARGET = cfg.CH_PASSWORD_TARGET
CH_DATABASE_TARGET = cfg.CH_DATABASE_TARGET




BASE_URL = "https://dianet.telemedai.ru"
LOGIN_URL = f"{BASE_URL}/login/?next=/requests/registry/list/dispetcher/"
TASK_LIST_URL = f"{BASE_URL}/requests/registry/list/dispetcher/"
TASK_LIST_AJAX_URL = f"{BASE_URL}/requests/registry/list/ajax/"

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / ".dianet_storage_state.json"
DIANET_LOGIN = cfg.DIANET_LOGIN
DIANET_PASSWORD = cfg.DIANET_PASSWORD
DIANET_BEFORE_TYPE_DELAY_MS = 1000
DIANET_TYPE_DELAY_MS = 80
DIANET_CAPTCHA_WAIT_MS = 7000
DIANET_DEFAULT_COMMAND = "run"
DIANET_OUTPUT = "dianet_tasks.json"
DEFAULT_OUTPUT = SCRIPT_DIR / DIANET_OUTPUT
DIANET_TARGET_TABLE = "dianet_parser"
YANDEX_BROWSER_PATH = os.getenv(
    "YANDEX_BROWSER_PATH",
    r"C:\Program Files\Yandex\YandexBrowser\Application\browser.exe",
)
CLICKHOUSE_COLUMNS = [
    "id",
    "date_time",
    "specialization",
    "source",
    "med_organization",
    "responsible",
    "type_zayavki",
    "norma_zagruzki",
    "sla",
    "status",
    "documents_dicom",
    "documents_pdf",
    "load_date",
    "stomatology"
]

DEFAULT_STATUSES = ["new", "responsible_assigned", "work", "rework"]
DENTISTRY_FILTER_NAME = "organization__is_dentistry"
DENTISTRY_FILTER_VALUE = "1"
STATUS_VALUES = {
    "новая": "new",
    "назначен ответственный": "responsible_assigned",
    "назначена": "responsible_assigned",
    "в работе": "work",
    "отправлено на доработку": "rework",
    "на доработке": "rework",
}

# Скрипт, который маскирует автоматизацию (убирает navigator.webdriver)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def load_settings() -> None:
    load_env_file(SCRIPT_DIR / ".env")
    load_env_file(Path.cwd() / ".env")


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is empty. Add it to {SCRIPT_DIR / '.env'}")
    return value


def get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "да"}


def normalize_status(status: str) -> str:
    return STATUS_VALUES.get(status.strip().lower(), status.strip())


async def launch_yandex_browser(playwright: Any, *, headless: bool, slow_mo: int = 0) -> Any:
    return await playwright.chromium.launch(
        headless=headless,
        slow_mo=slow_mo,
        executable_path=os.getenv("YANDEX_BROWSER_PATH", YANDEX_BROWSER_PATH),
        args=[
            "--no-first-run",
            "--disable-extensions",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )


async def human_pause(min_ms: int = 150, max_ms: int = 450) -> None:
    """Небольшая случайная пауза, имитирующая человеческую реакцию."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def fill_first_available(
    page: Page,
    selectors: list[str],
    value: str,
    field_name: str,
) -> None:
    last_error: Exception | None = None
    type_delay_ms = DIANET_TYPE_DELAY_MS

    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=3_000)

            # подводим мышь к полю перед кликом вместо мгновенного клика
            box = await locator.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                    steps=random.randint(5, 12),
                )
                await page.wait_for_timeout(random.randint(80, 220))

            await locator.click(timeout=3_000)
            await locator.fill("", timeout=3_000)

            # печатаем с небольшим случайным разбросом задержки на символ
            jittered_delay = type_delay_ms + random.randint(-20, 40)
            await locator.type(value, delay=max(20, jittered_delay), timeout=30_000)
            return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Could not fill {field_name} field") from last_error


async def click_login_button(page: Page) -> None:
    candidates = [
        'button[type="submit"]',
        'input[type="submit"]',
        'xpath=//*[self::button or self::input or self::a][contains(normalize-space(.), "Войти")]',
    ]

    for selector in candidates:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=2_000)

            box = await locator.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                    steps=random.randint(5, 12),
                )
                await page.wait_for_timeout(random.randint(80, 200))

            await locator.click(timeout=3_000)
            break
        except Exception:
            continue

    try:
        await page.wait_for_url("**/requests/registry/list/dispetcher/**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_load_state("networkidle", timeout=10_000)


async def click_agreement_checkbox(page: Page) -> None:
    """Кликает чекбокс 'Я не робот' (Yandex SmartCaptcha) внутри iframe."""
    frame = page.frame_locator('iframe[src*="smartcaptcha.yandexcloud.net/checkbox"]').first
    checkbox = frame.locator('#js-button')

    await checkbox.wait_for(state="visible", timeout=5_000)

    box = await checkbox.bounding_box()
    if box:
        target_x = box["x"] + box["width"] / 2
        target_y = box["y"] + box["height"] / 2

        # заходим издалека в несколько шагов, с паузами между ними —
        # имитация человеческого движения мыши, а не телепортации
        await page.mouse.move(
            max(target_x - 220, 0), max(target_y - 120, 0), steps=random.randint(8, 14)
        )
        await page.wait_for_timeout(random.randint(120, 260))
        await page.mouse.move(
            target_x - 35, target_y - 12, steps=random.randint(6, 10)
        )
        await page.wait_for_timeout(random.randint(90, 220))
        await page.mouse.move(target_x, target_y, steps=random.randint(3, 6))
        await page.wait_for_timeout(random.randint(120, 300))

    before = await checkbox.get_attribute("aria-checked")
    print(f"aria-checked до клика: {before}")

    await checkbox.click(timeout=3_000)
    await page.wait_for_timeout(500)

    after = await checkbox.get_attribute("aria-checked")
    print(f"aria-checked после клика: {after}")


async def login_and_get_storage_state() -> dict[str, Any]:
    login = DIANET_LOGIN
    password = DIANET_PASSWORD
    before_type_delay_ms = DIANET_BEFORE_TYPE_DELAY_MS
    captcha_wait_ms = DIANET_CAPTCHA_WAIT_MS

    async with async_playwright() as p:
        browser = await launch_yandex_browser(p, headless=False, slow_mo=300)
        context = await browser.new_context(locale="ru-RU")

        # маскируем автоматизацию до загрузки любой страницы
        await context.add_init_script(STEALTH_INIT_SCRIPT)

        page = await context.new_page()

        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(before_type_delay_ms + random.randint(-200, 400))

        await fill_first_available(
            page,
            [
                'xpath=//label[contains(normalize-space(.), "Email") or contains(normalize-space(.), "Логин")]/following::input[1]',
                'input[name="username"]',
                'input[name="login"]',
                'input[type="email"]',
                'input[type="text"]',
            ],
            login,
            "login",
        )
        print("Login filled.")
        await human_pause(300, 800)

        await fill_first_available(
            page,
            [
                'xpath=//label[contains(normalize-space(.), "Пароль")]/following::input[1]',
                'input[name="password"]',
                'input[type="password"]',
            ],
            password,
            "password",
        )
        print("Password filled.")
        await human_pause(300, 800)

        await click_agreement_checkbox(page)
        print("Checkbox clicked (if present).")

        print(f"Waiting {captcha_wait_ms // 1000} seconds for manual captcha completion.")
        await page.wait_for_timeout(captcha_wait_ms)
        await click_login_button(page)

        if "/login/" in page.url:
            await browser.close()
            raise RuntimeError(
                "Still on login page. Increase DIANET_CAPTCHA_WAIT_MS "
                "or complete captcha before the wait ends."
            )

        storage_state = await context.storage_state()
        await browser.close()
        return storage_state


async def login_and_save_session() -> None:
    storage_state = await login_and_get_storage_state()
    STATE_FILE.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved session to {STATE_FILE}")


async def ensure_authorized_page(page: Page) -> None:
    await page.goto(TASK_LIST_URL, wait_until="domcontentloaded")

    # На странице может быть постоянный фоновый поллинг/вебсокеты,
    # из-за чего networkidle никогда не наступает — не считаем это ошибкой.
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeoutError:
        pass

    if "/login/" in page.url:
        raise RuntimeError("Session is not authorized. Run get_dianet_tasks_df() with login_first=True.")


async def scrape_rows(
    statuses: list[str] | None = None,
    take: int = 100,
    storage_state: dict[str, Any] | str | Path | None = None,
) -> list[dict[str, Any]]:
    if storage_state is None and not STATE_FILE.exists():
        raise RuntimeError(f"Session file not found: {STATE_FILE}")

    status_values = [normalize_status(status) for status in (statuses or DEFAULT_STATUSES)]
    rows: list[dict[str, Any]] = []
    page_number = 0
    total: int | None = None

    async with async_playwright() as p:
        browser = await launch_yandex_browser(p, headless=True)
        context = await browser.new_context(
            storage_state=storage_state or str(STATE_FILE),
            locale="ru-RU",
            viewport={"width": 1920, "height": 1080},
        )

        # маскируем автоматизацию и здесь тоже
        await context.add_init_script(STEALTH_INIT_SCRIPT)

        page = await context.new_page()
        await ensure_authorized_page(page)

        while total is None or len(rows) < total:
            response = await context.request.get(
                TASK_LIST_AJAX_URL,
                params={
                    "take": str(take),
                    "page": str(page_number),
                    "sort_key": "",
                    "filters": json.dumps({"status": status_values}, ensure_ascii=False),
                    "search": "",
                    "is_initial": "false",
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": TASK_LIST_URL,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )

            if not response.ok:
                body = await response.text()
                await browser.close()
                raise RuntimeError(f"Ajax request failed: HTTP {response.status} {body[:300]}")

            data = await response.json()
            fields = data.get("fields", {})
            result = data.get("result") or []
            total = int(data.get("total") or 0)

            if not result:
                break

            for item in result:
                try:
                    document_links = await asyncio.wait_for(
                        fetch_documents_pdf_links(page, item),
                        timeout=6
                    )
                except Exception:
                    document_links = []

                rows.append(normalize_ajax_row(item, fields, document_links))

            page_number += 1

            # небольшая случайная пауза между страницами пагинации,
            # чтобы запросы не шли строго equidistant по времени
            await asyncio.sleep(random.randint(150, 500) / 1000)

        dentistry_ids = await scrape_stomatology_ids(context, status_values, take=take)
        await browser.close()

    result_rows = deduplicate_rows(rows[: total or len(rows)])
    for row in result_rows:
        row["stomatology"] = "Да" if row.get("id") in dentistry_ids else "Нет"

    return result_rows


async def request_task_list_ajax(
    context: Any,
    *,
    status_values: list[str],
    page_number: int,
    take: int,
    extra_filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {"status": status_values}
    if extra_filters:
        filters.update(extra_filters)

    response = await context.request.get(
        TASK_LIST_AJAX_URL,
        params={
            "take": str(take),
            "page": str(page_number),
            "sort_key": "",
            "filters": json.dumps(filters, ensure_ascii=False),
            "search": "",
            "is_initial": "false",
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": TASK_LIST_URL,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )

    if not response.ok:
        body = await response.text()
        raise RuntimeError(f"Ajax request failed: HTTP {response.status} {body[:300]}")

    return await response.json()


async def scrape_stomatology_ids(
    context: Any,
    status_values: list[str],
    *,
    take: int = 100,
) -> set[str]:
    result_ids: set[str] = set()
    page_number = 0
    total: int | None = None

    while total is None or len(result_ids) < total:
        data = await request_task_list_ajax(
            context,
            status_values=status_values,
            page_number=page_number,
            take=take,
            extra_filters={DENTISTRY_FILTER_NAME: DENTISTRY_FILTER_VALUE},
        )

        result = data.get("result") or []
        total = int(data.get("total") or 0)

        if not result:
            break

        for item in result:
            row_id = strip_html(item.get("code", ""))
            if row_id:
                result_ids.add(row_id)

        page_number += 1
        await asyncio.sleep(random.randint(150, 500) / 1000)

    return result_ids


def strip_html(value: Any) -> str:
    if value is None:
        return ""

    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def extract_ziparchive_texts(html: str) -> list[str]:
    if not html:
        return []

    texts = re.findall(r"<span[^>]*>(.*?)</span>", html, flags=re.S | re.I)
    return [strip_html(unescape(text)) for text in texts if strip_html(unescape(text))]


def extract_storage_urls(html: str, request_id: int | None = None) -> list[str]:
    if not html:
        return []

    urls = re.findall(r"https://storage\.yandexcloud\.net/[^\"'<>\\\s]+", html)
    result: list[str] = []
    seen: set[str] = set()

    for url in urls:
        url = unescape(url)
        if request_id is not None and f"/docs/{request_id}/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)

    return result


async def fetch_documents_pdf_links(page: Page, item: dict[str, Any]) -> list[str]:
    if not item.get("doc_json_list"):
        return []

    request_id = item.get("id")
    detail_url = item.get("url")
    if not detail_url:
        return []

    if detail_url.startswith("/"):
        detail_url = f"{BASE_URL}{detail_url}"

    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        for _ in range(10):
            html = await page.locator("body").evaluate("(body) => body.innerHTML", timeout=10_000)
            urls = extract_storage_urls(html, request_id=request_id)
            if urls:
                return urls
            await page.wait_for_timeout(1_000)
    except Exception:
        return []

    html = await page.locator("body").evaluate("(body) => body.innerHTML", timeout=10_000)
    return extract_storage_urls(html, request_id=request_id)


def normalize_ajax_row(
    item: dict[str, Any],
    fields: dict[str, str],
    document_links: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": strip_html(item.get("code", "")),
        "date_time": strip_html(item.get("created_at", "")),
        "specialization": strip_html(item.get("medservice__name", "")),
        "source": strip_html(item.get("source__name", "")),
        "med_organization": strip_html(item.get("organization_fmt", "")),
        "responsible": strip_html(item.get("responsible__user__full_name", "")),
        "type_zayavki": strip_html(item.get("work_type__name", "")),
        "norma_zagruzki": item.get("load_rate"),
        "sla": strip_html(item.get("sla_time", "")),
        "status": strip_html(item.get("status", "")),
        "documents_dicom": "; ".join(extract_ziparchive_texts(item.get("orthanc_json_list", ""))),
        "documents_pdf": "; ".join(document_links or [])
    }


def deduplicate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []

    for row in rows:
        row_id = str(row.get("id") or json.dumps(row, ensure_ascii=False, sort_keys=True))
        if row_id in seen:
            continue
        seen.add(row_id)
        result.append(row)

    return result


def save_rows(rows: list[dict[str, Any]], output: Path, output_format: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    if output_format == "csv":
        if not rows:
            output.write_text("", encoding="utf-8")
            return

        fieldnames = list(rows[0].keys())
        with output.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return

    raise ValueError("output_format must be 'json' or 'csv'")


async def async_get_dianet_tasks_df(
    *,
    output: str | Path | None = DEFAULT_OUTPUT,
    output_format: str = "json",
    login_first: bool = True,
    statuses: list[str] | None = None,
) -> Any:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required. Install it with: pip install pandas") from exc

    if login_first:
        storage_state = await login_and_get_storage_state()
    else:
        storage_state = None

    rows = await scrape_rows(statuses=statuses, storage_state=storage_state)

    df = pd.DataFrame(rows)
    df["load_date"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    if output is not None:
        save_rows(df.to_dict(orient="records"), Path(output), output_format)

    return df


def get_dianet_tasks_df(
    *,
    output: str | Path | None = DEFAULT_OUTPUT,
    output_format: str = "json",
    login_first: bool = True,
    statuses: list[str] | None = None,
) -> Any:
    return asyncio.run(
        async_get_dianet_tasks_df(
            output=output,
            output_format=output_format,
            login_first=login_first,
            statuses=statuses,
        )
    )


def validate_clickhouse_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", name):
        raise ValueError(f"Unsafe ClickHouse table name: {name}")
    return name


def prepare_df_for_clickhouse(df: Any) -> Any:
    df_to_load = df.copy()

    for column in CLICKHOUSE_COLUMNS:
        if column not in df_to_load.columns:
            df_to_load[column] = "" if column != "norma_zagruzki" else 0.0

    df_to_load = df_to_load[CLICKHOUSE_COLUMNS]

    string_columns = [column for column in CLICKHOUSE_COLUMNS if column != "norma_zagruzki"]
    for column in string_columns:
        df_to_load[column] = df_to_load[column].fillna("").astype(str)

    df_to_load["norma_zagruzki"] = df_to_load["norma_zagruzki"].fillna(0).astype(float)
    return df_to_load


def upload_df_to_clickhouse(df: Any) -> None:
    client = clickhouse_connect.get_client(
        host=CH_HOST_TARGET,
        port=CH_PORT_TARGET,
        username=CH_USER_TARGET,
        password=CH_PASSWORD_TARGET,
        database=CH_DATABASE_TARGET,
        secure=True,
        verify=False,
        connect_timeout=60,
        send_receive_timeout=600,
    )

    table_name = DIANET_TARGET_TABLE
    df_to_load = prepare_df_for_clickhouse(df)

    client.command(f"TRUNCATE TABLE {table_name}")
    client.insert_df(table_name, df_to_load)
    print(f"Uploaded {len(df_to_load)} rows to ClickHouse table {table_name}")


if __name__ == "__main__":
    df = get_dianet_tasks_df(output=None)
    print(f"Downloaded {len(df)} rows")
    upload_df_to_clickhouse(df)
