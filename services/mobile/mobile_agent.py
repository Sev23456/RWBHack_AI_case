"""Агент-разработчик Android (MOBILE).

Получает Task {"task", "context"} (в context уже лежит общий контракт) и возвращает
{"files": {путь: содержимое}, "tokens": int} — Gradle-проект на Kotlin + Jetpack Compose.

Как работает:
  1. design: Qwen проектирует структуру — пакет, разрешения, версии сборки, зависимости,
     Kotlin-файлы с публичными API. Версии — из ТЗ, а чего в ТЗ нет, выбирает Qwen;
     в коде МАС версий нет. Версия Kotlin из ТЗ проверяется.
  2. files: каждый файл генерируется отдельным запросом (до CONCURRENCY одновременно);
     в каждый запрос уходит вся структура, чтобы файлы сходились по именам.
  3. Сборочные файлы (Gradle, манифест, ресурсы, README) — из шаблонов, а не от LLM:
     так сборка предсказуема, Qwen задаёт только значения версий и список зависимостей.

Плохая генерация (невалидный JSON, чужой package, несбалансированные скобки, обрыв по лимиту)
повторяется до MAX_ATTEMPTS раз с причиной отказа, потом GenerationError — дальше решает человек.
"""

import asyncio
import json
import os
import re
from html import escape

from llm import ask_llm
from orchestrator import MAX_ATTEMPTS, GenerationError, _extract_json, _strip_think

MAX_FILES = int(os.getenv("MOBILE_MAX_FILES", "14"))
CONCURRENCY = int(os.getenv("MOBILE_CONCURRENCY", "2"))  # общий лимит ключа делят все агенты (см. llm.py)
BASE_URL = os.getenv("MOBILE_BASE_URL", "http://10.0.2.2:8080/")  # хост-машина из эмулятора
CONFIGURATIONS = {"implementation", "debugImplementation", "testImplementation", "androidTestImplementation"}
# Без них шаблон сборки не соберётся: Compose-активность и JUnit для unit-тестов
REQUIRED_DEPENDENCIES = {"androidx.activity:activity-compose", "junit:junit"}

LIBRARIES = (
    "Jetpack Compose Material 3 (+ material-icons-extended), Navigation Compose, Lifecycle ViewModel Compose, "
    "androidx.core (NotificationCompat), Kotlin Coroutines (+ kotlinx-coroutines-play-services), "
    "Retrofit + OkHttp + kotlinx.serialization (конвертер retrofit2.converter.kotlinx.serialization.asConverterFactory), "
    "Google Play Services Location (FusedLocationProviderClient); "
    "для unit-тестов — JUnit 4 и kotlinx-coroutines-test. Других библиотек нет: Room, Hilt, Koin, Gson недоступны."
)

DESIGN_SYSTEM = f"""Ты — ведущий Android-разработчик. По задаче и контексту спроектируй нативное Android-приложение на Kotlin + Jetpack Compose.
Доступные библиотеки: {LIBRARIES}

Правила:
1. Архитектура MVVM: data (API-клиент строго по контракту из контекста, модели, репозитории), бизнес-логика, ui (экраны Compose, ViewModel), MainActivity.
2. Файл MainActivity.kt обязателен и лежит в корне пакета: app/src/main/java/<пакет>/MainActivity.kt, класс MainActivity : ComponentActivity.
3. Не больше {MAX_FILES} Kotlin-файлов, из них хотя бы один unit-тест в app/src/test/java/<пакет>/... на бизнес-логику без Android-зависимостей.
4. Адрес бэкенда — BuildConfig.BASE_URL. Внешние сервисы — только через интерфейс-адаптер с mock-реализацией.
5. Gradle-файлы, AndroidManifest и ресурсы генерируются по шаблону: от тебя нужны только разрешения ("permissions"), версии ("versions") и зависимости ("dependencies").
6. Версии: если версия указана в контексте (ТЗ) — используй именно её. Остальные выбери стабильными и совместимыми между собой и с версией Kotlin: Android Gradle Plugin, Gradle, compileSdk/minSdk, библиотеки. Плагины Compose Compiler и kotlinx.serialization берут версию Kotlin.
7. dependencies — все библиотеки, которые используются в коде, включая {", ".join(sorted(REQUIRED_DEPENDENCIES))}. Координата "группа:артефакт:версия"; для BOM — "platform": true, тогда артефакты из него указывай без версии.
8. "purpose" каждого файла — точные публичные объявления, которые он содержит (классы, интерфейсы, функции с сигнатурами), и от каких файлов проекта он зависит. По этому описанию файлы пишутся независимо друг от друга, поэтому имена должны совпадать.

Ответь только JSON без пояснений:
{{"app_name": "...", "package": "ru.example.app", "permissions": ["android.permission.INTERNET"],
 "versions": {{"kotlin": "<версия>", "android_gradle_plugin": "<версия>", "gradle": "<версия>", "compile_sdk": <число>, "min_sdk": <число>}},
 "dependencies": [{{"configuration": "implementation", "coordinate": "группа:артефакт:<версия>", "platform": false}}],
 "files": [{{"path": "app/src/main/java/ru/example/app/MainActivity.kt", "purpose": "..."}}]}}"""

