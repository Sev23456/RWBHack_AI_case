"""DevOps: всё, что нужно для сборки и запуска бэкенда. Без модели — только шаблоны.

- prepare()  — Backend.csproj, Dockerfile, .dockerignore, compose.backend.yml, .env.example
- build()    — dotnet build (если установлен .NET SDK), ошибки сгруппированы по файлам
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">

  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <LangVersion>14</LangVersion>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <RootNamespace>Backend</RootNamespace>
    <AssemblyName>Backend</AssemblyName>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="Microsoft.AspNetCore.OpenApi" Version="10.0.*" />
    <PackageReference Include="Npgsql.EntityFrameworkCore.PostgreSQL" Version="10.0.*" />
    <PackageReference Include="Npgsql.EntityFrameworkCore.PostgreSQL.NetTopologySuite" Version="10.0.*" />
    <PackageReference Include="NetTopologySuite.IO.GeoJSON4STJ" Version="4.*" />
  </ItemGroup>

</Project>
"""

DOCKERFILE = """FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY Backend.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish Backend.csproj -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:{port}
EXPOSE {port}
ENTRYPOINT ["dotnet", "Backend.dll"]
"""

DOCKERIGNORE = """bin/
obj/
debug/
*.json
!appsettings*.json
task.txt
openapi.yaml
compose.backend.yml
.env*
"""

COMPOSE = """# Бэкенд + PostgreSQL/PostGIS. Запуск из этой папки: docker compose -f compose.backend.yml up --build
services:
  {service}:
    build: {build_dir}
    ports:
      - "{port}:{port}"
    environment:
      ConnectionStrings__Default: "Host=db;Port=5432;Database=${{DB_NAME:-{db_name}}};Username=${{DB_USER:-{db_user}}};Password=${{DB_PASSWORD:-{db_password}}}"
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgis/postgis:17-3.5
    environment:
      POSTGRES_DB: ${{DB_NAME:-{db_name}}}
      POSTGRES_USER: ${{DB_USER:-{db_user}}}
      POSTGRES_PASSWORD: ${{DB_PASSWORD:-{db_password}}}
    ports:
      - "{db_port}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${{DB_USER:-{db_user}}}"]
      interval: 5s
      retries: 20
"""

ENV_EXAMPLE = """# Скопируйте в .env и задайте свои значения
DB_NAME={db_name}
DB_USER={db_user}
DB_PASSWORD=
"""

DEFAULTS = {"service": "backend", "build_dir": ".", "port": 8080, "db_port": 5432,
            "db_name": "app", "db_user": "app", "db_password": "app"}


def prepare(out: Path, settings: dict) -> list[str]:
    """Пишет инфраструктурные файлы. settings — порты и имена из context (недостающее — по умолчанию)."""
    s = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS and v not in (None, "")}}
    files = {
        "Backend.csproj": CSPROJ,
        "Dockerfile": DOCKERFILE.format(**s),
        ".dockerignore": DOCKERIGNORE,
        "compose.backend.yml": COMPOSE.format(**s),
        ".env.example": ENV_EXAMPLE.format(**s),
    }
    for name, content in files.items():
        (out / name).write_text(content, encoding="utf-8")
    return list(files)


_ERR = re.compile(r"^(?P<file>[^(\s][^(]*\.cs)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>\w+): (?P<msg>.*?)(?: \[.*\])?$")


def build(out: Path, timeout: int = 900) -> dict | None:
    """Собирает проект. None — .NET SDK не установлен.

    Иначе {"ok": bool, "by_file": {путь: [ошибки]}, "general": [ошибки не из .cs-файлов]}.
    """
    if not shutil.which("dotnet"):
        return None
    proc = subprocess.run(["dotnet", "build", "Backend.csproj", "-nologo", "-v", "q", "-clp:NoSummary"],
                          cwd=out, capture_output=True, text=True, timeout=timeout)
    if proc.returncode == 0:
        return {"ok": True, "by_file": {}, "general": []}

    by_file: dict[str, list[str]] = {}
    general: list[str] = []
    root = out.resolve()
    for raw in (proc.stdout + proc.stderr).splitlines():
        line = raw.strip()
        if ": error " not in line:
            continue
        m = _ERR.match(line)
        if m:
            f = Path(m.group("file"))
            try:
                rel = str(f.resolve().relative_to(root)) if f.is_absolute() else str(f)
            except ValueError:
                rel = str(f)
            msg = f"строка {m.group('line')}: {m.group('code')} {m.group('msg')}"
            if msg not in by_file.setdefault(rel.replace("\\", "/"), []):
                by_file[rel.replace("\\", "/")].append(msg)
        elif line not in general:
            general.append(line)
    if not by_file and not general:
        general = [l.strip() for l in (proc.stdout + proc.stderr).splitlines()[-15:] if l.strip()]
    return {"ok": False, "by_file": {k: v[:20] for k, v in by_file.items()}, "general": general[:20]}
