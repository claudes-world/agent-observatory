import json
from obs import redact as R


def load(tmp_path):
    rx, nf, ok, sk, key_of = R.load_secrets_ex(dirs=[str(tmp_path)])
    return rx, ok, sk, key_of


def test_plain_token_file(tmp_path):
    (tmp_path / "cloudflare_r2_token").write_text("abcDEF123456xyz\n")
    rx, ok, sk, k = load(tmp_path)
    assert rx.search("t=abcDEF123456xyz") and k["abcDEF123456xyz"] == "cloudflare_r2_token"
    assert R.LAST_SOURCES["cloudflare_r2_token"]["loaded"] >= 1


def test_sa_json_pem_lines_and_body(tmp_path):
    l1, l2 = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "KcwggSjAgEAAoIBAQC7abcdefgh"
    pk = f"-----BEGIN PRIVATE KEY-----\n{l1}\n{l2}\n-----END PRIVATE KEY-----\n"
    (tmp_path / "gcp-x-sa.json").write_text(json.dumps({"private_key": pk, "client_email": "a@b-proj.iam.gserviceaccount.com"}))
    rx, ok, sk, k = load(tmp_path)
    assert rx.search(l1) and rx.search(l2) and rx.search(l1 + l2)
    assert rx.search(json.dumps(pk)[1:-1])


def test_json_string_leaf(tmp_path):
    (tmp_path / "creds.json").write_text(json.dumps({"a": {"b": ["Zx9-leafvalue_QQ77"]}, "n": 5}))
    rx, ok, sk, k = load(tmp_path)
    assert rx.search("Zx9-leafvalue_QQ77")


def test_short_junk_skipped(tmp_path):
    (tmp_path / "notes.txt").write_text("true\n8080\nhello world\n")
    rx, ok, sk, k = load(tmp_path)
    assert rx is None and ok == 0 and sk >= 3
    assert R.LAST_SOURCES["notes.txt"]["loaded"] == 0


def test_name_matched_short_kept(tmp_path):
    (tmp_path / "db_password").write_text("hunter22\n")
    (tmp_path / "tiny_token").write_text("abc\n")
    rx, ok, sk, k = load(tmp_path)
    assert rx.search("hunter22") and not rx.search("abc")
