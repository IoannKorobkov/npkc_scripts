"""
ai_using_up.py  →  dwh_test_db.ai_using_studies  (Target CH)

Анализ использования ИИ врачами при описании КТ, МРТ, РГ, ММГ в НПКЦ.
Фаза 1 (VPN ON):  extract_phase()  — запросы к datalake, результат в _buffer
Фаза 2 (VPN OFF): load_phase()     — вставка из _buffer в dwh_test_db

Интегрируется в all_dashboards_up.py.
Глубина: DAYS_TO_SYNC (по умолчанию 3, задаётся из all_dashboards_up).
"""

import sys
import subprocess
import time
import traceback
import numpy as np
import pandas as pd
import pyautogui
import clickhouse_connect
from datetime import date, timedelta

sys.path.insert(0, r'C:\Users\risen\Desktop\MySecondBrain\project\scripts')
import personal_config as cfg
from ntfy_notifier import send_ntfy_alert

# Задаётся из all_dashboards_up.py
DAYS_TO_SYNC = 10

# Тарифы по модели ИИ (modelId -> цена за исследование), ведутся в отдельном
# CSV, а не в коде, т.к. периодически обновляются (см. ai_model_tariffs.csv).
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
TARIFFS_PATH = SCRIPT_DIR / 'ai_model_tariffs.csv'


def _load_tariffs() -> pd.DataFrame:
    df = pd.read_csv(TARIFFS_PATH)
    df['modelId'] = df['modelId'].astype(int)
    return df.set_index('modelId')[['Текущий тариф', 'Возможный тариф']]


TARIFFS = _load_tariffs()

MU_ID = '100001912827'

# Нижняя граница describe_start. Период в base_study фильтруется по дате
# ПОДПИСАНИЯ заключения, а не по дате начала описания — из-за этого
# исследования, начатые до этой даты, но подписанные позже, регулярно
# просачиваются в выборку при каждой новой синхронизации. У майского хвоста
# (до 2026-06-01) аномально высокая доля "нет логирования" (46 383 из 58 026
# по всей базе, 80% при 9 днях из 68) — не репрезентативно, отсекаем жёстко.
MIN_DESCRIBE_START = '2026-06-01'

MODALITY_CONFIG = {
    # ИИ-серия в kis.v_ai_log почти всегда (~97% случаев на КТ/МРТ) помечена
    # generic modality='ASMT', а не 'CT'/'CT_ASMT'/'MR'/'MR_ASMT' — как уже было
    # учтено для РГ/ММГ. Без 'ASMT' здесь исследования с реальным логированием
    # ошибочно попадали в ai_interaction='no_ai' (обнаружено 2026-08-06).
    'КТ':  {'device_type': 'КТ',  'ai_log_modalities': ('CT', 'CT_ASMT', 'ASMT')},
    'МРТ': {'device_type': 'МРТ', 'ai_log_modalities': ('MR', 'MR_ASMT', 'ASMT')},
    'РГ':  {'device_type': 'РГ',  'ai_log_modalities': ('ASMT', 'CR', 'DX')},
    'ММГ': {'device_type': 'ММГ', 'ai_log_modalities': ('ASMT', 'MG')},
}

# Процедуры, которые исключаются из анализа целиком (не считаются ни как "есть
# логирование", ни как "нет логирования") — по требованию 2026-08-06.
EXCLUDED_PROCEDURES = (
    'Флюорография легких профилактическая',
    'Рентгенография органов грудной клетки обзорная',
    'Рентгенография органов грудной клетки',
)

# Буфер между фазами
_buffer: pd.DataFrame | None = None


# ===========================================================================
# VPN (подменяются заглушками в all_dashboards_up.py)
# ===========================================================================
VPN_APP_PATH            = cfg.VPN_APP_PATH
VPN_PASSWORD            = cfg.VPN_PASSWORD
PASSWORD_FIELD_X,       PASSWORD_FIELD_Y       = cfg.PASSWORD_FIELD_X,       cfg.PASSWORD_FIELD_Y
CONNECT_BUTTON_X,       CONNECT_BUTTON_Y       = cfg.CONNECT_BUTTON_X,       cfg.CONNECT_BUTTON_Y
RIGHT_CLICK_MENU_X,     RIGHT_CLICK_MENU_Y     = cfg.RIGHT_CLICK_MENU_X,     cfg.RIGHT_CLICK_MENU_Y
DISCONNECT_MENU_ITEM_X, DISCONNECT_MENU_ITEM_Y = cfg.DISCONNECT_MENU_ITEM_X, cfg.DISCONNECT_MENU_ITEM_Y
CONFIRMATION_CLICK_X,   CONFIRMATION_CLICK_Y   = cfg.CONFIRMATION_CLICK_X,   cfg.CONFIRMATION_CLICK_Y


def connect_vpn():
    print('[ai_using] Запускаю TrGUI...')
    send_ntfy_alert('Запускаю VPN для ai_using...', title='VPN Connect', priority='default', tags='lock')
    try:
        process = subprocess.Popen(VPN_APP_PATH)
        print(f'   PID: {process.pid}')
    except Exception as e:
        msg = f'Ошибка запуска VPN: {e}'
        print(msg)
        send_ntfy_alert(msg, title='VPN Error', priority='urgent', tags='warning')
        raise
    time.sleep(15)

    for window in pyautogui.getWindowsWithTitle(''):
        if any(kw in window.title.lower() for kw in ['check point', 'trgui', 'endpoint']):
            try:
                window.activate()
                time.sleep(2)
                print(f"   Окно '{window.title}' активировано.")
                break
            except Exception:
                pass

    pyautogui.click(PASSWORD_FIELD_X, PASSWORD_FIELD_Y)
    time.sleep(0.5)
    pyautogui.click(PASSWORD_FIELD_X, PASSWORD_FIELD_Y)
    time.sleep(0.3)
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.3)
    pyautogui.press('delete')
    time.sleep(0.5)
    for char in VPN_PASSWORD:
        pyautogui.write(char)
        time.sleep(0.1)
    time.sleep(1)
    pyautogui.click(CONNECT_BUTTON_X, CONNECT_BUTTON_Y)
    time.sleep(15)
    print('[ai_using] VPN подключён.')
    send_ntfy_alert('VPN подключён', title='VPN Connected', priority='high', tags='key')


