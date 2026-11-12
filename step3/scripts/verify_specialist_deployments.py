"""Verify all agent and Dapr sidecars plus isolated specialist subscriptions."""

import json
import subprocess


WORKERS = {
    "continuity-worker": ("story.continuity-check.requested", "continuity-check"),
    "brief-compliance-worker": ("story.brief-compliance.requested", "brief-compliance"),
}
STORY_AGENTS = {
    "story-writer-agent": (8100, "story-writer"),
    "quality-review-agent": (8101, "quality-review"),
}


def kubectl(*args):
    return subprocess.run(
        ["kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def verify(name, event_type, agent_name):
    deployment = json.loads(kubectl("get", "deployment", name, "-o", "json"))
    template = deployment["spec"]["template"]
    app_id = template["metadata"]["annotations"]["dapr.io/app-id"]
    if app_id != name:
        raise AssertionError(f"{name} has unexpected Dapr app ID {app_id!r}")
    expected_containers = {name, f"{name.removesuffix('-worker')}-agent"}
    template_containers = {
        container["name"] for container in template["spec"]["containers"]
    }
    if not expected_containers.issubset(template_containers):
        raise AssertionError(
            f"{name} pod template is missing its workflow host or agent sidecar: "
            f"{template_containers}"
        )

    pods = json.loads(kubectl("get", "pod", "-l", f"app={name}", "-o", "json"))["items"]
    ready_pods = []
    for candidate in pods:
        statuses = candidate.get("status", {}).get("containerStatuses", [])
        if len(statuses) == len(candidate["spec"]["containers"]) and all(
            container.get("ready", False) for container in statuses
        ):
            ready_pods.append(candidate)
    if not ready_pods:
        raise AssertionError(f"{name} has no pod with all containers ready")
    pod = max(ready_pods, key=lambda item: item["metadata"]["creationTimestamp"])
    containers = {item["name"] for item in pod["spec"]["containers"]}
    if not expected_containers.issubset(containers) or "daprd" not in containers:
        raise AssertionError(
            f"{name} pod is missing its workflow host, agent, or Dapr sidecar: "
            f"{containers}"
        )
    readiness = {
        item["name"]: item.get("ready", False)
        for item in pod.get("status", {}).get("containerStatuses", [])
    }
    if not all(readiness.get(container) for container in expected_containers | {"daprd"}):
        raise AssertionError(f"{name} containers are not all ready: {readiness}")

    code = (
        "import json,urllib.request;"
        "print(json.dumps(json.load(urllib.request.urlopen("
        "'http://127.0.0.1:8001/dapr/subscribe'))))"
    )
    subscriptions = json.loads(kubectl(
        "exec", f"pod/{pod['metadata']['name']}", "-c", name, "--", "python", "-c", code
    ))
    if len(subscriptions) != 1:
        raise AssertionError(f"{name} has unexpected subscriptions: {subscriptions}")
    subscription = subscriptions[0]
    rules = subscription["routes"]["rules"]
    expected = {"match": f'event.type == "{event_type}"', "path": "/events"}
    if (
        subscription.get("pubsubname") != "pubsub"
        or subscription.get("topic") != "story-events"
        or rules != [expected]
    ):
        raise AssertionError(f"{name} does not have an isolated {event_type} subscription: {subscription}")
    health = json.loads(kubectl(
        "exec", f"pod/{pod['metadata']['name']}", "-c", name, "--",
        "python", "-c",
        "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8100/healthz'))))",
    ))
    if health != {"status": "ok", "agent": agent_name}:
        raise AssertionError(f"{name} agent sidecar health check failed: {health}")
    agent_container_name = name.removesuffix("-worker") + "-agent"
    print(
        f"[PASS] {name} pod has ready workflow-host, {agent_container_name} agent sidecar, "
        f"and daprd containers; subscribes only to {event_type}"
    )


def verify_story_agent_sidecars():
    name = "story-agent"
    deployment = json.loads(kubectl("get", "deployment", name, "-o", "json"))
    template = deployment["spec"]["template"]
    expected_containers = {name, *STORY_AGENTS.keys()}
    template_containers = {
        container["name"] for container in template["spec"]["containers"]
    }
    if not expected_containers.issubset(template_containers):
        raise AssertionError(
            f"{name} pod template is missing its agent sidecars: {template_containers}"
        )

    pods = json.loads(kubectl("get", "pod", "-l", f"app={name}", "-o", "json"))["items"]
    ready_pods = []
    for candidate in pods:
        statuses = candidate.get("status", {}).get("containerStatuses", [])
        if len(statuses) == len(candidate["spec"]["containers"]) and all(
            container.get("ready", False) for container in statuses
        ):
            ready_pods.append(candidate)
    if not ready_pods:
        raise AssertionError(f"{name} has no pod with all containers ready")
    pod = max(ready_pods, key=lambda item: item["metadata"]["creationTimestamp"])
    containers = {item["name"] for item in pod["spec"]["containers"]}
    if not expected_containers.issubset(containers) or "daprd" not in containers:
        raise AssertionError(
            f"{name} pod is missing its app, agent, or Dapr sidecars: {containers}"
        )

    for container_name, (port, agent_name) in STORY_AGENTS.items():
        health = json.loads(kubectl(
            "exec", f"pod/{pod['metadata']['name']}", "-c", container_name, "--",
            "python", "-c",
            "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen("
            f"'http://127.0.0.1:{port}/healthz'))))",
        ))
        if health != {"status": "ok", "agent": agent_name}:
            raise AssertionError(f"{container_name} health check failed: {health}")

    print(
        "[PASS] story-agent pod has ready story-agent, story-writer-agent, "
        "quality-review-agent, and daprd containers"
    )


if __name__ == "__main__":
    verify_story_agent_sidecars()
    for worker, (assigned_event, agent_name) in WORKERS.items():
        verify(worker, assigned_event, agent_name)
