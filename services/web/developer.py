"""Разработчик фронтенда: пишет компоненты экранов (Angular 20 + Angular Material) по ui_design.json.

- write_file() — один файл за вызов модели: экран или общий модуль
- fix_file()   — исправляет один файл по ошибкам сборки или замечаниям тестировщика
- check_file() — быстрые проверки без сборки (импорты только из разрешённых пакетов и т.п.)
"""

from __future__ import annotations

import json
import os
import re

from devops import ALLOWED_IMPORT_PREFIXES
from llm import ask_llm, extract_code

SYSTEM = (
    "You are a senior Angular 20 developer. You write complete, compilable TypeScript with strict mode "
    "and strict templates. You answer with exactly one ```typescript code block containing one complete file "
    "and nothing else."
)

RULES = """## Правила кода (обязательны)
- Angular 20.1, standalone-компоненты (без NgModule), TypeScript strict, strictTemplates.
- Доступны ТОЛЬКО пакеты @angular/* (core, common, forms, router, material, cdk) и rxjs. Других библиотек нет.
- Один файл = один компонент: @Component({ selector: 'app-...', imports: [...], template: `...`,
  styles: [`...`] }) и `export class <ИмяКомпонента>`. Шаблон и стили — внутри файла (inline).
  Внутри шаблона не используй символ обратной кавычки.
- Зависимости через inject(): `private readonly api = inject(ApiService);`.
- Все запросы к бэкенду — только через ApiService (методы перечислены ниже). HttpClient напрямую не используй.
  Методы возвращают Observable<any>: подписывайся через .subscribe({ next, error }).
- Шаблоны: новый синтаксис @if / @for (item of items; track item.id) / @empty. Не используй *ngIf и *ngFor.
- Состояние — обычные поля класса (zone.js включён), например `items: any[] = []; loading = false;`.
- Angular Material: импортируй нужные модули в imports компонента — MatTableModule, MatButtonModule,
  MatFormFieldModule, MatInputModule, MatSelectModule, MatDatepickerModule, MatCardModule,
  MatProgressSpinnerModule, MatSnackBarModule, MatChipsModule, MatDividerModule.
  Импорт только из подпутей, у корня '@angular/material' экспортов нет:
  {material_paths}.
  Не используй mat-icon (шрифт иконок не подключён) и MatDialog — формы создания и редактирования
  размещай на той же странице (карточка над таблицей). Форма создания видна сразу, без кнопки-переключателя
  «показать/скрыть форму»: приёмочные тесты открывают страницу и ищут поля и кнопки без кликов.
- Тексты заголовков, подписей полей и кнопок, указанные в требованиях, используй дословно.
- Формы — ReactiveFormsModule: `private readonly fb = inject(FormBuilder);`, форма строго через
  `this.fb.nonNullable.group({ name: ['', Validators.required], ... })`. Несколько валидаторов — только
  массивом во втором элементе: `amount: [0, [Validators.required, Validators.min(0)]]` (третий элемент
  Angular считает асинхронным валидатором, и страница падает при загрузке). Тело запроса — из
  `this.form.getRawValue()`: тогда значения имеют тип string/number, а не string | undefined, и подходят
  к типам запросов ApiService. Для необязательных полей тела: `value || undefined`.
  Кнопка отправки неактивна, пока форма невалидна.
- Значения, которые могут быть undefined или null, не передавай туда, где ожидается string:
  используй `?? ''`, проверку `if (!x) return;` или оператор `!` только когда значение точно есть.
- Ошибки и успешные действия — MatSnackBar: `this.snack.open('Сохранено', 'OK', { duration: 3000 })`.
- Даты отправляй строками ISO: дата без времени — 'YYYY-MM-DD', дата со временем — toISOString().
- Перечисления — строками, ровно как в контракте API.
- Параметр маршрута: `inject(ActivatedRoute).snapshot.paramMap.get('id')`. Ссылки — routerLink (импорт RouterLink).
- Корневой элемент шаблона — <section class="page"> с заголовком <h2>. Готовые CSS-классы: page, toolbar-row, form-grid.
- Весь текст интерфейса — на русском языке.
- Допущения отмечай комментарием // ASSUMPTION: ...
"""

