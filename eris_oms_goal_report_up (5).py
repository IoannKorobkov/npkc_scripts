"""
eris_oms_goal_report_up.py
Отчёт по data_views.v_eris_assignment_results за последние 5 дней: все поля витрины,
только виды оплаты ОМС / Другое / пусто (без ПМУ — Платные услуги/Профосмотр/ДМС),
только коды процедур 34, 33, 427, 76. Обоснование (justification) не выгружается —
вместо него к каждой записи подтягивается «цель» направления (ASSIGNMENT_GOAL_NAME)
через мост accession_number(ERIS_ID) -> v_mosdoctor_ldp_fact.ASSIGNMENT_DOCUMENT_ID ->
v_assignments.DOCUMENT_ID (аналог моста из eris_pmu_justification_up.py, см. память
algorithm_justification_bridge — тот же паттерн, но конечное поле другое).
МКБ-10 код диагноза (diag_code) выгружается как есть, отдельного моста не требует.
При каждом запуске: удаление и полная перезагрузка окна в 10 дней — DELETE по toDate(conduct_date)
(периоду данных), а НЕ по load_datetime (возрасту загрузки). См. баг из eris_pmu_justification_up.py
(память project_eris_pmu_justification_report, исправлен 2026-09-17): чистка по возрасту загрузки
при частых запусках не удаляет предыдущий пересекающийся снимок периода — строки копятся дублями.

# INPUT:  data_views.v_eris_assignment_results   (source CH, через VPN) — основной источник, все поля
#         data_views.v_mosdoctor_ldp_fact        (source CH, через VPN) — мост №1 (accession_number -> document_id)
#         data_views.v_assignments               (source CH, через VPN) — мост №2 (document_id -> ASSIGNMENT_GOAL_NAME)
# OUTPUT: eris_oms_goal_report                    (target CH, Yandex Cloud, dwh_test_db)
"""

import subprocess
import socket
import time
import sys
import os
import sqlite3
import clickhouse_connect
from datetime import datetime, timedelta, date
import datetime as datetime_module

from ntfy_notifier import send_ntfy_alert
import personal_config as cfg

# === Настройки целевой ClickHouse ===
CH_HOST_TARGET     = cfg.CH_HOST_TARGET
CH_PORT_TARGET     = cfg.CH_PORT_TARGET
CH_USER_TARGET     = cfg.CH_USER_TARGET
CH_PASSWORD_TARGET = cfg.CH_PASSWORD_TARGET
CH_DATABASE_TARGET = cfg.CH_DATABASE_TARGET

# === Настройки исходной ClickHouse ===
CH_HOST_SOURCE     = cfg.CH_HOST
CH_PORT_SOURCE     = cfg.CH_PORT
CH_USER_SOURCE     = cfg.CH_USER
CH_PASSWORD_SOURCE = cfg.CH_PASSWORD
CH_DATABASE_SOURCE = cfg.CH_DATABASE

# === Настройки VPN (CLI, trac.exe) — тот же паттерн, что в instrumental_3w_up.py / eris_pmu_justification_up.py ===
VPN_TRAC_PATH = cfg.VPN_TRAC_PATH
VPN_USERNAME  = cfg.VPN_USERNAME
VPN_PASSWORD  = cfg.VPN_PASSWORD

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BUFFER_PATH = os.path.join(_SCRIPTS_DIR, 'temp_buffer_eris_oms_goal.db')
_TABLE_NAME  = 'eris_oms_goal_report'
_SQLITE_TMP  = 'temp_eris_oms_goal'

PERIOD_DAYS     = 10  # скользящее окно: данные только за последние N дней (витрины огромные)
# v_assignments (ReplicatedReplacingMergeTree, PARTITION BY (DOCUMENT_CLASS_ID, toYYYYMM(DOCUMENT_CREATED)))
# без фильтра по DOCUMENT_CREATED в JOIN сканируется целиком -> MEMORY_LIMIT_EXCEEDED (200 GiB).
# Буфер нужен, т.к. дата создания документа-направления обычно раньше даты проведения/подписи.
# Проверено вживую: с буфером 60 дней JOIN отрабатывает за ~20 сек вместо падения по памяти.
GOAL_JOIN_BUFFER_DAYS = 60

# Виды оплаты: только ОМС, Другое и пусто (NULL/'') — противоположность ПМУ
OMS_OTHER_PAYMENT_SOURCES = ('ОМС', 'Другое')
# Коды процедур (diagnostic_code в v_eris_assignment_results — тот же код, что играет роль
# research_id/research_code в других мостах, см. eris_pmu_justification_up.py)
PROCEDURE_CODES = ('34', '33', '427', '76')

