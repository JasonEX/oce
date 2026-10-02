"""Conservative import-origin evidence for bare Python calls.

Only a unique module-level named import can add an origin name. Any other
binding of its local alias anywhere in the file disables that evidence; this
deliberate abstention avoids reconstructing Python's dynamic name resolution.
Member calls retain their own leaf and never inherit a bare-name alias.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass

from oce.infrastructure.astchunk.compat import CompatNode
from oce.infrastructure.astchunk.declarations import _leaf_name

_BINDING_FIELDS = {
    "assignment": "left",
    "augmented_assignment": "left",
    "named_expression": "name",
    "for_statement": "left",
    "for_in_clause": "left",
    "function_definition": "name",
    "class_definition": "name",
    "as_pattern": "alias",
    "type_alias_statement": "left",
}
_BINDING_CONTAINERS = frozenset(
    {
        "parameters",
        "lambda_parameters",
        "global_statement",
        "nonlocal_statement",
        "delete_statement",
        "case_pattern",
        "type_parameter",
    }
)


def _identifiers(node: CompatNode) -> set[str]:
    names: set[str] = set()
    pending = [node]
    while pending:
        current = pending.pop()
        if current.type == "identifier":
            names.add(current.text.decode("utf-8", errors="replace"))
        else:
            pending.extend(current.named_children)
    return names


def _bound_identifiers(node: CompatNode) -> set[str]:
    """Assignment targets bind names; attribute/subscript stores bind no receiver."""
    names: set[str] = set()
    pending = [node]
    while pending:
        current = pending.pop()
        if current.type in ("attribute", "subscript"):
            continue
        if current.type == "identifier":
            names.add(current.text.decode("utf-8", errors="replace"))
        else:
            pending.extend(current.named_children)
    return names


def _parameter_name(node: CompatNode) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        name = (
            node if node.type == "identifier" else next(iter(node.named_children), None)
        )
    return _leaf_name(name) if name is not None else None


def _parameter_names(node: CompatNode) -> set[str]:
    return {
        name
        for child in node.named_children
        if (name := _parameter_name(child)) is not None
    }


@dataclass(frozen=True)
class PythonCallBindings:
    import_origins: dict[str, tuple[str, int]]
    shadowed_names: frozenset[str]
    rebound_receivers: frozenset[str]
    receiver_spans: dict[str, tuple[tuple[int, int], ...]]

    def permits_receiver(self, name: str, position: int) -> bool:
        if name in self.rebound_receivers:
            return False
        spans = self.receiver_spans.get(name, ())
        index = bisect_right(spans, position, key=lambda span: span[0]) - 1
        return index >= 0 and position <= spans[index][1]


def python_call_bindings(root: CompatNode) -> PythonCallBindings:
    """Unshadowed named imports and local declaration evidence for this file.

    Nested/conditional imports, wildcard imports and rebinding cause
    abstention. A source name is import evidence, never a declaration identity.
    """
    candidates: dict[str, tuple[str, int]] = {}
    counts: Counter[str] = Counter()
    candidate_nodes: set[int] = set()
    module_definitions: set[int] = set()
    module_counts: Counter[str] = Counter()
    for statement in root.named_children:
        declaration = statement
        if statement.type == "decorated_definition":
            declaration = statement.child_by_field_name("definition") or statement
        if declaration.type in ("function_definition", "class_definition"):
            name_node = declaration.child_by_field_name("name")
            if name_node is not None:
                name = _leaf_name(name_node)
                if name:
                    module_counts[name] += 1
                    module_definitions.add(declaration.start_byte)
        if statement.type != "import_from_statement":
            continue
        for imported in statement.named_children:
            if imported.type != "aliased_import":
                continue
            imported_name = imported.child_by_field_name("name")
            alias = imported.child_by_field_name("alias")
            if imported_name is None or alias is None:
                continue
            source = _leaf_name(imported_name)
            local = _leaf_name(alias)
            if source and local:
                counts[local] += 1
                candidates[local] = (source, statement.end_byte)
                candidate_nodes.add(imported.start_byte)

    conflicts: set[str] = set()
    shadowed: set[str] = set()
    rebound: set[str] = set()
    receiver_spans: dict[str, list[tuple[int, int]]] = {}
    pending = [(root, False, False, "", False)]
    while pending:
        node, in_class, inside_function, receiver, static = pending.pop()
        if node.type == "ERROR" or node.start_byte == node.end_byte:
            unsafe = frozenset((*module_counts, "self", "cls"))
            return PythonCallBindings({}, unsafe, unsafe, {})
        if node.type == "wildcard_import" or any(
            child.type == "wildcard_import" for child in node.named_children
        ):
            unsafe = frozenset((*module_counts, "self", "cls"))
            return PythonCallBindings({}, unsafe, unsafe, {})
        if node.type in ("import_statement", "import_from_statement"):
            for imported in node.named_children:
                names = _identifiers(imported)
                shadowed.update(names)
                rebound.update(names)
                if imported.start_byte not in candidate_nodes:
                    conflicts.update(names)
            pending.extend(
                (child, in_class, inside_function, receiver, static)
                for child in node.children
            )
            continue
        if node.type == "class_definition":
            in_class, inside_function, receiver = True, False, ""
        if node.type == "decorated_definition":
            static = any(
                child.type == "decorator" and "staticmethod" in _identifiers(child)
                for child in node.named_children
            )
        if node.type == "function_definition":
            receiver = ""
            if in_class and not inside_function and not static:
                parameters = node.child_by_field_name("parameters")
                if parameters is not None and parameters.named_children:
                    first = parameters.named_children[0]
                    first_name = _parameter_name(first)
                    if first_name in ("self", "cls"):
                        receiver = first_name
                        receiver_spans.setdefault(receiver, []).append(
                            (node.start_byte, node.end_byte)
                        )
            inside_function = True
        field = _BINDING_FIELDS.get(node.type)
        target = node.child_by_field_name(field) if field else None
        if target is not None:
            names = _bound_identifiers(target)
            conflicts.update(names)
            if node.start_byte not in module_definitions or any(
                module_counts[name] > 1 for name in names
            ):
                shadowed.update(names)
                rebound.update(names)
        if node.type in _BINDING_CONTAINERS:
            names = (
                _parameter_names(node)
                if node.type in ("parameters", "lambda_parameters")
                else _identifiers(node)
            )
            conflicts.update(names)
            shadowed.update(names)
            rebound.update(names - {receiver} if node.type == "parameters" else names)
        pending.extend(
            (child, in_class, inside_function, receiver, static)
            for child in node.children
        )
    return PythonCallBindings(
        import_origins={
            local: origin
            for local, origin in candidates.items()
            if counts[local] == 1 and local not in conflicts
        },
        shadowed_names=frozenset(shadowed),
        rebound_receivers=frozenset(rebound),
        receiver_spans={
            name: tuple(sorted(spans)) for name, spans in receiver_spans.items()
        },
    )


def bare_callee(node: CompatNode) -> str | None:
    target = node.child_by_field_name("function")
    if target is None or target.type != "identifier":
        return None
    return target.text.decode("utf-8", errors="replace")
