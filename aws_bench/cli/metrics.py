"""Parse ``--metric`` CLI values into MetricConfigs.

``parse_kwargs`` only handles ``key=value`` tokens, so it cannot parse a bare
metric type (``mean``) or the ``type:k=v`` shape. This helper splits the type
from the optional kwargs tail and reuses ``parse_kwargs`` for the tail only.
"""

from harbor.cli.utils import parse_kwargs
from harbor.models.metric.config import MetricConfig
from harbor.models.metric.type import MetricType


def parse_metric(value: str) -> MetricConfig:
    """Parse ``type`` or ``type:k=v,k2=v2`` into a MetricConfig.

    Raises ValueError on an unknown metric type (via MetricType(...)).
    """
    type_part, _, kw_part = value.partition(":")
    metric_type = MetricType(type_part.strip())
    kwargs = parse_kwargs(kw_part.split(",")) if kw_part else {}
    return MetricConfig(type=metric_type, kwargs=kwargs)
