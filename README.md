# Bot de notícias — Data Centers & Mercado de Carbono

Dois canais automáticos, rodando no GitHub Actions:

| Canal | Frequência | Conteúdo |
|---|---|---|
| **Telegram** (`telegram_news_bot.py`) | a cada 15 min | Notícias novas de **data center**, por palavras-chave e domínios permitidos |
| **E-mail** (`digest_email.py`) | 1x/dia, às 9h BRT | Digest curado por IA: até **3 do Brasil + 1 do exterior** por tema, com resumo |

## Onde configurar o quê

### Fontes e palavras-chave — `common.py`

`TOPICOS` define, para cada tema (`data_center`, `carbono`):

- **`keywords`** — a notícia precisa conter uma dessas expressões (título ou resumo) para ser considerada.
- **`feeds`** — lista de `{"url": ..., "origem": "BR" | "INT"}`. `origem` diz se o feed traz notícia do Brasil ou de fora (usado para separar "Brasil" e "Exterior").

Os domínios aceitos nos dois canais estão em `reliability.py`, em `DOMINIOS`.
O hostname precisa corresponder exatamente à lista (o prefixo `www.` é normalizado).
Subdomínios adicionais precisam ser cadastrados explicitamente. O nome declarado pelo RSS
não concede confiança à fonte. O Telegram continua sem curadoria por IA.

### Critério de seleção do digest — `digest_email.py`

- **`PROMPT_SELECAO`** — prioriza nível do veículo, montante financeiro ou impacto regulatório e depois o recorte setorial.
- **`VEICULOS_NIVEL_1` / `VEICULOS_NIVEL_2`** — referências editoriais; a admissão efetiva depende do domínio em `DOMINIOS`.
- **`FOCO_SETORIAL`** — o recorte de "setor elétrico" de cada tema.
- **`PROMPT_RESUMO`** — como o parágrafo de resumo é escrito.
- **`MAX_BR` / `MAX_US`** — quantas notícias por bucket (hoje 3 e 1).

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

A fila conserva candidatos mesmo depois que saem do RSS. O limite de 400 vale por lote/tema;
os ainda não avaliados têm prioridade na próxima execução. O e-mail só admite matérias publicadas
nas últimas **72 horas**, com tolerância de 15 minutos para relógios adiantados. A data vem de
`published` do RSS/Atom; `updated` não renova a idade. Sem data válida, a notícia não chega à IA
nem ao envio; pode receber uma data numa coleta posterior. Pendências expiram após sete dias
desde a coleta ou antes, quando a publicação sai da janela. Essas regras de data são exclusivas
do e-mail. Os limites ficam em `email_policy.py`.

Uma seleção válida `[]` rejeita o lote naquele tema. Erros da IA não publicam fallback e mantêm
os candidatos pendentes. Uma falha em data centers não impede tentar carbono, e vice-versa.
Se houver seleção válida em outro tema, ela é enviada; a execução sinaliza falha parcial no
Actions, preservando o histórico do que já foi enviado. Não selecionados continuam pendentes.

No **digest**, a coleta usa apenas o cache para links do Google (`resolver=False`). Links
inéditos são resolvidos somente após a seleção. O domínio final precisa estar na lista permitida,
e a deduplicação é repetida antes do envio. Se escolhas forem descartadas, há até três rodadas
de seleção por tema, com no máximo quatro escolhas por rodada. Falha de resolução preserva o
candidato para outra execução. O nome da fonte no RSS pode orientar a seleção preliminar, mas
nunca autoriza o envio sem a verificação do domínio. O **Telegram** mantém a resolução imediata.
URLs equivalentes
(Google/original, `www`, fragmentos e parâmetros de rastreamento conhecidos) compartilham
identidade. Notícias associadas aos dois temas participam de ambos até serem escolhidas.
A identificação do mesmo fato em textos/URLs diferentes ainda depende da curadoria da IA;
a normalização de URLs não garante deduplicação semântica.

Registros concluídos da fila perdem o título/resumo após sete dias e são removidos após 30 dias
do encerramento. A lista de aliases enviados em `digest_enviados.json` permanece para impedir
reenvios; registros descartados removidos não contornam o filtro de data se reaparecerem no RSS.

O HTML do e-mail escapa títulos, fontes, resumos, links e a data de edição. Links inválidos ou
fora dos domínios permitidos impedem a montagem. Resumos não têm mínimo obrigatório de palavras:
com menos de 20 palavras de apoio, o trecho/título é usado diretamente, sem expansão pela IA;
com mais conteúdo, o modelo recebe instruções para resumir somente fatos fornecidos.

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