def disconnect_vpn():
    print('[ai_using] Отключаю VPN...')
    send_ntfy_alert('Отключаюсь от VPN...', title='VPN Disconnect', priority='default', tags='unlock')
    pyautogui.rightClick(RIGHT_CLICK_MENU_X, RIGHT_CLICK_MENU_Y)
    time.sleep(2)
    pyautogui.click(DISCONNECT_MENU_ITEM_X, DISCONNECT_MENU_ITEM_Y)
    time.sleep(3)
    pyautogui.click(CONFIRMATION_CLICK_X, CONFIRMATION_CLICK_Y)
    time.sleep(3)
    print('[ai_using] VPN отключён.')
    send_ntfy_alert('VPN отключён', title='VPN Disconnected', priority='default', tags='check')


# ===========================================================================
# Подключения
# ===========================================================================
def _source_client():
    return clickhouse_connect.get_client(
        host=cfg.CH_HOST, port=cfg.CH_PORT,
        username=cfg.CH_USER, password=cfg.CH_PASSWORD,
        secure=True, verify=False,
        connect_timeout=15, send_receive_timeout=900,
    )

def _target_client():
    return clickhouse_connect.get_client(
        host=cfg.CH_HOST_TARGET, port=cfg.CH_PORT_TARGET,
        username=cfg.CH_USER_TARGET, password=cfg.CH_PASSWORD_TARGET,
        secure=True, verify=False,
        connect_timeout=15, send_receive_timeout=600,
    )


