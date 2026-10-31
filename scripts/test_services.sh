#!/usr/bin/env bash
# Проверка сервисов MAS Pipeline через curl — каждый сервис отдельно, затем оркестратор.
#
# Стек должен быть запущен с опубликованными портами:
#   docker compose -f docker-compose.yml -f docker-compose.ports.yml up -d
#
# Запуск из папки проекта на той же машине, где работает стек (Git Bash или Linux):
#   scripts/test_services.sh quick   # /health всех + рабочий запрос к analyst, planner, docs, кодерам, tester (~5 мин)
#   scripts/test_services.sh full    # quick + полный цикл агентов backend/web + прогон через оркестратор (до часа)
#
# Переменные: HOST (по умолчанию localhost), RUN_TIMEOUT — сколько ждать прогон оркестратора, с (3600).
# Нужны curl, python3 (или python) и docker compose. Модель (LLM) должна быть доступна.
set -u
cd "$(dirname "$0")/.."

MODE=${1:-quick}
HOST=${HOST:-localhost}
RUN_TIMEOUT=${RUN_TIMEOUT:-3600}
PY=$(command -v python3 || command -v python)
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.ports.yml)
declare -A PORT=([analyst]=8101 [planner]=8102 [docs]=8103 [backend-coder]=8104 [web-coder]=8105
                 [backend]=8106 [web]=8107 [mobile]=8108 [tester]=8109 [orchestrator]=8110)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PASS=0; FAILED=()

url() { echo "http://$HOST:${PORT[$1]}$2"; }
uuid() { "$PY" -c 'import uuid;print(uuid.uuid4())'; }

# request NAME METHOD URL [ФАЙЛ_ТЕЛА] [ТАЙМАУТ]  ->  ответ в $TMP/NAME.json, код в CODE
request() {
  local args=(-s -m "${5:-300}" -o "$TMP/$1.json" -w '%{http_code}' -X "$2" "$3")
  [ -n "${4:-}" ] && args+=(-H 'Content-Type: application/json' --data-binary "@$4")
  CODE=$(curl "${args[@]}") || CODE=000
}

# check NAME ОЖИДАЕМЫЙ_КОД [УСЛОВИЕ_PYTHON]   (r — ответ, разобранный из JSON)
check() {
  local ok=1
  if [ "$CODE" != "$2" ]; then ok=0
  elif [ -n "${3:-}" ]; then
    "$PY" - "$TMP/$1.json" "$3" <<'PY' || ok=0
import json, sys
r = json.load(open(sys.argv[1], encoding='utf-8'))
sys.exit(0 if eval(sys.argv[2]) else 1)
PY
  fi
  if [ $ok = 1 ]; then PASS=$((PASS+1)); echo "  OK    $1"
  else FAILED+=("$1"); echo "  FAIL  $1 — HTTP $CODE: $(head -c 400 "$TMP/$1.json" 2>/dev/null)"; fi
}

body() { cat > "$TMP/$1.req.json"; echo "$TMP/$1.req.json"; }

echo "== Health"
for s in analyst planner docs backend-coder web-coder backend web mobile tester orchestrator; do
  request "health-$s" GET "$(url "$s" /health)" "" 10
  check "health-$s" 200 "r.get('status')=='ok'"
done
request ui GET "http://$HOST:${UI_PORT:-8088}/" "" 10; check ui 200
request ui-proxy GET "http://$HOST:${UI_PORT:-8088}/health" "" 10; check ui-proxy 200 "r.get('status')=='ok'"

echo "== analyst: бизнес-требования -> ТЗ"
request analyze POST "$(url analyst /analyze)" "$(body analyze <<'EOF'
{"text":"Нужен простой сервис заметок: REST API и веб-интерфейс, мобильное приложение не нужно. Заметка: заголовок (обязательный, до 100 символов), текст, дата создания. На главной странице заголовок «Мои заметки», список заметок, форма с полями «Заголовок» и «Текст» и кнопкой «Добавить», кнопка удаления у каждой заметки. Данные в PostgreSQL."}
EOF
)" 400
check analyze 200 "r['success'] and r['data']['user_stories']"

