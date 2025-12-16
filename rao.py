# -*- coding: utf-8 -*-
from __future__ import annotations
# -*- coding: utf-8 -*-
import sys

"""
РАО ТВ/радио — расчёт по ИНН на основе:
- Таблица РКН.xlsx (выгрузка РКН)
- Переменные из ставок.xlsx (категории, ставки, минималки, коэффициенты)
- Логика по рассчетам.docx (карта развилок)

Цель: получить воспроизводимый отчёт «% от дохода, но не менее …/квартал»,
а если чего-то не хватает — вывести список "НУЖНЫ ДАННЫЕ/НУЖНА ПРОВЕРКА"
и подсказки, что именно искать в интернете.

Запуск:
  python rao_calc.py --inn 0326499787 --year 2024 --annual_revenue 33986000 --internet_resources 0 --contract_quarter 1

Если доходов нет и пользователь 100% госструктура:
  python rao_calc.py --inn ... --contract_quarter 1 --expenses_q 1234567

Файлы по умолчанию берутся рядом со скриптом. Можно переопределить путями:
  --rkn_xlsx "/path/Таблица РКН.xlsx" --vars_xlsx "/path/Переменные из ставок.xlsx"
"""
import argparse
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from typing import Dict, List, Optional, Tuple, Any
import openpyxl
import pandas as pd


# --------------------------- helpers: progress ---------------------------

class Progress:
    def __init__(self) -> None:
        self.total = 10
        self.step = 0

    def tick(self, msg: str) -> None:
        self.step = min(self.total, self.step + 1)
        filled = int((self.step / self.total) * 10)
        bar = "■" * filled + "□" * (10 - filled)
        pct = int((self.step / self.total) * 100)
        print(f"Прогресс: [{bar}] {pct}% — {msg}")


# --------------------------- parsing ---------------------------

def parse_inn(raw: str) -> str:
    s = re.sub(r"\D+", "", raw or "")
    if len(s) not in (10, 12):
        raise ValueError("ИНН должен состоять из 10 или 12 цифр.")
    return s

def parse_int_like(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int,)):
        return int(v)
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return int(round(v))
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("\u00a0", " ")
    s = s.replace(" ", "").replace(",", ".")
    m = re.match(r"^-?\d+(\.\d+)?$", s)
    if not m:
        return None
    f = float(s)
    return int(round(f))

def parse_population(v: Any) -> Tuple[Optional[int], List[str]]:
    """
    Возвращает (население_в_человеках, notes[]).
    Поддерживает форматы:
      "978 500 чел.", "978,5 тыс", "0,978 млн", 142.2, "60,0" и т.п.

    ВАЖНО: в выгрузках РКН иногда встречаются числа вида "60,0" без единиц.
    Здесь применяется эвристика: если число < 10000 и содержит дробную часть,
    считаем, что это "тыс. человек" (умножаем на 1000).
    """
    notes: List[str] = []
    if v is None:
        return None, notes

    # numeric
    if isinstance(v, (int,)):
        return int(v), notes
    if isinstance(v, float):
        if math.isnan(v):
            return None, notes
        # эвристика "тыс."
        if v < 10000 and abs(v - round(v)) > 1e-9:
            notes.append("Население было числом с дробной частью без единиц; применена эвристика «тыс.» (×1000).")
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

    # оставим только цифры/разделители
    num = re.sub(r"[^0-9,.\- ]+", "", s)
    num = num.replace(" ", "").replace(",", ".")
    if not num:
        return None, notes
    try:
        f = float(num)
    except ValueError:
        return None, notes

    if mult == 1 and f < 10000 and abs(f - round(f)) > 1e-9:
        notes.append("Население было дробным числом без единиц; применена эвристика «тыс.» (×1000).")
        mult = 1_000

    return int(round(f * mult)), notes


def parse_hours_week(brcst_time: Any, smi_name: Any) -> Tuple[Optional[float], List[str]]:
    """
    Часы вещания в неделю.
    Приоритет:
      1) ns1:brcst_time (если число/строка)
      2) число в скобках в ns1:smi_name (например «Канал» (168))
      3) 'круглосуточно' -> 168
    """
    notes: List[str] = []

    # 1) explicit
    if brcst_time is not None and str(brcst_time).strip() != "":
        s = str(brcst_time).strip().lower()
        if "кругл" in s:
            return 168.0, notes
        n = parse_int_like(s)
        if n is not None:
            return float(n), notes

    # 2) in name "(168)"
    if smi_name:
        m = re.search(r"\((\d{1,3})\)", str(smi_name))
        if m:
            return float(int(m.group(1))), notes

    # 3) fallback
    return None, notes


def normalize_media(sreda: Any) -> str:
    s = (str(sreda or "")).lower()
    # базовая классификация под таблицу минималок
    has_air = ("эфир" in s) or ("назем" in s)
    has_cable = ("кабель" in s)
    has_univ = ("универс" in s)
    if has_univ:
        return "Одновременно в эфире и по кабелю"
    if has_air and has_cable:
        return "Одновременно в эфире и по кабелю"
    return "В эфире или по кабелю"


def clean_channel_name(name: Any) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    # убрать "(168)" в конце
    s = re.sub(r"\s*\(\d{1,3}\)\s*$", "", s).strip()
    return s


# --------------------------- models ---------------------------

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
        """
        A2 по логике: ставка по телеканалу.
        - если есть доли и какая-то >50% -> ставка этой тематики
        - иначе: если доли есть -> взвешенное среднее
        - иначе: простое среднее
        """
        notes: List[str] = []
        if not self.topics:
            notes.append("Тематики не найдены; ставка по умолчанию 2,5%.")
            return 2.5, notes

        # доли
        shares = [t for t in self.topics if t.share_pct is not None]
        if shares:
            # преобладающая
            for t in shares:
                if t.share_pct > 50:
                    notes.append(f"Преобладающая тематика >50%: «{t.topic_raw}» ({t.share_pct}%).")
                    return t.rate_pct, notes

            total = sum(t.share_pct for t in shares)
            if total > 0:
                wavg = sum(t.share_pct * t.rate_pct for t in shares) / total
                notes.append("Ставка телеканала рассчитана как взвешенное среднее по долям тематик.")
                return wavg, notes

        # простое среднее
        avg = sum(t.rate_pct for t in self.topics) / len(self.topics)
        notes.append("Доли тематик отсутствуют/неполные; ставка телеканала рассчитана как простое среднее.")
        return avg, notes


