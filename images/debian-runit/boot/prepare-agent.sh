#!/bin/sh
set -eu

mkdir -p /run/koi-agent/.mnt
mount -t 9p agent /run/koi-agent/.mnt -o ro,access=0,trans=virtio,cache=readahead
cp -a "/run/koi-agent/.mnt/incus-agent.linux.$(uname -m)" /run/koi-agent/incus-agent
chown root:root /run/koi-agent/incus-agent
umount /run/koi-agent/.mnt
rmdir /run/koi-agent/.mnt
