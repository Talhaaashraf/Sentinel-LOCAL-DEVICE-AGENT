"""Unit tests for the diagnostic/agentic additions. Run with: pytest backend/tests"""

import os
import tempfile

import pytest

# Point the SQLite stores at a throwaway file before importing the app modules.
os.environ.setdefault("ALLOW_REMOTE_SHELL", "false")


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    from backend import device_store
    db = tmp_path / "test.db"
    monkeypatch.setattr(device_store, "DB_PATH", db)
    yield


def test_typo_correction():
    from backend.typos import normalize
    corrected, changes = normalize("dignose browser useing alot of ram on my lpatop")
    assert "diagnose" in corrected and "laptop" in corrected and "a lot" in corrected
    assert any(w == "dignose" for w, _ in changes)


def test_classify_and_app_guess():
    from backend import playbooks
    cats, corrected, _ = playbooks.classify("cant uninstal McAfee not showing in contol pannel")
    assert cats[0] == "uninstall"
    assert "control panel" in corrected
    assert "McAfee" in playbooks.guess_app_name("cannot uninstall McAfee please")


def test_tool_policy_read_vs_change():
    from backend import tool_policy
    _, _, needs = tool_policy.check("top_processes", {"sort": "cpu"}, "windows")
    assert needs is False
    _, _, needs = tool_policy.check("kill_process", {"pid": 10}, "windows")
    assert needs is True


def test_tool_policy_rejects_unknown_and_shell(monkeypatch):
    from backend import tool_policy
    with pytest.raises(tool_policy.ToolValidationError):
        tool_policy.check("does_not_exist", {}, "linux")
    monkeypatch.setenv("ALLOW_REMOTE_SHELL", "false")
    with pytest.raises(tool_policy.ToolValidationError):
        tool_policy.check("run_shell", {"command": "x"}, "linux")


def test_tool_policy_validates_args():
    from backend import tool_policy
    with pytest.raises(tool_policy.ToolValidationError):
        tool_policy.check("top_processes", {"sort": "banana"}, "linux")  # not in enum


def test_command_store_approval_lifecycle():
    from backend import command_store
    cmd = command_store.create_command("dev1", "kill_process", "change", {"pid": 5}, "tech", needs_approval=True)
    assert cmd["status"] == "pending_approval"
    assert command_store.claim_next("dev1") == []  # not handed out before approval
    assert command_store.approve(cmd["id"]) is True
    claimed = command_store.claim_next("dev1")
    assert claimed and claimed[0]["id"] == cmd["id"]
    assert command_store.record_result(cmd["id"], "dev1", "done", result={"killed": ["x"]})
    assert command_store.get_command(cmd["id"])["status"] == "done"


def test_command_store_read_tool_skips_approval():
    from backend import command_store
    cmd = command_store.create_command("dev2", "disk_usage", "read", {}, "ai", needs_approval=False)
    assert cmd["status"] == "queued"
    assert command_store.claim_next("dev2")[0]["tool"] == "disk_usage"


def test_health_score():
    from backend import health_score
    good = health_score.score({"cpu": {"total_usage_percent": 5}, "memory": {"ram": {"usage_percent": 20}}, "disk": []})
    assert good["score"] >= 90 and good["grade"] == "good"
    bad = health_score.score({"cpu": {"total_usage_percent": 95}, "memory": {"ram": {"usage_percent": 95}},
                              "disk": [{"usage_percent": 97}], "security": {"firewall": {"enabled": False}}})
    assert bad["score"] < 60


def test_playbook_analyze_browser_and_disk():
    from backend import playbooks
    results = {
        "browser_memory": {"ram_total": "16 GB", "browsers": [
            {"browser": "Google Chrome", "memory": "5.0 GB", "memory_bytes": 5 * 1024**3, "percent_of_ram": 31,
             "process_count": 20, "approx_tabs": 15, "extension_processes": 4}]},
        "disk_usage": {"volumes": [{"mountpoint": "C:", "percent": 95, "free": "10 GB"}]},
    }
    analysis = playbooks.analyze(results)
    titles = " ".join(f["title"] for f in analysis["findings"])
    assert "Chrome" in titles and "95%" in titles
    assert any(fix["tool"] == "kill_process" for fix in analysis["fixes"])


def test_decision_memory_roundtrip_and_search(monkeypatch, tmp_path):
    from backend import decision_memory
    monkeypatch.setattr(decision_memory, "CASES_DIR", tmp_path / "cases")
    decision_memory.save_case("chrome using lots of ram and hanging", "browser_ram",
                              "Too many Chrome tabs", [{"severity": "high", "title": "Chrome 5GB", "detail": ""}],
                              [], "fixed", device={"hostname": "pc1", "os_type": "Windows"})
    hits = decision_memory.search("chrome ram high", os_type="Windows")
    assert hits and hits[0]["outcome"] == "fixed"
    assert (tmp_path / "cases").exists()