@dataclass
class License:
    license_id: str               # ns1:license_num (в выгрузке РКН это ID)
    org_name: str
    inn: str
    media_raw: str
    media_class: str
    population_total: Optional[int]
    population_notes: List[str] = field(default_factory=list)
    rkn_url: str = ""
    channels: List[Channel] = field(default_factory=list)

    def total_hours(self) -> float:
        # суммарные часы по каналам; если нет данных -> 168 (допущение)
        hrs = [c.hours_week for c in self.channels if c.hours_week is not None]
        if hrs:
            # иногда в одной ВЛ несколько строк по одному каналу — но часы одинаковые; агрегируем как сумму уникальных
            return float(sum(hrs))
        return 168.0


# --------------------------- topic -> rate ---------------------------

DEFAULT_TOPIC_RATE = 2.5

def build_category_rate_map(vars_xlsx: Path) -> Dict[str, float]:
    df = pd.read_excel(vars_xlsx, sheet_name="Категории и ставки")
    out = {}
    for _, r in df.iterrows():
        cat = str(r.get("Категория использования произведений (по Приложению 1)", "")).strip()
        rate = r.get("Ставка авторского вознаграждения, процентов от дохода или расходов")
        if cat and pd.notna(rate):
            out[cat] = float(rate)
    return out


def topic_to_rate(topic: str, category_rate: Dict[str, float], mapping_df: Optional[pd.DataFrame]) -> Tuple[float, List[str]]:
    """
    A1 по логике: тематика -> категория -> ставка.
    Приоритет:
      1) явная таблица "Тематики по категориям" (если заполнена)
      2) встроенная эвристика по ключевым словам
      3) дефолт 2,5%
    """
    notes: List[str] = []
    t = (topic or "").strip()
    tl = t.lower()

    # 1) mapping table if non-empty
    if mapping_df is not None and not mapping_df.empty:
        # ожидаем колонки
        col_cat = "Категория тематики использования произведений по Приложению 1"
        col_topic = "Формулировка тематики вещания в лицензии пользователя"
        if col_cat in mapping_df.columns and col_topic in mapping_df.columns:
            m = mapping_df[mapping_df[col_topic].astype(str).str.lower().str.strip() == tl]
            if not m.empty:
                cat = str(m.iloc[0][col_cat]).strip()
                rate = category_rate.get(cat)
                if rate is not None:
                    notes.append(f"Тематика сопоставлена по таблице «Тематики по категориям»: категория {cat}.")
                    return float(rate), notes

    # 2) heuristic keywords (можно расширять)
    def hit(*keys: str) -> bool:
        return any(k in tl for k in keys)

    # ВАЖНО: «информационно-развлекательное» относим к развлекательному (2.7%),

    # а не к чисто информационному (2.0%).

    if ("информац" in tl and "развлек" in tl) or ("информационно-развлекатель" in tl):

        rate = category_rate.get("IV", 2.7)

        notes.append("Тематика распознана как «информационно-развлекательная» (категория IV).")

        return rate, notes


    if hit("информац", "новост", "аналит"):
        # категория I (2.0) в типовой схеме
        rate = category_rate.get("I", 2.0)
        notes.append("Тематика распознана эвристикой как «информационная» (категория I).")
        return rate, notes
    if hit("культур", "просвет", "познав", "документ"):
        rate = category_rate.get("III", 2.5)
        notes.append("Тематика распознана эвристикой как «культурно-просветительская» (категория III).")
        return rate, notes
    if hit("спорт", "образоват", "здоров", "зож", "дет", "научн"):
        rate = category_rate.get("II", 2.3)
        notes.append("Тематика распознана эвристикой как «социально-полезная/образовательная/спорт/ЗОЖ» (категория II).")
        return rate, notes
    if hit("развлек", "юмор", "шоу", "игр"):
        rate = category_rate.get("IV", 2.7)
        notes.append("Тематика распознана эвристикой как «развлекательная» (категория IV).")
        return rate, notes
    if hit("музык", "клип", "концерт"):
        rate = category_rate.get("V", 3.0)
        notes.append("Тематика распознана эвристикой как «музыкальная» (категория V).")
        return rate, notes

    # 3) default
    notes.append("Тематика не распознана; применена ставка по умолчанию 2,5% (категория III).")
    return DEFAULT_TOPIC_RATE, notes


# --------------------------- loading: RKN table ---------------------------

def iter_rkn_rows(rkn_xlsx: Path) -> Tuple[List[str], Any]:
    wb = openpyxl.load_workbook(rkn_xlsx, read_only=True, data_only=True)
    ws = wb.active

    # читаем заголовок ровно одной строкой
    header_raw = list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True)))
    # обрежем хвост пустых колонок, чтобы len(header) был адекватным
    while header_raw and (header_raw[-1] is None or str(header_raw[-1]).strip() == ""):
        header_raw.pop()
    header = header_raw
    max_col = len(header)

    # дальше читаем строки той же ширины, иначе у openpyxl бывают "короткие" tuple
    it = ws.iter_rows(min_row=2, max_col=max_col, values_only=True)
    return header, it

def build_rkn_url(license_id: str) -> str:
    # РКН принимает id= (нужно URL-кодирование для Л033-…/…)
    return "https://rkn.gov.ru/activity/mass-media/for-broadcasters/teleradio/?id=" + quote(str(license_id), safe="")

