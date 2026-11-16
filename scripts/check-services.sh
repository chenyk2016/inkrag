#!/bin/sh
# 在本机或 CI 启动专用 PostgreSQL 和 OpenSearch，避免在业务库运行集成回归。
set -eu
cd "$(dirname "$0")/.."

prefix="inkrag-ci-$$"
pg="$prefix-pg"
search="$prefix-search"
cleanup() {
    docker rm -f -v "$pg" "$search" >/dev/null 2>&1 || :
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

docker run --rm -d --name "$pg" -p 127.0.0.1::5432 \
    -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine >/dev/null
docker run --rm -d --name "$search" -p 127.0.0.1::9200 \
    -e discovery.type=single-node -e DISABLE_INSTALL_DEMO_CONFIG=true \
    -e DISABLE_SECURITY_PLUGIN=true -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
    opensearchproject/opensearch:3.3.2 >/dev/null

pg_address=$(docker port "$pg" 5432)
search_address=$(docker port "$search" 9200)
tries=0
until docker exec "$pg" pg_isready -h 127.0.0.1 -U postgres -d postgres >/dev/null 2>&1 &&
    curl -fsS --noproxy '*' --max-time 2 "http://$search_address/_cluster/health" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 90 ]; then
        printf '%s\n' '集成服务在规定时间内未就绪' >&2
        exit 1
    fi
    sleep 2
done
docker exec -i "$pg" psql -U postgres -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE ROLE inkrag_app LOGIN;
CREATE DATABASE inkrag_integration;
SQL

export INKRAG_TEST_DATABASE_DSN="postgresql+psycopg://inkrag_app@$pg_address/inkrag_integration"
export INKRAG_TEST_MIGRATION_DSN="postgresql+psycopg://postgres@$pg_address/inkrag_integration"
export INKRAG_SEARCH_INTEGRATION=1
export INKRAG_OPENSEARCH_TEST_URL="http://$search_address"
uv run --locked --all-packages --all-extras python scripts/check-platform-pg.py "$@"