def test_agent_loop_rule_mode(monkeypatch):
    """With no LLM, a session runs read tools via a fake dispatcher and produces findings."""
    from backend import agent_loop, llm_provider
    monkeypatch.setattr(llm_provider, "is_available", lambda: False)

    def fake_dispatch(device_id, tool, kind, args, session_id):
        data = {
            "disk_usage": {"volumes": [{"mountpoint": "C:", "percent": 93, "free": "8 GB"}]},
            "top_processes": {"ram_used_percent": 50, "processes": []},
        }.get(tool, {})
        return {"status": "done", "result": data}

    device = {"device_id": "dev9", "hostname": "pc9", "os_type": "windows"}
    session_id = agent_loop.start_session(device, "unusual storgae spike on C drive", fake_dispatch)
    import time
    for _ in range(50):
        session = agent_loop.get_session(session_id)
        if session["status"] in ("awaiting_decision", "error", "closed"):
            break
        time.sleep(0.1)
    session = agent_loop.get_session(session_id)
    assert session["status"] == "awaiting_decision"
    assert session["corrected_problem"].startswith("unusual storage spike")
    assert any("93%" in f["title"] for f in session["findings"])


def test_agent_loop_llm_mode(monkeypatch):
    """LLM branch: first reply calls a read tool, second reply concludes."""
    from backend import agent_loop, llm_provider
    monkeypatch.setattr(llm_provider, "is_available", lambda: True)
    monkeypatch.setattr(llm_provider, "provider_name", lambda: "ollama")

    calls = {"n": 0}

    def fake_chat(messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "name": "disk_usage", "arguments": {}}]}
        return {"content": "Root cause: disk nearly full. Fix: 1) clean_junk.", "tool_calls": []}

    monkeypatch.setattr(llm_provider, "chat", fake_chat)

    def fake_dispatch(device_id, tool, kind, args, session_id):
        return {"status": "done", "result": {"volumes": [{"mountpoint": "C:", "percent": 96, "free": "4 GB"}]}}

    device = {"device_id": "devL", "hostname": "pcL", "os_type": "windows"}
    session_id = agent_loop.start_session(device, "disk almost full", fake_dispatch)
    import time
    for _ in range(50):
        if agent_loop.get_session(session_id)["status"] != "running":
            break
        time.sleep(0.1)
    session = agent_loop.get_session(session_id)
    assert session["status"] == "awaiting_decision"
    assert "disk" in (session["conclusion"] or "").lower()
    assert any(step["tool"] == "disk_usage" for step in session["steps"] if step["kind"] == "tool_result")


def test_enable_startup_is_change_tool():
    from backend import tool_policy
    _, _, needs = tool_policy.check("enable_startup_item", {"item_id": "x"}, "linux")
    assert needs is True
    _, _, needs = tool_policy.check("list_backups", {}, "linux")
    assert needs is False


def test_backup_manifest_record_list_restore(tmp_path):
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "agent"))
    import tools_common as c, tools_unix as u
    bd = str(tmp_path / "bk")
    (tmp_path / "App").mkdir()
    import shutil
    shutil.move(str(tmp_path / "App"), str(tmp_path / "App.q"))
    entry = c.record_backup(bd, "quarantine_folder", original=str(tmp_path / "App"), backup=str(tmp_path / "App.q"), app="X")
    listed = c.list_backups(backup_dir=bd)
    assert listed["restorable_count"] == 1 and listed["backups"][0]["id"] == entry["id"]
    result = u.restore_backup(entry_id=entry["id"], backup_dir=bd)
    assert result["restored"] and (tmp_path / "App").exists()
    assert c.list_backups(backup_dir=bd)["backups"][0]["restored"] is True


def test_fleet_allowlist_rejects_unsafe():
    from backend import diagnostics_api
    assert "clean_junk" in diagnostics_api.FLEET_ALLOWED_TOOLS
    assert "kill_process" not in diagnostics_api.FLEET_ALLOWED_TOOLS
    assert "uninstall_app" not in diagnostics_api.FLEET_ALLOWED_TOOLS


def test_schedule_crud_and_due(monkeypatch, tmp_path):
    from backend import device_store, schedules
    monkeypatch.setattr(device_store, "DB_PATH", tmp_path / "s.db")
    s = schedules.create("devX", "cpu", 5)
    assert s["enabled"] and s["interval_minutes"] == 5
    assert any(x["id"] == s["id"] for x in schedules.list_all("devX"))
    # Not due yet (next_run is in the future), but due() with a far-future clock returns it.
    from datetime import timedelta
    assert schedules.due() == [] or all(d["id"] != s["id"] for d in schedules.due())
    future = schedules.utc_now() + timedelta(minutes=10)
    assert any(d["id"] == s["id"] for d in schedules.due(now=future))
    schedules.mark_ran(s["id"], "ok")
    assert schedules.get(s["id"])["last_result"] == "ok"
    assert schedules.delete(s["id"]) is True


def test_metrics_history_record_and_downsample(monkeypatch, tmp_path):
    from backend import device_store, metrics_history
    monkeypatch.setattr(device_store, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(metrics_history, "MIN_SAMPLE_GAP_SECONDS", 0)
    for i in range(5):
        metrics_history.record_report("devH", {"cpu": {"total_usage_percent": 10 + i},
                                               "memory": {"ram": {"usage_percent": 40}},
                                               "disk": [{"usage_percent": 55}]})
    h = metrics_history.history("devH", hours=24)
    assert h["points"] == 5
    assert len(h["series"]["cpu"]) >= 1 and h["series"]["ram"][0] == 40
    metrics_history.record_stress("devH", "stress_cpu", {"avg_cpu_percent": 90, "max_temp_c": 70, "throttling_suspected": False})
    runs = metrics_history.stress_runs("devH")
    assert runs and runs[0]["test"] == "stress_cpu" and "CPU" in runs[0]["summary"]
