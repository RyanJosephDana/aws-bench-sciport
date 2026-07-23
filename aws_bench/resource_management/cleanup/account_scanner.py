"""Post-cleanup account scan.

Enumerates all resources via the configured scanner (fast-scan by default,
CCAPI when selected), then filters out infrastructure and reports orphans.
"""

from __future__ import annotations

from concurrent.futures import as_completed
from pathlib import Path

import boto3

from aws_bench.logging.logger import get_logger, log_context
from aws_bench.resource_management.ccapi.manager import CloudControlManager, Resource
from aws_bench.resource_management.ccapi.models import MAX_WORKERS_HEAVY, ScanResult
from aws_bench.resource_management.ccapi.region_resolvers import RegionResolver
from aws_bench.resource_management.cleanup.models import (
    SWEEPABLE_INFRA_TYPES,
    AccountScanResult,
    RegionScanAggregate,
    exclude_infra_resources,
)
from aws_bench.resource_management.deferred import exclude_deferred
from aws_bench.resource_management.scanner import make_scanner
from aws_bench.resource_management.utils.file_io import write_json
from aws_bench.resource_management.verify.comparators import filter_aws_managed_resources
from aws_bench.utils.concurrent import interruptible_executor, raise_if_shutdown
from aws_bench.utils.credentials_provider import create_regional_session

logger = get_logger(__name__)

# Sentinel key suffixes that mark a genuine *whole-region* scan failure, as
# opposed to a benign per-lister failure (keyed "<region>/<service>:<Op>").
# Per-lister failures are always present under fast-scan (hundreds of optional
# List/Describe calls a scenario never uses) and do NOT mean the scan was
# incomplete; only these sentinels do.
REGION_SCAN_ERROR_KEY = "_scan_error"
REGION_TASK_ERROR_KEY = "_task_error"


