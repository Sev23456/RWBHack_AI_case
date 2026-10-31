"""DevOps фронтенда: всё, что не требует модели.

- prepare()      — каркас Angular 20.1.6 + Angular Material: package.json, angular.json, tsconfig, main.ts,
                   app.config.ts, Dockerfile (Node -> nginx), nginx.conf, compose.frontend.yml и т.д.
- api_client()   — клиент API из openapi-контракта бэкенда: src/app/api/models.ts, src/app/api/api.service.ts
- shell()        — оболочка приложения и маршруты по ui_design.json: src/app/app.ts, src/app/app.routes.ts
- build()        — npm install + ng build (если есть Node.js), ошибки сгруппированы по файлам
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------- версии (стек хакатона)

ANGULAR = "20.1.6"          # Frontend — Angular 20.1.6 (фиксировано)
ANGULAR_LINE = "~20.1.0"     # пакеты той же линейки 20.1.x (Material, CDK, CLI, build)
TYPESCRIPT = "~5.8.2"        # Angular 20.1: TypeScript >=5.8.0 <5.9.0
NODE_IMAGE = "node:22-alpine"  # Angular 20: Node ^20.19 || ^22.12 || ^24

PACKAGE_JSON = {
    "name": "frontend",
    "version": "0.0.0",
    "private": True,
    "scripts": {"start": "ng serve", "build": "ng build"},
    "dependencies": {
        "@angular/animations": ANGULAR,
        "@angular/cdk": ANGULAR_LINE,
        "@angular/common": ANGULAR,
        "@angular/compiler": ANGULAR,
        "@angular/core": ANGULAR,
        "@angular/forms": ANGULAR,
        "@angular/material": ANGULAR_LINE,
        "@angular/platform-browser": ANGULAR,
        "@angular/router": ANGULAR,
        "rxjs": "~7.8.0",
        "tslib": "^2.3.0",
        "zone.js": "~0.15.0",
    },
    "devDependencies": {
        "@angular/build": ANGULAR_LINE,
        "@angular/cli": ANGULAR_LINE,
        "@angular/compiler-cli": ANGULAR,
        "typescript": TYPESCRIPT,
    },
}

# Разрешённые импорты в коде компонентов (всё остальное — пакеты, которых нет в package.json)
ALLOWED_IMPORT_PREFIXES = ("@angular/", "rxjs", "./", "../")

ANGULAR_JSON = {
    "$schema": "./node_modules/@angular/cli/lib/config/schema.json",
    "version": 1,
    "newProjectRoot": "projects",
    "cli": {"analytics": False},
    "projects": {
        "frontend": {
            "projectType": "application",
            "root": "",
            "sourceRoot": "src",
            "prefix": "app",
            "architect": {
                "build": {
                    "builder": "@angular/build:application",
                    "options": {
                        "browser": "src/main.ts",
                        # app.config uses provideZoneChangeDetection: without zone.js bootstrap fails (NG0908)
                        "polyfills": ["zone.js"],
                        "tsConfig": "tsconfig.app.json",
                        "assets": [],
                        "styles": ["@angular/material/prebuilt-themes/azure-blue.css", "src/styles.css"],
                    },
                    "configurations": {
                        "production": {
                            "budgets": [{"type": "initial", "maximumWarning": "2MB", "maximumError": "5MB"}],
                            "outputHashing": "all",
                        },
                        "development": {"optimization": False, "extractLicenses": False, "sourceMap": True},
                    },
                    "defaultConfiguration": "production",
                },
                "serve": {
                    "builder": "@angular/build:dev-server",
                    "options": {"proxyConfig": "proxy.conf.json"},
                    "configurations": {
                        "production": {"buildTarget": "frontend:build:production"},
                        "development": {"buildTarget": "frontend:build:development"},
                    },
                    "defaultConfiguration": "development",
                },
            },
        }
    },
}

TSCONFIG = {
    "compileOnSave": False,
    "compilerOptions": {
        "strict": True,
        "noImplicitOverride": True,
        "noImplicitReturns": True,
        "noFallthroughCasesInSwitch": True,
        "skipLibCheck": True,
        "isolatedModules": True,
        "importHelpers": True,
        "target": "ES2022",
        "module": "preserve",
    },
    "angularCompilerOptions": {
        "enableI18nLegacyMessageIdFormat": False,
        "strictInjectionParameters": True,
        "strictInputAccessModifiers": True,
        "strictTemplates": True,
    },
}

TSCONFIG_APP = {
    "extends": "./tsconfig.json",
    "compilerOptions": {"outDir": "./out-tsc/app", "types": []},
    "include": ["src/**/*.ts"],
}

INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <base href="/">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body class="mat-typography">
  <app-root></app-root>
</body>
</html>
"""

