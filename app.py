from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List
import shlex

from rao import run_calc_capture

app = FastAPI()


class RunRequest(BaseModel):
    line: str


class RunArgvRequest(BaseModel):
    argv: List[str]


@app.get("/", response_class=HTMLResponse)
def home():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


# старый вариант: одна строка аргументов
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

    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    return {"exit_code": code, "output": out}


# новый вариант: браузер собирает список argv пошагово
@app.post("/api/run_argv")
def api_run_argv(req: RunArgvRequest):
    argv = list(req.argv or [])

    if "--wizard" in argv:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Во веб-версии нельзя --wizard."},
        )

    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    return {"exit_code": code, "output": out}