# Строго упорядоченный список колонок — порядок совпадает с SELECT в _build_query()
_COLUMNS = [
    'assignment_result_id',
    'assignment_result_doc_id',
    'assignment_result_emp_job_execution_id',
    'assessment_result_type_code',
    'assignment_result_doc_created_date',
    'assignment_result_doc_cct',
    'assignment_describe_mu_id',
    'assignment_describe_start_date',
    'assignment_id',
    'patient_id',
    'diag_code',                  # МКБ-10 код диагноза
    'technician_job_execution_id',
    'payment_source',
    'conduct_mu_id',
    'conduct_date',
    'conduct_doc_id',
    'ae_title',
    'accession_number',
    'study_uid',
    'equipment_result_date',
    'assignment_status',
    'diagnostic_code',            # код процедуры (фильтр 34/33/427/76)
    'diagnostic_name',
    'device_type',
    'body_part',
    'multiplicity',
    'conduct_mo_id',
    'conduct_mu_name',
    'conduct_mo_name',
    'conduct_district_name',
    'conduct_region_name',
    'patient_birth_date',         # Nullable(String) — Date/DateTime не поддерживают даты рождения до 1970
    'patient_gender',
    'assignment_result_emp_id',
    'assignment_result_emp_fio',
    'technician_id',
    'technician_fio',
    'assignment_goal_name',       # цель направления (мост v_mosdoctor_ldp_fact -> v_assignments)
    'load_datetime',
]

_DATETIME_COLS = {
    'assignment_result_doc_created_date', 'assignment_describe_start_date',
    'conduct_date', 'equipment_result_date',
}
_INT_NOT_NULL = {'assignment_result_id'}
_INT_NULLABLE = {
    'assignment_result_emp_job_execution_id', 'assessment_result_type_code',
    'assignment_result_doc_cct', 'assignment_describe_mu_id', 'assignment_id',
    'patient_id', 'technician_job_execution_id', 'conduct_mu_id', 'conduct_mo_id',
    'multiplicity', 'assignment_result_emp_id', 'technician_id',
}


def _period_start() -> str:
    """Скользящее окно: сегодня минус PERIOD_DAYS. Вычисляется на каждый запуск заново."""
    return (datetime.now() - timedelta(days=PERIOD_DAYS)).strftime('%Y-%m-%d')


# === SQLite адаптеры ===
def setup_sqlite_adapters():
    def adapt_date(val):     return val.isoformat()
    def adapt_datetime(val): return val.isoformat()
    def convert_date(val):   return datetime_module.date.fromisoformat(val.decode())
    def convert_ts(val):     return datetime_module.datetime.fromisoformat(val.decode())
    sqlite3.register_adapter(datetime_module.date, adapt_date)
    sqlite3.register_adapter(datetime_module.datetime, adapt_datetime)
    sqlite3.register_converter("date", convert_date)
    sqlite3.register_converter("timestamp", convert_ts)


