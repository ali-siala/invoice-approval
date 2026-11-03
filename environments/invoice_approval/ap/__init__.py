"""Alpenwerk accounts-payable app: SQLite backend, seed data, policy and agent tools."""

from pathlib import Path

POLICY_PATH = Path(__file__).with_name("policy.md")


def load_policy() -> str:
    """Return the Kreditorenrichtlinie as the agent reads it."""
    return POLICY_PATH.read_text(encoding="utf-8")
