"""Итоговый отчёт прогона: задача -> ТЗ -> план -> код -> тестирование -> покрытие -> затраты.

Собирается только из фактических файлов папки прогона, без вызова модели, поэтому
пишется и для успешного прогона, и для упавшего на любом шаге.
"""

import json
from pathlib import Path

from common import attempt_reports

SKIP_DIRS = {'node_modules', 'bin', 'obj', '.angular', '.gradle', 'build', 'dist', 'debug', '__pycache__'}
SKIP_FILES = {'design.json', 'ui_design.json', 'progress.json', 'report.json', 'task.txt'}
ROLE_TITLES = {'BACKEND': 'Backend (ASP.NET Core)', 'WEB': 'Web (Angular)', 'MOBILE': 'Mobile (Android, Kotlin)'}
STATUS_TITLES = {'passed': 'passed', 'unverified': 'unverified', 'disabled': 'компонент отключён'}


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _cell(text, limit=160):
    text = ' '.join(str(text).split())
    text = text if len(text) <= limit else text[:limit - 1] + '…'
    return text.replace('|', '\\|')


def _code_stats(folder: Path):
    files = []
    for p in sorted(folder.rglob('*')):
        rel = p.relative_to(folder)
        if p.is_file() and not SKIP_DIRS.intersection(rel.parts) and rel.name not in SKIP_FILES:
            try:
                lines = len(p.read_text(encoding='utf-8').splitlines())
            except (UnicodeError, OSError):
                lines = 0
            files.append((rel.as_posix(), lines))
    return files


def _usage_lines(folder: Path):
    usage = (_load(folder / 'report.json') or {}).get('usage') or {}
    return [f'{name}: {u.get("calls", 0)} вызовов, {u.get("tokens", 0)} токенов'
            + (f', ожидание лимита {round(u["rate_limit_seconds"])} с' if u.get('rate_limit_seconds') else '')
            for name, u in usage.items()]


