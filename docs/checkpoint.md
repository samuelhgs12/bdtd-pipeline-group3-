# Checkpoint — validação da coleta em 12/09/2026

## Escopo desta etapa

Esta etapa validou o novo recorte da API, mediu a paginação real, selecionou uma amostra reproduzível de 50 trabalhos, tentou seus downloads e preparou a revisão humana de pertinência. Não foi implementada extração de texto e não foi iniciada coleta em grande escala. Os recortes anteriores, bancos SQLite, Raw e diretórios de download foram preservados.

## Consulta validada

A consulta representa os campos separadamente, sem usar `AllFields`:

```text
(Subject:Linguagem OR Abstract:Linguagem OR
 Subject:Comunicação OR Abstract:Comunicação)
AND
(format:masterThesis OR format:doctoralThesis)
```

Na API VuFind, ela foi enviada como um grupo avançado com `join=AND`, `bool0[]=OR`, quatro pares repetidos de `lookfor0[]` e `type0[]`, e dois `filter[]` para os tipos. A configuração integral está em `conf/api-validacao-linguagem-comunicacao.json`.

Em 12/09/2026, a API informou **115.070 resultados**, exatamente o valor de referência observado no navegador. Duas páginas com `limit=20` retornaram 40 IDs distintos. Os parâmetros, horários, URLs, hashes e respostas estão arquivados em `data/validacao-linguagem-comunicacao/api/`.

Essa igualdade confirma a representação técnica da consulta observada. Ela não comprova, por si só, cobertura integral da área acadêmica “Linguagem e Comunicação”; essa validação continua dependendo da revisão do professor e da amostra.

## Limite real de paginação

O diagnóstico comparou `limit=1`, `20` e `100`. A API mantém `resultCount=115070`, porém só permite avançar nas primeiras **1.000 posições**:

- com `limit=1`, a página 1.001 repete exatamente o ID da página 1.000;
- com `limit=20`, a página 51 repete a página 50;
- com `limit=100`, a página 11 repete a página 10;
- páginas muito mais profundas também repetem a última resposta da janela, em vez de retornar erro ou página vazia.

O relatório e as 31 respostas de prova estão em `data/validacao-linguagem-comunicacao/pagination-probe/`. Uma tentativa inicial de sortear sobre as 115.070 posições detectou IDs repetidos e foi interrompida antes de qualquer download; seus dados permanecem em `data/validacao-linguagem-comunicacao/sample/`. O relatório dessa tentativa não deve ser usado como amostra completa.

## Amostra de 50

A amostra final usa seed fixa **20260912** e `random.Random(seed).sample(range(1, 1001), 50)`, com uma requisição `limit=1` para cada posição. Foram obtidos 50 IDs distintos, entre as posições 31 e 991. A distribuição pelos dez décimos da janela foi `8, 4, 5, 1, 7, 6, 6, 4, 4, 5`.

Essa é uma amostra aleatória reproduzível da **janela acessível das primeiras 1.000 posições na ordenação padrão da API**. Ela não é uma amostra aleatória dos 115.070 resultados informados. A ordenação também não é um snapshot imutável; mudanças futuras no índice podem mudar o registro de uma posição.

A lista foi salva antes dos downloads, com SHA-256 `18c40823a408f62405558b885ffb5bebd4d4e59fba15eea006b5e978c0f59199`, em:

- `data/validacao-linguagem-comunicacao/sample-accessible/selected_50.jsonl`;
- `data/validacao-linguagem-comunicacao/sample-accessible/selected_50.csv`;
- `data/validacao-linguagem-comunicacao/sample-accessible/sample_report.json`.

## Resultado dos downloads

Todos os 50 trabalhos selecionados foram processados com o contato configurado, espera mínima por domínio, verificação de `robots.txt`, até 100 MiB por arquivo e orçamento de 500 MiB na execução.

- trabalhos selecionados: **50**;
- trabalhos com pelo menos um PDF candidato do trabalho: **17**;
- taxa de sucesso por trabalho: **34%**;
- PDFs únicos efetivamente baixados no Raw: **20** (52.831.365 bytes);
- PDFs candidatos a arquivo dos trabalhos: **19**;
- documento institucional do site: **1**;
- trabalhos com múltiplos PDFs candidatos: **2**;
- possíveis arquivos adicionais/anexos ainda não verificados: **2**;
- resultados sem PDF obtido: **29**;
- registros sem URL de origem na resposta da API: **4**.

