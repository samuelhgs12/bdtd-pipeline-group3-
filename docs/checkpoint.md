# Checkpoint — 12/09/2026

## Estado atual

O projeto contém as etapas Raw e Staging de metadados, o piloto da API BDTD e o piloto de download de PDFs. A consulta exploratória continua sendo `Linguagem OR Comunicação` no campo `Subject`, limitada a `masterThesis` e `doctoralThesis`. A API informou 30.476 resultados e as duas páginas já coletadas contêm 40 registros distintos, 39 com links. Esse recorte ainda não foi validado como cobertura integral da área definida pelo professor.

As duas rodadas reais do piloto de PDFs processaram os 16 primeiros registros do snapshot. O resultado preservado em `data/pdf-piloto` contém quatro sucessos, 11 registros `nao_obtido`, um `sem_link_origem` e 24 registros ainda não processados. Foram obtidos quatro PDFs distintos, com 21.300.021 bytes no Raw.

## Diagnóstico do piloto real

O relatório, o manifesto e os payloads do SQLite são consistentes. O `PRAGMA quick_check` do banco, aberto em modo somente leitura, retornou `ok`. O hash do snapshot corresponde à configuração no banco e os arquivos de `robots.txt` arquivados correspondem aos SHA-256 usados em seus nomes. Há 16 resultados, 23 tentativas históricas e 12 respostas no cache.

Na primeira rodada, nenhuma página de trabalho ou URL de PDF chegou a ser requisitada. As oito requisições registradas foram exclusivamente para `robots.txt`, incluindo o redirecionamento HTTP para HTTPS da FURB:

- Bahiana: timeout ao consultar `robots.txt`.
- PUC Minas: três timeouts ao consultar `robots.txt`, um para cada registro.
- FURB: bloqueio confirmado; a política arquivada contém `Disallow: /docs/`, que abrange a URL recebida.
- PUC-SP: duas respostas HTTP 403 ao consultar `robots.txt`.

As quatro falhas por timeout foram registradas pela versão 0.3.0 como `erro_rede_ou_processamento`. Elas ocorreram na verificação de política e não demonstram falha do PDF. O histórico não foi reclassificado nem reescrito.

Na segunda rodada, executada com a versão 0.3.1, quatro dos oito novos registros tiveram sucesso:

- UFMG: dois PDFs, com 7.879.437 e 10.838.209 bytes.
- UFSC: dois PDFs, com 348.622 e 2.233.753 bytes.

Os quatro arquivos conferem com o SHA-256 do manifesto, começam com uma assinatura PDF, contêm marcador `%%EOF` e apresentam contagens internas plausíveis de 213, 191, 68 e 166 páginas. Os títulos e autores das páginas HTML correspondem aos registros de origem. Isso valida download e associação em nível técnico básico; não equivale a validar todo o texto extraído.

As quatro falhas da segunda rodada foram: HTTP 403 no `robots.txt` da UFPE; HTTP 500 do Handle em dois registros da UFPR, após tentativas limitadas; e falha de validação da cadeia TLS da UFRGS. A verificação TLS não foi desativada.

## Mudanças das versões 0.3.1 e 0.3.2

