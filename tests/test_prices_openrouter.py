from obs.prices import Prices, norm, provider_guess


def mk(tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text('prices_as_of: "2026-11-08"\nmodels:\n  - {id: claude-x, provider: anthropic, input: 1, cached_input: 0.1, output: 5}\n'
                 '  - {id: deepseek-empty, provider: deepseek, input: null, cached_input: null, output: null}\n')
    o = tmp_path / "o.yaml"
    rows = [("anthropic/claude-x", 9), ("deepseek/deepseek-chat", 0.32), ("deepseek/deepseek-v3.2", 0.26), ("deepseek/deepseek-v3.2-exp", 0.27),
            ("moonshotai/kimi-k2.7-code", 0.6), ("z-ai/glm-5.1", 1.4), ("qwen/qwen3-coder", 0.3), ("other/glm-5.1", 7)]
    o.write_text('prices_as_of: "2026-11-09"\nmodels:\n' + "".join(
        f'  - {{id: "{i}", provider: "{i.split("/")[0]}", input: {p}, cached_input: null, output: {p * 2}}}\n' for i, p in rows))
    return Prices(str(y), str(o))


def test_rules(tmp_path):
    p = mk(tmp_path)
    src = lambda m: p.resolve(m)[1]
    rid = lambda m: (p.resolve(m)[0] or {}).get("id")
    assert src("claude-x") == "yaml" and p.cost("claude-x", 1_000_000) == 1.0          # 1 yaml wins over OpenRouter
    assert src("deepseek/deepseek-chat") == "openrouter-exact"                           # 2 exact id
    assert (src("kimi-k2.7-code"), rid("kimi-k2.7-code")) == ("openrouter-guess", "moonshotai/kimi-k2.7-code")  # 3 kimi
    assert (src("glm-5.1"), rid("glm-5.1")) == ("openrouter-guess", "z-ai/glm-5.1")     # 4 glm, not other/glm-5.1
    assert (src("qwen3-coder"), rid("qwen3-coder")) == ("openrouter-guess", "qwen/qwen3-coder")  # 5 qwen
    assert src("deepseek-empty") == "none"                                              # 6 null-rate yaml row falls through, nothing close
    assert (src("DeepSeek-V3.2-20251201"), rid("DeepSeek-V3.2-20251201")) == ("openrouter-fuzzy", "deepseek/deepseek-v3.2")  # 7 fuzzy, closest id
    assert src("gpt-9") == "none" and p.cost("gpt-9", 5) is None                         # 8 unknown
    assert p.asof_text() == "list prices as of 2026-11-08; OpenRouter prices as of 2026-11-09"


def test_norm():
    assert norm("Kimi-K2.7-code") == "kimik27code" and provider_guess("kimi-k2") == "moonshotai"
