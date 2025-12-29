# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import io
import math
import re
import warnings
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import openpyxl
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

# -------------------------
# Пути к данным по умолчанию (относительно rao.py)
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
RKN_XLSX_DEFAULT = BASE_DIR / "data" / "Таблица РКН.xlsx"
VARS_XLSX_DEFAULT = BASE_DIR / "data" / "Переменные из ставок.xlsx"

# =========================
# ПАРСИНГ/ХЕЛПЕРЫ
# =========================
def parse_inn(raw: str) -> str:
    s = re.sub(r"\D+", "", raw or "")
    if len(s) not in (10, 12):
        raise ValueError("ИНН должен состоять из 10 или 12 цифр.")
    return s


def parse_number(s: Any) -> float:
    """
    Читает число со пробелами/неразрывными пробелами/запятыми.
    Принимает и float/int.
    """
    if s is None:
        raise ValueError("Пустое число")
    if isinstance(s, (int, float)) and not (isinstance(s, float) and math.isnan(s)):
        return float(s)
    x = str(s).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if x == "":
        raise ValueError("Пустое число")
    return float(x)


def parse_int_like(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return int(round(v))
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return int(round(float(s)))


def parse_population(v: Any) -> Tuple[Optional[int], List[str]]:
    notes: List[str] = []
    if v is None:
        return None, notes

    if isinstance(v, int):
        return int(v), notes

    if isinstance(v, float):
        if math.isnan(v):
            return None, notes
        if v < 10000 and abs(v - round(v)) > 1e-9:
            notes.append("Население указано дробным без единиц; принято как «тыс.» (×1000).")
            return int(round(v * 1000)), notes
        return int(round(v)), notes

    s = str(v).strip().lower().replace("\u00a0", " ")
    if not s:
        return None, notes

    mult = 1
    if "млн" in s:
        mult = 1_000_000
    elif "тыс" in s:
        mult = 1_000

    num = re.sub(r"[^0-9,.\- ]+", "", s).replace(" ", "").replace(",", ".")
    if not num:
        return None, notes

    try:
        f = float(num)
    except ValueError:
        return None, notes

    if mult == 1 and f < 10000 and abs(f - round(f)) > 1e-9:
        notes.append("Население было дробным без единиц; принято как «тыс.» (×1000).")
        mult = 1_000

    return int(round(f * mult)), notes


def parse_hours_week(brcst_time: Any, smi_name: Any) -> Tuple[Optional[float], List[str]]:
    notes: List[str] = []
    if brcst_time is not None and str(brcst_time).strip() != "":
        s = str(brcst_time).strip().lower()
        if "кругл" in s:
            return 168.0, notes
        n = parse_int_like(s)
        if n is not None:
            return float(n), notes

    if smi_name:
        m = re.search(r"\((\d{1,3})\)", str(smi_name))
        if m:
            return float(int(m.group(1))), notes

    return None, notes


def normalize_media(sreda: Any) -> str:
    s = (str(sreda or "")).lower()
    has_air = ("эфир" in s) or ("назем" in s)
    has_cable = ("кабель" in s)
    has_univ = ("универс" in s)
    if has_univ or (has_air and has_cable):
        return "Одновременно в эфире и по кабелю"
    return "В эфире или по кабелю"


def clean_channel_name(name: Any) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    return re.sub(r"\s*\(\d{1,3}\)\s*$", "", s).strip()


def build_rkn_url(license_id: str) -> str:
    return "https://rkn.gov.ru/activity/mass-media/for-broadcasters/teleradio/?id=" + quote(
        str(license_id), safe=""
    )


# =========================
# МОДЕЛИ
# =========================
@dataclass
class TopicShare:
    topic_raw: str
    share_pct: Optional[float]
    rate_pct: float
    note: Optional[str] = None


@dataclass
class Channel:
    name: str
    hours_week: Optional[float]
    topics: List[TopicShare] = field(default_factory=list)

    def avg_rate(self) -> Tuple[float, List[str]]:
        notes: List[str] = []
        if not self.topics:
            notes.append("Тематики не найдены; применена ставка 2,5%.")
            return 2.5, notes

        shares = [t for t in self.topics if t.share_pct is not None]
        if shares:
            for t in shares:
                if t.share_pct > 50:
                    notes.append(f"Преобладающая тематика: «{t.topic_raw}» ({t.share_pct}%).")
                    return t.rate_pct, notes
            total = sum(t.share_pct for t in shares)
            if total > 0:
                wavg = sum(t.share_pct * t.rate_pct for t in shares) / total
                notes.append("Ставка рассчитана как взвешенное среднее по долям.")
                return wavg, notes

        avg = sum(t.rate_pct for t in self.topics) / len(self.topics)
        notes.append("Долей нет/неполные; ставка — простое среднее.")
        return avg, notes


@dataclass
class License:
    license_id: str
    org_name: str
    inn: str
    media_raw: str
    media_class: str
    population_total: Optional[int]
    population_notes: List[str] = field(default_factory=list)
    rkn_url: str = ""
    channels: List[Channel] = field(default_factory=list)

    def total_hours(self) -> float:
        hrs = [c.hours_week for c in self.channels if c.hours_week is not None]
        return float(sum(hrs)) if hrs else 168.0


# =========================
# СТАВКИ И ТАБЛИЦЫ
# =========================
DEFAULT_TOPIC_RATE = 2.5


def build_category_rate_map(vars_xlsx: Path) -> Dict[str, float]:
    df = pd.read_excel(vars_xlsx, sheet_name="Категории и ставки")
    out: Dict[str, float] = {}
    for _, r in df.iterrows():
        cat = str(r.get("Категория использования произведений (по Приложению 1)", "")).strip()
        rate = r.get("Ставка авторского вознаграждения, процентов от дохода или расходов")
        if cat and pd.notna(rate):
            out[cat] = float(rate)
    return out


def topic_to_rate(topic: str, category_rate: Dict[str, float], mapping_df: Optional[pd.DataFrame]) -> Tuple[float, List[str]]:
    notes: List[str] = []
    t = (topic or "").strip()
    tl = t.lower()

    if mapping_df is not None and not mapping_df.empty:
        col_cat = "Категория тематики использования произведений по Приложению 1"
        col_topic = "Формулировка тематики вещания в лицензии пользователя"
        if col_cat in mapping_df.columns and col_topic in mapping_df.columns:
            m = mapping_df[mapping_df[col_topic].astype(str).str.lower().str.strip() == tl]
            if not m.empty:
                cat = str(m.iloc[0][col_cat]).strip()
                rate = category_rate.get(cat)
                if rate is not None:
                    notes.append(f"Тематика сопоставлена по таблице: категория {cat}.")
                    return float(rate), notes

    def hit(*keys: str) -> bool:
        return any(k in tl for k in keys)

    if ("информац" in tl and "развлек" in tl) or ("информационно-развлекатель" in tl):
        rate = category_rate.get("IV", 2.7)
        notes.append("Информационно-развлекательная (IV).")
        return rate, notes
    if hit("информац", "новост", "аналит"):
        rate = category_rate.get("I", 2.0)
        notes.append("Информационная (I).")
        return rate, notes
    if hit("культур", "просвет", "познав", "документ"):
        rate = category_rate.get("III", 2.5)
        notes.append("Культурно-просветительная (III).")
        return rate, notes
    if hit("спорт", "образоват", "здоров", "зож", "дет", "научн"):
        rate = category_rate.get("II", 2.3)
        notes.append("Соц-полезная/спорт/ЗОЖ (II).")
        return rate, notes
    if hit("развлек", "юмор", "шоу", "игр"):
        rate = category_rate.get("IV", 2.7)
        notes.append("Развлекательная (IV).")
        return rate, notes
    if hit("музык", "клип", "концерт"):
        rate = category_rate.get("V", 3.0)
        notes.append("Музыкальная (V).")
        return rate, notes

    notes.append("Тематика не распознана; ставка 2,5% (III).")
    return DEFAULT_TOPIC_RATE, notes


# =========================
# ЗАГРУЗКА РКН
# =========================
def iter_rkn_rows(rkn_xlsx: Path) -> Tuple[List[str], Any]:
    wb = openpyxl.load_workbook(rkn_xlsx, read_only=True, data_only=True)
    ws = wb.active
    header_raw = list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True)))
    while header_raw and (header_raw[-1] is None or str(header_raw[-1]).strip() == ""):
        header_raw.pop()
    header = header_raw
    max_col = len(header)
    it = ws.iter_rows(min_row=2, max_col=max_col, values_only=True)
    return header, it


