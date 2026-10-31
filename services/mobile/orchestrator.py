"""Оркестратор МАС.

Получает ТЗ (поле data от Requirements Agent) и:
  1. через Qwen определяет классы задач -> план [{agent: Agent, task: Task}],
     где Task = {"task": "...", "context": "..."};
  2. через Qwen составляет общий контракт и добавляет его в context каждого агента;
  3. параллельно вызывает BACKEND / MOBILE / WEB, их zip сохраняет в output/<project>/;
  4. вызывает TESTING, после "ok" от тестировщика — агента документации;
  5. собирает итоговый project.zip: backend/ mobile/ web/ (что есть) + docs/ + manifest.json.

Ошибки:
  - ошибка генерации / несоответствие формату -> до MAX_ATTEMPTS попыток, потом GenerationError
    (отдаётся человеку как 422);
  - ошибка в коде самой МАС -> исключение не перехватывается, падает весь прогон.
"""

import asyncio
import io
import json
import os
import re
import shutil
import time
import zipfile
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from llm import ask_llm

MAX_ATTEMPTS = 3
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))


class Agent(str, Enum):
    BACKEND = "BACKEND"
    MOBILE = "MOBILE"
    WEB = "WEB"
    TESTING = "TESTING"


# Агенты, генерирующие код, и их папки в итоговом архиве
CODE_AGENTS = {Agent.BACKEND: "backend", Agent.MOBILE: "mobile", Agent.WEB: "web"}


class GenerationError(Exception):
    """Генерация не удалась за MAX_ATTEMPTS попыток — нужен человек."""

    def __init__(self, step: str, message: str, attempts: int, agent: str | None = None):
        super().__init__(message)
        self.step = step
        self.message = message
        self.attempts = attempts
        self.agent = agent

    def to_dict(self) -> dict:
        return {
            "error": "generation_failed",
            "step": self.step,
            "agent": self.agent,
            "attempts": self.attempts,
            "last_error": self.message,
        }


PLAN_SYSTEM = """Ты — оркестратор мультиагентной системы генерации приложений.
На вход — ТЗ в JSON от Requirements Agent. Определи, какие агенты нужны, и сформулируй каждому одну задачу на весь его домен.

Агенты:
- BACKEND — серверная часть: C# / ASP.NET Core, PostgreSQL (+ PostGIS, если есть геоданные), Kafka, REST API, Docker / Docker Compose, GitLab CI/CD.
- MOBILE — нативное Android-приложение на Kotlin.
- WEB — web-интерфейс (админка, личный кабинет) на Angular, UI Kit на выбор.
- TESTING — тестирование: unit, integration, contract, E2E. Нужен всегда.

Правила:
1. BACKEND, MOBILE и WEB включай, только если они нужны по ТЗ. TESTING включай всегда. Каждый агент — не больше одного раза.
2. "task" — инструкция агенту: что именно он должен сделать. Агент не видит ТЗ и не помнит прошлых запусков — у него есть только task и context.
3. "context" — только то, что нужно этому агенту: относящиеся к нему функции, user stories, сущности, критерии приёмки, ограничения и версии его стека. Детали других доменов не передавай — только то, как с ними взаимодействовать.
4. Межкомпонентный контракт (API, события, модель данных) не пиши — его добавят отдельно.

Ответь только JSON без пояснений:
[{"agent": "BACKEND", "task": {"task": "...", "context": "..."}}, ...]"""

CONTRACT_SYSTEM = """Ты — архитектор мультиагентной системы генерации приложений.
По ТЗ и плану задач составь общий межкомпонентный контракт, который все агенты обязаны соблюдать и не могут менять.
Включи то, что нужно по ТЗ: REST API (эндпоинты, методы, схемы запросов и ответов, коды ошибок), события Kafka (топики, схемы сообщений), модель данных общих сущностей, внешние интеграции через адаптеры и mock-сервисы.
Формат выбери сам (например, OpenAPI YAML + описание событий). Пиши компактно: только то, что нужно для согласования компонентов, без пояснений и примеров.
Ответь только текстом контракта."""


class Stats:
    """Токены, время и попытки по шагам — для manifest.json."""

    def __init__(self):
        self.started = time.time()
        self.steps: dict[str, dict] = {}

    def add(self, step: str, tokens: int = 0, attempts: int = 0, seconds: float = 0.0):
        s = self.steps.setdefault(step, {"tokens": 0, "attempts": 0, "seconds": 0.0})
        s["tokens"] += tokens
        s["attempts"] += attempts
        s["seconds"] = round(s["seconds"] + seconds, 2)

    def to_dict(self) -> dict:
        return {
            "tokens": sum(s["tokens"] for s in self.steps.values()),
            "seconds": round(time.time() - self.started, 2),
            "steps": self.steps,
        }


