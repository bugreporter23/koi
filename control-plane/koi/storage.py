"""Incus-owned ZFS capacity and Koi admission checks."""

from .common import require


def validate_pool(pool):
    require(pool["driver"] == "zfs", "Koi requires native Incus ZFS storage")
    require(not pool["config"].get("size"), "Koi does not support loop-backed ZFS")
    return pool["driver"]


def storage_usage(c, api):
    pool = api.request("GET", "/1.0/storage-pools/" + c["pool"])
    driver = validate_pool(pool)
    space = api.request("GET", "/1.0/storage-pools/" + c["pool"] + "/resources")[
        "space"
    ]
    total, used = space["total"], space["used"]
    require(total > 0 and 0 <= used <= total, "invalid storage capacity report")
    return {
        "driver": driver,
        "usedBytes": used,
        "totalBytes": total,
        "usedPercent": used * 100 / total,
    }


def storage(c, api):
    usage = storage_usage(c, api)
    require(
        usage["usedBytes"] * 100 < usage["totalBytes"] * c["maxPoolPercent"],
        "storage data reserve reached; no automatic deletion",
    )
    return usage