class AccountScanner:
    """Scans an AWS account for orphaned resources across regions."""

    def __init__(self, session: boto3.Session, account_id: str | None = None) -> None:
        """Initialize with a boto3 session.

        Args:
            session: boto3 session for the target account.
            account_id: Target account for the scan. Routes the per-region
                fast-scan to the management-account Lambda; without it the scan
                degrades to the throttled host path, where a failed lister is
                swallowed into ``scan_result.failed`` and the cleanup phases skip
                that type — leaving orphaned resources undeleted.
        """
        self._session = session
        self._account_id = account_id
        # Set per-run in run(): resource_type -> set of baseline Identifiers
        # to exclude from orphan reporting (the account's pre-setup defaults).
        self._baseline_ids: dict[str, set[str]] = {}

    def run(
        self,
        output_dir: Path,
        regions: list[str],
        predeploy_baseline: dict[str, list[dict]] | None = None,
        *,
        include_infra: bool = False,
    ) -> AccountScanResult:
        """Run a full account scan across all regions and save results.

        Args:
            output_dir: Directory to write the scan results file.
            regions: Regions to scan.
            predeploy_baseline: The account's pre-setup default resources
                (resource_type -> resources), captured at init. When provided,
                resources present in the baseline are excluded so only
                resources created after init are reported as orphans. When
                None, all non-infra resources are reported (legacy behavior).
            include_infra: Keep CDK bootstrap/toolkit resources in the report so
                the CDKToolkit stack's retained leftovers surface as orphans;
                by default they are filtered out.
        """
        if not regions:
            logger.debug("No regions to scan.")
            return AccountScanResult(orphaned_resources={}, region_counts={})

        logger.debug("Starting post-cleanup deep scan across %d region(s)...", len(regions))
        logger.debug("This may take several minutes for accounts with many regions...")
        # Build a (type -> set of known Identifiers) index from the pre-setup
        # baseline so each region scan can exclude the account's defaults and
        # report accurate per-region orphan counts.
        self._baseline_ids = {
            rtype: {item.get("Identifier", "") for item in items}
            for rtype, items in (predeploy_baseline or {}).items()
        }
        aggregate = self._scan_all_regions(regions, include_infra=include_infra)
        total = sum(len(items) for items in aggregate.scan_result.detected.values())
        failed = aggregate.scan_result.failed

        # A region is only "incomplete" if its whole scan failed (sentinel keys),
        # NOT because some optional per-lister List/Describe call failed — those
        # are expected on every fast-scan and would otherwise mark every clean
        # run INCOMPLETE.
        region_failure_suffixes = (
            f"/{REGION_SCAN_ERROR_KEY}",
            f"/{REGION_TASK_ERROR_KEY}",
        )
        failed_regions = {
            key: val for key, val in failed.items() if key.endswith(region_failure_suffixes)
        }

        if total:
            logger.warning("Post-cleanup scan found %d orphaned resource(s)", total)
        elif failed_regions:
            logger.warning(
                f"Post-cleanup scan INCOMPLETE: {len(failed_regions)} region(s) failed or "
                "were cancelled; not reporting clean."
            )
        else:
            logger.info("Post-cleanup scan clean — no orphaned resources found.")

        # Persist only the actionable region failures (the same set that drives the
        # INCOMPLETE verdict), not the full per-lister failure map — under fast-scan
        # the latter is dominated by benign optional-API failures that would swamp
        # the on-disk triage artifact and misrepresent what the scan acted on.
        self._write_scan_results(
            ScanResult(detected=aggregate.scan_result.detected, failed=failed_regions),
            total,
            output_dir,
        )
        # Consumers only need identifiers, so project the resource dicts to IDs.
        orphaned_resources = {
            rtype: [item.get("Identifier", "") for item in items]
            for rtype, items in aggregate.scan_result.detected.items()
        }
        return AccountScanResult(
            orphaned_resources=orphaned_resources,
            region_counts=aggregate.region_counts,
            failed_regions=failed_regions,
        )

    def scan_region(self, region: str, *, include_infra: bool = False) -> ScanResult:
        """Return the current per-region scan: detected resources AND failed types.

        A thin public wrapper over the internal per-region scan used by the
        orphan report, so the cleanup phases can ask "what is live here right now?"
        (grouped by CloudFormation type -> resources) without the orphan-report
        consolidation. Both ``detected`` and ``failed`` are returned so the phases
        can skip a type that failed to enumerate rather than treating it as "new"
        and deleting it. Baseline exclusion is applied by the caller, not here.

        ``include_infra`` keeps CDK bootstrap/toolkit resources (``CDKToolkit`` and
        ``cdk-hnb659fds-*``) in the scan; by default they are filtered out. It lets
        the sweep reach the CDKToolkit stack's retained assets bucket, which CFN
        leaves behind on stack deletion.
        """
        return self._scan_region(region, RegionResolver(self._session), include_infra=include_infra)

    def _write_scan_results(
        self, scan_result: ScanResult, total_orphaned: int, output_dir: Path
    ) -> None:
        """Write scan results to JSON file.

        Args:
            scan_result: The scan result containing detected resources and failures
            total_orphaned: Total count of orphaned resources
            output_dir: Directory to write the results file
        """
        write_json(
            {
                "orphaned_resources": {
                    rtype: [item.get("Identifier", "") for item in items]
                    for rtype, items in scan_result.detected.items()
                },
                "total_orphaned": total_orphaned,
                "types_failed": scan_result.failed,
            },
            output_dir / "post_cleanup_scan.json",
        )

    def _scan_all_regions(
        self, regions: list[str], *, include_infra: bool = False
    ) -> RegionScanAggregate:
        """Scan all regions in parallel and merge results.

        Global resources (e.g. CloudFront) have no region resolver, so
        ``RegionResolver.filter_resources_by_region`` keeps them in every region's
        scan. Merging deduplicates by ``(resource_type, Identifier)`` so each
        physical resource is reported and counted exactly once — attributed to
        whichever region's scan completes first — rather than once per region.
        Without this a single global orphan inflates ``total_orphaned`` and the
        per-region counts by a factor of the region count.

        Resources without an ``Identifier`` are never deduplicated: an empty id
        carries no identity, so two distinct id-less resources must not collide on
        ``(rtype, "")``. (CloudControl resources always carry an identifier, so
        this is defensive only.)
        """
        all_orphans: dict[str, list[dict]] = {}
        all_failed: dict[str, str] = {}
        region_counts: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()

        # One shared resolver so a global resource's region (e.g. an S3 bucket)
        # is looked up once, not once per region scan.
        resolver = RegionResolver(self._session)

        completed = 0
        with interruptible_executor(max_workers=min(len(regions), MAX_WORKERS_HEAVY)) as executor:
            futures = {
                executor.submit(
                    self._scan_region, region, resolver, include_infra=include_infra
                ): region
                for region in regions
            }
            for future in as_completed(futures):
                region = futures[future]
                completed += 1
                try:
                    scan_result = future.result()
                except Exception as exc:
                    # Record-and-continue: one region's scan failure must not discard the others.
                    logger.error("Region '%s' scan task failed: %s", region, exc)
                    all_failed[f"{region}/{REGION_TASK_ERROR_KEY}"] = str(exc)
                    region_counts[region] = 0
                    logger.debug("Progress: %d/%d regions scanned", completed, len(regions))
                    continue

                # Merge this region's orphans, skipping any (type, identifier)
                # already reported by an earlier-completing region — a global
                # resource surfaces identically in every region and must count
                # once. An empty identifier carries no identity, so it is always
                # kept (never deduplicated).
                region_count = 0
                for rtype, items in scan_result.detected.items():
                    for item in items:
                        identifier = item.get("Identifier", "")
                        key = (rtype, identifier)
                        if identifier and key in seen:
                            continue
                        if identifier:
                            seen.add(key)
                        all_orphans.setdefault(rtype, []).append(item)
                        region_count += 1
                region_counts[region] = region_count
                all_failed.update(scan_result.failed)

                logger.debug(
                    "Progress: %d/%d regions scanned (%s: %d orphans)",
                    completed,
                    len(regions),
                    region,
                    region_count,
                )

        return RegionScanAggregate(
            scan_result=ScanResult(detected=all_orphans, failed=all_failed),
            region_counts=region_counts,
        )

    def _scan_region(
        self, region: str, resolver: RegionResolver, *, include_infra: bool = False
    ) -> ScanResult:
        """Scan a single region and filter results.

        ``resolver`` is shared across regions so global-resource region lookups
        are memoized account-wide. When ``include_infra`` is True, CDK
        bootstrap/toolkit resources are NOT filtered out (see ``scan_region``).
        """
        raise_if_shutdown()
        with log_context(region):
            logger.debug("Starting region scan...")
            try:
                region_session = create_regional_session(self._session, region)
                scan_mgr = make_scanner(
                    region_session, region_name=region, account_id=self._account_id
                )
                scan_result = scan_mgr.scan_resources(region=region)
                raw_count = sum(len(items) for items in scan_result.detected.values())
                logger.debug("Scan complete, found %d raw resources", raw_count)
            except Exception as exc:
                logger.error("Failed to scan region: %s", exc)
                return ScanResult(
                    detected={}, failed={f"{region}/{REGION_SCAN_ERROR_KEY}": str(exc)}
                )

            # Filter out infrastructure, then AWS-managed resources. When the caller
            # sets include_infra we re-include only the CDKToolkit stack's retained
            # *regional* assets (SWEEPABLE_INFRA_TYPES — the assets bucket, bootstrap
            # param, image repo), NOT the global cdk-hnb659fds-* bootstrap IAM roles:
            # those may still back a surviving stack's RoleARN in this or another
            # region, and deleting one wedges every stack that references it.
            filtered = exclude_infra_resources(
                scan_result.detected,
                keep_types=SWEEPABLE_INFRA_TYPES if include_infra else frozenset(),
            )
            filtered = filter_aws_managed_resources(filtered)

            for rtype in list(filtered.keys()):
                resources = [
                    Resource(type=rtype, identifier=item.get("Identifier", ""))
                    for item in filtered[rtype]
                ]
                region_filtered = resolver.filter_resources_by_region(region, resources)
                filtered_ids = {res.identifier for res in region_filtered}
                filtered[rtype] = [
                    item for item in filtered[rtype] if item.get("Identifier", "") in filtered_ids
                ]
                if not filtered[rtype]:
                    del filtered[rtype]

            # Exclude the account's pre-setup default resources: anything present
            # at init is not an orphan created by the scenario.
            if self._baseline_ids:
                excluded = 0
                for rtype in list(filtered.keys()):
                    known = self._baseline_ids.get(rtype)
                    if not known:
                        continue
                    kept = [i for i in filtered[rtype] if i.get("Identifier", "") not in known]
                    excluded += len(filtered[rtype]) - len(kept)
                    if kept:
                        filtered[rtype] = kept
                    else:
                        del filtered[rtype]
                if excluded:
                    logger.debug(f"Excluded {excluded} pre-setup baseline resource(s)")

            # Exclude resources whose deletion was deferred this run (eventually
            # consistent), so the post-cleanup scan doesn't report them as orphans and fail cleanup.
            filtered = exclude_deferred(filtered)

            # Final guard against phantom orphans: the fast-scan Lambda's List/Describe can lag
            # (eventual consistency) and keep returning a just-deleted resource. Re-verify each
            # remaining orphan host-side and drop only the ones definitively confirmed gone.
            filtered = self._drop_confirmed_absent(region_session, filtered)

            return ScanResult(
                detected=filtered,
                failed={f"{region}/{key}": val for key, val in scan_result.failed.items()},
            )

    def _drop_confirmed_absent(
        self, session: boto3.Session, filtered: dict[str, list[dict]]
    ) -> dict[str, list[dict]]:
        """Drop only orphans a host-side CCAPI existence check confirms are gone.

        The fast-scan Lambda's ``List*``/``Describe*`` can lag behind a deletion (eventual
        consistency) and report a just-deleted resource as a phantom orphan — observed with a
        Cognito identity pool whose ``ListIdentityPools``/``DescribeIdentityPool`` kept returning
        a deleted pool at the Lambda's endpoint for well over an hour, while the host endpoint
        (and ``delete_identity_pool``) reported it gone. A definitive host-side CCAPI
        ``GetResource`` (ResourceNotFound) is authoritative here, so such a resource is dropped;
        a resource that still EXISTS, whose type CCAPI cannot check, or that errs is KEPT, so a
        real orphan is never masked. No-op when nothing was detected (the common clean run).
        """
        if not filtered:
            return filtered
        ccm = CloudControlManager(session)
        result: dict[str, list[dict]] = {}
        for rtype, items in filtered.items():
            kept = [
                item
                for item in items
                if not self._confirmed_absent(ccm, rtype, item.get("Identifier", ""))
            ]
            dropped = len(items) - len(kept)
            if dropped:
                # A host-side re-check corrected a stale fast-scan read.
                logger.debug(
                    "Dropped %d %s orphan(s) a host-side re-check confirmed gone "
                    "(stale fast-scan read)",
                    dropped,
                    rtype,
                )
            if kept:
                result[rtype] = kept
        return result

    @staticmethod
    def _confirmed_absent(ccm: CloudControlManager, rtype: str, identifier: str) -> bool:
        """True only when CCAPI GetResource definitively reports the resource gone.

        Any other outcome — it exists, CCAPI does not support the type, or the check errs —
        returns False so the resource is kept (a real orphan must never be silently dropped).
        """
        if not identifier:
            return False
        try:
            return not ccm.resource_exists(Resource(type=rtype, identifier=identifier))
        except Exception:  # noqa: BLE001 — unsupported/transient/unknown -> keep, never mask
            return False