def load_licenses_by_inn(rkn_xlsx: Path, inn: str, vars_xlsx: Path) -> Tuple[List[License], List[str]]:
    notes: List[str] = []
    header, it = iter_rkn_rows(rkn_xlsx)
    idx = {h: i for i, h in enumerate(header)}

    required = [
        "ns1:inn", "ns1:org_name", "ns1:license_num", "ns1:sreda", "ns1:population",
        "ns1:smi_name14", "ns1:smi_name", "ns1:brcst_direction", "ns1:percentage", "ns1:brcst_time",
    ]
    missing = [c for c in required if c not in idx]
    if missing:
        notes.append(f"В таблице РКН отсутствует часть колонок: {missing}. Используем доступные данные.")

    category_rate = build_category_rate_map(vars_xlsx)
    try:
        topics_map = pd.read_excel(vars_xlsx, sheet_name="Тематики по категориям")
        if topics_map.dropna(how="all").shape[0] <= 1 and topics_map.isna().all(axis=None):
            topics_map = pd.DataFrame()
    except Exception:
        topics_map = pd.DataFrame()

    by_license: Dict[str, Dict[str, Any]] = {}

    def get(row, col):
        j = idx.get(col)
        if j is None or j >= len(row):
            return None
        return row[j]

    for row in it:
        row_inn = str(get(row, "ns1:inn") or "").strip()
        if row_inn != inn:
            continue

        org_name = str(get(row, "ns1:org_name") or "").strip()
        lic_id = str(get(row, "ns1:license_num") or "").strip()
        sreda = str(get(row, "ns1:sreda") or "").strip()
        pop_raw = get(row, "ns1:population")
        smi14 = clean_channel_name(get(row, "ns1:smi_name14"))
        smi = clean_channel_name(get(row, "ns1:smi_name"))
        channel_name = smi14 or smi or "Неизвестный канал"
        brcst_time = get(row, "ns1:brcst_time")
        direction = str(get(row, "ns1:brcst_direction") or "").strip()
        perc = get(row, "ns1:percentage")

        if not lic_id:
            continue

        lic = by_license.setdefault(
            lic_id,
            {"org_name": org_name, "inn": inn, "sreda": sreda, "pop_values": [], "pop_notes": [], "channels": {}},
        )

        pop_int, pop_notes = parse_population(pop_raw)
        if pop_int is not None:
            lic["pop_values"].append(pop_int)
        lic["pop_notes"].extend(pop_notes)

        ch = lic["channels"].setdefault(channel_name, {"hours": None, "hours_notes": [], "topics": []})
        hrs, hrs_notes = parse_hours_week(brcst_time, get(row, "ns1:smi_name"))
        if hrs is not None:
            ch["hours"] = hrs
        ch["hours_notes"].extend(hrs_notes)

        if direction:
            share = None
            if perc is not None and str(perc).strip() != "":
                try:
                    share = float(str(perc).replace(",", "."))
                except ValueError:
                    share = None

            rate, rate_notes = topic_to_rate(direction, category_rate, topics_map)
            note = "; ".join(rate_notes) if rate_notes else None
            ch["topics"].append(TopicShare(topic_raw=direction, share_pct=share, rate_pct=rate, note=note))

    licenses: List[License] = []
    for lic_id, data in by_license.items():
        media_class = normalize_media(data.get("sreda"))
        pop_total = int(sum(sorted(set(data["pop_values"])))) if data["pop_values"] else None

        license_obj = License(
            license_id=lic_id,
            org_name=data.get("org_name", ""),
            inn=inn,
            media_raw=data.get("sreda", ""),
            media_class=media_class,
            population_total=pop_total,
            population_notes=data.get("pop_notes", []),
            rkn_url=build_rkn_url(lic_id),
            channels=[],
        )

        for ch_name, ch_data in data["channels"].items():
            license_obj.channels.append(Channel(name=ch_name, hours_week=ch_data.get("hours"), topics=ch_data.get("topics", [])))

        licenses.append(license_obj)

    if not licenses:
        notes.append("По указанному ИНН записи в выгрузке РКН не найдены.")
    return licenses, notes