def load_licenses_by_inn(rkn_xlsx: Path, inn: str, vars_xlsx: Path) -> Tuple[List[License], List[str]]:
    notes: List[str] = []

    header, it = iter_rkn_rows(rkn_xlsx)
    idx = {h: i for i, h in enumerate(header)}

    required = ["ns1:inn", "ns1:org_name", "ns1:license_num", "ns1:sreda", "ns1:population", "ns1:smi_name14", "ns1:smi_name", "ns1:brcst_direction", "ns1:percentage", "ns1:brcst_time"]
    missing = [c for c in required if c not in idx]
    if missing:
        notes.append(f"В таблице РКН не найдены ожидаемые колонки: {missing}. Скрипт будет работать частично.")
    # mapping tables
    category_rate = build_category_rate_map(vars_xlsx)
    try:
        topics_map = pd.read_excel(vars_xlsx, sheet_name="Тематики по категориям")
        # если таблица пустая (NaN) — считаем как пустую
        if topics_map.dropna(how="all").shape[0] <= 1 and topics_map.isna().all(axis=None):
            topics_map = pd.DataFrame()
    except Exception:
        topics_map = pd.DataFrame()

    # group by license_id then channel
    by_license: Dict[str, Dict[str, Any]] = {}

    def get(row, col):
        j = idx.get(col)
        if j is None:
            return None
        if j >= len(row):
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

        lic = by_license.setdefault(lic_id, {
            "org_name": org_name,
            "inn": inn,
            "sreda": sreda,
            "pop_values": [],
            "pop_notes": [],
            "channels": {}
        })

        pop_int, pop_notes = parse_population(pop_raw)
        if pop_int is not None:
            lic["pop_values"].append(pop_int)
        lic["pop_notes"].extend(pop_notes)

        ch = lic["channels"].setdefault(channel_name, {
            "hours": None,
            "hours_notes": [],
            "topics": []
        })
        hrs, hrs_notes = parse_hours_week(brcst_time, get(row, "ns1:smi_name"))
        if hrs is not None:
            ch["hours"] = hrs
        ch["hours_notes"].extend(hrs_notes)

        if direction:
            # процент
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
        pop_total = None
        if data["pop_values"]:
            # если в выгрузке по лицензии несколько территорий — суммируем уникальные
            pop_total = int(sum(sorted(set(data["pop_values"]))))
        license_obj = License(
            license_id=lic_id,
            org_name=data.get("org_name", ""),
            inn=inn,
            media_raw=data.get("sreda", ""),
            media_class=media_class,
            population_total=pop_total,
            population_notes=data.get("pop_notes", []),
            rkn_url=build_rkn_url(lic_id),
            channels=[]
        )
        for ch_name, ch_data in data["channels"].items():
            license_obj.channels.append(Channel(
                name=ch_name,
                hours_week=ch_data.get("hours"),
                topics=ch_data.get("topics", [])
            ))
        licenses.append(license_obj)

    if not licenses:
        notes.append("По этому ИНН в таблице РКН не найдено строк. Проверьте, что ИНН есть в выгрузке.")
    return licenses, notes


# --------------------------- computations ---------------------------

def round_rate(x: float) -> float:
    # округление до 0,1%
    return round(x + 1e-9, 1)

def compute_contract_rate(licenses: List[License]) -> Tuple[float, Dict[str, Any]]:
    """
    A4: ставка по договору (по всем лицензиям).
    Веса: часы_ВЛ * население_ВЛ
    """
    details: Dict[str, Any] = {"licenses": []}
    num = 0.0
    den = 0.0
    for lic in licenses:
        # ставка по ВЛ (A3)
        lic_rate, lic_rate_details = compute_license_rate(lic)
        pop = lic.population_total
        hrs = lic.total_hours()
        w = (pop or 0) * hrs
        details["licenses"].append({
            "license_id": lic.license_id,
            "license_rate": lic_rate,
            "population": pop,
            "hours": hrs,
            "weight": w,
            "license_rate_details": lic_rate_details
        })
        if pop is None:
            continue
        num += lic_rate * w
        den += w
    if den == 0:
        # fallback
        return 2.5, {"warning": "Не удалось рассчитать взвешенную ставку (нет населения). Применена 2,5%."}
    return round_rate(num / den), details

def compute_license_rate(lic: License) -> Tuple[float, Dict[str, Any]]:
    """
    A3: ставка по вещательной лицензии.
    Если несколько каналов — среднее с весами по часам.
    """
    det: Dict[str, Any] = {"channels": []}
    num = 0.0
    den = 0.0
    for ch in lic.channels:
        ch_rate, ch_notes = ch.avg_rate()
        hrs = ch.hours_week if ch.hours_week is not None else 168.0
        det["channels"].append({
            "channel": ch.name,
            "hours": hrs,
            "channel_rate_raw": ch_rate,
            "channel_rate": round_rate(ch_rate),
            "notes": ch_notes,
            "topics": [
                {"topic": t.topic_raw, "share_pct": t.share_pct, "rate_pct": t.rate_pct, "note": t.note}
                for t in ch.topics
            ]
        })
        num += ch_rate * hrs
        den += hrs
    if den == 0:
        return 2.5, {"warning": "Не удалось рассчитать ставку по ВЛ (нет часов/каналов). Применена 2,5%."}
    return round_rate(num / den), det

def compute_percent_sum_q(contract_rate: float, annual_revenue: Optional[float], revenue_q: Optional[float], expenses_q: Optional[float]) -> Tuple[Optional[float], Dict[str, Any], List[str]]:
    """
    B1–B2: база и сумма по проценту за квартал.
    """
    notes: List[str] = []
    det: Dict[str, Any] = {"base_type": None, "base_q": None}

    base_q = None
    if revenue_q is not None:
        base_q = revenue_q
        det["base_type"] = "доходы (квартал)"
        notes.append("База для процента: доходы за квартал (введено пользователем).")
    elif annual_revenue is not None:
        base_q = annual_revenue / 4.0
        det["base_type"] = "доходы (год/4)"
        notes.append("База для процента: годовая выручка/доход разделён на 4 (допущение, если нет поквартальных данных).")
    elif expenses_q is not None:
        base_q = expenses_q
        det["base_type"] = "расходы (квартал)"
        notes.append("База для процента: расходы за квартал (ветка 100% госструктура / нет доходов).")
    else:
        notes.append("Не задана база для процента (нет доходов/выручки/расходов).")
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
        if hours_week >= lo and hours_week <= hi:
            return float(r["Коэффициент к установленной минимальной сумме вознаграждения"])
    return 1.0

