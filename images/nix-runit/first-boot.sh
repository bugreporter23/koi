#!/usr/local/libexec/busybox sh
set -eu
bb=/usr/local/libexec/busybox
seed=/run/koi-seed
IFS= read -r name < "$seed/name"
IFS= read -r identity < "$seed/id"
IFS= read -r key < "$seed/authorized_keys"
printf '%s\n' "$name" | "$bb" grep -Eq '^[a-z][a-z0-9-]{0,30}$'
printf '%s\n' "$identity" | "$bb" grep -Eq '^[0-9a-f]{32}$'
printf '%s\n' "$key" | "$bb" grep -Eq '^ssh-ed25519 [A-Za-z0-9+/]+={0,2}$'
[ "$("$bb" wc -l < "$seed/authorized_keys")" -eq 1 ]
/usr/bin/ssh-keygen -l -f "$seed/authorized_keys" >/dev/null
marker=/var/lib/koi/identity
if [ -f "$marker" ]; then
    [ "$("$bb" cat "$marker")" = "$identity" ]
else
    "$bb" mkdir -p /home/dev/.ssh
    "$bb" chmod 700 /home/dev/.ssh
    if [ -f "$seed/certificate-auth" ]; then
        [ "$("$bb" cat "$seed/certificate-auth")" = 1 ]
        printf '%s\n' "$key" > /etc/ssh/koi-ca.pub
        printf 'koi-%s\n' "$identity" > /etc/ssh/koi-principals
        printf 'TrustedUserCAKeys /etc/ssh/koi-ca.pub\nAuthorizedPrincipalsFile /etc/ssh/koi-principals\n' >> /etc/ssh/sshd_config
        : > /home/dev/.ssh/authorized_keys
    else
        printf '%s\n' "$key" > /home/dev/.ssh/authorized_keys
    fi
    "$bb" chmod 600 /home/dev/.ssh/authorized_keys
    "$bb" chown dev:dev /home/dev/.ssh /home/dev/.ssh/authorized_keys
    printf '%s\n' "$name" > /etc/hostname
    printf '127.0.0.1 localhost\n127.0.1.1 %s\n' "$name" > /etc/hosts
    "$bb" tr -d '-' < /proc/sys/kernel/random/uuid > /etc/machine-id
    if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
        /usr/bin/ssh-keygen -q -t ed25519 -N '' -C '' -f /etc/ssh/ssh_host_ed25519_key
    fi
    "$bb" mkdir -p /var/lib/koi
    "$bb" cp "$seed/network" /etc/koi/network
    printf '%s\n' "$identity" > "$marker"
fi
"$bb" hostname "$("$bb" cat /etc/hostname)"
