"""Shared constants for the fast-scan discovery Lambda: its identity, config, and timeouts.

Home for the values shared across the fast-scan modules — the Lambda's name/role/runtime and
the three nested timeouts — so they live in one place instead of scattered literals. Values used
only inside one module (per-function retry counts, the zip file manifest, IAM policy ARNs, wire
keys) stay with their logic.

Closure-safe (pure values, no third-party deps): the in-Lambda handler imports from here, so this
module ships in the deployment zip.
"""

from __future__ import annotations

# --- Discovery Lambda identity and configuration -----------------------------------------------

LAMBDA_FUNCTION_NAME = "awsbench-fastscan-discovery"
LAMBDA_ROLE_NAME = "awsbench-fastscan-discovery-role"
LAMBDA_HANDLER = "aws_bench.resource_management.fastscan.lambda_handler.handler"
LAMBDA_RUNTIME = "python3.12"
# 1 full vCPU is ~1769 MB; the sweep's wide thread pool does botocore response parsing (CPU
# work under the GIL), so it needs >= 1 vCPU, not 1024 MB (~0.57 vCPU).
LAMBDA_MEMORY_MB = 1769

# Assume-role credential lifetime for one scan. 900s is the STS minimum and outlives one invoke.
# The role is assumed unscoped (no session policy), matching the host scan path.
SCAN_ASSUME_ROLE_DURATION_S = 900

# --- Nested scan timeouts (ordered SCAN_SWEEP < LAMBDA_FUNCTION < HOST_INVOKE_READ) -------------
#
# One region's scan is bounded at three layers that must stay ordered:
#   SCAN_SWEEP_TIMEOUT_S        the lister sweep self-terminates and returns its partial result
#                              (pending listers recorded as failed).
#   LAMBDA_FUNCTION_TIMEOUT_S   the runtime must outlive the sweep long enough to serialize and
#                              return that result before the process is hard-killed.
#   HOST_INVOKE_READ_TIMEOUT_S  the host's invoke read timeout must outlive the Lambda run, or a
#                              healthy long scan is abandoned mid-flight (and keeps running
#                              server-side).
#
# Each outer layer is the previous plus a positive margin, so the ordering holds by construction.

SCAN_SWEEP_TIMEOUT_S = 420.0

# Seconds the Lambda runtime is given beyond the sweep budget to serialize and return the result.
# Private: only an input to LAMBDA_FUNCTION_TIMEOUT_S below, not consumed elsewhere.
_LAMBDA_SHUTDOWN_MARGIN_S = 60
LAMBDA_FUNCTION_TIMEOUT_S = int(SCAN_SWEEP_TIMEOUT_S) + _LAMBDA_SHUTDOWN_MARGIN_S

# Seconds the host invoke waits beyond the Lambda timeout so it never abandons a running scan.
# Private: only an input to HOST_INVOKE_READ_TIMEOUT_S below, not consumed elsewhere.
_INVOKE_ROUNDTRIP_MARGIN_S = 40
HOST_INVOKE_READ_TIMEOUT_S = LAMBDA_FUNCTION_TIMEOUT_S + _INVOKE_ROUNDTRIP_MARGIN_S
