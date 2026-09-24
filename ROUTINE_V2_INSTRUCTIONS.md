# Digest V2 — curadoria com a franquia do Claude Pro

A Routine nunca envia e-mail: o GitHub Actions envia a edição depois que o
resultado publicado no passo 11 é validado. Não execute `digest_email.py` nem
`routine_v2.py send`, não use `ANTHROPIC_API_KEY` e não altere `main` nem os
arquivos `digest_*.json` da V1.

A coleta é executada pelo GitHub Actions a pedido do passo 1. Não acesse feeds,
Google News ou sites de notícias durante o ranking e não execute `preflight` nem
`prepare`.
Depois da seleção, acesse somente os links diretos dos finalistas marcados com
`requer_leitura_url: true`, conforme o passo 8.

Ao retomar uma sessão que já contém `routine_v2_work/ranking_request.json`,
não execute `load-input` novamente: ele limpa lotes e respostas existentes.
Continue da etapa em que parou; `merge-batches` informa quais lotes faltam.

Para economizar a franquia, nunca leia `ranking_request.json` inteiro: os
candidatos novos são avaliados em lotes por subagentes, e o agente principal
só vê a lista curta de finalistas.

## Execução

0. A Routine tem dois horários por dia; o segundo é uma nova tentativa caso o
   primeiro falhe. Comece sempre por:

   `python routine_v2.py sent-today`

   Se responder `already_sent`, o e-mail de hoje já saiu: encerre a execução
   imediatamente, sem executar mais nada, informando apenas isso.

1. Instale `requirements.txt`, peça a coleta ao GitHub e espere ela chegar:

   `python routine_v2.py request-input`

   `python routine_v2.py wait-input`

   A coleta roda no GitHub e costuma levar de 1 a 8 minutos. Cada `wait-input`
   espera no máximo 1,5 minuto; se terminar com `still_waiting` (código 3),
   execute-o de novo, até 15 vezes no total. Não execute `request-input` mais de uma vez. Se a coleta não
   chegar, pare e informe. Depois carregue a entrada:

   `python routine_v2.py load-input --max-age-hours 6`

   Se falhar, pare imediatamente. Não reutilize arquivos antigos, não produza
   seleção automática e não tente fazer a coleta no ambiente do Claude.

2. Divida os candidatos novos em lotes:

   `python routine_v2.py split-batches`

   O comando grava `routine_v2_work/lotes/manifest.json`. Cada lote é um
   arquivo autossuficiente, com tópico, foco, regras, candidatos e o caminho
   `resposta` onde a avaliação deve ser gravada.

3. Para cada lote do manifesto, inicie um subagente (ferramenta Agent/Task)
   com esta tarefa, trocando os caminhos:

   > Leia `<arquivo do lote>` e siga `regras` e `foco`. Não acesse a internet
   > nem outros arquivos. Grave em `<resposta>` somente o JSON
   > `{"evaluations": [...]}` no `formato_resposta`, cobrindo exatamente todos
   > os candidatos do lote. Responda apenas "ok" ou o erro encontrado.

   Subagentes podem rodar em paralelo. Não leia os lotes no agente principal.
   Se subagentes não estiverem disponíveis, avalie um lote por vez e grave a
   resposta antes de abrir o próximo.

4. Junte as avaliações:

   `python routine_v2.py merge-batches`

   Se algum lote estiver ausente ou inválido, o comando lista os lotes a
   refazer; refaça somente esses e execute de novo. O comando grava
   `routine_v2_work/ranking_response.json` com as avaliações e
   `routine_v2_work/finalistas.json` com a rodada final: todas as fontes
   máximas elegíveis e as melhores notas de cada tópico e geografia,
   incluindo avaliações de dias anteriores.