- Diagnósticos HTTP novos registram fase (`robots` ou `resource`), URL efetiva, status HTTP, causa, número da tentativa e evidência da política arquivada.
- Timeout e erro de rede durante a consulta à política são classificados como `robots_indisponivel`. Códigos de limite e `Retry-After` permanecem distintos.
- Falhas de política são reutilizadas por origem somente na instância atual do cliente. Isso evita repetir o mesmo timeout ou HTTP 403 para vários registros, sem transformar a falha em permissão persistente. Uma nova execução consulta a política novamente.
- Redirecionamentos continuam limitados e cada destino tem sua própria política verificada. Bloqueios, autenticação e desafios não são contornados.
- O orçamento é verificado antes de iniciar novas requisições e durante o streaming. Respostas parciais continuam sendo removidas; os limites encerram a execução de modo retomável.
- Registros interrompidos por limite agora recebem `limited: true` de forma consistente.
- `--retry-failed` também retoma sucessos parciais. PDFs íntegros e metadados já obtidos são preservados, e candidatos ainda não obtidos têm prioridade nas execuções seguintes.
- Se um objeto esperado estiver corrompido, seus bytes são preservados em `raw/corrupt/` e a cópia válida baixada pode restaurar o caminho endereçado pelo hash.
- O relatório novo inclui hash da entrada, versões dos resultados, fase das falhas e resumo das tentativas. Resultados históricos 0.3.0 continuam compatíveis.
- A versão 0.3.2 deduplica variantes conhecidas do mesmo bitstream DSpace antes do download. Aplicado aos quatro HTMLs reais, o parser passou de dois candidatos para um candidato por registro.
- Quando URLs não reconhecidas ainda entregarem bytes idênticos, a segunda resposta será registrada como `pdf_duplicado`; o relatório informará quantidade e bytes duplicados.
- `links_localizados` deixou de aparecer como motivo de falha em registros bem-sucedidos.
- Redirects de `robots.txt` para a política canônica HTTPS podem reutilizar a política confirmada durante a mesma execução.
- Erros de certificado são identificados como `tls_verification`, mantendo a validação TLS obrigatória.
- Novos logs recebem `run_id` e `record_id`, permitindo correlacionar requisições, execução e registro.

## Verificações

Ambiente: Windows, Python 3.14.0 (`C:\Python314\python.exe`).

- `python -m unittest discover -s tests -v`: 57 testes passaram.
- Os 27 testes anteriores continuam passando.
- Foram acrescentados testes simulados para política indisponível, cache transitório, nova consulta em nova execução, redirecionamentos, fechamento de respostas HTTP, limites exatos, logs, preservação da política, retomada em lotes de oito, sucesso parcial, avanço além de oito anexos, recuperação sem perda de objeto corrompido, deduplicação DSpace e classificação TLS.
- Os testes usam diretórios temporários e respostas simuladas; não fizeram coleta real.
- O banco, o manifesto, o relatório, os logs e os diretórios existentes do piloto não foram apagados nem reinicializados.

## Limitações

O acesso real a PDFs foi comprovado para dois registros da UFMG e dois da UFSC. Isso não permite generalizar a taxa de sucesso para todas as instituições. A assinatura `%PDF-`, o marcador `%%EOF`, o checksum e o tamanho HTTP são verificações básicas; não comprovam estrutura integral, correspondência semântica de todas as páginas ou qualidade de extração. Ainda não há extração de texto, OCR, classificação entre corpo principal e anexos, Processed, Curated, índice RAG ou benchmarks.

A versão 0.3.1 recebeu 43.603.035 bytes em respostas de recursos. Cada um dos quatro PDFs foi transferido duas vezes por URLs DSpace equivalentes, embora o Raw tenha armazenado apenas uma cópia de cada SHA-256. A versão 0.3.2 corrige os quatro padrões observados; o relatório histórico foi mantido sem reescrita.

Também permanecem pendentes a validação acadêmica do recorte “Linguagem e Comunicação”, o enriquecimento dos registros sem URL e a avaliação de plataformas institucionais que dependem de JavaScript ou endpoints específicos, sempre respeitando `robots.txt` e controles de acesso.

## Próximo passo

Abrir os quatro PDFs obtidos e testar extração de texto, registrando páginas, erros e correspondência com título/autor antes de ampliar o piloto. Depois dessa validação, executar no máximo mais oito registros com a versão 0.3.2. Falhas HTTP e de política podem ser reavaliadas posteriormente com `--retry-failed`, mas qualquer bloqueio e falha TLS deve continuar sendo respeitado. Não iniciar coleta em massa enquanto a qualidade textual e o recorte da área não estiverem validados.
