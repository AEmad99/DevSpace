"""Pin the `agent_edit_verify_enforce` setting.

The auto-approve-by-default agent design depends on this setting being
off out of the box:

  * Default value MUST be False (opt-in to the verify enforcement, not
    opt-out). Flipping the default would re-introduce the
    "agent refuses to edit" bug for every fresh install.
  * The setting MUST be listed in DEFAULT_SETTINGS so the loader materialises
    it on a missing-key read (a missing key would force a `None` fallback
    path that the loop would have to special-case).
  * The default must NOT be 'true' / '1' / 1 — a string "true" would be
    truthy but signals the wrong type to a future maintainer.
"""
from src.settings import DEFAULT_SETTINGS


def test_agent_edit_verify_enforce_setting_exists():
    assert "agent_edit_verify_enforce" in DEFAULT_SETTINGS, (
        "DEFAULT_SETTINGS must include agent_edit_verify_enforce so a "
        "missing key in data/settings.json is materialised to the safe "
        "default (False) at read time."
    )


def test_agent_edit_verify_enforce_default_is_false():
    val = DEFAULT_SETTINGS.get("agent_edit_verify_enforce")
    assert val is False, (
        f"agent_edit_verify_enforce default must be False (auto-approve "
        f"by default). Got {val!r}."
    )


def test_agent_edit_verify_enforce_default_is_strict_bool():
    val = DEFAULT_SETTINGS.get("agent_edit_verify_enforce")
    assert isinstance(val, bool), (
        f"agent_edit_verify_enforce default must be a real bool, not a "
        f"truthy string/int. Got {type(val).__name__} ({val!r})."
    )


def test_agent_edit_verify_enforce_does_not_default_true_via_string():
    # Belt-and-suspenders: a string "false" / "False" / "0" is truthy in
    # Python and would silently flip the default on. A regression that
    # swapped the literal `False` for a string would break this test.
    val = DEFAULT_SETTINGS.get("agent_edit_verify_enforce")
    assert val != "false" and val != "False" and val != "0", (
        "agent_edit_verify_enforce default must be the literal `False`, "
        "not a string that happens to look false-y."
    )