def discount_by_licenses(disc_df: pd.DataFrame, n_licenses: int) -> float:
    for _, r in disc_df.iterrows():
        lo = int(r["Минимальное количество вещательных лицензий одного пользователя"])
        hi = r["Максимальное количество вещательных лицензий одного пользователя"]
        hi_val = int(hi) if pd.notna(hi) else None
        if n_licenses >= lo and (hi_val is None or n_licenses <= hi_val):
            disc_pct = float(r["Размер скидки к совокупной минимальной сумме вознаграждения, процентов"])
            return 1.0 - disc_pct / 100.0
    return 1.0

def contract_period_coeff(period_df: pd.DataFrame, contract_quarter: int) -> float:
    for _, r in period_df.iterrows():
        lo = int(r["Отчетный период действия лицензионного договора, начиная с (номер квартала)"])
        hi = int(r["Отчетный период действия лицензионного договора, по (номер квартала включительно)"])
        if contract_quarter >= lo and contract_quarter <= hi:
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
) -> Tuple[Optional[float], Dict[str, Any], List[str]]:
    """
    C1–C7 + D («гильотина»).
    Возвращает (min_total, details, notes).
    """
    notes: List[str] = []
    details: Dict[str, Any] = {"steps": []}

    mins_df = pd.read_excel(vars_xlsx, sheet_name="Минимальные суммы по населению")
    disc_df = pd.read_excel(vars_xlsx, sheet_name="Скидки по количеству лицензий")
    hours_df = pd.read_excel(vars_xlsx, sheet_name="Коэффициенты по часам")
    period_df = pd.read_excel(vars_xlsx, sheet_name="Коэфф по периодам договора")
    params_df = pd.read_excel(vars_xlsx, sheet_name="Параметры для расчетов")

    # параметры
    def get_param_contains(substr: str, default: float) -> float:
        sub = params_df[params_df["Наименование параметра для расчета авторского вознаграждения"].astype(str).str.contains(substr, case=False, na=False)]
        if sub.empty:
            return default
        return float(sub.iloc[0]["Значение параметра"])

    THRESH_SMALL = get_param_contains("Порог годового дохода", 1_500_000.0)
    SMALL_K = get_param_contains("Коэффициент уменьшения", 0.5)
    SMALL_MAX_Q = int(get_param_contains("Максимальное количество отчетных периодов применения половины", 8))
    INTERNET_PCT = get_param_contains("Дополнительный процент увеличения", 0.15)
    INTERNET_MIN_ADD = get_param_contains("Минимальное увеличение", 12500.0)
    GUILLOTINE_PCT = get_param_contains("Порог превышения", 0.1)

    # входные проверки
    pops_missing = [lic.license_id for lic in licenses if lic.population_total is None]
    if pops_missing:
        notes.append(f"Не найдена численность населения по лицензиям: {pops_missing}. Без населения минималка будет неполной.")
    # определим наличие двух сред у пользователя (для агрегированного расчёта)
    has_two_media = False
    media_classes = [lic.media_class for lic in licenses]
    if "Одновременно в эфире и по кабелю" in media_classes:
        has_two_media = True
    else:
        # если есть и эфир и кабель отдельными лицензиями — тоже считаем как две среды
        has_air = any("эфир" in (lic.media_raw or "").lower() or "назем" in (lic.media_raw or "").lower() for lic in licenses)
        has_cable = any("кабель" in (lic.media_raw or "").lower() for lic in licenses)
        has_two_media = bool(has_air and has_cable)

    media_for_agg = "Одновременно в эфире и по кабелю" if has_two_media else "В эфире или по кабелю"

    # OVERRIDE: среда договора важнее «универсальной» в РКН для расчёта минималки
    contract_media = (contract_media or "auto").lower().strip()
    if contract_media in ("cable", "air"):
        # По таблице минималок это одна среда: «В эфире или по кабелю»
        has_two_media = False
        media_for_agg = "В эфире или по кабелю"
        notes.append("Среда договора принудительно задана как «В эфире или по кабелю» (air/cable).")
    elif contract_media == "both":
        has_two_media = True
        media_for_agg = "Одновременно в эфире и по кабелю"
        notes.append("Среда договора принудительно задана как «Одновременно в эфире и по кабелю». ")

    # решить, включать ли ветку малого дохода
    small_branch = False
    if use_small_income_branch is not None:
        small_branch = use_small_income_branch
    else:
        if annual_income_for_rules is not None and annual_income_for_rules <= THRESH_SMALL and contract_quarter <= SMALL_MAX_Q:
            small_branch = True

    # --- C1/C2/C3: базовая минималка ---
    min_total = 0.0
    if small_branch:
        # C3: суммарная численность и 1 раз по таблице + 1/2
        N_sum = 0
        for lic in licenses:
            if lic.population_total is not None:
                N_sum += int(lic.population_total)
        if N_sum <= 0:
            return None, details, notes + ["Нельзя применить ветку малого дохода: нет суммарной численности населения."]
        m = lookup_min_sum(mins_df, N_sum, media_for_agg)
        if m is None:
            return None, details, notes + ["Не найдена минималка в таблице по суммарной численности населения."]
        min_total = SMALL_K * m
        details["steps"].append({"step": "C3", "N_sum": N_sum, "media": media_for_agg, "min_table": m, "k_small": SMALL_K, "min_after": min_total})
        notes.append("Включена ветка малого дохода: минималка по суммарной численности населения и затем ×0,5.")
    else:
        # C1+C2: по каждой ВЛ и сложение
        per_lic = []
        for lic in licenses:
            if lic.population_total is None:
                continue
            media_for_min = lic.media_class
            if contract_media in ("cable", "air"):
                media_for_min = "В эфире или по кабелю"
            elif contract_media == "both":
                media_for_min = "Одновременно в эфире и по кабелю"
            m = lookup_min_sum(mins_df, int(lic.population_total), media_for_min)
            if m is None:
                notes.append(f"Не найдена минималка по таблице для лицензии {lic.license_id} (население={lic.population_total}, среда={lic.media_class}).")
                continue

            # C7 (по часам) — если <126, применим коэффициент к минималке этой ВЛ
            hrs = lic.total_hours()
            coeff = 1.0
            if hrs < 126:
                coeff = hour_coeff(hours_df, hrs)
            m2 = m * coeff
            per_lic.append({"license_id": lic.license_id, "population": lic.population_total, "media": media_for_min, "min_table": m, "hours_week": hrs, "hour_coeff": coeff, "min_after": m2})
            min_total += m2

        details["steps"].append({"step": "C1+C2(+C7)", "per_license": per_lic, "min_after": min_total})

    # --- C4: скидка по количеству лицензий (>3) ---
    n_lic = len(licenses)
    if n_lic > 3:
        k = discount_by_licenses(disc_df, n_lic)
        min_total *= k
        details["steps"].append({"step": "C4", "n_licenses": n_lic, "coeff": k, "min_after": min_total})
        notes.append(f"Применена скидка по числу лицензий (кол-во ВЛ={n_lic}).")

    # --- E1: коэффициент по периодам договора (1–4:0.75, 5–8:0.88) ---
    k_period = contract_period_coeff(period_df, contract_quarter)
    if k_period != 1.0:
        min_total *= k_period
        details["steps"].append({"step": "E1(period)", "contract_quarter": contract_quarter, "coeff": k_period, "min_after": min_total})
        notes.append("Применён коэффициент по периоду действия договора (стимулирующий/понижающий).")

    # --- C6: интернет-доплата ---
    if internet_resources and internet_resources > 0:
        add_per = max(INTERNET_PCT * min_total, INTERNET_MIN_ADD)
        delta = add_per * internet_resources
        min_total += delta
        details["steps"].append({"step": "C6", "resources": internet_resources, "add_per_resource": add_per, "delta": delta, "min_after": min_total})
        notes.append("Добавлена доплата за интернет-вещание (+15%, но не менее 12 500 за ресурс).")

    # --- D: гильотина ---
    if percent_sum_q is not None:
        if min_total > (1.0 + GUILLOTINE_PCT) * percent_sum_q:
            details["steps"].append({"step": "D1", "condition": f"min_total > {(1.0 + GUILLOTINE_PCT):.2f} * percent_sum_q", "min_total": min_total, "percent_sum_q": percent_sum_q})
            notes.append("Сработала «гильотина»: минималка превышает расчёт по проценту более чем на 10%. Запускаем выравнивание.")

            # D2: пересчёт по суммарной численности без деления пополам
            N_sum = 0
            for lic in licenses:
                if lic.population_total is not None:
                    N_sum += int(lic.population_total)
            if N_sum > 0:
                alt1 = lookup_min_sum(mins_df, N_sum, media_for_agg)
                if alt1 is not None:
                    # применяем те же корректировки (скидка по кол-ву ВЛ, период, интернет)
                    alt = alt1
                    if n_lic > 3:
                        alt *= discount_by_licenses(disc_df, n_lic)
                    if k_period != 1.0:
                        alt *= k_period
                    if internet_resources and internet_resources > 0:
                        add_per = max(INTERNET_PCT * alt, INTERNET_MIN_ADD)
                        alt += add_per * internet_resources

                    details["steps"].append({"step": "D2", "N_sum": N_sum, "min_table": alt1, "min_after_adjust": alt})
                    if alt <= (1.0 + GUILLOTINE_PCT) * percent_sum_q:
                        min_total = alt
                        notes.append("«Гильотина», шаг 1: минималка пересчитана по суммарной численности населения и принята.")
                    else:
                        # D3: фактические платежи за год
                        if past_year_percent_paid is not None:
                            min_total = 0.25 * float(past_year_percent_paid)
                            details["steps"].append({"step": "D3", "S_year": past_year_percent_paid, "k": 0.25, "min_after": min_total})
                            notes.append("«Гильотина», шаг 2: минималка установлена как 1/4 от суммы фактических платежей по проценту за год.")
                        else:
                            notes.append("«Гильотина», шаг 2 требует суммы фактических платежей по проценту за год (S_год). Укажите past_year_percent_paid.")
                else:
                    notes.append("Не удалось выполнить шаг 1 «гильотины»: не найдена минималка по суммарной численности в таблице 3.1.")
            else:
                notes.append("Не удалось выполнить «гильотину»: нет суммарной численности населения.")

    return round(min_total, 2), details, notes


