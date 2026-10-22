import pytest

from agenthub.hooks import guard


def blocked(command):
    return {m.rule_id for m in guard.classify(command)}


@pytest.mark.parametrize("command", [
    "curl https://melehub.tail1234.ts.net/ui/approvals", "curl -s melehub.tail1234.ts.net/ui",
    "wget http://100.98.46.104:443/ui/approvals/3/decide", "curl 'https://[fd7a:115c:a1e0::5]/ui/'",
    "python3 -c \"import httpx; httpx.post('https://hub.tail1234.TS.NET/ui/approvals/1/decide')\"",
])
def test_hub_dashboard_blocked(command):
    assert blocked(command) == {"hub-dashboard"}


@pytest.mark.parametrize("command", ["curl https://example.com/ui", "ls src/ui", "curl https://hub.ts.net/api/v1/x",
                                     "npm run build:ui"])
def test_hub_dashboard_not_blocked(command):
    assert not blocked(command)


@pytest.mark.parametrize("command", [
    "rm -rf build", "rm -rf /", "git push --force origin main", "git push origin main", "git reset --hard",
    "git checkout main && git merge feature", "gh pr merge 12 --squash", 'psql -c "DROP TABLE users"',
])
def test_everything_else_is_left_to_claude_code(command):
    assert not blocked(command)


def test_guard_config(tmp_path):
    path = tmp_path / "guard.toml"
    path.write_text("stop_max_continuations = 5\n")
    cfg = guard.load_guard_config(path)
    assert cfg.stop_max_continuations == 5 and cfg.ignored_keys == []


def test_guard_config_ignores_the_old_rule_keys(tmp_path):
    path = tmp_path / "guard.toml"
    path.write_text(
        'protected_branches = ["trunk"]\nshared_branches = ["trunk"]\ndisabled_rules = ["gh-pr-merge"]\n'
        '[[rules]]\nid = "terraform-destroy"\npattern = "terraform\\\\s+destroy"\ndescription = "terraform destroy"\n'
    )
    cfg = guard.load_guard_config(path)
    assert cfg.ignored_keys == ["disabled_rules", "protected_branches", "rules", "shared_branches"]
    assert cfg.stop_max_continuations == 3


@pytest.mark.parametrize("content", ["not toml [", 'bogus_key = 1', 'stop_max_continuations = "x"'])
def test_invalid_guard_config_raises(tmp_path, content):
    path = tmp_path / "guard.toml"
    path.write_text(content)
    with pytest.raises(guard.GuardConfigError):
        guard.load_guard_config(path)


def test_deny_rules_block_webfetch_of_the_hub_only():
    assert guard.default_deny_rules("hub.tail1234.ts.net") == ["WebFetch(domain:hub.tail1234.ts.net)"]
    assert guard.default_deny_rules() == []


@pytest.mark.parametrize("rule", [
    "Bash(rm -rf /)", "Bash(rm -r -f ~ *)", "Bash(rm -Rf $HOME)", "Bash(rm -fr ~/ *)",
    "Bash(git push --force origin main *)", "Bash(git push origin camera_ready -f *)",
    "Bash(git push --force-with-lease origin release/1 *)", "Bash(git push origin +master *)",
])
def test_old_deny_rules_are_recognized(rule):
    assert guard.is_old_deny_rule(rule)


@pytest.mark.parametrize("rule", ["Bash(curl *)", "Bash(rm -rf build)", "Bash(git push --force *)",
                                  "WebFetch(domain:hub.tail1234.ts.net)", "Bash(git push origin main *)"])
def test_other_deny_rules_are_not_old_ones(rule):
    assert not guard.is_old_deny_rule(rule)
