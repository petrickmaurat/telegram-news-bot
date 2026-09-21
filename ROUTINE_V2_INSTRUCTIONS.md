# Piloto V2 — curadoria com a franquia do Claude Pro

Este fluxo é deliberadamente incapaz de enviar e-mail. Não execute
`digest_email.py`, não use `ANTHROPIC_API_KEY`, não altere `main` nem os
arquivos `digest_*.json` da V1 e não habilite envio.

A coleta já foi executada pelo GitHub Actions. Não acesse feeds, Google News
ou sites de notícias nesta Routine e não execute `preflight` nem `prepare`.

Ao retomar uma sessão que já contém `routine_v2_work/ranking_request.json` e
`routine_v2_work/ranking_response.json`, não execute `load-input` novamente:
ele limpa a resposta existente. Preserve a mesma coleta e continue em
`validate-ranking` após atualizar apenas o código da branch V2.

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
   O GitHub já tentou resolver e ler cada candidato. `leitura.selecionavel`
   precisa ser `true` para que uma matéria possa entrar em `selections`.
   Prefira `leitura.nivel: artigo_completo`; use `trecho_disponivel` somente
   quando ele ainda sustentar um resumo fiel. `trecho_limitado` é reservado a
   uma fonte máxima e exige um resumo de uma frase, sem inferências. Itens
   `insuficiente` permanecem avaliados no cache, mas devem ser substituídos
   nesta edição.
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

7. Execute `python routine_v2.py validate-ranking`. Se falhar por decisão
   editorial ou formato, corrija apenas o JSON de resposta e rode a validação
   novamente. Não afrouxe as regras editoriais do validador.
   O validador recusará finalistas sem link direto ou material suficiente; não
   tente acessar os sites no ambiente do Claude e escolha o próximo elegível.
8. Leia `routine_v2_work/summary_request.json`. Resuma em português do Brasil,
   usando somente título e `texto`, preservando números, datas, atribuições e
   incertezas. Faça até quatro frases, proporcionalmente ao material disponível.
   Quando `base_resumo` for `trecho_disponivel`, seja especialmente conciso.
   Para `trecho_limitado`, use uma única frase. Nunca complete contexto que não
   esteja no texto fornecido.
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

Uma falha no `git push` depois de `finalize` não invalida a curadoria, o
relatório nem a prévia. Nesse caso, encerre a execução como piloto concluído e
informe separadamente que somente a persistência do cache ficou pendente por
falta de permissão de escrita no GitHub.
