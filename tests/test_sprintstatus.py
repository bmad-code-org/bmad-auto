import pytest
from conftest import write_sprint

from automator import sprintstatus


def test_load_classifies_keys(project):
    write_sprint(
        project,
        {
            "epic-1": "in-progress",
            "1-1-user-auth": "done",
            "1-2-account-mgmt": "ready-for-dev",
            "epic-1-retrospective": "optional",
            "epic-2": "backlog",
            "2-1-personality": "backlog",
            "epic-2-retrospective": "optional",
            "epic-1-retro-item-1-test-design": "done",
            "epic-2-retro-item-3-fts5-research": "backlog",
            "weird-key": "huh",
        },
    )
    ss = sprintstatus.load(project.sprint_status)
    assert ss.epics == {1: "in-progress", 2: "backlog"}
    assert [s.key for s in ss.stories] == [
        "1-1-user-auth",
        "1-2-account-mgmt",
        "2-1-personality",
    ]
    assert ss.stories[1].epic == 1 and ss.stories[1].num == 2
    assert ss.retros == {1: "optional", 2: "optional"}
    assert ss.unknown_keys == ("weird-key",)


def test_load_classifies_retro_items(project):
    write_sprint(
        project,
        {
            "epic-1-retrospective": "done",
            "epic-1-retro-item-1-test-design-in-stories": "done",
            "epic-5-retro-item-2-singleflight-inflight-guard-helper": "backlog",
        },
    )
    ss = sprintstatus.load(project.sprint_status)
    # retro action items are recognized, not dumped into unknown_keys
    assert ss.unknown_keys == ()
    assert ss.retros == {1: "done"}  # plain retrospective key is unaffected
    assert [(r.key, r.epic, r.num, r.slug, r.status) for r in ss.retro_items] == [
        ("epic-1-retro-item-1-test-design-in-stories", 1, 1, "test-design-in-stories", "done"),
        (
            "epic-5-retro-item-2-singleflight-inflight-guard-helper",
            5,
            2,
            "singleflight-inflight-guard-helper",
            "backlog",
        ),
    ]


def test_retro_items_do_not_become_actionable_stories(project):
    # recognition only: retro items must not leak into story selection
    write_sprint(project, {"epic-3-retro-item-1-do-a-thing": "backlog"})
    ss = sprintstatus.load(project.sprint_status)
    assert ss.stories == ()
    assert sprintstatus.next_actionable(ss) is None


def test_legacy_drafted_maps_to_ready(project):
    write_sprint(project, {"1-1-x": "drafted"})
    ss = sprintstatus.load(project.sprint_status)
    assert ss.stories[0].status == "ready-for-dev"


def test_next_actionable_order_and_skip(project):
    write_sprint(
        project,
        {"1-1-a": "done", "1-2-b": "ready-for-dev", "1-3-c": "backlog"},
    )
    ss = sprintstatus.load(project.sprint_status)
    assert sprintstatus.next_actionable(ss).key == "1-2-b"
    assert sprintstatus.next_actionable(ss, skip={"1-2-b"}).key == "1-3-c"
    assert sprintstatus.next_actionable(ss, skip={"1-2-b", "1-3-c"}) is None


def test_story_status_reread(project):
    write_sprint(project, {"1-1-a": "in-progress"})
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "in-progress"
    assert sprintstatus.story_status(project.sprint_status, "9-9-z") is None


def test_missing_file_raises(project):
    with pytest.raises(sprintstatus.SprintStatusError, match="not found"):
        sprintstatus.load(project.sprint_status)


def test_malformed_yaml_raises(project):
    project.sprint_status.write_text("development_status: [unclosed")
    with pytest.raises(sprintstatus.SprintStatusError, match="not valid YAML"):
        sprintstatus.load(project.sprint_status)


def test_missing_map_raises(project):
    project.sprint_status.write_text("project: x\n")
    with pytest.raises(sprintstatus.SprintStatusError, match="development_status"):
        sprintstatus.load(project.sprint_status)
