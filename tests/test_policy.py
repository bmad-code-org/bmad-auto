import pytest

from automator import policy


def test_defaults_when_file_missing(tmp_path):
    pol = policy.load(tmp_path / "nope.toml")
    assert pol.gates.mode == "per-epic"
    assert pol.limits.max_review_cycles == 3
    assert pol.adapter.name == "claude"
    assert pol.adapter.extra_args is None  # None = use the profile's bypass flags


def test_cleanup_session_on_finish_default_and_override(tmp_path):
    assert policy.load(None).adapter.cleanup_session_on_finish is True
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
cleanup_session_on_finish = false
""")
    assert policy.load(p).adapter.cleanup_session_on_finish is False


def test_load_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[gates]
mode = "none"
[limits]
max_review_cycles = 5
[verify]
commands = ["pytest -q"]
[adapter]
model = "haiku"
extra_args = ["--permission-mode", "plan"]
""")
    pol = policy.load(p)
    assert pol.gates.mode == "none"
    assert pol.limits.max_review_cycles == 5
    assert pol.limits.max_dev_attempts == 2  # default survives partial table
    assert pol.verify.commands == ("pytest -q",)
    assert pol.adapter.model == "haiku"
    assert pol.adapter.extra_args == ("--permission-mode", "plan")
    # no stage tables: both roles resolve to the base
    assert pol.adapter.resolved("dev") == policy.ResolvedAdapter(
        "claude", "haiku", ("--permission-mode", "plan")
    )
    assert pol.adapter.resolved("review").model == "haiku"


def test_stage_overrides_and_inheritance(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
name = "claude"
model = "opus"
extra_args = ["--permission-mode", "plan"]
[adapter.review]
name = "codex"
model = "gpt-5-codex"
""")
    pol = policy.load(p)
    dev = pol.adapter.resolved("dev")
    assert dev == policy.ResolvedAdapter("claude", "opus", ("--permission-mode", "plan"))
    review = pol.adapter.resolved("review")
    assert review.name == "codex"
    assert review.model == "gpt-5-codex"
    # client switch: claude-specific extra_args must not leak into codex
    assert review.extra_args is None


def test_stage_client_switch_drops_base_model_and_extra_args(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
name = "claude"
model = "opus"
extra_args = ["--permission-mode", "plan"]
[adapter.review]
name = "codex"
""")
    review = policy.load(p).adapter.resolved("review")
    assert review == policy.ResolvedAdapter("codex", "", None)