def _wait_for_dns(hostname: str, timeout: int = 40, interval: int = 3) -> bool:
    """Ждёт, пока hostname начнёт резолвиться через DNS (VPN-туннель поднимается не мгновенно)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.gethostbyname(hostname)
            return True
        except socket.gaierror:
            time.sleep(interval)
    return False


# === VPN (CLI, trac.exe — Check Point Endpoint Connect), паттерн из instrumental_3w_up.py ===
def _trac(*args, timeout: int = 60) -> tuple:
    """Запускает trac.exe с аргументами, возвращает (returncode, stdout, stderr)."""
    cmd = [VPN_TRAC_PATH] + list(args)
    print(f"  > {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.stdout.strip():
            print(f"  stdout: {r.stdout.strip()}")
        if r.stderr.strip():
            print(f"  stderr: {r.stderr.strip()}")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        print(f"  Таймаут {timeout} сек")
        return -1, '', 'timeout'
    except Exception as e:
        print(f"  Ошибка запуска trac: {e}")
        return -1, '', str(e)


def _parse_trac_status(info_out: str) -> str:
    """Достаёт status: активного (active site: true) конна из вывода `trac info`."""
    current_status = ''
    for line in info_out.splitlines():
        s = line.strip()
        if s.startswith('Conn '):
            current_status = ''
        if s.lower().startswith('status:'):
            current_status = s.split(':', 1)[1].strip()
        if 'active site: true' in s:
            return current_status
    return ''


def connect_vpn():
    print("🔄 Подключаю VPN через trac.exe...")
    send_ntfy_alert("Запускаю VPN для eris_oms_goal_report...", title="VPN Connect", priority="default", tags="lock")

    if _wait_for_dns(CH_HOST_SOURCE, timeout=1, interval=1):
        print(f"✅ VPN уже поднят ('{CH_HOST_SOURCE}' резолвится) — пропускаю подключение.")
        send_ntfy_alert("VPN подключён", title="VPN Connected", priority="high", tags="key")
        return

    _, info_out, _ = _trac('info', timeout=15)
    active_status = _parse_trac_status(info_out)
    if active_status.lower() != 'idle':
        print(f"   Активный сайт в статусе '{active_status or 'неизвестно'}' — отключаю перед новым подключением...")
        _trac('disconnect', timeout=20)
        time.sleep(3)

    cmd = [VPN_TRAC_PATH, 'connect']
    stdin_bytes = (VPN_USERNAME + '\n' + VPN_PASSWORD + '\n').encode('utf-8')
    print(f"  > {' '.join(cmd)}  (креды через stdin)")
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_bytes, err_bytes = proc.communicate(input=stdin_bytes, timeout=120)
        out_text = out_bytes.decode('utf-8', errors='replace').strip()
        if out_text:
            print(f"  stdout: {out_text}")
        if 'successfully established' in out_text:
            print("  trac сообщил об успешном подключении")
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        print("  Таймаут 120 сек при подключении")
    except Exception as e:
        print(f"  Ошибка: {e}")

    if _wait_for_dns(CH_HOST_SOURCE, timeout=40, interval=3):
        print(f"✅ DNS '{CH_HOST_SOURCE}' резолвится — туннель готов.")
    else:
        print(f"⚠️ DNS '{CH_HOST_SOURCE}' не резолвится спустя 40с после подключения — пробую дальше как есть.")

    print("✅ VPN подключён.")
    send_ntfy_alert("VPN подключён", title="VPN Connected", priority="high", tags="key")


def disconnect_vpn():
    print("🛑 Отключаю VPN через trac.exe...")
    send_ntfy_alert("Отключаюсь от VPN...", title="VPN Disconnect", priority="default", tags="unlock")
    _trac('disconnect', timeout=30)
    time.sleep(3)
    print("🛑 VPN отключён.")
    send_ntfy_alert("VPN отключён", title="VPN Disconnected", priority="default", tags="check")


def _ensure_table_exists(client) -> None:
    client.command(f"""
        CREATE TABLE IF NOT EXISTS {_TABLE_NAME}
        (
            assignment_result_id                   Int64,
            assignment_result_doc_id               Nullable(String),
            assignment_result_emp_job_execution_id Nullable(Int64),
            assessment_result_type_code            Nullable(Int64),
            assignment_result_doc_created_date     Nullable(DateTime),
            assignment_result_doc_cct              Nullable(Int64),
            assignment_describe_mu_id              Nullable(Int64),
            assignment_describe_start_date         Nullable(DateTime),
            assignment_id                          Nullable(Int64),
            patient_id                             Nullable(Int64),
            diag_code                              Nullable(String),
            technician_job_execution_id            Nullable(Int64),
            payment_source                         Nullable(String),
            conduct_mu_id                          Nullable(Int64),
            conduct_date                           Nullable(DateTime),
            conduct_doc_id                         Nullable(String),
            ae_title                               Nullable(String),
            accession_number                       Nullable(String),
            study_uid                              Nullable(String),
            equipment_result_date                  Nullable(DateTime),
            assignment_status                      Nullable(String),
            diagnostic_code                        Nullable(String),
            diagnostic_name                        Nullable(String),
            device_type                            Nullable(String),
            body_part                              Nullable(String),
            multiplicity                           Nullable(Int64),
            conduct_mo_id                          Nullable(Int64),
            conduct_mu_name                        Nullable(String),
            conduct_mo_name                        Nullable(String),
            conduct_district_name                  Nullable(String),
            conduct_region_name                    Nullable(String),
            patient_birth_date                     Nullable(String),
            patient_gender                         Nullable(String),
            assignment_result_emp_id               Nullable(Int64),
            assignment_result_emp_fio              Nullable(String),
            technician_id                          Nullable(Int64),
            technician_fio                         Nullable(String),
            assignment_goal_name                   Nullable(String),
            load_datetime                          DateTime
        )
        ENGINE = MergeTree()
        ORDER BY (assignment_result_id)
        SETTINGS index_granularity = 8192;
    """)
    print(f"✅ Таблица {_TABLE_NAME} проверена/создана.")


def _build_query(period_start: str, limit: int = 0) -> str:
    # ВАЖНО: порядок колонок в финальном SELECT совпадает с _COLUMNS (без load_datetime)
    limit_clause = f"LIMIT {limit}" if limit else ""
    goal_buffer_start = (
        datetime.strptime(period_start, '%Y-%m-%d') - timedelta(days=GOAL_JOIN_BUFFER_DAYS)
    ).strftime('%Y-%m-%d')
    return f"""
