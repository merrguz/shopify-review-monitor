"""Source registry. Add a new adapter here and the runner picks it up."""

from .base import Source, SourceHealth
from .g2 import G2Source
from .shopify import ShopifySource
from .trustpilot import TrustpilotSource

REGISTRY = {
    "shopify": ShopifySource(),
    "trustpilot": TrustpilotSource(),
    "g2": G2Source(),
}

__all__ = ["REGISTRY", "Source", "SourceHealth", "ShopifySource"]
