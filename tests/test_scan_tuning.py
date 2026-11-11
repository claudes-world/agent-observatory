import random
import string

from obs.scan import scan_text

A = string.ascii_letters + string.digits
R = random.Random(7)


def rnd(n, alpha=A):
    return "".join(R.choice(alpha) for _ in range(n))


def kinds_of(t):
    return [k for _, _, k, _ in scan_text(t, names=None)]


def high_entropy():
    while True:
        s = rnd(R.randint(36, 56))
        if len(s) >= 32 and kinds_of(s) == ["base64_secret"]:
            return s


def b64url(n):
    return rnd(n, A + "-_")


SEEDED = {
    "sk": [f"sk-{rnd(40)}" for _ in range(6)],
    "ghp": [f"ghp_{rnd(36)}" for _ in range(6)],
    "xox": [f"xox{R.choice('abp')}-{rnd(12, string.digits)}-{rnd(14)}" for _ in range(6)],
    "akia": [f"AKIA{rnd(16, string.ascii_uppercase + string.digits)}" for _ in range(6)],
    "pem": [f"-----BEGIN {k}-----" for k in ("RSA PRIVATE KEY", "PRIVATE KEY", "OPENSSH PRIVATE KEY",
                                              "EC PRIVATE KEY", "DSA PRIVATE KEY", "CERTIFICATE")],
    "jwt": [f"eyJ{b64url(20)}.eyJ{b64url(30)}.{b64url(30)}" for _ in range(6)],
    "telegram_bot_token": [f"{rnd(R.choice((8, 9, 10)), string.digits)}:{b64url(35)}" for _ in range(6)],
    "base64_secret": [high_entropy() for _ in range(6)],
}


def kinds(text):
    return [k for _, _, k, _ in scan_text(text, names=None)]


def test_seeded_true_positives_all_caught():
    caught = total = 0
    for kind, items in SEEDED.items():
        for s in items:
            total += 1
            got = kinds(f"token is {s} end")
            want = "api_key" if kind in ("sk", "ghp", "xox", "akia") else kind
            ok = want in got
            caught += ok
            assert ok, (kind, s[:6])
    assert total == 48 and caught == total


def test_known_id_shapes_not_base64_secret():
    ids = [
        f"toolu_{rnd(24)}", f"msg_{rnd(30)}", "123e4567-e89b-12d3-a456-426614174000",
        "a" * 5 + "1b2c3d4e5f" * 3 + "0" * 5,  # 40 hex
        "".join(R.choice("0123456789abcdef") for _ in range(64)),
        "agent-" + "".join(R.choice("0123456789abcdef") for _ in range(17)),
        f"rollout-2026-11-08T10-00-00-{rnd(36)}",
        f'attachment_file_id="AwACAgEAAxkBAAK{rnd(50)}"',
        f'\\"file_id\\":\\"BQACAgEAAxkBAAK{rnd(50)}\\"',
        f"sha256={rnd(43)}=",
        f"see https://example.com/a/{rnd(40)}/b",
        f'"signature":"CAQS{rnd(200)}=="',
    ]
    for s in ids:
        assert "base64_secret" not in kinds(f"x {s} y"), s[:20]


def test_data_image_counted_as_image():
    from collections import Counter
    c = Counter()
    t = f"data:image/png;base64,iVBORw0KGgo{rnd(300)}"
    assert scan_text(t, names=None, skipped=c) == []
    assert c["image"] >= 1
