"""Tree-sitter language registry with dynamic loading.

Central source of truth for all tree-sitter language configuration.
Uses per-language packages (tree-sitter-python, tree-sitter-javascript, etc.)
instead of the deprecated tree-sitter-languages bundle.

Languages are loaded on demand via importlib — only packages that are
installed get loaded. Adding a new language only requires adding a
registry entry and installing the package.
"""

import importlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Language configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageConfig:
    """Configuration for a single tree-sitter language.

    Attributes:
        name: Language identifier (e.g., "python", "javascript").
        module: Python module name (e.g., "tree_sitter_python").
        extensions: File extensions this language handles.
        language_attr: Attribute name in the module that returns the language
            function. Most packages use "language", but some (TypeScript/TSX)
            use different names.
        function_types: AST node types that represent function definitions.
        class_types: AST node types that represent class/struct/type definitions.
        method_types: AST node types inside classes that represent methods.
        decorator_types: AST node types that wrap definitions (e.g., Python decorators).
        definition_queries: Tree-sitter queries for extracting definitions (for repomap).
        reference_queries: Tree-sitter queries for extracting references (for repomap).
        call_queries: Tree-sitter queries for extracting call relationships.
        import_queries: Tree-sitter queries for extracting import relationships.
        inheritance_queries: Tree-sitter queries for extracting inheritance relationships.
    """

    name: str
    module: str
    extensions: tuple[str, ...]
    language_attr: str = "language"

    # Chunking: node type categories for generic AST walker
    function_types: frozenset[str] = frozenset()
    class_types: frozenset[str] = frozenset()
    method_types: frozenset[str] = frozenset()
    decorator_types: frozenset[str] = frozenset()

    # Repomap: tree-sitter queries for tag extraction
    definition_queries: tuple[tuple[str, str], ...] = ()
    reference_queries: tuple[tuple[str, str], ...] = ()

    # Relationship queries: tree-sitter queries for call/import/inheritance extraction
    call_queries: tuple[tuple[str, str], ...] = ()
    import_queries: tuple[tuple[str, str], ...] = ()
    inheritance_queries: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

