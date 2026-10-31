#!/usr/bin/env bash
# Передача образов MAS Pipeline на удалённый Linux-сервер по SSH и подготовка запуска там.
#
# Запуск (Git Bash, из любой папки):
#   REMOTE=team7@85.143.113.230 SSH_PORT=10007 scripts/transfer_images.sh
#
# Переменные:
#   REMOTE          user@host сервера (обязательно)
#   SSH_PORT        порт SSH (по умолчанию 22)
#   REMOTE_DIR      папка проекта на сервере (по умолчанию ~/mas-pipeline)
#   REMOTE_LLM_URL  адрес модели, как его видят контейнеры на сервере
#                   (по умолчанию http://10.10.10.10:4000/v1 — модель в сети ВМ, туннель не нужен)
#   WITH_BASE=0     не передавать базовые образы для сборки сгенерированных приложений
#                   (dotnet, node, nginx, postgis). По умолчанию передаются: без них tester на сервере
#                   без доступа к mcr.microsoft.com / Docker Hub не соберёт backend и web
#   START=1         сразу поднять стек на сервере
#
# Что делает:
#   1. проверяет Docker и Compose на сервере;
#   2. передаёт образы потоком docker save | gzip | ssh docker load (без временных файлов);
#      образ, который на сервере уже есть с тем же ID, пропускается — повторный запуск докачивает только изменённые;
#   3. кладёт на сервер docker-compose.yml без секций build (только готовые образы), ports-файл и скрипт проверки;
#   4. кладёт .env с REMOTE_LLM_URL вместо локального адреса (права 600, ключ по сети идёт только внутри SSH).
#
# Пароль SSH спрашивается на каждое соединение. Чтобы ввести его один раз, заранее скопируйте ключ:
#   ssh-copy-id -p 10007 team7@85.143.113.230
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE=${REMOTE:?Укажите REMOTE=user@host}
SSH_PORT=${SSH_PORT:-22}
REMOTE_DIR=${REMOTE_DIR:-mas-pipeline}
REMOTE_LLM_URL=${REMOTE_LLM_URL:-http://10.10.10.10:4000/v1}
PY=$(command -v python3 || command -v python)

SERVICES=(analyst planner docs orchestrator backend backend-coder web web-coder mobile tester ui)
BASE_IMAGES=(postgis/postgis:17-3.5 nginx:1.27-alpine node:22-alpine
             mcr.microsoft.com/dotnet/sdk:10.0 mcr.microsoft.com/dotnet/aspnet:10.0)

remote() { ssh -p "$SSH_PORT" "$REMOTE" "$@"; }

echo "== 1. Сервер $REMOTE:$SSH_PORT"
remote 'docker version --format "Docker {{.Server.Version}}" && docker compose version --short' \
  || { echo "На сервере нет Docker или Compose v2"; exit 1; }
remote '[ -e /dev/kvm ] && echo "KVM есть — MOBILE можно проверять на эмуляторе" || echo "KVM нет — MOBILE на эмуляторе не проверить"'

IMAGES=()
for s in "${SERVICES[@]}"; do IMAGES+=("mas-pipeline-$s"); done
docker image inspect mas-android-runner >/dev/null 2>&1 && IMAGES+=(mas-android-runner)
if [ "${WITH_BASE:-1}" = 1 ]; then
  for img in "${BASE_IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
    IMAGES+=("$img")
  done
fi

echo "== 2. Образы (${#IMAGES[@]})"
for img in "${IMAGES[@]}"; do
  local_id=$(docker image inspect -f '{{.Id}}' "$img" 2>/dev/null) \
    || { echo "  нет локально: $img — соберите: docker compose build"; exit 1; }
  remote_id=$(remote "docker image inspect -f '{{.Id}}' '$img' 2>/dev/null" || true)
  if [ "$local_id" = "$remote_id" ]; then
    echo "  уже есть  $img"
    continue
  fi
  size=$(docker image inspect -f '{{.Size}}' "$img" | awk '{printf "%.0f МБ", $1/1048576}')
  echo "  передаю   $img ($size без сжатия)"
  docker save "$img" | gzip -1 | remote 'gunzip | docker load' | sed 's/^/            /'
done

echo "== 3. Файлы запуска в ~/$REMOTE_DIR"
remote "mkdir -p '$REMOTE_DIR/scripts' '$REMOTE_DIR/tests'"
# compose без build: на сервере нет исходников, сервисы берут готовые образы mas-pipeline-<сервис>.
# --no-interpolate оставляет ${LLM_API_KEY} и т.п. как есть — ключ попадает только в .env.
docker compose config --no-interpolate --format json | "$PY" -c "
import json,sys
c=json.load(sys.stdin)
for name,s in c['services'].items():
    if s.pop('build',None) is not None:
        s['image']=c['name']+'-'+name
        s['pull_policy']='never'
json.dump(c,sys.stdout,indent=1,ensure_ascii=False)
" | remote "cat > '$REMOTE_DIR/docker-compose.yml'"
remote "cat > '$REMOTE_DIR/docker-compose.ports.yml'" < docker-compose.ports.yml
remote "cat > '$REMOTE_DIR/scripts/test_services.sh' && chmod +x '$REMOTE_DIR/scripts/test_services.sh'" < scripts/test_services.sh
remote "cat > '$REMOTE_DIR/scripts/test_analyst.py'" < scripts/test_analyst.py
remote "cat > '$REMOTE_DIR/tests/analyst_cases.json'" < tests/analyst_cases.json

if [ -f .env ]; then
  { tr -d '\r' < .env; echo; } | grep -v '^$' | sed "s#^LLM_URL=.*#LLM_URL=$REMOTE_LLM_URL#" \
    | remote "umask 077; cat > '$REMOTE_DIR/.env'"
  echo "  .env передан, LLM_URL=$REMOTE_LLM_URL"
else
  echo "  .env нет локально — создайте ~/$REMOTE_DIR/.env на сервере (LLM_URL, LLM_API_KEY, LLM_MODEL)"
fi

if [ "${START:-0}" = 1 ]; then
  echo "== 4. Запуск на сервере"
  remote "cd '$REMOTE_DIR' && docker compose up -d && docker compose ps"
fi

cat <<EOF

Готово. На сервере:
  cd ~/$REMOTE_DIR
  docker compose up -d                                                   # весь стек
  docker compose -f docker-compose.yml -f docker-compose.ports.yml up -d # с портами 8101-8110 для curl
  scripts/test_services.sh quick                                         # проверка сервисов
  python3 scripts/test_analyst.py                                        # analyst на 10 эталонных запросах
EOF
