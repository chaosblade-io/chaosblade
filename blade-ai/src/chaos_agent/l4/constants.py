"""Shared L4 adapter protocol constants."""

# Upper layers normally resolve a card within seconds; keep a generous upper
# bound before the adapter fails closed.
DEFAULT_CARD_DECISION_TIMEOUT_S: float = 600.0
