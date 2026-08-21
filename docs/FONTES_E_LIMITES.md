# Fontes e limites dos dados

Este documento registra o que cada fonte realmente fornece. A separação evita apresentar um `id_local` como unidade individual, um ponto de quadra como lote exato ou um valor venal como certidão fiscal.

## Mogi das Cruzes

### Cadastro imobiliário

Uso o pacote oficial `cadastro-imobiliario`:

- [API do pacote de cadastro imobiliário](https://dados.mogidascruzes.sp.gov.br/api/3/action/package_show?id=cadastro-imobiliario)

Os 26 arquivos `pmmc_imobiliario_2024_partN.csv` publicam 233.377 cadastros individuais. O campo `cadastro_id` é mantido como identificador do cadastro. O campo `id_local` é preservado separadamente e pode aparecer em várias unidades.

O exemplo oficial mostra o mesmo `id_local` `01-004811-001` associado a `cadastro_id` diferentes e complementos como `unid.001` e `unid.002`. Por isso o programa não reduz mais o cadastro pelo `id_local`.

O portal não inclui um dicionário que confirme o significado de cada trecho numérico do `id_local`. O programa não atribui automaticamente ao sufixo o papel de subunidade. A unidade é diferenciada pelos campos efetivamente publicados.

### Valores venais

Os arquivos `pmmc_iptu_ANO_partN.csv` fornecem `cadastro_id`, `id_local`, valor venal do terreno e valor venal da construção. O vínculo é feito por `cadastro_id`, que também aparece nos arquivos anuais de IPTU/ITBI.

Na execução validada em 20 de agosto de 2026:

- 177.689 cadastros atuais possuem algum valor venal;
- 160.366 usam valor do exercício de 2024;
- 17.323 usam o exercício histórico mais recente;
- 55.688 não possuem valor nas bases consultadas;
- o histórico de 2016 a 2024 possui 1.382.284 linhas por cadastro e exercício.

O IPTU tem prioridade. O ITBI somente preenche o mesmo cadastro e exercício quando não existe registro de IPTU. Campos de lançamento, recolhimento, pagamento e guia são descartados.

O valor venal publicado não é certidão emitida e não demonstra presença ou ausência de pendência fiscal.

### Georreferência e contexto urbanístico

Uso as camadas públicas do GeoMogi:

- [Quadras](https://geomogi.mogidascruzes.sp.gov.br/mapa/quadra)
- [Logradouros](https://geomogi.mogidascruzes.sp.gov.br/mapa/logradouro)
- [Bairros](https://geomogi.mogidascruzes.sp.gov.br/mapa/bairro)
- [Distritos](https://geomogi.mogidascruzes.sp.gov.br/mapa/distrito)
- [Macrozonas](https://geomogi.mogidascruzes.sp.gov.br/mapa/macrozona)
- [Zoneamento](https://geomogi.mogidascruzes.sp.gov.br/mapa/zoneamento)

Cada cadastro herda a referência espacial de seu `id_local`. A prioridade é o centro da quadra; o centro do logradouro é usado quando a quadra não é encontrada. Isso georreferenciou 230.802 cadastros; 2.575 ficaram sem ponto.

O ponto é cruzado com bairro, distrito, macrozona e zoneamento. Os parâmetros urbanísticos publicados são preservados, mas descrevem o contexto do ponto aproximado. Eles devem ser confirmados no município antes de uma conclusão jurídica sobre o lote.

A camada de lote referenciada pelo GeoMogi respondeu `Usuário Não autorizado`. O projeto não tenta contornar essa restrição.

## Itanhaém

Uso o TileJSON público de arruamento:

- [TileJSON de arruamento](https://www2.itanhaem.sp.gov.br/ortofoto2012/mapa/tileserver.php?/arruamento.json)
- [Índice público do TileServer](https://www2.itanhaem.sp.gov.br/ortofoto2012/mapa/tileserver.php?/index.json)

As feições representam ruas e trechos viários. O `gid` não é cadastro imobiliário, inscrição, lote ou identificação de proprietário.

## Dados que não são coletados

O fluxo ativo não baixa boletos e não armazena CPF, CNPJ, nome de proprietário, linha digitável, pagamento ou guia. Importações locais de certidão ou situação fiscal ficam separadas da base pública e rejeitam essas colunas.