def build(path: Path, final: dict | None = None) -> str:
    state = {**(_load(path / 'state.json') or {}), **(final or {})}
    text = (_load(path / 'input.json') or {}).get('text', '')
    spec = _load(path / 'specification.json') or {}
    plan = _load(path / 'plan.json') or {}
    contract = plan.get('contract') or {}
    results = {r.get('role'): r for r in (_load(path / 'components.json') or [])}
    for role in (t.get('role') for t in plan.get('tasks', [])):  # run may stop before components.json
        results.setdefault(role, _load(path / f'{role.lower()}-result.json') or {})
    integration = _load(path / 'integration.json')
    coverage = _load(path / 'product' / 'coverage.json')
    manifest = _load(path / 'product' / 'manifest.json') or {}

    status = state.get('status', 'unknown')
    out = [f'# Отчёт о прогоне {state.get("run_id", path.name)}', '',
           f'**Статус:** {status}' + (f' — {state["error"]}' if state.get('error') else ''),
           f'**Время:** {state.get("seconds", "—")} с', '']

    out += ['## 1. Задача', '']
    out += ['Исходный запрос пользователя:', '', *[f'> {line}' for line in (text or '(передано готовое ТЗ)').splitlines()], '']
    if spec:
        out += [f'**Проект:** {spec.get("project_name", "—")}', '', spec.get('description', ''), '']
        stories = spec.get('user_stories') or []
        if stories:
            out += ['Пользовательские истории:', '']
            out += [f'- Как {s.get("role")}, я {s.get("action")}, чтобы {s.get("so_that")}' if isinstance(s, dict) else f'- {s}'
                    for s in stories]
            out.append('')
    else:
        out += ['ТЗ не сформировано — прогон остановился на этапе анализа требований.', '']

    out += ['## 2. План и контракт', '']
    if plan:
        out += ['| Компонент | Задача |', '|---|---|']
        out += [f'| {ROLE_TITLES.get(t["role"], t["role"])} | {_cell(t.get("task", ""), 300)} |' for t in plan.get('tasks', [])]
        out += [f'| {ROLE_TITLES.get(r, r)} | не генерируется: компонент отключён (DISABLED_ROLES) |'
                for r in plan.get('disabled', [])]
        paths = (contract.get('openapi') or {}).get('paths') or {}
        if paths:
            out += ['', 'API (замороженный контракт):', '']
            out += [f'- `{m.upper()} {p}`' for p, ops in paths.items() for m in ops
                    if m in ('get', 'post', 'put', 'patch', 'delete')]
        out.append('')
    else:
        out += ['План не построен.', '']

    out += ['## 3. Код', '']
    for role in results:
        folder = path / 'product' / role.lower()
        folder = folder if folder.is_dir() else path / role.lower()
        files = _code_stats(folder) if folder.is_dir() else []
        out += [f'### {ROLE_TITLES.get(role, role)}', '']
        if files:
            out += [f'Файлов: {len(files)}, строк: {sum(n for _, n in files)}', '']
            out += [f'- `{name}` — {n} строк' for name, n in files[:40]]
            if len(files) > 40:
                out.append(f'- … ещё {len(files) - 40}')
        else:
            out.append('Код не сохранён.')
        usage = _usage_lines(path / role.lower())
        if usage:
            out += ['', 'Вызовы модели при генерации: ' + '; '.join(usage)]
        out.append('')

    out += ['## 4. Тестирование', '']
    for role, result in results.items():
        verdict = result.get('status', 'не завершён')
        out += [f'### {ROLE_TITLES.get(role, role)}: {verdict}, попыток {result.get("attempts", "—")}', '']
        for rp in attempt_reports(path / 'qa' / role.lower()):
            rep = _load(rp) or {}
            stage = rep.get('stage') or ('tests' if rep.get('cases') else '')
            out.append(f'**{rp.stem}:** {rep.get("status")}' + (f' (стадия {stage})' if stage else ''))
            cases = rep.get('cases') or []
            if cases:
                out += ['', '| Тест | Критерий | Результат |', '|---|---|---|']
                out += [f'| {_cell(c.get("name"), 80)} | {c.get("requirement_id", "")} | '
                        + ('пройден' if c.get('passed') else 'не пройден: ' + _cell(c.get('error', ''), 200)) + ' |'
                        for c in cases]
            elif rep.get('error'):
                out += ['', '```', str(rep['error']).strip()[-1500:], '```']
            out.append('')
    out += ['### Интеграция компонентов', '']
    if integration:
        out.append(f'Результат: **{integration.get("status")}**. {integration.get("scope", "")}')
        failed = [c for c in integration.get('cases', []) if not c.get('passed')]
        out.append(f'Проверок: {len(integration.get("cases", []))}, не пройдено: {len(failed)}.')
        out += [f'- {_cell(c.get("name"), 80)}: {_cell(c.get("error", ""), 200)}' for c in failed]
        if integration.get('error'):
            out += ['```', str(integration['error'])[-1500:], '```']
    else:
        out.append('Не выполнялась.')
    out.append('')

    out += ['## 5. Покрытие критериев приёмки', '']
    criteria = coverage or [dict(c, status='не проверялся') for c in contract.get('acceptance_criteria', [])]
    if criteria:
        out += ['| ID | Критерий | Компоненты | Статус | Тестов |', '|---|---|---|---|---|']
        out += [f'| {c.get("id")} | {_cell(c.get("text"), 200)} | {", ".join(c.get("roles", []))} | '
                f'{STATUS_TITLES.get(c.get("status"), c.get("status"))} | {len(c.get("evidence", []))} |' for c in criteria]
        unverified = [c['id'] for c in criteria if c.get('status') not in ('passed', 'disabled')]
        if unverified:
            out += ['', f'Не подтверждены тестами: {", ".join(unverified)}. Их выполнение нужно проверить вручную.']
        disabled = [c['id'] for c in criteria if c.get('status') == 'disabled']
        if disabled:
            out += ['', f'Не реализуются, потому что их компонент отключён: {", ".join(disabled)}.']
    else:
        out.append('Критерии не сформированы.')
    out.append('')

    out += ['## 6. Затраты', '']
    tokens = manifest.get('tokens')
    if tokens is None:
        tokens = sum(r.get('tokens', 0) for r in results.values())
        out.append(f'Токены компонентов (прогон не завершён, без планировщика и документации): {tokens}')
    else:
        out.append(f'Токены всего: {tokens} (planner, агенты с тестировщиком, документация; analyst не сообщает расход)')
    out += [f'- {ROLE_TITLES.get(role, role)}: {r.get("tokens", 0)} токенов' for role, r in results.items()]
    out += ['', f'Время: {state.get("seconds", "—")} с', '']

    out += ['## 7. Итог', '']
    if status == 'completed':
        out.append('Все выбранные компоненты собраны, запущены и приняты тестировщиком, интеграция пройдена. '
                   'Инструкции по запуску — `README.md` и `docs/`.')
        if plan.get('disabled'):
            out.append('Не сгенерированы отключённые компоненты: '
                       + ', '.join(ROLE_TITLES.get(r, r) for r in plan['disabled'])
                       + '. Контракт API для них уже заморожен, backend готов их обслуживать.')
        docs_error = _load(path / 'docs-error.json')
        if docs_error:
            out.append(f'Документацию модель не написала ({_cell(docs_error.get("error"), 300)}); '
                       'в `docs/README.md` вместо неё — этот отчёт.')
    else:
        out.append('Продукт не собран. Причина — в строке «Статус» выше; подробности по компонентам — в разделе 4.')
    return '\n'.join(out) + '\n'


def write(path: Path, final: dict | None = None) -> Path | None:
    """Пишет REPORT.md в папку прогона (и в product/, если он есть). Ошибки отчёта не роняют прогон."""
    try:
        content = build(path, final)
    except Exception as e:  # отчёт — вспомогательный, его поломка не должна менять итог прогона
        content = f'# Отчёт не построен\n\n{e!r}\n'
    target = path / 'REPORT.md'
    target.write_text(content, encoding='utf-8')
    if (path / 'product').is_dir():
        (path / 'product' / 'REPORT.md').write_text(content, encoding='utf-8')
    return target
