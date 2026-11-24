#!/bin/bash
# Run with elevated privileges. All temporary binary files stay in private tmpfs.
set -eu
export LC_ALL=C PATH=/usr/sbin:/usr/bin:/sbin:/bin
for tool in blktrace blkparse strace; do
    command -v "$tool" >/dev/null || { echo "缺少 $tool。Ubuntu 可安装 blktrace 和 strace。" >&2; exit 1; }
done
dir=$(mktemp -d /dev/shm/cleandist-blk.XXXXXXXX)
trap 'rm -rf -- "$dir"' EXIT
cd "$dir"

# Inspect only the collector's ioctls. A successful START confirms that the
# kernel is recording. Relay files already exist during SETUP, before START.
strace -D -e trace=ioctl -o startup blktrace -a issue -d "$1" -o io -w "$2" >&2 &
pid=$!
ready=0
while kill -0 "$pid" 2>/dev/null; do
    if (( ! ready )) && grep -Eq 'BLKTRACESTART\).* = 0$' startup 2>/dev/null; then
        echo READY
        ready=1
    fi
    if read -r -t 0.1; then
        kill -INT "$pid" 2>/dev/null || true
        break
    fi
    if (( $(du -sk . | cut -f1) >= 131072 )); then
        echo CAPTURE_SIZE_LIMIT >&2
        kill -INT "$pid" 2>/dev/null || true
        break
    fi
done
wait "$pid"
blkparse -i io -f 'ROW %T.%9t %p %D %a %d %S %n\n'
