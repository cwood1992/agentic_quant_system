import os
import re
import yaml
from dotenv import load_dotenv

# Load .env file so ${VAR} references in config.yaml resolve correctly.
# Does nothing if .env is absent; does not override existing env vars.
load_dotenv()

ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def resolve_env_vars(config):
    """Recursively substitute ${VAR_NAME} patterns with os.environ values.

    Unset variables are left as the literal ${VAR_NAME} string (no crash).
    """
    if isinstance(config, dict):
        return {k: resolve_env_vars(v) for k, v in config.items()}
    if isinstance(config, list):
        return [resolve_env_vars(item) for item in config]
    if isinstance(config, str):
        return ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)), config
        )
    return config


def load_config(path="config.yaml"):
    """Read a YAML config file and resolve all ${VAR} references."""
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    return resolve_env_vars(raw)


def validate_config(config):
    """Validate config constraints. Raises ValueError on failure.

    Checks that enabled agents' capital_allocation_pct values sum to <= 1.0.
    """
    agents = config.get("agents")
    if not agents or not isinstance(agents, dict):
        return

    total = 0.0
    enabled_ids = []
    for agent_id, agent_cfg in agents.items():
        if not isinstance(agent_cfg, dict):
            continue
        if agent_cfg.get("enabled", False):
            pct = agent_cfg.get("capital_allocation_pct", 0.0)
            total += pct
            enabled_ids.append(agent_id)

    if total > 1.0:
        raise ValueError(
            f"Enabled agents' capital_allocation_pct sum is {total:.2f} (> 1.0). "
            f"Enabled agents: {enabled_ids}"
        )