# ===========================================================================
# SQL-анализ одной модальности
# ===========================================================================
def _analyse_one(client, date_from: str, date_to: str, modality: str) -> pd.DataFrame:
    conf = MODALITY_CONFIG[modality]
    device_type   = conf['device_type']
    ai_modalities = ", ".join(f"'{m}'" for m in conf['ai_log_modalities'])
    excluded_procs = ", ".join(f"'{p}'" for p in EXCLUDED_PROCEDURES)

    sql = f"""
        WITH
        base_study AS (
            SELECT
                study_uid,
                argMin(accession_number, assignment_describe_start_date)          AS accession_number,
                argMin(assignment_result_emp_fio, assignment_describe_start_date)  AS doctor_fio,
                min(assignment_describe_start_date)                                AS describe_start,
                ifNull(min(assignment_result_doc_created_date),
                       toDateTime('2099-01-01 00:00:00'))                          AS conclusion_time,
                argMin(diagnostic_name, assignment_describe_start_date)            AS diagnostic_name
            FROM data_views.v_eris_assignment_results
            WHERE assignment_describe_mu_id = '{MU_ID}'
              AND device_type = '{device_type}'
              -- период фильтруется по дате подписания заключения (как в concl_npkc_up.py),
              -- а не по дате начала описания — иначе популяция исследований расходится
              AND toDate(assignment_result_doc_created_date) BETWEEN '{date_from}' AND '{date_to}'
            GROUP BY study_uid
            -- diagnostic_name/describe_start — алиасы агрегатов (argMin/min),
            -- поэтому фильтр по ним должен быть в HAVING, а не в WHERE
            HAVING diagnostic_name NOT IN ({excluded_procs})
               AND describe_start >= '{MIN_DESCRIBE_START}'
        ),
        ai_result AS (
            -- Группировка по (study_uid, seriesIUID), а не только по study_uid:
            -- на одном исследовании может отработать 2-4 разных модели ИИ (~12%
            -- случаев), каждая со своей seriesIUID. Раньше any(modelId) молча
            -- схлопывал все модели в одну — теряя остальные (найдено 2026-08-06).
            SELECT
                JSONExtractString(raw_data, 'studyIUID')               AS study_uid,
                JSONExtractString(raw_data, 'aiResult', 'seriesIUID')  AS seriesIUID,
                any(JSONExtractString(raw_data, 'aiResult', 'norma'))  AS norma_value,
                any(if(raw_data = 'null',
                       JSONExtractInt(computed_data, 'modelId'),
                       JSONExtractInt(raw_data, 'aiResult', 'modelId'))) AS modelId
            FROM dwh_views.v_eris_report
            WHERE app_source = 'CDS'
              AND parseDateTimeBestEffortOrNull(
                      JSONExtractString(computed_data, 'pumStudyReadyForAiTime')
                  ) IS NOT NULL
              AND notEmpty(trim(JSONExtractString(raw_data, 'aiResult', 'norma')))
            GROUP BY study_uid, seriesIUID
        ),
        ai_log_msk AS (
            SELECT
                l.study_iuid,
                l.series_iuid,
                l.opened_at + INTERVAL 3 HOUR AS ts_msk,
                l.access_method,
                l.procedure_name,
                b.describe_start,
                b.conclusion_time
            FROM kis.v_ai_log AS l
            INNER JOIN base_study AS b ON l.study_iuid = b.study_uid
            WHERE l.modality IN ({ai_modalities})
              AND toDate(l.opened_at)
                  BETWEEN '{date_from}'::Date - INTERVAL 7 DAY
                      AND '{date_to}'::Date   + INTERVAL 30 DAY
        ),
        ai_actions AS (
            -- Группировка по (study_iuid, series_iuid): kis.v_ai_log.series_iuid
            -- совпадает с aiResult.seriesIUID из ИИ-витрины (проверено вручную
            -- 2026-08-06) — так каждая строка-модель получает только СВОИ события
            -- лога, а не все события study огулом, как было раньше.
            SELECT
                study_iuid,
                series_iuid,
                minIf(ts_msk, ts_msk >= describe_start - INTERVAL 5 MINUTE
                              AND ts_msk <= conclusion_time)                   AS first_ai_open,
                -- сгруппированные метрики (оставлены для обратной совместимости с дашбордами)
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND access_method IN ('MANUAL_LOAD', 'SCROLL'))         AS active_in_window,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name IN ('AI_VIEW_START', 'AI_VIEW_END')) AS view_in_window,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_VIEW_ENLARGE')                 AS enlarge_in_window,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND access_method IN ('AUTO_LOAD', 'AUTO_GRID'))        AS passive_in_window,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND access_method IN ('MANUAL_LOAD', 'SCROLL'))         AS outside_active_cnt,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name IN ('AI_VIEW_START', 'AI_VIEW_END', 'AI_VIEW_ENLARGE'))
                                                                                 AS outside_view_cnt,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND access_method IN ('AUTO_LOAD', 'AUTO_GRID'))        AS outside_passive_cnt,
                countIf(ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                                                                                 AS actions_outside,
                -- детализация по каждому procedure_name отдельно (во время описания)
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_AUTO_LOAD')                    AS cnt_auto_load,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_AUTO_GRID')                    AS cnt_auto_grid,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_MANUAL_LOAD')                  AS cnt_manual_load,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_VIEW_START')                   AS cnt_view_start,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_VIEW_END')                     AS cnt_view_end,
                countIf((ts_msk >= describe_start - INTERVAL 5 MINUTE AND ts_msk <= conclusion_time)
                        AND procedure_name = 'AI_VIEW_ENLARGE')                 AS cnt_enlarge,
                -- детализация по каждому procedure_name отдельно (вне окна описания)
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name = 'AI_AUTO_LOAD')                    AS outside_auto_load,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name = 'AI_AUTO_GRID')                    AS outside_auto_grid,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name = 'AI_MANUAL_LOAD')                  AS outside_manual_load,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name = 'AI_VIEW_START')                   AS outside_view_start,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name = 'AI_VIEW_END')                     AS outside_view_end,
                countIf((ts_msk < describe_start - INTERVAL 5 MINUTE OR ts_msk > conclusion_time)
                        AND procedure_name = 'AI_VIEW_ENLARGE')                 AS outside_enlarge,
                arrayStringConcat(groupArray(DISTINCT access_method), ', ')    AS methods_used
            FROM ai_log_msk
            GROUP BY study_iuid, series_iuid
        )
        SELECT
            '{modality}'                                                                  AS modality,
            b.study_uid                                                                   AS study_uid,
            b.accession_number,
            b.doctor_fio,
            b.diagnostic_name,
            b.describe_start,
            if(b.conclusion_time = toDateTime('2099-01-01 00:00:00'), NULL,
               b.conclusion_time)                                                         AS conclusion_time,
            if(r.study_uid IS NOT NULL, 1, 0)                                            AS has_ai,
            r.norma_value,
            r.modelId,
            r.seriesIUID,
            -- Добавляем вычисление service_name на основе modelId
            CASE r.modelId
                WHEN 1003 THEN 'Третье мнение РГ'
                WHEN 1004 THEN 'Цельс ММГ'
                WHEN 1006 THEN 'FBM РГ'
                WHEN 1015 THEN 'Цельс ФЛГ'
                WHEN 1040 THEN 'TrioDM (MTL)'
                WHEN 1059 THEN 'IMV MS'
                WHEN 1062 THEN 'Цельс РГ'
                WHEN 1063 THEN 'FBM ФЛГ'
                WHEN 1067 THEN 'Третье мнение ФЛГ'
                WHEN 1068 THEN 'CVisionRad - Knee Arthrosis'
                WHEN 1089 THEN 'АИ Диагностик МРТ ПКОП'
                WHEN 1094 THEN 'Oxytech Spine XR Scoliosis'
                WHEN 1096 THEN 'ТретьеМнение.Маммограммы'
                WHEN 1099 THEN '.Цельс ММГ (ОМС)'
                WHEN 1100 THEN 'Chest-IRA'
                WHEN 1101 THEN 'Цельс КС КТ ОГК'
                WHEN 1102 THEN 'CVL - Chest CT Complex'
                WHEN 1120 THEN 'АрхиМед Aivory Chest X'
                WHEN 1125 THEN 'АИ Диагностик МРТ ГМ'
                WHEN 1128 THEN 'IMV GLIOMAS'
                WHEN 1139 THEN 'CVL - Abdomen CT Spine fracture'
                WHEN 1144 THEN 'IMV МРТ ПКОП'
                WHEN 1145 THEN 'АИ Диагностик КТ ОГК комплекс'
                WHEN 1147 THEN 'Esper.Scoliosis'
                WHEN 1148 THEN 'АрхиМед Aivory AI Nose X'
                WHEN 1149 THEN 'Oxytech РГ синусит'
                WHEN 1150 THEN '.Третье Мнение ММГ (ОМС)'
                WHEN 1151 THEN 'Abdomen-IRA'
                WHEN 1155 THEN 'Третье мнение КТ ОГК комплекс'
                WHEN 1159 THEN 'Oxytech РГ коленного сустава'
                WHEN 1160 THEN 'А-рг-сколиоз'
                WHEN 1161 THEN 'Oxytech РГ тазобедренного сустава'
                WHEN 1164 THEN 'Цельс КС КТ ГМ'
                WHEN 1165 THEN 'IMV PIRADS'
                WHEN 1166 THEN 'Oxytech Spine XR Spondylolisthesis'
                WHEN 1167 THEN 'NTechMed CT Brain Complex'
                WHEN 1168 THEN 'NtechMed MRI Brain'
                WHEN 1172 THEN 'IMV МРТ ШОП'
                WHEN 1176 THEN 'NTechMed Norm Brain MRI'
                WHEN 1177 THEN 'А-рг-синусит'
                WHEN 1178 THEN 'Oxytech РГ перелом тазобедренного сустава'
                WHEN 1180 THEN 'Третье мнение КТ ГМ комплекс'
                WHEN 1181 THEN '.FBM ФЛГ (ОМС)'
                WHEN 1182 THEN '.FBM РГ (ОМС)'
                WHEN 1185 THEN '.Третье мнение ФЛГ (ОМС)'
                WHEN 1186 THEN '.Цельс ФЛГ (ОМС)'
                WHEN 1187 THEN '.Цельс РГ (ОМС)'
                WHEN 1188 THEN 'Cognitic-MRI-T'
                WHEN 1190 THEN '.Третье мнение РГ (ОМС)'
                WHEN 1196 THEN 'АИ Диагностик КТ ГМ комплекс'
                WHEN 1197 THEN 'АИ Диагностик КТ ОБП комплекс'
                WHEN 1198 THEN 'FBM PesPlanus'
                WHEN 1200 THEN 'Oxytech РГ перелом плечевого сустава'
                WHEN 1201 THEN 'Oxytech РГ плоскостопие'
                WHEN 1202 THEN 'Oxytech РГ перелом голеностопного сустава'
                WHEN 1203 THEN 'Oxytech РГ перелом лучезапястного сустава'
                WHEN 1204 THEN 'Oxytech КТ ОБП остеопороз'
                WHEN 1205 THEN 'IMV SUBCORTICAL'
                WHEN 1206 THEN 'IMV МРТ ГОП'
                WHEN 1208 THEN 'ЛОР КТ РГ синусит'
                WHEN 1209 THEN 'Oxytech РГ переломы позвоночника'
                WHEN 1210 THEN 'Цельс МРТ ОМТ'
                WHEN 1212 THEN 'СберМедИИ MoonShot'
                WHEN 1213 THEN 'Oxytech РГ костный возраст'
                WHEN 1214 THEN 'Oxytech КТ ОБП надпочечники'
                WHEN 1215 THEN 'Oxytech КТ ОБП аорта'
                WHEN 1216 THEN 'Oxytech КТ ОБП мочекаменная болезнь'
                WHEN 1217 THEN 'Oxytech КТ ОБП почки'
                WHEN 1218 THEN 'Oxytech РГ ОГК'
                WHEN 1219 THEN 'Oxytech ФЛГ'
                WHEN 1220 THEN 'Oxytech КТ ОБП печень'
                WHEN 1221 THEN 'Oxytech КТ ОБП желчнокаменная болезнь'
                WHEN 1226 THEN 'АрхиМед Aivory Pirads AI MR'
                WHEN 1227 THEN 'HealthSpine XR (сколиоз)'
                WHEN 1228 THEN 'HealthSpine XR (компрессионные переломы)'
                WHEN 1229 THEN 'Эирвэй РГ артроз тазобедренного сустава'
                WHEN 1230 THEN 'Эирвэй РГ переломы тел позвонков'
                WHEN 1231 THEN 'IMV UTERUS'
                WHEN 1232 THEN 'Oxytech КТ ОБП морфометрия почек'
                WHEN 1233 THEN 'Oxytech КТ ОБП морфометрия печени'
                WHEN 1234 THEN 'Oxytech КТ ОБП морфометрия поджелудочной железы и селезенки'
                WHEN 1235 THEN 'Multivox Cerebrum'
                WHEN 1236 THEN 'RAZUM MEDX'
                WHEN 1237 THEN 'NtechMed MRI Brain GLIOMAS'
                WHEN 1238 THEN 'Эирвэй РГ перелом плечевого сустава'
                WHEN 1239 THEN 'Эирвэй РГ перелом лучезапястного сустава'
                WHEN 1240 THEN 'Эирвэй РГ перелом голеностопного сустава'
                WHEN 1241 THEN 'Эирвэй РГ перелом тазобедренного сустава'
                WHEN 1242 THEN 'Эирвэй КТ ОБП. Образование печени'
                WHEN 1243 THEN 'Эирвэй КТ ОБП. Образование надпочечников'
                WHEN 1244 THEN 'Эирвэй КТ ОБП. Образование почек'
                WHEN 1245 THEN 'Эирвэй КТ ОБП. Мочекаменная болезнь'
                WHEN 1246 THEN 'Эирвэй КТ ОБП. Остеопороз'
                WHEN 1247 THEN 'Эирвэй КТ ОБП. Желчнокаменная болезнь'
                WHEN 1248 THEN 'Эирвэй КТ ОБП. Аневризма аорты'
                WHEN 1249 THEN 'ЛОР КТ РГ перелом лучезапястного сустава'
                WHEN 1250 THEN 'ЛОР КТ РГ перелом плечевого сустава'
                WHEN 1251 THEN 'Oxytech РГ коксометрия'
                WHEN 1252 THEN 'Эирвэй РГ артроз коленного сустава'
                WHEN 1253 THEN '.TrioDM (MTL) ОМС'
                WHEN 1254 THEN 'ЛОР КТ РГ перелом тазобедренного сустава'
                WHEN 1255 THEN 'Oxytech КТ ОГК комплекс'
                WHEN 1256 THEN 'СберМед РГ артроз коленного сустава'
                WHEN 1257 THEN 'Oxytech КТ ГМ комплекс'
                WHEN 1258 THEN 'ЛОР КТ РГ спондилолистез'
                WHEN 1259 THEN 'ЛОР КТ РГ сколиоз'
                WHEN 1260 THEN 'ИИнтеллект РГ артроз коленного сустава'
                WHEN 1261 THEN 'Cognitic-MRI-R'
                WHEN 1262 THEN 'ИИнтеллект РГ плоскостопие'
                WHEN 1263 THEN 'Oxytech КТ ОБП комлекс'
                WHEN 1264 THEN 'ЛОР КТ РГ перелом тел позвонков'
                WHEN 1265 THEN 'МРТ ГМ Скрининг склероз'
                WHEN 1266 THEN 'ИИнтеллект РГ артроз тазобедренного сустава'
                WHEN 1267 THEN 'RadPoint РГ спондилолистез'
                WHEN 1268 THEN 'ЛОР КТ РГ остеохондроз'
                WHEN 1269 THEN 'ИИнтеллект МРТ ОМТ предстательная железа'
                WHEN 1270 THEN 'RadPoint РГ сколиоз'
                WHEN 1271 THEN 'АрхиМед Aivory Al Knee Arthrosis X'
                WHEN 1272 THEN 'Эирвэй КТ ОБП комлекс'
                WHEN 1273 THEN 'RadPoint РГ перелом тел позвонков'
                WHEN 1274 THEN 'Эирвэй КТ ОГК комлекс'
                WHEN 1275 THEN 'RadPoint РГ остеохондроз'
                WHEN 1276 THEN 'ИИнтеллект РГ перелом голеностопного сустава'
                WHEN 1277 THEN 'ИИнтеллект РГ костный возраст'
                WHEN 1278 THEN 'ИИнтеллект РГ перелом тазобедренного сустава'
                WHEN 1279 THEN 'ИИнтеллект РГ перелом плечевого сустава'
                WHEN 1280 THEN 'ИИнтеллект РГ перелом лучезапястного сустава'
                WHEN 1281 THEN 'ESPER SpondyLens РГ спондилолистез'
                WHEN 1282 THEN 'ESPER SpondyLens РГ перелом тел позвонков'
                WHEN 1300 THEN 'CT HepatoScan AI'
                WHEN 1337 THEN 'ПУМТЕСТ'
                ELSE 'Неизвестный сервис'
            END                                                                           AS service_name,
            CASE
                WHEN a.study_iuid IS NULL
                  OR (a.active_in_window + a.view_in_window
                      + a.enlarge_in_window + a.passive_in_window) = 0 THEN 'no_ai'
                WHEN (a.active_in_window + a.view_in_window + a.enlarge_in_window) > 0   THEN 'active'
                ELSE 'passive'
            END                                                                           AS ai_interaction,
            nullIf(a.first_ai_open, toDateTime('1970-01-01 00:00:00'))                   AS first_ai_open,
            if(a.first_ai_open > toDateTime('1970-01-01 00:00:00'),
               round(dateDiff('minute', b.describe_start, a.first_ai_open), 0),
               NULL)                                                                      AS mins_to_first_ai,
            if(a.first_ai_open > toDateTime('1970-01-01 00:00:00')
               AND a.first_ai_open < b.conclusion_time, 1, 0)                            AS ai_before_conclusion,
            a.active_in_window,
            a.view_in_window,
            a.enlarge_in_window,
            a.passive_in_window,
            CASE
                WHEN a.study_iuid IS NULL                              THEN 'none'
                WHEN (a.outside_active_cnt + a.outside_view_cnt) > 0  THEN 'active'
                WHEN a.outside_passive_cnt > 0                         THEN 'passive'
                ELSE 'none'
            END                                                                           AS outside_ai_interaction,
            a.outside_active_cnt,
            a.outside_view_cnt,
            a.outside_passive_cnt,
            if(a.actions_outside > 0, 1, 0)                                              AS has_action_outside,
            a.methods_used,
            a.cnt_auto_load,
            a.cnt_auto_grid,
            a.cnt_manual_load,
            a.cnt_view_start,
            a.cnt_view_end,
            a.cnt_enlarge,
            a.outside_auto_load,
            a.outside_auto_grid,
            a.outside_manual_load,
            a.outside_view_start,
            a.outside_view_end,
            a.outside_enlarge
        FROM base_study AS b
        LEFT JOIN ai_result  AS r ON b.study_uid = r.study_uid
        -- Джойн лога строго по (study_iuid, series_iuid): каждая строка-модель
        -- получает только события лога своей серии. Для has_ai=0 (r.seriesIUID
        -- NULL) лог сознательно не привязывается — без модели нет серии для
        -- сопоставления, и AI_AUTO_LOAD в принципе не должен был сработать.
        LEFT JOIN ai_actions AS a ON b.study_uid = a.study_iuid AND r.seriesIUID = a.series_iuid
        ORDER BY b.describe_start
    """
    print(f'  [{modality}] запрос за {date_from} — {date_to}...')
    df = client.query_df(sql)
    print(f'  [{modality}] получено {len(df)} строк')
    return df