STYLES_CSS = """html, body { height: 100%; }
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.page { padding: 16px; display: block; }
.page h2 { margin: 0 0 16px; }
.toolbar-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
table { width: 100%; }
"""

MAIN_TS = """import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { App } from './app/app';

bootstrapApplication(App, appConfig).catch((err) => console.error(err));
"""

APP_CONFIG_TS = """import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async';
import { provideNativeDateAdapter } from '@angular/material/core';
import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes, withComponentInputBinding()),
    provideHttpClient(),
    provideAnimationsAsync(),
    provideNativeDateAdapter(),
  ],
};
"""

PROXY_CONF = {"/api": {"target": "http://localhost:8080", "secure": False}}

DOCKERFILE = f"""FROM {NODE_IMAGE} AS build
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npx ng build --configuration production

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html
EXPOSE 80
"""

# /api проксируется на бэкенд. Адрес в переменной + resolver Docker: nginx стартует, даже если бэкенд ещё не поднят
NGINX_CONF = """server {{
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    resolver 127.0.0.11 valid=10s ipv6=off;
    set $backend {backend_url};

    location /api/ {{
        proxy_pass $backend;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
"""

DOCKERIGNORE = """node_modules/
dist/
.angular/
out-tsc/
debug/
ui_design.json
report.json
progress.json
task.txt
compose.frontend.yml
"""

# build: ../frontend — путь верный и при запуске из этой папки, и вместе с compose бэкенда (output/backend)
COMPOSE = """# Фронтенд (Angular + nginx). Вместе с бэкендом (из папки output/backend):
#   docker compose -f compose.backend.yml -f ../frontend/compose.frontend.yml up --build
# Админка: http://localhost:{port}
services:
  {service}:
    build: ../frontend
    ports:
      - "{port}:80"
"""

DEFAULTS = {"service": "frontend", "port": 4200, "backend_url": "http://backend:8080", "title": "Администрирование"}


def _write(out: Path, rel: str, content: str) -> str:
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return rel


def prepare(out: Path, settings: dict, title: str = "") -> list[str]:
    """Каркас проекта. settings — из context (недостающее — по умолчанию)."""
    s = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS and v not in (None, "")}}
    s["title"] = title or s["title"]
    files = [
        _write(out, "package.json", json.dumps(PACKAGE_JSON, indent=2) + "\n"),
        _write(out, "angular.json", json.dumps(ANGULAR_JSON, indent=2) + "\n"),
        _write(out, "tsconfig.json", json.dumps(TSCONFIG, indent=2) + "\n"),
        _write(out, "tsconfig.app.json", json.dumps(TSCONFIG_APP, indent=2) + "\n"),
        _write(out, "proxy.conf.json", json.dumps(PROXY_CONF, indent=2) + "\n"),
        _write(out, "src/index.html", INDEX_HTML.format(title=_html(s["title"]))),
        _write(out, "src/styles.css", STYLES_CSS),
        _write(out, "src/main.ts", MAIN_TS),
        _write(out, "src/app/app.config.ts", APP_CONFIG_TS),
        _write(out, "Dockerfile", DOCKERFILE),
        _write(out, "nginx.conf", NGINX_CONF.format(backend_url=s["backend_url"])),
        _write(out, ".dockerignore", DOCKERIGNORE),
        _write(out, "compose.frontend.yml", COMPOSE.format(**s)),
    ]
    return files


