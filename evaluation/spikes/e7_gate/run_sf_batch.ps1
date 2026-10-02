# E7 gate spike batch: SiliconFlow small models vs deepseek-flash baseline
# NOTE: keep this file ASCII-only -- Windows PowerShell 5.1 reads BOM-less .ps1 as GBK.
# Usage:
#   powershell -ExecutionPolicy Bypass -File run_sf_batch.ps1 -Mode smoke   # --limit 1, no report
#   powershell -ExecutionPolicy Bypass -File run_sf_batch.ps1 -Mode full    # full run + reports
param([string]$Mode = "smoke")

# locate repo root from script path (script sits at AI-Customer-Service/evaluation/spikes/e7_gate/)
Set-Location (Join-Path $PSScriptRoot "..\..\..")
$py = Join-Path $PSScriptRoot "..\..\..\..\.venv\Scripts\python.exe"

# candidate set (full mode)
#  - Qwen3-8B: smoke showed thinking-chain pollution (48s / 1352 tok); --enable-thinking off fixes it
#  - Qwen/Qwen3.5-4B excluded: latency collapse 21s~101s in smoke (server-side scheduling), fails speed goal
$models = @(
    @{ name = "Qwen/Qwen2.5-7B-Instruct"; opts = @() },
    @{ name = "Qwen/Qwen3-8B"; opts = @("--enable-thinking", "off") },
    @{ name = "Qwen/Qwen3-30B-A3B-Instruct-2507"; opts = @() },
    @{ name = "THUDM/GLM-4-9B-0414"; opts = @() }
)
$common = @("--base-url", "https://api.siliconflow.cn/v1", "--key", "embedding")

foreach ($m in $models) {
    $opts = $m.opts
    Write-Host "`n========== $($m.name) ($Mode) =========="
    if ($Mode -eq "smoke") {
        & $py -X utf8 -m evaluation.spikes.e7_gate.run_spike_api --model $m.name @common @opts --limit 1 --no-report
    }
    else {
        & $py -X utf8 -m evaluation.spikes.e7_gate.run_spike_api --model $m.name @common @opts
    }
}

if ($Mode -eq "full") {
    Write-Host "`n========== deepseek-flash (v2 baseline) =========="
    & $py -X utf8 -m evaluation.spikes.e7_gate.run_spike_api
}
Write-Host "`n== batch done =="
