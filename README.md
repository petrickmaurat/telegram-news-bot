# Bot de notícias — Data Centers, Baterias & Mercado de Carbono

Dois canais automáticos, rodando no GitHub Actions:

| Canal | Frequência | Conteúdo |
|---|---|---|
| **Telegram** (`telegram_news_bot.py`) | a cada 15 min | Notícias novas de **data center**, por palavras-chave e domínios permitidos |
| **E-mail** (`digest_email.py`) | 1x/dia, às 7h17 BRT | Digest curado por IA, na ordem data centers → baterias → carbono, com resumo |

O Telegram pede explicitamente `topicos=["data_center"]`: novos temas entram só no e-mail.

## Onde configurar o quê

### Fontes e palavras-chave — `common.py`

`TOPICOS` define, para cada tema (`data_center`, `baterias`, `carbono`):

- **`keywords`** — a notícia precisa conter uma dessas expressões (título ou resumo) para ser considerada.
- **`feeds`** — lista de `{"url": ..., "origem": "BR" | "INT"}`. `origem` diz se o feed traz notícia do Brasil ou de fora (usado para separar "Brasil" e "Exterior").

O Telegram mantém a lista permitida de `reliability.py`, em `DOMINIOS`, e não usa IA.
No e-mail, os veículos de [FONTES_EMAIL.md](FONTES_EMAIL.md) têm prioridade de ordenação,
mas outros veículos também podem preencher vagas. O domínio final valida a prioridade;
um nome de fonte no RSS não basta. A configuração do e-mail fica em `email_sources.py`.

### Critério de seleção do digest — `digest_email.py`

- **`email_ranking.py` / `PROMPT`** — classifica e justifica cada candidata, sem limite por nota.
- **`email_sources.py` / `FONTES`** — catálogo único de veículos prioritários para o e-mail.
- **`FOCO_SETORIAL`** — o recorte editorial de cada tema. Em baterias: leilão e preço/custo no
  Brasil; preço/custo, inovação e fabricantes chinesas (CATL, BYD, EVE, Gotion, Hithium) fora.
  Texto fora de português/inglês é rejeitado como `fora_tema` — não buscamos conteúdo em chinês.
- **`PROMPT_RESUMO`** — como o parágrafo de resumo é escrito.
- **`VAGAS`** — quantas notícias por tema e bucket: data centers e carbono 3 BR + 1 Exterior;
  baterias 2 BR + 2 Exterior. `ORDEM_TOPICOS` define a ordem das seções no e-mail.

### Visual do e-mail — `digest_email.py`, função `montar_html`

O comentário acima da função descreve o objetivo visual. Cores por tema em `TEMA`.

### Horários — `.github/workflows/`

- `news.yml` linha `cron` — Telegram
- `digest.yml` linhas `cron` — digest (horário em UTC; BRT = UTC−3)

## Secrets necessários (GitHub → Settings → Secrets and variables → Actions)

| Secret | Para quê |
|---|---|
| `TELEGRAM_TOKEN` | bot do Telegram (@BotFather) |
| `CHAT_ID` | chat de destino no Telegram |
| `ANTHROPIC_API_KEY` | filtro + resumo do digest (console.anthropic.com) |
| `BREVO_API_KEY` | envio do e-mail (brevo.com) |
| `EMAIL_REMETENTE` | endereço verificado na Brevo |
| `EMAIL_DESTINO` | quem recebe o digest |

## Estado e prevenção de duplicatas

- `enviados.json` — links já mandados no Telegram
- `digest_enviados.json` — novos registros são apenas de envios confirmados; os registros legados continuam sendo respeitados
- `google_cache.json` — cache de links do Google Notícias já resolvidos
- `digest_fila.json` — candidatos pendentes, enviados, rejeitados por tema ou expirados
- `telegram_incerto.json` / `digest_incerto.json` — envio em andamento ou com resultado incerto; `null` indica canal liberado