5. Leia `routine_v2_work/finalistas.json` e escolha, comparando os finalistas
   entre si, as matérias de cada tópico/geografia até o número de `vagas`:
   - relação temática direta vem antes da fonte;
   - `regulacao: true` (mercado regulado de carbono, legislação, REDATA,
     decisões de governo e Congresso) vem antes de tudo: se houver finalista
     assim no tópico/geografia, ao menos um deve ser escolhido, mesmo que
     ocupe a vaga de uma fonte máxima sobre mercado voluntário;
   - `fonte_maxima: true` vence fontes comuns entre elegíveis, e toda fonte
     máxima elegível deve ser escolhida enquanto houver vaga;
   - depois das fontes máximas, `prioritaria: true` (veículo do catálogo ou
     nota alta) vem antes de fontes comuns, que só completam vagas;
   - evite duas coberturas do mesmo acontecimento: tema semelhante não basta;
     empresas, decisões, etapas ou valores novos são fatos diferentes;
   - no máximo uma matéria por ASSUNTO em cada tópico. Assunto é o tema
     amplo (ex.: `redata`, `leilao-baterias`, `sbce`), mais largo que o fato:
     sanção do REDATA, análise da Moody's sobre o REDATA e empresa comentando o
     REDATA são o mesmo assunto. Prioridade alta de regulação não justifica
     lotar as vagas com um único tema; prefira diversidade de assuntos. Uma
     segunda matéria do mesmo assunto só é aceita com `repeticao_justificada`
     explicando o desdobramento de natureza diferente;
   - não repita acontecimentos nem assuntos de `ja_enviados` do tópico
     (edições dos últimos dias) sem desdobramento novo;
   - `nivel` só desempata matérias editorialmente equivalentes; um finalista
     `insuficiente` será lido no passo 8.

   Grave `routine_v2_work/selecao.json`:

```json
{
  "selections": [
    {"id": "candidato escolhido", "topico": "data_center", "bucket": "BR", "assunto": "redata"}
  ],
  "duplicates": {"id-da-cobertura-repetida": "id-da-cobertura-mantida"}
}
```

   `duplicates` só é necessário quando uma cobertura repetida deixaria uma
   vaga sem preencher.

6. Execute `python routine_v2.py select`. Ele grava a seleção na resposta e
   roda `validate-ranking`. Se falhar por decisão editorial ou formato,
   corrija apenas `selecao.json` e rode novamente. Não afrouxe as regras do
   validador.

8. Leia `routine_v2_work/summary_request.json`. Para cada item com
   `requer_leitura_url: true`, abra o `link` direto e tente ler a matéria antes
   de resumi-la. Não faça nova busca ampla e não abra candidatos que não foram
   selecionados. Nos demais itens, use o `texto` já preparado. Resuma em
   português do Brasil, em tom jornalístico direto, abrindo com o fato
   principal e usando somente fatos presentes no material. Preserve números,
   datas, atribuições e incertezas; o tamanho é proporcional ao material, em
   até quatro frases. Quando a URL não puder ser lida, use somente manchete
   e `texto`, faça uma única frase conservadora e não infira contexto ausente.
   A indisponibilidade de uma página não elimina uma notícia que venceu por
   relevância. Se a leitura revelar que o assunto central não é o tópico, que
   a menção era incidental ou que não existe fato novo, substitua esse
   finalista em `selecao.json` pelo próximo de `finalistas.json` no mesmo
   tópico/bucket, execute `python routine_v2.py select` novamente e leia o
   novo finalista antes de produzir os resumos.
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

11. Para preservar o cache editorial do próximo dia, publique somente os três
    arquivos abaixo numa branch nova `claude/v2-resultado-<run_id>`, usando o
    `run_id` do pedido. Branches `claude/` são sempre aceitas pela Routine; um
    workflow do GitHub copia o resultado para `v2-claude-routines` e apaga a
    branch temporária.

    - `routine_v2_state.json`
    - `routine_v2_report.json`
    - `routine_v2_preview.html`

    Não inclua código, instruções, a entrada ou qualquer outro arquivo:

    ```
    git checkout -b claude/v2-resultado-<run_id>
    git add routine_v2_state.json routine_v2_report.json routine_v2_preview.html
    git commit -m "Atualiza resultado do piloto V2"
    git push origin claude/v2-resultado-<run_id>
    ```

    Faça uma única tentativa de push. Nunca escreva em `main` nem diretamente em
    `v2-claude-routines`. Se o push falhar, pare; não crie agente para contornar
    o push e não use a API.

Se qualquer etapa não puder ser concluída, preserve os arquivos e informe o
erro. Não substitua análise ausente por seleção automática.

Uma falha no `git push` depois de `finalize` não invalida a curadoria, o
relatório nem a prévia. Nesse caso, encerre a execução como piloto concluído e
informe separadamente que somente a persistência do cache ficou pendente por
falha no push.