O PDF institucional é “Política de Informação do Repositório Locus — UFV”, encontrado no menu/rodapé da página. Seus bytes e o evento histórico foram preservados, mas ele foi separado dos arquivos candidatos dos trabalhos. A versão 0.3.3 agora filtra, com registro explícito, nomes inequívocos de políticas de informação, privacidade e termos de uso encontrados como links comuns. URLs indicadas por `citation_pdf_url` continuam elegíveis.

Entre as ocorrências sem sucesso houve 23 `robots_indisponivel` (13 respostas HTTP que não confirmaram a política, quatro timeouts, três políticas retornadas como HTML inválido e três falhas de validação TLS), um `robots_bloqueado`, cinco HTTP 500 em recursos, duas respostas com PDF inválido/incompleto e três páginas sem link PDF. Nenhuma política indisponível foi tratada como permissão; bloqueios, TLS e controles de acesso não foram contornados.

Os resultados estão em:

- `data/validacao-linguagem-comunicacao/pdf-sample-50/reports/pdf_pilot.json`;
- `data/validacao-linguagem-comunicacao/pdf-sample-50/raw/manifest_pdf.jsonl`;
- `data/validacao-linguagem-comunicacao/pdf-sample-50/logs/http.jsonl`;
- `data/validacao-linguagem-comunicacao/pdf-sample-50/raw/pdf/`.

## Revisão de pertinência

A tabela contém os 50 trabalhos, inclusive os 33 sem PDF candidato. Ela reúne ID, título, resumo disponível, assuntos, URL de origem, resultado do download, classificação preliminar, justificativa curta e a coluna vazia `final_decision`.

A API de busca não forneceu resumo para nenhum dos 50 registros. Metadados HTML obtidos durante o download acrescentaram resumo para cinco; os demais 45 campos foram mantidos vazios. Nenhum resumo foi inventado.

A triagem preliminar resultou em **46 pertinentes, dois não pertinentes e dois duvidosos**. Os dois não pertinentes tratam de linguagem de programação/computação. Os casos duvidosos têm título truncado ou evidência temática insuficiente. Nenhum registro foi excluído e a classificação não representa validação humana.

Arquivos para revisão:

- `data/validacao-linguagem-comunicacao/review/review_50.csv` — planilha a preencher;
- `data/validacao-linguagem-comunicacao/review/review_50.jsonl` — versão estruturada;
- `data/validacao-linguagem-comunicacao/review/review_report.json` — taxas separadas de download e pertinência;
- `conf/triagem-validacao-20260912.json` — decisões preliminares e justificativas rastreáveis.

## Comandos executados

O e-mail foi lido com `git config user.email` e passado a `--contact` sem ser gravado nos relatórios.

```powershell
C:\Python314\python.exe -X utf8 -m bdtd.api --contact $bdtdContact --data-dir data\validacao-linguagem-comunicacao\api --config conf\api-validacao-linguagem-comunicacao.json --pages 2

C:\Python314\python.exe -X utf8 -m bdtd.pagination_probe --contact $bdtdContact --config conf\api-validacao-linguagem-comunicacao.json --data-dir data\validacao-linguagem-comunicacao\pagination-probe

C:\Python314\python.exe -X utf8 -m bdtd.sample --contact $bdtdContact --config conf\api-validacao-linguagem-comunicacao.json --data-dir data\validacao-linguagem-comunicacao\sample-accessible --seed 20260912 --size 50 --frame-size 1000

C:\Python314\python.exe -X utf8 -m bdtd.pdfs --input data\validacao-linguagem-comunicacao\sample-accessible\selected_50.jsonl --data-dir data\validacao-linguagem-comunicacao\pdf-sample-50 --contact $bdtdContact --limit 50 --max-mb 100 --budget-mb 500

C:\Python314\python.exe -X utf8 -m bdtd.review --sample data\validacao-linguagem-comunicacao\sample-accessible\selected_50.jsonl --manifest data\validacao-linguagem-comunicacao\pdf-sample-50\raw\manifest_pdf.jsonl --metadata-html data\validacao-linguagem-comunicacao\pdf-sample-50\staging\metadata_html.jsonl --triage conf\triagem-validacao-20260912.json --output-dir data\validacao-linguagem-comunicacao\review

C:\Python314\python.exe -X utf8 -m unittest discover -s tests -v
```

## Verificações e limitações

