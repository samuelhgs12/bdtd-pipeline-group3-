# BDTD — v0.3.0: piloto de download de PDFs

## Execute no Windows

Extraia o ZIP em **uma pasta nova**. Abra o PowerShell na pasta que contém este README, `bdtd`, `conf` e `examples`.

O pacote já contém os **40 metadados reais enviados pelo usuário**, em `examples/piloto_40_metadados.jsonl`. Não precisa copiar arquivos da versão anterior. Nenhum PDF é incluído no ZIP.

Python 3.11 ou superior; sem bibliotecas externas. Substitua o e-mail pelo seu contato:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com"
```

O comando tenta **até oito registros por execução**, na ordem do arquivo de entrada. Não é amostra estatisticamente representativa: é um piloto técnico que inclui links diretos, páginas institucionais e um registro sem URL. Default: 100 MiB por resposta e 500 MiB transferidos por execução (inclui HTML/robots; pode exceder até um bloco de 64 KiB ao interromper). O total em disco pode crescer entre execuções; não há buffer rotativo nem exclusão automática de PDFs.

Ao terminar, envie **`data/pdf-piloto/reports/pdf_pilot.json`**.

Para continuar os próximos oito registros, repita o mesmo comando. Registros com resultados anteriores são pulados, salvo sucesso cujo arquivo esteja ausente/corrompido. Para incluir falhas anteriores na nova tentativa:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com" --retry-failed
```

Para tentar até 40 registros numa execução:

```powershell
python -m bdtd.pdfs --contact "seu-email@dominio.com" --limit 40
```

`--limit` limita os registros tentados **nesta execução**, diferente de `--pages` do piloto API. Limite máximo por execução: 50 registros. O orçamento de transferência pode interromper antes de terminar todos. Use `--retry-failed` para retomar tentativas afetadas por esse limite. Cada resposta interrompida é reiniciada; não há HTTP Range para continuar de um byte parcial.

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
| `verificacao_navegador` | Foi identificado desafio de navegador. |
| `limite_arquivo`, `limite_execucao` | Limite configurado atingido. |
| `limited: true` | A fila de candidatos não foi esgotada ou um limite foi atingido. |
| `unique_pdfs` | Quantidade de arquivos distintos por SHA-256, não número de teses. |

A mesma tese pode ter vários arquivos; o piloto guarda os candidatos identificados dentro dos limites. **Ainda não distingue corpo principal, anexos e outros materiais.** `all_attachments_confirmed` permanece falso. Um registro com sucesso pode ter outros candidatos que falharam; verifique também `attempts` e `limited`.

Assinatura `%PDF-` e marcador `%%EOF`, junto à conferência de Content-Length quando enviado, são verificações básicas: não provam validade estrutural completa, correspondência à tese, completude semântica ou qualidade de extração. O passo seguinte inclui abrir uma amostra e extrair texto para validar isso. Não foi implementado OCR ou parsing de texto nesta versão.

`by_initial_domain` agrupa pelo domínio do primeiro link tentado. Links Handle aparecem como `hdl.handle.net`; isso não é uma distribuição por instituição. Não confundir com representatividade do corpus.

## Evidência usada para construir esta versão

- Entrada recebida: 40 registros, 39 com pelo menos um link, 4 URLs terminando em PDF e 13 URLs em `hdl.handle.net`.
- Inspeção pública confirmou links de arquivos nas páginas da PUC-SP e da UFSC abaixo. Não foi executado download real pelo novo cliente nesta entrega.
- PUC-SP: https://tede2.pucsp.br/handle/handle/5217 — arquivo anunciado de aproximadamente 37,68 MB, justificando streaming e limite acima dos 20 MB do antigo cliente de metadados.
- UFSC: https://repositorio.ufsc.br/handle/123456789/192778

A capacidade de acesso do crawler depende do ambiente e das políticas atuais de cada servidor. Inspeção via busca web não substitui teste HTTP do cliente no notebook.

## Testes e módulos anteriores

```powershell
python -m unittest discover -s tests -v
```

27 testes passaram em Linux/Python 3.12. Cobrem retomada, cache, múltiplos arquivos, respostas HTML disfarçadas de PDF, arquivos truncados, redirecionamentos com robots no destino, limite de streaming e preservação de estado. Downloads de teste usam respostas simuladas; nenhum teste comprova sucesso real nos repositórios.

Os módulos anteriores continuam disponíveis:

```powershell
python -m bdtd demo
python -m bdtd.api --contact "seu-email@dominio.com" --data-dir data/api-linguagem
```

Nesta versão, a configuração padrão da API foi atualizada para **Linguagem OR Comunicação**, com 30.476 como referência da captura. Os módulos têm versões de processamento próprias: OAI 0.1.0, API 0.2.0, PDF 0.3.0. Documentos em `docs/README_v0.1.md` e `docs/README_v0.2.md` são históricos; este README descreve o estado atual.

Continuam pendentes: delimitação definitiva da área, metadados de detalhe da BDTD, adaptadores adicionais, Parquet, extração e validação textual, Processed, anonimização e produtos Curated. Não há treino de modelos nem publicação do corpus.
