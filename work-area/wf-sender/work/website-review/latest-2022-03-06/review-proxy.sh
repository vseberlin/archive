#!/bin/sh
set -eu

review_container_ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' wf_sender_review_web)"
test -n "$review_container_ip"

exec socat \
  TCP-LISTEN:8877,bind=127.0.0.1,reuseaddr,fork \
  "TCP:${review_container_ip}:80"