# ===========================================================================
# ФАЗА 1: извлечение (VPN ON, datalake)
# ===========================================================================
def extract_phase() -> bool:
    global _buffer
    today     = date.today()
    date_to   = today - timedelta(days=1)
    date_from = date_to - timedelta(days=DAYS_TO_SYNC - 1)
    d_from, d_to = str(date_from), str(date_to)

    print(f'\n[ai_using] ФАЗА 1 — период {d_from} — {d_to}')
    try:
        client = _source_client()
        frames = [_analyse_one(client, d_from, d_to, mod) for mod in MODALITY_CONFIG]
        _buffer = pd.concat(frames, ignore_index=True)
        _buffer.attrs['date_from'] = d_from
        _buffer.attrs['date_to']   = d_to
        total = len(_buffer)
        print(f'[ai_using] извлечено: {total} строк ({" + ".join(MODALITY_CONFIG)})')
        return total > 0
    except Exception as e:
        print(f'[ai_using] ❌ extract_phase: {e}')
        return False


# ===========================================================================
# ФАЗА 2: загрузка (VPN OFF, target CH)
# ===========================================================================
# Две целевые таблицы с разной грануляцией, из одной SQL-выгрузки (_analyse_one):
#   ai_using_studies           — "старые рельсы", 1 строка = 1 исследование
#                                 (для отчётов по исследованиям и их логированию)
#   ai_using_studies_by_model  — 1 строка = 1 исследование × 1 модель ИИ,
#                                 с тарифами (для отчётов по моделям и деньгам)
TABLE_BY_STUDY = 'dwh_test_db.ai_using_studies'
TABLE_BY_MODEL = 'dwh_test_db.ai_using_studies_by_model'

