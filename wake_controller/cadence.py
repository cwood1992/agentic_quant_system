"""Cadence computation and modifier evaluation for agent wake scheduling.

Computes effective cadence by applying conditional modifiers to a base cadence,
then clamping the result to the allowed [1, 24] hour range.
"""

import operator
import re

from logging_config import get_logger
from risk.limits import MAXIMUM_WAKE_CADENCE_HOURS, MINIMUM_WAKE_CADENCE_HOURS

logger = get_logger("wake_controller.cadence")

# Supported comparison operators for modifier conditions
_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}

# Pattern: "variable_name >= 42.5"
_CONDITION_RE = re.compile(
    r"^\s*(\w+)\s*(>=|<=|>|<|==)\s*(-?\d+(?:\.\d+)?)\s*$"
)


def evaluate_modifiers(modifiers: list[dict], conditions: dict) -> float:
    """Evaluate a list of conditional modifiers against current conditions.

    Each modifier dict has the form::

        {"condition": "volatility_score > 70", "multiplier": 0.5}

    The condition is a simple comparison: ``<variable> <op> <number>``.
    Only modifiers whose condition evaluates to True contribute; the
    return value is the product of all matching multipliers.

    Args:
        modifiers: List of modifier dicts with ``condition`` and ``multiplier``.
        conditions: Dict mapping variable names to their current numeric values
            (e.g. ``{"volatility_score": 85}``).

    Returns:
        Product of all matching modifier multipliers.  Returns 1.0 when no
        modifiers match or the list is empty.
    """
    product = 1.0

    for mod in modifiers:
        condition_str = mod.get("condition", "")
        multiplier = mod.get("multiplier", 1.0)

        match = _CONDITION_RE.match(condition_str)
        if match is None:
            logger.warning("Unparseable modifier condition: %r", condition_str)
            continue

        var_name = match.group(1)
        op_str = match.group(2)
        threshold = float(match.group(3))

        current_value = conditions.get(var_name)
        if current_value is None:
            # Variable not present in conditions — skip this modifier
            continue

        op_func = _OPS[op_str]
        if op_func(float(current_value), threshold):
            product *= multiplier

    return product


def compute_effective_cadence(
    agent_id: str,
    base_cadence_hours: float,
    modifiers: list[dict],
    current_conditions: dict,
) -> float:
    """Compute the effective cadence for an agent after applying modifiers.

    The base cadence is multiplied by the product of all matching modifier
    multipliers, then clamped to ``[MINIMUM_WAKE_CADENCE_HOURS,
    MAXIMUM_WAKE_CADENCE_HOURS]``.

    Args:
        agent_id: Agent identifier (for logging).
        base_cadence_hours: The agent's configured base cadence in hours.
        modifiers: List of conditional modifier dicts.
        current_conditions: Current market/system conditions as a dict of
            variable names to numeric values.

    Returns:
        Effective cadence in hours, clamped to [1, 24].
    """
    modifier_product = evaluate_modifiers(modifiers, current_conditions)
    raw = base_cadence_hours * modifier_product

    clamped = max(MINIMUM_WAKE_CADENCE_HOURS, min(MAXIMUM_WAKE_CADENCE_HOURS, raw))

    if clamped != raw:
        logger.info(
            "Agent %s cadence clamped: raw=%.2fh -> clamped=%.2fh",
            agent_id, raw, clamped,
        )

    logger.debug(
        "Agent %s effective cadence: base=%.2fh * modifier=%.4f = %.2fh",
        agent_id, base_cadence_hours, modifier_product, clamped,
    )

    return clamped