FILE_SYSTEM = f"""Ты — Android-разработчик. Напиши ровно один файл Kotlin-проекта (Kotlin + Jetpack Compose, MVVM) по его описанию в структуре проекта.
Доступные библиотеки: {LIBRARIES}
Правила:
- Первая строка кода — package, соответствующий пути файла.
- Объявления других файлов проекта импортируй по их пакетам из структуры; используй только те имена, что в ней описаны.
- Соблюдай контракт API из контекста без изменений. Адрес бэкенда — BuildConfig.BASE_URL.
- Код полный и компилируемый, без заглушек TODO() и без пояснений вне кода.
Ответь только содержимым файла в одном блоке ```kotlin."""

PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PERMISSION_RE = re.compile(r"^android\.permission\.[A-Z_]+$")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}(-[A-Za-z0-9.]+)?$")
COORDINATE_RE = re.compile(r"^([\w.-]+:[\w.-]+)(:[\w.+-]+)?$")


def _kotlin_version(context: str) -> str | None:
    """Версия Kotlin, указанная в ТЗ, или None."""
    m = re.search(r"Kotlin\D{0,3}(\d+\.\d+(?:\.\d+)?)", context, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _validate_versions(v, context: str) -> dict:
    if not isinstance(v, dict):
        raise ValueError("versions — объект {kotlin, android_gradle_plugin, gradle, compile_sdk, min_sdk}")
    for key in ("kotlin", "android_gradle_plugin", "gradle"):
        if not isinstance(v.get(key), str) or not VERSION_RE.match(v[key]):
            raise ValueError(f"versions.{key} = {v.get(key)!r} — нужна конкретная версия вида X.Y.Z")
    required = _kotlin_version(context)
    if required and v["kotlin"] != required and not v["kotlin"].startswith(required + "."):
        raise ValueError(f"versions.kotlin = {v['kotlin']}, а в ТЗ указан Kotlin {required}")
    for key in ("compile_sdk", "min_sdk"):
        if not isinstance(v.get(key), int) or isinstance(v[key], bool) or v[key] < 21:
            raise ValueError(f"versions.{key} — целый уровень API не ниже 21")
    if v["min_sdk"] > v["compile_sdk"]:
        raise ValueError("versions.min_sdk больше compile_sdk")
    return {k: v[k] for k in ("kotlin", "android_gradle_plugin", "gradle", "compile_sdk", "min_sdk")}


def _validate_dependencies(deps) -> list[dict]:
    if not isinstance(deps, list) or not deps:
        raise ValueError("dependencies — непустой список {configuration, coordinate, platform}")
    result, has_platform, artifacts = [], False, set()
    for d in deps:
        if not isinstance(d, dict) or d.get("configuration") not in CONFIGURATIONS:
            raise ValueError(f"dependency {d!r}: configuration — одна из {sorted(CONFIGURATIONS)}")
        m = COORDINATE_RE.match(d.get("coordinate") or "") if isinstance(d.get("coordinate"), str) else None
        if not m:
            raise ValueError(f"dependency {d.get('coordinate')!r}: координата группа:артефакт[:версия]")
        platform = d.get("platform") is True
        if platform and not m.group(2):
            raise ValueError(f"BOM {d['coordinate']} без версии")
        has_platform |= platform
        artifacts.add(m.group(1))
        result.append({"configuration": d["configuration"], "coordinate": d["coordinate"], "platform": platform})
    if not has_platform:
        missing = [d["coordinate"] for d in result if not COORDINATE_RE.match(d["coordinate"]).group(2)]
        if missing:
            raise ValueError(f"без версии и без BOM: {missing}")
    missing = REQUIRED_DEPENDENCIES - artifacts
    if missing:
        raise ValueError(f"в dependencies нет {sorted(missing)}")
    return result


def _validate_design(text: str, context: str) -> dict:
    d = _extract_json(text, dict)
    pkg = d.get("package")
    if not isinstance(pkg, str) or not PACKAGE_RE.match(pkg):
        raise ValueError(f"package {pkg!r} — нужен вида ru.example.app (строчные латинские, через точку)")
    if not isinstance(d.get("app_name"), str) or not d["app_name"].strip():
        raise ValueError("нет app_name")

    perms = d.get("permissions", [])
    if not isinstance(perms, list) or not all(isinstance(p, str) and PERMISSION_RE.match(p) for p in perms):
        raise ValueError("permissions — список вида android.permission.ИМЯ")
    perms = sorted(set(perms) | {"android.permission.INTERNET"})

    files = d.get("files")
    if not isinstance(files, list) or not 2 <= len(files) <= MAX_FILES:
        raise ValueError(f"files — список от 2 до {MAX_FILES} файлов")
    pkg_dir = pkg.replace(".", "/")
    path_re = re.compile(rf"^app/src/(main|test)/java/{re.escape(pkg_dir)}/([a-z0-9_]+/)*[A-Z]\w*\.kt$")
    seen = set()
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("purpose"), str):
            raise ValueError("элемент files — объект {path, purpose}")
        if not path_re.match(f["path"]):
            raise ValueError(f"путь {f['path']!r} должен быть app/src/main|test/java/{pkg_dir}/.../Имя.kt")
        if f["path"] in seen:
            raise ValueError(f"файл {f['path']} повторяется")
        seen.add(f["path"])
    if f"app/src/main/java/{pkg_dir}/MainActivity.kt" not in seen:
        raise ValueError(f"нет app/src/main/java/{pkg_dir}/MainActivity.kt")
    if not any(p.startswith("app/src/test/") for p in seen):
        raise ValueError("нет ни одного unit-теста в app/src/test/java/...")

    return {"app_name": d["app_name"].strip(), "package": pkg, "permissions": perms,
            "versions": _validate_versions(d.get("versions"), context),
            "dependencies": _validate_dependencies(d.get("dependencies")),
            "files": [{"path": f["path"], "purpose": f["purpose"]} for f in files]}


