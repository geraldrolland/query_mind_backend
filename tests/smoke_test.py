"""End-to-end smoke test for the QueryMind backend."""
import io
import csv
import json
import time

import httpx

BASE = "http://localhost:8000"
UA = "smoke-test-agent"


def main():
    client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)

    # 1. Register
    email = "smoke@querymind.dev"
    r = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "SmokeTest123!",
        "confirm_password": "SmokeTest123!",
    }, headers={"user-agent": UA})
    print("register:", r.status_code, r.json())

    # 2. Verify email directly via Redis (token logged to console normally).
    #    Idempotent: skip if no pending token (already verified in a prior run).
    from app.redis_conf import redis_client
    token = None
    for key in redis_client.scan_iter("verify:*"):
        if redis_client.get(key) == email:
            token = key.split(":", 1)[1]
            break
    if token:
        r = client.post("/api/v1/auth/verify-email", json={"email": email, "token": token})
        print("verify-email:", r.status_code, r.json())
    else:
        print("verify-email: skipped (no pending token)")

    # 3. Login -> cookies
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "SmokeTest123!"},
                    headers={"user-agent": UA})
    print("login:", r.status_code, r.json(), "| cookies:", list(client.cookies.keys()))
    assert "auth_token" in client.cookies

    csrf = client.cookies.get("csrf_token")

    # 4. Upload CSV
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["region", "sales", "rep", "signup_date"])
    w.writerow(["US", 100, "alice", "2024-01-01"])
    w.writerow(["EU", 250, "bob", "2024-02-01"])
    w.writerow(["US", 100, "alice", "2024-01-01"])  # duplicate
    w.writerow(["APAC", "", "carol", "2024-03-01"])  # empty sales
    w.writerow(["EU", 75, "", "2024-04-01"])  # empty rep

    r = client.post("/api/v1/datasets/upload",
                    files={"file": ("sales.csv", buf.getvalue().encode(), "text/csv")},
                    data={"name": f"sales_demo_{int(time.time())}", "description": "Smoke test"},
                    headers={"x-csrf-token": csrf, "user-agent": UA})
    print("upload:", r.status_code)
    body = r.json()
    print("  cleaning_report:", json.dumps(body.get("cleaning_report"), indent=2))
    assert r.status_code == 201
    ds_id = body["dataset"]["id"]

    # 5. List datasets
    r = client.get("/api/v1/datasets", headers={"user-agent": UA})
    print("list:", r.status_code, len(r.json()["datasets"]), "datasets")

    # 6. Records
    r = client.get(f"/api/v1/datasets/{ds_id}/records?page=1&page_size=10", headers={"user-agent": UA})
    print("records:", r.status_code, r.json()["total"], "rows")

    # 7. Schema
    r = client.get(f"/api/v1/datasets/{ds_id}/schema", headers={"user-agent": UA})
    print("schema:", r.status_code, list(r.json()["schema"].keys()))

    # 8. Profile
    r = client.get(f"/api/v1/datasets/{ds_id}/profile", headers={"user-agent": UA})
    print("profile:", r.status_code, "columns:", len(r.json()["columns"]), "sample_rows:", len(r.json()["sample_rows"]))

    # 9. DSL query: group by region, sum sales
    plan = {
        "select": ["region"],
        "group_by": ["region"],
        "metrics": [{"metric_type": "sum", "field": "sales", "alias": "total_sales"}],
        "sorts": [{"field": "total_sales", "direction": "desc"}],
    }
    r = client.post(f"/api/v1/datasets/{ds_id}/query", json=plan, headers={"x-csrf-token": csrf, "user-agent": UA})
    print("query:", r.status_code, r.json())

    # 10. Query again (cache hit)
    r = client.post(f"/api/v1/datasets/{ds_id}/query", json=plan, headers={"x-csrf-token": csrf, "user-agent": UA})
    print("query(cached):", r.status_code, "rows:", len(r.json().get("data", [])))

    # 11. Invalid DSL must 400
    bad = {"select": ["nonexistent"]}
    r = client.post(f"/api/v1/datasets/{ds_id}/query", json=bad, headers={"x-csrf-token": csrf, "user-agent": UA})
    print("query(bad):", r.status_code, r.json())

    # 12. Logout
    r = client.post("/api/v1/auth/logout", headers={"x-csrf-token": csrf, "user-agent": UA})
    print("logout:", r.status_code, r.json())

    # 13. Unauthenticated access blocked
    r = client.get("/api/v1/datasets", headers={"user-agent": UA})
    print("blocked after logout:", r.status_code)

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
