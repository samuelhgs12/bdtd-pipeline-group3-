# O plano explicado e a ordem de execução

## Qual é o trabalho?

Construir uma linha de produção de dados acadêmicos: coletar informações e textos de teses/dissertações, organizar, tratar e entregar conjuntos utilizáveis por outras pessoas. Não se treina um LLM. O trabalho avalia a qualidade e a rastreabilidade do pipeline e dos dados produzidos.

Uma tese terá dois materiais: **metadados** (título, autor, resumos, instituição, links etc.) e, quando disponível, **PDF**. O catálogo e o texto completo precisam de rotinas diferentes. O plano supõe obter os metadados de toda a área e PDFs em melhor esforço, mas a cobertura dos metadados também precisa ser demonstrada por consulta validada e total reconciliado.

## As quatro camadas

| Camada | Pergunta que responde | Exemplo |
|---|---|---|
| Raw | O que a fonte entregou exatamente? | XML original do catálogo e PDF original |
| Staging | Como consultar esse material em uma estrutura uniforme? | Linhas com identificador, campos DC e texto por página |
| Processed | Que conteúdo está padronizado, com qualidade avaliada? | Texto normalizado, duplicatas identificadas, dados pessoais tratados |
| Curated | Como preparar o dado para uma finalidade específica? | Exemplos de instrução, corpus, índice de busca e benchmarks |

Exemplo hipotético: uma dissertação sobre leitura entra como XML e PDF em Raw. Staging separa metadados e páginas. Processed trata cabeçalhos e duplicatas, mede qualidade e aplica a política de dados pessoais. Curated pode usar o corpo como corpus, formar um par resumo→título para SFT e dividir o texto em trechos recuperáveis para RAG, respeitando a política de splits.

Raw precisa guardar o original para auditar transformações. Staging estrutura sem adivinhar significados ambíguos. Processed altera conteúdo, por isso cada regra precisa de justificativa e contagens. Curated seleciona e formata para um uso; não basta renomear o mesmo arquivo quatro vezes.

## Os quatro produtos

1. **Pré-treino continuado:** corpus de texto limpo. O entregável é o texto formatado e documentado; um consumidor futuro faria o treinamento.
2. **SFT:** pares de instrução e resposta, por exemplo “Gere um título para este resumo” → título do trabalho. Primeira opção prática: pares já existentes nos metadados. QA e reescrita assistidas são expansão posterior.
3. **RAG:** trechos ligados à origem, mais índice para localizar passagens. Recuperação funciona sem treinar modelo; geração de resposta exige definir um gerador ou entregar inicialmente apenas a busca com citações.
4. **Benchmarks:** entradas e referências separadas para medir desempenho de sistemas. O benchmark é um conjunto de avaliação, não evidência de qualidade obtida até que um sistema seja avaliado nele.

## Ordem que faz sentido agora

1. Confirmar a definição da área, a URL de busca e o acesso automatizado permitido.
2. Executar um piloto de poucas páginas e conferir manualmente os campos.
3. Fechar o contrato dos dados entre A, B e C.
4. Expandir a coleta de metadados; medir cobertura.
5. Escolher adaptadores de PDF a partir das instituições realmente presentes.
6. Validar extração de PDFs de poucos exemplos antes da execução em volume.
7. Implementar Processed com relatórios por etapa.
8. Congelar um snapshot, definir agrupamentos/splits e construir os produtos.

No trio, A conduz acesso e coleta; B define parsing e tratamento; C define formatos finais e avaliação. B e C podem trabalhar com exemplos sintéticos enquanto o acesso real está sendo resolvido. Todos participam da revisão manual. A divisão não justifica desenvolver três implementações incompatíveis do esquema.

## Ajustes necessários no plano v1.2

- **Área 9 e 120.841 registros:** tratar como informações do planejamento, não como filtro técnico já validado. Confirmar origem, taxonomia, data da consulta e total. Não inferir o conjunto por busca textual de “linguagem”.
- **OAI/API:** caminhos candidatos precisam de validação. Implementar os três coletores de uma vez é prematuro; priorizar o acesso que o spike demonstrar funcionar.
- **Raw imutável versus buffer de PDFs:** o plano simultaneamente exige conservar Raw e permite apagar PDFs. Se escolher descarte, registrar que reprocessamento exato depende de novo download e disponibilidade da versão original. SHA e URL permitem verificar uma cópia; não reconstroem bytes apagados. A primeira implementação não apaga originais.
- **Amostragem:** fila aleatória randomiza tentativas, não os sucessos. Disponibilidade, tamanho e plataforma podem afetar download. Medir cobertura por instituição, período e tipo; não declarar representatividade garantida.
- **Dublin Core:** não usar automaticamente a segunda data como publicação, a primeira como defesa ou a segunda ocorrência de título como tradução. Preservar valores, idiomas e ambiguidade até validar o mapeamento.
- **Deduplicação:** similaridade é indício a revisar/calibrar; uma dissertação e uma tese relacionadas não são necessariamente duplicatas. Usar grupos de duplicação na separação de treino/avaliação, além de IDs.
- **RAG e benchmark de recuperação:** um documento relevante precisa estar no corpus pesquisável do benchmark B2. Criar corpus de avaliação separado dos produtos de desenvolvimento, com queries e julgamentos próprios. Não exigir que o documento-alvo esteja ausente do índice onde será procurado. No B2, o registro de origem é relevante conhecido; outros também podem ser relevantes, exigindo revisão dos julgamentos.
- **Sem treino:** regressão logística é um modelo treinado. O baseline proposto precisa ser removido ou explicitamente permitido como exceção a “nenhum modelo será treinado”.
- **Geração de exemplos e respostas:** o plano cita LLMs para QA/reescrita e resposta RAG, mas não define acesso/modelo/custo. Começar com pares de metadados e busca; definir esse recurso antes de prometer geração.
- **Tempo, disco e velocidade:** 8–25 mil PDFs e tempos de embedding são estimativas a substituir por medições do piloto. Não assumir 16 GB livres de RAM em cada computador. Reservar orçamento real por integrante.
- **Licenças e dados pessoais:** ausência de restrição explícita não é autorização comprovada. A seção jurídica precisa de revisão específica antes de uso/distribuição; esta entrega não implementa nem valida a base legal proposta. Permissões de metadados e texto completo devem ser examinadas separadamente.
- **Revisão humana:** 200 documentos completos para anonimização mais cinco benchmarks de 200 itens é uma carga significativa. Definir unidade de anotação, protocolo e tempo por item em um piloto. Kappa só faz sentido para rótulos categóricos definidos, não como métrica universal de respostas livres.

## Critério para encerrar esta primeira fase

O software local é verificável quando importa a amostra, reexecuta sem duplicar, preserva o original e retoma paginação simulada. A fase de coleta real só estará validada depois de confirmar o acesso, o recorte e conferir uma amostra real. A demonstração offline não é resultado empírico sobre a BDTD.