def _strip_literals(code: str) -> str:
    """Убирает строки, символы и комментарии — чтобы считать скобки только в коде."""
    return re.sub(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])+\'|//[^\n]*|/\*[\s\S]*?\*/', "", code)


def _validate_kotlin(text: str, path: str) -> str:
    text = _strip_think(text)
    blocks = re.findall(r"```(?:kotlin|kt)?[^\n]*\n(.*?)```", text, flags=re.DOTALL)
    code = (max(blocks, key=len) if blocks else text).strip()
    if not code:
        raise ValueError("пустой файл")

    expected = path.split("/java/", 1)[1].rsplit("/", 1)[0].replace("/", ".")
    first = next((l.strip() for l in _strip_literals(code).splitlines() if l.strip() and not l.strip().startswith("@file:")), "")
    if first != f"package {expected}":
        raise ValueError(f"первая строка должна быть 'package {expected}', а не {first[:80]!r}")

    bare = _strip_literals(code)
    for open_, close in ("{}", "()", "[]"):
        if bare.count(open_) != bare.count(close):
            raise ValueError(f"несбалансированные скобки {open_}{close} — файл оборван или с ошибкой")
    if path.endswith("/MainActivity.kt") and "class MainActivity" not in code:
        raise ValueError("в MainActivity.kt нет class MainActivity")
    return code + "\n"


async def _ask_until_valid(step: str, prompt: str, system: str, think: bool, validate) -> tuple:
    """Запрос к Qwen до MAX_ATTEMPTS раз, пока validate не примет ответ. Возвращает (значение, токены)."""
    tokens, feedback = 0, ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        p = prompt + (f"\n\nПредыдущий ответ отклонён: {feedback}. Исправь." if feedback else "")
        r = await ask_llm(p, system=system, think=think)
        tokens += r["tokens"]
        try:
            if r.get("truncated"):
                raise ValueError("ответ обрезан по лимиту токенов — сделай короче")
            return validate(r["text"]), tokens
        except ValueError as e:
            feedback = str(e)
            print(f"[MOBILE {step}] попытка {attempt} отклонена: {feedback}", flush=True)
    raise GenerationError(f"MOBILE {step}", feedback, MAX_ATTEMPTS, "MOBILE")


def _render(template: str, **values) -> str:
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    return template


SETTINGS = """\
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "@NAME@"
include(":app")
"""

ROOT_BUILD = """\
plugins {
    id("com.android.application") version "@AGP@" apply false
    id("org.jetbrains.kotlin.android") version "@KOTLIN@" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "@KOTLIN@" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "@KOTLIN@" apply false
}
"""

