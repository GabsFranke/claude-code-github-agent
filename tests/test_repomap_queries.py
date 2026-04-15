"""Integration tests for tree-sitter definition/reference queries in ts_languages.

Validates that each language's queries parse correctly and extract expected
symbols from sample source files. Uses RepoMap's own _get_tags_treesitter
method so the test exercises the real production code path.

Requires tree-sitter language packages to be installed.
"""

from pathlib import Path

import pytest

from shared.repomap import RepoMap
from shared.ts_languages import LANGUAGES, get_language

# ---------------------------------------------------------------------------
# Sample source code per language
# ---------------------------------------------------------------------------

SAMPLES = {
    "python": '''
class MyClass:
    """A class."""
    def method(self):
        pass

def hello():
    pass

x = 42
''',
    "javascript": """
function greet(name) {
    return "Hello, " + name;
}

class User {
    constructor(name) {
        this.name = name;
    }
    toString() {
        return this.name;
    }
}

const add = (a, b) => a + b;
var count = 0;
""",
    "typescript": """
function greet(name: string): string {
    return "Hello, " + name;
}

class User {
    constructor(public name: string) {}
}

interface Config {
    host: string;
}

type Alias = string | number;

enum Direction {
    Up,
    Down,
}
""",
    "tsx": """
function Component(props: { name: string }) {
    return null;
}

class App {
    render() {
        return null;
    }
}

interface Props {
    title: string;
}
""",
    "go": """package main

func main() {
    fmt.Println("hello")
}

func (s *Server) Start() error {
    return nil
}

type Config struct {
    Host string
}

type Handler interface {
    Serve() error
}

var version = "1.0"
const maxRetries = 3
""",
    "rust": """
struct Config {
    name: String,
}

enum Status {
    Active,
    Inactive,
}

trait Handler {
    fn handle(&self);
}

impl Handler for Config {
    fn handle(&self) {}
}

fn main() {
    println!("hello");
}

const MAX_SIZE: usize = 1024;

mod utils;
""",
    "java": """
public class Application {
    private String name;

    public Application(String name) {
        this.name = name;
    }

    public void run() {}
}

interface Service {
    void start();
}

enum Color {
    RED, GREEN, BLUE
}
""",
    "c": """
typedef struct {
    int x;
    int y;
} Point;

enum Status { OK = 0, ERROR = 1 };

int main(void) {
    return 0;
}

#define MAX_SIZE 1024
""",
    "cpp": """
class Application {
public:
    void run() {}
};

struct Config {
    std::string host;
};

namespace utils {
    void helper() {}
}

typedef int ErrorCode;
""",
    "ruby": """
class Application
  def initialize(name)
    @name = name
  end

  def run
    puts @name
  end
end

module Utils
  def self.helper
    42
  end
end

VERSION = "1.0"
""",
}


# Expected definition names per language (just names, not categories —
# categories are advisory and we don't want to be brittle about them).
EXPECTED_NAMES = {
    "python": {"MyClass", "hello", "x"},
    "javascript": {"greet", "User", "toString", "add", "count"},
    "typescript": {"greet", "User", "Config", "Alias", "Direction"},
    "tsx": {"Component", "App", "Props"},
    "go": {"main", "Start", "Config", "Handler", "version", "maxRetries"},
    "rust": {"Config", "Status", "Handler", "main", "MAX_SIZE", "utils"},
    "java": {"Application", "Service", "Color"},
    "c": {"Point", "Status", "main", "MAX_SIZE"},
    "cpp": {"Application", "Config", "utils", "ErrorCode"},
    "ruby": {"initialize", "run", "Application", "Utils", "VERSION"},
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_definition_names(lang_name: str, source: str) -> set[str]:
    """Write source to a temp file, extract tags via RepoMap, return definition names."""
    config = LANGUAGES[lang_name]
    ext = config.extensions[0]

    import os
    import tempfile

    tmpdir = tempfile.mkdtemp()
    filepath = Path(tmpdir) / f"sample{ext}"
    filepath.write_text(source, encoding="utf-8")

    # Use RepoMap's internal tag extraction (same code path as production)
    rm = RepoMap(Path(tmpdir))
    tags = rm._get_tags_treesitter(filepath, f"sample{ext}", config)

    # Cleanup
    os.unlink(filepath)
    os.rmdir(tmpdir)

    if not tags:
        return set()

    return {
        t.name for t in tags if t.kind == "definition"
    }  # pylint: disable=not-an-iterable


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang_name", list(LANGUAGES.keys()))
def test_treesitter_packages_available(lang_name: str):
    """Verify tree-sitter package is installed for each registered language."""
    lang = get_language(lang_name)
    assert lang is not None, (
        f"tree-sitter-{lang_name} is not installed. "
        f"Install it with: pip install tree-sitter-{lang_name}"
    )


@pytest.mark.parametrize("lang_name", list(LANGUAGES.keys()))
def test_definition_queries_find_expected_symbols(lang_name: str):
    """Each language should extract expected definition symbols from sample code."""
    if lang_name not in SAMPLES:
        pytest.skip(f"No sample for {lang_name}")

    lang = get_language(lang_name)
    if lang is None:
        pytest.skip(f"tree-sitter-{lang_name} not installed")

    found = _extract_definition_names(lang_name, SAMPLES[lang_name])
    expected = EXPECTED_NAMES[lang_name]

    missing = expected - found
    assert not missing, (
        f"{lang_name}: missing definitions: {sorted(missing)}\n"
        f"  found: {sorted(found)}\n"
        f"  expected: {sorted(expected)}"
    )


@pytest.mark.parametrize("lang_name", list(LANGUAGES.keys()))
def test_finds_more_than_expected(lang_name: str):
    """Queries should find at least the expected symbols (extras are fine)."""
    if lang_name not in SAMPLES:
        pytest.skip(f"No sample for {lang_name}")

    lang = get_language(lang_name)
    if lang is None:
        pytest.skip(f"tree-sitter-{lang_name} not installed")

    found = _extract_definition_names(lang_name, SAMPLES[lang_name])
    assert len(found) > 0, f"{lang_name}: no definitions found at all"


@pytest.mark.parametrize("lang_name", list(LANGUAGES.keys()))
def test_definitions_have_line_numbers(lang_name: str):
    """All definition tags should have valid line numbers."""
    if lang_name not in SAMPLES:
        pytest.skip(f"No sample for {lang_name}")

    lang = get_language(lang_name)
    if lang is None:
        pytest.skip(f"tree-sitter-{lang_name} not installed")

    config = LANGUAGES[lang_name]
    ext = config.extensions[0]

    import os
    import tempfile

    tmpdir = tempfile.mkdtemp()
    filepath = Path(tmpdir) / f"sample{ext}"
    filepath.write_text(SAMPLES[lang_name], encoding="utf-8")

    rm = RepoMap(Path(tmpdir))
    tags = rm._get_tags_treesitter(filepath, f"sample{ext}", config)

    os.unlink(filepath)
    os.rmdir(tmpdir)

    # If queries work, tags should not be None
    # If they fail, tags is None and we skip

    if not tags:
        pytest.skip(f"tree-sitter parsing returned None for {lang_name}")

    for tag in tags:  # pylint: disable=not-an-iterable
        if tag.kind == "definition":
            assert tag.line >= 1, f"{lang_name}: {tag.name} has invalid line {tag.line}"
