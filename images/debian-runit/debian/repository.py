"""Index a local repository of Nix-verified Debian packages."""

import hashlib
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
index = root / "dists/trixie/main/binary-amd64/Packages"
with index.open("w") as output:
    for package in sorted((root / "pool").iterdir()):
        control = subprocess.check_output(
            ["dpkg-deb", "--field", str(package)], text=True
        )
        with package.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        output.write(control.rstrip() + "\n")
        output.write(f"Filename: pool/{package.name}\n")
        output.write(f"Size: {package.stat().st_size}\nSHA256: {digest}\n\n")
digest = hashlib.sha256(index.read_bytes()).hexdigest()
(root / "dists/trixie/Release").write_text(
    "Suite: trixie\nCodename: trixie\nArchitectures: amd64\nComponents: main\n"
    f"SHA256:\n {digest} {index.stat().st_size} main/binary-amd64/Packages\n"
)
