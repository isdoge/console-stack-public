# -*- coding: utf-8 -*-
"""gen_third_party_licenses：上游原文抽取、镜像覆盖核对（B7 t41u 许可缺口）。"""
import hashlib
import importlib.util
import io
import json
import pathlib
import tarfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gen_third_party_licenses", ROOT / "scripts" / "gen_third_party_licenses.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _inventory(**over):
    inv = {
        "packages": {"bash": {"version": "5.2", "copyright": "a" * 64},
                     "docker-ce-cli": {"version": "5:29.9.2", "copyright": None}},
        "copyright_texts": {"a" * 64: "bash copyright"},
        "common_licenses": {"GPL-3": "gpl text"},
        "psycopg_libs": ["libpq-e9b5c9ac.so.5.18", "libcrypto-2d1629ee.so.3"],
        "node": "v24.22.2", "python": "3.10.22",
        "docker": {"docker": "29.9.2", "docker-buildx": "v0.38.1",
                   "docker-compose": "v5.6.1"},
    }
    inv.update(over)
    return inv


COVERS = ["debian docker-ce-cli", "psycopg-lib libpq", "psycopg-lib libcrypto",
          "node v24.22.2", "python 3.10.22", "docker-bin docker 29.9.2",
          "docker-bin docker-buildx v0.38.1", "docker-bin docker-compose v5.6.1"]


def _write(tmp_path, inventory, covers=COVERS, gaps=()):
    debian, debian_covers = gen.debian_entries(inventory)
    text = gen.render([("Debian", debian)], list(gaps), list(covers) + debian_covers)
    path = tmp_path / "TPL.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_lib_stem_strips_auditwheel_hash():
    assert gen.lib_stem("libcom_err-2abe824b.so.2.1") == "libcom_err"
    assert gen.lib_stem("libgssapi_krb5-497db0c6.so.2.2") == "libgssapi_krb5"
    assert gen.lib_stem("libpq.so.5") == "libpq"


def test_extract_section_tar_and_head_comment():
    readme = b"# x\n\nLicense\n-------\n\n(The MIT License)\nbody\n\n[link]: http://x\n"
    assert gen.extract(readme, {"start": "^License\\n-+\\n", "end": "^\\["}) == \
        "License\n-------\n\n(The MIT License)\nbody"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        data = b"LICENCE TEXT\n"
        info = tarfile.TarInfo("pkg-1/LICENCE")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    assert gen.extract(buf.getvalue(), {"tar_member": "pkg-1/LICENCE"}) == "LICENCE TEXT"
    src = b"/*\n * Copyright MIT\n */\n#include <x.h>\n"
    assert gen.extract(src, {"head_comment": True}) == "/*\n * Copyright MIT\n */"
    with pytest.raises(RuntimeError):
        gen.extract(b"nothing here", {"start": "^License"})


def test_fetch_rejects_sha_mismatch(tmp_path, monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(gen.urllib.request, "urlopen", lambda *a, **k: _Resp(b"tampered"))
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        gen.fetch("https://example.invalid/LICENSE", "0" * 64, tmp_path)
    good = hashlib.sha256(b"tampered").hexdigest()
    assert gen.fetch("https://example.invalid/LICENSE", good, tmp_path) == b"tampered"


def test_verify_image_passes_when_everything_covered(tmp_path):
    inv = _inventory()
    path = _write(tmp_path, inv)
    ok, report = gen.verify_image(path, inventory=inv)
    assert ok, report["failures"]
    assert report["warnings"] == []


@pytest.mark.parametrize("change, needle", [
    (lambda inv: inv["packages"].__setitem__("curl", {"version": "7", "copyright": None}),
     "debian package not covered: curl"),
    (lambda inv: inv["psycopg_libs"].append("libsasl2-84219a89.so.3.0.0"),
     "psycopg bundled library not covered"),
    (lambda inv: inv.__setitem__("node", "v26.0.0"), "node: image has v26.0.0"),
])
def test_verify_image_fails_closed_on_uncovered_component(tmp_path, change, needle):
    path = _write(tmp_path, _inventory())
    inv = _inventory()
    change(inv)
    ok, report = gen.verify_image(path, inventory=inv)
    assert not ok
    assert any(needle in f for f in report["failures"]), report["failures"]


def test_verify_image_warns_on_same_major_drift_and_fails_on_gaps(tmp_path):
    inv = _inventory()
    path = _write(tmp_path, inv)
    drift = _inventory(node="v24.23.0")
    ok, report = gen.verify_image(path, inventory=drift)
    assert ok and any("node" in w for w in report["warnings"])
    gapped = _write(tmp_path, inv, gaps=["npm x@1: no license file"])
    ok, report = gen.verify_image(gapped, inventory=inv)
    assert not ok and any("GAPS not zero" in f for f in report["failures"])


def test_verify_image_requires_identical_static_copy(tmp_path):
    inv = _inventory()
    path = _write(tmp_path, inv)
    static = tmp_path / "static.txt"
    static.write_text("stale", encoding="utf-8")
    ok, report = gen.verify_image(path, static_copy=static, inventory=inv)
    assert not ok and any("differs" in f for f in report["failures"])


def test_committed_file_has_zero_gaps_and_pinned_manifest():
    text = (ROOT / "kernel" / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    assert "\n# GAPS (0)" in text
    manifest = json.loads((ROOT / "scripts" / "third_party_upstream.json").read_text())
    for entry in manifest["entries"]:
        for item in entry["texts"]:
            assert len(item.get("sha256", "")) == 64, item["url"]
            assert item["url"] in text, item["url"]
    for gap_kind in ("npm agent-base@6.0.2", "npm dlv@1.1.3", "npm https-proxy-agent@5.0.1",
                     "npm react-remove-scroll-bar@2.3.8", "python flask-cors==6.0.5",
                     "go standard library (go1.27.3)", "covers: node v24.22.2",
                     "covers: psycopg-lib libpq", "covers: debian docker-ce-cli"):
        assert gap_kind in text, gap_kind
