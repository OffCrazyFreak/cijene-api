#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
name="price-tests-$$"
image="$name:local"
postgres="postgres:17-alpine"
existing_images=$(docker image ls -aq --no-trunc)
build_log=$(mktemp)
cleanup() {
    docker rm -f "$name-client" "$name-db" >/dev/null 2>&1 || true
    docker network rm "$name" >/dev/null 2>&1 || true
    image_ids=$(docker image history --no-trunc -q "$image" 2>/dev/null || true)
    for short_id in $(sed -n 's/^ ---> \([0-9a-f]\{12\}\)$/\1/p' "$build_log" | tac); do
        image_ids+=$'\n'$(docker image inspect --format '{{.Id}}' "$short_id" 2>/dev/null || true)
    done
    while read -r id; do
        [[ "$id" == sha256:* ]] || continue
        if ! printf '%s\n' "$existing_images" | grep -qx "$id"; then
            docker image rm "$id" >/dev/null 2>&1 || true
        fi
    done < <(printf '%s\n' "$image_ids" | awk '!seen[$0]++')
    rm -f "$build_log"
}
trap cleanup EXIT
# The database image must already exist; this script never pulls it implicitly.
docker image inspect "$postgres" >/dev/null
DOCKER_BUILDKIT=0 docker build --rm --force-rm --target development-api \
    --label "test-run=$name" -t "$image" . 2>&1 | tee "$build_log"
docker network create --internal --label "test-run=$name" "$name" >/dev/null
docker run -d --name "$name-db" --network "$name" --network-alias db \
    --label "test-run=$name" --cpus 1 --memory 512m \
    --tmpfs /var/lib/postgresql/data:rw,size=256m \
    -e POSTGRES_HOST_AUTH_METHOD=trust "$postgres" >/dev/null
ready=false
for attempt in {1..120}; do
    if docker logs "$name-db" 2>&1 | grep -q 'PostgreSQL init process complete' && \
        docker exec "$name-db" pg_isready -U postgres >/dev/null; then
        ready=true
        break
    fi
    sleep 0.25
done
if [[ "$ready" != true ]]; then
    docker logs "$name-db"
    exit 1
fi
docker run --rm --name "$name-client" --network "$name" \
    --label "test-run=$name" --cpus 1 --memory 768m \
    --tmpfs /tmp:rw,size=128m \
    -v "$PWD:/src:ro" -w /src "$image" /app/.venv/bin/python -m pytest \
    -p no:cacheprovider --service-test-dsn=postgresql://postgres@db/postgres "$@"
