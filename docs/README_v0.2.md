# BDTD — v0.2.0: piloto pela API

## Como executar no Windows

Extraia este ZIP em uma pasta nova. **Nesta versão, README.md, bdtd, conf e tests estão diretamente na raiz do ZIP**, sem a subpasta extra da versão anterior. Abra o PowerShell na pasta que contém `bdtd` e `README.md`.

Python 3.11+; sem bibliotecas externas. Python 3.13 é adequado. Os testes foram executados em Linux/Python 3.12, não nativamente no Windows.

Substitua o e-mail pelo contato real do responsável:

```powershell
python -m bdtd.api --contact "seu-email@dominio.com"
```

O comando tenta coletar **duas páginas**, com 20 registros por página, preservando respostas originais e gerando:

- `data/api-piloto/reports/api_pilot.json`: relatório para enviar na conversa.
- `data/api-piloto/staging/conferencia.csv`: títulos, autores, assuntos e links para conferência em Excel/VS Code.
- `data/api-piloto/staging/api_metadados.jsonl`: metadados estruturados e cópia do objeto original por registro.
- `data/api-piloto/raw/api/`: respostas originais, identificadas pelo SHA-256.
- `data/api-piloto/raw/api_manifest.jsonl`: páginas validadas e proveniência.
- `data/api-piloto/api.sqlite`: progresso e registros.
- `data/api-piloto/logs/http.jsonl`: log das requisições.

Repetir o comando não baixa novamente páginas concluídas. Para ampliar o piloto para até 10 páginas:

```powershell
python -m bdtd.api --contact "seu-email@dominio.com" --pages 5
```

`--pages` é o total desejado, contando páginas existentes. Nesta fase o teto é 10; não é o coletor em massa final. Use apenas um processo por diretório de dados.

## A consulta e o que está sendo validado

A captura enviada pelo usuário mostra:

- Assunto: **Linguagens OU Comunicação**.
- Tipos: **Dissertação OU Tese**.
- **21.707 resultados**, na captura de 11/09/2026.

O piloto usa `lookfor=Linguagens OR Comunicação`, `type=Subject`, os dois filtros `~format`, `page` e `limit` na API. **Essa tradução da busca avançada para os parâmetros da API é candidata e ainda precisa de verificação no servidor.** A resposta real anterior comprova `status`, `resultCount` e `records`; ela não comprova todos os parâmetros da nova consulta.

Não afirmamos que “Linguagens” e “Linguagem” sejam equivalentes. O plural foi preservado exatamente como na URL enviada. `join=AND` combina grupos da busca avançada; o grupo não vazio tem `bool0[]=OR`. Campos vazios e datas não preenchidas não foram transformados em critérios de busca.

O relatório informa se o total recebido coincide com a captura, mas **coincidir não prova equivalência dos conjuntos nem valida a área do enunciado**. Confira também títulos, assuntos e, se necessário, IDs e páginas na interface. Ordenação por relevância pode mudar; comparar a lista de primeiros títulos é um indício auxiliar.

A captura inclui, entre os primeiros títulos, um trabalho de robótica educacional cujo assunto menciona tecnologia da comunicação. Isso mostra por que pertinência à área exige avaliação adicional; o coletor não exclui automaticamente esse trabalho.

A quantidade de 120.841 do enunciado continua como referência de origem/critério ainda não esclarecidos. Não se busca reproduzir esse número por ajuste arbitrário de palavras.

## O que o piloto verifica

- JSON com `status=OK`, `resultCount` inteiro não negativo e registros com ID/título.
- Tipos de documento restritos a `masterThesis`/`doctoralThesis`.
- Ausência de IDs repetidos dentro e entre páginas.
- Total estável durante a execução e tamanho de página coerente com o solicitado.
- Separação entre configurações: mudar `conf/api-piloto.json` exige outro diretório de dados.
- HTTP conserva política de robots.txt, intervalo, backoff e interrupção diante de redirects/desafios.

Em erro, páginas já confirmadas ficam preservadas e o relatório registra o motivo. Respostas recebidas são arquivadas antes da validação; o manifesto lista apenas páginas aceitas, e o log registra requisições. A retomada usa SQLite; não é ainda reconstrução automática integral do banco a partir do Raw. Não apague o banco para “consertar” uma coleta.

O piloto não implementa paginação profunda, ordenação estável por ID, divisão temporal, esquema de snapshots ou coleta de PDFs. Mesmo duas páginas distintas não garantem cobertura integral de uma coleta longa. Esses itens dependem da validação desta etapa.

## Metadados e limites

O parser mantém autores, formatos, idiomas, assuntos e URLs, além do registro original. Assuntos em listas aninhadas são achatados para consulta; o original é preservado. Ausência de link é reportada, não classificada como ausência definitiva de PDF.

A busca anterior enviada tinha 20 registros e 3 sem links. Ela não fornecia resumos, datas ou licenças como campos próprios. O endpoint de detalhe/enriquecimento continua pendente; campos ausentes não são inventados. O piloto não extrai instituição a partir do prefixo de ID.

Saídas tabulares são JSONL e CSV nesta fase; Parquet, PDFs, Processed, produtos Curated e avaliação permanecem pendentes. CSV usa ponto e vírgula, BOM UTF-8 e proteção de fórmulas para inspeção; JSONL/Raw guardam os valores originais.

## Testes e demonstração

```powershell
python -m unittest discover -s tests -v
python -m bdtd demo
```

15 testes passaram nesta entrega. O parser também foi executado sobre os 20 registros reais que o usuário enviou, sem incluí-los no ZIP. A paginação HTTP foi testada com respostas simuladas. O acesso real à nova consulta ainda não foi validado deste ambiente.

O módulo OAI e os comandos antigos continuam disponíveis. Sua versão de parser é 0.1.0; o novo módulo API usa versão 0.2.0 e estado separado. `python -m bdtd status` refere-se ao módulo antigo; para o novo, consulte `api_pilot.json`. Não misture os diretórios ou formatos.

Histórico e explicação do plano: `docs/plano_explicado.md`, `docs/README_v0.1.md`, `docs/checkpoint.md`. Os documentos históricos descrevem a entrega anterior; este README descreve a versão atual.

## Próximo resultado necessário

Execute o piloto e envie **`data/api-piloto/reports/api_pilot.json`**. Se houver divergência, isso é resultado do diagnóstico, não motivo para iniciar coleta em massa. Depois de verificar a equivalência e o recorte, a próxima implementação será enriquecimento dos registros e resolução de links de PDFs.
