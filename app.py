from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Literal, Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# --- Pydantic v1/v2 совместимость для валидаторов ---
try:
    from pydantic import field_validator  # type: ignore
    _PYDANTIC_V2 = True
except Exception:
    from pydantic import validator as field_validator  # type: ignore
    _PYDANTIC_V2 = False

from rao import run_calc_capture

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"

app = FastAPI()

DASH_TOKENS = {"", "-", "—", "–", "нет"}


def _is_dash(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s.lower() in DASH_TOKENS
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
    if isinstance(v, float) and v.is_integer():
        return int(v)
    s = str(v).strip().replace(" ", "")
    if s == "":
        return None
    return int(s)


def _to_none_or_float(v: Any) -> Optional[float]:
    if _is_dash(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    if s == "":
        return None
    return float(s)


class CalcRequest(BaseModel):
    inn: str = Field(..., description="ИНН 10 или 12 цифр")
    year: Optional[int] = Field(None, ge=1900, le=2100)

    annual_revenue: Optional[float] = Field(None, ge=0)
    revenue_q: Optional[float] = Field(None, ge=0)
    expenses_q: Optional[float] = Field(None, ge=0)

    internet_resources: int = Field(0, ge=0, le=1000)
    contract_quarter: int = Field(1, ge=1, le=4)
    contract_media: Literal["auto", "cable", "air", "both"] = "auto"

    new_user: bool = False

    only_license: Optional[str] = None
    population_override: Optional[int] = Field(None, ge=0, le=2_000_000_000)
    past_year_percent_paid: Optional[float] = Field(None, ge=0)

    small_income_mode: Literal["auto", "force_on", "force_off"] = "auto"

    if _PYDANTIC_V2:
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

        @field_validator("year", "population_override", mode="before")
        @classmethod
        def _v_ints(cls, v: Any) -> Optional[int]:
            return _to_none_or_int(v)

        @field_validator("annual_revenue", "revenue_q", "expenses_q", "past_year_percent_paid", mode="before")
        @classmethod
        def _v_floats(cls, v: Any) -> Optional[float]:
            return _to_none_or_float(v)

        @field_validator("only_license", mode="before")
        @classmethod
        def _v_only_license(cls, v: Any) -> Optional[str]:
            return _to_none_or_str(v)

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

        @field_validator("year", "population_override", pre=True)
        def _v1_ints(cls, v: Any) -> Optional[int]:
            return _to_none_or_int(v)

        @field_validator("annual_revenue", "revenue_q", "expenses_q", "past_year_percent_paid", pre=True)
        def _v1_floats(cls, v: Any) -> Optional[float]:
            return _to_none_or_float(v)

        @field_validator("only_license", pre=True)
        def _v1_only_license(cls, v: Any) -> Optional[str]:
            return _to_none_or_str(v)


@app.get("/", response_class=HTMLResponse)
def home():
    if not INDEX_HTML.exists():
        return HTMLResponse(
            "<h1>index.html не найден</h1><p>Положи index.html рядом с app.py</p>",
            status_code=500,
        )
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.post("/api/calc")
def api_calc(req: CalcRequest):
    argv: List[str] = ["--inn", req.inn.strip()]

    if req.year is not None:
        argv += ["--year", str(req.year)]

    if req.revenue_q is not None:
        argv += ["--revenue_q", str(req.revenue_q)]
    if req.annual_revenue is not None:
        argv += ["--annual_revenue", str(req.annual_revenue)]
    if req.expenses_q is not None:
        argv += ["--expenses_q", str(req.expenses_q)]

    argv += ["--internet_resources", str(req.internet_resources)]
    argv += ["--contract_quarter", str(req.contract_quarter)]
    argv += ["--contract_media", str(req.contract_media)]

    if req.new_user:
        argv.append("--new_user")

    if req.only_license:
        argv += ["--only_license", req.only_license.strip()]

    if req.population_override is not None:
        argv += ["--population_override", str(int(req.population_override))]

    if req.past_year_percent_paid is not None:
        argv += ["--past_year_percent_paid", str(req.past_year_percent_paid)]

    if req.small_income_mode == "force_on":
        argv += ["--force_small_income"]
    elif req.small_income_mode == "force_off":
        argv += ["--no_small_income"]

    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    status = 200 if code == 0 else 400
    return JSONResponse(status_code=status, content={"exit_code": code, "output": out})
