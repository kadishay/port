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


def test_run_shell_strips_ambient_git_env(tmp_path, monkeypatch):
    """Git sets GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE in the environment of any process
    it spawns hooks from (e.g. the pre-commit hook running this very test suite). If
    run_shell() inherits those ambient vars into its subprocess, `cwd=` no longer
    controls which repo a git command operates on — it silently redirects to whatever
    repo the ambient env points at instead of the isolated tmp repo passed via cwd."""
    bogus_git_dir = tmp_path.parent / "bogus-git-dir-should-not-be-touched"
    monkeypatch.setenv("GIT_DIR", str(bogus_git_dir))
    monkeypatch.setenv("GIT_INDEX_FILE", str(bogus_git_dir / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path.parent))

    run_shell("git init", cwd=str(tmp_path))
    run_shell("git commit --allow-empty -m init", cwd=str(tmp_path))

    assert (tmp_path / ".git").is_dir(), "git init should create .git inside cwd, not ambient GIT_DIR"
    assert not bogus_git_dir.exists(), "ambient GIT_DIR leaked into the subprocess"

    diff = git_diff(str(tmp_path))
    assert diff == ""