def _html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- клиент API из контракта


def _pascal(text: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", str(text))
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return ("N" + out) if out[:1].isdigit() else (out or "Item")


def _ident(text: str) -> str:
    p = _pascal(text)
    return p[:1].lower() + p[1:]


def _ts_type(schema: dict | None) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return _pascal(str(schema["$ref"]).rsplit("/", 1)[-1])
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"]) or "string"
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "array":
        return f"{_ts_type(schema.get('items'))}[]"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "string":
        return "string"
    if t == "object" and isinstance(schema.get("properties"), dict):
        return "{ " + "; ".join(f"{_prop(k)}?: {_ts_type(v)}" for k, v in schema["properties"].items()) + " }"
    return "any"


def _prop(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else json.dumps(name)


def operations(contract: dict) -> list[dict]:
    """Операции контракта: [{"name", "method", "path", "params", "query", "body", "summary"}]."""
    ops, used = [], set()
    for path, item in (contract.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            segs = [s for s in path.strip("/").split("/") if s and s.lower() != "api"]
            words = ["By" + _pascal(s[1:-1].split(":")[0]) if s.startswith("{") else _pascal(s) for s in segs]
            name = op.get("operationId") and _ident(op["operationId"]) or method.lower() + "".join(words or ["Root"])
            base, n = name, 2
            while name in used:
                name, n = f"{base}{n}", n + 1
            used.add(name)
            params = [s[1:-1].split(":")[0] for s in path.strip("/").split("/") if s.startswith("{")]
            query = [p["name"] for p in op.get("parameters") or []
                     if isinstance(p, dict) and p.get("in") == "query" and p.get("name")]
            body = None
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            if isinstance(schema, dict):
                body = schema
            ops.append({"name": name, "method": method.upper(), "path": path, "params": params,
                        "query": query, "body": body, "summary": op.get("summary", "")})
    return ops


def api_client(out: Path, contract: dict) -> tuple[list[str], str]:
    """Генерирует models.ts и api.service.ts. Возвращает (файлы, справку по методам для промптов)."""
    schemas = (contract.get("components") or {}).get("schemas") or {}
    model_lines = ["// Сгенерировано из контракта API бэкенда (openapi). Не редактировать вручную.", ""]
    for name, schema in schemas.items():
        props = (schema or {}).get("properties") or {}
        required = set((schema or {}).get("required") or [])
        model_lines.append(f"export interface {_pascal(name)} {{")
        for prop, ps in props.items():
            opt = "" if prop in required else "?"
            model_lines.append(f"  {_prop(prop)}{opt}: {_ts_type(ps)};")
        model_lines.append("}")
        model_lines.append("")

    ops = operations(contract)
    svc = [
        "// Сгенерировано из контракта API бэкенда (openapi). Не редактировать вручную.",
        "import { Injectable, inject } from '@angular/core';",
        "import { HttpClient, HttpParams } from '@angular/common/http';",
        "import { Observable } from 'rxjs';",
        "",
    ]
    for op in ops:
        if op["body"]:
            iface = _pascal(op["name"]) + "Request"
            props = op["body"].get("properties") or {}
            required = set(op["body"].get("required") or [])
            svc.append(f"export interface {iface} {{")
            for prop, ps in props.items():
                svc.append(f"  {_prop(prop)}{'' if prop in required else '?'}: {_ts_type(ps)};")
            svc.append("}")
            svc.append("")
    svc += [
        "/** Все вызовы бэкенда. Адреса относительные: /api проксирует nginx (в разработке — proxy.conf.json). */",
        "@Injectable({ providedIn: 'root' })",
        "export class ApiService {",
        "  private readonly http = inject(HttpClient);",
        "",
        "  private toParams(query?: Record<string, unknown>): HttpParams {",
        "    let params = new HttpParams();",
        "    for (const [key, value] of Object.entries(query ?? {})) {",
        "      if (value !== undefined && value !== null && value !== '') {",
        "        params = params.set(key, String(value));",
        "      }",
        "    }",
        "    return params;",
        "  }",
    ]
    reference = []
    for op in ops:
        args = [f"{_ident(p)}: string" for p in op["params"]]
        if op["body"]:
            args.append(f"body: {_pascal(op['name'])}Request")
        if op["query"]:
            args.append("query?: { " + "; ".join(f"{_prop(q)}?: string | number | boolean" for q in op["query"]) + " }")
        url = re.sub(r"\{(\w+)(?::[^}]*)?\}", lambda m: "${encodeURIComponent(" + _ident(m.group(1)) + ")}", op["path"])
        m = op["method"].lower()
        opts = ", { params: this.toParams(query) }" if op["query"] else ""
        if m in ("post", "put", "patch"):
            call = f"this.http.{m}<any>(`{url}`, {'body' if op['body'] else '{}'}{opts})"
        else:
            call = f"this.http.{m}<any>(`{url}`{opts})"
        svc += ["", f"  /** {op['method']} {op['path']}{(' — ' + op['summary']) if op['summary'] else ''} */",
                f"  {op['name']}({', '.join(args)}): Observable<any> {{",
                f"    return {call};",
                "  }"]
        reference.append(f"- api.{op['name']}({', '.join(args)}): Observable<any>  — {op['method']} {op['path']}"
                         + (f" — {op['summary']}" if op["summary"] else ""))
    svc.append("}")

    files = [_write(out, "src/app/api/models.ts", "\n".join(model_lines) + "\n"),
             _write(out, "src/app/api/api.service.ts", "\n".join(svc) + "\n")]
    body_ifaces = [f"- {_pascal(op['name'])}Request: {_ts_type(op['body'])}" for op in ops if op["body"]]
    ref = ("### ApiService (import { ApiService } from '<путь>/api/api.service')\n" + "\n".join(reference)
           + ("\n\n### Типы тел запросов (из того же файла)\n" + "\n".join(body_ifaces) if body_ifaces else "")
           + "\n\n### Модели (import { ... } from '<путь>/api/models')\n"
           + "\n".join(f"- {_pascal(n)}: {_ts_type({'type': 'object', 'properties': (s or {}).get('properties') or {}})}"
                       for n, s in schemas.items()))
    return files, ref


# --------------------------------------------------------------------------- оболочка и маршруты


def _rel_import(from_dir: str, target: str) -> str:
    rel = os.path.relpath(target[:-3] if target.endswith(".ts") else target, from_dir).replace("\\", "/")
    return rel if rel.startswith(".") else "./" + rel


def shell(out: Path, design: dict) -> list[str]:
    """app.routes.ts и app.ts (панель навигации) по экранам из ui_design.json."""
    screens = [s for s in design.get("screens", []) if isinstance(s, dict)]
    imports, routes = [], []
    seen = set()
    for s in screens:
        comp, file = s["component"], s["file"]
        if comp not in seen:
            imports.append(f"import {{ {comp} }} from '{_rel_import('src/app', file)}';")
            seen.add(comp)
        routes.append(f"  {{ path: {json.dumps(s['route'])}, component: {comp}, title: {json.dumps(s.get('title', ''), ensure_ascii=False)} }},")
    menu = [s for s in screens if s.get("in_menu", True) and ":" not in s["route"]]
    first = (menu or screens)[0]["route"] if screens else ""
    routes_ts = "\n".join([
        "// Сгенерировано по ui_design.json. Не редактировать вручную.",
        "import { Routes } from '@angular/router';",
        *imports,
        "",
        "export const routes: Routes = [",
        f"  {{ path: '', redirectTo: {json.dumps(first)}, pathMatch: 'full' }},",
        *routes,
        f"  {{ path: '**', redirectTo: {json.dumps(first)} }},",
        "];",
        "",
    ])
    title = _html((design.get("app") or {}).get("title") or DEFAULTS["title"]).replace("'", "\\'")
    links = "\n".join(
        f'          <a mat-list-item routerLink="/{s["route"]}" routerLinkActive="active-link">'
        f'{_html(s.get("title") or s["route"])}</a>' for s in menu)
    app_ts = f"""// Сгенерировано по ui_design.json. Не редактировать вручную.
import {{ Component }} from '@angular/core';
import {{ RouterLink, RouterLinkActive, RouterOutlet }} from '@angular/router';
import {{ MatToolbarModule }} from '@angular/material/toolbar';
import {{ MatSidenavModule }} from '@angular/material/sidenav';
import {{ MatListModule }} from '@angular/material/list';

@Component({{
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, MatToolbarModule, MatSidenavModule, MatListModule],
  template: `
    <mat-toolbar color="primary">{title}</mat-toolbar>
    <mat-sidenav-container class="layout">
      <mat-sidenav mode="side" opened class="nav">
        <mat-nav-list>
{links}
        </mat-nav-list>
      </mat-sidenav>
      <mat-sidenav-content>
        <router-outlet />
      </mat-sidenav-content>
    </mat-sidenav-container>
  `,
  styles: [`
    .layout {{ height: calc(100vh - 64px); }}
    .nav {{ width: 240px; }}
    .active-link {{ font-weight: 600; }}
  `],
}})
export class App {{}}
"""
    return [_write(out, "src/app/app.routes.ts", routes_ts), _write(out, "src/app/app.ts", app_ts)]


# --------------------------------------------------------------------------- сборка

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOC = re.compile(r"^\s*(?P<file>[^\s:]+\.(?:ts|html|css|scss)):(?P<line>\d+):(?P<col>\d+)")
_TSC = re.compile(r"^(?P<file>\S+\.ts):(?P<line>\d+):(?P<col>\d+) - error (?P<code>\w+): (?P<msg>.*)$")


def build(out: Path, timeout: int = 1200) -> dict | None:
    """npm install (если нужно) + ng build. None — Node.js не установлен.

    Иначе {"ok": bool, "by_file": {путь: [ошибки]}, "general": [ошибки без файла]}.
    """
    if not shutil.which("npm"):
        return None
    env = {**os.environ, "NG_CLI_ANALYTICS": "false", "CI": "true", "NG_FORCE_TTY": "false"}
    if not (out / "node_modules" / "@angular" / "core").exists():
        proc = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=out, env=env,
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            tail = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip()][-15:]
            return {"ok": False, "by_file": {}, "general": ["npm install не удался:"] + tail}
    proc = subprocess.run(["npx", "ng", "build", "--configuration", "development"], cwd=out, env=env,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode == 0:
        return {"ok": True, "by_file": {}, "general": []}

    lines = [_ANSI.sub("", l) for l in (proc.stdout + proc.stderr).splitlines()]
    by_file: dict[str, list[str]] = {}
    general: list[str] = []
    root = out.resolve()

    def add(file: str, line: str, msg: str) -> None:
        f = file.replace("\\", "/")
        if os.path.isabs(f):
            try:
                f = str(Path(f).resolve().relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
        text = f"строка {line}: {msg}"
        if text not in by_file.setdefault(f, []):
            by_file[f].append(text)

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _TSC.match(line)
        if m:
            add(m["file"], m["line"], f"{m['code']} {m['msg']}")
        elif "[ERROR]" in line:
            msg = re.sub(r"^.*?\[ERROR\]\s*", "", line)
            msg = re.sub(r"\s*\[plugin [^\]]+\]\s*$", "", msg)
            loc = next((_LOC.match(lines[j]) for j in range(i + 1, min(i + 6, len(lines))) if _LOC.match(lines[j])),
                       None)
            if loc:
                add(loc["file"], loc["line"], msg)
            elif msg not in general:
                general.append(msg)
        i += 1
    if not by_file and not general:
        general = [l for l in lines if l.strip()][-15:]
    return {"ok": False, "by_file": {k: v[:20] for k, v in by_file.items()}, "general": general[:20]}