A fila conserva candidatos mesmo depois que saem do RSS. Todos os candidatos são avaliados em
lotes de até 30 (rodada base, com cache por conteúdo, prompt, foco editorial e modelo — um lote malformado não
derruba o tópico, só fica pendente pra próxima execução). Se os elegíveis dessa rodada passarem
de 30, é um torneio: até dez melhores por geografia em cada lote avançam para uma nova rodada de
comparação direta entre vencedores, sem cache, repetindo até caber numa chamada só — essa rodada
final decide a ordem entre os finalistas com o contexto completo dos rivais. Quem não avança não é
descartado: continua elegível com a nota da última rodada em que participou, disponível como
reserva. Detalhes em `email_ranking.py`.
O e-mail só admite matérias publicadas
nas últimas **72 horas**, com tolerância de 15 minutos para relógios adiantados. A data vem de
`published` do RSS/Atom; `updated` não renova a idade. Sem data válida, a notícia não chega à IA
nem ao envio; pode receber uma data numa coleta posterior. Pendências expiram após sete dias
desde a coleta ou antes, quando a publicação sai da janela. Essas regras de data são exclusivas
do e-mail. Os limites ficam em `email_policy.py`.

Prioridade ordena, não exclui: o código preenche as vagas de cada tema (`VAGAS`) quando
há candidatos suficientes, atuais, inéditos, com links válidos e de fatos distintos. Não existe
corte mínimo de nota. Valores financeiros, regulação e setor elétrico aumentam a nota, mas sua
ausência não rejeita uma notícia do tema. Notícias de veículos não prioritários completam vagas.
A geografia é a do fato: CNN Brasil sobre Emirados é Exterior; Reuters sobre Brasil é BR.
Cada candidata precisa de decisão e motivo. Só há rejeição editorial por fora do tema, ausência
de informação substantiva/fato novo ou evidência de fonte duvidosa. Array vazio ou índices
faltantes são falhas técnicas, não rejeições. Uma falha em data centers não impede tentar carbono.
Se houver seleção válida em outro tema, ela é enviada; a execução sinaliza falha parcial no
Actions, preservando o histórico do que já foi enviado. Não selecionados continuam pendentes.

No **digest**, a coleta usa apenas o cache para links do Google (`resolver=False`). Cada execução
também consulta Google Notícias por `site:` para CADA veículo prioritário e CADA tema, com quatro
consultas simultâneas e registro de resultados/falhas. Isso complementa os feeds existentes;
não garante cobertura de páginas que o Google não indexa ou disponibiliza no RSS.
Os links dos elegíveis são resolvidos na ordem do ranking até preencher as vagas com as melhores
fontes; reservas continuam sendo usadas após falha/duplicidade, sem o antigo limite de três rodadas.
Não são resolvidos links que já não podem superar vagas preenchidas por fontes prioritárias.
Falha de resolução preserva o candidato para outra execução. O **Telegram** mantém a resolução imediata.
URLs equivalentes
(Google/original, `www`, fragmentos e parâmetros de rastreamento conhecidos) compartilham
identidade. Notícias associadas aos dois temas participam de ambos até serem escolhidas.
A identificação do mesmo fato em textos/URLs diferentes depende primeiro da curadoria da IA
(campo "fato"); quando veículos diferentes cobrem o mesmo acontecimento em lotes ou execuções
que o modelo nunca viu juntos, o "fato" pode divergir. A similaridade lexical agora só identifica pares para confirmação semântica, conforme os ajustes abaixo.

Registros concluídos da fila perdem o título/resumo após sete dias e são removidos após 30 dias
do encerramento. A lista de aliases enviados em `digest_enviados.json` permanece para impedir
reenvios; registros descartados removidos não contornam o filtro de data se reaparecerem no RSS.