WITH
base AS (
    SELECT
        assignment_result_id AS assignment_result_id,
        assignment_result_doc_id AS assignment_result_doc_id,
        assignment_result_emp_job_execution_id AS assignment_result_emp_job_execution_id,
        assessment_result_type_code AS assessment_result_type_code,
        assignment_result_doc_created_date AS assignment_result_doc_created_date,
        assignment_result_doc_cct AS assignment_result_doc_cct,
        assignment_describe_mu_id AS assignment_describe_mu_id,
        assignment_describe_start_date AS assignment_describe_start_date,
        assignment_id AS assignment_id,
        patient_id AS patient_id,
        diag_code AS diag_code,
        technician_job_execution_id AS technician_job_execution_id,
        payment_source AS payment_source,
        conduct_mu_id AS conduct_mu_id,
        conduct_date AS conduct_date,
        conduct_doc_id AS conduct_doc_id,
        ae_title AS ae_title,
        accession_number AS accession_number,
        study_uid AS study_uid,
        equipment_result_date AS equipment_result_date,
        assignment_status AS assignment_status,
        diagnostic_code AS diagnostic_code,
        diagnostic_name AS diagnostic_name,
        device_type AS device_type,
        body_part AS body_part,
        multiplicity AS multiplicity,
        conduct_mo_id AS conduct_mo_id,
        conduct_mu_name AS conduct_mu_name,
        conduct_mo_name AS conduct_mo_name,
        conduct_district_name AS conduct_district_name,
        conduct_region_name AS conduct_region_name,
        -- toString() обязателен: часть дат рождения до 1970 года, бинарный DateTime64
        -- в этом случае роняет клиент ClickHouse (см. feedback_clickhouse_date_birthdate)
        toString(patient_birth_date) AS patient_birth_date,
        patient_gender AS patient_gender,
        assignment_result_emp_id AS assignment_result_emp_id,
        assignment_result_emp_fio AS assignment_result_emp_fio,
        technician_id AS technician_id,
        technician_fio AS technician_fio
    FROM data_views.v_eris_assignment_results
    WHERE toDate(conduct_date) >= '{period_start}'
      AND (payment_source IN {OMS_OTHER_PAYMENT_SOURCES} OR payment_source IS NULL OR payment_source = '')
      AND diagnostic_code IN {PROCEDURE_CODES}
      AND accession_number IS NOT NULL
),
-- === Мост «цель направления» (тот же каскад мостов, что для обоснования в
-- eris_pmu_justification_up.py, см. память algorithm_justification_bridge,
-- но конечное поле — ASSIGNMENT_GOAL_NAME из v_assignments). Мост №1 (task_list_doc)
-- покрывает ~98.5% записей, мост №2 (mosdoc) — фолбэк для остатка (~2%), проверено вживую.
task_list_doc AS (
    SELECT assignment_id AS assignment_id, any(assignment_doc_id) AS assignment_doc_id
    FROM data_views.v_instrumental_task_lists
    WHERE assignment_id IN (SELECT assignment_id FROM base WHERE assignment_id IS NOT NULL)
    GROUP BY assignment_id
),
mosdoc_doc AS (
    SELECT ERIS_ID AS accession_number, any(ASSIGNMENT_DOCUMENT_ID) AS assignment_doc_id
    FROM data_views.v_mosdoctor_ldp_fact
    WHERE SIGN_DATE >= '{period_start}'
      AND ERIS_ID IN (SELECT accession_number FROM base)
      AND ASSIGNMENT_DOCUMENT_ID IS NOT NULL
    GROUP BY ERIS_ID
),
goal_doc AS (
    SELECT b.accession_number AS accession_number,
           coalesce(tld.assignment_doc_id, mdd.assignment_doc_id) AS assignment_doc_id
    FROM base b
    LEFT JOIN task_list_doc tld ON b.assignment_id = tld.assignment_id
    LEFT JOIN mosdoc_doc    mdd ON b.accession_number = mdd.accession_number
),
goal_final AS (
    SELECT gd.accession_number AS accession_number, va.ASSIGNMENT_GOAL_NAME AS assignment_goal_name
    FROM goal_doc gd
    INNER JOIN data_views.v_assignments va ON va.DOCUMENT_ID = gd.assignment_doc_id
    -- Фильтр по партиции (см. GOAL_JOIN_BUFFER_DAYS) — без него JOIN сканирует всю v_assignments
    WHERE gd.assignment_doc_id IS NOT NULL
      AND va.DOCUMENT_CREATED >= '{goal_buffer_start}'
)
SELECT
    b.assignment_result_id,
    b.assignment_result_doc_id,
    b.assignment_result_emp_job_execution_id,
    b.assessment_result_type_code,
    b.assignment_result_doc_created_date,
    b.assignment_result_doc_cct,
    b.assignment_describe_mu_id,
    b.assignment_describe_start_date,
    b.assignment_id,
    b.patient_id,
    b.diag_code,
    b.technician_job_execution_id,
    b.payment_source,
    b.conduct_mu_id,
    b.conduct_date,
    b.conduct_doc_id,
    b.ae_title,
    b.accession_number,
    b.study_uid,
    b.equipment_result_date,
    b.assignment_status,
    b.diagnostic_code,
    b.diagnostic_name,
    b.device_type,
    b.body_part,
    b.multiplicity,
    b.conduct_mo_id,
    b.conduct_mu_name,
    b.conduct_mo_name,
    b.conduct_district_name,
    b.conduct_region_name,
    b.patient_birth_date,
    b.patient_gender,
    b.assignment_result_emp_id,
    b.assignment_result_emp_fio,
    b.technician_id,
    b.technician_fio,
    gf.assignment_goal_name AS assignment_goal_name
