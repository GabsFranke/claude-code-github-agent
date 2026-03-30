#!/bin/bash
# Quick code quality check

set -e

# Check if required tools are installed
check_and_install_tools() {
    local missing_tools=()

    # Determine Python command
    PYTHON_CMD="python"
    if ! command -v python &> /dev/null; then
        if command -v python3 &> /dev/null; then
            PYTHON_CMD="python3"
        else
            echo "Error: Python is not installed. Please install Python first."
            exit 1
        fi
    fi

    # Check for each required tool using python -m
    if ! $PYTHON_CMD -m black --version &> /dev/null; then
        missing_tools+=("black")
    fi
    if ! $PYTHON_CMD -m isort --version &> /dev/null; then
        missing_tools+=("isort")
    fi
    if ! $PYTHON_CMD -m flake8 --version &> /dev/null; then
        missing_tools+=("flake8")
    fi
    if ! $PYTHON_CMD -m mypy --version &> /dev/null; then
        missing_tools+=("mypy")
    fi
    if ! $PYTHON_CMD -m ruff --version &> /dev/null; then
        missing_tools+=("ruff")
    fi

    # If any tools are missing, install them
    if [ ${#missing_tools[@]} -gt 0 ]; then
        echo "Missing tools detected: ${missing_tools[*]}"
        echo "Installing development dependencies..."
        echo ""

        # Install requirements-dev.txt
        if [ -f "requirements-dev.txt" ]; then
            $PYTHON_CMD -m pip install -r requirements-dev.txt
            echo ""
            echo "✓ Development dependencies installed successfully"
            echo ""
        else
            echo "Error: requirements-dev.txt not found"
            exit 1
        fi
    fi
}

# Check and install tools before running checks
check_and_install_tools

# Determine Python command for running tools
PYTHON_CMD="python"
if ! command -v python &> /dev/null; then
    if command -v python3 &> /dev/null; then
        PYTHON_CMD="python3"
    else
        echo "Error: Python is not installed. Please install Python first."
        exit 1
    fi
fi

# Parse arguments
FIX=false
FAST=false
VERBOSE=false
SKIP_MYPY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --fix)
            FIX=true
            shift
            ;;
        --fast)
            FAST=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --skip-mypy)
            SKIP_MYPY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--fix] [--fast] [--verbose] [--skip-mypy]"
            exit 1
            ;;
    esac
done

echo ""
echo "========================================"
echo "  Code Quality Check"
echo "========================================"
echo ""

PATHS="services/ shared/ subagents/ hooks/ plugins/ tests/"
HAS_ERRORS=false

# Black
echo "[1/5] Black (Formatter)"
if [ "$FIX" = true ]; then
    $PYTHON_CMD -m black $PATHS > /dev/null 2>&1
    echo "  ✓ OK - Formatted"
else
    if $PYTHON_CMD -m black --check $PATHS > /dev/null 2>&1; then
        echo "  ✓ OK"
    else
        echo "  ✗ FAIL - Run with --fix"
        HAS_ERRORS=true
    fi
fi

# isort
echo "[2/5] isort (Imports)"
if [ "$FIX" = true ]; then
    $PYTHON_CMD -m isort $PATHS > /dev/null 2>&1
    echo "  ✓ OK - Organized"
else
    if $PYTHON_CMD -m isort --check-only $PATHS > /dev/null 2>&1; then
        echo "  ✓ OK"
    else
        echo "  ✗ FAIL - Run with --fix"
        HAS_ERRORS=true
    fi
fi

# Flake8
echo "[3/5] Flake8 (Linter)"
OUTPUT=$($PYTHON_CMD -m flake8 $PATHS 2>&1) || true
if [ -z "$OUTPUT" ]; then
    echo "  ✓ OK"
else
    COUNT=$(echo "$OUTPUT" | wc -l)
    echo "  ✗ FAIL - $COUNT issues"
    if [ "$VERBOSE" = true ]; then
        echo "$OUTPUT" | sed 's/^/    /'
    else
        echo "$OUTPUT" | head -10 | sed 's/^/    /'
        if [ $COUNT -gt 10 ]; then
            echo "    ... and $((COUNT - 10)) more (use --verbose to see all)"
        fi
    fi
    HAS_ERRORS=true
fi

# Mypy
if [ "$FAST" = false ] && [ "$SKIP_MYPY" = false ]; then
    echo "[4/5] Mypy (Types)"
    MYPY_OUTPUT=$($PYTHON_CMD -m mypy services/ shared/ subagents/ --ignore-missing-imports 2>&1) || true
    if echo "$MYPY_OUTPUT" | grep -q "Success:"; then
        echo "  ✓ OK"
    else
        echo "  ✗ FAIL - Type errors found"
        if [ "$VERBOSE" = true ]; then
            echo ""
            echo "Mypy Errors:"
            echo "$MYPY_OUTPUT" | sed 's/^/  /'
            echo ""
        else
            ERROR_COUNT=$(echo "$MYPY_OUTPUT" | grep -c "error:" || echo "0")
            echo "  $ERROR_COUNT type errors (use --verbose to see details)"
        fi
        HAS_ERRORS=true
    fi
else
    if [ "$SKIP_MYPY" = true ]; then
        echo "[4/5] Mypy - SKIPPED (use without --skip-mypy to enable)"
    else
        echo "[4/5] Mypy - SKIPPED (fast mode)"
    fi
fi

# Ruff
echo "[5/5] Ruff (Fast Linter)"
if [ "$FIX" = true ]; then
    $PYTHON_CMD -m ruff check --fix $PATHS > /dev/null 2>&1 || true
    echo "  ✓ OK - Fixed"
else
    RUFF_OUTPUT=$($PYTHON_CMD -m ruff check $PATHS 2>&1) || true
    if echo "$RUFF_OUTPUT" | grep -q "All checks passed"; then
        echo "  ✓ OK"
    else
        echo "  ✗ FAIL - Issues found"
        if [ "$VERBOSE" = true ]; then
            echo "$RUFF_OUTPUT" | sed 's/^/    /'
        else
            echo "$RUFF_OUTPUT" | head -10 | sed 's/^/    /'
        fi
        HAS_ERRORS=true
    fi
fi

# Summary
echo ""
echo "========================================"
if [ "$HAS_ERRORS" = true ]; then
    echo "  FAILED - Issues found"
    echo "========================================"
    echo ""
    echo "To fix: ./check-code.sh --fix"
    echo ""
    exit 1
else
    echo "  SUCCESS - All checks passed!"
    echo "========================================"
    echo ""
    exit 0
fi