O HTML do e-mail escapa títulos, fontes, resumos, links e a data de edição. Links inválidos ou
Google Notícias não resolvidos impedem a montagem. Resumos não têm mínimo obrigatório de palavras:
com menos de 20 palavras de apoio, aparece um aviso de resumo indisponível, sem repetir a manchete;
com mais conteúdo, o modelo recebe instruções para resumir somente fatos fornecidos.

### Por que uma notícia não entrou?

Cada e-mail inclui `curadoria.txt`, com resultado e motivo por notícia e fontes consultadas.
O mesmo diagnóstico fica em `digest_auditoria.json`, versionado no GitHub e nos artefatos do
workflow, inclusive quando nada foi enviado. O resultado distingue: selecionada, sem vaga,
duplicada, já enviada, fora da janela, sem data, rejeição editorial e falha técnica.
As avaliações recentes rejeitadas pela regra anterior voltam a ser consideradas. A justificativa
antiga não pode ser recuperada: não foi registrada. Os novos motivos correspondem à nova avaliação.
O relatório é uma justificativa editorial estruturada da IA, não comprovação independente de seus juízos.

Os workflows compartilham um grupo de concorrência por branch, preservam a fila de execuções
e fazem checkout da versão atual da branch. No Actions, `BOT_PERSIST_STATE=1` grava um marcador
no remoto **antes** de cada POST e persiste o resultado depois. Se o push anterior ao POST
falhar, o envio não acontece. Há até três tentativas de push e uma etapa final de persistência
mesmo em falhas. Artefatos de recuperação ficam disponíveis por 14 dias quando a execução falha.
Essa proteção gera commits adicionais por envio. Evite executar cópias locais ou branches
diferentes simultaneamente contra os mesmos destinatários: o lock do Actions é por branch.

### Recuperar um envio incerto

Timeout, interrupção ou resposta ambígua podem ocorrer depois de o serviço aceitar um envio.
O canal fica bloqueado para não repetir automaticamente. Confira a mensagem no Telegram ou
o assunto/horário nos logs transacionais da Brevo; o arquivo `*_incerto.json` contém os itens.
Em um checkout atualizado, execute apenas a opção que corresponde ao resultado verificado:

```sh
python resolve_delivery.py telegram --entregue
python resolve_delivery.py telegram --nao-entregue
# Para e-mail, substitua telegram por digest.
```

Depois faça commit/push dos arquivos de estado alterados antes de reexecutar o workflow.
`--entregue` registra os aliases como enviados; `--nao-entregue` permite uma nova tentativa.
Se não for possível confirmar o resultado, mantenha o bloqueio. O script não envia mensagens.
Os históricos antigos não permitem distinguir descartes de envios reais; a migração não apaga
esses registros, evitando uma onda de reenvios. Notícias descartadas pelo código antigo não
são recuperadas automaticamente.

## Testes

```sh
python -m unittest discover -s tests
```

Os testes simulam APIs e usam estado temporário; não enviam mensagens nem consomem IA.
Também rodam antes do processamento nos workflows.

## Rodar localmente

```
pip install -r requirements.txt
TELEGRAM_TOKEN=... CHAT_ID=... python telegram_news_bot.py
ANTHROPIC_API_KEY=... BREVO_API_KEY=... EMAIL_REMETENTE=... EMAIL_DESTINO=... python digest_email.py
```