# =========================
# РАСЧЁТЫ
# =========================
def round_rate(x: float) -> float:
    return round(x + 1e-9, 1)


def compute_license_rate(lic: License) -> Tuple[float, Dict[str, Any]]:
    det: Dict[str, Any] = {"channels": []}
    num = den = 0.0
    for ch in lic.channels:
        ch_rate, ch_notes = ch.avg_rate()
        hrs = ch.hours_week if ch.hours_week is not None else 168.0
        det["channels"].append(
            {
                "channel": ch.name,
                "hours": hrs,
                "channel_rate_raw": ch_rate,
                "channel_rate": round_rate(ch_rate),
                "notes": ch_notes,
                "topics": [{"topic": t.topic_raw, "share_pct": t.share_pct, "rate_pct": t.rate_pct, "note": t.note} for t in ch.topics],
            }
        )
        num += ch_rate * hrs
        den += hrs
    if den == 0:
        return 2.5, {"warning": "Нет часов/каналов; принята 2,5%."}
    return round_rate(num / den), det


def compute_contract_rate(licenses: List[License]) -> Tuple[float, Dict[str, Any]]:
    details: Dict[str, Any] = {"licenses": []}
    num = den = 0.0
    for lic in licenses:
        lic_rate, lic_rate_details = compute_license_rate(lic)
        pop = lic.population_total
        hrs = lic.total_hours()
        w = (pop or 0) * hrs
        details["licenses"].append(
            {
                "license_id": lic.license_id,
                "license_rate": lic_rate,
                "population": pop,
                "hours": hrs,
                "weight": w,
                "license_rate_details": lic_rate_details,
            }
        )
        if pop is None:
            continue
        num += lic_rate * w
        den += w
    if den == 0:
        return 2.5, {"warning": "Нет населения; принята 2,5%."}
    return round_rate(num / den), details


