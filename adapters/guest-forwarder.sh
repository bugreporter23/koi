#!/usr/local/libexec/busybox sh
set -eu
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec 2>&1
exec runuser -u dev -- socat TCP4-LISTEN:8400,bind=127.0.0.1,reuseaddr,fork VSOCK-CONNECT:2:8400