### Ajustes da revisão editorial
- O torneio continua em lotes de 30. Até dez candidatos de cada geografia avançam por lote; finalistas precedem reservas mesmo quando a nota final é menor. As reservas podem completar vagas após exclusões.
- O cache da avaliação base considera conteúdo, prompt, foco editorial e modelo. Avaliações das rodadas finais não contaminam esse cache. Falha ao renovar uma avaliação não autoriza usar a avaliação antiga.
- Similaridade de títulos e identificadores de fato indicam pares para confirmação semântica por IA. Empresas diferentes ou novos desdobramentos devem ser preservados. A comparação é reutilizada dentro da seleção; falha técnica preserva o candidato e aparece no log.
- Baterias valida o idioma do título/trecho RSS localmente com Lingua antes do ranking. Aceita português e inglês. Texto ambíguo fica pendente; o idioma do artigo completo não é verificado nesta etapa.
- Buscas do Google por tema seguem para a IA mesmo sem palavra-chave no trecho. Feeds gerais continuam com o filtro de palavras. BYD, EVE Energy, Gotion, Hithium e tecnologias de células ampliam as buscas exclusivas do e-mail.
- A coleta aproveita o resumo mais completo ao mesclar o mesmo link. A auditoria por feed mostra capturados, mesclados, fora das palavras-chave e problemas de link.
- Consultas site: dependem da indexação do Google e não garantem capturar tudo que um veículo publica. O histórico abaixo ajuda a identificar buscas que precisam ser conferidas.


### Recuperação de artigos e acompanhamento das fontes

Quando falta data de publicação ou o resumo RSS tem menos de 20 palavras, o digest consulta o artigo antes do ranking. São até 20 tentativas por execução, com intervalo de seis horas para repetir uma tentativa incompleta. Itens ainda não consultados vêm primeiro; enviados e notícias já fora da janela não são buscados. Os demais continuam na fila e participam da seleção quando têm dados suficientes.

A data é recuperada de metadados explícitos de publicação (Open Graph ou JSON-LD de artigo), com fuso horário. Datas de atualização não servem; a data válida do RSS é preservada. Sem data confiável, a notícia continua pendente. O texto extraído é reutilizado no ranking, na validação de idioma e no resumo, evitando uma segunda consulta na mesma execução. A auditoria registra resultado e motivo da tentativa.

O arquivo `digest_fontes.json`, persistido pelo GitHub Actions, mantém até 30 dias ou 120 execuções. O anexo `curadoria.txt` mostra, por fonte/consulta e tema:
- consultas feitas, falhas, resultados RSS, capturas e itens mesclados;
- notícias elegíveis e selecionadas na fila;
- sugestões para conferir feeds com muitas falhas, buscas vazias por três execuções ou filtros que eliminam quase todos os resultados.

Os totais medem ocorrências por consulta, não publicações únicas: a mesma notícia pode aparecer em várias execuções e feeds, e uma seleção pode ser atribuída a mais de uma origem. "Selecionada" também não é confirmação de entrega. Busca vazia não prova que o site não publicou: serve como sinal para conferir indexação, termos e RSS direto. Nenhuma fonte é removida automaticamente.

O ranking exige saída estruturada da API: códigos de decisão e geografia e notas inteiras de 0 a 100. Há uma tentativa de recuperação de formato; avaliações válidas são preservadas e armazenadas em cache mesmo quando parte do lote continua inválida. Os lotes permanecem com até 30 candidatos. O agendamento diário é às 07h17 de Brasília (10h17 UTC), inclusive fins de semana, sujeito a atrasos do GitHub Actions.

### Prioridade máxima no digest
Brazil Journal, MegaWhat, Valor Econômico (incluindo Pipeline), Agência iNFRA e eixos têm preferência absoluta entre candidatos elegíveis dos três temas. Primeiro preenchem as vagas da sua geografia; demais fontes completam as vagas restantes. Se excederem a quantidade de vagas, o torneio e a nota ordenam as matérias desses veículos. Tema, idioma aplicável, janela de 72 horas e deduplicação continuam obrigatórios. Brasil Energia mantém a prioridade anterior, sem promoção. A prioridade vale também para reservas e links resolvidos do Google; estes podem exigir mais resoluções para identificar o veículo corretamente. Não há garantia de captura integral de um site. Telegram inalterado.

### Barreira de tema antes da prioridade de fonte
Antes de classificar ou ordenar, o digest exige uma referência ao tema no título/trecho disponível, sem considerar URL, nome do veículo ou justificativas inventadas pela IA. Sem essa evidência, o candidato permanece pendente com `sem_evidencia_tema`. A IA deve avaliar se a ligação é direta e substantiva; a presença de uma palavra, isoladamente, não garante elegibilidade. A classificação anterior é invalidada pela nova versão da regra.