LANGUAGES: dict[str, LanguageConfig] = {
    "python": LanguageConfig(
        name="python",
        module="tree_sitter_python",
        extensions=(".py", ".pyw"),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset({"class_definition"}),
        method_types=frozenset({"function_definition"}),
        decorator_types=frozenset({"decorated_definition"}),
        definition_queries=(
            ("class", "(class_definition name: (identifier) @name) @node"),
            (
                "function",
                "(function_definition name: (identifier) @name) @node",
            ),
            (
                "variable",
                "(assignment left: (identifier) @name) @node",
            ),
            (
                "decorator",
                "(decorated_definition definition: (class_definition name: (identifier) @name)) @node",
            ),
            (
                "decorator",
                "(decorated_definition definition: (function_definition name: (identifier) @name)) @node",
            ),
        ),
        reference_queries=(("identifier", "(identifier) @name"),),
        call_queries=(
            ("call", "(call function: (identifier) @target) @node"),
            (
                "call_method",
                "(call function: (attribute attribute: (identifier) @target)) @node",
            ),
        ),
        import_queries=(
            (
                "import",
                "(import_statement name: (dotted_name (identifier) @target)) @node",
            ),
            (
                "import_from_module",
                "(import_from_statement module_name: (dotted_name) @target) @node",
            ),
            (
                "import_from_name",
                "(import_from_statement name: (dotted_name (identifier) @target)) @node",
            ),
        ),
        inheritance_queries=(
            (
                "inherit",
                "(class_definition superclasses: (argument_list (identifier) @target)) @node",
            ),
        ),
    ),
    "javascript": LanguageConfig(
        name="javascript",
        module="tree_sitter_javascript",
        extensions=(".js", ".mjs", ".cjs"),
        function_types=frozenset(
            {
                "function_declaration",
                "generator_function_declaration",
            }
        ),
        class_types=frozenset({"class_declaration"}),
        method_types=frozenset({"method_definition", "generator_method_definition"}),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "function",
                "(function_declaration name: (identifier) @name) @node",
            ),
            (
                "function",
                "(generator_function_declaration name: (identifier) @name) @node",
            ),
            (
                "function",
                "(export_statement (function_declaration name: (identifier) @name)) @node",
            ),
            (
                "function",
                "(variable_declarator name: (identifier) @name value: (arrow_function)) @node",
            ),
            (
                "class",
                "(class_declaration name: (identifier) @name) @node",
            ),
            (
                "class",
                "(export_statement (class_declaration name: (identifier) @name)) @node",
            ),
            (
                "variable",
                "(variable_declarator name: (identifier) @name) @node",
            ),
            (
                "variable",
                "(export_statement (lexical_declaration (variable_declarator name: (identifier) @name))) @node",
            ),
            (
                "method",
                "(method_definition name: (property_identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("property", "(property_identifier) @name"),
        ),
        call_queries=(
            ("call", "(call_expression function: (identifier) @target) @node"),
            (
                "call_member",
                "(call_expression function: (member_expression property: (property_identifier) @target)) @node",
            ),
        ),
        import_queries=(
            ("import_source", "(import_statement source: (string) @target) @node"),
            ("import_clause", "(import_clause (identifier) @target) @node"),
            ("import_specifier", "(import_specifier name: (identifier) @target) @node"),
            ("namespace_import", "(namespace_import (identifier) @target) @node"),
        ),
        inheritance_queries=(
            (
                "extend",
                "(class_declaration (class_heritage (identifier) @target)) @node",
            ),
        ),
    ),
    "typescript": LanguageConfig(
        name="typescript",
        module="tree_sitter_typescript",
        language_attr="language_typescript",
        extensions=(".ts",),
        function_types=frozenset({"function_declaration"}),
        class_types=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "type_alias_declaration",
            }
        ),
        method_types=frozenset({"method_definition", "abstract_method_signature"}),
        decorator_types=frozenset({"decorator"}),
        definition_queries=(
            (
                "function",
                "(function_declaration name: (identifier) @name) @node",
            ),
            (
                "function",
                "(export_statement (function_declaration name: (identifier) @name)) @node",
            ),
            (
                "function",
                "(variable_declarator name: (identifier) @name value: (arrow_function)) @node",
            ),
            (
                "class",
                "(class_declaration name: (type_identifier) @name) @node",
            ),
            (
                "interface",
                "(interface_declaration name: (type_identifier) @name) @node",
            ),
            (
                "type",
                "(type_alias_declaration name: (type_identifier) @name) @node",
            ),
            (
                "enum",
                "(enum_declaration name: (identifier) @name) @node",
            ),
            (
                "method",
                "(method_definition name: (property_identifier) @name) @node",
            ),
            (
                "method",
                "(abstract_method_signature name: (property_identifier) @name) @node",
            ),
            (
                "namespace",
                "(internal_module name: (identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("type_identifier", "(type_identifier) @name"),
        ),
        call_queries=(
            ("call", "(call_expression function: (identifier) @target) @node"),
            (
                "call_member",
                "(call_expression function: (member_expression property: (property_identifier) @target)) @node",
            ),
        ),
        import_queries=(
            ("import_source", "(import_statement source: (string) @target) @node"),
            ("import_clause", "(import_clause (identifier) @target) @node"),
            ("import_specifier", "(import_specifier name: (identifier) @target) @node"),
            ("namespace_import", "(namespace_import (identifier) @target) @node"),
        ),
        inheritance_queries=(
            (
                "extend",
                "(class_declaration (class_heritage (extends_clause (identifier) @target))) @node",
            ),
            (
                "implement",
                "(class_declaration (class_heritage (implements_clause (type_identifier) @target))) @node",
            ),
        ),
    ),
    "tsx": LanguageConfig(
        name="tsx",
        module="tree_sitter_typescript",
        language_attr="language_tsx",
        extensions=(".tsx",),
        function_types=frozenset({"function_declaration"}),
        class_types=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "type_alias_declaration",
            }
        ),
        method_types=frozenset({"method_definition", "abstract_method_signature"}),
        decorator_types=frozenset({"decorator"}),
        definition_queries=(
            (
                "function",
                "(function_declaration name: (identifier) @name) @node",
            ),
            (
                "function",
                "(export_statement (function_declaration name: (identifier) @name)) @node",
            ),
            (
                "function",
                "(variable_declarator name: (identifier) @name value: (arrow_function)) @node",
            ),
            (
                "class",
                "(class_declaration name: (type_identifier) @name) @node",
            ),
            (
                "interface",
                "(interface_declaration name: (type_identifier) @name) @node",
            ),
            (
                "type",
                "(type_alias_declaration name: (type_identifier) @name) @node",
            ),
            (
                "enum",
                "(enum_declaration name: (identifier) @name) @node",
            ),
            (
                "method",
                "(method_definition name: (property_identifier) @name) @node",
            ),
            (
                "method",
                "(abstract_method_signature name: (property_identifier) @name) @node",
            ),
            (
                "namespace",
                "(internal_module name: (identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("type_identifier", "(type_identifier) @name"),
        ),
        call_queries=(
            ("call", "(call_expression function: (identifier) @target) @node"),
            (
                "call_member",
                "(call_expression function: (member_expression property: (property_identifier) @target)) @node",
            ),
        ),
        import_queries=(
            ("import_source", "(import_statement source: (string) @target) @node"),
            ("import_clause", "(import_clause (identifier) @target) @node"),
            ("import_specifier", "(import_specifier name: (identifier) @target) @node"),
            ("namespace_import", "(namespace_import (identifier) @target) @node"),
        ),
        inheritance_queries=(
            (
                "extend",
                "(class_declaration (class_heritage (extends_clause (identifier) @target))) @node",
            ),
            (
                "implement",
                "(class_declaration (class_heritage (implements_clause (type_identifier) @target))) @node",
            ),
        ),
    ),
    "go": LanguageConfig(
        name="go",
        module="tree_sitter_go",
        extensions=(".go",),
        function_types=frozenset({"function_declaration"}),
        class_types=frozenset({"type_declaration"}),
        method_types=frozenset({"method_declaration"}),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "function",
                "(function_declaration name: (identifier) @name) @node",
            ),
            (
                "method",
                "(method_declaration name: (field_identifier) @name) @node",
            ),
            (
                "type",
                "(type_declaration (type_spec name: (type_identifier) @name)) @node",
            ),
            (
                "type",
                "(type_alias name: (type_identifier) @name) @node",
            ),
            (
                "variable",
                "(short_var_declaration left: (expression_list (identifier) @name)) @node",
            ),
            (
                "variable",
                "(var_spec name: (identifier) @name) @node",
            ),
            (
                "constant",
                "(const_spec name: (identifier) @name) @node",
            ),
            (
                "method",
                "(method_elem name: (field_identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("field", "(field_identifier) @name"),
        ),
    ),
    "rust": LanguageConfig(
        name="rust",
        module="tree_sitter_rust",
        extensions=(".rs",),
        function_types=frozenset({"function_item"}),
        class_types=frozenset({"struct_item", "enum_item", "trait_item", "impl_item"}),
        method_types=frozenset({"function_item"}),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "function",
                "(function_item name: (identifier) @name) @node",
            ),
            (
                "struct",
                "(struct_item name: (type_identifier) @name) @node",
            ),
            (
                "enum",
                "(enum_item name: (type_identifier) @name) @node",
            ),
            (
                "enum_variant",
                "(enum_variant name: (identifier) @name) @node",
            ),
            (
                "trait",
                "(trait_item name: (type_identifier) @name) @node",
            ),
            (
                "impl",
                "(impl_item type: (type_identifier) @name) @node",
            ),
            (
                "constant",
                "(const_item name: (identifier) @name) @node",
            ),
            (
                "constant",
                "(static_item name: (identifier) @name) @node",
            ),
            (
                "type",
                "(type_item name: (type_identifier) @name) @node",
            ),
            (
                "module",
                "(mod_item name: (identifier) @name) @node",
            ),
            (
                "union",
                "(union_item name: (type_identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("type_identifier", "(type_identifier) @name"),
        ),
    ),
    "java": LanguageConfig(
        name="java",
        module="tree_sitter_java",
        extensions=(".java",),
        function_types=frozenset({"method_declaration"}),
        class_types=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
            }
        ),
        method_types=frozenset({"method_declaration", "constructor_declaration"}),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "class",
                "(class_declaration name: (identifier) @name) @node",
            ),
            (
                "interface",
                "(interface_declaration name: (identifier) @name) @node",
            ),
            (
                "enum",
                "(enum_declaration name: (identifier) @name) @node",
            ),
            (
                "enum_constant",
                "(enum_body (enum_constant name: (identifier) @name)) @node",
            ),
            (
                "method",
                "(method_declaration name: (identifier) @name) @node",
            ),
            (
                "constructor",
                "(constructor_declaration name: (identifier) @name) @node",
            ),
            (
                "field",
                "(field_declaration declarator: (variable_declarator name: (identifier) @name)) @node",
            ),
            (
                "annotation",
                "(annotation_type_declaration name: (identifier) @name) @node",
            ),
            (
                "record",
                "(record_declaration name: (identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("type_identifier", "(type_identifier) @name"),
        ),
    ),
    "c": LanguageConfig(
        name="c",
        module="tree_sitter_c",
        extensions=(".c", ".h"),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset({"struct_specifier", "enum_specifier"}),
        method_types=frozenset(),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "function",
                "(function_definition declarator: (function_declarator declarator: (identifier) @name)) @node",
            ),
            (
                "struct",
                "(struct_specifier name: (type_identifier) @name) @node",
            ),
            (
                "enum",
                "(enum_specifier name: (type_identifier) @name) @node",
            ),
            (
                "typedef",
                "(type_definition declarator: (type_identifier) @name) @node",
            ),
            (
                "macro",
                "(preproc_def name: (identifier) @name) @node",
            ),
            (
                "macro",
                "(preproc_function_def name: (identifier) @name) @node",
            ),
            (
                "union",
                "(union_specifier name: (type_identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("type_identifier", "(type_identifier) @name"),
        ),
    ),
    "cpp": LanguageConfig(
        name="cpp",
        module="tree_sitter_cpp",
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hxx"),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset({"class_specifier", "struct_specifier"}),
        method_types=frozenset({"function_definition"}),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "function",
                "(function_definition declarator: (function_declarator declarator: (identifier) @name)) @node",
            ),
            (
                "class",
                "(class_specifier name: (type_identifier) @name) @node",
            ),
            (
                "struct",
                "(struct_specifier name: (type_identifier) @name) @node",
            ),
            (
                "namespace",
                "(namespace_definition name: (namespace_identifier) @name) @node",
            ),
            (
                "enum",
                "(enum_specifier name: (type_identifier) @name) @node",
            ),
            (
                "typedef",
                "(type_definition declarator: (type_identifier) @name) @node",
            ),
            (
                "union",
                "(union_specifier name: (type_identifier) @name) @node",
            ),
            (
                "template_function",
                "(template_declaration (function_definition declarator: (function_declarator declarator: (identifier) @name))) @node",
            ),
            (
                "template_class",
                "(template_declaration (class_specifier name: (type_identifier) @name)) @node",
            ),
            (
                "using",
                "(alias_declaration name: (type_identifier) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("type_identifier", "(type_identifier) @name"),
        ),
    ),
    "ruby": LanguageConfig(
        name="ruby",
        module="tree_sitter_ruby",
        extensions=(".rb",),
        function_types=frozenset({"method", "singleton_method"}),
        class_types=frozenset({"class", "module"}),
        method_types=frozenset({"method", "singleton_method"}),
        decorator_types=frozenset(),
        definition_queries=(
            (
                "method",
                "(method name: (identifier) @name) @node",
            ),
            (
                "method",
                "(singleton_method name: (identifier) @name) @node",
            ),
            (
                "class",
                "(class name: (constant) @name) @node",
            ),
            (
                "module",
                "(module name: (constant) @name) @node",
            ),
            (
                "constant",
                "(assignment left: (constant) @name) @node",
            ),
        ),
        reference_queries=(
            ("identifier", "(identifier) @name"),
            ("constant", "(constant) @name"),
        ),
    ),
}


