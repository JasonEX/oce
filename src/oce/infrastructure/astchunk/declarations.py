"""Grammar-agnostic reading of declarations from tree-sitter nodes.

Grammars disagree on node names but mostly agree on *fields*: a declaration
carries ``name`` (or a ``declarator`` chain in C-like grammars, or ``type`` on a
Rust ``impl``), and a body hangs off ``body``/``block``. Matching on those
rather than on per-language type lists is what lets the same code read Python,
TypeScript, Go, Rust, Java, Kotlin and Swift without a table per grammar.
"""

from __future__ import annotations

from oce.infrastructure.astchunk.compat import CompatNode

# Types that syntactically wrap a declaration without naming it. They are
# transparent to scope chains and definition walks: skip the node, keep going.
TRANSPARENT_TYPES = frozenset(
    {
        "decorated_definition",
        "export_statement",
        "expression_statement",
        "lexical_declaration",
        "variable_declaration",
        "field_declaration",
        "declaration",
        "assignment",
        "template_declaration",
        "attribute_item",
    }
)

_DEFINITION_SUFFIXES = ("_definition", "_declaration", "_item", "_spec", "_specifier")
_DEFINITION_TYPES = frozenset(
    {
        "method",
        "singleton_method",
        "class",
        "module",
        "variable_declarator",
        "init_declarator",
    }
)
# Declarations that name something but never define a project symbol.
_EXCLUDED_MARKERS = (
    "parameter",
    "import",
    # ``export { x } from './x'`` re-exports a name declared elsewhere.
    "export_specifier",
    "package",
    "using",
    "annotation",
    "attribute",
    "macro",
    "local_variable",
    "field_declaration",
    "preproc",
    "extern_crate",
    "type_parameter",
    "enum_variant",
    "enumerator",
)
_FUNCTION_MARKERS = ("function", "method", "constructor", "lambda", "arrow")

MAX_SIGNATURE_CHARS = 160


def _leaf_name(node: CompatNode) -> str | None:
    """Text of the identifier a name-bearing node resolves to.

    Dotted and scoped names (``a.b.C``, ``std::io::Read``, ``Foo<T>``) resolve
    to their last plain identifier so the symbol index stores ``C``, ``Read``
    and ``Foo``.
    """
    if node.type.endswith("identifier") and not node.named_children:
        return node.text.decode("utf-8", errors="replace")
    # Bash names functions with a bare ``word`` node.
    if node.type == "word" and not node.named_children:
        return node.text.decode("utf-8", errors="replace")
    for field in ("name", "type"):
        child = node.child_by_field_name(field)
        if child is not None and child is not node:
            resolved = _leaf_name(child)
            if resolved:
                return resolved
    identifiers = [
        child for child in node.named_children if child.type.endswith("identifier")
    ]
    if identifiers:
        # Scoped paths list segments in order; the last one is the symbol.
        candidate = identifiers[-1] if node.type.endswith("_name") else identifiers[0]
        return _leaf_name(candidate)
    return None


def declared_name(node: CompatNode) -> str | None:
    """The symbol a declaration node introduces, or ``None`` for non-declarations."""
    name = node.child_by_field_name("name")
    if name is not None:
        return _leaf_name(name)
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        return declared_name(declarator) or _leaf_name(declarator)
    if node.type == "impl_item":
        # ``impl Trait for Type`` implements a trait declared elsewhere for a
        # type declared elsewhere; only an inherent ``impl Type`` extends the
        # type's own definition.
        if node.child_by_field_name("trait") is not None:
            return None
        target = node.child_by_field_name("type")
        return _leaf_name(target) if target is not None else None
    if node.type == "let_declaration":
        # ``let _: Router = ...`` names a type, not a binding.
        pattern = node.child_by_field_name("pattern")
        if pattern is not None and pattern.type.endswith("identifier"):
            return _leaf_name(pattern)
        return None
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return _leaf_name(left)
        return None
    if node.type == "assignment_expression":
        return assigned_function_name(node)
    # Kotlin/Swift properties: ``property_declaration > variable_declaration > id``.
    if node.type == "property_declaration":
        for child in node.named_children:
            if child.type == "variable_declaration":
                return _leaf_name(child)
        return None
    if node.type.endswith("_declarator"):
        return None
    if node.type in TRANSPARENT_TYPES:
        return None
    for child in node.named_children:
        if child.type.endswith("identifier") and not child.named_children:
            return child.text.decode("utf-8", errors="replace")
        # Only the first named child may name the node; a later identifier is
        # a reference, a type, or a parameter.
        break
    return None


_VALUE_DEFINITION_MARKERS = ("function", "arrow", "class")


def assigned_function_name(node: CompatNode) -> str | None:
    """Name defined by ``x = function``-style assignments in JavaScript/TypeScript.

    CommonJS and prototype code defines most of its API this way:
    ``app.use = function use(fn)``, ``exports.query = function query()``,
    ``Layer.prototype.match = function match(path)``. The property being
    assigned is the symbol; ``module.exports = createApplication`` only
    re-exports a name declared elsewhere and is skipped.
    """
    right = node.child_by_field_name("right")
    if right is None or not any(
        marker in right.type for marker in _VALUE_DEFINITION_MARKERS
    ):
        return None
    left = node.child_by_field_name("left")
    if left is None:
        return None
    if left.type == "identifier":
        return _leaf_name(left)
    if left.type == "member_expression":
        target = left.child_by_field_name("object")
        prop = left.child_by_field_name("property")
        if prop is None or prop.text == b"exports":
            return None
        if target is not None and target.text == b"module":
            return None
        return prop.text.decode("utf-8", errors="replace")
    return None


