import subprocess


def run_shell(cmd: str, cwd: str | None = None, timeout: int = 120) -> tuple[str, str, int]:
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


def git_diff(repo_path: str) -> str:
    stdout, _, _ = run_shell("git diff", cwd=repo_path)
    return stdout


def git_diff_staged(repo_path: str) -> str:
    stdout, _, _ = run_shell("git diff --staged", cwd=repo_path)
    return stdout
