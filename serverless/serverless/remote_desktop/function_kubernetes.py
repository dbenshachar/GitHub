"""Kubernetes API transport and manifests; no Docker daemon or socket."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

from .functions import FunctionError, FunctionSpec

LABEL = "mini-functions"


class KubernetesAPI:
    def __init__(self, namespace="mini-functions"):
        self.namespace = namespace
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token_file = directory / "token"
        self.context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = "[" + host + "]"
        self.base = f'https://{host}:{os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")}'

    def request(self, method, path, body=None):
        request = Request(self.base + path, None if body is None else json.dumps(body).encode(),
            {"Authorization": "Bearer " + self.token_file.read_text().strip(), "Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, context=self.context, timeout=2) as response:
                if "/log?" in path:
                    return response.read(2 * 1024 * 1024).decode(errors="replace")
                return json.load(response)
        except HTTPError as exc:
            raise APIError(exc.code, exc.read(4096).decode(errors="replace")) from exc

    def path(self, resource, name=""):
        group = "/apis/batch/v1" if resource in {"jobs", "cronjobs"} else "/api/v1"
        return f"{group}/namespaces/{quote(self.namespace, safe='')}/{resource}" + ("/" + quote(name, safe="") if name else "")

    def list(self, resource, selector):
        items, continuation = [], ""
        while True:
            query = urlencode({"labelSelector": selector, "limit": 500, "continue": continuation})
            page = self.request("GET", self.path(resource) + "?" + query)
            items.extend(page["items"])
            continuation = page.get("metadata", {}).get("continue", "")
            if not continuation:
                return items


class APIError(FunctionError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class JobRouter:
    """Stateless replicas share Kubernetes state behind a load-balanced Service."""
    def __init__(self, api, *, image, router_url, max_fanout=256, parallelism=64, max_depth=8):
        if not image or image.startswith("-"):
            raise ValueError("worker image is required")
        self.api, self.image, self.router_url = api, image, router_url
        self.max_fanout, self.parallelism, self.max_depth = max_fanout, parallelism, max_depth

    def job_manifest(self, spec, *, count=1, name=None, root=None, depth=0, parent_uid=None):
        if type(count) is not int or not 1 <= count <= self.max_fanout:
            raise ValueError("fanout count exceeds limit")
        if not 0 <= depth <= self.max_depth:
            raise ValueError("fanout depth exceeds limit")
        name = name or "mini-" + uuid4().hex
        labels = {"app": LABEL, "mini-root": root or name, "mini-depth": str(depth)}
        if parent_uid:
            labels["mini-parent-uid"] = parent_uid
        env = [
            {"name": "FUNCTION_SPEC", "value": json.dumps(spec.to_dict())},
            {"name": "FUNCTION_ROUTER_URL", "value": self.router_url},
            {"name": "FUNCTION_TOKEN", "valueFrom": {"secretKeyRef": {"name": "mini-functions-auth", "key": "token"}}},
            {"name": "FUNCTION_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {"name": "FUNCTION_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        ]
        return {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": self.api.namespace, "labels": labels},
            "spec": {
                "completionMode": "Indexed", "completions": count, "parallelism": min(count, self.parallelism),
                "backoffLimit": 0, "activeDeadlineSeconds": spec.timeout_s, "ttlSecondsAfterFinished": 600,
                "template": {"metadata": {"labels": dict(labels)}, "spec": {
                    "restartPolicy": "Never", "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 5,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534, "fsGroup": 65534,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                    "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
                        "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": {"app": LABEL}}}],
                    "containers": [{"name": "mini-os", "image": self.image, "imagePullPolicy": "IfNotPresent",
                        "command": ["python3", "-u", "-m", "remote_desktop.function_worker"], "env": env,
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": str(spec.cpus), "memory": f"{spec.memory_mb + 192}Mi", "ephemeral-storage": "256Mi"},
                                      "limits": {"cpu": str(spec.cpus), "memory": f"{spec.memory_mb + 192}Mi", "ephemeral-storage": "256Mi"}},
                        "volumeMounts": [{"name": "scratch", "mountPath": "/tmp"}],
                    }], "volumes": [{"name": "scratch", "emptyDir": {"sizeLimit": "256Mi"}}],
                }},
            },
        }

    def submit(self, spec, *, count=1, name=None, root=None, depth=0, parent_uid=None):
        manifest = self.job_manifest(spec, count=count, name=name, root=root, depth=depth, parent_uid=parent_uid)
        try:
            result = self.api.request("POST", self.api.path("jobs"), manifest)
        except APIError as exc:
            if exc.status != 409 or not parent_uid:
                raise
            result = self.api.request("GET", self.api.path("jobs", manifest["metadata"]["name"]))
            if result["metadata"]["labels"].get("mini-parent-uid") != parent_uid:
                raise FunctionError("fanout idempotency conflict")
        return {"job_id": result["metadata"]["name"], "count": count}

    def fanout(self, parent_pod, parent_uid, count, script, request_id):
        pod = self.api.request("GET", self.api.path("pods", parent_pod))
        metadata = pod["metadata"]
        if metadata["uid"] != parent_uid or metadata.get("labels", {}).get("app") != LABEL:
            raise ValueError("invalid parent identity")
        if metadata.get("deletionTimestamp") or pod.get("status", {}).get("phase") not in {"Pending", "Running"}:
            raise ValueError("parent pod is no longer active")
        labels = metadata["labels"]
        spec = next(env["value"] for env in pod["spec"]["containers"][0]["env"] if env["name"] == "FUNCTION_SPEC")
        child = FunctionSpec(**json.loads(spec)).child(script)
        # The same pod submission sequence maps to the same Job across router replicas.
        if type(request_id) is not int or request_id < 0:
            raise ValueError("invalid fanout request ID")
        name = "fanout-" + hashlib.sha256(f"{parent_uid}:{request_id}".encode()).hexdigest()[:32]
        return self.submit(child, count=count, name=name, root=labels.get("mini-root") or labels["batch.kubernetes.io/job-name"],
                           depth=int(labels.get("mini-depth", "0")) + 1, parent_uid=parent_uid)

    def cron_manifest(self, spec, *, name, cron, timezone="UTC", count=1):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,50}[a-z0-9])?", name):
            raise ValueError("cron name must be a DNS label of at most 52 characters")
        if len(cron.split()) != 5:
            raise ValueError("cron requires five fields; Kubernetes validates field syntax")
        ZoneInfo(timezone)
        job = self.job_manifest(spec, count=count)
        # A CronJob invocation gets its root from its generated Job identity.
        del job["metadata"]["labels"]["mini-root"]
        del job["spec"]["template"]["metadata"]["labels"]["mini-root"]
        return {"apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": name, "namespace": self.api.namespace, "labels": {"app": LABEL}},
            "spec": {"schedule": cron, "timeZone": timezone, "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 60,
                     "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 1,
                     "jobTemplate": {"metadata": {"labels": job["metadata"]["labels"]}, "spec": job["spec"]}}}

    def schedule(self, spec, **options):
        manifest = self.cron_manifest(spec, **options)
        result = self.api.request("POST", self.api.path("cronjobs"), manifest)
        return {"schedule_id": result["metadata"]["name"]}

    def status(self, name):
        job = self.api.request("GET", self.api.path("jobs", name))
        if job["metadata"].get("labels", {}).get("app") != LABEL:
            raise ValueError("not a Mini OS job")
        status = job.get("status", {})
        conditions = {c["type"] for c in status.get("conditions", []) if c["status"] == "True"}
        phase = "failed" if conditions & {"Failed", "FailureTarget"} else "complete" if "Complete" in conditions else "running"
        report = {"job_id": name, "phase": phase, "status": status, "results": []}
        if phase in {"failed", "complete"}:
            pods = self.api.list("pods", "batch.kubernetes.io/job-name=" + name)
            for pod in pods:
                log = self.api.request("GET", self.api.path("pods", pod["metadata"]["name"]) + "/log?container=mini-os&tailLines=1")
                # API transports return log text rather than JSON for the log endpoint.
                try:
                    value = json.loads(log) if isinstance(log, str) else log
                    report["results"].append({"pod": pod["metadata"]["name"], "node": pod["spec"].get("nodeName"),
                        "index": pod["metadata"].get("annotations", {}).get("batch.kubernetes.io/job-completion-index"), **value})
                except (ValueError, TypeError):
                    report["results"].append({"pod": pod["metadata"]["name"], "error": "worker result unavailable"})
        return report

    def cancel(self, name):
        job = self.api.request("GET", self.api.path("jobs", name))
        if job["metadata"].get("labels", {}).get("app") != LABEL:
            raise ValueError("not a Mini OS job")
        self.api.request("DELETE", self.api.path("jobs", name), {"propagationPolicy": "Foreground"})
        # Whole-tree cancellation is explicit; ordinary completion leaves children independent.
        root = job["metadata"]["labels"].get("mini-root") or name
        for child in self.api.list("jobs", "mini-root=" + root):
            if child["metadata"]["name"] != name:
                self.api.request("DELETE", self.api.path("jobs", child["metadata"]["name"]), {"propagationPolicy": "Foreground"})
        return {"cancelled": name}
