"""Общий модуль системы бэкенда: работа с Qwen и общие утилиты.

- ask_llm()       — запрос к модели (с повторами), вырезание рассуждения <think>, учёт токенов по агентам
- extract_code()  — код из ответа модели
- extract_json()  — JSON из ответа модели
- safe_path()     — проверка, что путь не выходит за папку вывода
- USAGE           — накопленная статистика вызовов: {агент: {"calls", "tokens", "seconds"}}

Настройки — переменные окружения или файл .env рядом:
  LLM_URL=http://host.docker.internal:4000/v1
  LLM_API_KEY=sk-...
  LLM_MODEL=qwen3.8-27b
  LLM_MAX_TOKENS=16000     # лимит ответа (рассуждения тоже входят в него)
  LLM_THINKING=off         # off — без рассуждений <think>: быстрее и дешевле
  LLM_MIN_INTERVAL=3       # пауза между запросами, с
  LLM_RATE_LIMIT_RETRIES=8 # сколько раз подождать при ответе 429 (лимит запросов)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- настройки


def load_dotenv() -> None:
    """Читает .env из текущей папки или папки скрипта. Переменные окружения важнее."""
    for p in (Path(".env"), Path(__file__).resolve().parent / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
            return


load_dotenv()

LLM_URL = os.getenv("LLM_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.8-27b")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))
# Рассуждения модели (<think>): off — выключены (быстрее, меньше токенов), on — включены
LLM_THINKING = os.getenv("LLM_THINKING", "off").lower() in ("on", "1", "true", "yes")
LLM_MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "3"))           # секунд между запросами
LLM_RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "8"))  # сколько раз ждать при 429

USAGE: dict[str, dict] = {}
_DEBUG_DIR: Path | None = None
_THINK_SWITCH_SUPPORTED = True     # сервер принимает chat_template_kwargs (выяснится при первом запросе)


def llm_configured() -> bool:
    return bool(LLM_URL and LLM_API_KEY)


def reset_usage() -> None:
    USAGE.clear()


def set_debug_dir(path: str | Path | None) -> None:
    """Папка для сырых ответов модели (для разбора ошибок). None — не сохранять."""
    global _DEBUG_DIR
    _DEBUG_DIR = Path(path) if path else None
    if _DEBUG_DIR:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _track(agent: str, tokens: int, seconds: float) -> None:
    u = USAGE.setdefault(agent, {"calls": 0, "tokens": 0, "seconds": 0.0})
    u["calls"] += 1
    u["tokens"] += tokens
    u["seconds"] = round(u["seconds"] + seconds, 2)


def _save_debug(agent: str, prompt: str, raw: str, finish, tokens: int) -> None:
    if not _DEBUG_DIR:
        return
    n = USAGE.get(agent, {}).get("calls", 0)
    (_DEBUG_DIR / f"{n:02d}_{agent}.txt").write_text(
        f"finish: {finish}, tokens: {tokens}\n\n===== PROMPT =====\n{prompt}\n\n===== RESPONSE =====\n{raw}\n",
        encoding="utf-8")


# --------------------------------------------------------------------------- модель


def strip_think(text: str) -> str:
    """Убирает рассуждение модели: всё до последнего </think>."""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    return text.strip()


async def ask_llm(prompt: str, system: str, agent: str = "unknown",
                  temperature: float = 0.1, retries: int = 2,
                  think: bool | None = None, max_tokens: int | None = None) -> dict:
    """Запрос к Qwen. Возвращает {"text", "tokens", "seconds", "finish"}.

    finish == "length" — ответ обрезан по лимиту токенов.
    think — включить рассуждения модели (по умолчанию LLM_THINKING из .env).
    """
    import httpx  # pip install httpx

    global _THINK_SWITCH_SUPPORTED
    if not llm_configured():
        raise RuntimeError("не заданы LLM_URL и LLM_API_KEY")

    think = LLM_THINKING if think is None else think
    user = prompt if think else prompt + "\n\n/no_think"   # мягкий переключатель Qwen3
    started = time.time()
    last_error: Exception | None = None
    errors, rate_limited = 0, 0
    while True:
        await _respect_interval()
        body = {
            "model": LLM_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens or LLM_MAX_TOKENS,
        }
        if _THINK_SWITCH_SUPPORTED:
            body["chat_template_kwargs"] = {"enable_thinking": think}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10), verify=False) as client:
                r = await client.post(f"{LLM_URL}/chat/completions",
                                      headers={"Authorization": f"Bearer {LLM_API_KEY}"}, json=body)
            if r.status_code in (400, 422) and "chat_template_kwargs" in body:
                # сервер не знает этот параметр — дальше работаем без него (остаётся /no_think)
                _THINK_SWITCH_SUPPORTED = False
                continue
            if r.status_code == 429:
                # лимит частоты запросов: ждём (Retry-After или 15, 30, 60, 60... с) и пробуем снова
                rate_limited += 1
                if rate_limited > LLM_RATE_LIMIT_RETRIES:
                    raise RuntimeError(f"лимит запросов к модели (429) не снимается после "
                                       f"{LLM_RATE_LIMIT_RETRIES} ожиданий")
                wait = _retry_after(r) or min(60, 15 * 2 ** (rate_limited - 1))
                _track_wait(agent, wait)
                print(f"[llm] 429: лимит запросов, жду {wait} с (попытка {rate_limited}/{LLM_RATE_LIMIT_RETRIES})",
                      file=sys.stderr)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            raw = choice["message"].get("content") or ""
            result = {
                "text": strip_think(raw),
                "tokens": data.get("usage", {}).get("total_tokens", 0),
                "seconds": round(time.time() - started, 2),
                "finish": choice.get("finish_reason"),
            }
            _track(agent, result["tokens"], result["seconds"])
            _save_debug(agent, user, raw, result["finish"], result["tokens"])
            return result
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_error = e
            errors += 1
            if errors > retries:
                break
            await asyncio.sleep(5 * errors)
    raise RuntimeError(f"модель недоступна: {last_error}")


def _retry_after(response) -> int | None:
    """Сколько секунд просит подождать сервер (заголовок Retry-After)."""
    value = response.headers.get("retry-after") if hasattr(response, "headers") else None
    try:
        return max(1, min(300, int(float(value)))) if value else None
    except ValueError:
        return None


def _track_wait(agent: str, seconds: float) -> None:
    u = USAGE.setdefault(agent, {"calls": 0, "tokens": 0, "seconds": 0.0})
    u["rate_limit_waits"] = u.get("rate_limit_waits", 0) + 1
    u["rate_limit_seconds"] = u.get("rate_limit_seconds", 0) + seconds


_last_request = 0.0


async def _respect_interval() -> None:
    """Пауза между запросами (LLM_MIN_INTERVAL), чтобы не упираться в лимит частоты."""
    global _last_request
    gap = LLM_MIN_INTERVAL - (time.time() - _last_request)
    if gap > 0:
        await asyncio.sleep(gap)
    _last_request = time.time()


# --------------------------------------------------------------------------- разбор ответа

_BLOCK = re.compile(r"```[ \t]*([\w#+-]*)[ \t]*\n(.*?)```", re.S)


def extract_code(text: str, langs: tuple[str, ...] = ("csharp", "cs", "c#")) -> str:
    """Самый длинный блок кода нужного языка. Если блок не закрыт (ответ обрезан) — всё после ```."""
    blocks = _BLOCK.findall(text)
    same = [b[1] for b in blocks if b[0].lower() in langs] or [b[1] for b in blocks]
    if same:
        return max(same, key=len).strip()
    m = re.search(r"```[ \t]*[\w#+-]*[ \t]*\n(.*)", text, re.S)
    return (m.group(1) if m else text).strip()


def extract_json(text: str):
    """JSON из ответа: блок ```json или первый {...}. Бросает ValueError, если не получилось."""
    candidates = [extract_code(text, ("json",))]
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ValueError("в ответе нет корректного JSON")


# --------------------------------------------------------------------------- утилиты


def safe_path(root: str | Path, rel: str) -> Path:
    """Путь внутри root. Запрещены абсолютные пути и выход наружу через '..'."""
    rel_path = Path(str(rel).replace("\\", "/"))
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts:
        raise ValueError(f"недопустимый путь: {rel}")
    root = Path(root).resolve()
    full = (root / rel_path).resolve()
    if root != full and root not in full.parents:
        raise ValueError(f"путь выходит за папку вывода: {rel}")
    return full
