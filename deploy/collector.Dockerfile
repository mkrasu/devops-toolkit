# SPDX-License-Identifier: MIT
# Collector image: the toolkit's tools plus kubectl and curl, for running
# as Kubernetes CronJobs (or any container scheduler) that POST results to
# the dashboard's ingest API.
#
# Build from the repository root:
#   docker build -f deploy/collector.Dockerfile -t devops-toolkit-collector .
FROM python:3.13-slim

ARG KUBECTL_VERSION=v1.31.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ARCH="$(dpkg --print-architecture)" \
 && curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && useradd --create-home --uid 1000 collector

COPY k8s-resource-auditor/ /toolkit/k8s-resource-auditor/
COPY endpoint-watchdog/    /toolkit/endpoint-watchdog/
COPY sys-triage/           /toolkit/sys-triage/
COPY host-hardening-check/ /toolkit/host-hardening-check/
COPY log-tailer-alert/     /toolkit/log-tailer-alert/
COPY db-backup-rotate/     /toolkit/db-backup-rotate/

USER collector
# No default command: the CronJob supplies the tool invocation.
CMD ["python3", "/toolkit/k8s-resource-auditor/k8s-audit.py", "--help"]
