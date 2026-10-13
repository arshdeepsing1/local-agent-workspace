"""The Python definitions outline_file lists, from ast. Imported by outline.py for small files and
run as a child process (python -I outline_ast.py, source on stdin, JSON on stdout) for large
ones: CPython's parser holds the GIL for as long as it runs (about a second at 1 MB), which would
pause the app's event loop, and a child can be stopped at the outline deadline. Imports only the
standard library, so it runs without the app's packages."""
import ast
import json
import sys


class _Full(Exception):
    pass


def definitions(text, limit, tick=None):
    """[(start, end_line, level, lineno), ...] of the classes and functions in text, in walk order,
    at most limit + 1 (one more tells the caller the outline is full). start is the first
    decorator's line; if/else, for, while, with, try and match blocks add no level. tick(count),
    when given, is called for every visited statement (the in-process deadline)."""
    tree = ast.parse(text)
    found, visited = [], [0]

    def walk(nodes, level):
        for node in nodes:
            visited[0] += 1
            if tick is not None:
                tick(visited[0])
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([decorator.lineno for decorator in node.decorator_list] + [node.lineno])
                found.append((start, node.end_lineno, level, node.lineno))
                if len(found) > limit:
                    raise _Full
                if level < 6:
                    walk(node.body, level + 1)
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                        walk([child], level)
    try:
        walk(tree.body, 1)
    except _Full:
        pass
    return found


def main():
    limit = int(sys.argv[1])
    text = sys.stdin.buffer.read().decode("utf-8")
    try:
        reply = {"found": definitions(text, limit)}
    except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
        reply = {"error": type(error).__name__, "lineno": getattr(error, "lineno", None)}
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
