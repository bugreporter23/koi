#!/usr/local/libexec/busybox sh
set -eu
bb=/usr/local/libexec/busybox
{
    IFS= read -r address
    IFS= read -r gateway
    IFS= read -r dns
    if IFS= read -r extra; then exit 1; fi
} < /etc/koi/network
printf '%s\n' "$address" | "$bb" grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}/24$'
for value in "$gateway" "$dns"; do
    printf '%s\n' "$value" | "$bb" grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
done
"$bb" ip link set lo up
"$bb" ip link set eth0 up
"$bb" ip -4 address replace "$address" dev eth0
"$bb" ip -4 route replace default via "$gateway" dev eth0
printf 'nameserver %s\nsearch koi\n' "$dns" > /etc/resolv.conf
