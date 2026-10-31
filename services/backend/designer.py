"""Проектировщик: текст бизнес-задачи -> design.json (проект бэкенда и план файлов).

Проектировщик сам решает, какие сущности, эндпоинты и C#-файлы нужны под задачу.
Код только проверяет, что проект корректен и укладывается в правила:
  - пути файлов безопасные, только .cs;
  - есть Program.cs и эндпоинт /health;
  - файлов не больше лимита, зависимости без циклов;
  - каждый эндпоинт закреплён за файлом из плана.
Мелкие несостыковки (эндпоинт ссылается на файл, которого нет в плане) чинятся кодом — repair().
Если проверка не пройдена — модель получает список проблем и переделывает (до max_attempts раз).
"""

from __future__ import annotations

import json
import re

from llm import ask_llm, extract_json, safe_path

SYSTEM = (
    "You are a software architect designing a backend service in C# 14 / ASP.NET Core 10 "
    "with EF Core and PostgreSQL. You answer with exactly one ```json code block and nothing else."
)

PROMPT = """Спроектируй бэкенд-сервис по бизнес-задаче и верни JSON строго по этой схеме:

```json
{{
  "service": {{"name": "snake_case_name", "summary": "одна фраза: что делает сервис"}},
  "uses_geo": false,
  "entities": [
    {{"name": "<EntityName>", "description": "...",
      "fields": [{{"name": "<fieldName>", "type": "<type>", "required": true, "description": "..."}}]}}
  ],
  "endpoints": [
    {{"method": "GET|POST|PUT|PATCH|DELETE", "path": "/api/...", "summary": "что делает",
      "request": [{{"name": "<fieldName>", "type": "<type>", "required": true}}],
      "response": "что возвращает", "file": "<путь .cs из files>"}}
  ],
  "events": [{{"name": "snake_case_event", "when": "когда возникает", "payload": ["<fieldName>"]}}],
  "integrations": [{{"name": "<SystemName>", "purpose": "...", "operations": ["<OperationName>"]}}],
  "rules": ["бизнес-правило одной фразой"],
  "assumptions": ["допущение там, где задача не уточняет"],
  "files": [
    {{"path": "<папка>/<Файл>.cs", "purpose": "что в файле", "depends_on": ["<путь другого файла>"]}}
  ]
}}
```

## Правила проектирования
- Стек: C# 14, ASP.NET Core 10, EF Core + PostgreSQL 17, REST API; геоданные — PostGIS (NetTopologySuite).
- Типы полей: string, int, long, decimal, double, bool, datetime, date, guid, enum(a|b|c), json,
  ref:<EntityName> (ссылка на сущность), point, linestring, polygon.
- uses_geo = true, только если в задаче есть координаты, области или маршруты (типы point/linestring/polygon).
- Поле id у сущностей не перечисляй — оно добавится само (Guid).
- Эндпоинты: только то, что нужно по задаче; пути начинаются с /api/; параметры пути — /api/x/{{id}}.
  CRUD добавляй только если он присутствует в утвержденном внешнем контракте. Внешний контракт в контексте обязателен и имеет приоритет над всеми примерами.
  Отдельно опиши GET /health (file: "Program.cs").
- events — значимые изменения состояния. Они сохраняются в журнал событий в БД (без брокеров сообщений):
  если events не пустой, добавь сущность журнала событий (тип, данные, время), сервис публикации событий
  и эндпоинт GET /api/events для административного интерфейса.
- integrations — только внешние системы, упомянутые в задаче; реализуются интерфейсом + mock.
- Эндпоинты реализуются в статических классах (minimal API), а не в контроллерах MVC:
  группируй их по смыслу, один файл — одна группа, например "Endpoints/<Group>Endpoints.cs".
  Поле file у КАЖДОГО эндпоинта — путь файла, который ОБЯЗАТЕЛЬНО есть в files.
- files — план C#-файлов проекта. Структуру выбери сам под задачу. Обязательно есть "Program.cs".
  Не включай .csproj, Dockerfile и прочие не-.cs файлы. Не больше {max_files} файлов.
  depends_on — файлы, типы из которых используются; Program.cs может указать ["*"].
- Не выдумывай функции, которых нет в задаче; недостающее — в assumptions.
- Пиши кратко: description, summary, purpose — не длиннее 10 слов. Без пояснений вне JSON.
{extra}
## Бизнес-задача
{task}
"""

FIX_SUFFIX = """
## Предыдущий ответ не прошёл проверку. Исправь проблемы и верни полный JSON заново:
{problems}
"""


def _group_file(path: str) -> str:
    """Файл для эндпоинта без файла: /api/admin/... -> Endpoints/AdminEndpoints.cs, /api/items/... -> ItemsEndpoints.cs."""
    parts = [x for x in path.strip("/").split("/") if x and not x.startswith("{") and x.lower() != "api"]
    name = "".join(w[:1].upper() + w[1:] for w in re.split(r"[^0-9A-Za-z]+", parts[0]) if w) if parts else "Api"
    return f"Endpoints/{name or 'Api'}Endpoints.cs"