_COUNT_COLS = [
    'active_in_window', 'view_in_window', 'enlarge_in_window', 'passive_in_window',
    'outside_active_cnt', 'outside_view_cnt', 'outside_passive_cnt',
    'cnt_auto_load', 'cnt_auto_grid', 'cnt_manual_load',
    'cnt_view_start', 'cnt_view_end', 'cnt_enlarge',
    'outside_auto_load', 'outside_auto_grid', 'outside_manual_load',
    'outside_view_start', 'outside_view_end', 'outside_enlarge',
]
_MULTI_SERIES_SPECIAL_COLS = {
    'first_ai_open', 'has_action_outside', 'seriesIUID', 'methods_used',
    'ai_interaction', 'outside_ai_interaction', 'mins_to_first_ai', 'ai_before_conclusion',
}


def _naive(ts):
    """Убирает tzinfo, сохраняя wall-clock время (не конвертирует в UTC).
    Нужно для арифметики над first_ai_open/describe_start/conclusion_time:
    они могут прийти как object-колонка с tz-aware Timestamp, которую общий
    цикл tz_localize(None) в _prepare() не ловит (select_dtypes видит только
    настоящий datetimetz dtype, а не object с Timestamp внутри)."""
    if pd.isna(ts):
        return None
    return ts.tz_localize(None) if getattr(ts, 'tzinfo', None) is not None else ts


def _combine_methods_used(values):
    methods = set()
    for v in values:
        if pd.isna(v) or v == '':
            continue
        methods.update(m.strip() for m in v.split(',') if m.strip())
    return ', '.join(sorted(methods)) if methods else None


