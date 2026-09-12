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
