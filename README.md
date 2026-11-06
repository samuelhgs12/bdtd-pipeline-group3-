# BDTD — v0.4.0: coleta particionada de PDFs candidatos

## Escopo da entrega final

A entrega final contém somente os dez lotes fechados `batch-0001` a `batch-0010`, com **1.737 PDFs candidatos únicos por SHA-256**. A coleta obteve **1.766 candidatos únicos**; por decisão de escopo, os **29 PDFs residuais** associados ao conjunto provisório `batch-0011` não integram esta entrega.

Os 29 residuais, seus metadados e os inventários anteriores permanecem preservados localmente como histórico da coleta. A exclusão da entrega não significa rejeição temática ou técnica desses arquivos e não altera `collection.json`. Os totais de coleta e de entrega são apresentados separadamente em `data/coleta-5000/reports/delivery_1737_summary.json`.

O inventário específico para transferência é `data/coleta-5000/reports/delivery_1737_inventory.json`, acompanhado por `delivery_1737_inventory.json.sha256`. A documentação mantém as cinco rejeições de metadados, as sinalizações de qualidade, os possíveis candidatos extras e o documento institucional conhecido; esses registros descrevem a coleta completa e não aumentam a contagem de PDFs entregues.

## Coleta de até 5.000 PDFs

A consulta desta etapa mantém os quatro campos explícitos:

```text
(Subject:Linguagem OR Abstract:Linguagem OR
 Subject:Comunicação OR Abstract:Comunicação)
AND (format:masterThesis OR format:doctoralThesis)
```

No PowerShell, na raiz do projeto, inicie ou retome com o mesmo comando:

```powershell
C:\Python314\python.exe -X utf8 -m bdtd.collect --contact (git config user.email) --config conf\api-validacao-linguagem-comunicacao.json --data-dir data\coleta-5000 --target-pdfs 5000 --reserve-gb 10 --batch-gb 1 --budget-gb 20 --max-hours 8 --rate-limit-wait-minutes 60 --max-rate-limit-waits 4 --repository-retry-hours 24 --import-metadata data\validacao-linguagem-comunicacao\api\staging\api_metadados.jsonl --import-metadata data\validacao-linguagem-comunicacao\sample-accessible\selected_50.jsonl --import-metadata examples\piloto_40_metadados.jsonl --reuse-pdf-dir data\validacao-linguagem-comunicacao\pdf-sample-50 --reuse-pdf-dir data\pdf-piloto
```

Use somente um processo nesse diretório. `Ctrl+C`, a reserva de disco, o orçamento de transferência e o limite de tempo encerram a execução de forma retomável. Em HTTP 429 sem `Retry-After`, o processo espera 60 minutos e tenta novamente, no máximo quatro vezes por execução. O comando exige um e-mail real em `git config user.email`, mantém no mínimo 10 GiB livres e transfere no máximo 20 GiB em cada execução de até oito horas. A meta exclui documentos institucionais conhecidos e deduplica PDFs por SHA-256.

`--max-hours` começa a contar na inicialização e inclui validação, downloads e esperas por HTTP 429. Durante uma espera, o limite é conferido em intervalos de no máximo 30 segundos e a API não é consultada depois do prazo. Se já houver registros na fila persistida, seus PDFs são tentados antes da API e também quando um 429 inicia; a limitação da BDTD não pausa os repositórios de destino. Cada repositório continua sujeito ao próprio `robots.txt`, intervalo por domínio, redirecionamentos e controles de acesso.

As opções repetíveis `--import-metadata` aceitam somente registros cujo URL de origem comprova a consulta atual ou a consulta anterior restrita a `Subject`, que é subconjunto lógico dela. IDs são deduplicados e cada associação de origem fica em `record_sources`. `--reuse-pdf-dir` verifica novamente o SHA-256, cria hardlink quando possível (ou copia) e preserva a associação do manifesto anterior. Isso permite processar a fila local antes de concluir a validação das partições.

Registros concluídos com falha só voltam a ser elegíveis após `--repository-retry-hours` (24 horas por padrão). O limite evita repetir imediatamente as mesmas falhas ao reiniciar; registros ainda inéditos continuam sendo processados.