def _merge_multi_series(df: pd.DataFrame) -> pd.DataFrame:
    """Схлопывает несколько строк (study_uid, modelId, seriesIUID) в одну на
    (study_uid, modelId): если одна модель дала несколько серий на одном
    исследовании (напр. парные снимки), лог-активность СУММИРУЕТСЯ по всем
    её сериям, а не берётся от произвольной одной. Раньше (до 2026-08-06)
    здесь стоял drop_duplicates(keep='last'), который терял лог-активность
    остальных серий той же модели — этот случай встречается часто (~40%
    строк с ИИ приходится схлопывать)."""
    key = ['study_uid', 'modelId']
    grp_size = df.groupby(key, dropna=False).size()
    multi_keys = grp_size[grp_size > 1]
    if len(multi_keys) == 0:
        return df

    is_multi = df.set_index(key).index.isin(multi_keys.index)
    single = df[~is_multi].copy()
    multi_rows = df[is_multi].copy()
    print(f'  Схлопнуто {len(multi_keys)} групп (study_uid, modelId) с несколькими сериями '
          f'({len(multi_rows)} строк -> {len(multi_keys)}), лог-активность просуммирована')

    const_cols = [c for c in df.columns
                  if c not in key and c not in _COUNT_COLS and c not in _MULTI_SERIES_SPECIAL_COLS]
    agg = {c: 'first' for c in const_cols}
    agg.update({c: 'sum' for c in _COUNT_COLS})
    agg['first_ai_open'] = 'min'
    agg['has_action_outside'] = 'max'
    agg['seriesIUID'] = lambda s: ', '.join(sorted(s.dropna())) or None
    agg['methods_used'] = _combine_methods_used

    merged = multi_rows.groupby(key, dropna=False, as_index=False).agg(agg)

    merged['mins_to_first_ai'] = merged.apply(
        lambda r: round((_naive(r['first_ai_open']) - _naive(r['describe_start'])).total_seconds() / 60, 0)
                  if pd.notna(r['first_ai_open']) else None,
        axis=1,
    )
    merged['ai_before_conclusion'] = merged.apply(
        lambda r: 1 if pd.notna(r['first_ai_open']) and pd.notna(r['conclusion_time'])
                       and _naive(r['first_ai_open']) < _naive(r['conclusion_time']) else 0,
        axis=1,
    )

    in_window_sum = merged[['active_in_window', 'view_in_window',
                             'enlarge_in_window', 'passive_in_window']].sum(axis=1, skipna=True)
    active_sum = merged[['active_in_window', 'view_in_window', 'enlarge_in_window']].sum(axis=1, skipna=True)
    merged['ai_interaction'] = np.select(
        [in_window_sum == 0, active_sum > 0], ['no_ai', 'active'], default='passive',
    )

    outside_active_sum = merged[['outside_active_cnt', 'outside_view_cnt']].sum(axis=1, skipna=True)
    merged['outside_ai_interaction'] = np.select(
        [outside_active_sum > 0, merged['outside_passive_cnt'].fillna(0) > 0],
        ['active', 'passive'], default='none',
    )

    merged = merged[df.columns]
    return pd.concat([single, merged], ignore_index=True)


def _prepare_by_model(df: pd.DataFrame, date_from: str, date_to: str) -> pd.DataFrame:
    """Готовит данные для ai_using_studies_by_model: 1 строка = исследование ×
    модель ИИ, с тарифами. Возвращает уже полностью подготовленный DataFrame —
    из него же строится study-грануляция в _prepare_by_study()."""
    upload = df.copy()

    # strip timezone
    for col in upload.select_dtypes(include=['datetimetz']).columns:
        upload[col] = upload[col].dt.tz_localize(None)

    upload['period_from'] = date.fromisoformat(date_from)
    upload['period_to']   = date.fromisoformat(date_to)

    # тариф по modelId (см. ai_model_tariffs.csv); NaN, если модели нет в
    # справочнике тарифов (напр. АИ ещё не тарифицирован или ID не завели)
    tariff_lookup = upload['modelId'].map(TARIFFS['Текущий тариф'])
    upload['tariff_current'] = tariff_lookup
    upload['tariff_possible'] = upload['modelId'].map(TARIFFS['Возможный тариф'])

    col_order = [
        'modality', 'study_uid', 'accession_number', 'doctor_fio', 'diagnostic_name',
        'describe_start', 'conclusion_time',
        'has_ai', 'norma_value', 'modelId', 'seriesIUID', 'service_name',
        'tariff_current', 'tariff_possible', 'ai_interaction',
        'first_ai_open', 'mins_to_first_ai', 'ai_before_conclusion',
        'active_in_window', 'view_in_window', 'enlarge_in_window', 'passive_in_window',
        'outside_ai_interaction', 'outside_active_cnt', 'outside_view_cnt',
        'outside_passive_cnt', 'has_action_outside', 'methods_used',
        'cnt_auto_load', 'cnt_auto_grid', 'cnt_manual_load',
        'cnt_view_start', 'cnt_view_end', 'cnt_enlarge',
        'outside_auto_load', 'outside_auto_grid', 'outside_manual_load',
        'outside_view_start', 'outside_view_end', 'outside_enlarge',
        'period_from', 'period_to',
    ]
    upload = upload[col_order]

    # строки без study_uid — битые данные источника
    bad = upload['study_uid'].isna().sum()
    if bad:
        print(f'  Пропущено {bad} строк с пустым study_uid')
        upload = upload.dropna(subset=['study_uid'])

    # несколько серий одной модели на одном study_uid — не дубли, а данные,
    # которые нужно правильно объединить (сумма лог-активности по всем сериям),
    # см. _merge_multi_series
    upload = _merge_multi_series(upload)

    # norma_value: float/str → '0'/'1'/None
    upload['norma_value'] = upload['norma_value'].apply(
        lambda x: str(int(float(x))) if pd.notna(x) and x != '' else None
    )

    for col in ['has_ai', 'ai_before_conclusion', 'has_action_outside']:
        upload[col] = pd.to_numeric(upload[col], errors='coerce').fillna(0).astype(int)

    for col in ['active_in_window', 'view_in_window', 'enlarge_in_window', 'passive_in_window',
                'outside_active_cnt', 'outside_view_cnt', 'outside_passive_cnt',
                'cnt_auto_load', 'cnt_auto_grid', 'cnt_manual_load',
                'cnt_view_start', 'cnt_view_end', 'cnt_enlarge',
                'outside_auto_load', 'outside_auto_grid', 'outside_manual_load',
                'outside_view_start', 'outside_view_end', 'outside_enlarge']:
        upload[col] = upload[col].apply(lambda x: int(x) if pd.notna(x) else None)

    upload['mins_to_first_ai'] = upload['mins_to_first_ai'].apply(
        lambda x: float(x) if pd.notna(x) else None
    )

    def _clean(v):
        if v is None:
            return None
        if isinstance(v, float) and np.isnan(v):
            return None
        return v

    for col in upload.select_dtypes(include=['object']).columns:
        upload[col] = upload[col].apply(_clean)

    return upload


