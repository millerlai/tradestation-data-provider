from tradestation_data.domain.bar import Bar

# Deliberately only Bar: the domain is the value range of the wire, and the
# wire carries one shape.
# Anything the wire does not carry (orders, positions, ...) belongs to the
# consumer, not to a data provider. See docs/architecture.md §3.2.
__all__ = [
    "Bar",
]
