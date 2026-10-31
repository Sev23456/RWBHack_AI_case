"""Разработчик: пишет C#-файлы по design.json — по одному файлу за вызов модели.

- write_file()  — новый файл по плану: получает проект, назначение файла и код файлов, от которых он зависит
- fix_file()    — исправляет один файл по ошибкам сборки
- check_file()  — быстрые проверки без компилятора
"""

from __future__ import annotations

import json
import re

from designer import summary
from llm import ask_llm, extract_code

SYSTEM = (
    "You are a senior C# 14 / ASP.NET Core 10 developer. You write complete, compilable, idiomatic code. "
    "You answer with exactly one ```csharp code block containing one complete file and nothing else."
)

RULES = """## Правила кода (обязательны)
- .NET 10, C# 14, ASP.NET Core minimal API, EF Core + Npgsql (PostgreSQL 17). Nullable включён, ImplicitUsings включены.
- Доступны ТОЛЬКО пакеты: Microsoft.AspNetCore.OpenApi, Npgsql.EntityFrameworkCore.PostgreSQL,
  Npgsql.EntityFrameworkCore.PostgreSQL.NetTopologySuite, NetTopologySuite.IO.GeoJSON4STJ.
- Все файлы, кроме Program.cs, используют file-scoped namespace: `namespace Backend;`.
- Program.cs — top-level statements, первая строка `using Backend;`; типы в Program.cs не объявляй.
- Эндпоинты в отдельных файлах: `public static class XxxEndpoints` с методом-расширением
  `public static WebApplication MapXxxEndpoints(this WebApplication app)`; Program.cs вызывает их.
- Program.cs: строка подключения builder.Configuration.GetConnectionString("Default")
  ?? "Host=db;Port=5432;Database=app;Username=app;Password=app"; ожидание БД при старте (до 30 попыток по 2 с)
  и db.Database.EnsureCreatedAsync(); app.MapGet("/health", ...);
  CORS: builder.Services.AddCors() ДО builder.Build() и app.UseCors(p => p.AllowAnyOrigin().AllowAnyHeader().AllowAnyMethod());
  OpenAPI: только builder.Services.AddOpenApi() и app.MapOpenApi() — без трансформеров, Swagger/NSwag недоступны;
  JSON: builder.Services.ConfigureHttpJsonOptions(o => { o.SerializerOptions.Converters.Add(
  new JsonStringEnumConverter(JsonNamingPolicy.CamelCase)); o.SerializerOptions.ReferenceHandler =
  ReferenceHandler.IgnoreCycles; }) — нужны using System.Text.Json и System.Text.Json.Serialization;
  фоновые сервисы — builder.Services.AddHostedService<T>() ДО builder.Build(), не создавать их через new.
- События (design.events) пишутся в таблицу журнала событий БД (сущность журнала из проекта: тип, данные
  JSON-строкой, время) и дублируются в лог. Брокеров сообщений (Kafka и т.п.) нет. Публикация — через сервис
  журнала событий из проекта; админка читает журнал через REST.
- Внешние системы — только интерфейс + Mock-реализация, реальных HTTP-вызовов наружу нет.
- Геоданные (если uses_geo): NetTopologySuite, SRID 4326, UseNetTopologySuite() в UseNpgsql,
  GeoJsonConverterFactory в ConfigureHttpJsonOptions.
- Запросы к БД асинхронные; Id сущностей — Guid.
- Имена своих классов и перечислений не должны совпадать со стандартными типами .NET, которые подключены
  через ImplicitUsings: Task, TaskStatus, Thread, Timer, File, Directory, Path, Action, Range, Index, Environment,
  Monitor, Attribute. Для задачи — TaskItem и TaskItemStatus и т.п.; в JSON и маршрутах имена берутся из контракта.
- Даты: дата со временем — DateTimeOffset (текущее — DateTimeOffset.UtcNow); дата без времени — DateOnly.
  DateTime не используй. Тип поля в сущности и в DTO запроса/ответа должен совпадать.
- API отдаёт и принимает DTO (record), а не сущности EF напрямую — у сущностей циклические навигации.
- Перечисления в JSON — строками (JsonStringEnumConverter выше); значения как в design.json.
- Minimal API: не используй атрибуты [FromQuery], [FromBody], [FromRoute], [Required] — параметры
  привязываются сами; если атрибут всё же нужен, добавь соответствующий using.
- В тернарном выражении со значимым типом и null приводи к nullable: cond ? (T?)value : null.
- Проверяй навигационные свойства на null (или используй ?.), Nullable включён.
- Используй только типы из стандартной библиотеки, разрешённых пакетов и переданных файлов проекта.
  Не ссылайся на типы, которых нет; если нужен новый вспомогательный тип — объяви его в этом файле.
- Допущения отмечай комментарием // ASSUMPTION: ...
"""

