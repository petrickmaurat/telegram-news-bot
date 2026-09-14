# Configuração manual do piloto V2

A V1 permanece na branch `main`. O piloto vive em `v2-claude-routines`, usa
estado separado e não possui comando de envio de e-mail.

## Antes do primeiro teste

1. Abra `https://claude.ai/code/routines` com a conta Claude Pro.
2. Confirme em Settings > Usage que uso adicional/extra está desativado.
   Assim, atingir o limite interrompe a Routine em vez de gerar cobrança extra.
3. Crie uma Routine e conecte o repositório `telegram-news-bot`.
4. Selecione explicitamente a branch `v2-claude-routines`.
5. Use execução manual/one-off no primeiro teste. Não crie agenda diária ainda.
6. No ambiente da Routine, habilite acesso de rede aos feeds e artigos usados
   pelo projeto. Para o piloto, não configure `ANTHROPIC_API_KEY`, `BREVO_API_KEY`,
   `EMAIL_REMETENTE` nem `EMAIL_DESTINO`: ele não precisa desses secrets.

## Prompt para colar na Routine

```text
Trabalhe somente na branch v2-claude-routines deste repositório. Leia e siga
ROUTINE_V2_INSTRUCTIONS.md. Este é um piloto sem envio: nunca execute
digest_email.py e não envie e-mail. Execute a preparação limitada a 60 novos
candidatos por tópico, faça o ranking e os resumos nos JSONs especificados,
valide as duas etapas e disponibilize routine_v2_preview.html. Informe o
resultado de routine_v2_report.json. Se a validação falhar, corrija a resposta;
não mude o validador nem relaxe as regras. Ao final, grave somente
routine_v2_state.json, routine_v2_google_cache.json, routine_v2_report.json e
routine_v2_preview.html em um commit na própria branch
v2-claude-routines e faça push. Não altere main nem arquivos digest_*.json.
```

## O que anotar no primeiro teste

Antes de executar, registre uma captura ou os números de uso do Claude em
Settings > Usage. Depois do run, anote novamente:

- percentual da janela de cinco horas, se exibido;
- percentual/quantidade do limite semanal, se exibido;
- horário inicial e final;
- quantidade `needs_evaluation`, `cached` e `counts` do relatório;
- se a prévia preencheu as vagas e se as matérias são editorialmente corretas.

O limite de 60 vale apenas para candidatos ainda sem cache em cada tópico.
Elegíveis em cache continuam no confronto, e nenhum item truncado é marcado como
rejeitado ou enviado. Portanto, um segundo piloto pode processar o restante.

## Critério para avançar

Não agende nem habilite envio após um único teste. Faça um segundo run manual em
outro dia para medir o custo com cache. Só avance se ambos forem verdadeiros:

1. a seleção e os resumos mantêm a qualidade do digest atual;
2. o consumo diário deixa margem confortável para seu uso normal do Claude Pro.

Depois disso, a próxima etapa de desenvolvimento adicionará envio e confirmação
de entrega à V2. Só então o cron da V1 deverá ser desativado, evitando dois e-mails.
