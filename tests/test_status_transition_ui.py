from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_blocking_is_not_presented_as_a_workflow_status():
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert '<option value="blocked"' not in template
    assert '<option value="waiting_person"' not in template
    assert 'class="waiting-form"' in template
    assert 'class="inline-form add-blocker-form"' in template
    assert 'row.dataset.blocked = String(Boolean(task.blocked));' in script
    assert 'statuses.has("blocked") && row.dataset.blocked === "true"' in script


def test_follow_up_actions_make_the_still_waiting_state_and_dialog_clear():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert template.count("Followed up, still waiting…") == 2
    assert '>Followed up, still waiting</h2>' in template
    assert '>Context <span class="optional">(optional)</span></label>' in template
    assert '>Next follow-up</label>' in template
    assert '>Exact time</label>' in template
    assert '>Cancel</button>' in template
    assert '>Save follow-up</button>' in template