# --------------------------- reporting ---------------------------

def money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}".replace(",", " ").replace(".00", "")

def format_report(
    inn: str,
    year: Optional[int],
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
) -> str:
    lines: List[str] = []

    org_name = licenses[0].org_name if licenses else "Не найдено (нет записей в РКН)"
    lines.append(f"{org_name}, ИНН {inn}.")
    lines.append("")

    # Финансы
    if annual_revenue is not None:
        lines.append(f"Выручка/доход за {year or 'год'} (введено): {money(annual_revenue)} ₽.")
        lines.append("База квартала: год/4 (если нет поквартальных данных).")
    elif revenue_q is not None:
        lines.append(f"Доходы за квартал (введено): {money(revenue_q)} ₽.")
    elif expenses_q is not None:
        lines.append(f"Расходы за квартал (введено): {money(expenses_q)} ₽ (ветка 100% госструктура / нет доходов).")
    else:
        lines.append("Финансовая база для расчёта процента: НЕ ЗАДАНА.")
    lines.append("")

    # Лицензии
    lines.append(f"Вещательные лицензии (по таблице РКН): {len(licenses)} шт.")
    for lic in licenses:
        pop = lic.population_total
        pop_str = f"{pop:,}".replace(",", " ") if pop is not None else "не найдено"
        lines.append(f"— ID лицензии в выгрузке РКН: {lic.license_id}; среда: {lic.media_raw} → {lic.media_class}; население: {pop_str}.")
        lines.append(f"  РКН: {lic.rkn_url}")
        if lic.population_notes:
            for n in lic.population_notes[:2]:
                lines.append(f"  Примечание: {n}")

        # каналы/тематики
        for ch in lic.channels[:10]:
            hrs = ch.hours_week if ch.hours_week is not None else 168.0
            ch_rate, ch_notes = ch.avg_rate()
            lines.append(f"  • Канал/СМИ: {ch.name}; часы/нед: {hrs:g}; ставка канала (расчёт): {round_rate(ch_rate):.1f}%.")
            for tn in ch_notes[:2]:
                lines.append(f"    — {tn}")
            if ch.topics:
                for t in ch.topics[:8]:
                    share = f"{t.share_pct:g}%" if t.share_pct is not None else "без доли"
                    lines.append(f"    Тематика: {t.topic_raw} ({share}) → {t.rate_pct:.1f}%.")
    lines.append("")

    # ставка по договору
    lines.append(f"Процентная ставка по договору (взвешенная по часам×населению): {contract_rate:.1f}%.")
    lines.append("")

    # сумма по проценту
    lines.append(f"Расчётная сумма по проценту за квартал: {money(percent_sum_q)} ₽.")
    lines.append("")

    # минималка
    lines.append(f"Минимальная сумма за квартал (по правилам/таблицам): {money(min_total)} ₽.")
    lines.append("")

    # итог
    if percent_sum_q is not None and min_total is not None:
        pay = max(percent_sum_q, min_total)
        which = "по проценту" if percent_sum_q >= min_total else "по минималке"
        lines.append(f"Итог: {contract_rate:.1f}% от базы за квартал, но не менее {money(min_total)} ₽. К оплате: {money(pay)} ₽ ({which}).")
    else:
        lines.append("Итог: недостаточно данных для финального вывода (см. «Нужно уточнить»).")
    lines.append("")

    if internet_resources:
        lines.append(f"Интернет-вещание: указано ресурсов — {internet_resources} (применена доплата по правилам).")
        lines.append("")

    if needs:
        lines.append("Нужно уточнить/проверить:")
        for x in needs:
            lines.append(f"— {x}")
        lines.append("")

    if notes:
        lines.append("Примечания и допущения:")
        for n in notes:
            lines.append(f"— {n}")
        lines.append("")

    return "\n".join(lines)


