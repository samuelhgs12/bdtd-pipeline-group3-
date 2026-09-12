# BDTD — primeira implementação (v0.1.0)

Este projeto inicia a implementação do plano v1.2. **Ainda não é o pipeline completo.**
Entrega uma base executável para Raw e Staging de metadados, um diagnóstico de acesso e um coletor OAI-PMH genérico com retomada. Não exige bibliotecas externas. Python 3.11 ou superior; comandos compatíveis com Windows, Linux e macOS.

## Comece aqui no Windows

Extraia o ZIP e abra o PowerShell na pasta `bdtd-pipeline`, onde está este README.

```powershell
python --version
python -m bdtd demo
python -m unittest discover -s tests -v
```

Se `python` não estiver disponível, tente `py -3` no lugar de `python`. Não é preciso instalar dependências nem ativar ambiente virtual para esta versão.

A demonstração não acessa a internet. Gera **três registros sintéticos**, sendo dois ativos e um marcado como excluído, em `data/demo`. Repetir o comando não duplica os registros.

```text
data/demo/
  raw/oai/<sha256>.xml       # resposta original preservada
  raw/manifest.jsonl         # catálogo regenerável das respostas
  state.sqlite              # estado de coleta e registros estruturados
  staging/metadados.jsonl    # um registro por linha
  staging/rejeitados.jsonl   # registros inválidos e motivos
  reports/status.json       # contagens e limitações explícitas
```

Abra `data/demo/staging/metadados.jsonl` no VS Code. Observe `record_id`, `sha256`, `synthetic`, `dc.title`, `dc.description` e `deleted`. O número de registros inclui os excluídos; excluídos não devem entrar nos produtos futuros.

## Diagnóstico real da BDTD

Execute substituindo o e-mail pelo seu contato real:

```powershell
python -m bdtd spike --contact "seu-email@dominio.com"
```

Consulte `data/reports/spike.json` e `data/logs/http.jsonl`. URLs em `conf/crawler.json` são **candidatas, não endpoints confirmados**. O diagnóstico verifica robots.txt antes de cada origem, respeita o intervalo e não resolve desafios de navegador. Erros de acesso são registrados; não demonstram que a API inexiste. As respostas válidas ficam em `data/diagnostics` para inspeção. `ListSets` no diagnóstico testa uma página; se houver token, a enumeração completa dos sets continua pendente.

Na verificação desta entrega (11/09/2026), a home apresentou uma verificação de navegador. OAI, API e robots.txt não puderam ser confirmados pela consulta web. Nenhum registro real foi coletado. A contagem de 120.841 e a definição de “área 9” vêm do plano e não foram verificadas.

## Coletar após validar endpoint e recorte

O comando exige um `setSpec` obtido e validado no diagnóstico. Não preencha com `9` ou `demo` por suposição. Exemplo **com placeholders**, não pronto para copiar sem edição:

```powershell
python -m bdtd harvest-oai --contact "seu-email@dominio.com" --endpoint "URL_OAI_CONFIRMADA" --set "SET_SPEC_CONFIRMADO" --max-pages 2
python -m bdtd staging
python -m bdtd status
```

Comece com duas páginas, confira os registros e só depois aumente o limite. Executar de novo retoma pelo token. Um trabalho concluído não reinicia automaticamente. Para novo snapshot, use outro `--data-dir` antes do subcomando. Token expirado é reportado; esta versão não reinicia silenciosamente nem usa token inválido em loop. Se expirar, use um diretório novo para uma nova coleta, preservando o anterior.

Se não existir set para a área, será preciso implementar seleção por IDs obtidos de uma busca validada. **O coletor atual não seleciona a área por palavras-chave e não implementa coleta via API/HTML.** API/HTML fazem parte somente do diagnóstico. Nenhum registro recebe rótulo de área confirmada automaticamente.

## Importar XML já obtido legitimamente

```powershell
python -m bdtd import-oai "caminho\resposta.xml" --source-url "URL_ORIGINAL_DA_RESPOSTA"
python -m bdtd staging
```

O arquivo precisa conter resposta `ListRecords` OAI-PMH com metadados `oai_dc`. A importação preserva bytes originais; Staging valida o envelope, mantém todas as ocorrências DC e registra rejeições estruturais. Não é uma validação XSD completa.

## O que funciona

- Raw XML endereçado por SHA-256; verificação de integridade ao estruturar.
- SQLite para checkpoints de paginação, rejeições e registros; operação local em uma máquina por diretório.
- OAI: namespace, erros, registros excluídos, paginação por token e limite de páginas.
- HTTP: intervalo mínimo de 1 segundo, consulta a robots.txt, timeout, tentativas limitadas para 429/5xx, backoff e Retry-After. Erros de transporte param a execução para retomada posterior.
- Redirects interrompem o acesso e devem ser investigados; não há tentativa de contornar controles de acesso.
- Staging incremental por hash/versão do parser; exportação JSONL reconstruída a cada execução.
- Atualizações por data OAI; versão mais antiga não substitui a mais recente. Datas OAI são usadas como timestamps do registro, não como ano de defesa.
- Campos repetidos e `xml:lang` preservados. Empate de datestamp usa a última versão importada. Todas as respostas originais continuam no Raw.

## Limites e próxima entrega

Ainda faltam: validação da área e do endpoint real; esquema canônico específico das fontes; Parquet tipado/particionado; coleta e extração de PDFs; Processed; anonimização; deduplicação textual; produtos Curated e avaliação. Esta versão não oferece comandos fictícios `processed` ou `curated`.

JSONL é o formato inicial inspecionável, diferente do Parquet previsto no plano. A migração para Parquet será feita após validar os campos reais. Não há instalação de NER, embeddings ou OCR nesta fase.

Use apenas **um processo por diretório de dados**. Compartilhe código; distribua coletas por domínio depois de implementar a fila de PDFs. Não junte bancos SQLite manualmente. Dados sintéticos e reais devem permanecer em diretórios separados.

O cliente não mantém cache HTTP geral; a retomada evita revisitar páginas concluídas em condições normais. Uma interrupção entre gravar o Raw e confirmar o checkpoint pode refazer uma requisição, sem duplicar conteúdo no Raw. Mudanças de versão do parser ou importações manuais não substituem um sistema completo de snapshots: isso é trabalho futuro.

## Organização

- `bdtd/core.py`: Raw, parser, estado, exportação e rastreabilidade.
- `bdtd/network.py`: política HTTP, diagnóstico e paginação.
- `bdtd/__main__.py`: comandos de uso.
- `tests/test_pipeline.py`: falhas, retomada, integridade e preservação de metadados.
- `docs/plano_explicado.md`: significado das etapas e decisões necessárias.
- `docs/checkpoint.md`: estado verificável desta entrega.
- `conf/crawler.json`: endpoints candidatos e parâmetros de diagnóstico.

Referência do protocolo implementado: https://www.openarchives.org/OAI/openarchivesprotocol.html
