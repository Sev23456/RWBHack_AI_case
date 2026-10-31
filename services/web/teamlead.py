"""Тимлид фронтенда — точка входа системы фронтенда (административный веб-интерфейс, Angular 20.1.6).

Вход (JSON), как у бэкенда:
  {"task": "текст бизнес-задачи", "context": { ...что угодно... }}
  Фронтенду нужен контракт API бэкенда: context.contract_path (по умолчанию output/contract/openapi.yaml —
  туда его кладёт система бэкенда). Известные ключи context используются, остальные — справочно для
  проектировщика; секреты (пароли, токены, ключи) в промпты и отчёт не попадают.

Режим исправления: context = {"mode": "fix", "feedback": {"errors": [{"file", "line", "message", "fix_hint"}]}}
  — проект берётся из готового ui_design.json, исправляются только файлы с замечаниями, затем сборка.

Порядок:
  1. Контракт API бэкенда -> список операций.
  2. UI-проектировщик: задача + контракт -> ui_design.json (экраны, вызовы API, план файлов).
  3. DevOps: каркас Angular, клиент API из контракта, маршруты и меню (без модели).
  4. Разработчик: компоненты экранов по одному, в порядке плана.
  5. DevOps: npm install + ng build; ошибки -> разработчик чинит файлы (до max_fix раундов).
  6. Тимлид: сверка (все экраны, вызовы API только из контракта), report.json.

Запуск:
  python teamlead.py input.json
  python teamlead.py input.json --resume     # продолжить прерванный прогон
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
    "output_dir": "output/frontend",
    "contract_path": "output/contract/openapi.yaml",
    "port": 4200,
    "backend_url": "http://backend:8080",
    "max_screens": 12,
    "max_design_attempts": 2,
    "max_fix": 3,
}

ALIASES = {
    "output_dir": ["frontend_dir", "paths.frontend", "dirs.frontend", "output_dir", "out_dir", "output"],
    "contract_path": ["contract_path", "contract_out", "openapi_path", "contract", "paths.contract", "paths.openapi"],
    "port": ["frontend_port", "ports.frontend", "ports.admin", "port"],
    "backend_url": ["backend_url", "api_url", "urls.backend"],
    "max_screens": ["max_screens", "limits.max_screens"],
    "max_design_attempts": ["max_design_attempts", "limits.max_design_attempts"],
    "max_fix": ["max_fix", "max_attempts", "limits.max_fix", "limits.max_attempts"],
    "service": ["frontend_service", "service_name"],
}
INT_KEYS = ("port", "max_screens", "max_design_attempts", "max_fix")
SECRET_RE = re.compile(r"pass(word)?|secret|token|api[_-]?key|private|credential|auth", re.I)
CONTEXT_PROMPT_LIMIT = 100000


def lookup(ctx: dict, dotted: str):
    cur = ctx
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_RE.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def resolve_settings(ctx: dict) -> tuple[dict, list[str]]:
    settings = dict(DEFAULTS)
    used_top: set[str] = set()
    for name, keys in ALIASES.items():
        for key in keys:
            value = lookup(ctx, key)
            if value not in (None, "", {}) and not isinstance(value, dict):
                settings[name] = value
                used_top.add(key.split(".")[0])
                break
    # output_dir бэкенда (output/backend) не должен стать папкой фронтенда
    if str(settings["output_dir"]).rstrip("/").endswith("backend"):
        settings["output_dir"] = DEFAULTS["output_dir"]
    for name in INT_KEYS:
        try:
            settings[name] = int(settings[name])
        except (TypeError, ValueError):
            settings[name] = DEFAULTS[name]
    unused = [k for k in ctx if k not in used_top and k not in ("mode", "feedback")]
    return settings, unused


def load_contract(path: Path) -> dict:
    """openapi (JSON или YAML). Бэкенд пишет JSON — это подмножество YAML."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # pip install pyyaml — только если контракт настоящий YAML
        except ImportError as e:
            raise ValueError("контракт не JSON, а для YAML нужен пакет pyyaml") from e
        return yaml.safe_load(text)


