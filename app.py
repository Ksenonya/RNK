from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import shlex

from rao import run_calc_capture

app = FastAPI()


class RunRequest(BaseModel):
    line: str


@app.get("/", response_class=HTMLResponse)
def home():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/run")
def api_run(req: RunRequest):
    line = (req.line or "").strip()
    if not line:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Пустая строка. Пример: --inn 0326499787 --year 2024 --annual_revenue 33986000 --internet_resources 0 --contract_quarter 1"},
        )

    argv = shlex.split(line)

    if "--wizard" in argv:
        return JSONResponse(
            status_code=400,
            content={"exit_code": 400, "output": "Во веб-версии нельзя --wizard. Передавай аргументы строкой и/или используй --non_interactive."},
        )

    if "--non_interactive" not in argv:
        argv.append("--non_interactive")

    code, out = run_calc_capture(argv)
    return {"exit_code": code, "output": out}
