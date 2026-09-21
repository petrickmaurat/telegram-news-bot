# Piloto V2 — curadoria com a franquia do Claude Pro

Este fluxo é deliberadamente incapaz de enviar e-mail. Não execute
`digest_email.py`, não use `ANTHROPIC_API_KEY`, não altere `main` nem os
arquivos `digest_*.json` da V1 e não habilite envio.

A coleta já foi executada pelo GitHub Actions. Não acesse feeds, Google News
ou sites de notícias durante o ranking e não execute `preflight` nem `prepare`.
Depois da seleção, acesse somente os links diretos dos finalistas marcados com
`requer_leitura_url: true`, conforme o passo 8.

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
   precisa ser `true` para que uma matéria possa entrar em `selections`; esse
   campo confirma que existe link direto, não que o scraping conseguiu ler a
   página. Julgue primeiro a relevância. `leitura.nivel` serve como indicador
   de qualidade e desempate entre notícias editorialmente equivalentes, nunca
   como motivo isolado para rejeitar uma notícia relevante. Um candidato
   `insuficiente` pode ser finalista e será lido no passo 8.
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
   O validador recusará somente finalistas sem link direto.
8. Leia `routine_v2_work/summary_request.json`. Para cada item com
   `requer_leitura_url: true`, abra o `link` direto e tente ler a matéria antes
   de resumi-la. Não faça nova busca ampla e não abra candidatos que não foram
   selecionados. Nos demais itens, use o `texto` já preparado. Resuma em
   português do Brasil, preservando números, datas, atribuições e incertezas,
   em até quatro frases. Quando a URL não puder ser lida, use somente manchete
   e `texto`, faça uma única frase conservadora e não infira contexto ausente.
   A indisponibilidade de uma página não elimina uma notícia que venceu por
   relevância. Se a leitura revelar que o assunto central não é o tópico, que
   a menção era incidental ou que não existe fato novo, volte ao
   `ranking_response.json`, substitua esse finalista pelo próximo elegível do
   mesmo tópico/bucket, execute `validate-ranking` novamente e leia o novo
   finalista antes de produzir os resumos.
9. Grave `routine_v2_work/summary_response.json`:

```json
{
  "summary_sha256": "copiar do pedido de resumos",
  "summaries": [
    {"id": "id selecionado", "resumo": "texto"},
    {"id": "finalista que exigia acesso", "resumo": "texto", "leitura_url": "confirmada"}
  ]
}
```

   Para todo item com `requer_leitura_url: true`, `leitura_url` é obrigatório:
   use `confirmada` quando conseguiu ler conteúdo útil na página e
   `indisponivel` quando o site bloqueou o acesso. Nesse segundo caso, o e-mail
   exibirá explicitamente que o resumo usou apenas manchete/trecho disponível.

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