def _strip_think(text: str) -> str:
    """Qwen3 возвращает рассуждения перед </think> (открывающего тега может не быть) — отбрасываем их."""
    return text.rsplit("</think>", 1)[-1].strip()


def _extract_json(text: str, kind: type = list):
    """Последний JSON-массив (или объект при kind=dict) в ответе: Qwen может писать похожие на JSON куски в рассуждениях."""
    text = _strip_think(text)
    opener = "[" if kind is list else "{"
    decoder, found = json.JSONDecoder(), None
    i = text.find(opener)
    while i != -1:
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            end = i + 1
        else:
            if isinstance(value, kind):
                found = value  # вложенные скобки внутри найденного значения пропускаем
        i = text.find(opener, end)
    if found is None:
        raise ValueError(f"в ответе нет валидного JSON-{'массива' if kind is list else 'объекта'}")
    return found


TRUNCATED = "\x00truncated"  # call вернул ответ, обрезанный по max_tokens


def _check_truncated(text: str):
    if text == TRUNCATED:
        raise ValueError("ответ обрезан по лимиту токенов — ответь короче")


def _validate_plan(text: str) -> list[dict]:
    """Проверяет план от Qwen. Любое несоответствие — ValueError (будет повтор)."""
    _check_truncated(text)
    raw = _extract_json(text)
    if not raw:
        raise ValueError("ожидался непустой JSON-массив [{agent, task}]")

    plan, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("элемент плана должен быть объектом {agent, task}")
        try:
            agent = Agent(item.get("agent"))
        except ValueError:
            raise ValueError(f"неизвестный агент {item.get('agent')!r}, допустимы: {[a.value for a in Agent]}")
        if agent in seen:
            raise ValueError(f"агент {agent.value} встречается больше одного раза")
        seen.add(agent)

        task = item.get("task")
        if not isinstance(task, dict):
            raise ValueError(f"{agent.value}: task должен быть объектом {{task, context}}")
        for key in ("task", "context"):
            if not isinstance(task.get(key), str) or not task[key].strip():
                raise ValueError(f"{agent.value}: поле task.{key} должно быть непустой строкой")
        plan.append({"agent": agent, "task": {"task": task["task"], "context": task["context"]}})

    if Agent.TESTING not in seen:
        raise ValueError("в плане нет TESTING — он нужен всегда")
    if not seen & CODE_AGENTS.keys():
        raise ValueError("в плане нет ни одного из BACKEND, MOBILE, WEB")
    return plan


def _validate_contract(text: str) -> str:
    _check_truncated(text)
    contract = _strip_think(text)
    if len(contract) < 200:
        raise ValueError("контракт пустой или слишком короткий")
    return contract


