"""Тимлид бэкенда — точка входа системы бэкенда.

Вход (JSON):
  {"task": "текст бизнес-задачи", "context": { ...что угодно: пути, порты, переменные... }}
  context необязателен; известные ключи используются, остальное передаётся проектировщику справочно,
  секреты (пароли, токены, ключи) в промпты и отчёт не попадают.

Режим исправления: context = {"mode": "fix", "feedback": {"errors": [{"file", "line", "message", "fix_hint"}]}}
  — проект берётся из готового design.json, разработчик исправляет только файлы с замечаниями,
  затем сборка и сверка как обычно.

Порядок:
  1. Проектировщик: задача -> design.json (проект + план C#-файлов), с проверкой.
  2. DevOps: Backend.csproj, Dockerfile, compose.backend.yml, .env.example.
  3. Разработчик: C#-файлы по плану, по одному, в порядке зависимостей.
  4. DevOps: сборка; ошибки -> разработчик чинит конкретные файлы (до max_fix попыток).
  5. Тимлид: сверка (все эндпоинты в коде, /health), openapi.yaml для фронтенда и мобайла, report.json.

Запуск:
  python teamlead.py input.json
  python teamlead.py input.json --resume     # продолжить прерванный прогон (не тратя токены заново)
  echo '{"task": "..."}' | python teamlead.py -
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import designer
import developer
import devops
from llm import USAGE, reset_usage, safe_path, set_debug_dir

if os.getenv("CODER_URL"):  # кодер вынесен в отдельный контейнер
    import llm
    from coder_client import RemoteDeveloper
    developer = RemoteDeveloper(os.environ["CODER_URL"], developer, llm)

# --------------------------------------------------------------------------- context

DEFAULTS = {
    "output_dir": "output/backend",
    "contract_out": None,            # по умолчанию <output_dir>/openapi.yaml
    "port": 8080,
    "max_files": 15,
    "max_design_attempts": 2,
    "max_fix": 3,
}

# Как известные параметры могут называться в context (первое найденное — используется)
ALIASES = {
    "output_dir": ["output_dir", "out_dir", "output", "backend_dir", "paths.backend", "paths.output", "dirs.backend"],
    "contract_out": ["contract_out", "contract_path", "openapi_out", "openapi_path", "paths.contract", "paths.openapi"],
    "port": ["port", "backend_port", "ports.backend", "ports.api"],
    "max_files": ["max_files", "limits.max_files"],
    "max_design_attempts": ["max_design_attempts", "limits.max_design_attempts"],
    "max_fix": ["max_fix", "max_attempts", "limits.max_fix", "limits.max_attempts"],
    "db_name": ["db_name", "database", "db.name", "env.DB_NAME"],
    "db_user": ["db_user", "db.user", "env.DB_USER"],
    "db_port": ["db_port", "ports.db", "ports.postgres"],
    "service": ["service_name", "backend_service"],
}

SECRET_RE = re.compile(r"pass(word)?|secret|token|api[_-]?key|private|credential|auth", re.I)
CONTEXT_PROMPT_LIMIT = 100000      # символов context в промпте проектировщика


def lookup(ctx: dict, dotted: str):
    cur = ctx
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def redact(obj):
    """Копия без секретов: значения ключей вроде password/token/api_key заменяются на ***."""
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_RE.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def resolve_settings(ctx: dict) -> tuple[dict, list[str]]:
    """Известные параметры из context + умолчания. Возвращает (settings, неиспользованные ключи верхнего уровня)."""
    settings = dict(DEFAULTS)
    used_top: set[str] = set()
    for name, keys in ALIASES.items():
        for key in keys:
            value = lookup(ctx, key)
            if value not in (None, "", {}):
                settings[name] = value
                used_top.add(key.split(".")[0])
                break
    for name in ("port", "max_files", "max_design_attempts", "max_fix", "db_port"):
        if name in settings:
            try:
                settings[name] = int(settings[name])
            except (TypeError, ValueError):
                settings[name] = DEFAULTS.get(name)
    unused = [k for k in ctx if k not in used_top]
    return settings, unused


# --------------------------------------------------------------------------- контракт

_TYPES = {"int": ("integer", None), "long": ("integer", "int64"), "decimal": ("number", None),
          "double": ("number", None), "bool": ("boolean", None), "datetime": ("string", "date-time"),
          "date": ("string", "date"), "guid": ("string", "uuid"), "json": ("object", None),
          "point": ("object", "geojson"), "linestring": ("object", "geojson"), "polygon": ("object", "geojson")}


def _schema(field_type: str) -> dict:
    t = str(field_type).lower()
    if t.startswith("ref:"):
        return {"type": "string", "format": "uuid"}
    if t.startswith("enum("):
        return {"type": "string", "enum": [v.strip() for v in re.split(r"[|,]", t[5:].rstrip(")")) if v.strip()]}
    typ, fmt = _TYPES.get(t, ("string", None))
    return {"type": typ, **({"format": fmt} if fmt else {})}


def _snake_case_fields(api: dict) -> bool:
    """В схемах контракта есть имена полей вида created_at (ASP.NET по умолчанию отдаёт camelCase)."""
    names: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                names.extend(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(api)
    return any("_" in n for n in names) and not any(re.search(r"[a-z][A-Z]", n) for n in names)


def build_openapi(d: dict) -> str:
    """openapi.yaml из design.json (JSON — подмножество YAML, отдельная библиотека не нужна)."""
    paths: dict = {}
    for ep in d.get("endpoints", []):
        method, path = str(ep.get("method", "GET")).lower(), str(ep.get("path", "/"))
        op: dict = {"summary": ep.get("summary", ""),
                    "responses": {"200": {"description": ep.get("response") or "OK"}}}
        params = re.findall(r"\{(\w+)\}", path)
        if params:
            op["parameters"] = [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                                for p in params]
        fields = [f for f in (ep.get("request") or []) if isinstance(f, dict) and f.get("name")]
        if fields and method in ("post", "put", "patch"):
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {f["name"]: _schema(f.get("type", "string")) for f in fields},
                "required": [f["name"] for f in fields if f.get("required")]}}}}
        paths.setdefault(path, {})[method] = op
    schemas = {e["name"]: {"type": "object", "properties": {
        "id": {"type": "string", "format": "uuid"},
        **{f["name"]: _schema(f.get("type", "string")) for f in e.get("fields", []) if isinstance(f, dict)}}}
        for e in d.get("entities", []) if isinstance(e, dict) and e.get("name")}
    service = d.get("service") or {}
    spec = {"openapi": "3.1.0",
            "info": {"title": service.get("name", "backend"), "description": service.get("summary", ""),
                     "version": "1.0.0"},
            "paths": paths, "components": {"schemas": schemas}}
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- сверка


def route_regex(path: str) -> re.Pattern:
    """Последний статический сегмент пути, например /api/items/{id}/close -> close."""
    static = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
    return re.compile(r'"[^"]*' + re.escape(static[-1] if static else "") + r'[^"]*"')


def coverage(d: dict, written: dict[str, str]) -> list[str]:
    """Эндпоинты, маршрут которых не найден в коде (предупреждения, не ошибки)."""
    code = "\n".join(written.values())
    missing = []
    for ep in d.get("endpoints", []):
        path = str(ep.get("path", ""))
        if path and not route_regex(path).search(code):
            missing.append(f"{ep.get('method')} {path}")
    return missing


# --------------------------------------------------------------------------- запуск


async def run(task: str, context: dict | None = None, resume: bool = False) -> dict:
    """resume=True — продолжить прерванный прогон: взять готовый design.json и уже написанные файлы."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError('нужно поле "task" с текстом задачи')
    context = context if isinstance(context, dict) else {}
    started = time.time()
    reset_usage()

    settings, unused_keys = resolve_settings(context)
    feedback = context.get("feedback") if isinstance(context.get("feedback"), dict) else None
    fix_mode = str(context.get("mode", "")).lower() == "fix" or feedback is not None
    unused_keys = [k for k in unused_keys if k not in ("mode", "feedback")]
    out = safe_path(".", settings["output_dir"])
    contract = safe_path(".", settings["contract_out"] or str(Path(settings["output_dir"]) / "openapi.yaml"))
    out.mkdir(parents=True, exist_ok=True)
    set_debug_dir(out / "debug")        # сырые ответы модели — для разбора ошибок

    report: dict = {"agent": "backend", "status": "running", "mode": "fix" if fix_mode else "generate",
                    "output_dir": settings["output_dir"],
                    "settings": redact(settings), "context_unused_keys": unused_keys, "steps": {}}

    def log(msg: str) -> None:
        print(f"[тимлид] {msg}", file=sys.stderr)

    def finish(status: str) -> dict:
        report["status"] = status
        report["usage"] = USAGE
        report["tokens"] = sum(u["tokens"] for u in USAGE.values())
        report["llm_calls"] = sum(u["calls"] for u in USAGE.values())
        report["seconds"] = round(time.time() - started, 2)
        (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"готово: {status}, {report['tokens']} токенов, {report['llm_calls']} вызовов, {report['seconds']} с")
        return report

    # Продолжение прерванного прогона: та же задача, есть design.json и progress.json
    progress_file = out / "progress.json"
    previous = _load_resume(out, task) if resume and not fix_mode else None
    if resume and previous is None and not fix_mode:
        log("продолжать нечего (нет design.json для этой задачи) — начинаю заново")
    old_task = (out / "task.txt").read_text(encoding="utf-8").strip() if (out / "task.txt").exists() else ""

    # 1. Проектировщик (в режиме исправления — готовый design.json)
    if fix_mode:
        d = _load_design(out)
        if d is None:
            report["steps"]["design"] = {"status": "failed",
                                         "error": "режим исправления: в папке вывода нет корректного design.json"}
            return finish("failed")
        if old_task and old_task != task.strip():
            report["steps"]["design"] = {"status": "reused", "warning": "task отличается от task.txt проекта"}
        else:
            report["steps"]["design"] = {"status": "reused"}
        log("режим исправления: проект из design.json, проектировщик не вызывается")
        done = []
    elif previous:
        d, done = previous
        log(f"продолжение: проект из design.json, уже готово файлов: {len(done)}")
        (out / "task.txt").write_text(task.strip() + "\n", encoding="utf-8")
        report["steps"]["design"] = {"status": "resumed", "files": [f["path"] for f in d["files"]]}
    else:
        done = []
        progress_file.unlink(missing_ok=True)
        extra = json.dumps(redact(context), ensure_ascii=False)[:CONTEXT_PROMPT_LIMIT] if context else ""
        log("проектировщик: проект бэкенда")
        try:
            res = await designer.design(task, extra, settings["max_files"], settings["max_design_attempts"])
        except RuntimeError as e:
            report["steps"]["design"] = {"status": "failed", "error": str(e)}
            return finish("failed")
        d = res["design"]
        (out / "task.txt").write_text(task.strip() + "\n", encoding="utf-8")
        (out / "design.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        if res.get("auto_fixes"):
            log(f"проектировщик: автоисправлений плана — {len(res['auto_fixes'])}")
        report["steps"]["design"] = {"status": "ok", "attempts": res["attempts"],
                                     "auto_fixes": res.get("auto_fixes", []),
                                     "entities": len(d["entities"]), "endpoints": len(d["endpoints"]),
                                     "files": [f["path"] for f in d["files"]]}

    # Контракт сразу после проекта: фронтенд и мобайл могут начинать, даже если код ещё пишется
    contract.parent.mkdir(parents=True, exist_ok=True)
    approved = context.get("contract", {}).get("openapi")
    developer.JSON_NAMING = developer.SNAKE_CASE_RULE if approved and _snake_case_fields(approved) else ""
    developer.CONTEXT = developer.context_block(task, approved)
    if approved:
        expected = {(method.upper(), path) for path, methods in approved["paths"].items()
                    for method in methods if method in ("get", "post", "put", "patch", "delete")}
        actual = {(e["method"].upper(), e["path"]) for e in d["endpoints"]}
        if actual != expected:
            raise ValueError("Internal design changed frozen API routes: " + str(actual ^ expected))
        contract.write_text(json.dumps(approved, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        contract.write_text(build_openapi(d), encoding="utf-8")
    report["contract"] = str(contract.relative_to(Path(".").resolve()))

    # 2. DevOps: инфраструктура
    infra = devops.prepare(out, settings)
    report["steps"]["infra"] = {"status": "ok", "files": infra}

    # 3. Разработчик: файлы по плану (или исправления по замечаниям)
    order = designer.topo_order(d["files"])
    specs = {f["path"]: f for f in d["files"]}
    written: dict[str, str] = {}
    problems: dict[str, list[str]] = {}
    if fix_mode:
        for path in order:
            target = safe_path(out, path)
            if target.exists():
                written[path] = target.read_text(encoding="utf-8").rstrip("\n")
        by_file, unresolved, skipped = group_feedback(feedback or {}, d, list(written))
        log(f"замечаний к исправлению: {sum(len(v) for v in by_file.values())} в {len(by_file)} файлах, "
            f"без файла: {len(unresolved)}, пропущено предупреждений: {skipped}")
        fixed: list[str] = []
        for path, msgs in by_file.items():
            log(f"исправление {path}: {len(msgs)} замечаний")
            try:
                r = await developer.fix_file(path, written[path], msgs, written)
            except RuntimeError as e:
                report["steps"]["fix"] = {"status": "interrupted", "at": path, "error": str(e), "fixed": fixed}
                return finish("interrupted")
            if r["problems"]:
                problems[path] = r["problems"]          # новый вариант не прошёл проверки — оставлен старый
            else:
                written[path] = r["code"]
                safe_path(out, path).write_text(r["code"] + "\n", encoding="utf-8")
                fixed.append(path)
        report["steps"]["fix"] = {"status": "ok" if not problems else "with_problems", "fixed": fixed,
                                  "problems": problems, "unresolved": unresolved, "skipped_warnings": skipped}
        order = []                                          # писать файлы заново не нужно
    for path in order:
        target = safe_path(out, path)
        if path in done and target.exists():
            written[path] = target.read_text(encoding="utf-8").rstrip("\n")
            log(f"разработчик: {path} — уже готов, пропускаю")
            continue
        log(f"разработчик: {path}")
        try:
            r = await developer.write_file(d, specs[path], written)
        except RuntimeError as e:
            report["steps"]["code"] = {"status": "interrupted", "at": path, "error": str(e),
                                       "done": list(written)}
            log(f"прервано на {path}: {e}")
            log("продолжить с этого места: добавьте --resume к команде запуска")
            return finish("interrupted")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(r["code"] + "\n", encoding="utf-8")
        written[path] = r["code"]
        if r["problems"]:
            problems[path] = r["problems"]
        else:
            done.append(path)
            progress_file.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
    report["steps"]["code"] = {"status": "ok" if not problems else "with_problems",
                               "files": order, "problems": problems}

    # 4. Сборка и исправления
    build_status, fix_rounds = "skipped (не установлен .NET SDK)", 0
    for round_no in range(settings["max_fix"] + 1):
        result = devops.build(out)
        if result is None:
            break
        if result["ok"]:
            build_status = "ok"
            break
        build_status = "failed"
        report["steps"]["build_errors"] = result
        if round_no == settings["max_fix"]:
            break
        fixable = {f: errs for f, errs in result["by_file"].items() if f in written}
        if not fixable:
            log("ошибки сборки не относятся к сгенерированным файлам — исправить нельзя")
            break
        fix_rounds += 1
        for path, errs in fixable.items():
            log(f"исправление {path}: {len(errs)} ошибок (раунд {fix_rounds})")
            try:
                r = await developer.fix_file(path, written[path], errs, written)
            except RuntimeError as e:
                report["steps"]["build"] = {"status": "interrupted", "error": str(e), "fix_rounds": fix_rounds}
                return finish("interrupted")
            written[path] = r["code"]
            safe_path(out, path).write_text(r["code"] + "\n", encoding="utf-8")
    report["steps"]["build"] = {"status": build_status, "fix_rounds": fix_rounds}

    # 5. Сверка и контракт
    missing = coverage(d, written)
    report["steps"]["review"] = {"missing_endpoints": missing,
                                 "health": '"/health"' in written.get("Program.cs", "")}
    report["files"] = sorted(str(p.relative_to(out)).replace("\\", "/") for p in out.rglob("*") if p.is_file())

    unresolved = (report["steps"].get("fix") or {}).get("unresolved") or []
    ok = build_status != "failed" and not problems and not unresolved and report["steps"]["review"]["health"]
    return finish("ok" if ok else "partial")


def _load_design(out: Path):
    """Готовый design.json из папки вывода (для режима исправления) или None."""
    try:
        d = json.loads((out / "design.json").read_text(encoding="utf-8"))
        if designer.validate(d, 10**6) or designer.topo_order(d["files"]) is None:
            return None
        return d
    except (OSError, ValueError, KeyError):
        return None


def _match_file(raw, planned: list[str]) -> str | None:
    """Путь из замечания -> файл проекта. Понимает /src/..., /app/output/backend/..., обратные слэши."""
    if not raw:
        return None
    raw = str(raw).replace("\\", "/").strip()
    for path in sorted(planned, key=len, reverse=True):
        if raw == path or raw.endswith("/" + path):
            return path
    base = raw.rsplit("/", 1)[-1]
    same = [p for p in planned if p.rsplit("/", 1)[-1] == base]
    return same[0] if len(same) == 1 else None


def group_feedback(feedback: dict, d: dict, planned: list[str]):
    """Замечания тестировщика -> {файл: [тексты]}, [нераспределённые], число пропущенных предупреждений.

    Замечание без файла привязывается к файлу, который упомянут в тексте, или к файлу эндпоинта,
    путь которого упомянут; иначе остаётся нераспределённым (попадает в отчёт).
    Предупреждения компилятора (category *warning*) пропускаются — они не мешают сборке.
    """
    by_file: dict[str, list[str]] = {}
    unresolved: list[dict] = []
    skipped = 0
    endpoint_files = sorted(((str(e.get("path", "")), e.get("file")) for e in d.get("endpoints", [])
                             if isinstance(e, dict) and e.get("file") in planned),
                            key=lambda x: len(x[0]), reverse=True)
    for err in feedback.get("errors") or []:
        if not isinstance(err, dict):
            err = {"message": str(err)}
        if "warning" in str(err.get("category", "")).lower():
            skipped += 1
            continue
        message = str(err.get("message", "")).strip()
        target = _match_file(err.get("file"), planned)
        if not target:
            target = next((p for p in planned if p in message or p.rsplit("/", 1)[-1] in message), None)
        if not target:
            target = next((f for path, f in endpoint_files if path and path in message), None)
        if not target:
            unresolved.append(err)
            continue
        text = message
        if err.get("line"):
            text += f" (строка {err['line']})"
        if err.get("fix_hint"):
            text += f". Подсказка: {err['fix_hint']}"
        by_file.setdefault(target, []).append(text)
    return by_file, unresolved, skipped


def _load_resume(out: Path, task: str):
    """(design, done) из прошлого прогона той же задачи, иначе None."""
    try:
        if (out / "task.txt").read_text(encoding="utf-8").strip() != task.strip():
            return None
        d = json.loads((out / "design.json").read_text(encoding="utf-8"))
        done = json.loads((out / "progress.json").read_text(encoding="utf-8")).get("done", []) \
            if (out / "progress.json").exists() else []
        if designer.validate(d, 10**6) or designer.topo_order(d["files"]) is None:
            return None
        return d, [x for x in done if isinstance(x, str)]
    except (OSError, ValueError, KeyError):
        return None


def read_input(source: str) -> dict:
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('ожидается JSON вида {"task": "...", "context": {...}}')
    return data


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    try:
        data = read_input(args[0])
        rep = asyncio.run(run(data.get("task"), data.get("context"), resume="--resume" in sys.argv))
    except (ValueError, RuntimeError, json.JSONDecodeError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({k: rep.get(k) for k in ("status", "output_dir", "contract", "tokens", "llm_calls", "seconds")},
                     indent=2, ensure_ascii=False))
    sys.exit(0 if rep["status"] == "ok" else 1)
