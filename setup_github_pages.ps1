# ackman-tracker → GitHub 레포 생성 + 푸시 + GitHub Pages 활성화
#
# 실행:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_github_pages.ps1
#
# GitHub PAT 를 물어봅니다 (화면에 표시되지 않음).
# 필요한 스코프: repo, workflow   (fine-grained 라면 Contents/Workflows/Pages: Read and write)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding            = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$GH_USER = "jinhae8971"
$GH_REPO = "ackman-tracker"
$GH_MAIL = "jinhae8971@gmail.com"

# --------------------------------------------------------------- 토큰
if ($env:GH_TOKEN) {
    $GH_TOKEN = $env:GH_TOKEN
} else {
    $sec = Read-Host "GitHub PAT (repo + workflow 스코프)" -AsSecureString
    $GH_TOKEN = [System.Net.NetworkCredential]::new("", $sec).Password
}
if (-not $GH_TOKEN) { Write-Host "토큰이 필요합니다." -ForegroundColor Red; exit 1 }

$REMOTE_URL = "https://$GH_TOKEN@github.com/$GH_USER/$GH_REPO.git"
$API_HDR = @{
    "Authorization" = "token $GH_TOKEN"
    "Accept"        = "application/vnd.github+json"
    "User-Agent"    = "AckmanTrackerDeploy"
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# --------------------------------------------------------------- [1] Git
git config --global --add safe.directory ($ScriptDir -replace '\\','/') 2>$null
if (-not (Test-Path ".git")) { git init | Out-Null }
$prev = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
git remote remove origin 2>$null | Out-Null
$ErrorActionPreference = $prev
git remote add origin $REMOTE_URL
git config user.name  $GH_USER
git config user.email $GH_MAIL
Write-Host "[1] Git 초기화 OK" -ForegroundColor Green

# --------------------------------------------------------------- [2] 레포
# Pages 무료 호스팅은 public 레포에서 동작한다 (Private Pages 는 유료 플랜 전용).
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO" -Headers $API_HDR | Out-Null
    Write-Host "[2] 레포 이미 존재" -ForegroundColor Green
} catch {
    try {
        $body = @{
            name        = $GH_REPO
            private     = $false
            auto_init   = $false
            description = "Bill Ackman (Pershing Square) 13F/13D 포지션 추적 — SEC 공시 기반, 서버 0대"
        } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" `
            -Headers $API_HDR -Body $body -ContentType "application/json" | Out-Null
        Write-Host "[2] 레포 생성 (public)" -ForegroundColor Green
        Start-Sleep -Seconds 2
    } catch {
        Write-Host "[2] 수동 생성 필요: https://github.com/new (이름: $GH_REPO, Public)" -ForegroundColor Red
        Read-Host "레포 생성 후 Enter"
    }
}

# --------------------------------------------------------------- [3] 푸시
$ErrorActionPreference = "SilentlyContinue"
git add .
git commit -m "feat: Ackman 13F/13D 추적 시스템 초기 배포 (collector/analytics/pipeline/dashboard)" 2>$null
if ($LASTEXITCODE -ne 0) { git commit --allow-empty -m "chore: 재배포" 2>$null }
git branch -M main
git push -u origin main --force 2>$null
$pushCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pushCode -ne 0) {
    Write-Host "[3] 푸시 실패 — 토큰에 repo + workflow 스코프가 필요합니다." -ForegroundColor Red
    Write-Host "    https://github.com/settings/tokens/new" -ForegroundColor White
    git remote set-url origin "https://github.com/$GH_USER/$GH_REPO.git"
    exit 1
}
Write-Host "[3] 푸시 OK" -ForegroundColor Green

# --------------------------------------------------------------- [4] Pages 활성화
# build_type = "workflow" 로 지정해야 Actions 배포(deploy-pages)가 동작한다.
$pagesUrl = "https://api.github.com/repos/$GH_USER/$GH_REPO/pages"
$pagesBody = @{ build_type = "workflow" } | ConvertTo-Json
try {
    Invoke-RestMethod -Method Post -Uri $pagesUrl -Headers $API_HDR `
        -Body $pagesBody -ContentType "application/json" | Out-Null
    Write-Host "[4] Pages 활성화 (source = GitHub Actions)" -ForegroundColor Green
} catch {
    try {
        Invoke-RestMethod -Method Put -Uri $pagesUrl -Headers $API_HDR `
            -Body $pagesBody -ContentType "application/json" | Out-Null
        Write-Host "[4] Pages 설정 갱신" -ForegroundColor Green
    } catch {
        Write-Host "[4] 수동 설정: https://github.com/$GH_USER/$GH_REPO/settings/pages" -ForegroundColor Yellow
        Write-Host "    Source 를 'GitHub Actions' 로 선택하세요." -ForegroundColor White
        Read-Host "설정 후 Enter"
    }
}

# --------------------------------------------------------------- [5] 워크플로우 실행
try {
    Invoke-RestMethod -Method Post `
        -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO/actions/workflows/build-dashboard.yml/dispatches" `
        -Headers $API_HDR -Body '{"ref":"main"}' -ContentType "application/json" | Out-Null
    Write-Host "[5] build-dashboard 워크플로우 트리거" -ForegroundColor Green
} catch {
    Write-Host "[5] 수동 실행: https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
}

# --------------------------------------------------------------- [6] 정리
git remote set-url origin "https://github.com/$GH_USER/$GH_REPO.git"
Remove-Variable GH_TOKEN -ErrorAction SilentlyContinue
$env:GH_TOKEN = $null

Write-Host ""
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host " 완료 — 2~3분 후 아래 주소에서 대시보드 확인" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host " 대시보드 : https://$GH_USER.github.io/$GH_REPO/" -ForegroundColor White
Write-Host " 레포      : https://github.com/$GH_USER/$GH_REPO" -ForegroundColor White
Write-Host " 실행 상태 : https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
Write-Host ""
Write-Host " (remote URL 에서 토큰 제거됨)" -ForegroundColor DarkGray
