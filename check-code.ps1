# Quick code quality check
param([switch]$Fix, [switch]$Fast)

Write-Host ""
Write-Host "========================================"
Write-Host "  Code Quality Check"
Write-Host "========================================"
Write-Host ""

$paths = "services/", "shared/", "subagents/", "hooks/"
$hasErrors = $false

# Black
Write-Host "[1/4] Black (Formatter)"
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
Write-Host "[2/4] isort (Imports)"
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
Write-Host "[3/4] Flake8 (Linter)"
$output = flake8 $paths 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  OK" -ForegroundColor Green
} else {
    $count = ($output | Measure-Object -Line).Lines
    Write-Host "  FAIL - $count issues" -ForegroundColor Red
    $output | Select-Object -First 5 | ForEach-Object { Write-Host "    $_" }
    $hasErrors = $true
}

# Mypy
if (-not $Fast) {
    Write-Host "[4/4] Mypy (Types)"
    mypy services/ shared/ subagents/ --ignore-missing-imports 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAIL - Type errors found" -ForegroundColor Red
        $hasErrors = $true
    }
} else {
    Write-Host "[4/4] Mypy - SKIPPED (fast mode)"
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
