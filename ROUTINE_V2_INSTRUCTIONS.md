# Piloto V2 — curadoria com a franquia do Claude Pro

Este fluxo é deliberadamente incapaz de enviar e-mail. Não execute
`digest_email.py`, não use `ANTHROPIC_API_KEY`, não altere `main` nem os
arquivos `digest_*.json` da V1 e não habilite envio.

A coleta já foi executada pelo GitHub Actions. Não acesse feeds, Google News
ou sites de notícias nesta Routine e não execute `preflight` nem `prepare`.

## Execução do piloto

1. Instale `requirements.txt` e carregue a entrada preparada:

   `python routine_v2.py load-input --max-age-hours 6`

   Se falhar, pare imediatamente. Não reutilize arquivos antigos, não produza
   seleção automática e não tente fazer a coleta no ambiente do Claude.

2. Leia `routine_v2_work/ranking_request.json`. Analise somente candidatos
   com `precisa_avaliar: true`; use `avaliacao_cache` nos demais.
3. Trabalhe por tópico, em lotes de até 30. Em cada lote mantenha até dez
   elegíveis BR e dez US. Reúna os vencedores e repita em novos lotes de 30
   até restarem no máximo 30. Na escolha final, cumpra `limits`.
4. Relação temática direta vem antes da fonte. Menção incidental não basta.
   Use o `focus` de cada tema. Prioridade baixa não torna a notícia inelegível.
   Uma fonte com `fonte_maxima: true` vence fontes comuns entre matérias
   elegíveis, mas nunca transforma conteúdo fora do tema em elegível.
5. Evite duas coberturas do mesmo acontecimento. Tema semelhante não basta:
   empresas, decisões, etapas ou valores novos são fatos diferentes.
6. Grave `routine_v2_work/ranking_response.json` neste formato:

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

7. Execute `python routine_v2.py validate-ranking`. Se falhar, corrija apenas
   o JSON de resposta e rode a validação novamente. Não afrouxe o validador.
8. Leia `routine_v2_work/summary_request.json`. Resuma em português do Brasil,
   usando somente título e `texto`, preservando números, datas, atribuições e
   incertezas. Faça até quatro frases, proporcionalmente ao material disponível.
9. Grave `routine_v2_work/summary_response.json`:

```json
{
  "summary_sha256": "copiar do pedido de resumos",
  "summaries": [{"id": "id selecionado", "resumo": "texto"}]
}
```

10. Execute `python routine_v2.py finalize`. Informe os totais de
    `routine_v2_report.json`, liste títulos e fontes selecionados e disponibilize
    `routine_v2_preview.html`. Não envie e-mail.

11. Para preservar o cache editorial do próximo dia, permaneça na branch
    `v2-claude-routines` e confira que somente os três arquivos abaixo serão
    incluídos no commit:

    - `routine_v2_state.json`
    - `routine_v2_report.json`
    - `routine_v2_preview.html`

    Não inclua código, instruções, a entrada ou qualquer outro arquivo. Faça
    um commit com a mensagem `Atualiza resultado do piloto V2`, execute
    `git pull --rebase origin v2-claude-routines` e faça uma única tentativa de
    `git push origin HEAD:v2-claude-routines`. Nunca escreva em `main`. Se o
    push falhar, pare; não crie agente auxiliar e não tente contornar pela API.

Se qualquer etapa não puder ser concluída, preserve os arquivos e informe o
erro. Não substitua análise ausente por seleção automática.
