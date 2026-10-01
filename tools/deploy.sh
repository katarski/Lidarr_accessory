#!/bin/sh
# Incremental cue_pipeline deploy from /tmp/cue_test (a tar of the repo).
# Base image: cue_pipeline:pre-gate-20260929 (the last full build). Never
# Edit->Apply; tpl2run + mkrun.py assertions; recreate only if all pass.
set -e
TAG=${1:?usage: cue_deploy.sh <guard-tag>}
rm -rf /tmp/cuebuild && mkdir /tmp/cuebuild
cp /tmp/cue_test/*.py /tmp/cuebuild/ && cp -r /tmp/cue_test/tools /tmp/cuebuild/tools
chmod -R a+rX /tmp/cuebuild
printf 'FROM cue_pipeline:pre-gate-20260929\nCOPY *.py /app/\nCOPY tools/ /app/tools/\n' > /tmp/cuebuild/Dockerfile
docker build -q -t cue_pipeline:latest -t "cue_pipeline:$TAG" /tmp/cuebuild >/dev/null
docker run --rm --user 99:100 --entrypoint python cue_pipeline:latest -c \
  "import main, orchestrator, dataclasses; assert dataclasses.is_dataclass(orchestrator.OrchestratorConfig)"
( umask 077
  docker run --rm --entrypoint python3 -v /boot/config/plugins/dockerMan/templates-user:/tpl:ro \
    cue_pipeline:latest /app/tools/tpl2run.py /tpl/my-cue_pipeline.xml > /tmp/cue_run.sh )
docker run --rm -v /tmp:/t --entrypoint python cue_pipeline:latest /app/tools/mkrun.py /t/cue_run.sh /t/cue_run2.sh
docker stop -t 90 cue_pipeline >/dev/null || true
docker rm cue_pipeline >/dev/null
sh /tmp/cue_run2.sh >/dev/null 2>&1
rm -f /tmp/cue_run.sh /tmp/cue_run2.sh
# SearXNG (on br2.2) over a private network the pipeline owns: Park cannot
# reach its own ipvlan children, and NATed container traffic routed through
# the router is broken by bridged conntrack. --internal = no gateway, so
# neither container's egress moves. Idempotent; re-attaches a recreated
# SearXNG too.
docker network inspect searxng_link >/dev/null 2>&1 \
  || docker network create --internal --driver bridge searxng_link >/dev/null
for c in cue_pipeline SearXNG; do
  docker network connect searxng_link "$c" 2>/dev/null || true
done
sleep 20
docker inspect -f '{{.State.Status}} restarts={{.RestartCount}} img={{.Image}}' cue_pipeline
docker exec cue_pipeline sh -c 'echo downloads=$(ls /downloads | wc -l) music=$(ls /music | wc -l)'
docker logs --since 30s cue_pipeline 2>&1 | grep -E "Traceback|Error" | head -5 || true