echo "== planner: ТЗ -> план и контракт"
# ТЗ из ответа analyst; если его нет — запасное, чтобы planner проверялся независимо
"$PY" - "$TMP/analyze.json" "$TMP/plan.req.json" <<'PY'
import json, sys
try:
    spec = json.load(open(sys.argv[1], encoding='utf-8'))['data']
except Exception:
    spec = None
spec = spec or {'project_name': 'notes', 'description': 'Сервис заметок: REST API и веб-интерфейс',
                'user_stories': [{'role': 'пользователь', 'action': 'создаёт и просматривает заметки', 'so_that': 'хранить мысли'}],
                'acceptance_criteria': ['GET /api/notes возвращает список заметок', "Страница показывает заголовок 'Мои заметки'"]}
json.dump({'spec': spec}, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)
PY
request plan POST "$(url planner /plan)" "$TMP/plan.req.json" 600
check plan 200 "{t['role'] for t in r['plan']['tasks']} and r['plan']['contract']['acceptance_criteria']"
request plan-invalid POST "$(url planner /plan)" "$(body plan-invalid <<<'{}')" 30
check plan-invalid 422

echo "== docs: документация"
request docs POST "$(url docs /document)" "$(body docs <<'EOF'
{"spec":{"project_name":"Заметки","description":"Сервис заметок"},"contract":{"openapi":{"paths":{"/api/notes":{"get":{},"post":{}}}}},"files":["backend/Program.cs","web/src/app/pages/notes.ts","docker-compose.yml"],"integration":{"status":"passed"},"coverage":[{"id":"AC-1","text":"Список заметок","status":"passed"}]}
EOF
)" 600
check docs 200 "r['docs'] and all(isinstance(v,str) and v for v in r['docs'].values())"

echo "== backend-coder: исправление C#-файла"
request backend-coder POST "$(url backend-coder /call)" "$(body backend-coder <<'EOF'
{"fn":"fix_file","args":["Models/Note.cs","namespace Backend;\npublic class Note\n{\n    public Guid Id { get; set; }\n    public string Title { get; set; }\n}\n",["Models/Note.cs(5,19): error CS8618: Non-nullable property 'Title' must contain a non-null value when exiting constructor"],{}],"kwargs":{},"usage":{},"debug_dir":null}
EOF
)" 900
check backend-coder 200 "'class Note' in r['result']['code'] and r['usage']['developer_fix']['calls']==1"
request backend-coder-unknown POST "$(url backend-coder /call)" "$(body backend-coder-unknown <<<'{"fn":"nope"}')" 30
check backend-coder-unknown 400

echo "== web-coder: исправление Angular-файла"
request web-coder POST "$(url web-coder /call)" "$(body web-coder <<'EOF'
{"fn":"fix_file","args":["src/app/pages/hello.ts","import { Component } from '@angular/core';\nimport { MatCardModule } from '@angular/material';\n\n@Component({ selector: 'app-hello', imports: [MatCardModule], template: `<section class=\"page\"><h2>Привет</h2></section>` })\nexport class HelloPage {}\n",["TS2305: Module '\"@angular/material\"' has no exported member 'MatCardModule'."],{},""],"kwargs":{"component":"HelloPage"},"usage":{},"debug_dir":null}
EOF
)" 900
check web-coder 200 "'@angular/material/card' in r['result']['code']"

echo "== tester: сборка, запуск и браузерный тест готового приложения"
TID=$(uuid)
"${COMPOSE[@]}" exec -T tester sh -c "d=/app/output/$TID/web; mkdir -p \$d \
  && printf 'FROM nginx:1.27-alpine\nCOPY index.html /usr/share/nginx/html/index.html\n' > \$d/Dockerfile \
  && printf '<html><body><h1>Hello MAS</h1></body></html>' > \$d/index.html"
request tester POST "$(url tester /validate)" "$(body tester <<EOF
{"run_id":"$TID","role":"WEB","source":"/app/output/$TID/web","attempt":1,"requirements":{"text":"Страница показывает Hello MAS"},"contract":{"openapi":{"openapi":"3.0.3","paths":{}},"acceptance_criteria":[{"id":"AC-1","text":"Страница показывает Hello MAS","roles":["WEB"]}]}}
EOF
)" 900
check tester 200 "r['status']=='passed' and r['zip_path']"