# Задаётся тимлидом на каждый прогон по стилю имён полей в утверждённом контракте
JSON_NAMING = ""
# Задаётся тимлидом на каждый прогон: утверждённый контракт и задача с замечаниями тестировщика
CONTEXT = ""
CONTRACT_LIMIT = 14000
TASK_LIMIT = 6000


def context_block(task: str, openapi: dict | None) -> str:
    """Контракт (коды ответов, схемы, формат ошибок) и задача — то, по чему тестировщик проверяет код."""
    parts = []
    if openapi:
        api = json.dumps({k: openapi[k] for k in ("paths", "components") if k in openapi}, ensure_ascii=False)
        parts.append("## Утверждённый контракт API — реализуй строго по нему\n"
                     "Коды ответов (например 201 для создания), тела ответов и формат ошибок — ровно как в контракте: "
                     "приёмочные тесты сверяют ответы с ним.\n" + api[:CONTRACT_LIMIT])
    if task.strip():
        if len(task) > TASK_LIMIT:  # отчёты тестировщика длинные: начало задачи и последние замечания важнее середины
            task = task[:TASK_LIMIT // 2] + "\n…\n" + task[-TASK_LIMIT // 2:]
        parts.append("## Бизнес-задача и замечания тестировщика (учти их)\n" + task)
    return "\n" + "\n\n".join(parts) + "\n" if parts else ""
SNAKE_CASE_RULE = (
    "- Имена полей JSON в контракте — snake_case (например created_at). В ConfigureHttpJsonOptions обязательно\n"
    "  o.SerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower; — тогда свойство CreatedAt\n"
    "  отдаётся и принимается как created_at. Свойства C# называй в PascalCase.\n")

WRITE_PROMPT = """Напиши файл {path} для проекта бэкенда.

## Назначение файла
{purpose}
{context}
## Проект (design.json)
{design}

## Уже написанные файлы, от которых зависит этот файл
{deps}

{rules}
Верни полный файл {path} одним блоком ```csharp."""

FIX_PROMPT = """Исправь ошибки в файле {path}.

## Текущий файл {path}
```csharp
{code}
```

## Ошибки в этом файле (от компилятора или тестировщика; «подсказка» — как исправить)
{errors}
{context}
## Связанные файлы проекта (сигнатуры)
{related}

{rules}
Исправь только то, что вызывает ошибки, не удаляй функциональность.
Верни полный исправленный файл {path} одним блоком ```csharp."""

DEPS_BUDGET = 24000      # символов кода зависимостей в одном промпте

_OUTLINE = re.compile(
    r"^\s*(namespace |public |internal |using |\[|app\.Map|.*\bMap(Get|Post|Put|Patch|Delete|Group)\()")


def outline(code: str) -> str:
    """Сигнатуры файла: объявления типов, публичные члены, маршруты. Для экономии контекста."""
    return "\n".join(line.rstrip() for line in code.splitlines() if _OUTLINE.match(line))


def deps_block(spec: dict, written: dict[str, str]) -> str:
    """Код зависимостей: полностью, пока помещается в бюджет, дальше — только сигнатуры."""
    deps = spec.get("depends_on") or []
    names = list(written) if "*" in deps else [d for d in deps if d in written]
    if not names:
        return "(нет)"
    parts, used = [], 0
    for name in names:
        code = written[name]
        if used + len(code) > DEPS_BUDGET:
            code = "// только сигнатуры\n" + outline(code)
        used += len(code)
        parts.append(f"### {name}\n```csharp\n{code}\n```")
    return "\n\n".join(parts)


# Вызовы и библиотеки, которых нет среди разрешённых пакетов: такой код не соберётся
FORBIDDEN = [
    (re.compile(r"\bUseOpenApi\s*\("), "UseOpenApi() — это NSwag, его нет; для OpenAPI только app.MapOpenApi()"),
    (re.compile(r"\bUseSwagger(UI)?\s*\("), "UseSwagger/UseSwaggerUI — Swashbuckle не подключён"),
    (re.compile(r"\bAddSwaggerGen\s*\("), "AddSwaggerGen — Swashbuckle не подключён"),
    (re.compile(r"\bAddOpenApiDocument\s*\("), "AddOpenApiDocument — это NSwag, его нет"),
    (re.compile(r"^\s*using\s+(Swashbuckle|NSwag|Newtonsoft|Dapper|MediatR|AutoMapper|FluentValidation|Confluent)\b",
                re.M), "using пакета, которого нет в проекте"),
]


def check_file(path: str, code: str) -> list[str]:
    """Быстрые проверки файла без компилятора."""
    p = []
    if not code.strip():
        return ["файл пустой"]
    if "```" in code:
        p.append("в коде остались символы ``` от markdown")
    if code.count("{") != code.count("}"):
        p.append(f"не сходятся фигурные скобки ({code.count('{')} '{{' и {code.count('}')} '}}') — вероятно, ответ обрезан")
    if path.endswith("Program.cs"):
        for need, msg in (("WebApplication.CreateBuilder", "нет WebApplication.CreateBuilder"),
                          ("app.Run", "нет app.Run()"), ('"/health"', "нет эндпоинта /health")):
            if need not in code:
                p.append(msg)
    elif "namespace Backend" not in code:
        p.append("нет `namespace Backend;`")
    for pattern, msg in FORBIDDEN:
        if pattern.search(code):
            p.append(msg)
    return p


async def write_file(design_data: dict, spec: dict, written: dict[str, str], retries: int = 1) -> dict:
    """Пишет один файл. Возвращает {"code", "problems", "attempts"}."""
    path = spec["path"]
    base = WRITE_PROMPT.format(path=path, purpose=spec.get("purpose", ""), context=CONTEXT, design=summary(design_data),
                               deps=deps_block(spec, written), rules=RULES + JSON_NAMING)
    prompt, code, problems = base, "", []
    for attempt in range(1, retries + 2):
        result = await ask_llm(prompt, SYSTEM, agent="developer")
        code = extract_code(result["text"])
        problems = check_file(path, code)
        if result["finish"] == "length":
            problems.insert(0, "ответ обрезан по лимиту токенов — напиши файл компактнее")
        if not problems:
            break
        prompt = base + "\n\n## Предыдущий вариант не прошёл проверку, исправь:\n" + \
            "\n".join(f"- {x}" for x in problems)
    return {"code": code, "problems": problems, "attempts": attempt}


async def fix_file(path: str, code: str, errors: list[str], written: dict[str, str]) -> dict:
    """Исправляет один файл по ошибкам (компилятора или тестировщика; подсказки можно включать в текст).

    Возвращает {"code", "problems"}; если новый вариант не прошёл быстрые проверки — остаётся старый код.
    """
    related = "\n\n".join(f"### {name}\n```csharp\n{outline(c)}\n```"
                          for name, c in written.items() if name != path) or "(нет)"
    prompt = FIX_PROMPT.format(path=path, code=code, errors="\n".join(f"- {e}" for e in errors), context=CONTEXT,
                               related=related[:DEPS_BUDGET], rules=RULES + JSON_NAMING)
    result = await ask_llm(prompt, SYSTEM, agent="developer_fix")
    new_code = extract_code(result["text"])
    problems = check_file(path, new_code)
    return {"code": new_code if not problems else code, "problems": problems}
