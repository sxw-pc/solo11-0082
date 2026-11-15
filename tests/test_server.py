import http.client
import threading

import pytest

from pointlog.server import LOOPBACK_HOST, create_server
from pointlog.store import Store


@pytest.fixture
def running_server(demo_run):
    server = create_server(demo_run.paths.db, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, demo_run
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _request(server, method, path):
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    headers = dict(response.getheaders())
    connection.close()
    return response.status, headers, body


def test_binds_only_loopback(running_server):
    server, _ = running_server
    assert server.server_address[0] == LOOPBACK_HOST == "127.0.0.1"


def test_index_and_batch_pages(running_server):
    server, demo_run = running_server
    status, headers, body = _request(server, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert demo_run.result.batch_id in body
    assert "只读复核台" in body
    assert "http://" not in body
    assert "https://" not in body

    status, _, detail = _request(
        server, "GET", f"/batch/{demo_run.result.batch_id}"
    )
    assert status == 200
    assert "批次详情" in detail
    assert "质量码分布" in detail
    assert "问题帧列表" in detail
    assert "待重采点位" in detail
    assert "bad_crc_request" in detail
    assert "line_a_energy" in detail


def test_unknown_paths_and_batches_return_404(running_server):
    server, _ = running_server
    for path in ("/nope", "/batch/does-not-exist", "/batch/..", "/static/app.js"):
        status, _, body = _request(server, "GET", path)
        assert status == 404
        assert "404" in body


def test_non_get_methods_return_405(running_server):
    server, _ = running_server
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
        status, headers, body = _request(server, method, "/")
        assert status == 405
        assert headers.get("Allow") == "GET"
        if method == "HEAD":
            assert body == ""
        else:
            assert "只读服务" in body


def test_server_does_not_mutate_ledger(running_server):
    server, demo_run = running_server
    store = Store(demo_run.paths.db, read_only=True)
    try:
        before = tuple(
            store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("batches", "frames", "readings", "issues", "resume_state")
        )
    finally:
        store.close()
    _request(server, "GET", "/")
    _request(server, "GET", f"/batch/{demo_run.result.batch_id}")
    _request(server, "POST", "/")
    store = Store(demo_run.paths.db, read_only=True)
    try:
        after = tuple(
            store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("batches", "frames", "readings", "issues", "resume_state")
        )
    finally:
        store.close()
    assert after == before


def test_create_server_requires_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_server(tmp_path / "missing.db", 0)