_STUDY_COL_ORDER = [
    'modality', 'study_uid', 'accession_number', 'doctor_fio', 'diagnostic_name',
    'describe_start', 'conclusion_time',
    'has_ai', 'norma_value', 'modelId', 'service_name', 'ai_interaction',
    'first_ai_open', 'mins_to_first_ai', 'ai_before_conclusion',
    'active_in_window', 'view_in_window', 'enlarge_in_window', 'passive_in_window',
    'outside_ai_interaction', 'outside_active_cnt', 'outside_view_cnt',
    'outside_passive_cnt', 'has_action_outside', 'methods_used',
    'cnt_auto_load', 'cnt_auto_grid', 'cnt_manual_load',
    'cnt_view_start', 'cnt_view_end', 'cnt_enlarge',
    'outside_auto_load', 'outside_auto_grid', 'outside_manual_load',
    'outside_view_start', 'outside_view_end', 'outside_enlarge',
    'period_from', 'period_to',
]


def _prepare_by_study(upload_by_model: pd.DataFrame) -> pd.DataFrame:
    """"Старые рельсы" — схлопывает уже подготовленные по-модельные строки
    (_prepare_by_model) до 1 строки на study_uid. Лог-активность суммируется
    по ВСЕМ моделям исследования (а не только сериям одной модели, как в
    _merge_multi_series) — та же логика на уровень выше. modelId/service_name/
    tariff_* на этом уровне не имеют смысла (может быть несколько моделей):
    modelId/service_name берутся как произвольный представитель (как было в
    скрипте до 2026-08-06), tariff_current/tariff_possible/seriesIUID здесь не
    нужны — за точной разбивкой по деньгам и моделям см. ai_using_studies_by_model."""
    key = ['study_uid']
    const_cols = ['modality', 'accession_number', 'doctor_fio', 'diagnostic_name',
                  'describe_start', 'conclusion_time', 'norma_value',
                  'period_from', 'period_to']
    agg = {c: 'first' for c in const_cols}
    agg.update({c: 'sum' for c in _COUNT_COLS})
    agg['has_ai'] = 'max'
    agg['modelId'] = 'first'
    agg['service_name'] = 'first'
    agg['first_ai_open'] = 'min'
    agg['has_action_outside'] = 'max'
    agg['methods_used'] = _combine_methods_used

    out = upload_by_model.groupby(key, dropna=False, as_index=False).agg(agg)

    out['mins_to_first_ai'] = out.apply(
        lambda r: round((_naive(r['first_ai_open']) - _naive(r['describe_start'])).total_seconds() / 60, 0)
                  if pd.notna(r['first_ai_open']) else None,
        axis=1,
    )
    out['ai_before_conclusion'] = out.apply(
        lambda r: 1 if pd.notna(r['first_ai_open']) and pd.notna(r['conclusion_time'])
                       and _naive(r['first_ai_open']) < _naive(r['conclusion_time']) else 0,
        axis=1,
    )

    in_window_sum = out[['active_in_window', 'view_in_window',
                          'enlarge_in_window', 'passive_in_window']].sum(axis=1, skipna=True)
    active_sum = out[['active_in_window', 'view_in_window', 'enlarge_in_window']].sum(axis=1, skipna=True)
    out['ai_interaction'] = np.select(
        [in_window_sum == 0, active_sum > 0], ['no_ai', 'active'], default='passive',
    )

    outside_active_sum = out[['outside_active_cnt', 'outside_view_cnt']].sum(axis=1, skipna=True)
    out['outside_ai_interaction'] = np.select(
        [outside_active_sum > 0, out['outside_passive_cnt'].fillna(0) > 0],
        ['active', 'passive'], default='none',
    )

    for col in ['has_ai', 'ai_before_conclusion', 'has_action_outside']:
        out[col] = pd.to_numeric(out[col], errors='coerce').fillna(0).astype(int)
    for col in _COUNT_COLS:
        out[col] = out[col].apply(lambda x: int(x) if pd.notna(x) else None)

    return out[_STUDY_COL_ORDER]


def _dedup_mutation_by_model(tc):
    """ALTER TABLE ... DELETE на ai_using_studies_by_model — оставляет по
    каждой паре (study_uid, modelId) только самую свежую версию (по
    loaded_at). Ключ включает modelId, т.к. 1 строка = исследование × модель
    (см. комментарий у ai_result в _analyse_one). ifNull нужен, чтобы NULL
    modelId (has_ai=0) не ломал сравнение тройки в NOT IN. Кидает исключение
    при ошибке."""
    tc.command(
        f'''
        ALTER TABLE {TABLE_BY_MODEL}
        DELETE WHERE (study_uid, ifNull(modelId, -1), loaded_at) NOT IN (
            SELECT study_uid, ifNull(modelId, -1), max(loaded_at)
            FROM {TABLE_BY_MODEL}
            GROUP BY study_uid, ifNull(modelId, -1)
        )
        ''',
        settings={'mutations_sync': '1'},
    )


def _dedup_mutation_by_study(tc):
    """ALTER TABLE ... DELETE на ai_using_studies (старые рельсы) — оставляет
    по каждому study_uid только самую свежую версию (по loaded_at). Кидает
    исключение при ошибке."""
    tc.command(
        f'''
        ALTER TABLE {TABLE_BY_STUDY}
        DELETE WHERE (study_uid, loaded_at) NOT IN (
            SELECT study_uid, max(loaded_at)
            FROM {TABLE_BY_STUDY}
            GROUP BY study_uid
        )
        ''',
        settings={'mutations_sync': '1'},
    )


def _dedup_check_one(tc, table: str, uniq_expr: str, mutation_fn, verbose: bool) -> bool:
    try:
        total = int(tc.query_df(f"SELECT count() AS cnt FROM {table}").iloc[0, 0])
        uniq = int(tc.query_df(f"SELECT count(DISTINCT {uniq_expr}) AS cnt FROM {table}").iloc[0, 0])
        dup = total - uniq

        if dup <= 0:
            if verbose:
                print(f'[ai_using] dedup_check[{table}]: дублей нет ({total} строк)')
            return True

        if verbose:
            print(f'[ai_using] dedup_check[{table}]: найдено {dup} дублей '
                  f'({total} строк, {uniq} уникальных) — устраняю...')
        mutation_fn(tc)

        total_after = int(tc.query_df(f"SELECT count() AS cnt FROM {table}").iloc[0, 0])
        if verbose:
            print(f'[ai_using] dedup_check[{table}]: удалено {total - total_after} строк, '
                  f'осталось {total_after}')
        return True
    except Exception as e:
        print(f'[ai_using] ❌ dedup_check[{table}]: {e}')
        print(traceback.format_exc())
        send_ntfy_alert(
            f'ai_using dedup_check[{table}]: ошибка — {e}',
            title='AI Using Dedup Check Failed', priority='urgent', tags='warning',
        )
        return False


