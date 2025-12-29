from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from rao import run_calc_capture

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"

app = FastAPI()


class CalcRequest(BaseModel):
    inn: str = Field(..., description="ИНН 10 или 12 цифр")
    year: Optional[int] = Field(None, ge=1900, le=2100)

    # финансовая база (ровно одно обычно, но можно оставить пусто => будет needs)
    annual_revenue: Optional[float] = Field(None, ge=0)
    revenue_q: Optional[float] = Field(None, ge=0)
    expenses_q: Optional[float] = Field(None, ge=0)

    internet_resources: int = Field(0, ge=0, le=1000)
    contract_quarter: int = Field(1, ge=1, le=4)
    contract_media: Literal["auto", "cable", "air", "both"] = "auto"

    only_license: Optional[str] = None
    population_override: Optional[int] = Field(None, ge=0, le=2_000_000_000)
    past_year_percent_paid: Optional[float] = Field(None, ge=0)

    # управление веткой малого дохода
    small_income_mode: Literal["auto", "force_on", "force_off"] = "auto"


class RunArgvRequest(BaseModel):
    argv: List[str]


@app.get("/", response_class=HTMLResponse)
def home():
    if not INDEX_HTML.exists():
        return HTMLResponse(
            "<h1>index.html не найден</h1><p>Положи index.html рядом с app.py</p>",
            status_code=500,
        )
    return INDEX_HTML.read_text(encoding="utf-8")


@app.post("/api/calc")
def api_calc(req: CalcRequest):
    """
    Основной веб-эндпоинт: принимает JSON (как форма на сайте),
    собирает argv и запускает расчёт в неинтерактивном режиме.
    """
    argv: List[str] = ["--inn", str(req.inn).strip()]

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

    if req.only_license:
        argv += ["--only_license", str(req.only_license).strip()]

    if req.population_override is not None:
        argv += ["--population_override", str(int(req.population_override))]

    if req.past_year_percent_paid is not None:
        argv += ["--past_year_percent_paid", str(req.past_year_percent_paid)]

    if req.small_income_mode == "force_on":
        argv += ["--force_small_income"]
    elif req.small_income_mode == "force_off":
        argv += ["--no_small_income"]

    # ВАЖНО: для сайта всегда запрещаем интерактив
    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    status = 200 if code == 0 else 400
    return JSONResponse(status_code=status, content={"exit_code": code, "output": out})


@app.post("/api/run_argv")
def api_run_argv(req: RunArgvRequest):
    """
    Совместимость: принять готовый argv[] из браузера.
    """
    argv = list(req.argv or [])

    if "--wizard" in argv:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Во веб-версии нельзя --wizard."},
        )

    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    status = 200 if code == 0 else 400
    return JSONResponse(status_code=status, content={"exit_code": code, "output": out})
