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


def test_scm_defaults_reproduce_today(tmp_path):
    pol = policy.load(None)
    assert pol.scm.isolation == "none"
    assert pol.scm.branch_per == "story"
    assert pol.scm.target_branch == ""
    assert pol.scm.merge_strategy == "merge"
    assert pol.scm.delete_branch is True
    assert pol.scm.keep_failed is True
    assert pol.scm.failed_diff_max_mb == 5
    assert pol.scm.failed_diff_unlimited is False
    assert pol.scm.commit_message_template == ""
    assert pol.scm.max_parallel == 1
    # worktree config-seeding is on by default with no extra paths
    assert pol.scm.seed_adapter_defaults is True
    assert pol.scm.worktree_seed == ()


def test_scm_worktree_seed_settings(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        "[scm]\nseed_adapter_defaults = false\n" 'worktree_seed = [".mcp.json", ".envrc"]\n'
    )
    pol = policy.load(p)
    assert pol.scm.seed_adapter_defaults is False
    assert pol.scm.worktree_seed == (".mcp.json", ".envrc")


def test_scm_override(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        '[scm]\nisolation = "worktree"\nbranch_per = "story"\n'
        'target_branch = "integration"\nmerge_strategy = "squash"\n'
        "delete_branch = false\nkeep_failed = false\n"
        'commit_message_template = "feat: {story_key} ({run_id})"\n'
    )
    pol = policy.load(p)
    assert pol.scm.isolation == "worktree"
    assert pol.scm.branch_per == "story"
    assert pol.scm.target_branch == "integration"
    assert pol.scm.merge_strategy == "squash"
    assert pol.scm.delete_branch is False
    assert pol.scm.keep_failed is False
    assert pol.scm.commit_message_template == "feat: {story_key} ({run_id})"


def test_scm_branch_per_run_forces_delete_branch_off(tmp_path):
    # branch_per="run" shares one branch across the run; deleting it after each
    # merge would defeat that, so delete_branch is coerced off even if set true.
    p = tmp_path / "policy.toml"
    p.write_text('[scm]\nbranch_per = "run"\ndelete_branch = true\n')
    assert policy.load(p).scm.delete_branch is False


def test_scm_max_parallel_clamped_to_one(tmp_path):
    # Parallel fan-out (Phase 5) is unbuilt: the knob is accepted and validated
    # but any value > 1 is clamped to 1 so it stays inert.
    p = tmp_path / "policy.toml"
    p.write_text("[scm]\nmax_parallel = 4\n")
    assert policy.load(p).scm.max_parallel == 1
    p.write_text("[scm]\nmax_parallel = 0\n")
    with pytest.raises(policy.PolicyError, match="scm.max_parallel"):
        policy.load(p)


def test_scm_failed_diff_settings(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[scm]\nfailed_diff_max_mb = 25\nfailed_diff_unlimited = true\n")
    pol = policy.load(p)
    assert pol.scm.failed_diff_max_mb == 25
    assert pol.scm.failed_diff_unlimited is True
    # the cap must be a positive size
    p.write_text("[scm]\nfailed_diff_max_mb = 0\n")
    with pytest.raises(policy.PolicyError, match="scm.failed_diff_max_mb"):
        policy.load(p)


def test_scm_invalid_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[scm]\nisolation = "vm"\n')
    with pytest.raises(policy.PolicyError, match="scm.isolation"):
        policy.load(p)
    p.write_text('[scm]\nbranch_per = "epic"\n')
    with pytest.raises(policy.PolicyError, match="scm.branch_per"):
        policy.load(p)
    p.write_text('[scm]\nmerge_strategy = "rebase"\n')
    with pytest.raises(policy.PolicyError, match="scm.merge_strategy"):
        policy.load(p)


def test_engine_defaults_and_disabled():
    pol = policy.load(None)
    assert pol.engine.name == ""  # disabled by default
    assert pol.engine.editor_mode == "shared"
    assert pol.engine.mcp == "ivanmurzak"
    assert pol.engine.ready_timeout_sec == 600
    assert pol.engine.ready_grace_sec == -1  # auto (per-mode default in the plugin)


def test_engine_load_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[engine]
name = "unity"
editor_mode = "shared"
mcp = "coplaydev"
unity_path = "/opt/Unity/Editor/Unity"
ready_timeout_sec = 120
ready_grace_sec = 90
""")
    eng = policy.load(p).engine
    assert eng.name == "unity"
    assert eng.mcp == "coplaydev"
    assert eng.unity_path == "/opt/Unity/Editor/Unity"
    assert eng.ready_timeout_sec == 120
    assert eng.ready_grace_sec == 90


def test_engine_editor_mode_validated(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[engine]\nname = "unity"\neditor_mode = "live"\n')
    with pytest.raises(policy.PolicyError, match="engine.editor_mode"):
        policy.load(p)


def test_shared_mode_rejects_worktree_isolation(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        '[engine]\nname = "unity"\neditor_mode = "shared"\n[scm]\nisolation = "worktree"\n'
    )
    with pytest.raises(policy.PolicyError, match="shared.*requires scm.isolation = 'none'"):
        policy.load(p)


def test_per_worktree_requires_worktree_isolation(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[engine]\nname = "unity"\neditor_mode = "per_worktree"\n')
    with pytest.raises(policy.PolicyError, match="per_worktree.*requires scm.isolation"):
        policy.load(p)


def test_engine_constraints_skipped_when_disabled(tmp_path):
    # name = "" leaves the editor_mode/isolation coupling unenforced
    p = tmp_path / "policy.toml"
    p.write_text('[engine]\neditor_mode = "shared"\n[scm]\nisolation = "worktree"\n')
    assert policy.load(p).engine.name == ""


def test_template_parses():
    import tomllib

    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["gates"]["mode"] == "per-epic"
    assert doc["review"]["enabled"] is True
    assert doc["scm"]["isolation"] == "none"
    assert doc["engine"]["name"] == ""
    assert doc["engine"]["editor_mode"] == "shared"


def test_to_dict_roundtrips_for_snapshot():
    pol = policy.load(None)
    snapshot = pol.to_dict()
    assert snapshot["limits"]["max_review_cycles"] == 3
