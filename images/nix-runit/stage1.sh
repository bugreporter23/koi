#!/usr/local/libexec/busybox sh
set -eu
trap 'exit 100' 0
bb=/usr/local/libexec/busybox
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mountfs() {
    $bb mountpoint -q "$2" || $bb mount -t "$1" -o "$3" "$1" "$2"
}
trace() {
    [ -f /etc/koi/profile-boot ] || [ -f /run/koi-trace ] || return 0
    /usr/local/libexec/koi-ready --stamp "$1"
}
/usr/local/libexec/koi-prefetch /etc/koi/startup-files &
mountfs proc /proc nosuid,nodev,noexec
mountfs sysfs /sys nosuid,nodev,noexec
mountfs devtmpfs /dev mode=0755,nosuid
$bb ln -sf /proc/self/fd /dev/fd
$bb ln -sf /proc/self/fd/0 /dev/stdin
$bb ln -sf /proc/self/fd/1 /dev/stdout
$bb ln -sf /proc/self/fd/2 /dev/stderr
$bb mkdir -p /run /dev/pts /dev/shm /sys/fs/cgroup
mountfs tmpfs /run mode=0755,nosuid,nodev,size=10%
case " $("$bb" cat /proc/cmdline) " in
    *" koi.trace=1 "*) "$bb" touch /run/koi-trace;;
esac
trace stage1.mounts
mountfs devpts /dev/pts nosuid,noexec,gid=5,mode=620,ptmxmode=666
mountfs tmpfs /dev/shm mode=1777,nosuid,nodev
mountfs cgroup2 /sys/fs/cgroup nosuid,nodev,noexec
$bb mkdir -p /run/lock /run/user /run/koi-seed /tmp
$bb chmod 1777 /run/lock /tmp
$bb mkdir -p /run/wrappers/bin
$bb ln -s /usr/local/libexec/unix_chkpwd /run/wrappers/bin/unix_chkpwd
/usr/bin/sysctl -q --system
trace stage1.sysctl.end
if [ -f /var/lib/koi/identity ]; then
    $bb hostname "$("$bb" cat /etc/hostname)"
    trace stage1.identity.retained
else
    $bb mount -t ext4 -o ro,nosuid,nodev,noexec /dev/vdb /run/koi-seed
    trace stage1.seed.mounted
    /usr/local/libexec/koi-first-boot
    trace stage1.seed.applied
    $bb umount /run/koi-seed
fi
/usr/local/libexec/koi-network
trace stage1.network.end
for service in /etc/sv/koi-*; do
    $bb mkdir -p "/run/koi-supervise/${service##*/}"
done
$bb install -m000 /dev/null /run/runit.stopit
$bb install -m000 /dev/null /run/runit.reboot
trace stage1.end
trap - 0
