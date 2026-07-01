"""Тесты новых дашборд-API эндпоинтов: /api/reads, /api/inbox (GET+POST),
/api/contacts/birthdays. Сторы — реальные SQLite на tmp_path; memory/llm/facts не
нужны (эти маршруты их не трогают), передаём None.
"""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from bills import BillStore
from contacts import ContactStore
from inbox import InboxStore
from reads import ReadStore
from tasks import TaskStore
from web.server import create_app


def _client(tmp_path):
    tasks = TaskStore(tmp_path / "t.db")
    bills = BillStore(tmp_path / "b.db")
    inbox = InboxStore(tmp_path / "i.db")
    contacts = ContactStore(tmp_path / "c.db")
    reads = ReadStore(tmp_path / "r.db")
    app = create_app(None, None, None, tasks, bills, None, inbox, contacts, reads)  # type: ignore[arg-type]
    return TestClient(app), inbox, contacts, reads, bills


def test_api_reads_filters_by_status(tmp_path):
    client, _, _, reads, _ = _client(tmp_path)
    reads.create(url="u1", title="A", summary="s", status="unread")
    reads.create(url="u2", title="B", summary="s2", status="read")
    data = client.get("/api/reads?status=unread").json()["reads"]
    assert [r["title"] for r in data] == ["A"]
    assert {"id", "url", "title", "summary", "status", "created_at"} <= set(data[0])


def test_api_inbox_get_pending_and_post_capture(tmp_path):
    client, inbox, _, _, _ = _client(tmp_path)
    inbox.create("старая мысль")
    assert len(client.get("/api/inbox").json()["items"]) == 1

    r = client.post("/api/inbox", json={"text": "новая идея"})
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["text"] == "новая идея" and item["source"] == "dashboard"
    assert len(inbox.list("pending")) == 2


def test_api_inbox_post_empty_rejected(tmp_path):
    client, *_ = _client(tmp_path)
    assert client.post("/api/inbox", json={"text": "   "}).status_code == 400


def test_api_contacts_birthdays(tmp_path):
    client, _, contacts, _, _ = _client(tmp_path)
    soon = (date.today() + timedelta(days=2)).replace(year=1990).isoformat()
    contacts.create(name="Мама", birthday=soon)
    contacts.create(name="Далеко", birthday="1980-12-31")
    bd = client.get("/api/contacts/birthdays?days=7").json()["birthdays"]
    assert [b["name"] for b in bd] == ["Мама"]
    assert bd[0]["in_days"] == 2


# --- /api/bills: единый список регулярных + разовых (§3-bis-2) --------------

def test_api_bills_combines_regular_and_one_time(tmp_path):
    client, _, _, _, bills = _client(tmp_path)
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    bills.create_one_time(name="кредит", due_date=date.today().replace(day=14).isoformat(), amount=14500)

    ym = date.today().strftime("%Y-%m")
    data = client.get(f"/api/bills?month={ym}").json()
    names = sorted(b["name"] for b in data["bills"])
    assert names == ["аренда", "кредит"]
    by_name = {b["name"]: b for b in data["bills"]}
    assert by_name["аренда"]["kind"] == "regular" and by_name["аренда"]["id"].startswith("r")
    assert by_name["кредит"]["kind"] == "one_time" and by_name["кредит"]["id"].startswith("o")


def test_api_bills_patch_marks_regular_paid(tmp_path):
    client, _, _, _, bills = _client(tmp_path)
    bills.create_template(name="аренда", day_of_month=5, amount=30000)
    ym = date.today().strftime("%Y-%m")
    bill_id = client.get(f"/api/bills?month={ym}").json()["bills"][0]["id"]

    r = client.patch(f"/api/bills/{bill_id}", json={"status": "paid"})
    assert r.status_code == 200
    assert r.json()["bill"]["status"] == "paid"


def test_api_bills_patch_marks_one_time_paid(tmp_path):
    client, _, _, _, bills = _client(tmp_path)
    bills.create_one_time(name="кредит", due_date=date.today().isoformat(), amount=14500)
    ym = date.today().strftime("%Y-%m")
    bill_id = client.get(f"/api/bills?month={ym}").json()["bills"][0]["id"]
    assert bill_id.startswith("o")

    r = client.patch(f"/api/bills/{bill_id}", json={"status": "paid"})
    assert r.status_code == 200
    assert r.json()["bill"]["status"] == "paid"


def test_api_bills_patch_unknown_id_404(tmp_path):
    client, *_ = _client(tmp_path)
    assert client.patch("/api/bills/o999", json={"status": "paid"}).status_code == 404


def test_api_bills_patch_malformed_id_404(tmp_path):
    client, *_ = _client(tmp_path)
    assert client.patch("/api/bills/xyz", json={"status": "paid"}).status_code == 404