Um 429 da API não é salvo em `api_cache`: somente JSON válido com status de sucesso entra nesse cache. O log `logs/api_http.jsonl` diferencia `source=network` de `source=cache`; novos erros registram `Retry-After`, `Content-Type` e um trecho limitado do corpo, sem cookies. O prazo de retomada fica em `api_cooldowns` no SQLite e continua valendo após reiniciar. Se `Retry-After` não vier na resposta, `--rate-limit-wait-minutes` é uma política conservadora do cliente. O `robots.txt` válido da BDTD é reaproveitado por até 24 horas para evitar a consulta redundante em cada retomada.

O terminal informa o início e o fim das etapas e de cada URL baixada, além de fila, trabalhos tentados, trabalhos com PDF, PDFs únicos, MiB no Raw e falhas observadas. Um heartbeat repete o estado real pelo menos a cada 30 segundos. Em espera, ele mostra o motivo e o horário local previsto para retomar a API.

Antes de colher metadados, o comando valida com respostas reais os filtros de ano, instituição, complementos e ausência de ano. A descoberta usa as facetas `publishDate` e `institution`. Os 30 valores de ano expostos pela API viram partições exatas; outro valor com o campo presente e registros sem ano recebem partições complementares explícitas. Partições anuais acima da janela de 1.000 posições são subdivididas pelas 30 instituições expostas e por um complemento negativo. Subpartições que ainda excedem 1.000 são coletadas até a janela, têm a repetição detectada e ficam registradas como parciais. IDs são deduplicados entre partições sem apagar suas associações de proveniência.

Respostas API são arquivadas por SHA-256, e o cache evita repetir consultas concluídas. O arquivo `data/coleta-5000/collection.sqlite` guarda páginas, partições, conflitos e retomada; `metadata/records.jsonl` é a fila incremental. Os PDFs e tentativas ficam em `pdfs/`, e o progresso consolidado fica em `reports/collection.json`.

Lotes fechados aparecem em `data/coleta-5000/batches/batch-NNNN/`. Cada lote contém PDFs, metadados, resultados, manifesto com caminhos relativos, `checksums.sha256` e `CLOSED.json`. Eles são fechados em torno de 1 GiB sem dividir arquivos, verificados antes da publicação no diretório final e não são alterados depois. Os Raw permanecem no lugar. Nenhum envio ao Drive é feito pelo programa.

Esta etapa não extrai texto, não anonimiza dados, não gera Curated e não treina modelos.

## Piloto anterior de downloads

## Execute no Windows

Extraia o ZIP em **uma pasta nova**. Abra o PowerShell na pasta que contém este README, `bdtd`, `conf` e `examples`.

O pacote já contém os **40 metadados reais enviados pelo usuário**, em `examples/piloto_40_metadados.jsonl`. Não precisa copiar arquivos da versão anterior. Nenhum PDF é incluído no ZIP.

Python 3.11 ou superior; sem bibliotecas externas. Substitua o e-mail pelo seu contato:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com"
```

O comando tenta **até oito registros por execução**, na ordem do arquivo de entrada. Não é amostra estatisticamente representativa: é um piloto técnico que inclui links diretos, páginas institucionais e um registro sem URL. Default: 100 MiB por resposta e 500 MiB transferidos por execução (inclui HTML/robots; pode exceder até um bloco de 64 KiB ao interromper). O total em disco pode crescer entre execuções; não há buffer rotativo nem exclusão automática de PDFs.

Ao terminar, envie **`data/pdf-piloto/reports/pdf_pilot.json`**.

Para continuar os próximos oito registros, repita o mesmo comando. Registros com resultados anteriores são pulados, salvo sucesso cujo arquivo esteja ausente/corrompido. Para incluir falhas e sucessos parciais anteriores na nova tentativa:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com" --retry-failed
```

Para tentar até 40 registros numa execução:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com" --limit 40
```

`--limit` limita os registros tentados **nesta execução**, diferente de `--pages` do piloto API. Limite máximo por execução: 50 registros. O orçamento de transferência pode interromper antes de terminar todos. Use `--retry-failed` para retomar tentativas afetadas por esse limite. PDFs íntegros já obtidos são mantidos, e candidatos pendentes têm prioridade. Cada resposta interrompida é reiniciada; não há HTTP Range para continuar de um byte parcial.

Para seu próprio arquivo de metadados:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com" --input "caminho\api_metadados.jsonl" --data-dir data/pdf-outro-snapshot
```

