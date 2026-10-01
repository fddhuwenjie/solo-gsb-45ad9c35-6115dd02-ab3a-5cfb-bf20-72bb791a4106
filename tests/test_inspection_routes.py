from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 30, 8, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def dts(sec):
    return BASE + timedelta(seconds=sec)


def pt(device, sec, lat, lon=0.005):
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def gate_geom(lat):
    return {
        "type": "LineString",
        "coordinates": [[0.002, lat], [0.008, lat]],
    }


def create_fence(client):
    r = client.post("/fences", json={"name": "巡检路线围栏", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def create_gate(client, fence_id, lat, fwd="向北", rev="向南"):
    r = client.post(
        f"/fences/{fence_id}/gates",
        json={
            "geometry": gate_geom(lat),
            "forward_name": fwd,
            "reverse_name": rev,
            "min_displacement_m": 0,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_route(client, fence_id, checkpoints, name="常规巡检"):
    r = client.post(
        f"/fences/{fence_id}/inspection-routes",
        json={"name": name, "checkpoints": checkpoints},
    )
    assert r.status_code == 201, r.text
    return r.json()


def cp(gate_id, direction="forward", max_interval_s=10):
    return {
        "gate_id": gate_id,
        "direction": direction,
        "max_interval_s": max_interval_s,
    }


def first_cp(gate_id, direction="forward"):
    return cp(gate_id, direction=direction, max_interval_s=None)


def report(client, fence_id, route_id, device="dev1", start=0, end=100):
    r = client.get(
        f"/fences/{fence_id}/inspection-routes/{route_id}/report",
        params={"device_id": device, "start": iso(start), "end": iso(end)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_route_crud_and_checkpoint_validation(client):
    fid = create_fence(client)
    g1, g2, g3 = (create_gate(client, fid, lat) for lat in (0.003, 0.005, 0.007))
    gates = [g1, g2, g3] + [create_gate(client, fid, 0.007 + i * 0.0002) for i in range(1, 7)]

    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=20)])
    assert route["fence_id"] == fid
    assert route["name"] == "常规巡检"
    assert [c["sequence"] for c in route["checkpoints"]] == [1, 2]
    assert route["checkpoints"][0] == {
        "sequence": 1,
        "gate_id": g1,
        "direction": "forward",
        "max_interval_s": None,
    }
    assert route["checkpoints"][1]["max_interval_s"] == 20

    listed = client.get(f"/fences/{fid}/inspection-routes").json()
    assert [r["id"] for r in listed] == [route["id"]]
    assert client.get(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).json()["name"] == "常规巡检"

    # 围栏不存在
    assert client.post("/fences/999/inspection-routes", json={
        "name": "x", "checkpoints": [first_cp(g1), cp(g2)]
    }).status_code == 404

    # 门线不存在 / 属于其他围栏
    assert client.post(f"/fences/{fid}/inspection-routes", json={
        "name": "bad", "checkpoints": [first_cp(999), cp(g2)]
    }).status_code == 422

    other = create_fence(client)
    assert client.post(f"/fences/{other}/inspection-routes", json={
        "name": "bad", "checkpoints": [first_cp(g1), cp(g2)]
    }).status_code == 422

    # 门线重复、首点带间隔、后续点缺间隔、数量不在 2~8
    bad_payloads = [
        {"name": "dup", "checkpoints": [first_cp(g1), cp(g1)]},
        {"name": "first-gap", "checkpoints": [cp(g1, max_interval_s=1), cp(g2)]},
        {"name": "missing-gap", "checkpoints": [first_cp(g1), cp(g2, None)]},
        {"name": "one", "checkpoints": [first_cp(g1)]},
        {"name": "nine", "checkpoints": [first_cp(gates[0])] + [cp(g) for g in gates[1:]]},
    ]
    for payload in bad_payloads:
        r = client.post(f"/fences/{fid}/inspection-routes", json=payload)
        assert r.status_code == 422, payload

    assert client.delete(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).status_code == 204
    assert client.get(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).status_code == 404
    assert client.delete(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).status_code == 404


def test_route_passes_when_crossings_match_order_direction_and_interval(client):
    fid = create_fence(client)
    g1, g2, g3 = (create_gate(client, fid, lat) for lat in (0.003, 0.005, 0.007))
    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=10)])

    # g1@5；g2@15，间隔恰好 10 秒（等于上限不超时）。
    # g3 是路线外门线；之后反向再过 g1 属于已通过门线，均忽略。
    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 10, 0.004),   # g1 forward @5
        pt("dev1", 20, 0.006),   # g2 forward @15
        pt("dev1", 30, 0.008),   # g3 forward @25, unrelated
        pt("dev1", 40, 0.002),   # 已通过门线的重复/反向穿越以及 g3 反向均忽略
    ])

    rep = report(client, fid, route["id"], end=50)
    assert rep["passed"] is True
    assert rep["first_violation_at"] is None
    assert rep["violation"] is None
    assert [c["status"] for c in rep["checkpoints"]] == ["passed", "passed"]
    assert datetime.fromisoformat(rep["checkpoints"][0]["passed_at"]) == dts(5)
    assert datetime.fromisoformat(rep["checkpoints"][1]["passed_at"]) == dts(15)
    assert all(c["failure_reason"] is None for c in rep["checkpoints"])


