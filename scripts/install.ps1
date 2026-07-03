$ErrorActionPreference = "Stop"

if (Get-Command pipx -ErrorAction SilentlyContinue) {
  pipx install reidcli
} else {
  python -m pip install --user reidcli
}

Write-Host "reidcli installed. Run: reidcli"