Não reutilize o diretório de saída com entrada diferente. O hash do snapshot é verificado. Use **um processo por diretório**. Ctrl+C preserva resultados já concluídos; a execução seguinte retoma. Temporários de uma interrupção abrupta podem ficar em `tmp`; não são contados como PDFs obtidos.

## O que foi implementado

- Links diretos para arquivos, sem confiar apenas em extensão `.pdf`.
- Redirecionamentos HTTP normais, incluindo links Handle; máximo de oito saltos, com verificação de robots.txt em cada origem de destino.
- Leitura de páginas HTML: `citation_pdf_url`, links `.pdf`, links de bitstreams DSpace e referências explícitas a conteúdo PDF.
- Até oito candidatos por página, uma etapa de descoberta a partir da página inicial e até 12 URLs por registro.
- Download em streaming, timeout, intervalo mínimo de um segundo por host e respeito a Crawl-delay/Request-rate quando interpretados.
- Tentativas limitadas em 429/5xx, backoff e Retry-After. Esperas acima de 60 segundos são reportadas para retomada posterior.
- Identificação por SHA-256; conteúdo idêntico ocupa um único objeto Raw e pode estar associado a vários registros.
- Cache de respostas e retomada em SQLite. Em `--retry-failed`, páginas HTML são consultadas novamente; PDFs válidos em cache podem ser reutilizados.
- Falhas ao consultar `robots.txt` são reutilizadas apenas durante a execução atual, evitando requisições repetidas ao mesmo domínio. Uma nova execução verifica a política outra vez.
- Logs e tentativas novos distinguem a fase de política da fase do documento e registram a URL/status da política consultada.
- Objetos cujo checksum divergiu são preservados em `raw/corrupt/` antes da recuperação da cópia válida.
- Variantes conhecidas do mesmo bitstream DSpace são deduplicadas antes do download; respostas idênticas ainda recebidas são contabilizadas separadamente.
- Cada execução e registro recebem identificadores correlacionáveis nos novos logs HTTP.
- Metatags bibliográficas reconhecidas são preservadas em `staging/metadata_html.jsonl`, sem inventar campos ausentes.

São seguidos apenas links recebidos em metadados, HTML e redirecionamentos. Não se adivinham IDs ou endpoints de bitstreams. Páginas que dependem de JavaScript/API DSpace 7 específica ainda podem ficar sem resolução; o motivo é registrado. Não há navegador automatizado, resolução de desafios, login, quebra de embargo ou desativação de TLS.

## Saídas

```text
data/pdf-piloto/
  raw/input/<sha>.jsonl        snapshot dos metadados enviados
  raw/pdf/<prefix>/<sha>.pdf  PDFs obtidos
  raw/html/                   páginas de origem
  raw/robots/                 políticas consultadas com sucesso
  raw/other/                  respostas de outros formatos, quando houver
  raw/invalid_pdf/            respostas PDF suspeitas/incompletas, quando houver
  raw/corrupt/                objetos divergentes preservados durante recuperação
  raw/manifest_pdf.jsonl       resultado detalhado por registro
  staging/metadata_html.jsonl metatags bibliográficas encontradas
  reports/pdf_pilot.json      resumo para enviar na conversa
  logs/pdf_http.jsonl         requisições e erros HTTP
  pdf_state.sqlite           estado, cache e histórico de tentativas
  tmp/                       arquivos temporários
```

Os PDFs têm nomes por hash. Para saber qual arquivo corresponde a cada trabalho, consulte `raw/manifest_pdf.jsonl`: há `record_id`, `title`, `files[].path`, URL final, tamanho e SHA-256.

Não apague SQLite: a reconstrução automática integral do estado a partir de Raw ainda não foi implementada. O manifesto reflete o último resultado por registro; tentativas anteriores são mantidas no banco.

## Como interpretar o resultado

