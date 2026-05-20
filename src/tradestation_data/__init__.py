"""tradestation_data — TradeStation EasyLanguage data ingestion with pluggable sinks."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tradestation-data-provider")
except PackageNotFoundError:  # pragma: no cover
    # Package metadata is missing only when running from a source tree
    # that was never installed (e.g. unpacked tarball without `pip
    # install`). Fall back to a sentinel so importing the package
    # never crashes in that edge case.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
