"""Turn tpl2run's command into the final one: the two labels it omits, and
assertions so a malformed command can never replace the container."""
import re, sys

src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
assert s.count("docker run -d") == 1, "expected exactly one docker run"
anchor = "--label net.unraid.docker.managed=dockerman \\\n"
assert s.count(anchor) == 1, "managed label line not found"
labels = ("  --label 'net.unraid.docker.icon=https://raw.githubusercontent.com/"
          "Lidarr/Lidarr/develop/Logo/256.png' \\\n"
          "  --label 'net.unraid.docker.webui=http://[IP]:[PORT:8830]' \\\n")
s = s.replace(anchor, anchor + labels)
assert chr(92) + "n" not in s, "literal backslash-n in command"
assert len(re.findall(r"(?m)^\s*-v ", s)) == 6, "expected 6 mounts"
assert len(re.findall(r"-e [A-Z_]+=", s)) == 52, "expected 52 env vars"
for k in ("HA_URL", "HA_TOKEN", "LLM_NUM_CTX", "LLM_KEEP_ALIVE", "LLM_MODEL"):
    assert ("-e %s=" % k) in s, k
assert s.rstrip().endswith("cue_pipeline:latest"), "image must be last"
open(dst, "w", encoding="utf-8").write(s)
print("command ok")