# --------------------------------------------------------------------------- сверка

_API_CALL = re.compile(r"\bthis\.api\.(\w+)\s*\(")


def review(d: dict, written: dict[str, str], ops: list[dict]) -> dict:
    names = {o["name"]: o for o in ops}
    by_key = {designer.api_key(f"{o['method']} {o['path']}"): o["name"] for o in ops}
    unknown, unused, missing = [], [], []
    for s in d.get("screens", []):
        code = written.get(s["file"], "")
        if not code:
            missing.append(s["file"])
            continue
        calls = set(_API_CALL.findall(code))
        unknown += [f"{s['file']}: api.{c}" for c in sorted(calls) if c not in names]
        for entry in s.get("api") or []:
            method = by_key.get(designer.api_key(entry))
            if method and method not in calls:
                unused.append(f"{s['route']}: {entry} (api.{method}) не вызывается")
    return {"missing_files": missing, "unknown_api_calls": unknown, "planned_api_not_called": unused}


# --------------------------------------------------------------------------- исправления по замечаниям


def _match_file(raw, planned: list[str]) -> str | None:
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
    """Замечания -> {файл: [тексты]}, [нераспределённые], число пропущенных предупреждений.

    Без файла — привязка по упомянутому в тексте файлу, компоненту или маршруту экрана.
    """
    by_file: dict[str, list[str]] = {}
    unresolved: list[dict] = []
    skipped = 0
    screens = d.get("screens", [])
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
            target = next((s["file"] for s in screens if s["component"] in message
                           or f"/{s['route']}" in message), None)
        if not target or target not in planned:
            unresolved.append(err)
            continue
        text = message
        if err.get("line"):
            text += f" (строка {err['line']})"
        if err.get("fix_hint"):
            text += f". Подсказка: {err['fix_hint']}"
        by_file.setdefault(target, []).append(text)
    return by_file, unresolved, skipped


# --------------------------------------------------------------------------- запуск


