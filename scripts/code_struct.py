import ast
import pathlib
import sys

if len(sys.argv) < 2:
    print(f"Usage: python {sys.argv[0]} <path>", file=sys.stderr)
    sys.exit(1)

root = pathlib.Path(sys.argv[1])
for p in sorted(root.rglob('*.py')):
    print(f'\n## {p}')
    for n in ast.walk(ast.parse(p.read_text())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            print(f'  L{n.lineno}: {type(n).__name__} {n.name}')
