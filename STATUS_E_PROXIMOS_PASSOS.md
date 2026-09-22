# Status e próximos passos — Coleta cadastral — Mogi das Cruzes e Itanhaém

> Auditoria de 22/09/2026. Esta é uma fotografia baseada em arquivos, Git, artefatos e endpoints observáveis. Nenhum build completo foi executado nesta classificação.

## Classificação

- **Estado:** recorte de Mogi concretizado; continuidade fiscal em evolução
- **Confiança:** alta
- **Natureza:** pipeline Python, base territorial e interface de conferência

## Evidências observadas

- O README documenta a conclusão do pacote público de Mogi de 2024 e seus limites.
- Há testes e interface local; existem 12 mudanças locais com nova auditoria e coletor fiscal.
- O Git registra uma base anterior, mas o trabalho atual ainda não foi consolidado.

## Diagnóstico franco

É um dos projetos de dados mais sólidos. A próxima etapa não é ampliar promessas, e sim preservar as mudanças e concluir o fluxo autorizado com rastreabilidade.

## Upgrades previstos

### P0 — preservar e tornar retomável

- Salvar as 12 mudanças em commits separados por coleta, auditoria, testes e documentação.
- Executar todos os testes e uma coleta amostral sem gravar credenciais.
- Registrar estado e lacunas de Itanhaém separadamente de Mogi.

### P1 — estabilizar

- Concluir renovação de sessão e seleção municipal dentro do fluxo oficial.
- Versionar manifestos, schemas e comparações entre exercícios.
- Adicionar validações de completude e qualidade espacial.

### P2 — evoluir

- Criar exportações Parquet/GeoPackage e relatório incremental.
- Integrar a interface local a um catálogo de fontes e datas.

## Critério para considerar retomado

O projeto será considerado retomado quando o pipeline atualizar uma amostra de forma autônoma e autorizada, com testes, manifesto e separação inequívoca entre Mogi e Itanhaém.

## Prompt de retomada para o Codex

> Retome o projeto **Coleta cadastral — Mogi das Cruzes e Itanhaém** nesta pasta. Leia este arquivo e o README, inspecione o Git e preserve todo trabalho local. Comece somente pelo P0, valide com evidências e não implemente P1/P2 antes de apresentar o diagnóstico atualizado.