# ---------------------------------------------------------------------------
# Extension map (built from registry)
# ---------------------------------------------------------------------------

EXTENSION_MAP: dict[str, str] = {}
for _cfg in LANGUAGES.values():
    for _ext in _cfg.extensions:
        EXTENSION_MAP[_ext] = _cfg.name


# ---------------------------------------------------------------------------
# Language loading
# ---------------------------------------------------------------------------

# Cache: language name → tree-sitter Language object (or None if unavailable)
_language_cache: dict[str, Any | None] = {}


def get_language(name: str) -> Any | None:
    """Get a cached tree-sitter Language object, loading the module on demand.

    Args:
        name: Language name (e.g., "python", "javascript").

    Returns:
        tree_sitter.Language object, or None if the package isn't installed.
    """
    if name in _language_cache:
        return _language_cache[name]

    config = LANGUAGES.get(name)
    if config is None:
        _language_cache[name] = None
        return None

    try:
        mod = importlib.import_module(config.module)
        lang_func = getattr(mod, config.language_attr)
        from tree_sitter import Language

        lang = Language(lang_func())
        _language_cache[name] = lang
        logger.debug(f"Loaded tree-sitter language: {name}")
        return lang
    except ImportError:
        logger.debug(f"tree-sitter language package not installed: {config.module}")
        _language_cache[name] = None
        return None
    except Exception as e:
        logger.warning(f"Failed to load tree-sitter language {name}: {e}")
        _language_cache[name] = None
        return None


def get_language_config(name: str) -> LanguageConfig | None:
    """Get the LanguageConfig for a language name.

    Args:
        name: Language name (e.g., "python", "javascript").

    Returns:
        LanguageConfig instance, or None if the language isn't in the registry.
    """
    return LANGUAGES.get(name)


def language_for_extension(
    ext: str,
) -> tuple[Any | None, LanguageConfig | None]:
    """Get language object and config for a file extension.

    Args:
        ext: File extension including dot (e.g., ".py", ".js").

    Returns:
        Tuple of (Language object or None, LanguageConfig or None).
    """
    name = EXTENSION_MAP.get(ext.lower())
    if name is None:
        return None, None
    return get_language(name), get_language_config(name)
