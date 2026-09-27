# Script para atualizar o package-lock.json do frontend

Write-Host "🔄 Atualizando package-lock.json..." -ForegroundColor Cyan

Push-Location "frontend"

try {
    # Regenera o lockfile a partir do package.json (npm resolve tudo de novo,
    # honrando os ranges de semver — equivalente ao antigo fluxo do pnpm).
    Write-Host "📝 Gerando novo lockfile..." -ForegroundColor Yellow
    npm install

    Write-Host "✅ package-lock.json atualizado com sucesso!" -ForegroundColor Green
    Write-Host "💡 Agora você pode executar o build novamente" -ForegroundColor Blue
}
catch {
    Write-Host "❌ Erro ao atualizar lockfile: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "💡 Tente limpar o cache e rodar novamente: docker compose -f compose/docker-compose.yml build --no-cache" -ForegroundColor Blue
}
finally {
    Pop-Location
}
