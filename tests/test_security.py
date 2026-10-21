import hmac

from agenthub import security


def test_token_embeds_agent_id_and_parses():
    token = security.new_token("ag123")
    assert security.parse_token(token)[0] == "ag123"


def test_tokens_are_unique_and_high_entropy():
    tokens = {security.new_token("ag1") for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(security.parse_token(t)[1]) >= 43 for t in tokens)  # 32 bytes urlsafe


def test_hash_is_not_the_token():
    token = security.new_token("ag1")
    h = security.hash_token(token)
    assert token not in h and security.parse_token(token)[1] not in h
    assert len(h) == 64


def test_verify_token():
    token = security.new_token("ag1")
    h = security.hash_token(token)
    assert security.verify_token(token, h)
    assert not security.verify_token(token + "x", h)
    assert not security.verify_token(security.new_token("ag1"), h)


def test_verify_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    token = security.new_token("ag1")
    security.verify_token(token, security.hash_token(token))
    assert len(calls) == 1


def test_parse_rejects_malformed():
    for bad in ["", "ahk_", "ahk_ag1", "ahk__secret", "xyz_ag1_secret", "Bearer ahk_a_b"]:
        assert security.parse_token(bad) is None


def test_enrollment_code_format_and_normalization():
    code = security.new_enrollment_code()
    assert len(code) == 14 and code.count("-") == 2
    assert security.hash_code(code) == security.hash_code(code.lower().replace("-", " "))
    assert security.hash_code(code) != security.hash_code(security.new_enrollment_code())