def compute_percent_sum_q(
    contract_rate: float,
    annual_revenue: Optional[float],
    revenue_q: Optional[float],
    expenses_q: Optional[float],
) -> Tuple[Optional[float], Dict[str, Any], List[str]]:
    notes: List[str] = []
    det: Dict[str, Any] = {"base_type": None, "base_q": None}

    base_q = None
    if revenue_q is not None:
        base_q = revenue_q
        det["base_type"] = "доходы (квартал)"
        notes.append("База: доходы за квартал.")
    elif annual_revenue is not None:
        base_q = annual_revenue / 4.0
        det["base_type"] = "доходы (год/4)"
        notes.append("База: годовая выручка (деление на 4).")
    elif expenses_q is not None:
        base_q = expenses_q
        det["base_type"] = "расходы (квартал)"
        notes.append("База: расходы (ветка 100% госструктуры).")
    else:
        notes.append("Не задана финансовая база.")
        return None, det, notes

    det["base_q"] = base_q
    return round(base_q * (contract_rate / 100.0), 2), det, notes


def lookup_min_sum(mins_df: pd.DataFrame, population: int, media_class: str) -> Optional[float]:
    sub = mins_df[
        (mins_df["Среда осуществления вещания (в эфире, по кабелю, одновременно в эфире и по кабелю)"].astype(str).str.strip() == media_class)
    ].copy()
    if sub.empty:
        return None

    for _, r in sub.iterrows():
        lo = int(r["Численность населения на территории вещания, от (человек)"])
        hi = r["Численность населения на территории вещания, до (человек)"]
        hi_val = int(hi) if pd.notna(hi) else None
        if population >= lo and (hi_val is None or population <= hi_val):
            return float(r["Минимальная сумма авторского вознаграждения за квартал, рублей"])
    return None


def hour_coeff(hours_df: pd.DataFrame, hours_week: float) -> float:
    for _, r in hours_df.iterrows():
        lo = float(r["Количество часов вещания в неделю, от"])
        hi = float(r["Количество часов вещания в неделю, до"])
        if lo <= hours_week <= hi:
            return float(r["Коэффициент к установленной минимальной сумме вознаграждения"])
    return 1.0


def discount_by_licenses(disc_df: pd.DataFrame, n_licenses: int) -> float:
    for _, r in disc_df.iterrows():
        lo = int(r["Минимальное количество вещательных лицензий одного пользователя"])
        hi = r["Максимальное количество вещательных лицензий одного пользователя"]
        hi_val = int(hi) if pd.notna(hi) else None
        if n_licenses >= lo and (hi_val is None or n_licenses <= hi_val):
            return 1.0 - float(r["Размер скидки к совокупной минимальной сумме вознаграждения, процентов"]) / 100.0
    return 1.0


def contract_period_coeff(period_df: pd.DataFrame, contract_quarter: int) -> float:
    for _, r in period_df.iterrows():
        lo = int(r["Отчетный период действия лицензионного договора, начиная с (номер квартала)"])
        hi = int(r["Отчетный период действия лицензионного договора, по (номер квартала включительно)"])
        if lo <= contract_quarter <= hi:
            return float(r["Коэффициент к минимальной сумме вознаграждения в указанный период"])
    return 1.0


