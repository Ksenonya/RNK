from pathlib import Path
from typing import Any, List, Optional, Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# Pydantic v1/v2 совместимость
try:
    from pydantic import field_validator  # type: ignore
    _V2 = True
except Exception:
    from pydantic import validator as field_validator  # type: ignore
    _V2 = False

from rao import run_calc_capture, parse_inn, get_org_name_by_inn, fix_mojibake
import rao as rao_mod

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"

RAO_DIR = Path(rao_mod.__file__).resolve().parent

def find_rkn_xlsx() -> Path:
    candidates = [
        BASE_DIR / "Таблица РКН.xlsx",
        RAO_DIR / "Таблица РКН.xlsx",
        Path.cwd() / "Таблица РКН.xlsx",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Не найден файл 'Таблица РКН.xlsx'. "
        f"Пробовал: {[str(c) for c in candidates]}"
    )

app = FastAPI()

INN_MAP: dict[str, str] = {}

@app.on_event("startup")
def load_inn_map():
    global INN_MAP
    csv_path = BASE_DIR / "inn_name.csv"
    if not csv_path.exists():
        print("⚠️ inn_name.csv not found рядом с app.py")
        return

    import csv
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)

        # 🔥 проверка, что колонки совпали
        if not r.fieldnames:
            raise RuntimeError("inn_name.csv пустой или без заголовков")
        need_cols = {"ns1:inn", "ns1:org_name_short"}
        missing = need_cols - set(r.fieldnames)
        if missing:
            raise RuntimeError(f"inn_name.csv: не найдены колонки {sorted(missing)}; есть {r.fieldnames}")

        mp = {}
        for row in r:
            inn = (row.get("ns1:inn") or "").strip()
            name = (row.get("ns1:org_name_short") or "").strip()
            if inn and name and inn not in mp:
                mp[inn] = name

        INN_MAP = mp
        print(f"✅ inn_name.csv loaded: {len(INN_MAP)}")



DASH_TOKENS = {"", "-", "—", "–", "нет"}


def _is_dash(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() in DASH_TOKENS
    return False


def _to_none_or_str(v: Any) -> Optional[str]:
    if _is_dash(v):
        return None
    return str(v).strip()


def _to_none_or_int(v: Any) -> Optional[int]:
    if _is_dash(v):
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip().replace(" ", "")
    if not s:
        return None
    return int(s)


def _to_none_or_float(v: Any) -> Optional[float]:
    if _is_dash(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    if not s:
        return None
    return float(s)


class CalcRequest(BaseModel):
    inn: str = Field(..., description="ИНН 10 или 12 цифр")

    annual_revenue: Optional[float] = Field(None, ge=0)
    revenue_q: Optional[float] = Field(None, ge=0)
    expenses_q: Optional[float] = Field(None, ge=0)

    internet_resources: int = Field(0, ge=0, le=1000)
    contract_media: Literal["auto", "cable", "air", "both"] = "auto"

    new_user: bool = False
    assoc_member: bool = False

    only_license: Optional[str] = None
    population_override: Optional[int] = Field(None, ge=0, le=2_000_000_000)

    small_income_mode: Literal["auto", "force_on", "force_off"] = "auto"

    if _V2:
        @field_validator("inn", mode="before")
        @classmethod
        def _v_inn(cls, v: Any) -> str:
            s = _to_none_or_str(v)
            if not s:
                raise ValueError("ИНН обязателен")
            s = s.replace(" ", "")
            if not s.isdigit() or len(s) not in (10, 12):
                raise ValueError("ИНН должен состоять из 10 или 12 цифр")
            return s

        @field_validator("annual_revenue", "revenue_q", "expenses_q", mode="before")
        @classmethod
        def _v_floats(cls, v: Any) -> Optional[float]:
            return _to_none_or_float(v)

        @field_validator("only_license", mode="before")
        @classmethod
        def _v_only_license(cls, v: Any) -> Optional[str]:
            return _to_none_or_str(v)

        @field_validator("population_override", mode="before")
        @classmethod
        def _v_pop(cls, v: Any) -> Optional[int]:
            return _to_none_or_int(v)

    else:
        @field_validator("inn", pre=True)
        def _v1_inn(cls, v: Any) -> str:
            s = _to_none_or_str(v)
            if not s:
                raise ValueError("ИНН обязателен")
            s = s.replace(" ", "")
            if not s.isdigit() or len(s) not in (10, 12):
                raise ValueError("ИНН должен состоять из 10 или 12 цифр")
            return s

        @field_validator("annual_revenue", "revenue_q", "expenses_q", pre=True)
        def _v1_floats(cls, v: Any) -> Optional[float]:
            return _to_none_or_float(v)

        @field_validator("only_license", pre=True)
        def _v1_only_license(cls, v: Any) -> Optional[str]:
            return _to_none_or_str(v)

        @field_validator("population_override", pre=True)
        def _v1_pop(cls, v: Any) -> Optional[int]:
            return _to_none_or_int(v)


@app.get("/", response_class=HTMLResponse)
def home():
    if not INDEX_HTML.exists():
        return HTMLResponse(
            "<h1>index.html не найден</h1><p>Положи index.html рядом с app.py</p>",
            status_code=500,
        )
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))




@app.get("/api/inninfo")
def api_inninfo(inn: str):
    try:
        inn_clean = parse_inn(inn)
        org_name = INN_MAP.get(inn_clean)
        if org_name:
            org_name = fix_mojibake(org_name)
            return {"ok": True, "inn": inn_clean, "org_name": org_name}
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    try:
        rkn_xlsx = find_rkn_xlsx()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    org_name = get_org_name_by_inn(rkn_xlsx, inn_clean)
    org_name = fix_mojibake(org_name)

    if not org_name:
        return JSONResponse(status_code=404, content={"ok": False, "org_name": ""})

    return {"ok": True, "inn": inn_clean, "org_name": org_name}


@app.post("/api/calc")
@app.post("/api/calc/")
def api_calc(req: CalcRequest):
    argv: List[str] = ["--inn", req.inn.strip(), "--non_interactive"]

    if req.revenue_q is not None:
        argv += ["--revenue_q", str(req.revenue_q)]
    if req.annual_revenue is not None:
        argv += ["--annual_revenue", str(req.annual_revenue)]
    if req.expenses_q is not None:
        argv += ["--expenses_q", str(req.expenses_q)]

    argv += ["--internet_resources", str(req.internet_resources)]
    argv += ["--contract_quarter", "1"]
    argv += ["--contract_media", str(req.contract_media)]

    if req.new_user:
        argv.append("--new_user")
    if req.assoc_member:
        argv.append("--assoc_member")

    if req.only_license:
        argv += ["--only_license", req.only_license.strip()]
    if req.population_override is not None:
        argv += ["--population_override", str(int(req.population_override))]

    if req.small_income_mode == "force_on":
        argv += ["--force_small_income"]
    elif req.small_income_mode == "force_off":
        argv += ["--no_small_income"]

    code, out = run_calc_capture(argv)
    out = (out or "").strip()

    if code == 0:
        return JSONResponse(status_code=200, content={"ok": True, "text": out})
    return JSONResponse(status_code=400, content={"ok": False, "error": out or "Ошибка расчёта"})


@app.get("/api/version")
def api_version():
    return {"app": "app_fix", "rao": "rao_fix2"}