if [ "$MODE" = full ]; then
  CONTRACT='{"openapi":{"openapi":"3.0.3","info":{"title":"Notes","version":"1"},"paths":{"/health":{"get":{"responses":{"200":{"description":"ok"}}}},"/api/notes":{"get":{"responses":{"200":{"description":"list","content":{"application/json":{"schema":{"type":"array","items":{"$ref":"#/components/schemas/Note"}}}}}}},"post":{"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/CreateNote"}}}},"responses":{"201":{"description":"created","content":{"application/json":{"schema":{"$ref":"#/components/schemas/Note"}}}},"400":{"description":"invalid"}}}}},"components":{"schemas":{"Note":{"type":"object","properties":{"id":{"type":"string"},"title":{"type":"string"},"text":{"type":"string"}},"required":["id","title"]},"CreateNote":{"type":"object","properties":{"title":{"type":"string","maxLength":100},"text":{"type":"string"}},"required":["title"]}}}},"acceptance_criteria":[{"id":"AC-1","text":"POST /api/notes создаёт заметку и возвращает 201","roles":["BACKEND"]},{"id":"AC-2","text":"GET /api/notes возвращает список заметок","roles":["BACKEND"]},{"id":"AC-3","text":"На странице виден заголовок «Мои заметки»","roles":["WEB"]}]}'

  echo "== backend: полный цикл агента (кодер + сборка + tester)"
  request backend-work POST "$(url backend /work)" "$(body backend-work <<EOF
{"run_id":"$(uuid)","task":"Сервис заметок на ASP.NET Core 10: список и создание заметок, хранение в PostgreSQL.","context":{"acceptance_criteria":[{"id":"AC-1","text":"POST /api/notes создаёт заметку и возвращает 201","roles":["BACKEND"]},{"id":"AC-2","text":"GET /api/notes возвращает список заметок","roles":["BACKEND"]}]},"contract":$CONTRACT}
EOF
)" 3600
  check backend-work 200 "r['status']=='passed'"

  echo "== web: полный цикл агента (кодер + сборка + tester)"
  request web-work POST "$(url web /work)" "$(body web-work <<EOF
{"run_id":"$(uuid)","task":"Веб-страница на Angular 20 с заголовком «Мои заметки» и списком заметок из GET /api/notes.","context":{"acceptance_criteria":[{"id":"AC-3","text":"На странице виден заголовок «Мои заметки»","roles":["WEB"]}]},"contract":$CONTRACT}
EOF
)" 3600
  check web-work 200 "r['status']=='passed'"

  echo "== orchestrator: сквозной прогон"
  request run POST "$(url orchestrator /runs)" "$TMP/analyze.req.json" 30
  check run 202 "r['run_id']"
  RID=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8')).get('run_id',''))" "$TMP/run.json" 2>/dev/null)
  if [ -n "$RID" ]; then
    echo "  run_id $RID — жду завершения (до $RUN_TIMEOUT с)"
    deadline=$((SECONDS+RUN_TIMEOUT)); status=queued
    while [ $SECONDS -lt $deadline ]; do
      request run-state GET "$(url orchestrator "/runs/$RID")" "" 10
      status=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8')).get('status'))" "$TMP/run-state.json" 2>/dev/null)
      case $status in completed|error|interrupted) break;; esac
      sleep 20
    done
    echo "  статус: $status"
    check run-state 200 "r['status']=='completed'"
    request download GET "$(url orchestrator "/runs/$RID/download")" "" 60; CODE_ZIP=$CODE
    [ "$CODE_ZIP" = 200 ] && { PASS=$((PASS+1)); echo "  OK    download (project.zip)"; } \
                         || { FAILED+=(download); echo "  FAIL  download — HTTP $CODE_ZIP"; }
  fi
fi

echo
echo "Итог: прошло $PASS, упало ${#FAILED[@]}${FAILED[*]:+ — ${FAILED[*]}}"
[ ${#FAILED[@]} = 0 ]
