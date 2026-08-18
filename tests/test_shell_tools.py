from agent.tools.shell_tools import run_shell, git_diff
from agent.tools.file_tools import read_file, write_file


def test_run_shell_success():
    stdout, stderr, rc = run_shell("echo hello")
    assert rc == 0
    assert stdout.strip() == "hello"
    assert stderr == ""


def test_run_shell_failure():
    stdout, stderr, rc = run_shell("ls /nonexistent-path-xyz")
    assert rc != 0


def test_run_shell_cwd(tmp_path):
    stdout, _, rc = run_shell("pwd", cwd=str(tmp_path))
    assert rc == 0
    assert str(tmp_path) in stdout


def test_read_write_file(tmp_path):
    path = str(tmp_path / "test.txt")
    write_file(path, "hello world")
    assert read_file(path) == "hello world"


def test_git_diff_empty_on_clean_repo(tmp_path):
    run_shell("git init", cwd=str(tmp_path))
    run_shell("git commit --allow-empty -m 'init'", cwd=str(tmp_path))
    diff = git_diff(str(tmp_path))
    assert diff == ""