# --------------------------- interactive wizard ---------------------------

def _ask(prompt: str, default: str = "") -> str:
    if default:
        s = input(f"{prompt} [по умолчанию: {default}]: ").strip()
        return s if s != "" else default
    else:
        return input(f"{prompt}: ").strip()

def _ask_int(prompt: str, default: int | None = None) -> int | None:
    while True:
        if default is None:
            s = input(f"{prompt} (число без пробелов, Enter = пропустить): ").strip()
            if s == "":
                return None
        else:
            s = input(f"{prompt} (Enter = оставить {default}): ").strip()
            if s == "":
                return default
        s2 = re.sub(r"\s+", "", s)
        if re.fullmatch(r"-?\d+", s2):
            return int(s2)
        print("❗ Введите целое число без пробелов. Пример: 978500")

def _ask_float(prompt: str, default: float | None = None) -> float | None:
    while True:
        if default is None:
            s = input(f"{prompt} (число без пробелов, Enter = пропустить): ").strip()
            if s == "":
                return None
        else:
            s = input(f"{prompt} (Enter = оставить {default}): ").strip()
            if s == "":
                return default
        s2 = s.replace(" ", "").replace(",", ".")
        try:
            return float(s2)
        except ValueError:
            print("❗ Введите число без пробелов. Пример: 33986000 или 33986000.50")

def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    ch = "/".join(choices)
    while True:
        s = input(f"{prompt} ({ch}) [по умолчанию: {default}]: ").strip().lower()
        if s == "":
            return default
        if s in choices:
            return s
        print(f"❗ Введите один из вариантов: {ch}")

def _ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    d = "Y" if default_yes else "N"
    s = input(f"{prompt} [Y/N, Enter = {d}]: ").strip().lower()
    if s == "":
        return default_yes
    if s in ("y", "yes", "д", "да"):
        return True
    if s in ("n", "no", "н", "нет"):
        return False
    print("❗ Ответьте Y или N.")
    return default_yes


def wizard_collect_argv() -> List[str]:
    """
    Первый этап мастера: собираем базовые вводные (ИНН, год, выручка, интернет, квартал договора и т.п.)
    и возвращаем argv-список для main(argv=...).

    Важно: числа вводятся БЕЗ пробелов (подсказка выводится в самих вопросах).
    """
    print("\n==============================")
    print("МАСТЕР РАСЧЁТА РАО (ТВ/радио)")
    print("Отвечай по порядку. Где можно — жми Enter.")
    print("==============================\n")

    inn = _ask(
        "1) Введите ИНН (10/12 цифр): ",
        required=True,
        validator=lambda s: (s.isdigit() and len(s) in (10, 12)),
        err_msg="ИНН должен состоять из 10 или 12 цифр (без пробелов).",
    )

    year = _ask_int("2) Год (например 2024) (Enter = пропустить): ", default=None, minv=1900, maxv=2100)

    have_q = _ask_yes_no("3) Есть точные ДОХОДЫ за квартал? [Y/N, Enter=N]: ", default=False)
    revenue_q = None
    annual_revenue = None

    if have_q:
        revenue_q = _ask_float("   Введите доходы за квартал (без пробелов): ", default=None, minv=0)
    else:
        have_y = _ask_yes_no("4) Есть ДОХОД/ВЫРУЧКА за год? [Y/N, Enter=Y]: ", default=True)
        if have_y:
            annual_revenue = _ask_float("   Введите годовую выручку/доход (без пробелов): ", default=None, minv=0)
        else:
            # Если нет вообще данных — калькулятор сам скажет, что требуется
            annual_revenue = None

    internet_resources = _ask_int("6) Интернет-ресурсы со стримингом (0 если нет): ", default=0, minv=0, maxv=1000)

    contract_quarter = _ask_int("7) Номер квартала действия договора (1..4): ", default=1, minv=1, maxv=4)

    contract_media = _ask_choice(
        "8) Среда по ДОГОВОРУ (auto/cable/air/both): ",
        ["auto", "cable", "air", "both"],
        default="auto",
    )

    only_license = _ask("9) Считать только одну лицензию? (Enter = все): ", default="").strip() or None

    population_override = _ask_int(
        "   Фактическая территория по письму (если отличается от РКН). Введите население (Enter = нет): ",
        default=None,
        minv=0,
        maxv=2_000_000_000,
    )

    past_year_percent_paid = None
    have_past = _ask_yes_no("10) Есть сумма фактических платежей по проценту за прошлый год (для D3)? [Y/N, Enter=N]: ", default=False)
    if have_past:
        past_year_percent_paid = _ask_float("   Введите сумму фактических платежей по проценту за прошлый год (без пробелов): ", default=None, minv=0)

    argv = []
    argv += ["--inn", inn]
    if year is not None:
        argv += ["--year", str(year)]
    if revenue_q is not None:
        argv += ["--revenue_q", str(int(revenue_q) if float(revenue_q).is_integer() else revenue_q)]
    if annual_revenue is not None:
        argv += ["--annual_revenue", str(int(annual_revenue) if float(annual_revenue).is_integer() else annual_revenue)]

    argv += ["--internet_resources", str(internet_resources)]
    argv += ["--contract_quarter", str(contract_quarter)]
    argv += ["--contract_media", contract_media]
    if only_license:
        argv += ["--only_license", only_license]
    if population_override is not None:
        argv += ["--population_override", str(int(population_override))]
    if past_year_percent_paid is not None:
        argv += ["--past_year_percent_paid", str(int(past_year_percent_paid) if float(past_year_percent_paid).is_integer() else past_year_percent_paid)]

    return argv

