#!/bin/sh
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mkdir -p /etc/runit/runsvdir/koi
printf 'koi\n' > /etc/runit/default.runsvdir
touch /etc/runit/no.emulate.sysv
service() {
    name=$1
    directory=/etc/sv/koi-$name
    mkdir -p "$directory/log" "/var/log/runit/$name"
    chmod 700 "/var/log/runit/$name"
    printf 's1000000\nn5\n' > "/var/log/runit/$name/config"
    { printf '#!/bin/sh\nset -eu\nexec 2>&1\n'; cat; } > "$directory/run"
    printf '#!/bin/sh\nexec svlogd -tt /var/log/runit/%s\n' "$name" > "$directory/log/run"
    chmod 755 "$directory/run" "$directory/log/run"
    ln -s "$directory" "/etc/runit/runsvdir/koi/$name"
}
service ssh <<'EOF'
test -f /var/lib/koi/initialized
mkdir -p /run/sshd
chmod 755 /run/sshd
rm -f /run/nologin
exec /usr/sbin/sshd -D -e
EOF
service incus-agent <<'EOF'
cd /run/incus_agent
exec /run/koi-agent/incus-agent
EOF
service dhcp <<'EOF'
ip link set lo up
exec /usr/sbin/udhcpc -f -i eth0 -s /etc/udhcpc/default.script
EOF
service chrony <<'EOF'
mkdir -p /run/chrony
chown _chrony:_chrony /run/chrony
chmod 750 /run/chrony
exec /usr/sbin/chronyd -d
EOF
mkdir -p /etc/acpi/events
printf 'event=button/power.*\naction=/sbin/poweroff\n' > /etc/acpi/events/koi-power
service acpi <<'EOF'
exec /usr/sbin/acpid -f -d
EOF

chmod 755 /usr/local/libexec/koi-first-boot
chmod 755 /usr/local/libexec/koi-prepare-agent
chmod 755 /etc/runit/1
rm -f /etc/resolv.conf
install -m 644 /dev/null /etc/resolv.conf
printf 'TrustedUserCAKeys /etc/ssh/koi-ca.pub\nAuthorizedPrincipalsFile /etc/ssh/koi-principals\nAllowUsers dev\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' \
    > /etc/ssh/sshd_config.d/10-koi.conf