Os **66 testes** passaram no Python 3.14.0. `PRAGMA quick_check` retornou `ok` nos cinco bancos desta validação, inclusive no banco da tentativa de amostragem interrompida. A entrada dos downloads confere com o hash da lista selecionada e os resultados abrangem exatamente os 50 IDs. Os arquivos Raw anteriores não foram removidos ou substituídos.

A taxa de 34% mede somente esta amostra da janela acessível e é afetada pela distribuição de repositórios e suas políticas. Assinatura PDF, marcador de fim, tamanho e SHA-256 confirmam a integridade técnica básica, mas não confirmam conteúdo integral, correspondência semântica ou papel de anexos. A cobertura além das primeiras 1.000 posições exige outro mecanismo oficial de paginação ou particionamento de consulta antes de qualquer inferência populacional.

## Próximo passo

Revisar `review_50.csv` e preencher `final_decision` para os 50 trabalhos. Depois da decisão humana, avaliar com o professor a adequação do recorte e investigar um particionamento oficial e não sobreposto da consulta que contorne a limitação estatística da janela sem contornar controles de acesso. A extração de texto permanece adiada.

## Preparação da coleta de até 5.000 candidatos — 12/09/2026

A revisão temática dos 50 registros permanece disponível, mas deixou de bloquear a etapa técnica. Foi criado o orquestrador `bdtd.collect`, que valida os filtros, descobre partições, arquiva respostas Raw, deduplica IDs entre partições, alimenta incrementalmente o downloader e fecha lotes verificados. A extração de texto continua fora do escopo.

A descoberta real da API, executada em 12/09/2026, voltou a informar **115.070** resultados e retornou facetas `publishDate` e `institution`. Foram expostos 30 anos, de 1997 a 2026, que somam 112.601 ocorrências; as demais ocorrências são cobertas por duas partições complementares explícitas: campo de ano presente sem qualquer dos 30 valores expostos e ausência de ano (`-publishDate:[* TO *]`). Assim, valores antigos, futuros ou não normalizados não são descartados silenciosamente. A faceta de instituições também foi limitada às 30 entradas mais frequentes. Seus próprios `href` confirmam os filtros `publishDate:"valor"` e `institution:"valor"`.

Cada partição anual com mais de 1.000 resultados é subdividida pelas instituições expostas e por um complemento com filtros negativos. As contagens das partições filhas são obtidas em consultas reais, sem reaproveitar incorretamente a contagem global da faceta. Se uma subpartição continuar acima de 1.000, o coletor registra a limitação, coleta somente a janela acessível e confirma a repetição posterior. O banco mantém todas as associações `record_id`–partição, permitindo deduplicar o registro sem perder a proveniência.

A API aplicou HTTP 429 às consultas seguintes à descoberta, inclusive com intervalo de dez segundos e tentativas espaçadas em 15, 30 e 60 segundos. Essas respostas estão em `data/coleta-5000/logs/api_http.jsonl`. O cliente foi corrigido para que 429 e 5xx não marquem uma partição como inacessível. No comando principal, um 429 sem `Retry-After` causa espera conservadora de 60 minutos, limitada a quatro esperas por execução; `Ctrl+C` continua preservando o estado. O comando conclui a validação pendente antes de iniciar downloads e prossegue automaticamente quando o servidor voltar a aceitar as consultas. O progresso parcial fica em `data/coleta-5000/reports/partition_validation_progress.json`, e o relatório concluído ficará em `partition_validation.json`.

As 23 ocorrências anteriores de `robots_indisponivel` foram reagrupadas por causa: seis HTTP 503 da UNICAMP, quatro HTTP 403 da PUC-SP, duas falhas TLS da UFRGS, duas respostas HTML em vez de política da UECE, dois timeouts da PUC Minas e ocorrências isoladas de HTML, TLS, timeout, HTTP 503, 468 e 403 em outros domínios. Não foi comprovado erro do cliente que autorizasse acesso. Políticas indisponíveis continuam sem autorizar o recurso; login, embargo, desafios e erros TLS não são contornados.

O downloader agora aceita apenas extensões imutáveis da fila anterior, preserva cada snapshot e interrompe antes de baixar outro candidato quando alcança 5.000 SHA-256 candidatos. Há reserva padrão de **10 GiB**, conferida antes e durante streaming e empacotamento. Na preparação havia 47,6 GiB livres. A execução mostra registros tentados, trabalhos com PDF, PDFs únicos candidatos, bytes e falhas por motivo.

