#!/bin/sh
set -eu
root=${1:?prepared root directory required}
shift
pam=$1
sshd=$2
udevd=$3
ca=$4
printf 'Defaults secure_path="/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\nroot ALL=(ALL) ALL\n@includedir /etc/sudoers.d\n' > "$root/etc/sudoers"
printf 'dev ALL=(ALL) NOPASSWD: ALL\n' > "$root/etc/sudoers.d/dev"
chmod 440 "$root/etc/sudoers" "$root/etc/sudoers.d/dev"
git config --file "$root/etc/gitconfig" user.name user.name
git config --file "$root/etc/gitconfig" user.email user.email
git config --file "$root/etc/gitconfig" user.useConfigOnly true
chmod 1775 "$root/nix/store"
cat > "$root/etc/nix/nix.conf" <<'NIX'
build-users-group = nixbld
experimental-features = nix-command flakes
sandbox = true
ssl-cert-file = /etc/ssl/certs/ca-certificates.crt
NIX
ln -s "$ca" "$root/etc/ssl/certs/ca-certificates.crt"
cat > "$root/etc/profile" <<'PROFILE'
export PATH="$HOME/.nix-profile/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG=C.UTF-8 TZ=UTC
export NIX_REMOTE=daemon NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
if [ -n "${BASH_VERSION-}" ]; then . /etc/bashrc; fi
for file in /etc/profile.d/*.sh; do
    [ ! -f "$file" ] || . "$file"
done
PROFILE
cat > "$root/etc/bashrc" <<'BASH'
case $- in
    *i*) PS1='\u@\h:\w\$ ';;
esac
BASH
mkdir -p "$root/home/dev/.local/state/nix/profiles"
ln -s .local/state/nix/profiles/profile "$root/home/dev/.nix-profile"
cat > "$root/etc/pam.d/sshd" <<PAM
auth required $pam/pam_unix.so
account required $pam/pam_nologin.so
account required $pam/pam_unix.so
session required $pam/pam_limits.so
session required $pam/pam_unix.so
session required $pam/pam_exec.so quiet /usr/local/libexec/busybox touch /run/koi-services-ready
PAM
cat > "$root/etc/pam.d/sudo" <<PAM
auth required $pam/pam_unix.so
account required $pam/pam_unix.so
session required $pam/pam_limits.so
session required $pam/pam_unix.so
PAM
printf 'auth include sudo\naccount include sudo\nsession include sudo\n' > "$root/etc/pam.d/sudo-i"
cat > "$root/etc/pam.d/runuser" <<PAM
auth required $pam/pam_rootok.so
account required $pam/pam_unix.so
session required $pam/pam_unix.so
PAM
cat > "$root/etc/pam.d/other" <<PAM
auth required $pam/pam_deny.so
account required $pam/pam_deny.so
password required $pam/pam_deny.so
session required $pam/pam_deny.so
PAM
: > "$root/etc/security/limits.conf"
cat > "$root/etc/ssh/sshd_config" <<SSH
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/sshd.pid
AllowUsers dev
AuthenticationMethods publickey
PubkeyAuthentication yes
AllowAgentForwarding no
UsePAM yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AuthorizedKeysFile .ssh/authorized_keys
Subsystem sftp internal-sftp
SetEnv PATH=/home/dev/.nix-profile/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin NIX_REMOTE=daemon NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
SSH
mkdir -p "$root/etc/koi/early-services"
service() {
    name=$1
    tree=/etc/service
    case "$name" in ssh|dhcp|acpi|ready|services) tree=/etc/koi/early-services;; esac
    directory=/etc/sv/koi-$name
    mkdir -p "$root$directory"
    { printf '#!/usr/local/libexec/busybox sh\nset -eu\nexport PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\nexec 2>&1\n'; cat; } > "$root$directory/run"
    chmod 755 "$root$directory/run"
    ln -s "/run/koi-supervise/koi-$name" "$root$directory/supervise"
    ln -s "$directory" "$root$tree/$name"
}
service ssh <<SSH
test -f /var/lib/koi/identity
exec $sshd -D -e -f /etc/ssh/sshd_config
SSH
service acpi <<'ACPI'
exec acpid -f -d
ACPI
service ready <<'READY'
/usr/local/libexec/koi-ready
exec /usr/local/libexec/busybox sleep infinity
READY
service services <<'SERVICES'
/usr/local/libexec/koi-ready --wait-session
/usr/local/libexec/koi-prefetch /etc/koi/service-files &
exec runsvdir -P /etc/service
SERVICES
service udev <<UDEV
exec $udevd
UDEV
service coldplug <<'COLDPLUG'
udevadm control --timeout=10 --reload
udevadm trigger --action=add
udevadm settle --timeout=15
exec /usr/local/libexec/busybox sleep infinity
COLDPLUG
service nix <<'NIX'
export NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
exec nix-daemon
NIX
cat > "$root/etc/chrony.conf" <<'CHRONY'
pool pool.ntp.org iburst
driftfile /var/lib/chrony/drift
makestep 1.0 3
rtcsync
CHRONY
service chrony <<'CHRONY'
exec chronyd -d -u chrony -f /etc/chrony.conf
CHRONY
cat > "$root/etc/runit/2" <<'STAGE2'
#!/usr/local/libexec/busybox sh
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec runsvdir -P /etc/koi/early-services
STAGE2
cat > "$root/etc/runit/3" <<'STAGE3'
#!/usr/local/libexec/busybox sh
set -eu
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# An unsuccessful shutdown task must not fall through to runit's final halt.
trap 'echo "koi: shutdown task failed; guest retained" >&2; exec /usr/local/libexec/busybox sleep infinity' 0
# Stop ordinary services first, while their supervisor and devices still exist.
for service in /etc/service/*; do
    [ -d "$service" ] || continue
    [ -p "$service/supervise/control" ] || continue
    sv -w 30 down "$service"
done
# Stop the ordinary parent before exiting its supervisors, preventing respawn.
sv -w 30 down /etc/koi/early-services/services
for tree in /etc/service /etc/koi/early-services; do
    for service in "$tree"/*; do
        [ -d "$service" ] || continue
        [ -p "$service/supervise/control" ] || continue
        sv -w 30 down "$service"
        sv -w 5 exit "$service"
    done
done
# Standard guest shutdown also terminates unsupervised processes. BusyBox's
# existing killall5 excludes PID 1, kernel threads and this shutdown session.
/usr/local/libexec/busybox killall5 -15 || [ "$?" -eq 2 ]
/usr/local/libexec/busybox sync
# Refuse to finish shutdown with an unflushed writable root. The controller
# times out and preserves evidence instead of force-killing the VMM.
# Allow ordinary SIGTERM cleanup to close writers; this is a shutdown deadline,
# not a boot delay or a forced VMM termination.
/usr/local/libexec/busybox timeout 5 /usr/local/libexec/busybox sh -c '
    until /usr/local/libexec/busybox mount -o remount,ro /; do
        /usr/local/libexec/busybox sleep 0.05
    done'
trap - 0
STAGE3
chmod 755 "$root/etc/runit/2" "$root/etc/runit/3"
ln -s /run/runit.stopit "$root/etc/runit/stopit"
ln -s /run/runit.reboot "$root/etc/runit/reboot"
for action in poweroff reboot; do
    target=/usr/sbin/$action
    rm -f "$root$target"
    number=0
    [ "$action" != reboot ] || number=6
    printf '#!/usr/local/libexec/busybox sh\nexec /usr/bin/runit-init %s\n' "$number" > "$root$target"
    chmod 755 "$root$target"
done
printf 'event=button/power.*\naction=/sbin/poweroff\n' > "$root/etc/acpi/events/koi-power"
