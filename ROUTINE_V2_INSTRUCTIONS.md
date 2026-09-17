# Piloto V2 — Claude Code Routine

Este fluxo é deliberadamente incapaz de enviar e-mail. Não execute
`digest_email.py`, não use `ANTHROPIC_API_KEY`, não altere os arquivos
`digest_*.json` da V1 e não habilite envio.

## Execução do piloto

1. Instale `requirements.txt` e execute primeiro:

   `python routine_v2.py preflight`

   Se falhar, pare imediatamente. Informe `routine_v2_preflight.json` e não
   execute coleta, ranking, resumo, commit ou qualquer nova chamada de rede.

2. Se o preflight passar, execute a coleta completa, sem limite artificial:

   `python routine_v2.py prepare --max-new-per-topic 0`

3. Leia `routine_v2_work/ranking_request.json`. Analise somente candidatos
   com `precisa_avaliar: true`; use `avaliacao_cache` nos demais.
4. Trabalhe por tópico, em lotes de até 30. Em cada lote mantenha até dez
   elegíveis BR e dez US. Reúna os vencedores e repita em novos lotes de 30
   até restarem no máximo 30. Na escolha final, cumpra `limits`.
5. Relação temática direta vem antes da fonte. Menção incidental não basta.
   Use o `focus` de cada tema. Prioridade baixa não torna a notícia inelegível.
   Uma fonte com `fonte_maxima: true` vence fontes comuns entre matérias
   elegíveis, mas nunca transforma conteúdo fora do tema em elegível.
6. Evite duas coberturas do mesmo acontecimento. Tema semelhante não basta:
   empresas, decisões, etapas ou valores novos são fatos diferentes.
7. Grave `routine_v2_work/ranking_response.json` neste formato:

```json
{
  "request_sha256": "copiar do pedido",
  "evaluations": [
    {"id": "somente candidato novo", "decisao": "fora_tema"},
    {"id": "somente candidato novo", "decisao": "elegivel", "bucket": "BR", "prioridade": 80, "fato": "id-curto-do-fato"}
  ],
  "selections": [
    {"id": "candidato escolhido", "topico": "data_center", "bucket": "BR"}
  ],
  "duplicates": {"id-da-cobertura-repetida": "id-da-cobertura-mantida"}
}
```

   `evaluations` deve cobrir exatamente todos os candidatos com
   `precisa_avaliar: true`. Decisões permitidas: `elegivel`, `fora_tema`,
   `sem_fato_novo`, `fonte_duvidosa`. Rejeitados levam somente `id` e decisão.

8. Execute `python routine_v2.py validate-ranking`. Se falhar, corrija apenas
   o JSON de resposta e rode a validação novamente. Não afrouxe o validador.
9. Leia `routine_v2_work/summary_request.json`. Resuma em português do Brasil,
   usando somente título e `texto`, preservando números, datas, atribuições e
   incertezas. Faça até quatro frases, proporcionalmente ao material disponível.
10. Grave `routine_v2_work/summary_response.json`:

```json
{
  "summary_sha256": "copiar do pedido de resumos",
  "summaries": [{"id": "id selecionado", "resumo": "texto"}]
}
```

11. Execute `python routine_v2.py finalize`. Termine informando os totais de
    `routine_v2_report.json` e disponibilize `routine_v2_preview.html`. Não envie e-mail.

12. Para publicar os artefatos, crie uma nova branch com prefixo `claude/`,
    por exemplo `claude/v2-pilot-AAAA-MM-DD-HHMM`. Nunca faça push direto para
    `v2-claude-routines` ou `main`. Grave somente `routine_v2_preflight.json`,
    `routine_v2_state.json`, `routine_v2_google_cache.json`,
    `routine_v2_report.json` e `routine_v2_preview.html`. Se o primeiro push
    falhar, pare; não crie agente auxiliar e não tente contornar pela API.

Se qualquer etapa não puder ser concluída, preserve os arquivos e informe o
erro. Não substitua análise ausente por seleção automática.