WRITE_PROMPT = """Напиши файл {path} административного веб-интерфейса.

## Что в файле
{spec}
{task}
## Интерфейс целиком (для навигации между экранами)
{app}

## Клиент API (уже сгенерирован; импорт из '{api_import}')
{api}

## Общие файлы проекта, от которых зависит этот файл
{deps}

{rules}
Верни полный файл {path} одним блоком ```typescript."""

FIX_PROMPT = """Исправь ошибки в файле {path}.

## Текущий файл {path}
```typescript
{code}
```

## Ошибки в этом файле (от сборки или тестировщика; «подсказка» — как исправить)
{errors}

## Клиент API (импорт из '{api_import}')
{api}

## Связанные файлы проекта (экспорты)
{related}

{rules}
Исправь только то, что вызывает ошибки, не удаляй функциональность.
Верни полный исправленный файл {path} одним блоком ```typescript."""

MATERIAL_PATHS = {
    "MatTableModule": "table", "MatButtonModule": "button", "MatFormFieldModule": "form-field",
    "MatInputModule": "input", "MatSelectModule": "select", "MatDatepickerModule": "datepicker",
    "MatCardModule": "card", "MatProgressSpinnerModule": "progress-spinner", "MatSnackBarModule": "snack-bar",
    "MatSnackBar": "snack-bar", "MatChipsModule": "chips", "MatDividerModule": "divider",
}
RULES = RULES.replace("{material_paths}", ", ".join(
    f"{name} — '@angular/material/{sub}'" for name, sub in MATERIAL_PATHS.items()))

LANGS = ("typescript", "ts")
DEPS_BUDGET = 16000
_IMPORT = re.compile(r"""^\s*import\s[^;]*?from\s+['"]([^'"]+)['"]""", re.M)
_EXPORT = re.compile(r"^\s*(export |@Component|@Injectable|  selector:)")


TASK_LIMIT = 6000
# [значение, Validators.x, Validators.y] — второй валидатор попадает в слот асинхронных и роняет страницу
SYNC_VALIDATORS_AS_ASYNC = re.compile(r"(\w+\s*:\s*\[[^\[\]]*?,\s*Validators\.[^\[\],]+(?:\([^)]*\))?\s*,\s*Validators\.[^\]]*\])")