def interactive_wizard(args, licenses: List[License]) -> tuple[Any, List[License]]:
    print("\n" + "="*70)
    print("ПРОВЕРКА ДАННЫХ ПЕРЕД РАСЧЁТОМ (чтобы всё было правильно)")
    print("Если всё верно — просто жмите Enter. Если неверно — введите исправление.")
    print("="*70 + "\n")

    # 1) Среда договора
    args.contract_media = _ask_choice(
        "Среда по договору (влияет на МИНИМАЛКУ)",
        ["auto", "cable", "air", "both"],
        (getattr(args, "contract_media", None) or "auto").lower()
    )

    # 2) Финансовая база
    if args.annual_revenue is None and args.revenue_q is None and args.expenses_q is None:
        print("\nФИНАНСЫ: нужно выбрать хотя бы одно: annual_revenue / revenue_q / expenses_q")
        args.annual_revenue = _ask_float("Введите годовую выручку/доход (annual_revenue)", None)
        if args.annual_revenue is None:
            args.revenue_q = _ask_float("ИЛИ введите доход за квартал (revenue_q)", None)
        if args.annual_revenue is None and args.revenue_q is None:
            args.expenses_q = _ask_float("ИЛИ введите расходы за квартал (expenses_q, если 100% госструктура)", None)

    args.internet_resources = _ask_int("Сколько интернет-ресурсов со стримингом? (internet_resources)", int(args.internet_resources or 0)) or 0
    args.contract_quarter = _ask_int("Номер квартала действия договора (contract_quarter)", int(args.contract_quarter or 1)) or 1

    # 3) Лицензии
    print("\nЛИЦЕНЗИИ И ДАННЫЕ ИЗ РКН:\n")
    new_list: List[License] = []
    for lic in licenses:
        print("-"*70)
        print(f"Лицензия: {lic.license_id}")
        print(f"Организация: {lic.org_name}")
        print(f"Среда (РКН): {lic.media_raw}  →  (классификация): {lic.media_class}")
        pop = lic.population_total
        pop_str = f"{pop:,}".replace(",", " ") if pop is not None else "НЕ НАЙДЕНО"
        print(f"Население: {pop_str}")
        if getattr(args, "population_override", None) and getattr(lic, "population_rkn", None) is not None:
            print(f"⚠️ Переопределено по письму: {lic.population_total:,} (РКН: {lic.population_rkn:,})".replace(",", " "))

        print(f"Ссылка РКН: {lic.rkn_url}")

        if not _ask_yes_no("Использовать эту лицензию в расчёте?", True):
            continue

        # население — подтверждение/ввод
        if lic.population_total is None:
            lic.population_total = _ask_int("Введите численность населения по этой лицензии", None)
        else:
            lic.population_total = _ask_int("Население верное?", int(lic.population_total))

        # каналы/часы
        for ch in lic.channels:
            hrs = ch.hours_week if ch.hours_week is not None else 168.0
            print(f"\nКанал: {ch.name}")
            ch.hours_week = float(_ask_int("Часы вещания в неделю", int(hrs)) or int(hrs))

            # кратко показать тематики
            if ch.topics:
                print("Тематики (как прочитали из РКН):")
                for tpc in ch.topics[:10]:
                    share = f"{tpc.share_pct:g}%" if tpc.share_pct is not None else "без доли"
                    print(f"  - {tpc.topic_raw} ({share}) → ставка {tpc.rate_pct:.1f}%")
            else:
                print("Тематики: НЕ НАЙДЕНЫ (будет ставка по умолчанию).")

        new_list.append(lic)

    if not new_list:
        print("\n❗ После валидации не осталось лицензий для расчёта.")
        return args, []

    print("\n" + "="*70)
    print("ОК. Данные подтверждены. Запускаю расчёт…")
    print("="*70 + "\n")
    return args, new_list

