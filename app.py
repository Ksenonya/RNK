from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List
from pathlib import Path
import shlex

from rao import run_calc_capture

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"


class RunRequest(BaseModel):
    line: str


class RunArgvRequest(BaseModel):
    argv: List[str]


@app.get("/", response_class=HTMLResponse)
def home():
    # На Render лучше читать файл относительно app.py
    if INDEX_PATH.exists():
        return INDEX_PATH.read_text(encoding="utf-8")
    # fallback (чтобы было понятнее, если забыли положить index.html рядом)
    return "<h3>index.html не найден рядом с app.py</h3>"


@app.post("/api/run")
def api_run(req: RunRequest):
    line = (req.line or "").strip()
    if not line:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Пустая строка."},
        )

    argv = shlex.split(line)

    if "--wizard" in argv:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Во веб-версии нельзя --wizard."},
        )

    # для сайта всегда без интерактива
    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    return {"exit_code": code, "output": out}


@app.post("/api/run_argv")
def api_run_argv(req: RunArgvRequest):
    argv = list(req.argv or [])

    if "--wizard" in argv:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Во веб-версии нельзя --wizard."},
        )

    # для сайта всегда без интерактива
    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    return {"exit_code": code, "output": out}
