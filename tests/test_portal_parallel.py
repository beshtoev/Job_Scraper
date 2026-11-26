import json, threading, time
import portal_scraper as ps


class Args:
    company = None; ats = None; limit = None; no_delay = True


def test_parallel_pass_keeps_inventory_order_and_lever_is_sequential(tmp_path, monkeypatch):
    companies = [{"company": f"C{i}", "ats": "lever" if i % 3 == 0 else "greenhouse", "enabled": True,
                  "careers_url": f"https://x/{i}"} for i in range(12)]
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"companies": companies, "aggregators": {}}))
    monkeypatch.setattr(ps, "INVENTORY_PATH", inv)
    active = {"lever": 0, "max_lever": 0}
    lock = threading.Lock()

    def fake(entry, blocked):
        if entry["ats"] == "lever":
            with lock:
                active["lever"] += 1
                active["max_lever"] = max(active["max_lever"], active["lever"])
        time.sleep(0.01)
        if entry["ats"] == "lever":
            with lock:
                active["lever"] -= 1
        if entry["company"] == "C4":
            raise ps.SourceFailure("boom")
        return [{"company": entry["company"], "title": "Director", "url": entry["careers_url"]}]

    monkeypatch.setattr(ps, "fetch_company", fake)
    monkeypatch.setenv("PORTAL_WORKERS", "6")
    jobs, report = ps.run(Args())
    assert [s["company"] for s in report["sources"]] == [c["company"] for c in companies]
    assert [j["company"] for j in jobs] == [c["company"] for c in companies if c["company"] != "C4"]
    assert report["status_counts"] == {"succeeded": 11, "failed": 1}
    assert active["max_lever"] == 1
