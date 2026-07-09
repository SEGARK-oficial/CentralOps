# Script de build otimizado para Windows PowerShell

$ImageName = if ($env:IMAGE_NAME) { $env:IMAGE_NAME } else { "centralops" }
$Dockerfile = "compose/Dockerfile"

Write-Host "Iniciando build otimizado com cache..." -ForegroundColor Cyan

try {
    Write-Host "Executando build..." -ForegroundColor Yellow

    docker build `
        --target final `
        --cache-from "${ImageName}:latest" `
        --cache-from "${ImageName}:frontend-deps" `
        --cache-from "${ImageName}:backend-deps" `
        -t "${ImageName}:latest" `
        -f $Dockerfile `
        .

    if ($LASTEXITCODE -ne 0) {
        throw "Erro no build da imagem principal"
    }

    Write-Host "Build concluido." -ForegroundColor Green

    Write-Host "Criando tags auxiliares de cache..." -ForegroundColor Yellow

    docker build `
        --target frontend-deps `
        -t "${ImageName}:frontend-deps" `
        -f $Dockerfile `
        .

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Cache do frontend criado." -ForegroundColor Green
    } else {
        Write-Host "Cache do frontend nao criado." -ForegroundColor Yellow
    }

    docker build `
        --target backend-deps `
        -t "${ImageName}:backend-deps" `
        -f $Dockerfile `
        .

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Cache do backend criado." -ForegroundColor Green
    } else {
        Write-Host "Cache do backend nao criado." -ForegroundColor Yellow
    }

    Write-Host "Build completo finalizado." -ForegroundColor Green
}
catch {
    Write-Host "Erro durante o build: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