def compute_min_total(
    licenses: List[License],
    vars_xlsx: Path,
    annual_income_for_rules: Optional[float],
    contract_quarter: int,
    internet_resources: int,
    past_year_percent_paid: Optional[float],
    percent_sum_q: Optional[float],
    contract_media: str = "auto",
    use_small_income_branch: Optional[bool] = None,
    new_user_only: bool = False,
) -> Tuple[Optional[float], Dict[str, Any], List[str]]:
    notes: List[str] = []
    details: Dict[str, Any] = {"steps": []}

    mins_df = pd.read_excel(vars_xlsx, sheet_name="Минимальные суммы по населению")
    disc_df = pd.read_excel(vars_xlsx, sheet_name="Скидки по количеству лицензий")
    hours_df = pd.read_excel(vars_xlsx, sheet_name="Коэффициенты по часам")
    period_df = pd.read_excel(vars_xlsx, sheet_name="Коэфф по периодам договора")
    params_df = pd.read_excel(vars_xlsx, sheet_name="Параметры для расчетов")

    def get_param_contains(substr: str, default: float) -> float:
        sub = params_df[
            params_df["Наименование параметра для расчета авторского вознаграждения"]
            .astype(str)
            .str.contains(substr, case=False, na=False)
        ]
        return default if sub.empty else float(sub.iloc[0]["Значение параметра"])

    THRESH_SMALL = get_param_contains("Порог годового дохода", 1_500_000.0)
    SMALL_K = get_param_contains("Коэффициент уменьшения", 0.5)
    SMALL_MAX_Q = int(get_param_contains("Максимальное количество отчетных периодов применения половины", 8))
    INTERNET_PCT = get_param_contains("Дополнительный процент увеличения", 0.15)
    INTERNET_MIN_ADD = get_param_contains("Минимальное увеличение", 12500.0)
    GUILLOTINE_PCT = get_param_contains("Порог превышения", 0.1)

    pops_missing = [lic.license_id for lic in licenses if lic.population_total is None]
    if pops_missing:
        notes.append(f"Не найдена численность населения по лицензиям: {pops_missing}. Расчёт минималки может быть неполным.")

    media_classes = [lic.media_class for lic in licenses]
    has_two_media = "Одновременно в эфире и по кабелю" in media_classes or (
        any("эфир" in (lic.media_raw or "").lower() or "назем" in (lic.media_raw or "").lower() for lic in licenses)
        and any("кабель" in (lic.media_raw or "").lower() for lic in licenses)
    )
    media_for_agg = "Одновременно в эфире и по кабелю" if has_two_media else "В эфире или по кабелю"

    contract_media = (contract_media or "auto").lower().strip()
    if contract_media in ("cable", "air"):
        has_two_media = False
        media_for_agg = "В эфире или по кабелю"
        notes.append("Среда договора: «В эфире или по кабелю».")
    elif contract_media == "both":
        has_two_media = True
        media_for_agg = "Одновременно в эфире и по кабелю"
        notes.append("Среда договора: «Одновременно в эфире и по кабелю».")

    small_branch = use_small_income_branch if use_small_income_branch is not None else (
        (annual_income_for_rules is not None) and (annual_income_for_rules <= THRESH_SMALL) and (contract_quarter <= SMALL_MAX_Q)
    )

    min_total = 0.0
    if small_branch:
        N_sum = sum(int(lic.population_total) for lic in licenses if lic.population_total is not None)
        if N_sum <= 0:
            return None, details, notes + ["Для ветки малого дохода требуется суммарная численность населения."]
        m = lookup_min_sum(mins_df, N_sum, media_for_agg)
        if m is None:
            return None, details, notes + ["Не удалось определить строку по суммарной численности."]
        min_total = SMALL_K * m
        details["steps"].append({"step": "C3", "N_sum": N_sum, "media": media_for_agg, "min_table": m, "k_small": SMALL_K, "min_after": min_total})
        notes.append("Применена ветка малого дохода (минимальная сумма по суммарной численности × 0,5).")
    else:
        per_lic = []
        for lic in licenses:
            if lic.population_total is None:
                continue
            media_for_min = (
                "В эфире или по кабелю" if contract_media in ("cable", "air")
                else ("Одновременно в эфире и по кабелю" if contract_media == "both" else lic.media_class)
            )
            m = lookup_min_sum(mins_df, int(lic.population_total), media_for_min)
            if m is None:
                notes.append(f"Не найдена минималка для ВЛ {lic.license_id} (население={lic.population_total}, среда={lic.media_class}).")
                continue
            hrs = lic.total_hours()
            coeff = hour_coeff(hours_df, hrs) if hrs < 126 else 1.0
            m2 = m * coeff
            per_lic.append({"license_id": lic.license_id, "population": lic.population_total, "media": media_for_min, "min_table": m, "hours_week": hrs, "hour_coeff": coeff, "min_after": m2})
            min_total += m2
        details["steps"].append({"step": "C1+C2(+C7)", "per_license": per_lic, "min_after": min_total})

    n_lic = len(licenses)
    if n_lic > 3:
        k = discount_by_licenses(disc_df, n_lic)
        min_total *= k
        details["steps"].append({"step": "C4", "n_licenses": n_lic, "coeff": k, "min_after": min_total})
        notes.append("Применена скидка по количеству вещательных лицензий.")

    # ВАЖНО: коэффициент периода договора — ТОЛЬКО для новых пользователей
    k_period = contract_period_coeff(period_df, contract_quarter) if new_user_only else 1.0
    if new_user_only and k_period != 1.0:
        min_total *= k_period
        details["steps"].append({"step": "E1(period, new user)", "contract_quarter": contract_quarter, "coeff": k_period, "min_after": min_total})
        notes.append("Применён стимулирующий коэффициент периода (только для новых пользователей).")

    if internet_resources and internet_resources > 0:
        add_per = max(INTERNET_PCT * min_total, INTERNET_MIN_ADD)
        delta = add_per * internet_resources
        min_total += delta
        details["steps"].append({"step": "C6", "resources": internet_resources, "add_per_resource": add_per, "delta": delta, "min_after": min_total})
        notes.append("Добавлена надбавка за интернет-вещание.")

    if percent_sum_q is not None and min_total > (1.0 + GUILLOTINE_PCT) * percent_sum_q:
        details["steps"].append({"step": "D1", "condition": f"min_total > {(1.0 + GUILLOTINE_PCT):.2f} * percent_sum_q", "min_total": min_total, "percent_sum_q": percent_sum_q})
        notes.append("Сработало правило «гильотины»: минимальная сумма превышает значение по проценту более чем на 10%.")

        N_sum = sum(int(lic.population_total) for lic in licenses if lic.population_total is not None)
        if N_sum > 0:
            alt1 = lookup_min_sum(mins_df, N_sum, media_for_agg)
            if alt1 is not None:
                alt = alt1
                if n_lic > 3:
                    alt *= discount_by_licenses(disc_df, n_lic)
                if new_user_only and k_period != 1.0:
                    alt *= k_period
                if internet_resources and internet_resources > 0:
                    add_per = max(INTERNET_PCT * alt, INTERNET_MIN_ADD)
                    alt += add_per * internet_resources

                details["steps"].append({"step": "D2", "N_sum": N_sum, "min_table": alt1, "min_after_adjust": alt})
                if alt <= (1.0 + GUILLOTINE_PCT) * percent_sum_q:
                    min_total = alt
                    notes.append("«Гильотина»: принята корректировка по суммарной численности населения.")
                else:
                    if past_year_percent_paid is not None:
                        min_total = 0.25 * float(past_year_percent_paid)
                        details["steps"].append({"step": "D3", "S_year": past_year_percent_paid, "k": 0.25, "min_after": min_total})
                        notes.append("«Гильотина»: принято 1/4 от фактических платежей за прошедший год.")
                    else:
                        notes.append("Для шага D3 требуется сумма фактических годовых платежей по проценту (S_год).")
            else:
                notes.append("Не найдена строка по суммарной численности населения (таблица минималок).")
        else:
            notes.append("Нет суммарной численности — завершить «гильотину» невозможно.")

    return round(min_total, 2), details, notes