APP_BUILD = """\
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "@PKG@"
    compileSdk = @SDK@

    defaultConfig {
        applicationId = "@PKG@"
        minSdk = @MIN_SDK@
        targetSdk = @SDK@
        versionCode = 1
        versionName = "0.1.0"
        buildConfigField("String", "BASE_URL", "\\"@BASE_URL@\\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
@DEPENDENCIES@
}
"""

MANIFEST = """\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

@PERMISSIONS@

    <application
        android:allowBackup="true"
        android:label="@string/app_name"
        android:usesCleartextTraffic="true"
        android:theme="@android:style/Theme.Material.Light.NoActionBar">
        <activity
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

README = """\
# @APP_NAME@ — Android

Нативное Android-приложение: Kotlin @KOTLIN@, Jetpack Compose, MVVM. Пакет `@PKG@`.

## Запуск

Нужны JDK 17 и Android SDK (API @SDK@) — проще всего открыть папку в Android Studio.

1. Сгенерировать Gradle Wrapper (бинарного `gradle-wrapper.jar` в архиве нет):
   ```
   gradle wrapper --gradle-version @GRADLE@
   ```
2. Собрать APK: `./gradlew assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`
3. Unit-тесты: `./gradlew testDebugUnitTest`
4. Установить на эмулятор или устройство: `adb install -r app/build/outputs/apk/debug/app-debug.apk`

Адрес бэкенда — `BASE_URL` в `app/build.gradle.kts` (по умолчанию `@BASE_URL@`: так эмулятор видит localhost хост-машины).

## Структура

@STRUCTURE@
"""


def _render_dependencies(deps: list[dict]) -> str:
    return "\n".join(
        f'    {d["configuration"]}(platform("{d["coordinate"]}"))' if d["platform"]
        else f'    {d["configuration"]}("{d["coordinate"]}")'
        for d in deps
    )


def _scaffold(design: dict) -> dict[str, str]:
    """Сборочные файлы проекта из шаблонов; версии и зависимости — из design."""
    pkg = design["package"]
    app_name = design["app_name"]
    v = design["versions"]
    common = dict(PKG=pkg, KOTLIN=v["kotlin"], AGP=v["android_gradle_plugin"], GRADLE=v["gradle"],
                  SDK=v["compile_sdk"], MIN_SDK=v["min_sdk"], BASE_URL=BASE_URL)
    android_string = escape(app_name, quote=False).replace("'", "\\'").replace('"', '\\"')
    return {
        "settings.gradle.kts": _render(SETTINGS, NAME=pkg.rsplit(".", 1)[-1]),
        "build.gradle.kts": _render(ROOT_BUILD, **common),
        "gradle.properties": "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\n"
                             "android.useAndroidX=true\nkotlin.code.style=official\n",
        "gradle/wrapper/gradle-wrapper.properties":
            f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{v['gradle']}-bin.zip\n",
        "app/build.gradle.kts": _render(APP_BUILD, DEPENDENCIES=_render_dependencies(design["dependencies"]), **common),
        "app/src/main/AndroidManifest.xml": _render(MANIFEST, PERMISSIONS="\n".join(
            f'    <uses-permission android:name="{p}" />' for p in design["permissions"])),
        "app/src/main/res/values/strings.xml":
            f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            f'    <string name="app_name">{android_string}</string>\n</resources>\n',
        ".gitignore": ".gradle/\nbuild/\nlocal.properties\n.idea/\n*.iml\n",
        "README.md": _render(README, APP_NAME=app_name, STRUCTURE="\n".join(
            f"- `{f['path']}` — {f['purpose']}" for f in design["files"]), **common),
    }


async def mobile_agent(task: dict) -> dict:
    """Task {"task", "context"} -> {"files": {путь: содержимое}, "tokens": int}."""
    brief = f"Задача:\n{task['task']}\n\nКонтекст:\n{task['context']}"
    # Без рассуждений: иначе Qwen продумывает в них весь проект и упирается в лимит токенов
    design, tokens = await _ask_until_valid("design", brief, DESIGN_SYSTEM, False,
                                            lambda text: _validate_design(text, task["context"]))

    structure = json.dumps(design, ensure_ascii=False, indent=2)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def write_file(f: dict) -> tuple:
        prompt = f"{brief}\n\nСтруктура проекта:\n{structure}\n\nНапиши файл {f['path']}\nНазначение: {f['purpose']}"
        async with sem:
            return await _ask_until_valid(f["path"], prompt, FILE_SYSTEM, False,
                                          lambda text: _validate_kotlin(text, f["path"]))

    written = await asyncio.gather(*(write_file(f) for f in design["files"]))
    files = _scaffold(design)
    for f, (code, t) in zip(design["files"], written):
        files[f["path"]] = code
        tokens += t
    return {"files": files, "tokens": tokens}
