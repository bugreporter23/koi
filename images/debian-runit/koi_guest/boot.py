"""The supported Debian/runit direct-boot contract."""

import hashlib
import json
from pathlib import Path

from koi.common import require, token


def configure(properties, config, *, name, ca, identifier):
    require(properties["koi.boot"] == "q35-runit-v3", "unsupported guest boot format")
    kernel_digest = token(properties.get("koi.kernel-sha256"), r"[0-9a-f]{64}")
    kernel = Path("/usr/share/qemu/koi") / kernel_digest / "kernel"
    with kernel.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == kernel_digest,
            "Image kernel is damaged; run koi build-image",
        )
    config["raw.qemu"] = (
        f"-kernel {kernel} -append 'root=/dev/sda1 rootwait rw "
        "console=ttyS0 net.ifnames=0 quiet lsm=landlock pci=lastbus=0 swiotlb=noforce nomodule' "
        "-machine sata=off,i8042=off"
    )
    config["raw.qemu.conf"] = Path(__file__).with_name("q35-headless.conf").read_text()
    config["user.koi.seed"] = json.dumps(
        {
            "name": name,
            "ca": ca,
            "id": identifier,
        }
    )