def task_block(task: str) -> str:
    """Бизнес-задача для экрана: тексты интерфейса и замечания тестировщика видит тот, кто пишет код."""
    if not task.strip():
        return ""
    if len(task) > TASK_LIMIT:  # отчёты тестировщика длинные: начало задачи и последние замечания важнее середины
        task = task[:TASK_LIMIT // 2] + "\n…\n" + task[-TASK_LIMIT // 2:]
    return ("\n## Бизнес-задача (из неё — только то, что относится к этому экрану)\n"
            "Тексты интерфейса, названные здесь (заголовки, подписи полей, надписи кнопок), используй дословно — "
            "по ним приёмочные тесты ищут элементы на странице. Замечания тестировщика ниже обязательно учти.\n"
            f"{task}\n")


def api_import(path: str) -> str:
    """Относительный путь импорта ApiService из файла path."""
    rel = os.path.relpath("src/app/api/api.service", os.path.dirname(path)).replace("\\", "/")
    return rel if rel.startswith(".") else "./" + rel


def outline(code: str) -> str:
    return "\n".join(l.rstrip() for l in code.splitlines() if _EXPORT.match(l))


def check_file(path: str, code: str, component: str | None = None) -> list[str]:
    """Быстрые проверки без сборки."""
    p: list[str] = []
    if not code.strip():
        return ["файл пустой"]
    if "```" in code:
        p.append("в коде остались символы ``` от markdown")
    if code.count("{") != code.count("}"):
        p.append(f"не сходятся фигурные скобки ({code.count('{')} '{{' и {code.count('}')} '}}') — вероятно, ответ обрезан")
    for mod in _IMPORT.findall(code):
        if not mod.startswith(ALLOWED_IMPORT_PREFIXES):
            p.append(f"импорт '{mod}': такого пакета нет в проекте (доступны только @angular/* и rxjs)")
        if mod == "@angular/material":
            p.append("импорт из '@angular/material' не работает — у корня пакета нет экспортов; "
                     "импортируй каждый модуль из его подпути, например MatCardModule из '@angular/material/card', "
                     "MatSnackBar из '@angular/material/snack-bar'")
    if component:
        if "@Component" not in code:
            p.append("нет декоратора @Component")
        if not re.search(rf"export\s+class\s+{re.escape(component)}\b", code):
            p.append(f"класс компонента должен называться {component} и экспортироваться")
        if re.search(r"\bHttpClient\b", code):
            p.append("HttpClient напрямую не используется — только ApiService")
        if re.search(r"\*ng(If|For)\b", code):
            p.append("*ngIf/*ngFor не используются — нужен синтаксис @if/@for")
        if "<mat-icon" in code:
            p.append("mat-icon не используется (шрифт иконок не подключён)")
    for control in SYNC_VALIDATORS_AS_ASYNC.findall(code):
        p.append(f"`{control.strip()}`: несколько валидаторов без массива — третий элемент Angular считает "
                 "асинхронным валидатором и страница падает при загрузке; пиши [значение, [Validators.a, Validators.b]]")
    return p


def _app_overview(design_data: dict) -> str:
    screens = [{"route": "/" + s["route"], "title": s.get("title"), "component": s["component"]}
               for s in design_data.get("screens", [])]
    return json.dumps({"app": design_data.get("app"), "screens": screens}, ensure_ascii=False, indent=1)


def _deps(spec: dict, written: dict[str, str]) -> str:
    names = [d for d in spec.get("depends_on") or [] if d in written]
    if not names:
        return "(нет)"
    parts, used = [], 0
    for name in names:
        code = written[name]
        if used + len(code) > DEPS_BUDGET:
            code = "// только экспорты\n" + outline(code)
        used += len(code)
        parts.append(f"### {name}\n```typescript\n{code}\n```")
    return "\n\n".join(parts)


async def write_file(design_data: dict, spec: dict, written: dict[str, str], api_ref: str,
                     retries: int = 1, avoid: list[str] | None = None, task: str = "") -> dict:
    """Пишет один файл. spec — элемент designer.plan(). Возвращает {"code", "problems", "attempts"}.

    avoid — ошибки прошлой версии файла (когда исправление не помогло и файл пишется заново).
    """
    path = spec["path"]
    screen = spec.get("screen")
    component = screen["component"] if screen else None
    if screen:
        what = json.dumps({k: screen.get(k) for k in
                           ("route", "title", "component", "kind", "purpose", "api", "actions")},
                          ensure_ascii=False, indent=1)
        what = f"Экран (компонент {component}, селектор app-{re.sub(r'(?<!^)(?=[A-Z])', '-', component).lower()}):\n{what}"
    else:
        what = f"Общий модуль: {spec.get('purpose', '')}"
    base = WRITE_PROMPT.format(path=path, spec=what, task=task_block(task) if screen else "",
                               app=_app_overview(design_data), api_import=api_import(path),
                               api=api_ref, deps=_deps(spec, written), rules=RULES)
    if avoid:
        base += ("\n\n## Прошлая версия этого файла не собиралась, исправления не помогли. "
                 "Напиши файл заново и не допусти этих ошибок:\n" + "\n".join(f"- {e}" for e in avoid))
    prompt, code, problems, attempt = base, "", [], 0
    for attempt in range(1, retries + 2):
        result = await ask_llm(prompt, SYSTEM, agent="developer")
        code = extract_code(result["text"], LANGS)
        problems = check_file(path, code, component)
        if result["finish"] == "length":
            problems.insert(0, "ответ обрезан по лимиту токенов — напиши файл компактнее")
        if not problems:
            break
        prompt = base + "\n\n## Предыдущий вариант не прошёл проверку, исправь:\n" + \
            "\n".join(f"- {x}" for x in problems)
    return {"code": code, "problems": problems, "attempts": attempt}


async def fix_file(path: str, code: str, errors: list[str], written: dict[str, str], api_ref: str,
                   component: str | None = None) -> dict:
    """Исправляет один файл. Если новый вариант не прошёл быстрые проверки — остаётся старый код."""
    related = "\n\n".join(f"### {name}\n```typescript\n{outline(c)}\n```"
                          for name, c in written.items() if name != path) or "(нет)"
    prompt = FIX_PROMPT.format(path=path, code=code, errors="\n".join(f"- {e}" for e in errors),
                               api_import=api_import(path), api=api_ref, related=related[:DEPS_BUDGET],
                               rules=RULES)
    result = await ask_llm(prompt, SYSTEM, agent="developer_fix")
    new_code = extract_code(result["text"], LANGS)
    problems = check_file(path, new_code, component)
    return {"code": new_code if not problems else code, "problems": problems}
