# Script para atualizar o pnpm-lock.yaml

Write-Host "🔄 Atualizando pnpm-lock.yaml..." -ForegroundColor Cyan

Push-Location "frontend"

try {
    # Verificar se pnpm está instalado
    if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
        Write-Host "📦 Instalando pnpm..." -ForegroundColor Yellow
        npm install -g pnpm
    }
    
    # Remover lockfile antigo se existir
    if (Test-Path "pnpm-lock.yaml") {
        Write-Host "🗑️  Removendo lockfile antigo..." -ForegroundColor Yellow
        Remove-Item "pnpm-lock.yaml" -Force
    }
    
    # Gerar novo lockfile
    Write-Host "📝 Gerando novo lockfile..." -ForegroundColor Yellow
    pnpm install
    
    Write-Host "✅ pnpm-lock.yaml atualizado com sucesso!" -ForegroundColor Green
    Write-Host "💡 Agora você pode executar o build novamente" -ForegroundColor Blue
}
catch {
    Write-Host "❌ Erro ao atualizar lockfile: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "💡 Tente limpar o cache e rodar novamente: docker compose -f compose/docker-compose.yml build --no-cache" -ForegroundColor Blue
}
finally {
    Pop-Location
}
