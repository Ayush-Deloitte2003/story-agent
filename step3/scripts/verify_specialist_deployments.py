"""Verify specialist Deployment sidecars and their isolated Dapr subscriptions."""

import json
import subprocess


WORKERS = {
    "continuity-worker": "story.continuity-check.requested",
    "brief-compliance-worker": "story.brief-compliance.requested",
}


def kubectl(*args):
    return subprocess.run(
        ["kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def verify(name, event_type):
    deployment = json.loads(kubectl("get", "deployment", name, "-o", "json"))
    template = deployment["spec"]["template"]
    app_id = template["metadata"]["annotations"]["dapr.io/app-id"]
    if app_id != name:
        raise AssertionError(f"{name} has unexpected Dapr app ID {app_id!r}")

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
    if not {name, "daprd"}.issubset(containers):
        raise AssertionError(f"{name} pod is missing its app or Dapr sidecar: {containers}")
    readiness = {
        item["name"]: item.get("ready", False)
        for item in pod.get("status", {}).get("containerStatuses", [])
    }
    if not readiness.get(name) or not readiness.get("daprd"):
        raise AssertionError(f"{name} app and sidecar are not both ready: {readiness}")

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
    print(f"[PASS] {name} has a ready daprd sidecar and subscribes only to {event_type}")


if __name__ == "__main__":
    for worker, assigned_event in WORKERS.items():
        verify(worker, assigned_event)
