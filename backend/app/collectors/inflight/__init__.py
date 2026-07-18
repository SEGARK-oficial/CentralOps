"""Classificação em voo (ADR-0015, Fase 1).

Regras avaliadas sobre UM evento, dentro do ciclo de coleta, emitindo
``Detection`` ANTES de o dado chegar ao SIEM. É o que torna a redução de volume
defensável: todo evento é classificado em fidelidade total; o que casa vai
íntegro para o destino de detecção, o que não casa pode ser amostrado.

**Não é correlação.** Sem estado entre eventos, sem janela, sem contagem, sem
cross-source, sem sequência. Isso é deliberado: correlação multi-evento exige um
substrato persistido (tabela de sinais + camada agendada) e foi orçada em 43-51
semanas-pessoa contra ~6 desta fase. Três lentes adversariais independentes
convergiram no corte.

Divisão dos dois módulos, que é o contrato deste pacote:

``matcher``
    PURO. Sem I/O, sem estado, sem log, sem métrica, sem ``await``. É o único
    código que roda POR EVENTO. Um guard estrutural em CI reprova qualquer
    import de redis/httpx/sqlalchemy aqui — a restrição R1 da ADR (zero I/O novo
    por evento) vira mecânica em vez de convenção.

``runtime``
    Tudo que toca o mundo: carga e compilação das regras (1x por ciclo, fora do
    laço), acumulação em memória dos matches, e o flush único no fim do ciclo.

O ``pipeline`` importa ``runtime`` de forma LAZY, dentro do ciclo, para que uma
org sem regras em voo não pague nem o custo do import.
"""