def test_future_checkpoint_crossing_is_sequence_violation(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=30)])

    # 从两线之间向北，只穿过下一检查点之后的 g2；之后再穿 g1 也不能纠正。
    submit(client, [
        pt("dev1", 0, 0.004),
        pt("dev1", 10, 0.006),   # g2 forward @5
        pt("dev1", 20, 0.002),   # 首个违规已发生，后续任何穿越都不改变结果
    ])

    rep = report(client, fid, route["id"], end=30)
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(5)
    violation = rep["violation"]
    assert violation["type"] == "sequence_violation"
    assert datetime.fromisoformat(violation["at"]) == dts(5)
    assert violation["expected_sequence"] == 1
    assert violation["actual_gate_id"] == g2
    assert violation["actual_direction"] == "forward"
    assert rep["checkpoints"][0]["status"] == "sequence_violation"
    assert "先遇到门线" in rep["checkpoints"][0]["failure_reason"]
    assert rep["checkpoints"][1]["status"] == "skipped"


def test_reverse_crossing_current_gate_is_sequence_violation(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    route = create_route(client, fid, [
        first_cp(g1), cp(g2, "forward", 30)
    ])

    submit(client, [
        pt("dev1", 0, 0.004),
        pt("dev1", 10, 0.002),   # g1 reverse @5
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(5)
    assert rep["violation"]["type"] == "sequence_violation"
    assert rep["violation"]["actual_gate_id"] == g1
    assert rep["violation"]["actual_direction"] == "reverse"
    assert rep["checkpoints"][0]["status"] == "sequence_violation"
    assert "实际为 reverse" in rep["checkpoints"][0]["failure_reason"]
    assert rep["checkpoints"][1]["status"] == "skipped"


def test_interval_timeout_and_exact_boundary(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=10)])

    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 10, 0.004),   # g1 @5；g2 deadline @15
        pt("dev1", 30, 0.004),   # 未穿过 g2
    ])

    # 查询窗尚未到达 deadline：还不能判超时，第二点 pending
    pending = report(client, fid, route["id"], end=15)
    assert pending["passed"] is False
    assert pending["violation"] is None
    assert [c["status"] for c in pending["checkpoints"]] == ["passed", "pending"]

    # deadline 后仍未匹配：超时，首个违规时刻是上一点通过时刻 + 间隔
    timed_out = report(client, fid, route["id"], end=30)
    assert timed_out["passed"] is False
    assert datetime.fromisoformat(timed_out["first_violation_at"]) == dts(15)
    assert timed_out["violation"]["type"] == "timeout"
    assert datetime.fromisoformat(timed_out["violation"]["at"]) == dts(15)
    assert timed_out["violation"]["actual_gate_id"] is None
    assert [c["status"] for c in timed_out["checkpoints"]] == ["passed", "timeout"]
    assert "最大间隔 10 秒" in timed_out["checkpoints"][1]["failure_reason"]

    # 另一设备恰好在 deadline @15 穿过 g2：不算超时，路线通过
    submit(client, [
        pt("dev2", 0, 0.002),
        pt("dev2", 10, 0.004),
        pt("dev2", 20, 0.006),
    ])
    exact = report(client, fid, route["id"], device="dev2", end=30)
    assert exact["passed"] is True
    assert datetime.fromisoformat(exact["checkpoints"][1]["passed_at"]) == dts(15)


def test_report_rebuilt_after_out_of_order_backfill(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=10)])

    # 只有 t0/t20/t30 时：g1@10，g2@25；间隔 15 秒，超过 10 秒上限
    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 20, 0.004),
        pt("dev1", 30, 0.006),
    ])
    first = report(client, fid, route["id"], end=40)
    assert first["passed"] is False
    assert first["violation"]["type"] == "timeout"
    assert datetime.fromisoformat(first["first_violation_at"]) == dts(20)

    # 乱序补录 t5/t10：重算后 g1@2.5、g2@7.5；报告按最新 crossing 实时重算
    submit(client, [
        pt("dev1", 5, 0.004),
        pt("dev1", 10, 0.006),
    ])
    second = report(client, fid, route["id"], end=40)
    assert second["passed"] is True
    assert second["violation"] is None
    assert datetime.fromisoformat(second["checkpoints"][0]["passed_at"]) == dts(2.5)
    assert datetime.fromisoformat(second["checkpoints"][1]["passed_at"]) == dts(7.5)


def test_referenced_gate_cannot_be_deleted_until_route_deleted(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=10)])

    r = client.delete(f"/fences/{fid}/gates/{g1}")
    assert r.status_code == 409
    assert "已被巡检路线引用" in r.json()["detail"]
    assert client.get(f"/fences/{fid}/gates").status_code == 200

    assert client.delete(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).status_code == 204
    assert client.delete(f"/fences/{fid}/gates/{g1}").status_code == 204


def test_report_requires_valid_window_and_existing_resources(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    route = create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=10)])

    url = f"/fences/{fid}/inspection-routes/{route['id']}/report"
    assert client.get(url, params={
        "device_id": "dev1", "start": iso(10), "end": iso(10)
    }).status_code == 422
    assert client.get(url, params={
        "device_id": "dev1", "start": iso(20), "end": iso(10)
    }).status_code == 422
    assert client.get(
        f"/fences/{fid}/inspection-routes/999/report",
        params={"device_id": "dev1", "start": iso(0), "end": iso(10)},
    ).status_code == 404
    assert client.get(
        f"/fences/999/inspection-routes/{route['id']}/report",
        params={"device_id": "dev1", "start": iso(0), "end": iso(10)},
    ).status_code == 404


def test_delete_fence_removes_inspection_routes(client):
    fid = create_fence(client)
    g1, g2 = create_gate(client, fid, 0.003), create_gate(client, fid, 0.005)
    create_route(client, fid, [cp(g1, max_interval_s=None), cp(g2, max_interval_s=10)])
    assert client.delete(f"/fences/{fid}").status_code == 204
    assert client.get(f"/fences/{fid}/inspection-routes").status_code == 404
