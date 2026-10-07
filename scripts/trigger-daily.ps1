<#
  trigger-daily.ps1  ——  外部兜底触发 GitHub Actions 的 Energy Daily 工作流
  ---------------------------------------------------------------------------
  用途：GitHub 自带 schedule(cron) 是"尽力而为"，可能延迟或静默丢弃。
        本脚本用 Windows「任务计划程序」在每天固定时间调用 GitHub API
        触发 workflow_dispatch，作为 100% 可靠的兜底。
        管线本身是幂等的（当天已生成则自动跳过），与 GitHub cron 双跑无害。

  前置：一枚 fine-grained PAT，仅授权本仓库，权限 Actions: Read and write
        （经典 token 亦可，勾 workflow scope）。切勿把 PAT 写进仓库。

  一次性注册（管理员 PowerShell）：
    $a = New-ScheduledTaskAction -Execute "powershell.exe" `
         -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\Projects\energy-daily\scripts\trigger-daily.ps1"'
    $t = New-ScheduledTaskTrigger -Daily -At 08:30
    Register-ScheduledTask -TaskName "AI答不锂·云端兜底触发" -Action $a -Trigger $t -RunLevel Highest

  手动测试：
    $env:GH_PAT = "<你的 PAT>"; powershell -File scripts\trigger-daily.ps1
#>

[CmdletBinding()]
param(
    [string]$Repo  = "zhencnli/energy-daily-aidabuli",
    [string]$Ref   = "main",
    [string]$WorkflowFile = "daily.yml",
    [string]$Token = $env:GH_PAT
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error "缺少 PAT。请设置环境变量 GH_PAT，或用 -Token 传入。"
    exit 2
}

$uri = "https://api.github.com/repos/$Repo/actions/workflows/$WorkflowFile/dispatches"
$headers = @{
    Authorization          = "Bearer $Token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "aidabuli-fallback-scheduler"
}
$body = @{ ref = $Ref } | ConvertTo-Json

try {
    Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body $body -ContentType "application/json" | Out-Null
    Write-Host "[OK] dispatched $WorkflowFile @ $Ref at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    exit 0
} catch {
    Write-Host "[FAIL] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -> $($_.Exception.Message)"
    exit 1
}