def repair(design) -> list[str]:
    """Чинит мелкие несостыковки кодом, без повторного вызова модели. Возвращает список исправлений.

    - эндпоинт без file -> файл группы по первому сегменту пути;
    - file эндпоинта отсутствует в files -> файл добавляется в план
      (зависит от всех уже запланированных файлов, кроме Program.cs и других файлов эндпоинтов).
    """
    if not isinstance(design, dict) or not isinstance(design.get("files"), list) \
            or not isinstance(design.get("endpoints"), list):
        return []
    fixes: list[str] = []
    files = [f for f in design["files"] if isinstance(f, dict) and f.get("path")]
    design["files"] = files
    planned = {f["path"] for f in files}
    endpoint_files = {e.get("file") for e in design["endpoints"] if isinstance(e, dict)}
    base_deps = [f["path"] for f in files if f["path"] != "Program.cs" and f["path"] not in endpoint_files]

    for e in design["endpoints"]:
        if not isinstance(e, dict):
            continue
        if str(e.get("path")) == "/health":
            e["file"] = "Program.cs"
            continue
        if not e.get("file"):
            e["file"] = _group_file(str(e.get("path", "")))
            fixes.append(f"{e.get('method')} {e.get('path')}: назначен файл {e['file']}")
        f = str(e["file"])
        if f not in planned and f.endswith(".cs"):
            files.insert(max(0, len(files) - 1) if files and files[-1]["path"] == "Program.cs" else len(files),
                         {"path": f, "purpose": "эндпоинты", "depends_on": list(base_deps)})
            planned.add(f)
            fixes.append(f"файл {f} добавлен в план (на него ссылаются эндпоинты)")
    return fixes


def validate(design, max_files: int) -> list[str]:
    """Проблемы в проекте (пустой список — всё хорошо)."""
    if not isinstance(design, dict):
        return ["ответ не JSON-объект"]
    p: list[str] = []

    entities = design.get("entities")
    if not isinstance(entities, list) or not entities:
        p.append("entities пустой")
    else:
        for e in entities:
            if not isinstance(e, dict) or not e.get("name"):
                p.append(f"сущность без имени: {e}")
            elif not isinstance(e.get("fields"), list) or not e["fields"]:
                p.append(f"у сущности {e['name']} нет полей")

    files = design.get("files")
    paths: list[str] = []
    if not isinstance(files, list) or not files:
        p.append("files пустой")
    else:
        for f in files:
            path = str(f.get("path", "")) if isinstance(f, dict) else ""
            try:
                safe_path(".", path)
            except ValueError as e:
                p.append(str(e))
                continue
            if not path.endswith(".cs"):
                p.append(f"файл {path}: разрешены только .cs")
            elif path in paths:
                p.append(f"файл {path} указан дважды")
            else:
                paths.append(path)
        if "Program.cs" not in paths:
            p.append("в files нет Program.cs")
        if len(paths) > max_files:
            p.append(f"файлов {len(paths)}, допустимо не больше {max_files}")
        for f in files:
            if isinstance(f, dict):
                for d in f.get("depends_on") or []:
                    if d != "*" and d not in paths:
                        p.append(f"{f.get('path')}: зависимость {d} отсутствует в files")
        if not p and topo_order(files) is None:
            p.append("в depends_on есть циклические зависимости")

    endpoints = design.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        p.append("endpoints пустой")
    else:
        if not any(str(e.get("path", "")) == "/health" for e in endpoints if isinstance(e, dict)):
            p.append("нет эндпоинта GET /health")
        for e in endpoints:
            if not isinstance(e, dict) or not str(e.get("path", "")).startswith("/"):
                p.append(f"некорректный эндпоинт: {e}")
            elif paths and e.get("file") not in paths:
                p.append(f"эндпоинт {e.get('method')} {e.get('path')}: file '{e.get('file')}' нет в files")
    return p


def topo_order(files: list[dict]) -> list[str] | None:
    """Порядок генерации: сначала зависимости. Файлы с "*" — в конце. None — если есть цикл."""
    deps = {f["path"]: [d for d in (f.get("depends_on") or []) if d != "*"] for f in files}
    star = [f["path"] for f in files if "*" in (f.get("depends_on") or [])]
    order: list[str] = []
    state: dict[str, int] = {}          # 1 — в обработке, 2 — готов

    def visit(n: str) -> bool:
        if state.get(n) == 2:
            return True
        if state.get(n) == 1:
            return False                # цикл
        state[n] = 1
        if not all(visit(d) for d in deps.get(n, [])):
            return False
        state[n] = 2
        order.append(n)
        return True

    for path in deps:
        if path not in star and not visit(path):
            return None
    for path in star:
        if not visit(path):
            return None
    return order


async def design(task: str, extra_context: str = "", max_files: int = 15, max_attempts: int = 2) -> dict:
    """Возвращает {"design": dict, "attempts": n}. Бросает RuntimeError, если проект так и не прошёл проверку."""
    extra = f"\n## Дополнительный контекст (справочно)\n{extra_context}\n" if extra_context else ""
    base = PROMPT.format(task=task, max_files=max_files, extra=extra)
    prompt, problems = base, []

    for attempt in range(1, max_attempts + 1):
        result = await ask_llm(prompt, SYSTEM, agent="designer")
        fixes: list[str] = []
        try:
            data = extract_json(result["text"])
            fixes = repair(data)
            problems = validate(data, max_files)
        except ValueError as e:
            data, problems = None, [str(e)]
        if result["finish"] == "length":
            problems.insert(0, "ответ обрезан по лимиту токенов — сделай проект компактнее")
        if not problems:
            return {"design": data, "attempts": attempt, "auto_fixes": fixes}
        prompt = base + FIX_SUFFIX.format(problems="\n".join(f"- {x}" for x in problems))

    raise RuntimeError("проектировщик не выдал корректный проект: " + "; ".join(problems[:10]))


def summary(design_data: dict) -> str:
    """Короткое текстовое представление проекта для промптов других агентов."""
    return json.dumps({k: design_data.get(k) for k in
                       ("service", "uses_geo", "entities", "endpoints", "events", "integrations", "rules", "files")},
                      ensure_ascii=False, indent=1)