async def run(task: str, context: dict | None = None, resume: bool = False) -> dict:
    if not isinstance(task, str) or not task.strip():
        raise ValueError('нужно поле "task" с текстом задачи')
    context = context if isinstance(context, dict) else {}
    started = time.time()
    reset_usage()

    settings, unused_keys = resolve_settings(context)
    feedback = context.get("feedback") if isinstance(context.get("feedback"), dict) else None
    fix_mode = str(context.get("mode", "")).lower() == "fix" or feedback is not None
    out = safe_path(".", settings["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    set_debug_dir(out / "debug")

    report: dict = {"agent": "frontend", "status": "running", "mode": "fix" if fix_mode else "generate",
                    "output_dir": settings["output_dir"], "settings": redact(settings),
                    "context_unused_keys": unused_keys, "steps": {}}

    def log(msg: str) -> None:
        print(f"[тимлид фронтенда] {msg}", file=sys.stderr)

    def finish(status: str) -> dict:
        report["status"] = status
        report["usage"] = USAGE
        report["tokens"] = sum(u["tokens"] for u in USAGE.values())
        report["llm_calls"] = sum(u["calls"] for u in USAGE.values())
        report["seconds"] = round(time.time() - started, 2)
        (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"готово: {status}, {report['tokens']} токенов, {report['llm_calls']} вызовов, {report['seconds']} с")
        return report

    # 1. Контракт API бэкенда
    try:
        contract_file = safe_path(".", settings["contract_path"])
        contract = load_contract(contract_file)
        ops = devops.operations(contract)
    except (OSError, ValueError) as e:
        report["steps"]["contract"] = {"status": "failed", "error": f"контракт API не прочитан: {e}"}
        return finish("failed")
    if not ops and contract.get("paths"):
        report["steps"]["contract"] = {"status": "failed", "error": "в контракте нет эндпоинтов"}
        return finish("failed")
    report["steps"]["contract"] = {"status": "ok", "path": settings["contract_path"], "operations": len(ops)}
    log(f"контракт: {len(ops)} операций API")

    # 2. UI-проектировщик (в режиме исправления и при продолжении — готовый ui_design.json)
    progress_file = out / "progress.json"
    old_task = (out / "task.txt").read_text(encoding="utf-8").strip() if (out / "task.txt").exists() else ""
    d = _load_design(out, ops) if (fix_mode or resume) else None
    done: list[str] = []
    if fix_mode and d is None:
        report["steps"]["design"] = {"status": "failed", "error": "режим исправления: нет корректного ui_design.json"}
        return finish("failed")
    if d is not None and (fix_mode or old_task == task.strip()):
        report["steps"]["design"] = {"status": "reused"}
        if old_task and old_task != task.strip():
            report["steps"]["design"]["warning"] = "task отличается от task.txt проекта"
        if resume and not fix_mode and progress_file.exists():
            done = json.loads(progress_file.read_text(encoding="utf-8")).get("done", [])
        log(f"проект из ui_design.json ({'исправление' if fix_mode else 'продолжение'}), готово файлов: {len(done)}")
    else:
        progress_file.unlink(missing_ok=True)
        extra = json.dumps(redact(context), ensure_ascii=False)[:CONTEXT_PROMPT_LIMIT] if context else ""
        log("UI-проектировщик: экраны и навигация")
        try:
            res = await designer.design(task, ops, extra, settings["max_screens"], settings["max_design_attempts"])
        except RuntimeError as e:
            report["steps"]["design"] = {"status": "failed", "error": str(e)}
            return finish("failed")
        d = res["design"]
        (out / "ui_design.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        report["steps"]["design"] = {"status": "ok", "attempts": res["attempts"], "auto_fixes": res["auto_fixes"],
                                     "screens": [s["route"] for s in d["screens"]]}
    (out / "task.txt").write_text(task.strip() + "\n", encoding="utf-8")

    # 3. DevOps: каркас, клиент API, маршруты и меню
    title = (d.get("app") or {}).get("title", "")
    infra = devops.prepare(out, settings, title)
    api_files, api_ref = devops.api_client(out, contract)
    shell_files = devops.shell(out, d)
    report["steps"]["infra"] = {"status": "ok", "files": infra + api_files + shell_files}

    # 4. Разработчик
    plan = designer.plan(d)
    components = {f["path"]: (f["screen"]["component"] if f.get("screen") else None) for f in plan}
    written: dict[str, str] = {}
    problems: dict[str, list[str]] = {}
    for f in plan:
        target = safe_path(out, f["path"])
        if target.exists() and (fix_mode or f["path"] in done):
            existing = target.read_text(encoding="utf-8").rstrip("\n")
            if fix_mode or not developer.check_file(f["path"], existing, components[f["path"]]):
                written[f["path"]] = existing
                continue
    if fix_mode:
        by_file, unresolved, skipped = group_feedback(feedback or {}, d, list(written))
        log(f"замечаний: {sum(len(v) for v in by_file.values())} в {len(by_file)} файлах, "
            f"без файла: {len(unresolved)}, пропущено предупреждений: {skipped}")
        fixed = []
        for path, msgs in by_file.items():
            log(f"исправление {path}: {len(msgs)} замечаний")
            try:
                r = await developer.fix_file(path, written[path], msgs, written, api_ref, components.get(path))
            except RuntimeError as e:
                report["steps"]["fix"] = {"status": "interrupted", "at": path, "error": str(e), "fixed": fixed}
                return finish("interrupted")
            if r["problems"]:
                problems[path] = r["problems"]
            else:
                written[path] = r["code"]
                safe_path(out, path).write_text(r["code"] + "\n", encoding="utf-8")
                fixed.append(path)
        report["steps"]["fix"] = {"status": "ok" if not problems else "with_problems", "fixed": fixed,
                                  "problems": problems, "unresolved": unresolved, "skipped_warnings": skipped}
    else:
        for f in plan:
            path = f["path"]
            if path in written:
                log(f"разработчик: {path} — уже готов, пропускаю")
                continue
            log(f"разработчик: {path}")
            try:
                r = await developer.write_file(d, f, written, api_ref, task=task)
            except RuntimeError as e:
                report["steps"]["code"] = {"status": "interrupted", "at": path, "error": str(e), "done": done}
                log(f"прервано на {path}: {e}. Продолжить: добавьте --resume")
                return finish("interrupted")
            target = safe_path(out, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(r["code"] + "\n", encoding="utf-8")
            written[path] = r["code"]
            if r["problems"]:
                problems[path] = r["problems"]
            else:
                done.append(path)
                progress_file.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
        report["steps"]["code"] = {"status": "ok" if not problems else "with_problems",
                                   "files": [f["path"] for f in plan], "problems": problems}

    # 5. Сборка и исправления
    build_status, fix_rounds = "skipped (не установлен Node.js)", 0
    specs = {f["path"]: f for f in plan}
    previous_errors: dict[str, set] = {}
    rewritten: list[str] = []
    for round_no in range(settings["max_fix"] + 1):
        log("сборка: npm install + ng build" if round_no == 0 else f"повторная сборка (раунд {round_no})")
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
        fixable = {}
        for f, errs in result["by_file"].items():
            path = _match_file(f, list(written))
            if path:
                fixable.setdefault(path, []).extend(errs)
        if not fixable:
            log("ошибки сборки не относятся к сгенерированным экранам — исправить нельзя")
            break
        fix_rounds += 1
        for path, errs in fixable.items():
            key = {_LINE_NO.sub("", e).strip() for e in errs}  # без номеров строк: они сдвигаются
            stuck = previous_errors.get(path) == key            # те же ошибки, что в прошлом раунде
            previous_errors[path] = key
            errs = with_source(errs, written[path])             # к ошибке — сама строка кода
            try:
                if stuck and path in specs:
                    log(f"{path}: исправление не помогло — пишу файл заново (раунд {fix_rounds})")
                    r = await developer.write_file(d, specs[path], written, api_ref, avoid=errs, task=task)
                    rewritten.append(path)
                else:
                    log(f"исправление {path}: {len(errs)} ошибок (раунд {fix_rounds})")
                    r = await developer.fix_file(path, written[path], errs, written, api_ref,
                                                 components.get(path))
            except RuntimeError as e:
                report["steps"]["build"] = {"status": "interrupted", "error": str(e), "fix_rounds": fix_rounds}
                return finish("interrupted")
            if not r["problems"]:
                written[path] = r["code"]
                safe_path(out, path).write_text(r["code"] + "\n", encoding="utf-8")
    report["steps"]["build"] = {"status": build_status, "fix_rounds": fix_rounds, "rewritten": rewritten}

    # 6. Сверка
    rv = review(d, written, ops)
    report["steps"]["review"] = rv
    report["files"] = sorted(str(p.relative_to(out)).replace("\\", "/") for p in out.rglob("*")
                             if p.is_file() and "node_modules" not in p.parts and ".angular" not in p.parts
                             and "dist" not in p.parts)
    unresolved = (report["steps"].get("fix") or {}).get("unresolved") or []
    ok = build_status != "failed" and not problems and not unresolved and not rv["missing_files"] \
        and not rv["unknown_api_calls"]
    return finish("ok" if ok else "partial")


_LINE_NO = re.compile(r"^строка (\d+):")


def with_source(errors: list[str], code: str) -> list[str]:
    """Добавляет к ошибке текст строки кода — модели проще найти место (номер строки сам по себе мало помогает)."""
    lines = code.splitlines()
    result = []
    for e in errors:
        m = _LINE_NO.match(e)
        if m and 0 < int(m.group(1)) <= len(lines):
            e = f"{e}  | код: {lines[int(m.group(1)) - 1].strip()[:200]}"
        result.append(e)
    return result


def _load_design(out: Path, ops: list[dict]):
    try:
        d = json.loads((out / "ui_design.json").read_text(encoding="utf-8"))
        return None if designer.validate(d, ops, 10**6) else d
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
    print(json.dumps({k: rep.get(k) for k in ("status", "output_dir", "tokens", "llm_calls", "seconds")},
                     indent=2, ensure_ascii=False))
    sys.exit(0 if rep["status"] == "ok" else 1)
