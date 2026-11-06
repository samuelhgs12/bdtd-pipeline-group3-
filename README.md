# BDTD — coleta de PDFs de teses e dissertações por eixo temático

Este programa consulta a API de busca da BDTD (bdtd.ibict.br), coleta os metadados de todas as teses e dissertações de uma consulta e tenta baixar os PDFs indicados pelos repositórios de origem. A coleta é retomável: pode ser interrompida e reiniciada com o mesmo comando, sem repetir o que já foi feito.

O eixo temático (os termos pesquisados) fica num arquivo JSON em `conf/`. Para mudar de tema, basta criar um JSON novo; o código não precisa mudar.

Requisitos: Python 3.11 ou superior, sem bibliotecas externas. Os comandos abaixo são para o PowerShell, na pasta que contém este README.

## 1. Defina o seu eixo

Copie o exemplo e edite a cópia:

```powershell
Copy-Item conf\exemplo-linguagem-comunicacao.json conf\meu-eixo.json
```

O exemplo pesquisa *Linguagem* ou *Comunicação* no assunto ou no resumo, somente em dissertações e teses:

```json
{
  "endpoint": "https://bdtd.ibict.br/vufind/api/v1/search",
  "join": "AND",
  "groups": [
    {
      "operator": "OR",
      "clauses": [
        {"lookfor": "Linguagem", "type": "Subject"},
        {"lookfor": "Linguagem", "type": "Abstract"},
        {"lookfor": "Comunicação", "type": "Subject"},
        {"lookfor": "Comunicação", "type": "Abstract"}
      ]
    }
  ],
  "filters": [
    "~format:\"masterThesis\"",
    "~format:\"doctoralThesis\""
  ],
  "limit": 20
}
```

O que mudar:

- **`clauses`**: um item por termo e campo. `lookfor` é o termo; `type` é o campo. `Subject` (assunto) e `Abstract` (resumo) foram validados neste projeto. Outros campos da API (por exemplo `Title` ou `AllFields`) devem ser conferidos com a validação do passo 2 antes da coleta.
- **`operator`**: `OR` aceita qualquer cláusula do grupo; `AND` exige todas.
- **Mais de um grupo**: cada grupo é um parêntese, e `join` liga os grupos. Exemplo para *(Educação no assunto OU no resumo) E (Inclusão no assunto)*:

  ```json
  "groups": [
    {"operator": "OR", "clauses": [
      {"lookfor": "Educação", "type": "Subject"},
      {"lookfor": "Educação", "type": "Abstract"}]},
    {"operator": "OR", "clauses": [
      {"lookfor": "Inclusão", "type": "Subject"}]}
  ]
  ```

- **`filters`**: os dois filtros do exemplo restringem a dissertações (`masterThesis`) e teses (`doctoralThesis`). Mantenha-os, a menos que queira outros tipos de documento.
- Não altere `endpoint` nem `limit`.
- Campos extras, como `"scope": "descrição do recorte"`, podem ser acrescentados como anotação; o programa os ignora na consulta.

Confira a quantidade de resultados: faça a mesma busca na busca avançada do site da BDTD e compare com o total que a validação do passo 2 informa (`root_total`).

**Regra importante:** cada eixo usa a sua própria pasta de dados (`--data-dir`). O programa grava uma assinatura da consulta na pasta e se recusa a misturar consultas: se o JSON mudar (qualquer campo), use uma pasta nova.

## 2. Valide a consulta

Use um e-mail real de contato; ele vai no cabeçalho das requisições, como pedem os repositórios.

```powershell
python -X utf8 -m bdtd.collect --contact "seu-email@dominio.com" --config conf\meu-eixo.json --data-dir data\meu-eixo --validate-only
```

A validação faz poucas consultas reais e confere se os filtros de ano, instituição e complementos somam o total da consulta. O resultado fica em `data\meu-eixo\reports\partition_validation.json`. Se aparecer divergência, a coleta não começa.

## 3. Colete

```powershell
python -X utf8 -m bdtd.collect --contact "seu-email@dominio.com" --config conf\meu-eixo.json --data-dir data\meu-eixo --target-pdfs 5000
```

Para retomar, repita exatamente o mesmo comando. Opções úteis (todas têm padrão):

| Opção | Padrão | Efeito |
|---|---|---|
| `--target-pdfs` | 5000 | Para ao atingir esse número de PDFs candidatos únicos (máximo 5000). |
| `--max-hours` | 8 | Duração máxima desta execução, incluindo esperas. |
| `--budget-gb` | 20 | Máximo transferido nesta execução. |
| `--reserve-gb` | 10 | Espaço livre mínimo no disco; abaixo disso a coleta para. |
| `--batch-gb` | 1 | Tamanho aproximado de cada lote fechado. |
| `--rate-limit-wait-minutes` | 60 | Espera quando a BDTD responde HTTP 429 sem dizer quanto esperar. |
| `--max-rate-limit-waits` | 4 | Quantas esperas por 429 cada execução aceita. |
| `--repository-retry-hours` | 24 | Intervalo antes de tentar de novo um registro que falhou. |
| `--max-records` | sem limite | Máximo de registros tentados nesta execução (útil para um teste curto). |

