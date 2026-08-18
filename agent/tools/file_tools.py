def read_file(path: str) -> str:
    import os
    if os.path.isdir(path):
        entries = os.listdir(path)
        return f"Error: {path} is a directory, not a file. Contents: {', '.join(sorted(entries)[:20])}"
    with open(path) as f:
        return f.read()


def write_file(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)
