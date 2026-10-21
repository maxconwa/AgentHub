import pytest

from agenthub.hooks import guard

CFG = guard.GuardConfig()


def risky(command, branch="feature/x"):
    never, matches = guard.classify(command, "/repo", CFG, branch_of=lambda cwd: branch)
    return {m.rule_id for m in matches}, {m.rule_id for m in never}


@pytest.mark.parametrize("command", [
    "rm -rf build", "rm -r dir", "rm -f file", "rm -fr x", "rm -Rf x", "rm -v -r x", "rm --recursive x",
    "rm --force x", "/bin/rm -rf x", "sudo rm -rf x", "cd /tmp && rm -rf x", 'bash -c "rm -rf x"',
    "find . | xargs rm -rf", "echo $(rm -rf x)",
])
def test_rm_flagged(command):
    assert "rm-recursive-or-force" in risky(command)[0]


@pytest.mark.parametrize("command", ["rm file.txt", "rm -i file", "git rm -r --cached x", "npm run format",
                                     "ls -rf", "terraform plan"])
def test_rm_not_flagged(command):
    assert "rm-recursive-or-force" not in risky(command)[0]


@pytest.mark.parametrize("command", [
    'psql -c "DROP TABLE users"', "sqlite3 db 'drop table x'", 'mysql -e "TRUNCATE orders"',
    "echo 'drop database prod;' | psql", "psql -c 'DROP SCHEMA public CASCADE'",
])
def test_sql_flagged(command):
    assert "sql-drop-truncate" in risky(command)[0]


@pytest.mark.parametrize("command", ['psql -c "select * from drops"', "echo dropdown"])
def test_sql_not_flagged(command):
    assert "sql-drop-truncate" not in risky(command)[0]


@pytest.mark.parametrize("command", [
    "git push --force origin feature", "git push -f origin feature", "git push origin feature --force",
    "git push --force-with-lease", "git push origin +feature", "git -C /x push -f",
    "git -c core.x=y push --force", "git push -uf origin feature", "git push --delete origin old",
    "git push origin :old", "git push --mirror",
])
def test_force_push_flagged(command):
    assert "git-push-force" in risky(command)[0]


@pytest.mark.parametrize("command", ["git push origin feature", "git push -u origin feature", "git fetch -f",
                                     "git push origin fix-force"])
def test_plain_push_not_force(command):
    assert "git-push-force" not in risky(command)[0]


@pytest.mark.parametrize("command", [
    "git push origin main", "git push origin master", "git push origin HEAD:main",
    "git push upstream feature:main", "git push origin refs/heads/main", "git -C repo push origin main",
])
def test_push_to_protected_flagged(command):
    assert "git-push-protected" in risky(command)[0]


@pytest.mark.parametrize("command", ["git push origin main-fix", "git push origin feature/maintenance",
                                     "git push origin domain"])
def test_push_to_similar_names_not_flagged(command):
    assert "git-push-protected" not in risky(command)[0]


@pytest.mark.parametrize("command", ["git push", "git push origin", "git push -u origin HEAD"])
def test_bare_push_depends_on_current_branch(command):
    assert "git-push-protected" in risky(command, branch="main")[0]
    assert "git-push-protected" not in risky(command, branch="feature/x")[0]


def test_unknown_branch_is_treated_as_protected():
    assert "git-push-protected" in risky("git push", branch=None)[0]


def test_changing_directory_makes_branch_unknown():
    # cwd is on a feature branch, but `cd` means git may run in another repo.
    assert "git-push-protected" in risky("cd ../other && git push", branch="feature/x")[0]
    assert "git-reset-hard-shared" in risky("git -C ../other reset --hard", branch="feature/x")[0]


@pytest.mark.parametrize("command", ["git merge feature", "git merge --no-ff feature"])
def test_merge_on_protected_branch(command):
    assert "git-merge-into-protected" in risky(command, branch="main")[0]
    assert "git-merge-into-protected" not in risky(command, branch="feature/x")[0]


def test_checkout_main_then_merge_flagged_from_any_branch():
    assert "git-merge-into-protected" in risky("git checkout main && git merge feature", branch="feature/x")[0]
    assert "git-merge-into-protected" in risky("git switch master; git merge x", branch="feature/x")[0]


def test_merge_base_is_not_merge():
    assert "git-merge-into-protected" not in risky("git merge-base main HEAD", branch="main")[0]


def test_gh_pr_merge_flagged():
    assert "gh-pr-merge" in risky("gh pr merge 12 --squash")[0]


@pytest.mark.parametrize("branch", ["main", "master", "develop", "release/1.2"])
def test_reset_hard_on_shared_branches(branch):
    assert "git-reset-hard-shared" in risky("git reset --hard origin/main", branch=branch)[0]


@pytest.mark.parametrize("command", ["git reset --hard HEAD~1", "git reset --soft HEAD~1"])
def test_reset_on_feature_branch_allowed(command):
    assert "git-reset-hard-shared" not in risky(command, branch="feature/x")[0]


@pytest.mark.parametrize("command", ["ls -la", "pytest -q", "git status", "git commit -m 'drop old code'",
                                     "git push origin feature", "npm install", "git log --oneline"])
def test_ordinary_commands_pass(command):
    assert risky(command) == (set(), set())


@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -rf / --no-preserve-root", "rm -rf ~", "rm -rf ~/", "rm -fr $HOME", "rm -rf /*",
    "sudo rm -rf / ", "git push --force origin main", "git push -f origin master", "git push origin main --force",
    "git push origin +main",
])
def test_never_list(command):
    assert risky(command)[1], command


@pytest.mark.parametrize("command", ["rm -rf /tmp/build", "rm -rf ~/project/dist", "git push --force origin feat",
                                     "git push origin main"])
def test_never_list_negatives(command):
    assert not risky(command)[1], command


def test_guard_config_overrides(tmp_path):
    path = tmp_path / "guard.toml"
    path.write_text(
        'protected_branches = ["trunk"]\nshared_branches = ["trunk"]\ndisabled_rules = ["gh-pr-merge"]\n'
        'stop_max_continuations = 5\n'
        '[[rules]]\nid = "terraform-destroy"\npattern = "terraform\\\\s+destroy"\ndescription = "terraform destroy"\n'
    )
    cfg = guard.load_guard_config(path)
    never, matches = guard.classify("terraform destroy && gh pr merge 1 && git push origin trunk", "/r", cfg,
                                    branch_of=lambda c: "x")
    assert {m.rule_id for m in matches} == {"terraform-destroy", "git-push-protected"}
    assert cfg.stop_max_continuations == 5


@pytest.mark.parametrize("content", ["not toml [", 'bogus_key = 1', 'protected_branches = []',
                                     '[[rules]]\nid = "x"\npattern = "("\ndescription = "bad regex"'])
def test_invalid_guard_config_raises(tmp_path, content):
    path = tmp_path / "guard.toml"
    path.write_text(content)
    with pytest.raises(guard.GuardConfigError):
        guard.load_guard_config(path)


def test_deny_rules_mirror_never_list():
    rules = guard.default_deny_rules(["main", "master"])
    assert "Bash(rm -rf /)" in rules and "Bash(git push --force origin main *)" in rules
    assert all(r.startswith("Bash(") and r.endswith(")") for r in rules)
    # nothing approvable is denied outright
    assert not any("feature" in r or r == "Bash(git push --force *)" for r in rules)
