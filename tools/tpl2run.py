"""Print the `docker run` that the Unraid template describes.

Recreating this container by hand is how you lose mounts: Docker CREATES a
mistyped bind-mount source as an empty directory, and an empty /downloads reads
as "every completed torrent was already imported". Generate the command from
the template instead of retyping it.

Usage (works even when the container is down, as long as the image exists):

    docker run --rm \
      -v /boot/config/plugins/dockerMan/templates-user:/tpl:ro \
      cue_pipeline:latest python3 /app/tools/tpl2run.py /tpl/my-cue_pipeline.xml
"""
from __future__ import annotations

import shlex
import sys
import xml.etree.ElementTree as ET

TEMPLATE = (sys.argv[1] if len(sys.argv) > 1
            else "/tpl/my-cue_pipeline.xml")


def main() -> int:
    root = ET.parse(TEMPLATE).getroot()

    def field(name: str) -> str:
        el = root.find(name)
        return (el.text or "").strip() if el is not None and el.text else ""

    name = field("Name") or "cue_pipeline"
    image = field("Repository") or "cue_pipeline:latest"
    args = [
        "docker run -d",
        "--name " + shlex.quote(name),
        "--net " + (field("Network") or "bridge"),
        # without this label Unraid's Docker page loses the Edit button
        "--label net.unraid.docker.managed=dockerman",
    ]
    if field("CPUset"):
        args.append("--cpuset-cpus " + field("CPUset"))
    if field("ExtraParams"):
        args.append(field("ExtraParams"))

    problems = []
    for cfg in root.findall("Config"):
        kind = cfg.get("Type")
        target = cfg.get("Target") or ""
        mode = cfg.get("Mode") or ""
        value = (cfg.text or "").strip()
        if kind == "Path":
            if not value:
                problems.append("no host path set for %s" % target)
                continue
            suffix = ":" + mode if mode and mode != "rw" else ""
            args.append("-v " + shlex.quote(value + ":" + target + suffix))
        elif kind == "Port":
            args.append("-p %s:%s/%s" % (value, target, mode or "tcp"))
        elif kind == "Variable" and value:
            args.append("-e " + shlex.quote(target + "=" + value))

    args.append("--restart unless-stopped")
    args.append(shlex.quote(image))
    for p in problems:
        print("# WARNING: " + p, file=sys.stderr)
    print(" \\n  ".join(args))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
