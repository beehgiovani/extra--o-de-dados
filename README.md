# Coleta e conferência cadastral de Mogi das Cruzes e Itanhaém

Organizei este projeto para coletar dados territoriais publicados pelas prefeituras, preservar a origem de cada informação e permitir a conferência local no mapa. Mogi e Itanhaém continuam separados nos resultados.

O programa de coleta é o `extrator_imobiliario_mestre.py`. A conferência de Mogi é aberta pelo `abrir_interface_mogi.bat`. O projeto ainda não possui nome comercial.

## Preparação do ambiente

Eu uso Python 3.10 ou superior. Na primeira execução, preparo o ambiente assim:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Para conferir se o código está íntegro antes de iniciar uma coleta:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile extrator_imobiliario_mestre.py servidor_conferencia_mogi.py
```

A divisão dos componentes e dos bancos está explicada em [docs/ARQUITETURA.md](docs/ARQUITETURA.md).

## O que temos completo em Mogi

Na execução validada em 20 de agosto de 2026:

- 233.377 cadastros imobiliários individuais de 2024;
- 13.483 `id_local` distintos, usados como agrupadores territoriais;
- 177.689 cadastros atuais com algum valor venal público;
- 160.366 cadastros com valor do exercício de 2024;
- 17.323 cadastros preenchidos pelo exercício histórico mais recente;
- 55.688 cadastros atuais sem valor nas bases consultadas;
- 1.382.284 registros no histórico venal de 2016 a 2024;
- 230.802 cadastros georreferenciados e 2.575 sem ponto;
- 230.404 cadastros com bairro, distrito, macrozona e zoneamento associados ao ponto de referência.

Também foram preservados os campos cadastrais publicados: situação, classe fiscal, uso, endereço, loteamento, distrito, categoria da propriedade, características do terreno, ocupação, conservação, ano da construção, testada, áreas e detalhes das construções.

## Como os identificadores são tratados

O arquivo municipal fornece dois identificadores diferentes:

- `cadastro_id`: cadastro individual de cada unidade;
- `id_local`: referência territorial que pode ser compartilhada por vários cadastros.

Por exemplo, o `id_local` `01-004811-001` possui cinco cadastros na base atual. Os cadastros `000.002` e `000.003` permanecem separados e aparecem, respectivamente, como `unid.001` e `unid.002`.

O programa não tenta adivinhar o significado jurídico de cada trecho numérico do `id_local`, pois o portal não fornece esse dicionário. Ele preserva o valor original e usa `cadastro_id`, número e complemento para distinguir as unidades.

## Interface local de conferência

Para abrir, dou dois cliques em:

```text
abrir_interface_mogi.bat
```

Ou executo no PowerShell:

```powershell
.\.venv\Scripts\python.exe servidor_conferencia_mogi.py
```

A interface permite:

- buscar por `cadastro_id`, `id_local`, rua, bairro ou zona;
- filtrar por bairro, zoneamento, situação venal e precisão espacial;
- conferir cada cadastro individual sem misturar unidades do mesmo local;
- visualizar áreas, uso, situação cadastral e características do terreno;
- conferir valor venal do terreno e da construção;
- acompanhar o histórico de 2016 a 2024;
- verificar bairro, distrito, macrozona, zoneamento e os parâmetros urbanísticos publicados;
- visualizar no mapa todas as posições compatíveis com a busca e os filtros;
- conferir quantos cadastros e quantos `id_local` compartilham cada referência espacial.

O servidor escuta somente em `127.0.0.1`. Os cadastros permanecem no computador; o navegador recebe a página consultada e os pontos do mapa já agrupados por coordenada, sem carregar as fichas completas em massa. O mapa-base usa OpenStreetMap e depende de internet, mas a pesquisa e as fichas continuam locais.

## Resultados principais de Mogi

```text
data/atual/resultados/mogi/
├── cadastros_individuais.csv
├── valores_venais_por_cadastro.csv
├── historico_valores_venais_por_cadastro.csv
├── locais_agrupados.csv
├── locais_agrupados.json
├── locais_agrupados.geojson
├── dados_territoriais.csv
├── dados_territoriais.json
├── mapa_territorial.geojson
└── manifesto.json
```

`cadastros_individuais.csv` é a planilha principal para conferência cadastral. `locais_agrupados` é a visão territorial que mostra quantos cadastros compartilham cada `id_local`. O histórico venal fica separado para não apresentar valores antigos como atuais.

## Coleta e atualização

Somente Mogi, usando o checkpoint existente:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py mogi
```

Reprocessamento integral de Mogi e do histórico público:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py mogi --mogi-historico-desde 2016 --force
```

Atualização somente das camadas de contexto territorial:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py contexto-mogi
```

O checkpoint fica em `data/atual/checkpoint.sqlite`. Os arquivos de dados e o banco da interface são gerados localmente e não entram no Git.

## Continuidade da situação fiscal

O banco já consegue importar e vincular uma situação fiscal obtida por uma fonte autorizada. Esse fluxo recebe inscrição, competência, resultado, quantidade, valor total, fonte, data da consulta e número do documento. Ele não consulta sozinho o portal municipal.

Para importar um arquivo real já conferido:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py importar-fiscal `
  --fiscal-file "entrada_privada\situacao_fiscal.csv" `
  --fiscal-cidade mogi
```

As pastas `entrada_privada`, `documentos_privados` e `capturas_locais` são ignoradas pelo Git. Assim, materiais de trabalho não são enviados ao repositório por engano.

A investigação do portal de Dívida Ativa, o formato exigido e a sequência segura para concluir essa integração estão registrados em [docs/CONTINUIDADE_MOGI.md](docs/CONTINUIDADE_MOGI.md). Esse documento diferencia resultado do Edital 4, posição financeira completa e certidão municipal para que uma resposta parcial não seja apresentada como quitação total.

## Limites mantidos

O projeto coleta cadastro público, valores venais publicados e georreferência territorial. Ele não baixa boletos e não armazena linha digitável, pagamento, CPF, CNPJ ou nome de proprietário.

A geometria exata do lote não foi disponibilizada publicamente pela fonte usada. O mapa identifica se o ponto representa o centro da quadra ou do logradouro. Bairro e zoneamento são contexto desse ponto e precisam ser confirmados na fonte municipal antes de uma conclusão jurídica definitiva.

As fontes e os limites técnicos estão descritos em [docs/FONTES_E_LIMITES.md](docs/FONTES_E_LIMITES.md).

## Para colaborar

Antes de alterar a coleta, eu peço que o desenvolvedor:

1. leia `docs/ARQUITETURA.md` e `docs/CONTINUIDADE_MOGI.md`;
2. preserve `cadastro_id` e `id_local` como identificadores diferentes;
3. não altere o checkpoint manualmente;
4. escreva no banco primeiro e gere as exportações a partir dele;
5. mantenha a origem e a data em qualquer informação fiscal importada;
6. execute os testes e a compilação de sintaxe antes de entregar.