def dedup_check(verbose: bool = True) -> bool:
    """Безопасная самостоятельная проверка/устранение дублей в обеих целевых
    таблицах. Не зависит от _buffer — можно вызывать отдельно (в конце main()
    или из all_dashboards_up.py) как страховку на случай, если дедуп внутри
    load_phase() не отработал."""
    tc = _target_client()
    ok_study = _dedup_check_one(tc, TABLE_BY_STUDY, 'study_uid', _dedup_mutation_by_study, verbose)
    ok_model = _dedup_check_one(tc, TABLE_BY_MODEL, '(study_uid, ifNull(modelId, -1))',
                                 _dedup_mutation_by_model, verbose)
    return ok_study and ok_model


def load_phase() -> bool:
    global _buffer
    if _buffer is None or len(_buffer) == 0:
        print('[ai_using] ⚠️ буфер пуст, пропускаю загрузку')
        return False

    date_from = _buffer.attrs.get('date_from', '')
    date_to   = _buffer.attrs.get('date_to', '')

    print(f'\n[ai_using] ФАЗА 2 — подготовка {len(_buffer)} строк для двух таблиц')
    try:
        upload_by_model = _prepare_by_model(_buffer, date_from, date_to)
        upload_by_study = _prepare_by_study(upload_by_model)
        tc = _target_client()
        print(f'  -> {TABLE_BY_MODEL}: {len(upload_by_model)} строк (исследование×модель)')
        tc.insert_df(TABLE_BY_MODEL, upload_by_model)
        print(f'  -> {TABLE_BY_STUDY}: {len(upload_by_study)} строк (1 на исследование)')
        tc.insert_df(TABLE_BY_STUDY, upload_by_study)
    except Exception as e:
        print(f'[ai_using] ❌ load_phase (insert): {e}')
        print(traceback.format_exc())
        send_ntfy_alert(
            f'ai_using load_phase: ошибка вставки — {e}',
            title='AI Using Insert Failed', priority='urgent', tags='warning',
        )
        return False

    # принудительная дедупликация. ReplacingMergeTree схлопывает дубли только
    # в рамках одного ORDER BY ключа — если у исследования между двумя
    # синхронизациями сдвинулся describe_start, ключ меняется и OPTIMIZE FINAL
    # дубль не убирает. Поэтому удаляем явно (см. _dedup_mutation_by_study/model).
    # Отдельный try — если дедуп упадёт, вставленные данные уже не теряем и не
    # гоняем повторно, но явно сообщаем об ошибке.
    print('[ai_using] дедупликация обеих таблиц (ALTER TABLE ... DELETE)...')
    try:
        _dedup_mutation_by_study(tc)
        _dedup_mutation_by_model(tc)
    except Exception as e:
        print(f'[ai_using] ❌ дедупликация не выполнена: {e}')
        print(traceback.format_exc())
        send_ntfy_alert(
            f'ai_using load_phase: дедупликация упала — {e}',
            title='AI Using Dedup Failed', priority='urgent', tags='warning',
        )
        # вставка прошла успешно — буфер очищаем, дубли почистятся при следующем запуске
        _buffer = None
        return True

    try:
        for table in (TABLE_BY_STUDY, TABLE_BY_MODEL):
            cnt = tc.query_df(
                f"SELECT count() AS cnt FROM {table} "
                f"WHERE period_from = '{date_from}' AND period_to = '{date_to}'"
            )
            total_all = tc.query_df(f"SELECT count() AS cnt FROM {table}")
            print(f'[ai_using] ✅ {table}: загружено за период {cnt.iloc[0,0]} | всего {total_all.iloc[0,0]}')
    except Exception as e:
        print(f'[ai_using] ⚠️ не удалось получить итоговые счётчики: {e}')
        print(traceback.format_exc())

    _buffer = None
    return True


# ===========================================================================
# Полный цикл (standalone): VPN → extract → VPN off → load
# ===========================================================================
def main():
    print(f'[ai_using] Запуск — последние {DAYS_TO_SYNC} дней')
    send_ntfy_alert(
        f'AI Using: синхронизация за {DAYS_TO_SYNC} дней...',
        title='AI Using Start', priority='default', tags='robot',
    )

    # Шаг 1: VPN ON → extract
    try:
        connect_vpn()
    except Exception as e:
        msg = f'[ai_using] Не удалось подключить VPN: {e}'
        print(msg)
        send_ntfy_alert(msg, title='AI Using Fatal', priority='urgent', tags='fire')
        sys.exit(1)

    ok = extract_phase()

    # Шаг 2: VPN OFF
    try:
        disconnect_vpn()
    except Exception as e:
        send_ntfy_alert(f'Ошибка отключения VPN: {e}', title='VPN Warning',
                        priority='default', tags='warning')

    if not ok:
        msg = f'[ai_using] Извлечение не удалось. Выход.'
        print(msg)
        send_ntfy_alert(msg, title='AI Using Failed', priority='urgent', tags='warning')
        sys.exit(1)

    # Шаг 3: load → target CH
    ok = load_phase()

    # Шаг 4: страховочная проверка дублей — всегда, независимо от ok
    dedup_check()

    if ok:
        send_ntfy_alert(
            f'AI Using: обновление ai_using_studies завершено ({DAYS_TO_SYNC} дней)!',
            title='AI Using Done', priority='high', tags='white_check_mark',
            topic_override='push_mrc_dashboards_7895',
        )
        print('[ai_using] Готово.')
    else:
        send_ntfy_alert(
            'AI Using: загрузка завершилась с ошибкой!',
            title='AI Using Failed', priority='urgent', tags='warning',
        )
        sys.exit(1)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='AI using studies — загрузка в dwh_test_db')
    parser.add_argument('--days', type=int, default=DAYS_TO_SYNC,
                        help=f'Глубина анализа в днях (по умолчанию {DAYS_TO_SYNC})')
    args = parser.parse_args()
    DAYS_TO_SYNC = args.days
    main()