def _validate_files(result: dict) -> dict:
    """Результат агента-генератора: {"files": {путь: содержимое}, "tokens": int}."""
    files = result.get("files") if isinstance(result, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError("агент не вернул файлы")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ValueError("files должен быть словарём {путь: текст}")
    if "README.md" not in files:
        raise ValueError("нет README.md с инструкцией по запуску")
    return result


async def _with_retries(step: str, call: Callable[[str], Awaitable], validate: Callable,
                        stats: Stats, agent: str | None = None):
    """Вызывает call(feedback) до MAX_ATTEMPTS раз, пока validate не примет результат.

    Повторяются только ValueError из validate (плохая генерация). Остальные исключения —
    ошибки МАС — летят наверх как есть.
    """
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.time()
        result = await call(feedback)
        stats.add(step, attempts=1, seconds=time.time() - started)
        try:
            return validate(result)
        except ValueError as e:
            feedback = str(e)
            print(f"[{step}] попытка {attempt} отклонена: {feedback}\n{str(result)[-2000:]}", flush=True)
    raise GenerationError(step, feedback, MAX_ATTEMPTS, agent)


async def build_plan(spec: dict, stats: Stats) -> list[dict]:
    """Классы задач и Task для каждого агента."""
    async def call(feedback: str) -> str:
        prompt = json.dumps(spec, ensure_ascii=False, indent=2)
        if feedback:
            prompt += f"\n\nПредыдущий ответ отклонён: {feedback}. Исправь и верни JSON заново."
        r = await ask_llm(prompt, system=PLAN_SYSTEM)
        stats.add("plan", tokens=r["tokens"])
        return TRUNCATED if r.get("truncated") else r["text"]

    return await _with_retries("plan", call, _validate_plan, stats)


async def build_contract(spec: dict, plan: list[dict], stats: Stats) -> str:
    """Общий контракт для всех агентов."""
    tasks = "\n".join(f"- {p['agent'].value}: {p['task']['task']}" for p in plan)

    async def call(feedback: str) -> str:
        prompt = f"ТЗ:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\nПлан задач:\n{tasks}"
        if feedback:
            prompt += f"\n\nПредыдущий ответ отклонён: {feedback}."
        # Без рассуждений: Qwen иначе пишет весь контракт сначала в <think> и не укладывается в лимит
        r = await ask_llm(prompt, system=CONTRACT_SYSTEM, think=False)
        stats.add("contract", tokens=r["tokens"])
        return TRUNCATED if r.get("truncated") else r["text"]

    return await _with_retries("contract", call, _validate_contract, stats)


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
    return buf.getvalue()


async def run_code_agent(agent: Agent, fn: Callable, task: dict, out_dir: Path, stats: Stats) -> Path:
    """Вызывает агента-генератора и сохраняет его zip на диск до тестирования."""
    step = agent.value

    async def call(feedback: str) -> dict:
        t = dict(task)
        if feedback:
            t["task"] += f"\n\nПредыдущая попытка отклонена: {feedback}. Исправь."
        result = await fn(t)
        stats.add(step, tokens=result.get("tokens", 0) if isinstance(result, dict) else 0)
        return result

    result = await _with_retries(step, call, _validate_files, stats, agent=step)
    path = out_dir / f"{CODE_AGENTS[agent]}.zip"
    path.write_bytes(_zip_bytes(result["files"]))
    return path


async def run(spec: dict, agents: dict[Agent, Callable], documentation: Callable) -> bytes:
    """Полный прогон. agents — реализации агентов из плана, documentation — агент документации.

    Сигнатуры:
      BACKEND/MOBILE/WEB: async (task) -> {"files": {...}, "tokens": int}
      TESTING:            async (task, artifacts) -> {"status": "ok"|"failed", "report": str, "tokens": int}
      documentation:      async (spec, contract, artifacts, test_report) -> {"files": {...}, "tokens": int}
    """
    stats = Stats()
    project = re.sub(r"[^\w.-]", "_", str(spec.get("project_name") or "project"))
    out_dir = OUTPUT_DIR / project
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    plan = await build_plan(spec, stats)
    contract = await build_contract(spec, plan, stats)
    (out_dir / "contract.md").write_text(contract, encoding="utf-8")
    for p in plan:
        p["task"]["context"] += f"\n\n## Общий контракт (менять нельзя)\n{contract}"

    # Генераторы кода — параллельно; ошибка любого роняет весь прогон
    code = [p for p in plan if p["agent"] in CODE_AGENTS]
    paths = await asyncio.gather(
        *(run_code_agent(p["agent"], agents[p["agent"]], p["task"], out_dir, stats) for p in code)
    )
    artifacts = {CODE_AGENTS[p["agent"]]: path for p, path in zip(code, paths)}
    artifacts["contract"] = out_dir / "contract.md"

    # Тестировщик сам гоняет цикл /fix с бэкендером; сюда приходит только итог
    testing_task = next(p["task"] for p in plan if p["agent"] == Agent.TESTING)
    started = time.time()
    report = await agents[Agent.TESTING](testing_task, artifacts)
    stats.add("TESTING", tokens=report.get("tokens", 0), attempts=1, seconds=time.time() - started)
    if report.get("status") != "ok":
        raise GenerationError("TESTING", str(report.get("report", "тесты не прошли")), 1, Agent.TESTING.value)

    async def call_docs(feedback: str) -> dict:
        result = await documentation(spec, contract, artifacts, report)
        stats.add("DOCUMENTATION", tokens=result.get("tokens", 0) if isinstance(result, dict) else 0)
        return result

    docs = await _with_retries("DOCUMENTATION", call_docs, _validate_files, stats, agent="DOCUMENTATION")

    # Итоговый архив для пользователя
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in CODE_AGENTS.values():
            if folder in artifacts:
                with zipfile.ZipFile(artifacts[folder]) as part:
                    for name in part.namelist():
                        z.writestr(f"{folder}/{name}", part.read(name))
        for name, content in docs["files"].items():
            z.writestr(f"docs/{name}", content)
        z.writestr("manifest.json", json.dumps(
            {"project": project, "agents": [p["agent"].value for p in plan], **stats.to_dict()},
            indent=2, ensure_ascii=False,
        ))
    archive = buf.getvalue()
    (out_dir / "project.zip").write_bytes(archive)
    return archive
