CONTAINER_ID=$1
PORT=$2
CRASHCMD=$3

PORT=${PORT:="0"}
CRASHCMD=${CRASHCMD:=""}

# added to prevent premature return of waitUntilAvailable fn
echo -n "" > ./../container/container-$CONTAINER_ID/postgres.log

docker run -dit \
    --ulimit nofile=1048576:1048576 \
    --ulimit memlock=8388608:8388608 \
    --memory=1.5gb \
    --shm-size=500mb \
    --name lazypostgres-$CONTAINER_ID \
    --device /dev/fuse \
    --cap-add SYS_ADMIN \
    --security-opt apparmor:unconfined \
    -p $PORT:5432 \
    -v ./../container/container-$CONTAINER_ID/persisted:/tmp/lazyfs.root \
    -v ./../container/container-$CONTAINER_ID/faults.fifo:/tmp/faults.fifo \
    -v ./../container/container-$CONTAINER_ID/lazyfs.log:/tmp/lazyfs.log \
    -v ./../container/container-$CONTAINER_ID/postgres.log:/tmp/postgres.log \
    --env CRASHCMD="${CRASHCMD}" \
    --env POSTGRES_INITDB_ARGS="--auth=trust" \
    --env POSTGRES_HOST_AUTH_METHOD=trust \
    lazypostgres
