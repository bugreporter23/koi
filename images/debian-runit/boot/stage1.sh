#!/bin/sh
set -eu
trap 'exit 100' 0
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mountfs() {
    mountpoint -q "$2" || mount -t "$1" -o "$3" "$1" "$2"
}

mountfs proc /proc nosuid,nodev,noexec
mountfs sysfs /sys nosuid,nodev,noexec
mountfs devtmpfs /dev mode=0755,nosuid
ln -s /proc/self/fd /dev/fd
ln -s /proc/self/fd/0 /dev/stdin
ln -s /proc/self/fd/1 /dev/stdout
ln -s /proc/self/fd/2 /dev/stderr
ln -s /proc/kcore /dev/core
mkdir -p /run /dev/pts /dev/shm /sys/fs/cgroup
mountfs tmpfs /run mode=0755,nosuid,nodev,size=10%
mountfs devpts /dev/pts nosuid,noexec,gid=5,mode=620,ptmxmode=666
mountfs tmpfs /dev/shm mode=1777,nosuid,nodev
mountfs cgroup2 /sys/fs/cgroup nosuid,nodev,noexec
mkdir -p /run/lock /run/user /tmp
chmod 1777 /run/lock /tmp
find /tmp -mindepth 1 -delete
/usr/local/libexec/koi-prepare-agent &
agent_pid=$!

if [ -f /var/lib/koi/initialized ] && [ -f /var/lib/koi/disk-sectors ] &&
    [ "$(cat /var/lib/koi/disk-sectors)" = "$(cat /sys/class/block/sda/size)" ]; then
    /etc/init.d/udev start &
    udev_pid=$!
    /etc/init.d/procps start
    /usr/local/libexec/koi-first-boot
    wait "$udev_pid"
else
    /etc/init.d/udev start
    /etc/init.d/procps start
    /usr/local/libexec/koi-first-boot
fi
wait "$agent_pid"
if ! /usr/bin/wtmpdb boot; then
    echo "warning: failed to write a wtmpdb boot entry"
fi
install -m000 /dev/null /run/runit.stopit
install -m000 /dev/null /run/runit.reboot
trap - 0