# =========================
# ОТЧЁТ
# =========================
def money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}".replace(",", " ").replace(".00", "")


def format_report(
    inn: str,
    annual_revenue: Optional[float],
    revenue_q: Optional[float],
    expenses_q: Optional[float],
    internet_resources: int,
    contract_quarter: int,
    licenses: List[License],
    contract_rate: float,
    contract_rate_details: Dict[str, Any],
    percent_sum_q: Optional[float],
    percent_details: Dict[str, Any],
    min_total: Optional[float],
    min_details: Dict[str, Any],
    notes: List[str],
    needs: List[str],
    new_user_only: bool,
) -> Tuple[str, str]:
    lines: List[str] = []
    org_name = licenses[0].org_name if licenses else "Организация не указана"
    lines.append(f"{org_name}, ИНН {inn}.")
    lines.append("")

    if annual_revenue is not None:
        lines.append(f"Выручка (за год): {money(annual_revenue)} ₽. База квартала = год/4.")
    elif revenue_q is not None:
        lines.append(f"Доходы за квартал: {money(revenue_q)} ₽.")
    elif expenses_q is not None:
        lines.append(f"Расходы за квартал: {money(expenses_q)} ₽ (ветка 100% госструктура).")
    else:
        lines.append("Финансовая база не указана.")

    lines.append("")
    lines.append(f"Вещательных лицензий: {len(licenses)} шт.")
    for lic in licenses:
        pop = lic.population_total
        pop_str = f"{pop:,}".replace(",", " ") if pop is not None else "не найдено"
        lines.append(f"— ВЛ: {lic.license_id}; среда: {lic.media_raw} → {lic.media_class}; население: {pop_str}.")
        if lic.rkn_url:
            lines.append(f"  РКН: {lic.rkn_url}")
        for ch in lic.channels[:10]:
            hrs = ch.hours_week if ch.hours_week is not None else 168.0
            ch_rate, _ = ch.avg_rate()
            lines.append(f"  • Канал: {ch.name}; часы в неделю: {hrs:g}; ставка канала: {round_rate(ch_rate):.1f}%.")
            for t in ch.topics[:8]:
                share = f"{t.share_pct:g}%" if t.share_pct is not None else "без доли"
                lines.append(f"    Тематика: {t.topic_raw} ({share}) → {t.rate_pct:.1f}%.")

    lines.append("")
    lines.append(f"Признак «новый пользователь»: {'да' if new_user_only else 'нет'}.")
    lines.append(f"Номер отчётного квартала (с начала договора): {contract_quarter}.")
    lines.append("")
    lines.append(f"Ставка по договору (взвешено по часам × населению): {contract_rate:.1f}%.")
    lines.append("")
    lines.append(f"Сумма по проценту за квартал: {money(percent_sum_q)} ₽.")
    lines.append("")
    lines.append(f"Минимальная сумма за квартал: {money(min_total)} ₽.")
    lines.append("")

    if (
        percent_details.get("contract_rate_override_pct") is not None
        or min_details.get("contract_min_override") is not None
        or percent_details.get("assoc_eatr")
    ):
        lines.append("Договорные условия и корректировки:")
        cr = percent_details.get("contract_rate_override_pct")
        cm = min_details.get("contract_min_override")
        lines.append(f"— Фиксированный процент по договору: {f'{cr:.2f}%' if cr is not None else '—'}")
        lines.append(f"— Фиксированный минимум за квартал: {money(cm)} ₽" if cm is not None else "— Фиксированный минимум за квартал: —")
        lines.append(f"— Членство в профильной организации: {'да (−15% от итога)' if percent_details.get('assoc_eatr') else 'нет'}")
        lines.append("")

    if percent_sum_q is not None and min_total is not None:
        pay = max(percent_sum_q, min_total)
        which = "по проценту" if percent_sum_q >= min_total else "по минимальной сумме"
        if percent_details.get("assoc_eatr"):
            lines.append(f"Итог к оплате (до коэфф. профильной организации): {money(pay)} ₽ ({which}).")
            lines.append(f"Итог к оплате с учётом профильной организации (−15%): {money(pay * 0.85)} ₽.")
        else:
            lines.append(f"Итог к оплате: {money(pay)} ₽ ({which}).")
    else:
        lines.append("Итог не сформирован — отсутствуют необходимые входные данные.")

    lines.append("")
    if internet_resources:
        lines.append(f"Интернет-ресурсы со стримингом: {internet_resources}.")
        lines.append("")
    if needs:
        lines.append("Требуется уточнить/проверить:")
        for x in needs:
            lines.append(f"— {x}")
        lines.append("")
    if notes:
        lines.append("Примечания и допущения:")
        for n in notes:
            lines.append(f"— {n}")
        lines.append("")

    return org_name, "\n".join(lines)


