from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_blocked_status_transition_opens_blocker_setup():
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert '>Blocked…</option>' in template
    assert '["blocked", "waiting_person"].includes(value)' in script
    assert "control.value = previous;" in script
    assert "expandTaskDetails(row);" in script
    assert "row.querySelector('.add-blocker-form select[name=\"blocker_task_id\"]')" in script
