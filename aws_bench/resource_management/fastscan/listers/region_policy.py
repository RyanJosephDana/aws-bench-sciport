"""Hand-curated per-lister regions where a lister's results can't be obtained.

Same ``(service, op)`` shape and handling as the generated ``region_skip.LISTER_REGION_SKIP``,
but a different reason: region_skip records where a service has *no endpoint* (topology); this
records where the endpoint is reachable but the scan gets no successful response across retries.
Listed here, the lister is recorded ``empty`` (not ``failed``, which would abort the region via
the fail-loud gate) and still scanned in every other region. Op-level, so a working sibling op
is never dropped. Pure data (ships in the Lambda closure).

Each entry requires proof: a lone isolated call returns no success across retries in that region
while succeeding elsewhere (rules out account-wide throttling and concurrency).
"""

from __future__ import annotations

# Greengrass V1 + V2 list calls returned no success in us-west-1 (isolated call, no success across
# retries there, <1s elsewhere, s3 control unaffected). Greengrass V1 is also EOL (2026-06-01).
UNAVAILABLE_LISTER_REGIONS: dict[tuple[str, str], frozenset[str]] = {
    ("greengrass", "ListCoreDefinitions"): frozenset({"us-west-1"}),
    ("greengrass", "ListLoggerDefinitionVersions"): frozenset({"us-west-1"}),
    ("greengrass", "ListSubscriptionDefinitions"): frozenset({"us-west-1"}),
    ("greengrass", "list_connector_definitions"): frozenset({"us-west-1"}),
    ("greengrass", "list_device_definitions"): frozenset({"us-west-1"}),
    ("greengrass", "list_function_definitions"): frozenset({"us-west-1"}),
    ("greengrass", "list_groups"): frozenset({"us-west-1"}),
    ("greengrass", "list_logger_definitions"): frozenset({"us-west-1"}),
    ("greengrass", "list_resource_definitions"): frozenset({"us-west-1"}),
    ("greengrassv2", "ListComponents"): frozenset({"us-west-1"}),
    ("greengrassv2", "ListCoreDevices"): frozenset({"us-west-1"}),
    ("greengrassv2", "ListDeployments"): frozenset({"us-west-1"}),
}