Os lotes são fechados em torno de 1 GiB, sem dividir PDF. Cada `batch-NNNN` contém `pdfs/`, `metadata/records.jsonl`, `results/download_results.jsonl`, `manifest.jsonl`, `checksums.sha256` e `CLOSED.json`. Caminhos são relativos, associações de vários registros ao mesmo SHA são preservadas e a integridade é verificada antes do fechamento. Os objetos originais não são apagados e não há integração ativa com Drive.

Comando exato para iniciar e retomar:

```powershell
C:\Python314\python.exe -X utf8 -m bdtd.collect --contact (git config user.email) --config conf\api-validacao-linguagem-comunicacao.json --data-dir data\coleta-5000 --target-pdfs 5000 --reserve-gb 10 --batch-gb 1 --budget-gb 20 --max-hours 8 --rate-limit-wait-minutes 60 --max-rate-limit-waits 4
```

Comandos de preparação e verificação executados:

```powershell
C:\Python314\python.exe -X utf8 -m bdtd.api --contact $bdtdContact --data-dir data\coleta-5000\partition-validation\facets --config conf\api-partition-discovery.json --pages 1
C:\Python314\python.exe -X utf8 -m bdtd.collect --contact $bdtdContact --config conf\api-validacao-linguagem-comunicacao.json --data-dir data\coleta-5000 --target-pdfs 5000 --reserve-gb 10 --batch-gb 1 --budget-gb 20 --max-hours 8 --validate-only
C:\Python314\python.exe -X utf8 -m unittest discover -s tests -v
```

Os **79 testes** passaram no Windows com Python 3.14. Eles cobrem a consulta avançada, facetas, filtros complementares, paginação repetida, deduplicação, extensão imutável da fila, retomada e espera limitada após rate limit, limite estrito de candidatos, limites de disco e transferência, robots, múltiplos arquivos, SHA-256 e fechamento/verificação de lotes. A evidência real da descoberta está em `data/coleta-5000/partition-validation/facets/`; o estado retomável e os diagnósticos ficam em `data/coleta-5000/`.

### Diagnóstico e visibilidade da execução em 12/09/2026

Às 14:30 no horário local, não havia processo Python da coleta em execução. O último evento HTTP foi um 429 da API BDTD às 14:21:53, e `collection.json` registrou interrupção pelo usuário às 14:28:31. O estado tinha uma validação parcial e uma resposta API em cache, com zero partições, zero registros na fila e zero downloads. `PRAGMA quick_check` retornou `ok`. Nenhuma segunda coleta foi iniciada durante o diagnóstico.

O relógio de `--max-hours` agora começa antes da validação e inclui todas as esperas por 429. A espera verifica o prazo em blocos de no máximo 30 segundos e termina com estado retomável ao alcançá-lo. Ao receber 429, o coletor tenta primeiro todos os registros já persistidos na fila; somente a API BDTD fica em pausa. Os downloads continuam sequenciais e respeitam `robots.txt`, atrasos e controles de cada domínio de destino.

Foi acrescentado heartbeat de 30 segundos com etapa, fila ainda sem resultado, trabalhos tentados, trabalhos com PDF candidato, PDFs únicos candidatos, MiB de PDFs no Raw e falhas reais. Início e fim de etapas e de cada requisição de download usam `flush=True`. A mensagem de 429 inclui o motivo e a hora local prevista para a próxima consulta.

### Auditoria do HTTP 429 e reaproveitamento local — 12/09/2026

O 429 foi localizado no endpoint de **busca** `https://bdtd.ibict.br/vufind/api/v1/search`, não em `robots.txt`, facetas isoladas ou repositórios de PDFs. A sequência inicial registrada em UTC foi: `16:38:25` robots 200; `16:38:26` busca raiz com facetas 200 e 26.281 bytes; `16:38:28`, `16:38:31`, `16:38:35.438` e `16:38:35.628` a mesma busca filtrada por `publishDate:"2024"` respondeu 429. Às `16:39:30` robots voltou a responder 200, `16:39:35` a raiz respondeu 200 e, entre `16:39:42` e `16:39:55`, o filtro por 2024 recebeu mais quatro 429. Mais tarde, o filtro de menor contagem, `publishDate:"1997"`, também recebeu 429. O último evento anterior a esta auditoria ocorreu às `17:52:39 UTC` (`14:52:39 -03:00`), novamente no filtro por 1997.