# =========================
# CLI / API-runner
# =========================
def _opt_float(x: Optional[str]) -> Optional[float]:
    if x is None:
        return None
    try:
        return parse_number(x)
    except Exception:
        raise ValueError(f"Некорректное число: {x}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--inn", required=True, help="ИНН (10/12 цифр)")

    # числа принимаем как str, чтобы поддержать пробелы/запятые (и для сайта тоже)
    ap.add_argument("--annual_revenue", type=str, default=None)
    ap.add_argument("--revenue_q", type=str, default=None)
    ap.add_argument("--expenses_q", type=str, default=None)

    ap.add_argument("--internet_resources", type=int, default=0)
    ap.add_argument("--contract_quarter", type=int, default=1)
    ap.add_argument("--contract_media", type=str, default="auto", choices=["auto", "cable", "air", "both"])

    ap.add_argument("--only_license", type=str, default=None)
    ap.add_argument("--past_year_percent_paid", type=str, default=None)

    ap.add_argument("--rkn_xlsx", type=str, default=str(RKN_XLSX_DEFAULT))
    ap.add_argument("--vars_xlsx", type=str, default=str(VARS_XLSX_DEFAULT))

    ap.add_argument("--force_small_income", action="store_true")
    ap.add_argument("--no_small_income", action="store_true")

    ap.add_argument("--population_override", type=int, default=None)
    ap.add_argument("--new_user", action="store_true")

    # договорные / ассоциации / ручной режим
    ap.add_argument("--contract_rate_override_pct", type=str, default=None)
    ap.add_argument("--contract_min_override", type=str, default=None)
    ap.add_argument("--assoc_eatr", action="store_true")

    ap.add_argument("--manual_mode", action="store_true")
    ap.add_argument("--hours_week_manual", type=str, default=None)
    ap.add_argument("--topics_manual_csv", type=str, default=None)

    args = ap.parse_args(argv)

    inn = parse_inn(args.inn)
    rkn_xlsx = Path(args.rkn_xlsx)
    vars_xlsx = Path(args.vars_xlsx)

    if not rkn_xlsx.exists():
        raise FileNotFoundError(f"Не найден файл: {rkn_xlsx}")
    if not vars_xlsx.exists():
        raise FileNotFoundError(f"Не найден файл: {vars_xlsx}")

    annual_revenue = _opt_float(args.annual_revenue)
    revenue_q = _opt_float(args.revenue_q)
    expenses_q = _opt_float(args.expenses_q)
    past_year_percent_paid = _opt_float(args.past_year_percent_paid)
    contract_rate_override_pct = _opt_float(args.contract_rate_override_pct)
    contract_min_override = _opt_float(args.contract_min_override)
    hours_week_manual = _opt_float(args.hours_week_manual)

    licenses, load_notes = load_licenses_by_inn(rkn_xlsx, inn, vars_xlsx)

    if args.only_license:
        licenses = [x for x in licenses if x.license_id == args.only_license]

    # переопределение населения
    if args.population_override is not None and licenses:
        pop_val = int(args.population_override)
        for lic in licenses:
            lic.population_total = pop_val

    # ручной режим, если РКН не найден (или отфильтровано в пустоту)
    if args.manual_mode and (not licenses):
        media_class_map = {
            "cable": "В эфире или по кабелю",
            "air": "В эфире или по кабелю",
            "both": "Одновременно в эфире и по кабелю",
            "auto": "В эфире или по кабелю",
        }
        media_class = media_class_map[args.contract_media]
        pop = int(args.population_override or 0) if args.population_override is not None else None
        hours = float(hours_week_manual or 168.0)

        topics_list: List[TopicShare] = []
        cat_map = build_category_rate_map(vars_xlsx)
        try:
            topics_map = pd.read_excel(vars_xlsx, sheet_name="Тематики по категориям")
        except Exception:
            topics_map = pd.DataFrame()

        if args.topics_manual_csv:
            for t in [x.strip() for x in args.topics_manual_csv.split(",") if x.strip()]:
                r, _ = topic_to_rate(t, cat_map, topics_map)
                topics_list.append(TopicShare(topic_raw=t, share_pct=None, rate_pct=float(r)))

        ch = Channel(name="Канал (введено вручную)", hours_week=hours, topics=topics_list)
        lic = License(
            license_id="MANUAL",
            org_name="(введено вручную)",
            inn=inn,
            media_raw=media_class,
            media_class=media_class,
            population_total=pop,
            population_notes=[],
            rkn_url="",
            channels=[ch],
        )
        licenses = [lic]

    needs: List[str] = []
    notes: List[str] = []
    notes.extend(load_notes)

    if not licenses:
        print("Нет данных по ИНН в таблице РКН.")
        return 2

    if all(lic.population_total is None for lic in licenses):
        needs.append("В РКН-таблице не заполнено население. Возьмите численность из карточек РКН по ссылкам.")

    contract_rate, contract_rate_details = compute_contract_rate(licenses)

    if contract_rate_override_pct is not None:
        contract_rate = float(contract_rate_override_pct)

    percent_sum_q, percent_details, percent_notes = compute_percent_sum_q(
        contract_rate=contract_rate,
        annual_revenue=annual_revenue,
        revenue_q=revenue_q,
        expenses_q=expenses_q,
    )
    notes.extend(percent_notes)
    if percent_sum_q is None:
        needs.append("Нужна финансовая база: annual_revenue (год) или revenue_q (квартал) или expenses_q (расходы квартала).")

    annual_income_for_rules = (
        float(annual_revenue) if annual_revenue is not None
        else (float(revenue_q) * 4.0 if revenue_q is not None else None)
    )

    use_small_income = None
    if args.force_small_income and args.no_small_income:
        raise ValueError("Нельзя одновременно --force_small_income и --no_small_income")
    if args.force_small_income:
        use_small_income = True
    if args.no_small_income:
        use_small_income = False

    min_total, min_details, min_notes = compute_min_total(
        licenses=licenses,
        vars_xlsx=vars_xlsx,
        annual_income_for_rules=annual_income_for_rules,
        contract_quarter=args.contract_quarter,
        internet_resources=args.internet_resources,
        past_year_percent_paid=past_year_percent_paid,
        percent_sum_q=percent_sum_q,
        contract_media=args.contract_media,
        use_small_income_branch=use_small_income,
        new_user_only=bool(args.new_user),
    )
    notes.extend(min_notes)

    if contract_min_override is not None:
        min_total = float(contract_min_override)

    if percent_sum_q is not None and min_total is not None:
        pay_gross = max(percent_sum_q, min_total)
    elif percent_sum_q is not None:
        pay_gross = percent_sum_q
    else:
        pay_gross = min_total

    assoc_coeff = 0.85 if args.assoc_eatr else 1.0
    pay_final = round(pay_gross * assoc_coeff, 2) if pay_gross is not None else None

    percent_details["contract_rate_override_pct"] = contract_rate_override_pct
    min_details["contract_min_override"] = contract_min_override
    percent_details["assoc_eatr"] = bool(args.assoc_eatr)

    org_name, report_text = format_report(
        inn=inn,
        annual_revenue=annual_revenue,
        revenue_q=revenue_q,
        expenses_q=expenses_q,
        internet_resources=args.internet_resources,
        contract_quarter=args.contract_quarter,
        licenses=licenses,
        contract_rate=contract_rate,
        contract_rate_details=contract_rate_details,
        percent_sum_q=percent_sum_q,
        percent_details=percent_details,
        min_total=min_total,
        min_details=min_details,
        notes=notes,
        needs=needs,
        new_user_only=bool(args.new_user),
    )

    if pay_final is not None and args.assoc_eatr:
        report_text = report_text.rstrip() + f"\nИтог к оплате с учётом профильной организации (−15%): {money(pay_final)} ₽.\n"

    print(report_text)
    return 0


def run_calc_capture(argv: List[str]) -> Tuple[int, str]:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = int(main(argv))
    except SystemExit as e:
        code = int(getattr(e, "code", 1) or 0)
    return code, buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
