{
 "cells": [
  {
   "cell_type": "code",
   "execution_count": null,
   "id": "29378610-91ee-4b71-8d05-724bc97aab86",
   "metadata": {},
   "outputs": [],
   "source": [
    "from fastapi import FastAPI\n",
    "from fastapi.responses import HTMLResponse, JSONResponse\n",
    "from pydantic import BaseModel\n",
    "import shlex\n",
    "\n",
    "from rao import run_calc_capture  # функция уже есть в твоем rao.py\n",
    "\n",
    "app = FastAPI()\n",
    "\n",
    "\n",
    "class RunRequest(BaseModel):\n",
    "    line: str\n",
    "\n",
    "\n",
    "@app.get(\"/\", response_class=HTMLResponse)\n",
    "def home():\n",
    "    with open(\"index.html\", \"r\", encoding=\"utf-8\") as f:\n",
    "        return f.read()\n",
    "\n",
    "\n",
    "@app.post(\"/api/run\")\n",
    "def api_run(req: RunRequest):\n",
    "    line = (req.line or \"\").strip()\n",
    "    if not line:\n",
    "        return JSONResponse(\n",
    "            status_code=400,\n",
    "            content={\"exit_code\": 400, \"output\": \"Пустая строка. Пример: --inn 0326499787 --year 2024 --annual_revenue 33986000 --internet_resources 0 --contract_quarter 1\"},\n",
    "        )\n",
    "\n",
    "    argv = shlex.split(line)\n",
    "\n",
    "    # Во вебе нельзя интерактивные вопросы (иначе сервер зависнет)\n",
    "    if \"--wizard\" in argv:\n",
    "        return JSONResponse(\n",
    "            status_code=400,\n",
    "            content={\"exit_code\": 400, \"output\": \"Во веб-версии нельзя --wizard. Передавай аргументы строкой и/или используй --non_interactive.\"},\n",
    "        )\n",
    "\n",
    "    # Принудительно неинтерактивный режим\n",
    "    if \"--non_interactive\" not in argv:\n",
    "        argv.append(\"--non_interactive\")\n",
    "\n",
    "    code, out = run_calc_capture(argv)\n",
    "    return {\"exit_code\": code, \"output\": out}\n"
   ]
  }
 ],
 "metadata": {
  "kernelspec": {
   "display_name": "Python [conda env:base] *",
   "language": "python",
   "name": "conda-base-py"
  },
  "language_info": {
   "codemirror_mode": {
    "name": "ipython",
    "version": 3
   },
   "file_extension": ".py",
   "mimetype": "text/x-python",
   "name": "python",
   "nbconvert_exporter": "python",
   "pygments_lexer": "ipython3",
   "version": "3.12.7"
  }
 },
 "nbformat": 4,
 "nbformat_minor": 5
}