Para um primeiro teste curto: `--max-records 20 --max-hours 0.5`.

O terminal mostra o início e o fim de cada etapa e de cada download, e a cada 30 segundos repete o estado atual (fila, PDFs únicos, MiB, falhas). `Ctrl+C` interrompe com segurança; o que já terminou fica salvo.

Use **um único processo por pasta de dados**.

### Como a coleta funciona

A API da BDTD só entrega as primeiras 1.000 posições de cada busca. Por isso a consulta é dividida em partições: um ano por partição, mais "outros valores de ano" e "sem ano". Um ano com mais de 1.000 resultados é subdividido por instituição. Se uma subpartição ainda passar de 1.000, as primeiras 1.000 posições são coletadas e a partição fica marcada como parcial em `reports/collection.json` (`inaccessible_records` diz quantos registros ficaram de fora).

Para cada registro, o programa segue os links do metadado: links diretos de PDF, páginas de repositório (DSpace, `citation_pdf_url`, links `.pdf`) e redirecionamentos, com até 12 URLs por registro. Respeita o `robots.txt` de cada site, espera pelo menos um segundo entre requisições ao mesmo domínio e não tenta contornar login, embargo ou verificação de navegador.

Os PDFs são identificados pelo SHA-256: o mesmo arquivo baixado por dois registros é guardado uma vez só. PDFs institucionais óbvios dos menus dos repositórios (política de privacidade, política de informação, termos de uso) não contam para a meta.

## Onde ficam os resultados

```text
data\meu-eixo\
  collection.sqlite              estado da coleta (não apague)
  metadata\records.jsonl         metadados de todos os registros encontrados
  metadata\rejections.jsonl      registros recusados (sem ID, ou sem título e sem URL)
  pdfs\raw\pdf\<xx>\<sha>.pdf    PDFs baixados, nomeados pelo SHA-256
  pdfs\raw\manifest_pdf.jsonl    resultado por registro: arquivos, URLs, tentativas
  pdfs\reports\pdf_pilot.json    resumo dos downloads
  batches\batch-NNNN\            lotes fechados (~1 GiB) com PDFs, metadados e checksums
  reports\collection.json        progresso consolidado e motivo da última parada
  logs\                          requisições HTTP
```

Para saber a que trabalho pertence um PDF, procure o SHA-256 em `pdfs\raw\manifest_pdf.jsonl` ou no `manifest.jsonl` do lote.

Os lotes são fechados automaticamente quando há cerca de 1 GiB pendente, e ao final da coleta. Um lote fechado não muda mais. Para conferir os lotes e preparar a cópia para outro lugar (por exemplo o Drive):

```powershell
python -X utf8 -m bdtd.batches --data-dir data\meu-eixo --verify
python -X utf8 -m bdtd.batches --data-dir data\meu-eixo --prepare-transfer
```

O segundo comando grava `reports\batch_transfer_plan.json` com o hash de cada arquivo. Depois de copiar um lote e conferir a cópia, registre a transferência; assim o programa não baixa de novo os PDFs daquele lote se a cópia local for apagada:

```powershell
python -X utf8 -m bdtd.batches --data-dir data\meu-eixo --mark-transferred 1 --transfer-plan data\meu-eixo\reports\batch_transfer_plan.json --external-location "Drive/pasta"
```

## Como ler o resultado de cada registro

| Status | Significado |
|---|---|
| `sucesso` | Pelo menos um arquivo passou na verificação básica de PDF. |
| `sem_link_origem` | O metadado não tinha URL. |
| `nao_obtido` | Nenhum PDF válido; veja os motivos nas tentativas. |
| `http_404`, `http_403` etc. | Resposta HTTP do endereço tentado. |
| `robots_bloqueado` | O `robots.txt` do site proíbe a coleta. |
| `robots_indisponivel` | Não foi possível confirmar o `robots.txt`. |
| `sem_link_pdf_no_html` | Nenhum link de PDF encontrado na página; não prova que o PDF não exista. |
| `pdf_invalido_ou_incompleto` | A resposta não tem assinatura `%PDF-` e marcador `%%EOF`. |
| `verificacao_navegador` | O site exigiu verificação de navegador. |
| `limite_arquivo`, `limite_execucao`, `disco_reserva` | Um limite configurado foi atingido; o registro é retomado depois. |
| `limited: true` | Ainda havia candidatos na fila ou um limite foi atingido. |

Limites do que é verificado: a checagem de PDF é básica (assinatura, marcador final e tamanho anunciado). Ela não prova que o arquivo é a tese, nem distingue corpo principal de anexos; um registro pode ter vários PDFs (`possible_extra_candidate_files`). A pertinência temática de cada trabalho ao eixo também não é verificada: o programa coleta o que a consulta retorna.

## Testes

```powershell
python -m unittest discover -s tests -v
```

Os testes usam respostas simuladas e não acessam a rede.
