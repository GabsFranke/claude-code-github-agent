# Quick code quality check
param([switch]$Fix, [switch]$Fast, [switch]$Verbose, [switch]$SkipMypy)

# Function to check if a command exists
function Test-Command {
    param($Command)
    $null -ne (Get-Command $Command -ErrorAction SilentlyContinue)
}

# Function to check and install required tools
function Install-RequiredTools {
    $missingTools = @()

    # Check for each required tool
    if (-not (Test-Command "black")) { $missingTools += "black" }
    if (-not (Test-Command "isort")) { $missingTools += "isort" }
    if (-not (Test-Command "flake8")) { $missingTools += "flake8" }
    if (-not (Test-Command "mypy")) { $missingTools += "mypy" }
    if (-not (Test-Command "ruff")) { $missingTools += "ruff" }

    # If any tools are missing, install them
    if ($missingTools.Count -gt 0) {
        Write-Host "Missing tools detected: $($missingTools -join ', ')" -ForegroundColor Yellow
        Write-Host "Installing development dependencies..." -ForegroundColor Yellow
        Write-Host ""

        # Check if python is available
        if (-not (Test-Command "python")) {
            Write-Host "Error: Python is not installed. Please install Python first." -ForegroundColor Red
            exit 1
        }

        # Install requirements-dev.txt using python -m pip
        if (Test-Path "requirements-dev.txt") {
            python -m pip install -r requirements-dev.txt
            Write-Host ""
            Write-Host "✓ Development dependencies installed successfully" -ForegroundColor Green
            Write-Host ""
        } else {
            Write-Host "Error: requirements-dev.txt not found" -ForegroundColor Red
            exit 1
        }
    }
}

# Check and install tools before running checks
Install-RequiredTools

Write-Host ""
Write-Host "========================================"
Write-Host "  Code Quality Check"
Write-Host "========================================"
Write-Host ""

$paths = "services/", "shared/", "subagents/", "hooks/", "plugins/", "tests/"
$hasErrors = $false

# Black
Write-Host "[1/5] Black (Formatter)"
if ($Fix) {
    black $paths | Out-Null
    Write-Host "  OK - Formatted" -ForegroundColor Green
} else {
    black --check $paths 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAIL - Run with -Fix" -ForegroundColor Red
        $hasErrors = $true
    }
}

# isort
Write-Host "[2/5] isort (Imports)"
if ($Fix) {
    isort $paths | Out-Null
    Write-Host "  OK - Organized" -ForegroundColor Green
} else {
    isort --check-only $paths 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAIL - Run with -Fix" -ForegroundColor Red
        $hasErrors = $true
    }
}

# Flake8
Write-Host "[3/5] Flake8 (Linter)"
$output = flake8 $paths 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  OK" -ForegroundColor Green
} else {
    $count = ($output | Measure-Object -Line).Lines
    Write-Host "  FAIL - $count issues" -ForegroundColor Red
    if ($Verbose) {
        $output | ForEach-Object { Write-Host "    $_" }
    } else {
        $output | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" }
        if ($count -gt 10) {
            Write-Host "    ... and $($count - 10) more (use -Verbose to see all)" -ForegroundColor Yellow
        }
    }
    $hasErrors = $true
}

# Mypy
if (-not $Fast -and -not $SkipMypy) {
    Write-Host "[4/5] Mypy (Types)"
    $mypyOutput = mypy services/ shared/ subagents/ --ignore-missing-imports 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAIL - Type errors found" -ForegroundColor Red
        if ($Verbose) {
            Write-Host ""
            Write-Host "Mypy Errors:" -ForegroundColor Yellow
            $mypyOutput | ForEach-Object { Write-Host "  $_" }
            Write-Host ""
        } else {
            $errorLines = $mypyOutput | Select-String "error:"
            $errorCount = ($errorLines | Measure-Object).Count
            Write-Host "  $errorCount type errors (use -Verbose to see details)" -ForegroundColor Yellow
        }
        $hasErrors = $true
    }
} else {
    if ($SkipMypy) {
        Write-Host "[4/5] Mypy - SKIPPED (use without -SkipMypy to enable)"
    } else {
        Write-Host "[4/5] Mypy - SKIPPED (fast mode)"
    }
}

# Ruff (fast linter)
Write-Host "[5/5] Ruff (Fast Linter)"
if ($Fix) {
    ruff check --fix $paths 2>&1 | Out-Null
    Write-Host "  OK - Fixed" -ForegroundColor Green
} else {
    $ruffOutput = ruff check $paths 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        $count = ($ruffOutput | Select-String "Found" | Select-Object -First 1)
        Write-Host "  FAIL - Issues found" -ForegroundColor Red
        if ($Verbose) {
            $ruffOutput | ForEach-Object { Write-Host "    $_" }
        } else {
            $ruffOutput | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" }
        }
        $hasErrors = $true
    }
}

# Summary
Write-Host ""
Write-Host "========================================"
if ($hasErrors) {
    Write-Host "  FAILED - Issues found" -ForegroundColor Red
    Write-Host "========================================"
    Write-Host ""
    Write-Host "To fix: .\check-code.ps1 -Fix" -ForegroundColor Yellow
    Write-Host ""
    exit 1
} else {
    Write-Host "  SUCCESS - All checks passed!" -ForegroundColor Green
    Write-Host "========================================"
    Write-Host ""
    exit 0
}
