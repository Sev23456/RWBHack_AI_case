"""UI-проектировщик: бизнес-задача + контракт API -> ui_design.json (экраны, навигация, план файлов).

Проектировщик сам решает, какие экраны нужны административному интерфейсу и какие вызовы API
использует каждый экран. Код проверяет:
  - каждый вызов API есть в контракте бэкенда (иначе фронтенд не состыкуется);
  - маршруты, имена компонентов и пути файлов корректны и уникальны, пути — только src/app/**/*.ts;
  - экранов не больше лимита.
Мелкие несостыковки чинятся кодом (repair), остальное — повторный запрос с перечнем проблем.
"""

from __future__ import annotations

import json
import re

from llm import ask_llm, extract_json, safe_path

SYSTEM = (
    "You are a senior frontend architect designing an Angular 20 admin web interface with Angular Material. "
    "You answer with exactly one ```json code block and nothing else."
)

PROMPT = """Спроектируй административный веб-интерфейс по бизнес-задаче. Бэкенд уже готов, его API ниже.
Верни JSON строго по схеме:

```json
{{
  "app": {{"title": "короткое название интерфейса", "summary": "одна фраза"}},
  "screens": [
    {{"route": "<kebab-case>", "title": "<название в меню>", "component": "<Name>Page",
      "file": "src/app/pages/<kebab-case>.ts", "kind": "list|form|detail|dashboard|log",
      "purpose": "что делает пользователь на экране",
      "api": ["<METHOD> <путь из контракта>"],
      "actions": ["действие пользователя"], "in_menu": true}}
  ],
  "shared": [{{"path": "src/app/shared/<file>.ts", "purpose": "...", "depends_on": []}}],
  "assumptions": ["допущение там, где задача не уточняет"]
}}
```

## Правила
- Стек: Angular 20, standalone-компоненты, Angular Material. Один экран = один файл компонента.
- Только экраны, нужные администратору по задаче: справочники (таблица + создание/редактирование),
  операции из задачи, журналы и списки событий, сводка. Не больше {max_screens} экранов.
- api — ТОЛЬКО вызовы из раздела «API бэкенда», в точности как там записаны (метод и путь).
  Не придумывай эндпоинты, которых нет. Если для функции задачи нет API — запиши это в assumptions.
- route — kebab-case латиницей, без "/" в начале; для экрана с параметром: "<route>/:id" и in_menu: false.
- shared — только если общий код действительно нужен нескольким экранам; обычно пусто.
- Клиент API, модели, маршруты, оболочка с меню генерируются автоматически — их не планируй.
- Пиши кратко: title, purpose, actions — не длиннее 10 слов.
- Тексты интерфейса, названные в задаче (заголовки страниц, подписи полей, надписи кнопок, сообщения),
  переноси ДОСЛОВНО в кавычках «…» в title, purpose и actions экрана, например: actions: ["кнопка «Добавить»"].
  Не перефразируй их: по ним приёмочные тесты ищут элементы на странице.
{extra}
## API бэкенда
{api}

## Бизнес-задача
{task}
"""

FIX_SUFFIX = """
## Предыдущий ответ не прошёл проверку. Исправь проблемы и верни полный JSON заново:
{problems}
"""

GENERATED = {"src/app/app.ts", "src/app/app.routes.ts", "src/app/app.config.ts"}
_ROUTE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/(?:[a-z0-9]+(?:-[a-z0-9]+)*|:[a-zA-Z]\w*))*$")


def api_key(entry: str) -> str | None:
    """'GET /api/x/{itemId}' -> 'GET /api/x/{}' (имена параметров не важны)."""
    m = re.match(r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)", str(entry), re.I)
    if not m:
        return None
    return m.group(1).upper() + " " + re.sub(r"\{[^}]*\}", "{}", m.group(2).rstrip("/") or "/")


def contract_keys(ops: list[dict]) -> dict[str, dict]:
    return {api_key(f"{o['method']} {o['path']}"): o for o in ops}