# --------------------------- main ---------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inn", required=False, help="ИНН (10/12 цифр)")
    ap.add_argument("--wizard", action="store_true", help="Запустить интерактивный мастер (если флаги не переданы).")
    ap.add_argument("--year", type=int, default=None, help="Год выручки/дохода (для подписи в отчёте)")
    ap.add_argument("--annual_revenue", type=float, default=None, help="Годовая выручка/доход (число без пробелов)")
    ap.add_argument("--revenue_q", type=float, default=None, help="Доходы за квартал (если есть)")
    ap.add_argument("--expenses_q", type=float, default=None, help="Расходы за квартал (если ветка госструктуры)")
    ap.add_argument("--internet_resources", type=int, default=0, help="Кол-во сайтов/приложений со стримингом")
    ap.add_argument("--contract_quarter", type=int, default=1, help="Номер квартала действия договора (1..)")
    ap.add_argument("--non_interactive", action="store_true", help="Не задавать вопросы (без input), чистый автоматический режим")
    ap.add_argument("--contract_media", type=str, default="auto", choices=["auto", "cable", "air", "both"],
                    help="Среда по ДОГОВОРУ: auto (как в РКН), cable (КТВ/кабель), air (эфир), both (одновременно).")
    ap.add_argument("--only_license", type=str, default=None,
                    help="Считать только по одной лицензии (укажи ID лицензии как в отчёте/в РКН).")
    ap.add_argument("--past_year_percent_paid", type=float, default=None, help="Сумма фактических платежей по проценту за прошлый год (для шага D3)")
    ap.add_argument("--rkn_xlsx", type=str, default="Таблица РКН.xlsx")
    ap.add_argument("--vars_xlsx", type=str, default="Переменные из ставок.xlsx")
    ap.add_argument("--force_small_income", action="store_true", help="Принудительно включить ветку малого дохода (C3)")
    ap.add_argument("--no_small_income", action="store_true", help="Принудительно выключить ветку малого дохода (C3)")

    ap.add_argument("--population_override", type=int, default=None, help="Переопределить население (например, по письму пользователя). Применяется ко всем выбранным лицензиям.")

    args = ap.parse_args(argv)
    # Если запрошен мастер — собираем вводные и перезапускаем main() с готовыми аргументами
    if getattr(args, 'wizard', False):
        argv2 = wizard_collect_argv()
        return main(argv2)

    # В обычном режиме ИНН обязателен
    if not getattr(args, 'inn', None):
        ap.error('Нужно указать --inn (или запустить мастер: --wizard).')


    p = Progress()
    inn = parse_inn(args.inn)

    rkn_xlsx = Path(args.rkn_xlsx)
    vars_xlsx = Path(args.vars_xlsx)

    if not rkn_xlsx.exists():
        raise FileNotFoundError(f"Не найден файл: {rkn_xlsx}")
    if not vars_xlsx.exists():
        raise FileNotFoundError(f"Не найден файл: {vars_xlsx}")

    p.tick("читаю РКН и собираю лицензии")
    licenses, load_notes = load_licenses_by_inn(rkn_xlsx, inn, vars_xlsx)

    
        # --- population override (например, по письму пользователя) ---
    if getattr(args, "population_override", None):
        po = int(args.population_override)
        for lic in licenses:
            old = getattr(lic, "population_total", None)
            setattr(lic, "population_rkn", old)
            lic.population_total = po
            note = f"Переопределено пользователем (по письму): {po}" + (f" (РКН: {old})" if old is not None else "")
            # кладём в общий список примечаний по населению (чтобы попало в отчёт)
            if getattr(lic, "population_notes", None) is None:
                lic.population_notes = [note]
            else:
                lic.population_notes.append(note)
            setattr(lic, "population_note", note)
    # --- end population override ---


    if not args.non_interactive:
        args, licenses = interactive_wizard(args, licenses)
        if not licenses:
            print('Нет лицензий для расчёта после проверки.')
            return 2


    if args.only_license:
        target = str(args.only_license).strip()
        licenses = [x for x in licenses if str(x.license_id).strip() == target]
        if not licenses:
            print(f"Не найдена лицензия {target} у этого ИНН в таблице РКН.")
            return 2

    needs: List[str] = []
    notes: List[str] = []
    notes.extend(load_notes)

    if not licenses:
        print("Нет данных по ИНН в таблице РКН.")
        return 2

    # если у всех лицензий нет населения — нужно веб-проверка
    if all(lic.population_total is None for lic in licenses):
        needs.append("В РКН-таблице не заполнено население. Нужно открыть карточки лицензий РКН по ссылкам и взять численность населения территории вещания.")

    p.tick("считаю процентную ставку по договору")
    contract_rate, contract_rate_details = compute_contract_rate(licenses)

    p.tick("считаю сумму по проценту за квартал")
    percent_sum_q, percent_details, percent_notes = compute_percent_sum_q(
        contract_rate=contract_rate,
        annual_revenue=args.annual_revenue,
        revenue_q=args.revenue_q,
        expenses_q=args.expenses_q,
    )
    notes.extend(percent_notes)
    if percent_sum_q is None:
        needs.append("Нужна финансовая база: annual_revenue (годовая) или revenue_q (квартальная) или expenses_q (расходы квартала для ветки госструктуры).")

    # для правил малого дохода берём годовой доход, если есть; если только квартал — умножаем на 4 как приближение
    annual_income_for_rules = None
    if args.annual_revenue is not None:
        annual_income_for_rules = float(args.annual_revenue)
    elif args.revenue_q is not None:
        annual_income_for_rules = float(args.revenue_q) * 4.0

    # флаги ветки малого дохода
    use_small_income = None
    if args.force_small_income and args.no_small_income:
        raise ValueError("Нельзя одновременно --force_small_income и --no_small_income")
    if args.force_small_income:
        use_small_income = True
    if args.no_small_income:
        use_small_income = False

    p.tick("считаю минимальную сумму")
    min_total, min_details, min_notes = compute_min_total(
        licenses=licenses,
        vars_xlsx=vars_xlsx,
        annual_income_for_rules=annual_income_for_rules,
        contract_quarter=args.contract_quarter,
        internet_resources=args.internet_resources,
        past_year_percent_paid=args.past_year_percent_paid,
        percent_sum_q=percent_sum_q,
        contract_media=args.contract_media,
        use_small_income_branch=use_small_income,
    )
    notes.extend(min_notes)

    p.tick("формирую отчёт")
    report = format_report(
        inn=inn,
        year=args.year,
        annual_revenue=args.annual_revenue,
        revenue_q=args.revenue_q,
        expenses_q=args.expenses_q,
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
    )

    print(report)
    return 0




# =========================
# Удобный API для Telegram‑бота
# =========================
def run_calc_capture(argv: List[str]) -> Tuple[int, str]:
    """
    Запускает расчёт как main(argv=...), но возвращает (exit_code, полный_вывод_в_stdout).
    Удобно для интеграции в Telegram‑бот: можно отправлять пользователю "как в консоли".

    Пример:
        code, out = run_calc_capture(["--inn","...", "--year","2024", "--annual_revenue","12345", "--contract_media","cable"])
    """
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = int(main(argv))
    except SystemExit as e:
        # если где-то внутри был SystemExit
        code = int(getattr(e, "code", 1) or 0)
    return code, buf.getvalue()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("--wizard")
    raise SystemExit(main())
