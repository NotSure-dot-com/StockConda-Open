"""Compare executable AST, erasing only this change's typing scaffolding."""
import ast
from pathlib import Path
import sys


class EraseTypes(ast.NodeTransformer):
    def visit_Import(self, node):
        if all(alias.name in {'chess', 'chess.engine'} for alias in node.names):
            return None
        return node

    def visit_ImportFrom(self, node):
        return None if node.module == 'typing' else node

    def visit_ClassDef(self, node):
        if node.name == 'PrintOptions':
            return None
        node = self.generic_visit(node)
        node.bases = [base.value if isinstance(base, ast.Subscript)
                      and isinstance(base.value, ast.Name) and base.value.id == 'dict'
                      else base for base in node.bases]
        return node

    def visit_FunctionDef(self, node):
        if any(isinstance(d, ast.Name) and d.id == 'overload' for d in node.decorator_list):
            return None
        node.returns = None
        return self.generic_visit(node)

    def visit_arg(self, node):
        node.annotation = None
        return node

    def visit_AnnAssign(self, node):
        if node.value is None:
            return None
        return self.generic_visit(ast.Assign(targets=[node.target], value=node.value))

    def visit_Assign(self, node):
        if any(isinstance(t, ast.Name) and t.id in {'HistoryKey', 'DefaultValue'}
               for t in node.targets):
            return None
        return self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == 'cast':
            return self.visit(node.args[1])
        return self.generic_visit(node)


def normalized(path):
    return ast.dump(EraseTypes().visit(ast.parse(path.read_text(encoding='utf-8'))),
                    include_attributes=False)


if __name__ == '__main__':
    before, after = map(Path, sys.argv[1:])
    for relative in ['stockconda.py', '__init__.py', 'cython_ext/__init__.py']:
        assert normalized(before / relative) == normalized(after / relative), relative
        print('Executable AST unchanged:', relative)
