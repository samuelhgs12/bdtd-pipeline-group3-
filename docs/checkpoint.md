# Checkpoint — 11/09/2026

## Resultado

Primeira versão funcional de Raw e Staging de metadados, com demonstração offline e coletor OAI-PMH genérico. Esta entrega inicia o plano v1.2; não conclui as oito semanas de projeto.

## Verificações executadas

Ambiente de teste: Linux, Python 3.12. Comandos para Windows documentados, sem execução nativa em Windows nesta entrega.

- `python -m unittest discover -s tests -v`: 8 testes passaram.
- `python -m bdtd demo`: 3 registros sintéticos estruturados (2 ativos e 1 excluído).
- Reexecução idempotente e paginação em duas chamadas verificadas por testes.
- Checksum, rejeição de HTML/erro OAI, registros inválidos, versões antigas e robots.txt verificados por testes.

Os testes HTTP usam simulação; não comprovam conectividade real com a BDTD.

## Acesso real

A consulta web à home da BDTD retornou “Verificando seu navegador”. As consultas ao robots.txt, à API e ao endpoint OAI candidato não produziram conteúdo verificável. Portanto: zero registros reais coletados, taxonomia e total da área ainda não confirmados, coletor genérico ainda não validado contra esse servidor.

## Próximo passo do usuário

1. Extrair o projeto e executar `python -m bdtd demo`.
2. Executar `python -m bdtd spike --contact "SEU_EMAIL_REAL"`.
3. Examinar `data/reports/spike.json`; compartilhar esse arquivo para definirmos o acesso a implementar/validar.
4. Confirmar a URL da busca que representa “Linguagem e Comunicação”.

## Escopo pendente

Coleta real da área, API/HTML se necessários, Parquet e esquema específico, fila de PDFs, extração e OCR, Processed, anonimização, deduplicação, splits por grupos, Curated e benchmarks. Não há treino, geração assistida nem publicação de dataset nesta versão.

## Referências de implementação

- Plano v1.2 fornecido pelo usuário.
- OAI-PMH: https://www.openarchives.org/OAI/openarchivesprotocol.html
- Página consultada: https://bdtd.ibict.br/vufind/