def _kebab(text: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", str(text))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "page"


def _pascal(text: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^0-9A-Za-z]+", str(text)) if p) or "Page"


def repair(design, ops: list[dict], drop_unknown_api: bool = False) -> list[str]:
    """Мелкие исправления кодом. Возвращает список исправлений."""
    if not isinstance(design, dict) or not isinstance(design.get("screens"), list):
        return []
    fixes: list[str] = []
    keys = contract_keys(ops)
    design["screens"] = [s for s in design["screens"] if isinstance(s, dict)]
    for s in design["screens"]:
        if not s.get("route") and s.get("title"):
            s["route"] = _kebab(s["title"])
            fixes.append(f"экран «{s['title']}»: маршрут {s['route']}")
        route = str(s.get("route", "")).strip().strip("/")
        s["route"] = route
        base = route.split("/:")[0].split("/")[0] if route else "page"
        if not s.get("component"):
            s["component"] = _pascal(base) + ("Detail" if ":" in route else "") + "Page"
            fixes.append(f"{route}: компонент {s['component']}")
        if not s.get("file"):
            s["file"] = f"src/app/pages/{_kebab(s['component'].removesuffix('Page'))}.ts"
            fixes.append(f"{route}: файл {s['file']}")
        if ":" in route:
            s["in_menu"] = False
        apis, unknown = [], []
        for entry in s.get("api") or []:
            k = api_key(entry)
            if k in keys:
                o = keys[k]
                apis.append(f"{o['method']} {o['path']}")
            else:
                unknown.append(str(entry))
        if unknown and drop_unknown_api:
            fixes.append(f"{route}: убраны вызовы, которых нет в контракте: {unknown}")
            s["api"] = apis
        elif not unknown:
            s["api"] = apis
    design.setdefault("shared", [])
    return fixes


def validate(design, ops: list[dict], max_screens: int) -> list[str]:
    if not isinstance(design, dict):
        return ["ответ не JSON-объект"]
    p: list[str] = []
    screens = design.get("screens")
    if not isinstance(screens, list) or not screens:
        return ["screens пустой"]
    if len(screens) > max_screens:
        p.append(f"экранов {len(screens)}, допустимо не больше {max_screens}")
    keys = contract_keys(ops)
    routes, comps, files = set(), set(), set()
    for s in screens:
        route, comp, file = s.get("route", ""), s.get("component", ""), s.get("file", "")
        if not _ROUTE.match(route):
            p.append(f"маршрут '{route}': только kebab-case латиницей, параметры как :id")
        if route in routes:
            p.append(f"маршрут '{route}' повторяется")
        routes.add(route)
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", comp or ""):
            p.append(f"компонент '{comp}': нужно имя класса в PascalCase")
        if comp in comps:
            p.append(f"компонент '{comp}' повторяется")
        comps.add(comp)
        p += _check_file(file, files)
        for entry in s.get("api") or []:
            if api_key(entry) not in keys:
                p.append(f"экран {route}: вызова '{entry}' нет в API бэкенда")
    if not any(s.get("in_menu", True) and ":" not in s.get("route", "") for s in screens):
        p.append("нет ни одного экрана в меню")
    for f in design.get("shared") or []:
        if not isinstance(f, dict):
            p.append(f"shared: ожидается объект, получено {f}")
            continue
        p += _check_file(f.get("path", ""), files)
    return p


def _check_file(path: str, seen: set) -> list[str]:
    try:
        safe_path(".", path)
    except ValueError as e:
        return [str(e)]
    if not path.startswith("src/app/") or not path.endswith(".ts"):
        return [f"файл {path}: только src/app/**/*.ts"]
    if path in GENERATED or path.startswith("src/app/api/"):
        return [f"файл {path} генерируется автоматически — выбери другой путь"]
    if path in seen:
        return [f"файл {path} указан дважды"]
    seen.add(path)
    return []


def api_overview(ops: list[dict]) -> str:
    """Компактное описание контракта для промпта проектировщика."""
    lines = []
    for o in ops:
        body = ""
        if o["body"]:
            body = " | тело: " + ", ".join((o["body"].get("properties") or {}).keys())
        lines.append(f"- {o['method']} {o['path']}" + (f" — {o['summary']}" if o["summary"] else "") + body)
    return "\n".join(lines) or "(в контракте нет эндпоинтов)"


async def design(task: str, ops: list[dict], extra_context: str = "", max_screens: int = 12,
                 max_attempts: int = 2) -> dict:
    """Возвращает {"design", "attempts", "auto_fixes"}. RuntimeError — если проект так и не прошёл проверку."""
    extra = f"\n## Дополнительный контекст (справочно)\n{extra_context}\n" if extra_context else ""
    base = PROMPT.format(task=task, api=api_overview(ops), max_screens=max_screens, extra=extra)
    prompt, problems = base, []
    for attempt in range(1, max_attempts + 1):
        result = await ask_llm(prompt, SYSTEM, agent="ui_designer")
        fixes: list[str] = []
        try:
            data = extract_json(result["text"])
            # на последней попытке несуществующие вызовы API выбрасываются, а не отправляются на переделку
            fixes = repair(data, ops, drop_unknown_api=(attempt == max_attempts))
            problems = validate(data, ops, max_screens)
        except ValueError as e:
            data, problems = None, [str(e)]
        if result["finish"] == "length":
            problems.insert(0, "ответ обрезан по лимиту токенов — сделай проект компактнее")
        if not problems:
            return {"design": data, "attempts": attempt, "auto_fixes": fixes}
        prompt = base + FIX_SUFFIX.format(problems="\n".join(f"- {x}" for x in problems))
    raise RuntimeError("UI-проектировщик не выдал корректный проект: " + "; ".join(problems[:10]))


def plan(design_data: dict) -> list[dict]:
    """Файлы для разработчика в порядке генерации: сначала shared, потом экраны."""
    files = [{"path": f["path"], "purpose": f.get("purpose", ""), "depends_on": f.get("depends_on") or [],
              "kind": "shared"} for f in design_data.get("shared") or [] if isinstance(f, dict)]
    shared = [f["path"] for f in files]
    for s in design_data.get("screens", []):
        files.append({"path": s["file"], "purpose": s.get("purpose", ""), "depends_on": shared,
                      "kind": "screen", "screen": s})
    return files


def summary(design_data: dict) -> str:
    return json.dumps({k: design_data.get(k) for k in ("app", "screens", "shared")}, ensure_ascii=False, indent=1)
