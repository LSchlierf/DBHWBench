CONTAINER_ID=$1
PORT=$2
CRASHCMD=$3

PORT=${PORT:="0"}
CRASHCMD=${CRASHCMD:=""}

docker run -dit \
    --ulimit nofile=1048576:1048576 \
    --ulimit memlock=8388608:8388608 \
    --memory=1.5gb \
    --shm-size=500mb \
    --name lazyumbra-assert-$CONTAINER_ID \
    --device /dev/fuse \
    --cap-add SYS_ADMIN \
    --security-opt apparmor:unconfined \
    -p $PORT:5432 \
    -v ./../container/container-$CONTAINER_ID/persisted:/tmp/lazyfs.root \
    -v ./../container/container-$CONTAINER_ID/faults.fifo:/tmp/faults.fifo \
    -v ./../container/container-$CONTAINER_ID/lazyfs.log:/tmp/lazyfs.log \
    -v ./../container/container-$CONTAINER_ID/umbra-assert.log:/tmp/umbra.log \
    --env CRASHCMD="${CRASHCMD}" \
    lazyumbra-assert