FROM base b
LEFT JOIN goal_final gf ON b.accession_number = gf.accession_number
-- Защита от дублей: если один accession_number встречается несколько раз — берём последний по дате
QUALIFY ROW_NUMBER() OVER (PARTITION BY b.accession_number ORDER BY b.conduct_date DESC) = 1
ORDER BY b.conduct_date DESC
{limit_clause}
"""


def _parse_dt(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    if isinstance(val, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return datetime.strptime(val[:19], fmt[:len(val)])
            except (ValueError, TypeError):
                continue
    return None


def _process_row(row: tuple, run_dt: datetime) -> list:
    """Позиционное преобразование строки из SQLite → типы ClickHouse.
    Порядок позиций строго соответствует _COLUMNS."""
    processed = []
    for i, val in enumerate(row):
        col = _COLUMNS[i]
        if col == 'load_datetime':
            processed.append(run_dt)  # единая метка на весь запуск
        elif col in _DATETIME_COLS:
            processed.append(_parse_dt(val))
        elif col in _INT_NOT_NULL:
            try:    processed.append(int(val) if val is not None else 0)
            except: processed.append(0)
        elif col in _INT_NULLABLE:
            try:    processed.append(int(val) if val is not None else None)
            except: processed.append(None)
        else:
            processed.append(str(val) if val is not None else None)
    return processed


def extract_phase():
   # Шаг 2: Source CH через VPN → SQLite
    client_source = None
    period_start = _period_start()
    try:

        for attempt in range(1, 4):
            try:
                client_source = clickhouse_connect.get_client(
                    host=CH_HOST_SOURCE, port=CH_PORT_SOURCE,
                    username=CH_USER_SOURCE, password=CH_PASSWORD_SOURCE,
                    database=CH_DATABASE_SOURCE, secure=True, verify=False,
                    send_receive_timeout=94200, connect_timeout=999999,
                )
                print("✅ Исходная ClickHouse подключена.")
                break
            except Exception as e_conn:
                print(f"⚠️ Подключение к источнику, попытка {attempt}: {e_conn}")
                client_source = None
                if attempt < 3:
                    _wait_for_dns(CH_HOST_SOURCE, timeout=20, interval=2)
                else:
                    raise

        print("📥 Выполняю запрос...")
        result    = client_source.query(_build_query(period_start))
        raw_rows  = result.result_rows
        col_names = result.column_names
        print(f"📥 Получено {len(raw_rows)} строк. Колонок CH: {len(col_names)}, ожидается: {len(_COLUMNS) - 1}")

        client_source.close()
        client_source = None

        # if not raw_rows:
        #     print(f"📭 Нет данных за последние {PERIOD_DAYS} дней.")
        #     send_ntfy_alert("Нет данных eris_oms_goal_report", title="OMS Goal Report Empty",
        #                     priority="default", tags="inbox")
        #     return True

        # CH уже вернул строки в порядке _COLUMNS[:-1]; добавляем load_datetime (заполнится в _process_row)
        ordered_rows = [list(row) + [None] for row in raw_rows]

        print(f"💾 Создаю SQLite буфер ({_BUFFER_PATH})...")
        conn   = sqlite3.connect(_BUFFER_PATH)
        cursor = conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {_SQLITE_TMP};")
        cursor.execute(
            f"CREATE TABLE {_SQLITE_TMP} ({', '.join(f'{c} TEXT' for c in _COLUMNS)});"
        )


        # cursor.executemany(
        #     f"INSERT INTO {_SQLITE_TMP} VALUES ({', '.join(['?' for _ in _COLUMNS])});",
        #     ordered_rows,
        # )
        # conn.commit()
        # conn.close()
        # print("✅ SQLite буфер заполнен.")

        if ordered_rows:
            cursor.executemany(
                f"INSERT INTO {_SQLITE_TMP} VALUES ({', '.join(['?' for _ in _COLUMNS])});",
                ordered_rows,
            )

        conn.commit()
        conn.close()

        if not raw_rows:
            print(f"📭 Нет данных за последние {PERIOD_DAYS} дней.")
            send_ntfy_alert("Нет данных eris_oms_goal_report", title="OMS Goal Report Empty",
                            priority="default", tags="inbox")

        print(f"✅ SQLite буфер заполнен ({len(raw_rows)} строк).")
        return True

    except Exception as e:
        msg = f"❌ Ошибка source CH / SQLite: {e}"
        print(msg)
        send_ntfy_alert(f"Сбой eris_oms_goal_report: {str(e)[:80]}", title="OMS Goal Report Error",
                        priority="urgent", tags="fire")
        if client_source:
            client_source.close()
        if os.path.exists(_BUFFER_PATH):
            try: os.remove(_BUFFER_PATH)
            except Exception: pass
        return False

def load_phase():
    period_start = _period_start()
    print(f"📊 [eris_oms_goal_report] Загрузка за последние {PERIOD_DAYS} дней (с {period_start})...")
    send_ntfy_alert(
        "Начинаю синхронизацию eris_oms_goal_report...",
        title="OMS Goal Report Start", priority="default", tags="inbox",
    )
    setup_sqlite_adapters()

    # Шаг 1: таблица + удаление предыдущего снимка периода (полная перезагрузка = replace, не append).
    # ВАЖНО: чистить по toDate(conduct_date) >= period_start (периоду данных), а не по load_datetime
    # (возрасту загрузки) — иначе при повторных запусках в пределах окна старый пересекающийся снимок
    # не удаляется и строки копятся дублями (тот же баг, что был в eris_pmu_justification_up.py).
    client_target_del = None
    try:
        print("🧹 Подключаюсь к целевой базе (очистка предыдущего снимка периода)...")
        client_target_del = clickhouse_connect.get_client(
            host=CH_HOST_TARGET, port=CH_PORT_TARGET,
            username=CH_USER_TARGET, password=CH_PASSWORD_TARGET,
            database=CH_DATABASE_TARGET, secure=True, verify=False,
        )
        _ensure_table_exists(client_target_del)
        client_target_del.command(
            f"ALTER TABLE {_TABLE_NAME} DELETE WHERE toDate(conduct_date) >= '{period_start}'"
        )
        print(f"✅ Предыдущий снимок периода с {period_start} удалён из {_TABLE_NAME}.")
        client_target_del.close()
        client_target_del = None
    except Exception as e:
        msg = f"❌ Ошибка подготовки целевой таблицы: {e}"
        print(msg)
        send_ntfy_alert(f"Ошибка подготовки БД: {str(e)[:80]}", title="OMS Goal Report Error",
                        priority="urgent", tags="database")
        if client_target_del:
            client_target_del.close()
        return False

    # Шаг 4: вставка в target CH
    client_target = None
    _BATCH_SIZE = 1000

    for attempt in range(1, 4):
        try:
            print(f"🔌 Подключаюсь к целевой ClickHouse (попытка {attempt})...")
            client_target = clickhouse_connect.get_client(
                host=CH_HOST_TARGET, port=CH_PORT_TARGET,
                username=CH_USER_TARGET, password=CH_PASSWORD_TARGET,
                database=CH_DATABASE_TARGET, secure=True, verify=False,
                send_receive_timeout=600, connect_timeout=30,
            )
            break
        except Exception as e:
            print(f"❌ Попытка {attempt}: {e}")
            if attempt < 3:
                time.sleep(5)
            else:
                if os.path.exists(_BUFFER_PATH):
                    try: os.remove(_BUFFER_PATH)
                    except Exception: pass
                return False

    try:
        conn   = sqlite3.connect(_BUFFER_PATH)
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {_SQLITE_TMP};")
        sqlite_rows = cursor.fetchall()
        conn.close()
        print(f"   Прочитано {len(sqlite_rows)} строк из SQLite.")

        run_dt = datetime.now()
        processed_rows = [_process_row(row, run_dt) for row in sqlite_rows]
        if not processed_rows:
            print(f"📭 [eris_oms_goal_report] load_phase: 0 строк за последние {PERIOD_DAYS} дней.")
            client_target.close()
            client_target = None
            try:
                os.remove(_BUFFER_PATH)
            except Exception:
                pass
            return True

        print(f"📤 Загружаю {len(processed_rows)} строк в {CH_DATABASE_TARGET}.{_TABLE_NAME} (батчами по {_BATCH_SIZE})...")
        for batch_start in range(0, len(processed_rows), _BATCH_SIZE):
            batch = processed_rows[batch_start:batch_start + _BATCH_SIZE]
            client_target.insert(_TABLE_NAME, batch, column_names=_COLUMNS)
            print(f"   ✔ Загружено {min(batch_start + _BATCH_SIZE, len(processed_rows))}/{len(processed_rows)}")

        msg = f"✅ [eris_oms_goal_report] Синхронизировано {len(processed_rows)} строк."
        print(msg)
        send_ntfy_alert(msg, title="OMS Goal Report Success", priority="high", tags="white_check_mark")

        client_target.close()
        client_target = None

        try:
            os.remove(_BUFFER_PATH)
        except Exception:
            pass
        print("🧹 Временный файл удалён.")

        return True

    except Exception as e:
        msg = f"❌ Ошибка выгрузки в целевую ClickHouse: {e}"
        print(msg)
        send_ntfy_alert(f"Ошибка выгрузки eris_oms_goal_report: {str(e)[:80]}",
                        title="OMS Goal Report Insert Error", priority="urgent", tags="database")
        if client_target:
            client_target.close()
        if os.path.exists(_BUFFER_PATH):
            try: os.remove(_BUFFER_PATH)
            except Exception: pass
        return False












def export_report() -> bool:
    """Полный цикл: очистка старых загрузок -> VPN -> source CH -> SQLite -> VPN off -> target CH."""
    period_start = _period_start()
    print(f"📊 [eris_oms_goal_report] Загрузка за последние {PERIOD_DAYS} дней (с {period_start})...")
    send_ntfy_alert(
        "Начинаю синхронизацию eris_oms_goal_report...",
        title="OMS Goal Report Start", priority="default", tags="inbox",
    )
    setup_sqlite_adapters()

    # Шаг 1: таблица + удаление предыдущего снимка периода (полная перезагрузка = replace, не append).
    # ВАЖНО: чистить по toDate(conduct_date) >= period_start (периоду данных), а не по load_datetime
    # (возрасту загрузки) — иначе при повторных запусках в пределах окна старый пересекающийся снимок
    # не удаляется и строки копятся дублями (тот же баг, что был в eris_pmu_justification_up.py).
    client_target_del = None
    try:
        print("🧹 Подключаюсь к целевой базе (очистка предыдущего снимка периода)...")
        client_target_del = clickhouse_connect.get_client(
            host=CH_HOST_TARGET, port=CH_PORT_TARGET,
            username=CH_USER_TARGET, password=CH_PASSWORD_TARGET,
            database=CH_DATABASE_TARGET, secure=True, verify=False,
        )
        _ensure_table_exists(client_target_del)
        client_target_del.command(
            f"ALTER TABLE {_TABLE_NAME} DELETE WHERE toDate(conduct_date) >= '{period_start}'"
        )
        print(f"✅ Предыдущий снимок периода с {period_start} удалён из {_TABLE_NAME}.")
        client_target_del.close()
        client_target_del = None
    except Exception as e:
        msg = f"❌ Ошибка подготовки целевой таблицы: {e}"
        print(msg)
        send_ntfy_alert(f"Ошибка подготовки БД: {str(e)[:80]}", title="OMS Goal Report Error",
                        priority="urgent", tags="database")
        if client_target_del:
            client_target_del.close()
        return False

    # Шаг 2: Source CH через VPN → SQLite
    client_source = None
    try:
        connect_vpn()

        for attempt in range(1, 4):
            try:
                client_source = clickhouse_connect.get_client(
                    host=CH_HOST_SOURCE, port=CH_PORT_SOURCE,
                    username=CH_USER_SOURCE, password=CH_PASSWORD_SOURCE,
                    database=CH_DATABASE_SOURCE, secure=True, verify=False,
                    send_receive_timeout=94200, connect_timeout=999999,
                )
                print("✅ Исходная ClickHouse подключена.")
                break
            except Exception as e_conn:
                print(f"⚠️ Подключение к источнику, попытка {attempt}: {e_conn}")
                client_source = None
                if attempt < 3:
                    _wait_for_dns(CH_HOST_SOURCE, timeout=20, interval=2)
                else:
                    raise

        print("📥 Выполняю запрос...")
        result    = client_source.query(_build_query(period_start))
        raw_rows  = result.result_rows
        col_names = result.column_names
        print(f"📥 Получено {len(raw_rows)} строк. Колонок CH: {len(col_names)}, ожидается: {len(_COLUMNS) - 1}")

        client_source.close()
        client_source = None

        if not raw_rows:
            print(f"📭 Нет данных за последние {PERIOD_DAYS} дней.")
            send_ntfy_alert("Нет данных eris_oms_goal_report", title="OMS Goal Report Empty",
                            priority="default", tags="inbox")
            disconnect_vpn()
            return True

        # CH уже вернул строки в порядке _COLUMNS[:-1]; добавляем load_datetime (заполнится в _process_row)
        ordered_rows = [list(row) + [None] for row in raw_rows]

        print(f"💾 Создаю SQLite буфер ({_BUFFER_PATH})...")
        conn   = sqlite3.connect(_BUFFER_PATH)
        cursor = conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {_SQLITE_TMP};")
        cursor.execute(
            f"CREATE TABLE {_SQLITE_TMP} ({', '.join(f'{c} TEXT' for c in _COLUMNS)});"
        )
        cursor.executemany(
            f"INSERT INTO {_SQLITE_TMP} VALUES ({', '.join(['?' for _ in _COLUMNS])});",
            ordered_rows,
        )
        conn.commit()
        conn.close()
        print("✅ SQLite буфер заполнен.")

    except Exception as e:
        msg = f"❌ Ошибка source CH / SQLite: {e}"
        print(msg)
        send_ntfy_alert(f"Сбой eris_oms_goal_report: {str(e)[:80]}", title="OMS Goal Report Error",
                        priority="urgent", tags="fire")
        if client_source:
            client_source.close()
        if os.path.exists(_BUFFER_PATH):
            try: os.remove(_BUFFER_PATH)
            except Exception: pass
        try: disconnect_vpn()
        except Exception: pass
        return False

    # Шаг 3: отключение VPN
    try:
        disconnect_vpn()
    except Exception as e:
        send_ntfy_alert(f"⚠️ Ошибка отключения VPN: {e}", title="VPN Warning",
                        priority="default", tags="warning")

    # Шаг 4: вставка в target CH
    client_target = None
    _BATCH_SIZE = 1000

    for attempt in range(1, 4):
        try:
            print(f"🔌 Подключаюсь к целевой ClickHouse (попытка {attempt})...")
            client_target = clickhouse_connect.get_client(
                host=CH_HOST_TARGET, port=CH_PORT_TARGET,
                username=CH_USER_TARGET, password=CH_PASSWORD_TARGET,
                database=CH_DATABASE_TARGET, secure=True, verify=False,
                send_receive_timeout=600, connect_timeout=30,
            )
            break
        except Exception as e:
            print(f"❌ Попытка {attempt}: {e}")
            if attempt < 3:
                time.sleep(5)
            else:
                if os.path.exists(_BUFFER_PATH):
                    try: os.remove(_BUFFER_PATH)
                    except Exception: pass
                return False

    try:
        conn   = sqlite3.connect(_BUFFER_PATH)
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {_SQLITE_TMP};")
        sqlite_rows = cursor.fetchall()
        conn.close()
        print(f"   Прочитано {len(sqlite_rows)} строк из SQLite.")

        run_dt = datetime.now()
        processed_rows = [_process_row(row, run_dt) for row in sqlite_rows]

        print(f"📤 Загружаю {len(processed_rows)} строк в {CH_DATABASE_TARGET}.{_TABLE_NAME} (батчами по {_BATCH_SIZE})...")
        for batch_start in range(0, len(processed_rows), _BATCH_SIZE):
            batch = processed_rows[batch_start:batch_start + _BATCH_SIZE]
            client_target.insert(_TABLE_NAME, batch, column_names=_COLUMNS)
            print(f"   ✔ Загружено {min(batch_start + _BATCH_SIZE, len(processed_rows))}/{len(processed_rows)}")

        msg = f"✅ [eris_oms_goal_report] Синхронизировано {len(processed_rows)} строк."
        print(msg)
        send_ntfy_alert(msg, title="OMS Goal Report Success", priority="high", tags="white_check_mark")

        client_target.close()
        client_target = None

        try:
            os.remove(_BUFFER_PATH)
        except Exception:
            pass
        print("🧹 Временный файл удалён.")

        return True

    except Exception as e:
        msg = f"❌ Ошибка выгрузки в целевую ClickHouse: {e}"
        print(msg)
        send_ntfy_alert(f"Ошибка выгрузки eris_oms_goal_report: {str(e)[:80]}",
                        title="OMS Goal Report Insert Error", priority="urgent", tags="database")
        if client_target:
            client_target.close()
        if os.path.exists(_BUFFER_PATH):
            try: os.remove(_BUFFER_PATH)
            except Exception: pass
        return False


# === Основная точка входа ===
def main():
    _start = datetime.now()
    print(f"🚀 Запуск eris_oms_goal_report (последние {PERIOD_DAYS} дней, полная перезагрузка)")
    send_ntfy_alert("Запускаю eris_oms_goal_report...", title="OMS Goal Report Start",
                    priority="default", tags="robot")

    success = export_report()
    dur_str = str(datetime.now() - _start).split('.')[0]

    if success:
        send_ntfy_alert(f"✅ eris_oms_goal_report обновлена! ({dur_str})",
                        title="Dashboard Done", priority="high", tags="tada")
        print(f"🏁 Готово за {dur_str}.")
    else:
        send_ntfy_alert("❌ Ошибка обновления eris_oms_goal_report!",
                        title="Dashboard Failed", priority="urgent", tags="warning")
        print(f"❌ Завершено с ошибками за {dur_str}.")

    return success


if __name__ == "__main__":
    sys.exit(0 if main() else 2)