def require_alias_names(node: CompatNode) -> list[str] | None:
    """Names a CommonJS ``var X = require(...)`` declarator binds, else ``None``.

    ``var Route = require('./route')``, ``var compileETag =
    require('./utils').compileETag`` and ``const { Layer } = require('./layer')``
    bind names declared in another module: they are imports, not definitions,
    exactly like an ES ``import`` statement.
    """
    if node.type != "variable_declarator":
        return None
    value = node.child_by_field_name("value")
    while value is not None and value.type == "member_expression":
        value = value.child_by_field_name("object")
    if value is None or value.type != "call_expression":
        return None
    callee = value.child_by_field_name("function")
    if callee is None or callee.text != b"require":
        return None
    name = node.child_by_field_name("name")
    if name is None:
        return []
    if name.type.endswith("identifier"):
        return [name.text.decode("utf-8", errors="replace")]
    names: list[str] = []
    pending = [name]
    while pending:
        current = pending.pop(0)
        if not current.named_children:
            if "identifier" in current.type:
                text = current.text.decode("utf-8", errors="replace")
                if text not in names:
                    names.append(text)
            continue
        pending.extend(current.named_children)
    return names


_CALL_MARKERS = ("call", "invocation")
_CALLEE_FIELDS = ("function", "name", "method", "callee")
_MEMBER_FIELDS = ("property", "field", "attribute", "name", "function", "method")


def is_call_type(node_type: str) -> bool:
    """``call``/``call_expression``/``method_invocation``/``invocation_expression``;
    macros and ``call_arguments``-style helper nodes are not call sites."""
    if "macro" in node_type or "argument" in node_type:
        return False
    return any(marker in node_type for marker in _CALL_MARKERS)


def callee_name(node: CompatNode) -> str | None:
    """Last identifier of the callee: ``self.pool.acquire()`` -> ``acquire``,
    ``a::b::c()`` -> ``c``, ``foo()`` -> ``foo``. ``None`` for computed calls."""
    for field in _CALLEE_FIELDS:
        target = node.child_by_field_name(field)
        if target is not None:
            return _member_leaf(target)
    return None


def _member_leaf(node: CompatNode) -> str | None:
    if node.type.endswith("identifier") and not node.named_children:
        return node.text.decode("utf-8", errors="replace")
    if node.type == "word" and not node.named_children:
        return node.text.decode("utf-8", errors="replace")
    for field in _MEMBER_FIELDS:
        child = node.child_by_field_name(field)
        if child is not None and child is not node:
            resolved = _member_leaf(child)
            if resolved:
                return resolved
    identifiers = [
        child for child in node.named_children if child.type.endswith("identifier")
    ]
    if identifiers:
        return _member_leaf(identifiers[-1])
    return None


# Containers grammars use for "extends"/"implements" lists. Python puts base
# classes in the declaration's ``superclasses`` argument list.
_HERITAGE_TYPES = frozenset(
    {
        "superclass",
        "super_interfaces",
        "class_heritage",
        "extends_clause",
        "implements_clause",
        "extends_type_clause",
        "implements_type_clause",
        "base_list",
        "delegation_specifiers",
        "inheritance_specifier",
        "type_inheritance_clause",
    }
)


def heritage_names(node: CompatNode) -> list[str]:
    """Base classes, interfaces and traits a declaration extends or implements.

    Generic arguments (``IRequestHandler<Ping>`` -> ``Ping``) are type
    parameters, not supertypes, so nodes holding arguments are not descended.
    """
    names: list[str] = []
    containers = [
        child
        for child in node.named_children
        if child.type in _HERITAGE_TYPES
        or (child.type == "argument_list" and node.type == "class_definition")
    ]
    superclasses = node.child_by_field_name("superclasses")
    if superclasses is not None and superclasses not in containers:
        containers.append(superclasses)
    pending = list(containers)
    while pending:
        current = pending.pop(0)
        if "argument" in current.type and current.type != "argument_list":
            continue
        if current.type.endswith("identifier") and not current.named_children:
            text = current.text.decode("utf-8", errors="replace")
            if text not in names:
                names.append(text)
            continue
        if current.type == "keyword_argument":
            # ``class Meta(metaclass=ABCMeta)`` configures, it does not inherit.
            continue
        pending.extend(current.named_children)
    return names


def is_definition_type(node_type: str) -> bool:
    if any(marker in node_type for marker in _EXCLUDED_MARKERS):
        return False
    if node_type in TRANSPARENT_TYPES:
        return False
    return node_type in _DEFINITION_TYPES or node_type.endswith(_DEFINITION_SUFFIXES)


def is_function_like(node_type: str) -> bool:
    return any(marker in node_type for marker in _FUNCTION_MARKERS)


def signature_line(node: CompatNode) -> str:
    """First non-empty line of a declaration, bounded for use as a header."""
    text = node.text.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > MAX_SIGNATURE_CHARS:
                return stripped[: MAX_SIGNATURE_CHARS - 1] + "…"
            return stripped
    return ""