def test_stage_same_client_inherits_and_overrides(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
model = "opus"
[adapter.dev]
model = ""
[adapter.review]
extra_args = ["--foo"]
""")
    pol = policy.load(p)
    # explicit empty model in the stage table means "CLI default", beating the base
    assert pol.adapter.resolved("dev") == policy.ResolvedAdapter("claude", "", None)
    assert pol.adapter.resolved("review") == policy.ResolvedAdapter("claude", "opus", ("--foo",))


def test_unknown_role_resolves_to_base(tmp_path):
    pol = policy.load(None)
    assert pol.adapter.resolved("retro") == policy.ResolvedAdapter("claude", "", None)


def test_legacy_model_keys_rejected(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[adapter]\nmodel_dev = "haiku"\n')
    with pytest.raises(policy.PolicyError, match=r"adapter\.model_dev"):
        policy.load(p)
    p.write_text('[adapter]\nmodel_review = "haiku"\n')
    with pytest.raises(policy.PolicyError, match=r"adapter\.model_review"):
        policy.load(p)


def test_stage_scalar_rejected(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[adapter]\ndev = "opus"\n')
    with pytest.raises(policy.PolicyError, match=r"\[adapter\.dev\] must be a table"):
        policy.load(p)


def test_invalid_gate_mode(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[gates]\nmode = "sometimes"\n')
    with pytest.raises(policy.PolicyError, match="gates.mode"):
        policy.load(p)


def test_bad_toml(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[gates\nmode=")
    with pytest.raises(policy.PolicyError, match="invalid policy TOML"):
        policy.load(p)


def test_loads_defaults_and_text():
    assert policy.loads("").gates.mode == policy.GatesPolicy.mode
    assert policy.loads('[gates]\nmode = "none"\n').gates.mode == "none"


def test_loads_validates():
    with pytest.raises(policy.PolicyError, match="gates.mode"):
        policy.loads('[gates]\nmode = "sometimes"\n')


def test_load_prefixes_path_in_errors(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[gates]\nmode = "sometimes"\n')
    with pytest.raises(policy.PolicyError, match=r"policy\.toml.*gates\.mode"):
        policy.load(p)


def test_zero_budget_rejected(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[limits]\nmax_dev_attempts = 0\n")
    with pytest.raises(policy.PolicyError):
        policy.load(p)


def test_cache_read_weight_default_and_override(tmp_path):
    assert policy.load(None).limits.cache_read_weight == 0.1
    p = tmp_path / "policy.toml"
    p.write_text("[limits]\ncache_read_weight = 1.0\n")
    assert policy.load(p).limits.cache_read_weight == 1.0
    p.write_text("[limits]\ncache_read_weight = 1.5\n")
    with pytest.raises(policy.PolicyError, match="cache_read_weight"):
        policy.load(p)


def test_sweep_defaults_and_override(tmp_path):
    pol = policy.load(None)
    assert pol.sweep.auto == "never"
    assert pol.sweep.max_bundles == 5
    assert pol.sweep.max_triage_attempts == 2
    assert pol.sweep.repeat is False
    assert pol.sweep.max_cycles == 5
    p = tmp_path / "policy.toml"
    p.write_text('[sweep]\nauto = "run-end"\nmax_bundles = 2\nrepeat = true\nmax_cycles = 3\n')
    pol = policy.load(p)
    assert pol.sweep.auto == "run-end"
    assert pol.sweep.max_bundles == 2
    assert pol.sweep.repeat is True
    assert pol.sweep.max_cycles == 3


def test_sweep_invalid_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[sweep]\nauto = "always"\n')
    with pytest.raises(policy.PolicyError, match="sweep.auto"):
        policy.load(p)
    p.write_text("[sweep]\nmax_bundles = 0\n")
    with pytest.raises(policy.PolicyError, match="max_bundles"):
        policy.load(p)
    p.write_text("[sweep]\nmax_cycles = 0\n")
    with pytest.raises(policy.PolicyError, match="max_cycles"):
        policy.load(p)


def test_triage_stage_adapter(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[adapter]\nmodel = "opus"\n[adapter.triage]\nmodel = "sonnet"\n')
    pol = policy.load(p)
    assert pol.adapter.resolved("triage").model == "sonnet"
    assert pol.adapter.resolved("dev").model == "opus"
    # without a stage table, triage inherits the base
    assert policy.load(None).adapter.resolved("triage") == policy.ResolvedAdapter(
        "claude", "", None
    )


def test_triage_client_switch_uses_profile_defaults(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        '[adapter]\nmodel = "opus"\nextra_args = ["--foo"]\n[adapter.triage]\nname = "gemini"\n'
    )
    pol = policy.load(p)
    # base model/extra_args are client-specific and must not follow a client switch
    assert pol.adapter.resolved("triage") == policy.ResolvedAdapter("gemini", "", None)
    assert pol.adapter.resolved("dev") == policy.ResolvedAdapter("claude", "opus", ("--foo",))


def test_review_enabled_default_and_override(tmp_path):
    assert policy.load(None).review.enabled is True
    p = tmp_path / "policy.toml"
    p.write_text("[review]\nenabled = false\n")
    assert policy.load(p).review.enabled is False


def test_template_parses():
    import tomllib

    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["gates"]["mode"] == "per-epic"
    assert doc["review"]["enabled"] is True


def test_to_dict_roundtrips_for_snapshot():
    pol = policy.load(None)
    snapshot = pol.to_dict()
    assert snapshot["limits"]["max_review_cycles"] == 3