Os eventos antigos registraram status 429, URL, tentativa e erro `HTTP Error 429: Too Many Requests`, mas não consumiram o corpo nem guardaram cabeçalhos. Portanto, **não há evidência histórica de `Retry-After`, `Content-Type` ou mensagem do corpo**; os 60 minutos usados eram política do cliente, não exigência comprovada da BDTD. Os 429 tiveram zero bytes registrados pelo logger antigo. O único item em `api_cache` é a resposta JSON bem-sucedida da busca raiz, SHA-256 `9480910f344a70e5595d5e2db842927c3f9eba5da329c7293967e276f0da4db1`; nenhum 429 foi reutilizado como resposta válida de cache.

Foi comprovado um excesso nas primeiras execuções: a camada de rede repetia cada 429 até quatro vezes e depois o controlador aplicava sua própria retomada. A configuração principal já passou a usar `retry_429=False`; a auditoria acrescentou teste que exige uma única chamada. Também foi eliminado o `ESPERA_INICIO` duplicado. A busca raiz bem-sucedida já era reaproveitada do cache, mas `robots.txt` era solicitado novamente em toda reinicialização; políticas válidas da BDTD agora têm cache persistente por 24 horas.

Novos erros HTTP passam a registrar `source=network`, `Retry-After`, `Content-Type`, data, servidor e até 4 KiB de texto do corpo, com leitura limitada a 64 KiB. Cabeçalhos de cookie e autenticação não são registrados. Eventos reaproveitados são marcados `source=cache`. O SQLite ganhou histórico `api_rate_limits` e o prazo ativo em `api_cooldowns`. Reiniciar antes do prazo não antecipa uma consulta; o prazo mais recente do log legado também é migrado. `Retry-After` numérico ou em data tem precedência. Ausente ou inválido, aplica-se a política configurada por `--rate-limit-wait-minutes` e sua origem fica explícita.

Foram identificados 95 metadados únicos compatíveis para importação: 40 da coleta de duas páginas da consulta atual, 50 da amostra da mesma consulta (um ID sobreposto) e seis IDs adicionais exclusivos do piloto anterior por `Subject`, subconjunto lógico do OR atual. A importação verifica tipos, filtros e parâmetros no URL de origem, deduplica por `record_id` e preserva cada payload/fonte em `record_sources`. Os dois pilotos de PDF contêm 23 PDFs candidatos únicos já obtidos, associados a 21 trabalhos, além de um documento institucional conhecido; todos serão conferidos por SHA-256 no reaproveitamento.

Comando atualizado para importar, reaproveitar, executar downloads pendentes antes da API e retomar a coleta:

```powershell
C:\Python314\python.exe -X utf8 -m bdtd.collect --contact (git config user.email) --config conf\api-validacao-linguagem-comunicacao.json --data-dir data\coleta-5000 --target-pdfs 5000 --reserve-gb 10 --batch-gb 1 --budget-gb 20 --max-hours 8 --rate-limit-wait-minutes 60 --max-rate-limit-waits 4 --repository-retry-hours 24 --import-metadata data\validacao-linguagem-comunicacao\api\staging\api_metadados.jsonl --import-metadata data\validacao-linguagem-comunicacao\sample-accessible\selected_50.jsonl --import-metadata examples\piloto_40_metadados.jsonl --reuse-pdf-dir data\validacao-linguagem-comunicacao\pdf-sample-50 --reuse-pdf-dir data\pdf-piloto
```

Os **83 testes** passaram no Python 3.14, incluindo ausência de cache para 429, persistência do prazo entre retomadas, importação/deduplicação com proveniência, reaproveitamento por SHA-256 e captura segura da evidência HTTP.

A preparação offline importou efetivamente os 95 IDs e 130 associações de fonte. Foram reaproveitados e conferidos 23 PDFs candidatos únicos de 21 trabalhos, mais um documento institucional conhecido. Duas rodadas controladas atingiram os repositórios sem consultar a BDTD. A primeira reavaliou nove URLs antigas e não obteve arquivo novo; a segunda processou dez registros inéditos e obteve quatro PDFs novos: dois da UFSC (405.411 e 428.595 bytes), um da PUCRS (1.987.957 bytes) e um da UEL (21.257.402 bytes). O estado passou a **76 trabalhos processados, 25 com PDF, 27 PDFs candidatos únicos e 19 registros inéditos restantes**. Falhas recentes não são repetidas antes de 24 horas por padrão. O prazo da API continuou intacto em `2026-09-12T18:52:39.687018+00:00` (`15:52:39 -03:00`), por isso nenhum diagnóstico antecipado foi enviado.