Os casos Light (recuperação judicial) e Brookfield (baterias sem vínculo explícito com data centers) não entram em Data Centers. Brookfield pode concorrer em Baterias; transmissão e conexão de data centers continuam elegíveis. Boletins de múltiplos assuntos sem trecho suficiente ficam pendentes, inclusive quando a URL original só é descoberta ao resolver o Google. A regra não impede todo erro semântico, mas bloqueia as associações genéricas observadas na curadoria antes da prioridade máxima.

Quando a Anthropic informa saldo insuficiente, o processamento interrompe novas chamadas e não envia a edição parcial. O histórico de envios fica intacto, os candidatos permanecem na fila e a auditoria registra `saldo_insuficiente`. Outras falhas transitórias mantêm o isolamento por tópico. Nenhuma recarga é automática.

Falhas comuns de resumo usam texto disponível identificado como “Trecho da fonte”, ou aviso de resumo indisponível quando falta conteúdo. A manchete não é aceita como resumo gerado.

### Economia de tokens e prévia offline

Rejeitados retornam apenas índice e código de decisão; a curadoria traduz o código
em descrição padronizada, sem atribuir à IA uma justificativa individual inexistente.
Elegíveis mantêm nota, geografia, fato e justificativa curta. URLs opacas do Google
não vão ao prompt, mas continuam preservadas na fila e nos aliases.

Antes do ranking, cópias só são unificadas com URL original confirmada, título e data
iguais, e trecho contido no da versão mais completa. Conteúdos divergentes, links
distintos e falhas de resolução preservam os candidatos. Nenhum corte de fontes,
vagas ou volume foi adicionado. Registros das cópias continuam na fila e na auditoria.

O cache base mantém sua assinatura anterior. O cache de rodadas exige os mesmos
concorrentes, ordem, conteúdo, contexto, tema, foco, modelo e versão editorial.
Somente lotes completos e válidos são salvos; dados alterados exigem nova avaliação.
Cada item mantém até oito resultados de rodadas por tópico; a remoção de cache só
provoca nova consulta. Para alterar apenas o formato de resposta, edite
`prompt_compacto`/schema; mudanças de critérios editoriais devem alterar `PROMPT`
ou `VERSAO`, invalidando avaliações antigas de propósito.

`digest_auditoria.json`, em `uso_ia`, registra tokens observados por chamada, etapa,
tópico (quando a chamada pertence a um só tema), run e tentativa do Actions. Linhas
`USO_IA` também aparecem no log. O custo é estimado pelas tarifas padrão de Haiku
4.5, não substitui o extrato da Anthropic. Chamadas sem uso disponível, erros e
tentativas internas do SDK não são apresentados como custo conhecido zero.

Para testar layout sem coleta, IA, envio ou alteração de histórico:

```sh
python digest_email.py --previa --saida _preview.html
```

Após um envio confirmado, `digest_ultima_edicao.json` guarda os campos de exibição
e é persistido pelo workflow. A prévia usa o mesmo renderizador do e-mail. Para
edições anteriores, pode reconstruir a seleção da última auditoria usando apenas
notícias enviadas e resumos preservados na fila; nesse caso a ordem dentro dos
grupos pode diferir. Se faltarem dados, falha sem acionar processamento ao vivo.
Não é necessário configurar chaves para a prévia; as dependências Python normais
do projeto precisam estar instaladas.

O JSON de entrada usa separadores compactos, preservando todos os valores. As
comparações de manchetes compartilham somente respostas booleanas válidas entre
tópicos da mesma execução, para o mesmo modelo, instrução e par exato. Falhas não
são compartilhadas como decisões editoriais; o cache é reiniciado a cada run.