| Status/campo | Significado |
|---|---|
| `sucesso` | Pelo menos um arquivo passou na verificação básica de assinatura e fim de PDF. |
| `sem_link_origem` | O registro de entrada não tinha URL; consulta de detalhe BDTD ainda pendente. |
| `nao_obtido` | Nenhum PDF validado; veja os motivos das tentativas. |
| `http_404`, `http_403` etc. | Resposta HTTP do endereço tentado; não se tenta contornar o controle. |
| `robots_bloqueado` | Política publicada impede coleta da URL. |
| `robots_indisponivel` | Não foi possível confirmar a política, por exemplo por erro ou HTML de verificação. |
| `sem_link_pdf_no_html` | Parser não encontrou link PDF no HTML recebido; não prova inexistência do PDF. |
| `pdf_invalido_ou_incompleto` | A resposta não passou na assinatura/EOF. |
| `pdf_duplicado` | Uma segunda URL entregou bytes já associados ao registro; o Raw continua deduplicado por SHA-256. |
| `verificacao_navegador` | Foi identificado desafio de navegador. |
| `limite_arquivo`, `limite_execucao` | Limite configurado atingido. |
| `limited: true` | A fila de candidatos não foi esgotada ou um limite foi atingido. |
| `unique_pdfs` | Quantidade de arquivos distintos por SHA-256, não número de teses. |

A mesma tese pode ter vários arquivos; o piloto guarda os candidatos identificados dentro dos limites. **Ainda não distingue corpo principal, anexos e outros materiais.** `all_attachments_confirmed` permanece falso. Um registro com sucesso pode ter outros candidatos que falharam; verifique também `attempts` e `limited`.

Assinatura `%PDF-` e marcador `%%EOF`, junto à conferência de Content-Length quando enviado, são verificações básicas: não provam validade estrutural completa, correspondência à tese, completude semântica ou qualidade de extração. OCR e parsing de texto permanecem para uma etapa posterior.

`by_initial_domain` agrupa pelo domínio do primeiro link tentado. Links Handle aparecem como `hdl.handle.net`; isso não é uma distribuição por instituição. Não confundir com representatividade do corpus.

## Evidência usada para construir esta versão

- Entrada recebida: 40 registros, 39 com pelo menos um link, 4 URLs terminando em PDF e 13 URLs em `hdl.handle.net`.
- Duas rodadas reais processaram 16 registros. A segunda obteve quatro PDFs distintos da UFMG e UFSC, com 21.300.021 bytes no Raw. Todos conferiram com o SHA-256 do manifesto, assinatura `%PDF-` e marcador `%%EOF`.
- As páginas publicaram duas URLs para cada PDF e a versão 0.3.1 transferiu cada conteúdo duas vezes. A versão 0.3.2 deduplica essas variantes conhecidas antes da transferência e explicita duplicatas remanescentes no relatório.
- PUC-SP: https://tede2.pucsp.br/handle/handle/5217 — arquivo anunciado de aproximadamente 37,68 MB, justificando streaming e limite acima dos 20 MB do antigo cliente de metadados.
- UFSC: https://repositorio.ufsc.br/handle/123456789/192778

A capacidade de acesso do crawler depende do ambiente e das políticas atuais de cada servidor. Inspeção via busca web não substitui teste HTTP do cliente no notebook.

## Testes e módulos anteriores

```powershell
python -m unittest discover -s tests -v
```

Na versão 0.3.2, 57 testes passaram em Windows/Python 3.14. Cobriam retomada, cache, múltiplos arquivos, respostas HTML disfarçadas de PDF, arquivos truncados, redirecionamentos com robots no destino, limites de streaming, diagnóstico de política, deduplicação de candidatos e preservação de estado. Os testes automatizados usaram respostas simuladas; a evidência real é a rodada descrita acima.

Os módulos anteriores continuam disponíveis:

```powershell
python -m bdtd demo
python -m bdtd.api --contact "seu-email@dominio.com" --data-dir data/api-linguagem
```

No piloto anterior, a configuração padrão da API usava **Linguagem OR Comunicação** somente em assunto, com 30.476 como referência da captura. Documentos em `docs/README_v0.1.md` e `docs/README_v0.2.md` também são históricos.

Continuam pendentes: delimitação definitiva da área, metadados de detalhe da BDTD, adaptadores adicionais, Parquet, extração e validação textual, Processed, anonimização e produtos Curated. Não há treino de modelos nem publicação do corpus.
